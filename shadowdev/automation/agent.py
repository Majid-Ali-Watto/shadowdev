import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from ..config import Config
from ..models.user import Actor, ClaudeAccount
from . import limits, runlog, sandbox
from .cancel import NEVER, CancelToken

READ_ONLY_TOOLS = "Read,Glob,Grep"
EDIT_TOOLS = "Read,Glob,Grep,Edit,Write"
LOG_TEXT_CHARS = 400

SMALL_MODEL_EFFORT = "high"  # Haiku and Sonnet always work at high effort
TRIAGE_MODEL = "haiku"  # rates how hard an issue is, from its text alone
# What works on an issue of each difficulty: (--model, --effort). None: the Claude account's default
# (the default model keeps its own recommended effort).
MODELS = {"simple": ("haiku", SMALL_MODEL_EFFORT), "standard": ("sonnet", SMALL_MODEL_EFFORT), "complex": (None, None)}
FIXED_MODELS = {"haiku": MODELS["simple"], "sonnet": MODELS["standard"], "default": MODELS["complex"]}  # Settings.model

# A request refused by a usage limit is waited for and continued in the same conversation.
LIMIT_ERROR_RE = re.compile(r"usage limit|session limit|weekly limit|rate.?limit|hit your limit", re.IGNORECASE)
CONTINUE = """Your work on the last request was interrupted before you finished. Continue it from where
you stopped, without redoing what is already done, and then give your answer."""

log = logging.getLogger("shadowdev")


def claude_config_dir(cfg: Config, account: ClaudeAccount | str) -> Path:
    """Each Claude account (or its config key) has its own config dir, so logins and sessions of a
    developer's personal and office Claude never mix."""
    key = account if isinstance(account, str) else account.config_key
    return cfg.data_dir / "claude" / key


def token_variable(token: str) -> str | None:
    """Environment variable the claude CLI reads this credential from."""
    if token.startswith("sk-ant-oat"):
        return "CLAUDE_CODE_OAUTH_TOKEN"  # from `claude setup-token` (Pro/Max/Team subscription)
    if token.startswith("sk-ant-api"):
        return "ANTHROPIC_API_KEY"  # Anthropic Console API key
    return None


def claude_env(token: str, config_dir: Path) -> dict:
    """Environment for running claude as one developer's Claude account. Its own config dir means
    the server's login is never used, and sessions stay separate per account. Nothing else from
    the server environment is passed on."""
    variable = token_variable(token)
    if not variable:
        raise ValueError("not a Claude token")
    config_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "TMPDIR") if k in os.environ}
    return {**env, "CLAUDE_CONFIG_DIR": str(config_dir), variable: token}


def claude_envs(cfg: Config, actor: Actor) -> list[dict]:
    """The environments of a run's Claude accounts: the one it works with, then those it may
    switch to at a usage limit (none unless the developer switched that on)."""
    return [claude_env(a.token, claude_config_dir(cfg, a)) for a in (actor.claude, *actor.spare_claudes)]


def config_dirs(envs: list[dict]) -> list[Path]:
    return [Path(env["CLAUDE_CONFIG_DIR"]) for env in envs]


def _run(cmd: list[str], prompt: str, cwd: Path, env: dict, timeout: int) -> dict:
    proc = subprocess.run(cmd, input=prompt, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"claude exited {proc.returncode}: {(proc.stderr or proc.stdout)[-2000:]}") from None
    if proc.returncode != 0 or result.get("is_error"):
        raise RuntimeError(f"claude failed: {str(result.get('result'))[:2000]}")
    return result


def in_sandbox(argv: list[str], cfg: Config | None, workspace: Path, config_dir: Path) -> list[str]:
    """Claude processes untrusted ticket text, so besides --restricted it runs in the sandbox:
    it can write only its workspace (not .git) and its own config dir, and sees nothing else
    of ShadowDev or the server's home."""
    if cfg is None:
        return argv
    return sandbox.wrap(argv, cfg, [workspace, config_dir], sandbox.git_dirs(workspace))


def check_token(token: str, config_dir: Path, cfg: Config | None = None) -> None:
    """One tiny request with the token; raises with Claude's error if it does not work."""
    with tempfile.TemporaryDirectory() as empty:
        try:
            argv = ["claude", "-p", "--output-format", "json", "--restricted", "--strict-mcp-config", "--tools", ""]
            _run(in_sandbox(argv, cfg, Path(empty), config_dir), "Reply with OK.", Path(empty),
                 claude_env(token, config_dir), timeout=120)
        finally:
            forget_claude_sessions(config_dir, Path(empty))


def session_dirs(config_dir: Path, workspace: Path) -> list[Path]:
    """The folders of a Claude config dir that hold the transcripts of sessions run in `workspace`."""
    found = []
    for project_dir in (config_dir / "projects").glob("*"):
        for transcript in project_dir.glob("*.jsonl"):
            with transcript.open() as lines:
                cwd = next((e.get("cwd") for e in map(_json_object, lines) if e.get("cwd")), None)
            if cwd and (cwd == str(workspace) or cwd.startswith(f"{workspace}/")):
                found.append(project_dir)
                break
    return found


def forget_claude_sessions(config_dir: Path, workspace: Path) -> None:
    """Delete the transcripts of Claude sessions run in `workspace` (they contain the code
    Claude read), from a developer's Claude config dir."""
    for project_dir in session_dirs(config_dir, workspace):
        shutil.rmtree(project_dir, ignore_errors=True)


def _json_object(line: str) -> dict:
    try:
        value = json.loads(line)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _one_line(text: str, limit: int = LOG_TEXT_CHARS) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit] + "…"


class ClaudeSession:
    """One headless Claude Code conversation, confined to a workspace, as one developer.

    Claude reads, edits and creates files itself (Edit/Write, auto-accepted). The ticket
    text is untrusted input, so Claude runs in --restricted mode with only file tools: it
    cannot run commands, reach the network, or touch git. Setup, tests, git, Jira and the
    PR are all handled by the pipeline, not by the model.

    Claude's output is streamed: what it says, each file it reads, searches, edits or
    creates, and rejected answers are logged as they happen, prefixed with `label`.

    A request that the account's usage limit refuses is not lost: the session goes on in the
    same conversation, with one of `spares` (the environments of the developer's other Claude
    accounts, when they switched that on) that is not at its limit, or else after waiting for
    the reset when it comes within limits.RESETS_SOON (see limits.py).
    """

    def __init__(self, cwd: Path, env: dict, label: str = "claude", cfg: Config | None = None,
                 cancel: CancelToken = NEVER, model: str | None = None, effort: str | None = None,
                 spares: list[dict] | None = None):
        self.cwd = cwd
        self.env = env
        self.spares = spares if spares is not None else []  # shared by the sessions of a run
        self.label = label
        self.cfg = cfg  # set: run in the sandbox
        self.cancel = cancel
        self.model = model  # None: the Claude account's default
        self.effort = effort
        self.session_id: str | None = None

    def _log_event(self, event: dict) -> None:
        message = event.get("message")
        if isinstance(message, str):  # e.g. a system notice such as an API retry
            if message.strip():
                log.info("%s: claude %s: %s", self.label, event.get("subtype") or event.get("type", "?"),
                         _one_line(message, 300))
            return
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            return
        for part in content:
            if not isinstance(part, dict):
                continue
            if event.get("type") == "assistant" and part.get("type") == "text" and part.get("text", "").strip():
                log.info("%s: claude says: %s", self.label, _one_line(part["text"]))
            elif event.get("type") == "assistant" and part.get("type") == "tool_use":
                name, args = part.get("name", "?"), part.get("input") or {}
                if name == "StructuredOutput":
                    log.info("%s: claude submits its answer", self.label)
                else:
                    target = args.get("file_path") or args.get("pattern") or args.get("path") or ""
                    log.info("%s: claude → %s %s", self.label, name, _one_line(target, 200))
            elif event.get("type") == "user" and part.get("type") == "tool_result" and part.get("is_error"):
                text = part.get("content")
                log.warning("%s: claude's step was rejected: %s", self.label,
                            _one_line(text if isinstance(text, str) else json.dumps(text), 300))

    def _switch_account(self) -> bool:
        """Go on with a spare Claude account that is not at its limit, taking this run's
        conversations along. False when there is none."""
        now = time.time()
        for n, env in enumerate(self.spares):
            if limits.USAGE.held(env["CLAUDE_CONFIG_DIR"], now):
                continue
            old, new = Path(self.env["CLAUDE_CONFIG_DIR"]), Path(env["CLAUDE_CONFIG_DIR"])
            for folder in session_dirs(old, self.cwd):
                shutil.copytree(folder, new / "projects" / folder.name, dirs_exist_ok=True)
            self.spares[n], self.env = self.env, env  # the one left is skipped while it is at its limit
            message = "its Claude account reached its limit; going on with another of your Claude accounts"
            log.info("%s: %s", self.label, message)
            limits.tell(message)
            return True
        return False

    def ask(self, prompt: str, tools: str, schema: dict) -> dict:
        resume = self.session_id
        while True:
            account = self.env.get("CLAUDE_CONFIG_DIR", "")
            result, working, error = self._request(prompt, tools, schema, resume, account)
            if not error:
                break
            until = limits.USAGE.refused(account, time.time(), bool(LIMIT_ERROR_RE.search(error)))
            if until is None:
                raise RuntimeError(error)
            if not self._switch_account():
                if until - time.time() > limits.RESETS_SOON:
                    raise RuntimeError(error)
                limits.wait_for_reset(until, self.cancel, self.label)
            if working:  # Claude had begun: it continues that conversation instead of starting over
                prompt, resume = CONTINUE, working
        self.session_id = result["session_id"]
        return result["structured_output"]

    def _request(self, prompt: str, tools: str, schema: dict, resume: str | None, account: str
                 ) -> tuple[dict | None, str | None, str | None]:
        """One call of claude. Returns (its result, the session Claude was working in when it
        failed, the error): a result without an error, or an error."""
        cmd = [
            "claude", "-p",
            "--output-format", "stream-json", "--verbose",
            "--restricted", "--strict-mcp-config",
            "--tools", tools,
            "--permission-mode", "acceptEdits",
            "--json-schema", json.dumps(schema),
        ]
        if self.model:
            cmd += ["--model", self.model]
        if self.effort:
            cmd += ["--effort", self.effort]
        if resume:
            cmd += ["--resume", resume]
        cmd = in_sandbox(cmd, self.cfg, self.cwd, Path(self.env["CLAUDE_CONFIG_DIR"])) if self.cfg else cmd
        self.cancel.check()
        started = time.monotonic()
        timeout = self.cfg.claude_timeout_seconds if self.cfg else Config.claude_timeout_seconds
        result, working = None, None
        with tempfile.TemporaryFile("w+") as stderr:
            proc = subprocess.Popen(cmd, cwd=self.cwd, env=self.env, text=True,
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr)
            watchdog = threading.Timer(timeout, proc.kill)
            watchdog.start()
            with self.cancel.tracking(proc):
                try:
                    proc.stdin.write(prompt)
                    proc.stdin.close()
                    for line in proc.stdout:
                        try:
                            event = json.loads(line)
                        except ValueError:
                            continue
                        if not isinstance(event, dict):
                            continue
                        if event.get("type") == "result":
                            result = event
                        elif event.get("type") == "rate_limit_event":
                            limits.USAGE.record(account, event.get("rate_limit_info") or {})
                        else:
                            if event.get("type") == "assistant":
                                working = event.get("session_id") or working
                            self._log_event(event)
                    proc.wait()
                except BrokenPipeError:
                    proc.wait()  # killed before it read the prompt
                finally:
                    watchdog.cancel()
                    if proc.poll() is None:
                        proc.kill()
                        proc.wait()
                    proc.stdout.close()
            self.cancel.check()
            if result is None:
                if time.monotonic() - started >= timeout:
                    raise RuntimeError(f"claude timed out after {timeout}s")
                stderr.seek(0)
                return None, working, f"claude exited {proc.returncode} without a result: {stderr.read()[-2000:]}"
        runlog.add_usage(result.get("total_cost_usd"), result.get("num_turns"))
        if result.get("is_error"):
            return None, working, f"claude failed: {str(result.get('result'))[:2000]}"
        if "structured_output" not in result:
            raise RuntimeError(f"claude returned no structured output: {str(result.get('result'))[:2000]}")
        log.info("%s: claude done in %.0fs (%s turns)", self.label, time.monotonic() - started, result.get("num_turns", "?"))
        return result, None, None
