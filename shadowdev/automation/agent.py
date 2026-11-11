import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from . import sandbox
from .cancel import NEVER, CancelToken
from ..config import Config
from ..models.user import ClaudeAccount

READ_ONLY_TOOLS = "Read,Glob,Grep"
EDIT_TOOLS = "Read,Glob,Grep,Edit,Write"
LOG_TEXT_CHARS = 400

TRIAGE_MODEL = "haiku"  # rates how hard an issue is, from its text alone
# What works on an issue of each difficulty: (--model, --effort). None: the Claude account's default.
MODELS = {"simple": ("haiku", None), "standard": ("sonnet", "medium"), "complex": (None, None)}
FIXED_MODELS = {"haiku": MODELS["simple"], "sonnet": MODELS["standard"], "default": MODELS["complex"]}  # Settings.model

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


def forget_claude_sessions(config_dir: Path, workspace: Path) -> None:
    """Delete the transcripts of Claude sessions run in `workspace` (they contain the code
    Claude read), from a developer's Claude config dir."""
    for project_dir in (config_dir / "projects").glob("*"):
        for transcript in project_dir.glob("*.jsonl"):
            with transcript.open() as lines:
                cwd = next((e.get("cwd") for e in map(_json_object, lines) if e.get("cwd")), None)
            if cwd and (cwd == str(workspace) or cwd.startswith(f"{workspace}/")):
                shutil.rmtree(project_dir, ignore_errors=True)
                break


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
    """

    def __init__(self, cwd: Path, env: dict, label: str = "claude", cfg: Config | None = None,
                 cancel: CancelToken = NEVER, model: str | None = None, effort: str | None = None):
        self.cwd = cwd
        self.env = env
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

    def ask(self, prompt: str, tools: str, schema: dict) -> dict:
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
        if self.session_id:
            cmd += ["--resume", self.session_id]
        cmd = in_sandbox(cmd, self.cfg, self.cwd, Path(self.env["CLAUDE_CONFIG_DIR"])) if self.cfg else cmd
        self.cancel.check()
        started = time.monotonic()
        timeout = self.cfg.claude_timeout_seconds if self.cfg else Config.claude_timeout_seconds
        result = None
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
                        else:
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
                raise RuntimeError(f"claude exited {proc.returncode} without a result: {stderr.read()[-2000:]}")
        if result.get("is_error"):
            raise RuntimeError(f"claude failed: {str(result.get('result'))[:2000]}")
        if "structured_output" not in result:
            raise RuntimeError(f"claude returned no structured output: {str(result.get('result'))[:2000]}")
        self.session_id = result["session_id"]
        log.info("%s: claude done in %.0fs (%s turns)", self.label, time.monotonic() - started, result.get("num_turns", "?"))
        return result["structured_output"]
