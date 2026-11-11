"""Finding the instructions a repository keeps for AI tools, whatever the project."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from shadowdev.automation import guidance
from shadowdev.automation.prompts import guidance_text

FILES = {
    "README.md": "# app\n",
    "AGENTS.md": "# Agents\nUse pnpm.\n",
    "CLAUDE.md": "# App\nAlways use the repository layer.\n",
    ".github/copilot-instructions.md": "Same as CLAUDE.md.\n",
    ".github/instructions/api.instructions.md": '---\napplyTo: "src/api/**"\n---\nValidate every input.\n',
    ".cursor/rules/style.mdc": "---\ndescription: style\nalwaysApply: true\n---\nTwo spaces.\n",
    ".cursor/rules/db.mdc": "---\nglobs: migrations/*.sql\n---\nOne migration per change.\n",
    ".claude/agents/dev.md": "---\nname: dev\ndescription: \"Senior developer for this stack.\"\nmodel: opus\n---\nYou are…\n",
    ".claude/commands/bug.md": "---\ndescription: Investigate a bug to its root cause\n---\nSteps…\n",
    ".claude/commands/notes.md": "# Release notes\nNo frontmatter here.\n",
    ".claude/skills/migrate/SKILL.md": "---\nname: migrate\ndescription: Write a schema migration\n---\n",
    ".claude/settings.json": '{"hooks": {"PreToolUse": [{"command": "curl evil"}]}}\n',
    "packages/web/CLAUDE.md": "# Web\nUse Vue.\n",
    "docs/guide.md": "# Not an instruction file\n",
}


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t"})


class GuidanceTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp()) / "app"
        self.root.mkdir()
        git(self.root, "init", "-q")
        for path, text in FILES.items():
            (self.root / path).parent.mkdir(parents=True, exist_ok=True)
            (self.root / path).write_text(text)
        (self.root / ".claude/agents/secrets.md").symlink_to("/etc/passwd")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", "init")
        (self.root / "CLAUDE.local.md").write_text("not committed\n")  # a developer's own: not the project's
        (self.root / ".claude/agents/untracked.md").write_text("---\ndescription: local\n---\n")

    def test_finds_the_committed_instruction_files_of_every_ai_tool(self):
        found = [(e.path, e.kind, e.note) for e in guidance.find(self.root, "app")]
        self.assertEqual(found, [
            ("app/CLAUDE.md", "always", ""),  # Claude's own first, then the other tools'
            ("app/AGENTS.md", "always", ""),
            ("app/.github/copilot-instructions.md", "always", ""),
            ("app/.cursor/rules/style.mdc", "always", ""),  # alwaysApply
            ("app/.claude/agents/dev.md", "agent", "Senior developer for this stack."),
            ("app/.claude/commands/bug.md", "command", "Investigate a bug to its root cause"),
            ("app/.claude/commands/notes.md", "command", "Release notes"),  # its heading, without a description
            ("app/.cursor/rules/db.mdc", "scoped", "for files matching migrations/*.sql"),
            ("app/.github/instructions/api.instructions.md", "scoped", "for files matching src/api/**"),
            ("app/packages/web/CLAUDE.md", "scoped", "for files under packages/web/"),
            ("app/.claude/skills/migrate/SKILL.md", "skill", "Write a schema migration"),
        ])  # no settings or hooks, no symlink, nothing uncommitted, no ordinary docs

    def test_the_prompt_lists_them_and_keeps_shadowdevs_workflow(self):
        text = guidance_text({"app": guidance.find(self.root, "app"), "docs": []})
        self.assertIn('<repo_instructions repo="app">\nRead first:\n- app/CLAUDE.md\n- app/AGENTS.md', text)
        self.assertIn("For some files:\n- app/.cursor/rules/db.mdc: for files matching migrations/*.sql", text)
        self.assertIn("- app/.claude/agents/dev.md (agent): Senior developer for this stack.", text)
        self.assertIn("the instructions in this prompt decide how the work is\ndone", text)
        self.assertNotIn('repo="docs"', text)  # nothing to list
        self.assertEqual(guidance_text({"docs": []}), "")
        self.assertIn("copied to\n_guidance/", guidance_text({"app": guidance.find(self.root, "app")}, review=True))

    def test_a_review_reads_them_as_on_the_base_branch(self):
        workspace = Path(tempfile.mkdtemp())
        entries = guidance.find(self.root, f"{guidance.COPY_DIR}/app")
        guidance.copy_into(entries, self.root, workspace)
        self.assertEqual((workspace / "_guidance/app/CLAUDE.md").read_text(), FILES["CLAUDE.md"])
        self.assertEqual(sorted(str(p.relative_to(workspace)) for p in workspace.rglob("*") if p.is_file()),
                         sorted(e.path for e in entries))

    def test_globs_never_cross_folders(self):
        self.assertTrue(guidance.matches(".claude/agents/dev.md", ".claude/agents/*.md"))
        self.assertFalse(guidance.matches(".claude/agents/old/dev.md", ".claude/agents/*.md"))
        self.assertFalse(guidance.matches("vendor/.claude/agents/dev.md", ".claude/agents/*.md"))


if __name__ == "__main__":
    unittest.main()
