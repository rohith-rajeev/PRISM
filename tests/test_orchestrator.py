"""Concurrency and engine-protocol tests for orchestrator.py.

Stdlib unittest, no Tkinter and no network — safe to run in CI. These cover
the two hazards that make parallel jobs possible at all: pinning each agent
conversation to its own session, and serialising mutations of a shared clone.

    python3 -m unittest discover -s tests -v
"""
import json
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import orchestrator as o  # noqa: E402


def jline(**kw):
    """One engine event, in the shape a live `run --format json` emits."""
    return json.dumps(kw)


def text_event(sid, text, part_id="prt_1"):
    """One *complete* text part, which is what the engine actually emits — an
    844-character answer arrived in a single event, not as token deltas."""
    return jline(type="text", timestamp=1, sessionID=sid,
                 part={"type": "text", "text": text, "id": part_id,
                       "sessionID": sid})


def tool_event(sid, tool, title, status="completed"):
    return jline(type="tool_use", timestamp=1, sessionID=sid,
                 part={"type": "tool", "tool": tool, "callID": "c1",
                       "state": {"status": status, "title": title,
                                 "input": {"command": title}}})


class EventShape(unittest.TestCase):
    """Shapes verified against a real `opencode run --format json`."""

    def test_session_id_from_top_level_or_part(self):
        self.assertEqual(o._event_session_id(json.loads(
            jline(type="x", sessionID="ses_a"))), "ses_a")
        self.assertEqual(o._event_session_id(json.loads(
            jline(type="x", part={"sessionID": "ses_b"}))), "ses_b")
        self.assertIsNone(o._event_session_id({"type": "x"}))

    def test_text_only_from_text_parts(self):
        self.assertEqual(o._event_text(json.loads(text_event("s", "hi"))), "hi")
        self.assertIsNone(o._event_text(json.loads(
            jline(type="step_start", part={"type": "step-start"}))))

    def test_tool_events_become_one_readable_line(self):
        ev = json.loads(tool_event("s", "bash", "git diff --stat"))
        self.assertEqual(o._event_tool(ev), "⚙ bash: git diff --stat")

    def test_failed_tool_is_marked(self):
        ev = json.loads(tool_event("s", "bash", "git push", status="error"))
        self.assertTrue(o._event_tool(ev).startswith("✗ bash"))

    def test_tool_detail_falls_back_to_input_and_is_bounded(self):
        ev = json.loads(jline(type="tool_use", part={
            "type": "tool", "tool": "read",
            "state": {"status": "completed", "input": {"filePath": "x" * 400}}}))
        line = o._event_tool(ev)
        self.assertTrue(line.startswith("⚙ read: "))
        self.assertLessEqual(len(line), 180, "a huge argument must not flood the log")

    def test_non_tool_events_are_not_tools(self):
        self.assertIsNone(o._event_tool(json.loads(text_event("s", "hi"))))


class StreamDecoding(unittest.TestCase):
    """_run_stream's JSON mode, driven through a real subprocess."""

    def _run(self, payload, json_mode=True):
        emitted = []
        rc, text, sid = o._run_stream(
            [sys.executable, "-c",
             "import sys; sys.stdout.write(sys.argv[1])", payload],
            cwd=".", emit=lambda m, tag=None: emitted.append((m, tag)),
            json_mode=json_mode)
        return rc, text, sid, emitted

    def test_captures_session_and_emits_prose_as_agent_text(self):
        sid = "ses_f549009ebffeP4JgANr76DIva5"
        report = "**Verdict:** OK Approve\n**Impact score:** 3/10 - small"
        payload = (jline(type="step_start", sessionID=sid,
                         part={"type": "step-start"}) + "\n"
                   + text_event(sid, report) + "\n")
        rc, text, got, emitted = self._run(payload)
        self.assertEqual(rc, 0)
        self.assertEqual(got, sid)
        self.assertEqual(text, report)
        self.assertIn(("**Verdict:** OK Approve", "agent"), emitted)

    def test_consecutive_parts_do_not_run_together(self):
        """Separate replies are separate messages; without a break between
        them the transcript reads as one unbroken paragraph."""
        sid = "ses_x"
        payload = (text_event(sid, "I have the full diff.", "prt_a") + "\n"
                   + text_event(sid, "Now let me verify the context.", "prt_b") + "\n")
        _rc, text, _sid, _e = self._run(payload)
        self.assertIn("diff.\nNow let me", text)
        self.assertNotIn("diff.Now let me", text)

    def test_tool_activity_reaches_the_log(self):
        """Most of a review is reading files and running git; without these
        lines the UI looks stalled."""
        sid = "ses_x"
        payload = (tool_event(sid, "bash", "git diff --stat") + "\n"
                   + text_event(sid, "Two commits, 7 files.") + "\n")
        _rc, _text, _sid, emitted = self._run(payload)
        self.assertIn(("⚙ bash: git diff --stat", "tool"), emitted)
        self.assertIn(("Two commits, 7 files.", "agent"), emitted)

    def test_tool_lines_stay_out_of_the_parsed_report(self):
        sid = "ses_x"
        payload = (tool_event(sid, "bash", "git log") + "\n"
                   + text_event(sid, "**Verdict:** OK Approve") + "\n")
        _rc, text, _sid, _e = self._run(payload)
        self.assertNotIn("git log", text, "tool chatter must not reach the parser")
        self.assertEqual(o.parse_review_output(text).verdict_key, "approve")

    def test_parses_to_the_same_verdict_as_the_markdown_path(self):
        report = ("## PR #7\n**Verdict:** OK Approve with comments\n"
                  "**Impact score:** 6/10 - shared auth\n"
                  "- **[High] security - `a.py:1`** bad\n")
        payload = text_event("ses_x", report) + "\n"
        _rc, text, _sid, _e = self._run(payload)
        a = o.parse_review_output(text)
        b = o.parse_review_output(report)
        self.assertEqual((a.verdict_key, a.impact_score, a.findings),
                         (b.verdict_key, b.impact_score, b.findings))

    def test_repeated_part_is_not_duplicated(self):
        """Guard for a build that streams cumulative updates per part."""
        sid = "ses_x"
        payload = (text_event(sid, "Hello", "prt_a") + "\n"
                   + text_event(sid, "Hello world", "prt_a") + "\n")
        _rc, text, _sid, _e = self._run(payload)
        self.assertEqual(text, "Hello world")

    def test_malformed_stream_refuses_the_session_id(self):
        """Any surprise means the id may not belong to this conversation."""
        payload = text_event("ses_x", "hello") + "\nthis is not json\n"
        _rc, _text, sid, _e = self._run(payload)
        self.assertIsNone(sid, "a malformed line must invalidate the session id")

    def test_plain_mode_passes_text_through_verbatim(self):
        """The fallback must not reflow output: extract_question() splits
        question blocks on blank lines, so dropping them would silently break
        agent-question detection in the degraded path."""
        raw = "Need input.\n\nWhich branch should I diff against main?\n"
        _rc, text, sid, _e = self._run(raw, json_mode=False)
        self.assertEqual(text, raw)
        self.assertIsNone(sid)
        self.assertTrue(o.extract_question(text),
                        "blank-line structure must survive the plain path")


class PathLocks(unittest.TestCase):
    def test_same_path_serialises_and_different_paths_do_not(self):
        self.assertIs(o._lock_for("/tmp/x"), o._lock_for("/tmp/./x"),
                      "the same real directory must map to one lock")
        self.assertIsNot(o._lock_for("/tmp/x"), o._lock_for("/tmp/y"))

    def test_concurrent_sync_on_one_clone_does_not_interleave(self):
        overlap = []
        active = []
        guard = threading.Lock()

        def fake_sync(path):
            with o._lock_for(path):
                with guard:
                    active.append(path)
                    overlap.append(len(active))
                time.sleep(0.05)
                with guard:
                    active.remove(path)

        threads = [threading.Thread(target=fake_sync, args=("/tmp/sameclone",))
                   for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(max(overlap), 1, "two syncs ran on one clone at once")


class LockDiscipline(unittest.TestCase):
    """The invariant that keeps deadlock structurally impossible."""

    def test_sync_helper_never_asks_the_user(self):
        import inspect
        src = inspect.getsource(o.sync_destination_into_source)
        src += inspect.getsource(o._sync_locked)
        self.assertNotIn("ask(", src,
                         "a lock must never be held across a human wait")

    def test_only_one_lock_is_taken_per_call_path(self):
        import inspect
        for fn in (o._sync_locked, o._continue_turn):
            src = inspect.getsource(fn)
            self.assertLessEqual(src.count("_lock_for("), 1,
                                 f"{fn.__name__} may hold at most one lock")


if __name__ == "__main__":
    unittest.main(verbosity=2)
