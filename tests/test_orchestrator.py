"""Concurrency and engine-protocol tests for orchestrator.py.

Stdlib unittest, no Tkinter and no network — safe to run in CI. These cover
the two hazards that make parallel jobs possible at all: pinning each agent
conversation to its own session, and serialising mutations of a shared clone.

    python3 -m unittest discover -s tests -v
"""
import importlib
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


def error_event(message, err_type="step-error"):
    """One provider-error event, in the nested shape a real free-tier
    rejection actually arrives in (error.data.message) rather than the
    flatter shape the parser used to assume."""
    return jline(type=err_type, error={"name": "APIError",
                                       "data": {"message": message}})


class ErrorMessageExtraction(unittest.TestCase):
    """The engine nests a provider error at error.data.message; reading
    evt['message'] instead silently falls back to the raw JSON line."""

    def _run(self, payload):
        emitted = []
        o._run_stream(
            [sys.executable, "-c",
             "import sys; sys.stdout.write(sys.argv[1])", payload],
            cwd=".", emit=lambda m, tag=None: emitted.append((m, tag)),
            json_mode=True)
        return emitted

    def test_nested_provider_message_reaches_the_user(self):
        msg = "OpenCode's free tier can only be used from within OpenCode"
        emitted = self._run(error_event(msg) + "\n")
        self.assertIn((f"⚠ {msg}", None), emitted)


class FreeTierRetry(unittest.TestCase):
    """A free-tier rejection is transient in practice — real data shows an
    identical call succeed moments after one was bounced — so this is
    retried rather than failed outright, unlike any other error."""

    def setUp(self):
        self.orig_backoff = o.PROVIDER_ERROR_BACKOFF
        o.PROVIDER_ERROR_BACKOFF = 0   # keep the test fast; behaviour under
                                       # test is *that* it retries, not *when*
        self.addCleanup(setattr, o, "PROVIDER_ERROR_BACKOFF", self.orig_backoff)

    def _flaky_script(self, marker):
        msg = "OpenCode's free tier can only be used from within OpenCode"
        return (
            "import sys, os, json\n"
            f"marker = {marker!r}\n"
            "if not os.path.exists(marker):\n"
            "    open(marker, 'w').close()\n"
            f"    print(json.dumps({{'type': 'step-error', 'error': "
            f"{{'name': 'APIError', 'data': {{'message': {msg!r}}}}}}}))\n"
            "    sys.exit(1)\n"
            "else:\n"
            "    print(json.dumps({'type': 'text', 'sessionID': 'ses_x',"
            "                     'part': {'type': 'text', 'text': 'ok',"
            "                              'id': 'p1', 'sessionID': 'ses_x'}}))\n"
        )

    def test_retries_once_and_then_succeeds(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            marker = str(Path(d) / "seen")
            emitted = []
            rc, text, sid = o._run_stream_resilient(
                [sys.executable, "-c", self._flaky_script(marker)],
                cwd=".", emit=lambda m, tag=None: emitted.append((m, tag)),
                json_mode=True)
            self.assertEqual(rc, 0)
            self.assertEqual(text, "ok")
            self.assertEqual(sid, "ses_x")
            self.assertTrue(any("retrying" in (m or "") for m, _ in emitted))

    def test_a_non_free_tier_failure_is_not_retried(self):
        calls = []
        real_run_stream = o._run_stream

        def counting_run_stream(*a, **k):
            calls.append(1)
            return real_run_stream(*a, **k)

        o._run_stream = counting_run_stream
        self.addCleanup(setattr, o, "_run_stream", real_run_stream)
        rc, _text, _sid = o._run_stream_resilient(
            [sys.executable, "-c", "import sys; sys.exit(3)"],
            cwd=".", emit=lambda *a, **k: None, json_mode=False)
        self.assertEqual(rc, 3)
        self.assertEqual(len(calls), 1, "an unrelated failure must not retry")


class ModelListCaching(unittest.TestCase):
    """Enumerating every configured provider is what made the picker feel
    slow to even open — this is what stops it from happening more than once
    a session."""

    def setUp(self):
        self.orig_cache = o._MODELS_CACHE
        self.addCleanup(setattr, o, "_MODELS_CACHE", self.orig_cache)
        o._MODELS_CACHE = None
        self.orig_engine_path = o.engine_path
        self.addCleanup(setattr, o, "engine_path", self.orig_engine_path)
        self.calls = []

        def fake_run(cmd, **kwargs):
            self.calls.append(cmd)
            class _Proc:
                returncode = 0
                stdout = "opencode/big-pickle\ngithub-copilot/gpt-5\n"
            return _Proc()

        o.engine_path = lambda: "/usr/bin/fake-opencode"
        self.orig_subprocess_run = o.subprocess.run
        self.addCleanup(setattr, o.subprocess, "run", self.orig_subprocess_run)
        o.subprocess.run = fake_run

    def test_second_call_reuses_the_cache(self):
        first = o.list_available_models()
        second = o.list_available_models()
        self.assertEqual(first, second)
        self.assertEqual(len(self.calls), 1, "a second call must not re-ask the engine")

    def test_force_bypasses_the_cache(self):
        o.list_available_models()
        o.list_available_models(force=True)
        self.assertEqual(len(self.calls), 2)

    def test_a_failed_refresh_keeps_the_previous_good_list(self):
        good = o.list_available_models()
        self.assertTrue(good)

        def failing_run(cmd, **kwargs):
            class _Proc:
                returncode = 1
                stdout = ""
            return _Proc()
        o.subprocess.run = failing_run
        self.assertEqual(o.list_available_models(force=True), good)


class TokenMeterTests(unittest.TestCase):
    """A session in progress overwrites its own entry each tick — it's
    already cumulative for that conversation — while different sessions add
    together for the run-wide total the UI shows next to the impact score."""

    def test_one_session_updates_in_place(self):
        meter = o.TokenMeter()
        meter.update("ses_a", {"input": 10, "output": 5, "reasoning": 0,
                                "cache_read": 0, "cache_write": 0, "cost": 0.0})
        total = meter.update("ses_a", {"input": 30, "output": 15, "reasoning": 0,
                                       "cache_read": 0, "cache_write": 0, "cost": 0.0})
        self.assertEqual(total["total"], 45, "a later tick must replace, not add")

    def test_different_sessions_sum(self):
        meter = o.TokenMeter()
        meter.update("ses_a", {"input": 10, "output": 0, "reasoning": 0,
                                "cache_read": 0, "cache_write": 0, "cost": 0.01})
        total = meter.update("ses_b", {"input": 20, "output": 0, "reasoning": 0,
                                       "cache_read": 0, "cache_write": 0, "cost": 0.02})
        self.assertEqual(total["total"], 30)
        self.assertAlmostEqual(total["cost"], 0.03)


class DirectDescriptionUpdate(unittest.TestCase):
    """Step 2 writes the PR description itself now (no poster agent), so
    this is the only thing that has to get the block right."""

    def setUp(self):
        self.orig_get_pr = o.get_pr
        self.orig_aws_cli = o.aws_cli
        self.addCleanup(setattr, o, "get_pr", self.orig_get_pr)
        self.addCleanup(setattr, o, "aws_cli", self.orig_aws_cli)
        self.written = {}

        def fake_aws_cli(*args, **kwargs):
            self.written["description"] = args[args.index("--description") + 1]
            return {}

        o.aws_cli = fake_aws_cli

    def test_embeds_a_provenance_stamp(self):
        o.get_pr = lambda *a, **k: {"description": ""}
        o.update_description_direct(
            "7", "Approve", "3", ["- **[Low] nit** cosmetic"],
            verdict_key="approve", source_commit="abc123")
        self.assertIn(
            "<!-- prism:meta reviewer=PRISM verdict=approve commit=abc123 -->",
            self.written["description"])

    def test_replaces_a_stale_block_from_an_earlier_run(self):
        stale = ("<!-- prism:start -->\n"
                 "<!-- prism:meta reviewer=PRISM verdict=request-changes commit=old -->\n"
                 "old findings\n<!-- prism:end -->")
        o.get_pr = lambda *a, **k: {"description": stale}
        o.update_description_direct(
            "7", "Approve", "3", ["- new finding"],
            verdict_key="approve", source_commit="new-commit")
        desc = self.written["description"]
        self.assertNotIn("old findings", desc)
        self.assertIn("commit=new-commit", desc)
        self.assertEqual(desc.count("prism:start"), 1,
                         "the stale block must be replaced, not appended to")

    def test_replaces_a_legacy_pr_reviewer_tagged_block(self):
        """A description written by a pre-rebrand PRISM must be cleanly
        replaced, not left duplicated alongside the new prism:-tagged one."""
        legacy = ("<!-- pr-reviewer:start -->\n"
                 "<!-- pr-reviewer:meta verdict=request-changes commit=old -->\n"
                 "old findings\n<!-- pr-reviewer:end -->")
        o.get_pr = lambda *a, **k: {"description": legacy}
        o.update_description_direct(
            "7", "Approve", "3", ["- new finding"],
            verdict_key="approve", source_commit="new-commit")
        desc = self.written["description"]
        self.assertNotIn("old findings", desc)
        self.assertNotIn("pr-reviewer:start", desc, "new writes use the prism: tag only")
        self.assertEqual(desc.count("prism:start"), 1)

    def test_findings_are_not_capped_at_twenty(self):
        findings = [f"- finding {i}" for i in range(30)]
        o.get_pr = lambda *a, **k: {"description": ""}
        o.update_description_direct("7", "Approve", "3", findings, verdict_key="approve")
        desc = self.written["description"]
        for f in findings:
            self.assertIn(f, desc)


class PreviousReview(unittest.TestCase):
    def test_no_stamp_returns_empty(self):
        self.assertEqual(o.previous_review(""), {})
        self.assertEqual(o.previous_review("just some text"), {})

    def test_parses_the_current_stamp_shape(self):
        desc = ("<!-- prism:start -->\n"
               "<!-- prism:meta reviewer=PRISM verdict=approve commit=abc123 -->\n"
               "---\n- a finding\n<!-- prism:end -->")
        self.assertEqual(o.previous_review(desc),
                         {"reviewer": "PRISM", "verdict": "approve", "commit": "abc123"})

    def test_parses_the_legacy_stamp_shape(self):
        desc = ("<!-- pr-reviewer:start -->\n"
               "<!-- pr-reviewer:meta verdict=block commit=deadbeef -->\n"
               "<!-- pr-reviewer:end -->")
        self.assertEqual(o.previous_review(desc),
                         {"verdict": "block", "commit": "deadbeef"})


class IncrementalReview(unittest.TestCase):
    """_incremental_context() decides whether a review can scope itself to
    the delta since a previous pass. Every uncertain branch must fall back
    to None (a full review) rather than guess."""

    DESC = ("<!-- prism:start -->\n"
           "<!-- prism:meta reviewer=PRISM verdict=request-changes commit=old123 -->\n"
           "---\n- **[Medium] x** something\n<!-- prism:end -->")

    def setUp(self):
        self.orig_get_pr = o.get_pr
        self.orig_run = o.subprocess.run
        self.addCleanup(setattr, o, "get_pr", self.orig_get_pr)
        self.addCleanup(setattr, o.subprocess, "run", self.orig_run)

    def _pr(self, description, source_commit, source_ref="feat"):
        return lambda *a, **k: {"description": description,
                                "sourceCommit": source_commit,
                                "sourceReference": source_ref}

    def test_no_previous_stamp_is_a_full_review(self):
        o.get_pr = self._pr("", "new456")
        self.assertIsNone(o._incremental_context("7", "/tmp/repo"))

    def test_same_commit_as_last_time_is_a_full_review(self):
        o.get_pr = self._pr(self.DESC, "old123")
        self.assertIsNone(o._incremental_context("7", "/tmp/repo"))

    def test_previous_commit_not_an_ancestor_falls_back(self):
        o.get_pr = self._pr(self.DESC, "new456")

        def fake_run(cmd, **k):
            class R:
                returncode = 0 if cmd[:2] != ["git", "merge-base"] else 1
            return R()
        o.subprocess.run = fake_run
        self.assertIsNone(o._incremental_context("7", "/tmp/repo"))

    def test_get_pr_failure_falls_back(self):
        def boom(*a, **k):
            raise RuntimeError("aws unavailable")
        o.get_pr = boom
        self.assertIsNone(o._incremental_context("7", "/tmp/repo"))

    def test_valid_ancestor_returns_the_previous_review_context(self):
        o.get_pr = self._pr(self.DESC, "new456")

        def fake_run(cmd, **k):
            class R:
                returncode = 0
            return R()
        o.subprocess.run = fake_run
        ctx = o._incremental_context("7", "/tmp/repo")
        self.assertIsNotNone(ctx)
        self.assertEqual(ctx["commit"], "old123")
        self.assertEqual(ctx["current_commit"], "new456")
        self.assertEqual(ctx["verdict"], "request-changes")
        self.assertIn("Medium", ctx["block"])

    def test_run_opencode_review_prompt_mentions_incremental_when_present(self):
        importlib.reload(o)
        self.addCleanup(importlib.reload, o)
        captured = {}
        o._incremental_context = lambda *a, **k: {
            "commit": "old123", "current_commit": "new456",
            "verdict": "request-changes", "block": "- prior finding"}
        o.ensure_bundled_agents = lambda *a, **k: None
        o.require_engine = lambda: "opencode"
        o.engine_supports_json = lambda exe: False

        def fake_stream(cmd, cwd, emit, control=None, json_mode=False, meter=None):
            captured["prompt"] = cmd[-1]
            return 0, "**Verdict:** OK Approve\n", "sid"
        o._run_stream_resilient = fake_stream
        o.run_opencode_review("7", "repo", "/tmp/repo", "/tmp/proj")
        self.assertIn("INCREMENTAL", captured["prompt"])
        self.assertIn("old123", captured["prompt"])
        self.assertIn("prior finding", captured["prompt"])

    def test_run_opencode_review_prompt_is_plain_without_previous_context(self):
        importlib.reload(o)
        self.addCleanup(importlib.reload, o)
        captured = {}
        o._incremental_context = lambda *a, **k: None
        o.ensure_bundled_agents = lambda *a, **k: None
        o.require_engine = lambda: "opencode"
        o.engine_supports_json = lambda exe: False

        def fake_stream(cmd, cwd, emit, control=None, json_mode=False, meter=None):
            captured["prompt"] = cmd[-1]
            return 0, "**Verdict:** OK Approve\n", "sid"
        o._run_stream_resilient = fake_stream
        o.run_opencode_review("7", "repo", "/tmp/repo", "/tmp/proj")
        self.assertNotIn("INCREMENTAL", captured["prompt"])


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
