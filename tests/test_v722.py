"""v7.2.2: the job chat answers from the documents it was given, and remembers the
conversation so far."""
import io
import queue
import subprocess
import sys
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import chat as CH  # noqa: E402
import chatsources as CS  # noqa: E402
import cli_jobs as CJ  # noqa: E402
import docreview as D  # noqa: E402
import findings as F  # noqa: E402
import jobs as J  # noqa: E402
import prism_cli as P  # noqa: E402
from test_cli import Isolated  # noqa: E402

GIT = ["git", "-c", "user.email=t@example.com", "-c", "user.name=T",
       "-c", "commit.gpgsign=false"]


def git(repo, *args):
    return subprocess.run([*GIT, "-C", str(repo), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def docx_bytes(*paragraphs):
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    xml = ('<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="http://schemas.'
           'openxmlformats.org/wordprocessingml/2006/main"><w:body>' + body
           + "</w:body></w:document>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", xml)
    return buf.getvalue()


class Base(Isolated):
    def repo_with_docs(self):
        repo = self.make_repo("acme-be")
        (repo / "docs" / "requirements").mkdir(parents=True)
        (repo / "docs" / "spec.md").write_text(
            "# Spec\n\n## Login\nSessions expire after 15 minutes of inactivity.\n",
            encoding="utf-8")
        (repo / "docs" / "requirements" / "FR-1.md").write_text(
            "FR-1: a session must expire after 30 minutes.\n", encoding="utf-8")
        (repo / "docs" / "requirements" / "FR-2.docx").write_bytes(
            docx_bytes("FR-2: passwords are at least 12 characters."))
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "docs")
        git(repo, "branch", "-M", "main")
        return repo

    def doc_spec(self, repo, uploads=(), req="docs/requirements"):
        return D.DocJobSpec(project_dir=str(repo), requirements_path=req, owner="Priya",
                            targets=(D.DocTarget("repo", "acme-be", str(repo), "main",
                                                 ("docs/spec.md",), uploads),))


class Sources(Base):
    def test_documents_and_requirements_are_read_from_the_branch(self):
        repo = self.repo_with_docs()
        text = CS.for_job(self.doc_spec(repo), [], "abcdef0123")
        self.assertIn("=== DOCUMENT: acme-be: docs/spec.md", text)
        self.assertIn("Sessions expire after 15 minutes", text)
        self.assertIn("=== REQUIREMENT: docs/requirements/FR-1.md", text)
        self.assertIn("a session must expire after 30 minutes", text)
        self.assertIn("passwords are at least 12 characters", text, "Word is extracted")

    def test_the_reviewed_commit_is_read_not_a_later_one(self):
        repo = self.repo_with_docs()
        reviewed = git(repo, "rev-parse", "HEAD")
        (repo / "docs" / "spec.md").write_text("# Spec\n\nRewritten later.\n", encoding="utf-8")
        git(repo, "commit", "-qam", "later")
        finding = F.make_finding("DR-1", "docs", "Major", "15 vs 30 minutes", F._trace(
            repo="acme-be", branch="main", commit=reviewed, path="docs/spec.md"))
        text = CS.for_job(self.doc_spec(repo), [finding], "j")
        self.assertIn("15 minutes", text)
        self.assertNotIn("Rewritten later", text)
        self.assertIn(f"acme-be @ {reviewed[:8]}", text)

    def test_prisms_own_review_section_is_left_out(self):
        repo = self.repo_with_docs()
        doc = repo / "docs" / "spec.md"
        doc.write_text(doc.read_text(encoding="utf-8") + "\n" + D.SECTION_START
                       + "\n## PRISM review comments\nDR-1 old comment\n" + D.SECTION_END
                       + "\n", encoding="utf-8")
        git(repo, "commit", "-qam", "review")
        text = CS.for_job(self.doc_spec(repo), [], "j")
        self.assertIn("15 minutes", text)
        self.assertNotIn("old comment", text)

    def test_an_uploaded_document_is_read_where_it_is(self):
        repo = self.repo_with_docs()
        up = self.home / "brief.docx"
        up.write_bytes(docx_bytes("The brief says: audit every login."))
        text = CS.for_job(self.doc_spec(repo, uploads=(D.Upload(str(up)),)), [], "j")
        self.assertIn("brief.docx (uploaded)", text)
        self.assertIn("audit every login", text)

    def test_what_cannot_be_read_says_so_and_nothing_raises(self):
        repo = self.repo_with_docs()
        spec = D.DocJobSpec(project_dir=str(repo), requirements_path="nowhere", targets=(
            D.DocTarget("repo", "acme-be", str(repo), "main", ("docs/missing.md",),
                        (D.Upload(str(self.home / "gone.md")),)),))
        text = CS.for_job(spec, [], "j")
        self.assertIn("[Not available to this chat: not found on main.]", text)
        self.assertIn("gone.md (uploaded)", text)
        self.assertIn("requirements folder was not found", text)

    def test_a_large_document_is_cut_and_its_whole_text_saved(self):
        src = CS.Source("document", "repo: docs/big.md", "x", "A" * 50 + "TAIL")
        text = CS.render([src], job_id="abcdef0123", max_file=20, max_total=100)
        self.assertIn("cut here: 20 of 54 characters shown", text)
        path = Path(text.split("The whole text is in ", 1)[1].split(": open it", 1)[0])
        self.assertTrue(str(path).startswith(str(CS.config.CONFIG_DIR)))
        self.assertTrue(path.read_text(encoding="utf-8").endswith("TAIL"))

    def test_the_total_budget_is_shared(self):
        a = CS.Source("document", "a", "", "x" * 60)
        b = CS.Source("document", "b", "", "y" * 60)
        text = CS.render([a, b], job_id="j", max_file=100, max_total=80)
        self.assertIn("x" * 60, text)
        self.assertIn("20 of 60 characters shown", text)

    def test_a_pull_request_has_no_documents(self):
        self.assertEqual(CS.for_job(J.JobSpec(project_dir="/p", repo_name="r", pr_id="1")), "")


class Prompt(Isolated):
    def test_documents_and_conversation_go_into_the_prompt(self):
        text = CH.prompt("why is that one a blocker?", "Job: x", "x",
                         history=[("you", "what is DR-2?"), ("chat", "DR-2 is the timeout.")],
                         sources="=== DOCUMENT: spec.md ===\n15 minutes")
        self.assertIn("SUPPLIED DOCUMENTS", text)
        self.assertIn("15 minutes", text)
        self.assertIn("CONVERSATION SO FAR", text)
        self.assertIn("Person: what is DR-2?", text)
        self.assertIn("You (PRISM chat): DR-2 is the timeout.", text)
        self.assertLess(text.index("CONVERSATION SO FAR (data"), text.index("QUESTION:"))
        self.assertIn("context, never a source", text)
        self.assertIn("Never say a document says or lacks something", text)

    def test_without_either_the_prompt_has_no_empty_sections(self):
        text = CH.prompt("q", "Job: x", "x")
        self.assertNotIn("SUPPLIED DOCUMENTS (the documents", text)
        self.assertNotIn("CONVERSATION SO FAR (data", text)

    def test_the_history_is_bounded_and_keeps_the_newest(self):
        turns = [("you", f"question {i} " + "z" * 3000) for i in range(30)]
        text = CH.history_text(turns)
        self.assertLessEqual(len(text), CH.HISTORY_CHARS)
        self.assertIn("question 29", text)
        self.assertNotIn("question 0 ", text)
        self.assertEqual(CH.history_text([("you", "  "), None, ("chat", "ok")]),
                         "You (PRISM chat): ok")

    def test_ask_hands_both_to_the_reviewer(self):
        seen = {}

        def fake(cmd, **_k):
            seen["prompt"] = cmd[-1]
            return 0, "It says 15 minutes.", "ses"
        with mock.patch.object(CH.O, "require_engine", return_value="opencode"), \
                mock.patch.object(CH.O, "engine_supports_json", return_value=False), \
                mock.patch.object(CH.O, "_dir_args", return_value=[]), \
                mock.patch.object(CH.O, "_approve_args", return_value=[]), \
                mock.patch.object(CH.O, "ensure_bundled_agents"), \
                mock.patch.object(CH.O, "_run_stream_resilient", side_effect=fake):
            reply, how = CH.ask("what does it say?", "Job: x", "x", "docs", "", history=[
                ("you", "hi there, which doc?"), ("chat", "spec.md")], sources="DOCTEXT",
                session_id="ses_review")
        self.assertEqual((reply, how), ("It says 15 minutes.", "record"))
        # with documents, never the reviewer's session: they would pile up in it
        self.assertIn("DOCTEXT", seen["prompt"])
        self.assertIn("Person: hi there, which doc?", seen["prompt"])


class DesktopJobs(Base):
    def setUp(self):
        super().setUp()
        self.q = queue.Queue()
        self.calls = []

        def runner(text, details, label, kind, project_dir, model=None, session_id=None,
                   control=None, history=None, sources=""):
            self.calls.append({"text": text, "history": history, "sources": sources})
            return f"answer {len(self.calls)}", "record"
        self.m = J.JobManager(self.q, runner=lambda **k: {}, chat_runner=runner)

    def wait(self, job):
        while True:
            _jid, kind, payload = self.q.get(timeout=20)
            if kind == "chat":
                job.chat_busy = False
                job.chat.append(("chat", payload))
                return payload

    def test_follow_ups_carry_the_conversation_and_the_documents(self):
        repo = self.repo_with_docs()
        job = self.m.create(self.doc_spec(repo))
        self.m.start_chat(job.id, "what does the spec say about sessions?")
        self.wait(job)
        self.m.start_chat(job.id, "and the requirement?")
        self.wait(job)
        first, second = self.calls
        self.assertEqual(first["history"], [])
        self.assertEqual(second["history"], [("you", "what does the spec say about sessions?"),
                                             ("chat", "answer 1")])
        self.assertIn("15 minutes", second["sources"])
        self.assertIn("30 minutes", second["sources"])

    def test_a_pull_request_chat_still_has_its_history(self):
        job = self.m.create(J.JobSpec(project_dir="/p", repo_name="r", pr_id="5"))
        self.m.start_chat(job.id, "one")
        self.wait(job)
        self.m.start_chat(job.id, "two")
        self.wait(job)
        self.assertEqual(self.calls[1]["history"], [("you", "one"), ("chat", "answer 1")])
        self.assertEqual(self.calls[1]["sources"], "")


class CliChat(Base):
    def test_prism_chat_follows_on_and_new_starts_over(self):
        repo = self.repo_with_docs()
        record = CJ.create(self.doc_spec(repo))
        CJ.finish(record["id"], J.DONE)
        calls = []

        def fake_ask(question, details, label, kind, project_dir, **kw):
            calls.append(kw)
            return f"reply to {question}", "record"
        with mock.patch.object(P.CHAT, "ask", side_effect=fake_ask):
            for q in (["what", "is", "DR-1?"], ["why?"]):
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(P.main(["chat", record["id"], *q]), P.EXIT_OK)
            self.assertEqual(calls[1]["history"], [("you", "what is DR-1?"),
                                                   ("chat", "reply to what is DR-1?")])
            self.assertIn("15 minutes", calls[1]["sources"])
            with redirect_stdout(io.StringIO()):
                self.assertEqual(P.main(["chat", record["id"], "--new", "again"]), P.EXIT_OK)
            self.assertEqual(calls[2]["history"], [])
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(P.main(["chat", record["id"], "--new"]), P.EXIT_OK)
            self.assertIn("new conversation", out.getvalue())
        self.assertEqual(CJ.read_chat(record["id"]), [])
        CJ.remove(record["id"])
        self.assertFalse(CJ._file(record["id"], "chat").exists())

    def test_a_question_is_required_without_new(self):
        record = CJ.create(J.JobSpec(project_dir="/p", repo_name="r", pr_id="1"))
        CJ.finish(record["id"], J.DONE)
        with redirect_stdout(io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(P.main(["chat", record["id"]]), P.EXIT_USAGE)

    def test_the_chat_file_ignores_damaged_lines(self):
        CJ.append_chat("abc", ("you", "q"), ("chat", "a"))
        with open(CJ._file("abc", "chat"), "a", encoding="utf-8") as fh:
            fh.write("not json\n[\"robot\", \"x\"]\n")
        self.assertEqual(CJ.read_chat("abc"), [("you", "q"), ("chat", "a")])


if __name__ == "__main__":
    import unittest
    unittest.main()
