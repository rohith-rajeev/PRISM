"""Job model and scheduler tests — no Tkinter, safe for CI."""
import queue
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import jobs as J  # noqa: E402


def spec(pr="1", repo="acme-be", **kw):
    return J.JobSpec(project_dir="/tmp/proj", repo_name=repo, pr_id=pr, **kw)


class SpecTests(unittest.TestCase):
    def test_label_and_clone_key(self):
        s = spec("214")
        self.assertEqual(s.label, "acme-be #214")
        self.assertEqual(spec(local_repo="/tmp/./clone").clone_key,
                         spec(local_repo="/tmp/clone").clone_key)

    def test_spec_is_immutable(self):
        """A job must not be mutated by later edits to the form."""
        with self.assertRaises(Exception):
            spec().pr_id = "999"

    def test_pipeline_kwargs_match_full_pipeline(self):
        import inspect
        from orchestrator import full_pipeline
        allowed = set(inspect.signature(full_pipeline).parameters)
        self.assertTrue(set(spec().pipeline_kwargs()).issubset(allowed))


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.m = J.JobManager(queue.Queue(), runner=lambda **k: {})

    def test_same_pr_twice_is_refused(self):
        self.m.create(spec("214"))
        with self.assertRaises(J.DuplicateJob):
            self.m.create(spec("214"))

    def test_same_pr_allowed_once_the_first_finished(self):
        j = self.m.create(spec("214"))
        j.status = J.DONE
        self.m.create(spec("214"))          # must not raise

    def test_different_prs_same_repo_are_fine(self):
        self.m.create(spec("214"))
        self.m.create(spec("215"))
        self.assertEqual(len(self.m.jobs), 2)

    def test_shared_clone_is_reported_not_refused(self):
        self.m.create(spec("214", local_repo="/tmp/clone"))
        s2 = spec("215", local_repo="/tmp/clone")
        self.assertIsNotNone(self.m.shares_clone_with(s2))
        self.m.create(s2)                   # allowed


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.q = queue.Queue()
        self.release = threading.Event()
        self.started = []
        def runner(**kw):
            self.started.append(kw["pr_id"])
            self.release.wait(5)
            return {"merged": False, "stopped": "test"}
        self.m = J.JobManager(self.q, runner=runner, max_parallel=3)

    def tearDown(self):
        self.release.set()

    def test_caps_at_max_parallel_and_queues_the_rest(self):
        for i in range(6):
            self.m.create(spec(str(i)))
        self.m.pump()
        time.sleep(0.2)
        self.assertEqual(self.m.running_count, 3)
        self.assertEqual(len(self.started), 3)
        statuses = [j.status for j in self.m.jobs.values()]
        self.assertEqual(statuses.count(J.QUEUED), 3)

    def test_finishing_one_starts_exactly_one_more(self):
        for i in range(6):
            self.m.create(spec(str(i)))
        self.m.pump()
        time.sleep(0.2)
        first = next(j for j in self.m.jobs.values() if j.status == J.RUNNING)
        first.status = J.DONE                # what the UI does on a "done" message
        self.m.pump()
        time.sleep(0.2)
        self.assertEqual(self.m.running_count, 3)
        self.assertEqual(len(self.started), 4)


class RoutingTests(unittest.TestCase):
    def test_messages_carry_their_job_id(self):
        q = queue.Queue()
        m = J.JobManager(q, runner=lambda **k: {"merged": True})
        a, b = m.create(spec("1")), m.create(spec("2"))
        a.emit_event("log", "from a")
        b.emit_event("log", "from b")
        got = [q.get_nowait(), q.get_nowait()]
        self.assertEqual([(g[0], g[2]) for g in got],
                         [(a.id, "from a"), (b.id, "from b")])

    def test_log_buffer_is_bounded(self):
        job = J.Job(1, spec(), queue.Queue())
        for i in range(J.LOG_LINES_PER_JOB + 500):
            job.append_log(f"line {i}")
        self.assertEqual(len(job.log), J.LOG_LINES_PER_JOB)
        self.assertEqual(job.log[-1][0], f"line {J.LOG_LINES_PER_JOB + 499}")


class CancellationTests(unittest.TestCase):
    def test_stopping_releases_a_job_parked_on_a_question(self):
        q = queue.Queue()
        answered = []
        def runner(**kw):
            answered.append(kw["ask"]("Which branch?"))   # blocks
            return {"merged": False}
        m = J.JobManager(q, runner=runner)
        job = m.create(spec())
        m.pump()
        time.sleep(0.3)
        self.assertEqual([t for t in (job.thread,) if t.is_alive()], [job.thread])
        m.stop(job.id)
        job.thread.join(timeout=5)
        self.assertFalse(job.thread.is_alive(), "stop must release a parked ask")
        self.assertEqual(answered, [None], "a cancelled ask returns no answer")

    def test_stopping_one_job_leaves_others_running(self):
        q = queue.Queue()
        release = threading.Event()
        m = J.JobManager(q, runner=lambda **k: release.wait(5) or {"merged": False})
        a, b = m.create(spec("1")), m.create(spec("2"))
        m.pump(); time.sleep(0.2)
        m.stop(a.id)
        self.assertEqual(a.status, J.STOPPING)
        self.assertEqual(b.status, J.RUNNING)
        self.assertFalse(b.control.cancelled())
        release.set()

    def test_stopping_a_queued_job_needs_no_thread(self):
        m = J.JobManager(queue.Queue(), runner=lambda **k: {}, max_parallel=0)
        job = m.create(spec())
        m.pump()
        self.assertEqual(job.status, J.QUEUED)
        m.stop(job.id)
        self.assertEqual(job.status, J.STOPPED)
        self.assertIsNone(job.thread)

    def test_stop_all_cancels_every_active_job(self):
        q = queue.Queue()
        release = threading.Event()
        m = J.JobManager(q, runner=lambda **k: release.wait(5) or {"merged": False})
        for i in range(3):
            m.create(spec(str(i)))
        m.pump(); time.sleep(0.2)
        m.stop_all()
        self.assertTrue(all(j.control.cancelled() or j.status == J.STOPPED
                            for j in m.jobs.values()))
        release.set()


if __name__ == "__main__":
    unittest.main(verbosity=2)
