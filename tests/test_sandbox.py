"""The sandbox's own rules: package caches per project, the git dirs it mounts, the resource caps (through
systemd when it can give a scope, and nothing when it can't), tmpfs sizes where bwrap has them, and that
every caller passes the config (a sandbox is never skipped for lack of one)."""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from shadowdev import config as settings
from shadowdev.automation import agent, gitops, sandbox, verify
from tests.helpers import make_cfg


class CachesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = make_cfg(self.tmp, sandbox_caches=(".npm",))
        for run in ("1-PROJ-1", "2-PROJ-1"):
            (self.cfg.worktree_root / run / "app").mkdir(parents=True)

    def test_each_project_gets_its_own_cache_folder(self):
        cwd = self.cfg.worktree_root / "1-PROJ-1" / "app"
        ok, output = verify.run_command(cwd, 'echo poisoned > "$HOME/.npm/marker"', self.cfg, "test command")
        self.assertTrue(ok, output)
        self.assertTrue((self.cfg.cache_root / "1" / ".npm" / "marker").exists())
        other = self.cfg.worktree_root / "2-PROJ-1" / "app"
        ok, output = verify.run_command(other, 'ls "$HOME/.npm" && cat "$HOME/.npm/marker"; true', self.cfg, "test command")
        self.assertNotIn("poisoned", output)  # the other project's commands never see it
        self.assertFalse((self.cfg.cache_root / "2" / ".npm" / "marker").exists())

    def test_a_run_folder_name_gives_its_projects_key(self):
        self.assertEqual(sandbox.cache_key(Path("/w/12-MAR-1")), "12")
        self.assertIsNone(sandbox.cache_key(Path("/w/MAR-1")))  # not a run of a project

    def test_a_command_outside_any_run_gets_no_cache_and_nothing_writable(self):
        outside = Path(tempfile.mkdtemp())
        argv = sandbox.wrap_command("true", self.cfg, outside)
        self.assertNotIn(str(self.cfg.cache_root), " ".join(argv))
        self.assertNotIn("--bind", argv)

    def test_the_git_dirs_of_a_run_are_mounted_read_only_at_their_own_path_and_in_the_clone(self):
        workspace = self.cfg.worktree_root / "1-PROJ-1"
        gits = gitops.gitdirs_of(workspace)
        (gits / "app").mkdir(parents=True)
        mounts = sandbox.git_mounts(workspace)
        self.assertIn((gits / "app", gits / "app"), mounts)
        self.assertIn((gits / "app", workspace / "app" / ".git"), mounts)  # only for a clone that exists
        self.assertEqual(sandbox.git_mounts(self.cfg.worktree_root / "9-PROJ-1"), [])


class LimitsTest(unittest.TestCase):
    def test_without_systemd_a_scope_there_is_no_limit_and_nothing_fails(self):
        cfg = make_cfg(Path(tempfile.mkdtemp()))
        with mock.patch.object(sandbox, "_scope_works", return_value=False):
            self.assertEqual(sandbox.limits_prefix(cfg), [])
            ok, output = verify.run_command(Path(tempfile.mkdtemp()), "echo still-runs", cfg, "test command")
        self.assertTrue(ok, output)
        self.assertIn("still-runs", output)

    def test_a_limit_of_zero_is_off(self):
        cfg = make_cfg(Path(tempfile.mkdtemp()), sandbox_memory_mb=0, sandbox_tasks_max=0)
        with mock.patch.object(sandbox, "_scope_works", return_value=True):
            self.assertEqual(sandbox.limits_prefix(cfg), [])

    def test_a_command_runs_in_a_scope_with_its_memory_cap_when_systemd_gives_one(self):
        if not sandbox._scope_works():
            self.skipTest("systemd gives no user scope here")
        cfg = make_cfg(Path(tempfile.mkdtemp()), sandbox_memory_mb=512, sandbox_tasks_max=64)
        command = 'cat "/sys/fs/cgroup$(cut -d: -f3 /proc/self/cgroup)/memory.max"'
        ok, output = verify.run_command(Path(tempfile.mkdtemp()), command, cfg, "test command")
        self.assertTrue(ok, output)
        self.assertEqual(output.strip(), str(512 * 1024**2))

    def test_the_limit_prefix_runs_the_command_outside_the_sandbox(self):
        cfg = make_cfg(Path(tempfile.mkdtemp()))
        with mock.patch.object(sandbox, "_scope_works", return_value=True):
            argv = sandbox.limits_prefix(cfg)
        self.assertEqual(argv[:4], ["systemd-run", "--user", "--scope", "--quiet"])
        self.assertIn("MemoryMax=4096M", argv)
        self.assertIn("TasksMax=2048", argv)

    def test_the_bus_is_unset_inside_the_sandbox(self):
        cfg = make_cfg(Path(tempfile.mkdtemp()))
        argv = sandbox.wrap(["true"], cfg, [])
        for name in sandbox.BUS_ENV:
            self.assertEqual(argv[argv.index(name) - 1], "--unsetenv")


class TmpfsSizeTest(unittest.TestCase):
    def setUp(self):
        sandbox.bwrap_supports_size.cache_clear()
        self.addCleanup(sandbox.bwrap_supports_size.cache_clear)

    def version(self, text):
        with mock.patch.object(sandbox.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, text, "")):
            sandbox.bwrap_supports_size.cache_clear()
            return sandbox.bwrap_supports_size()

    def test_bwrap_ten_and_later_size_the_tmpfs_others_do_not(self):
        self.assertFalse(self.version("bubblewrap 0.8.0\n"))  # Docker's bookworm
        self.assertTrue(self.version("bubblewrap 0.10.0\n"))
        self.assertTrue(self.version("bubblewrap 0.11.1\n"))
        self.assertFalse(self.version(""))

    def test_the_tmpfs_gets_its_size_only_when_bwrap_takes_one(self):
        cfg = make_cfg(Path(tempfile.mkdtemp()), sandbox_tmp_mb=100)
        with mock.patch.object(sandbox, "bwrap_supports_size", return_value=True):
            argv = sandbox.wrap(["true"], cfg, [])
        tmp = argv.index("/tmp")  # noqa: S108 (a bwrap argument, not a file)
        self.assertEqual(argv[tmp - 3:tmp], ["--size", str(100 * 1024**2), "--tmpfs"])
        with mock.patch.object(sandbox, "bwrap_supports_size", return_value=False):
            argv = sandbox.wrap(["true"], cfg, [])
        self.assertNotIn("--size", argv)


class RequiredConfigTest(unittest.TestCase):
    def test_claude_is_never_started_without_its_config(self):
        with self.assertRaises(TypeError):
            agent.ClaudeSession(Path(tempfile.mkdtemp()), {"CLAUDE_CONFIG_DIR": "x"})  # no cfg: no sandbox, refused
        with self.assertRaises(TypeError):
            agent.check_token("sk-ant-oat01-x", Path(tempfile.mkdtemp()))

    def test_limits_from_the_environment_are_whole_numbers(self):
        self.assertEqual(settings._cap(None, 4096, "SANDBOX_MEMORY_MB"), 4096)
        self.assertEqual(settings._cap("0", 4096, "SANDBOX_MEMORY_MB"), 0)
        with self.assertRaises(SystemExit):
            settings._cap("4G", 4096, "SANDBOX_MEMORY_MB")


if __name__ == "__main__":
    unittest.main()
