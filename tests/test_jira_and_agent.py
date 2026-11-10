import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from shadowdev.automation.agent import ClaudeSession
from shadowdev.clients.jira import Jira


class FakeWorkflowJira(Jira):
    """MAR's workflow: Backlog -> To Do -> In Progress -> In Review, plus side exits."""

    WORKFLOW = {
        "Backlog": ["Invalid", "On Hold", "To Do", "Ready for Pickup"],
        "To Do": ["In Progress", "On Hold", "Backlog"],
        "In Progress": ["In Review", "To Do", "On Hold"],
        "In Review": ["Ready for QA", "In Progress", "To Do"],
        "Reopened": ["In Progress", "To Do"],
        "On Hold": ["To Do"],
    }

    def __init__(self, status):
        self.current = status
        self.calls = []

    def status(self, key):
        return self.current

    def transitions(self, key):
        return [{"id": name, "name": name, "to": {"name": name}} for name in self.WORKFLOW.get(self.current, [])]

    def transition(self, key, transition_id):
        self.calls.append(transition_id)
        self.current = transition_id


class MoveAlongTest(unittest.TestCase):
    START = ["To Do", "In Progress"]

    def test_walks_one_transition_at_a_time(self):
        jira = FakeWorkflowJira("Backlog")
        self.assertEqual(jira.move_along("MAR-1", self.START), ("In Progress", ["To Do", "In Progress"]))
        self.assertEqual(jira.move_along("MAR-1", ["In Review"]), ("In Review", ["In Review"]))

    def test_takes_the_shortest_way_and_never_moves_back(self):
        jira = FakeWorkflowJira("Reopened")  # Reopened -> In Progress directly, not via To Do
        self.assertEqual(jira.move_along("MAR-1", self.START), ("In Progress", ["In Progress"]))
        jira = FakeWorkflowJira("In Progress")  # already there: no move back to To Do
        self.assertEqual(jira.move_along("MAR-1", self.START), ("In Progress", []))

    def test_stops_where_the_workflow_has_no_way_forward(self):
        jira = FakeWorkflowJira("Ready for QA")
        self.assertEqual(jira.move_along("MAR-1", self.START), ("Ready for QA", []))


class StreamingClaudeTest(unittest.TestCase):
    """ClaudeSession against a stand-in `claude` that prints stream-json events."""

    def fake_claude(self, events):
        bin_dir = Path(tempfile.mkdtemp())
        script = bin_dir / "claude"
        lines = "\n".join(json.dumps(e) for e in events)
        script.write_text(f"#!/bin/sh\ncat > /dev/null\ncat <<'EOF'\n{lines}\nEOF\n")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        return {"PATH": f"{bin_dir}:{os.environ['PATH']}"}

    def test_logs_each_step_and_returns_the_answer(self):
        env = self.fake_claude([
            {"type": "system", "subtype": "init"},
            {"type": "system", "subtype": "api_retry", "message": "Overloaded, retrying in 2s"},
            {"type": "assistant", "message": None},
            ["not", "an", "event"],
            {"type": "assistant", "message": {"content": [
                {"type": "text", "text": "Looking at the download route."},
                {"type": "tool_use", "name": "Read", "input": {"file_path": "/ws/services/app/documents.py"}},
            ]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "is_error": True, "content": "Output does not match required schema"}]}},
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "StructuredOutput", "input": {"summary": "x"}}]}},
            {"type": "result", "is_error": False, "session_id": "s-1", "num_turns": 3,
             "structured_output": {"summary": "Fixed the header."}},
        ])
        session = ClaudeSession(Path(tempfile.mkdtemp()), env, label="MAR-9")
        with self.assertLogs("shadowdev", "INFO") as logs:
            answer = session.ask("prompt", "Read", {"type": "object"})
        self.assertEqual(answer, {"summary": "Fixed the header."})
        self.assertEqual(session.session_id, "s-1")
        text = "\n".join(logs.output)
        for expected in ("MAR-9: claude api_retry: Overloaded, retrying in 2s",
                         "MAR-9: claude says: Looking at the download route.",
                         "MAR-9: claude → Read /ws/services/app/documents.py",
                         "MAR-9: claude's step was rejected: Output does not match required schema",
                         "MAR-9: claude submits its answer",
                         "MAR-9: claude done in"):
            self.assertIn(expected, text)

    def test_the_model_and_effort_are_passed_to_claude(self):
        bin_dir = Path(tempfile.mkdtemp())
        script = bin_dir / "claude"
        result = json.dumps({"type": "result", "is_error": False, "session_id": "s-1", "structured_output": {}})
        script.write_text(f"#!/bin/sh\ncat > /dev/null\necho \"$@\" > {bin_dir}/args\necho '{result}'\n")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}
        ClaudeSession(Path(tempfile.mkdtemp()), env, model="sonnet", effort="medium").ask("p", "Read", {})
        self.assertIn("--model sonnet --effort medium", (bin_dir / "args").read_text())
        ClaudeSession(Path(tempfile.mkdtemp()), env).ask("p", "Read", {})  # the account's default model
        self.assertNotIn("--model", (bin_dir / "args").read_text())

    def test_errors_are_raised(self):
        env = self.fake_claude([{"type": "result", "is_error": True, "result": "Not logged in"}])
        with self.assertRaisesRegex(RuntimeError, "Not logged in"):
            ClaudeSession(Path(tempfile.mkdtemp()), env).ask("p", "Read", {})
        env = self.fake_claude([{"type": "system"}])  # exits without a result
        with self.assertRaisesRegex(RuntimeError, "without a result"):
            ClaudeSession(Path(tempfile.mkdtemp()), env).ask("p", "Read", {})


if __name__ == "__main__":
    unittest.main()
