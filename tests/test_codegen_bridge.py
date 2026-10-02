"""codegen bridge tests — no Tkinter, no AWS, no real engine.

The HTTP tests run a real server on a loopback port with the manager's
pipeline replaced by a stub, so they exercise the actual request path:
auth, validation, hand-off to the "UI thread" (the test pumps the inbox the
way app.App._drain_codegen does) and the callback to codegen.
"""
import json
import queue
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config as C  # noqa: E402
import codegen_bridge as IB  # noqa: E402
import jobs as J  # noqa: E402
from orchestrator import ReviewResult  # noqa: E402

SETTINGS = {"enabled": True, "port": 0, "max_iterations": 3, "auto_merge": True}


class Env(unittest.TestCase):
    """A fake home (config + discovery file) and a git-looking clone."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.clone = self.tmp / "clone"
        (self.clone / ".git").mkdir(parents=True)
        for name, value in (("DISCOVERY_PATH", self.tmp / "codegen-bridge.json"),):
            old = getattr(IB, name)
            setattr(IB, name, value)
            self.addCleanup(setattr, IB, name, old)

    def body(self, **kw):
        base = {"request_id": "req-1", "repo_name": "acme-be", "pr_id": 214,
                "region": "us-east-1", "local_repo": str(self.clone), "iteration": 1}
        base.update(kw)
        return base


class ParseRequest(Env):
    def parse(self, **kw):
        return IB.parse_request(self.body(**kw), SETTINGS)

    def assertRefused(self, code=None, **kw):
        with self.assertRaises(IB.BridgeError) as cm:
            self.parse(**kw)
        self.assertEqual(cm.exception.status in (400,), True)
        if code:
            self.assertEqual(cm.exception.code, code)

    def test_a_good_request_parses(self):
        r = self.parse()
        self.assertEqual((r.repo_name, r.pr_id, r.iteration, r.key),
                         ("acme-be", "214", 1, "acme-be#214"))
        self.assertTrue(r.auto_merge)

    def test_bad_ids_are_refused(self):
        self.assertRefused(pr_id="12; rm -rf /")
        self.assertRefused(pr_id="")
        self.assertRefused(repo_name="a b")
        self.assertRefused(repo_name="../etc")
        self.assertRefused(request_id="")
        self.assertRefused(region="us east")
        self.assertRefused(iteration=0)
        self.assertRefused(iteration="x")

    def test_paths_must_be_an_existing_absolute_git_clone(self):
        self.assertRefused(local_repo="relative/path")
        self.assertRefused(local_repo=str(self.tmp / "nope"))
        plain = self.tmp / "plain"
        plain.mkdir()
        self.assertRefused(local_repo=str(plain))

    def test_protocol_mismatch_is_refused(self):
        self.assertRefused("unsupported-protocol", protocol=99)

    def test_callback_must_be_loopback(self):
        for url in ("http://example.com/cb", "http://169.254.169.254/x",
                    "http://10.0.0.5:80/cb", "file:///etc/passwd", "ftp://127.0.0.1/"):
            self.assertRefused("bad-callback", callback={"url": url})
        for url in ("http://127.0.0.1:9/cb", "http://localhost:9/cb", "http://[::1]:9/cb"):
            self.assertEqual(self.parse(callback={"url": url}).callback_url, url)

    def test_codegen_can_only_reduce_merge_permission(self):
        self.assertFalse(self.parse(options={"auto_merge": False}).auto_merge)
        off = dict(SETTINGS, auto_merge=False)
        r = IB.parse_request(self.body(options={"auto_merge": True}), off)
        self.assertFalse(r.auto_merge)

    def test_context_is_bounded(self):
        r = self.parse(context={"requirements": "x" * 100000})
        self.assertLessEqual(len(r.context), IB.MAX_CONTEXT_CHARS)

    def test_instructions_name_codegen_and_resubmissions(self):
        text = IB.instructions_for(self.parse(iteration=2, context="add a flag"))
        self.assertIn("codegen", text)
        self.assertIn("add a flag", text)
        self.assertIn("resubmission", text)


def review(verdict, findings=()):
    return ReviewResult(verdict_key=verdict, verdict_raw=verdict, impact_score="4",
                        impact_reason="r", findings=list(findings))


class Outcomes(unittest.TestCase):
    def decide(self, verdict, **summary):
        return IB.outcome_from_summary(summary, review(verdict))[0]

    def test_mapping(self):
        self.assertEqual(self.decide("approve", merged=True), IB.DECISION_MERGED)
        self.assertEqual(self.decide("request-changes", merged=False,
                                     stopped="verdict-blocks-merge"), IB.DECISION_CHANGES)
        self.assertEqual(self.decide("block", merged=False), IB.DECISION_BLOCKED)
        self.assertEqual(self.decide("approve", merged=False, stopped="merge-disabled"),
                         IB.DECISION_APPROVED)
        self.assertEqual(self.decide("approve", merged=False,
                                     stopped="pr-changed-since-review"), IB.DECISION_STALE)
        self.assertEqual(self.decide("approve", merged=False,
                                     stopped="high-impact-not-confirmed"),
                         IB.DECISION_NEEDS_HUMAN)
        self.assertEqual(self.decide("approve", merged=False,
                                     stopped="not-fast-forwardable"), IB.DECISION_NEEDS_HUMAN)
        self.assertEqual(self.decide("unknown", merged=False, stopped="unparsed-verdict"),
                         IB.DECISION_NEEDS_HUMAN)

    def test_unknown_outcomes_fall_to_a_human_never_to_a_loop(self):
        self.assertEqual(self.decide("approve", merged=False, stopped="something-new"),
                         IB.DECISION_NEEDS_HUMAN)
        self.assertEqual(IB.outcome_from_summary({}, None)[0], IB.DECISION_NEEDS_HUMAN)

    def test_feedback_does_not_tell_the_agent_to_ignore_where_a_defect_recurs(self):
        rv = review("request-changes", ["- **[High]** a.py:3 — bug. Same pattern also at: b.py:9."])
        text = IB.feedback_text(rv, IB.DECISION_CHANGES)
        self.assertIn("wherever a finding says it recurs", text)
        self.assertIn("Same pattern also at: b.py:9.", text)
        self.assertNotIn("within the original", text)

    def test_feedback_only_when_there_is_something_to_fix(self):
        rv = review("request-changes", ["- **[High]** a.py:3 — bug"])
        self.assertIn("a.py:3", IB.feedback_text(rv, IB.DECISION_CHANGES))
        self.assertEqual(IB.feedback_text(rv, IB.DECISION_MERGED), "")
        self.assertEqual(IB.feedback_text(rv, IB.DECISION_NEEDS_HUMAN), "")


class Deliver(unittest.TestCase):
    class Resp:
        def __init__(self, status=200):
            self.status = status

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def test_retries_then_succeeds_and_sends_the_token(self):
        calls = []

        def opener(req, timeout):
            calls.append(req)
            if len(calls) < 3:
                raise OSError("down")
            return self.Resp()
        ok = IB.deliver("http://127.0.0.1:1/cb", "tok", {"a": 1}, opener=opener,
                        sleep=lambda s: None)
        self.assertTrue(ok)
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[-1].get_header("Authorization"), "Bearer tok")

    def test_gives_up_after_the_attempt_budget(self):
        def opener(req, timeout):
            raise OSError("down")
        calls = []
        self.assertFalse(IB.deliver("http://127.0.0.1:1/cb", "", {}, opener=opener,
                                    sleep=calls.append))
        self.assertEqual(len(calls), IB.CALLBACK_ATTEMPTS - 1)

    def test_a_4xx_is_final(self):
        n = []

        def opener(req, timeout):
            n.append(1)
            raise urllib.error.HTTPError("u", 401, "no", {}, None)
        self.assertFalse(IB.deliver("http://127.0.0.1:1/cb", "", {}, opener=opener,
                                    sleep=lambda s: None))
        self.assertEqual(len(n), 1)


class Live(Env):
    """Real server, stub pipeline; the test thread plays the UI thread."""

    def setUp(self):
        super().setUp()
        self.out_q = queue.Queue()
        self.release = threading.Event()
        self.summary = {"review": review("approve"), "merged": True}
        self.commits = ("aaa", "bbb")
        self.delivered = []
        self.delivered_evt = threading.Event()

        def runner(**kw):
            self.release.wait(5)
            return self.summary

        self.manager = J.JobManager(self.out_q, runner=runner,
                                    on_outcome=lambda job, **o: self.bridge.job_finished(job, **o))

        def deliver(url, token, payload):
            self.delivered.append((url, token, payload))
            self.delivered_evt.set()
            return True

        self.bridge = IB.CodegenBridge(self.manager, settings=SETTINGS, deliver_fn=deliver,
                                    commit_lookup=lambda req: self.commits)
        self.bridge.start()
        self.addCleanup(self.bridge.stop)
        self.stop_pump = threading.Event()
        t = threading.Thread(target=self._pump, daemon=True)
        t.start()
        self.addCleanup(self.stop_pump.set)

    def _pump(self):
        """Stands in for the UI thread: admits submissions and applies the
        terminal events the worker posts, as app.App._handle does."""
        final = {"done": J.DONE, "error": J.ERROR, "stopped": J.STOPPED}
        while not self.stop_pump.is_set():
            try:
                self.bridge.admit(self.bridge.inbox.get(timeout=0.05))
            except queue.Empty:
                pass
            while True:
                try:
                    job_id, kind, _payload = self.out_q.get_nowait()
                except queue.Empty:
                    break
                if kind in final:
                    self.manager.jobs[job_id].status = final[kind]

    def call(self, method, path, body=None, token=True, host=None):
        url = f"http://127.0.0.1:{self.bridge.port}{path}"
        req = urllib.request.Request(url, method=method,
                                     data=json.dumps(body).encode() if body is not None else None)
        if token:
            req.add_header("Authorization", f"Bearer {self.bridge.token}")
        if host:
            req.add_header("Host", host)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def wait_finished(self, review_id):
        end = time.time() + 5
        while time.time() < end:
            st = self.call("GET", f"/v1/reviews/{review_id}")[1]
            if st.get("decision"):
                return st
            time.sleep(0.02)
        self.fail("review never finished")

    # -- discovery / auth --
    def test_discovery_file_is_private_and_correct(self):
        info = json.loads(IB.DISCOVERY_PATH.read_text())
        self.assertEqual(info["url"], f"http://127.0.0.1:{self.bridge.port}")
        self.assertEqual(info["token"], self.bridge.token)
        self.assertEqual(IB.DISCOVERY_PATH.stat().st_mode & 0o777, 0o600)

    def test_stop_removes_only_our_discovery_file(self):
        self.bridge.stop()
        self.assertFalse(IB.DISCOVERY_PATH.exists())

    def test_requests_need_the_token(self):
        self.assertEqual(self.call("GET", "/v1/health", token=False)[0], 401)
        self.assertEqual(self.call("GET", "/v1/health")[0], 200)

    def test_foreign_host_header_is_refused(self):
        self.assertEqual(self.call("GET", "/v1/health", host="evil.example")[0], 403)

    def test_unknown_routes_and_bodies(self):
        self.assertEqual(self.call("GET", "/v1/nope")[0], 404)
        self.assertEqual(self.call("POST", "/v1/reviews", {"x": 1})[0], 400)
        self.assertEqual(self.call("GET", "/v1/reviews/rv_0123456789abcdef")[0], 404)

    # -- the happy path --
    def test_review_runs_as_an_ordinary_job_and_calls_back(self):
        status, resp = self.call("POST", "/v1/reviews", self.body(
            callback={"url": "http://127.0.0.1:9/cb", "token": "t0k"},
            run_ref="run-7", context={"requirements": "add a --version flag"}))
        self.assertEqual(status, 202)
        job = self.manager.jobs[resp["job_id"]]
        self.assertEqual((job.spec.origin, job.spec.origin_ref), ("codegen", "req-1"))
        self.assertIn("add a --version flag", job.spec.custom_instructions)
        self.assertTrue(job.spec.do_merge)
        self.assertEqual(self.call("GET", f"/v1/reviews/{resp['review_id']}")[1]["state"],
                         "running")
        self.release.set()
        self.assertTrue(self.delivered_evt.wait(5))
        url, token, payload = self.delivered[0]
        self.assertEqual((url, token), ("http://127.0.0.1:9/cb", "t0k"))
        self.assertEqual(payload["decision"], IB.DECISION_MERGED)
        self.assertEqual(payload["run_ref"], "run-7")
        self.assertEqual(payload["revision"], 1)
        polled = self.wait_finished(resp["review_id"])
        self.assertTrue(polled["merged"])

    def test_changes_requested_carries_feedback(self):
        self.summary = {"review": review("request-changes", ["- **[High]** x.py:1 — bad"]),
                        "merged": False, "stopped": "verdict-blocks-merge"}
        self.release.set()
        _s, resp = self.call("POST", "/v1/reviews", self.body())
        out = self.wait_finished(resp["review_id"])
        self.assertEqual(out["decision"], IB.DECISION_CHANGES)
        self.assertIn("x.py:1", out["feedback"])
        self.assertEqual(out["findings"], ["- **[High]** x.py:1 — bad"])

    def test_a_crashing_pipeline_is_reported_as_failed(self):
        def boom(**kw):
            raise RuntimeError("aws exploded")
        self.manager._runner = boom
        _s, resp = self.call("POST", "/v1/reviews", self.body())
        out = self.wait_finished(resp["review_id"])
        self.assertEqual(out["decision"], IB.DECISION_FAILED)
        self.assertIn("aws exploded", out["reason"])

    def test_review_only_request_does_not_merge(self):
        self.release.set()
        _s, resp = self.call("POST", "/v1/reviews",
                             self.body(options={"auto_merge": False}))
        self.assertFalse(self.manager.jobs[resp["job_id"]].spec.do_merge)

    # -- guards --
    def test_retrying_a_post_is_idempotent(self):
        first = self.call("POST", "/v1/reviews", self.body())
        again = self.call("POST", "/v1/reviews", self.body())
        self.assertEqual(again[0], 200)
        self.assertEqual(again[1]["review_id"], first[1]["review_id"])
        self.assertEqual(len(self.manager.jobs), 1)

    def test_same_pr_while_active_is_a_conflict(self):
        self.call("POST", "/v1/reviews", self.body())
        status, resp = self.call("POST", "/v1/reviews", self.body(request_id="req-2"))
        self.assertEqual((status, resp["error"]), (409, "already-reviewing"))

    def test_iteration_ceiling_stops_the_loop(self):
        self.release.set()
        for n in (1, 2, 3):
            status, resp = self.call("POST", "/v1/reviews",
                                     self.body(request_id=f"r{n}", iteration=n))
            self.assertEqual(status, 202, resp)
            self.wait_finished(resp["review_id"])
            self.commits = (f"c{n}", "old")
        status, resp = self.call("POST", "/v1/reviews",
                                 self.body(request_id="r4", iteration=4))
        self.assertEqual((status, resp["error"]), (409, "iteration-limit"))

    def test_our_own_round_count_catches_an_codegen_that_never_increments(self):
        self.release.set()
        for n in (1, 2, 3):
            status, resp = self.call("POST", "/v1/reviews",
                                     self.body(request_id=f"r{n}", iteration=1))
            self.assertEqual(status, 202, resp)
            self.wait_finished(resp["review_id"])
            self.commits = (f"c{n}", "old")
        status, resp = self.call("POST", "/v1/reviews",
                                 self.body(request_id="r4", iteration=1))
        self.assertEqual((status, resp["error"]), (409, "iteration-limit"))

    def test_resubmission_without_new_commits_is_refused(self):
        self.commits = ("same", "same")
        status, resp = self.call("POST", "/v1/reviews",
                                 self.body(request_id="r2", iteration=2))
        self.assertEqual((status, resp["error"]), (409, "no-new-commits"))
        self.assertEqual(len(self.manager.jobs), 0)

    def test_commit_lookup_failure_fails_open(self):
        def broken(req):
            raise RuntimeError("no aws")
        self.bridge._commit_lookup = broken
        self.release.set()
        status, _ = self.call("POST", "/v1/reviews", self.body(request_id="r2", iteration=2))
        self.assertEqual(status, 202)

    def test_cancel_stops_the_job(self):
        _s, resp = self.call("POST", "/v1/reviews", self.body())
        status, out = self.call("POST", f"/v1/reviews/{resp['review_id']}/cancel")
        self.assertEqual((status, out["cancelled"]), (202, True))
        self.assertEqual(self.manager.jobs[resp["job_id"]].status, J.STOPPING)
        self.release.set()

    def test_manual_jobs_are_ignored_by_the_hook(self):
        job = self.manager.create(J.JobSpec(project_dir="/tmp", repo_name="x", pr_id="9"))
        self.bridge.job_finished(job, summary={"review": None, "merged": False})
        self.assertEqual(self.delivered, [])


class NoCodegen(unittest.TestCase):
    def test_manager_without_a_hook_behaves_as_before(self):
        q = queue.Queue()
        m = J.JobManager(q, runner=lambda **k: {"merged": False})
        job = m.create(J.JobSpec(project_dir="/tmp", repo_name="x", pr_id="1"))
        m.pump()
        job.thread.join(5)
        kinds = []
        while not q.empty():
            kinds.append(q.get()[1])
        self.assertEqual(kinds, ["done"])

    def test_a_raising_hook_never_breaks_the_job(self):
        q = queue.Queue()

        def hook(job, **o):
            raise RuntimeError("hook bug")
        m = J.JobManager(q, runner=lambda **k: {"merged": False}, on_outcome=hook)
        job = m.create(J.JobSpec(project_dir="/tmp", repo_name="x", pr_id="1"))
        m.pump()
        job.thread.join(5)
        self.assertEqual(q.get()[1], "done")

    def test_origin_does_not_reach_the_pipeline(self):
        spec = J.JobSpec(project_dir="/p", repo_name="r", pr_id="1", origin="codegen")
        self.assertNotIn("origin", spec.pipeline_kwargs())
        self.assertIn("via codegen", spec.summary_bits())


class CodegenSettings(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for name, value in (("CONFIG_DIR", Path(tmp.name) / ".prism"),
                            ("CONFIG_PATH", Path(tmp.name) / ".prism" / "config.json")):
            old = getattr(C, name)
            setattr(C, name, value)
            self.addCleanup(setattr, C, name, old)

    def test_off_by_default(self):
        self.assertFalse(C.get_codegen()["enabled"])

    def test_round_trip_and_coercion(self):
        C.set_codegen(enabled=True, max_iterations=999, port=-5, bogus=1)
        got = C.get_codegen()
        self.assertTrue(got["enabled"])
        self.assertEqual(got["max_iterations"], 50)
        self.assertEqual(got["port"], 0)
        self.assertNotIn("bogus", C.load()["codegen"])

    def test_only_a_real_true_enables(self):
        C.save({"codegen": {"enabled": "yes"}})
        self.assertFalse(C.get_codegen()["enabled"])
        C.save({"codegen": "garbage"})
        self.assertFalse(C.get_codegen()["enabled"])

    def test_other_settings_survive(self):
        C.set_webhook_url("https://example.invalid/h")
        C.set_codegen(enabled=True)
        self.assertEqual(C.get_webhook_url(), "https://example.invalid/h")


if __name__ == "__main__":
    unittest.main(verbosity=2)
