"""Review consistency across stages — the review memory, content-based matching,
the late-find signal and the reviewer's instructions.

The git-backed tests build a real origin + clone and put the same change through
a rebase, a squash, a cherry-pick, a promotion that bundles other work and a
follow-up fix — the situations in which commit shas stop being a usable identity.
PRISM only reviews: one test proves nothing here writes to the repository.
"""
import importlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import orchestrator as o  # noqa: E402

QUIET = lambda *a, **k: None  # noqa: E731
POSIX = hasattr(os, "getuid")


def git(cwd, *args, check=True):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    return subprocess.run(["git", *args], cwd=cwd, check=check, capture_output=True,
                          text=True, env=env).stdout.strip()


class Isolated(unittest.TestCase):
    def setUp(self):
        importlib.reload(o)
        self.addCleanup(importlib.reload, o)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.mem = self.tmp / "memory"
        patcher = mock.patch.dict(os.environ, {"PRISM_HISTORY_DIR": str(self.mem)})
        patcher.start()
        self.addCleanup(patcher.stop)
        o._REVIEWED_STORE_PATH = self.tmp / "reviewed_commits.json"


def review(verdict="request-changes", findings=("- **[High] error-handling — `a.py:3`** x",),
           raw="🔴 Request changes", impact="5"):
    return o.ReviewResult(verdict_key=verdict, verdict_raw=raw, impact_score=impact,
                          findings=list(findings))


SHA = lambda n: f"{n:040x}"  # noqa: E731


class Memory(Isolated):
    """Where the review memory lives, and how it is kept trustworthy and small."""

    def test_it_lives_outside_any_repository_in_a_per_user_temp_directory(self):
        fake_tmp = self.tmp / "faketmp"
        fake_tmp.mkdir()
        with mock.patch.dict(os.environ), mock.patch("tempfile.gettempdir",
                                                     return_value=str(fake_tmp)):
            os.environ.pop("PRISM_HISTORY_DIR")
            base = o._history_dir(create=True)
            self.assertIsNotNone(base)
            self.assertEqual(base.parent, fake_tmp)
            self.assertTrue(base.name.startswith("prism-"))
            if POSIX:
                self.assertEqual(stat.S_IMODE(base.stat().st_mode) & 0o077, 0)

    def test_override_directory_is_honoured(self):
        o._record_review_history("repo", "10", "/nonexistent", "f", "d", SHA(1), review())
        self.assertTrue((self.mem / "review_history.json").is_file())

    @unittest.skipUnless(POSIX, "ownership and mode checks are POSIX")
    def test_a_symlinked_or_group_writable_directory_is_not_trusted(self):
        fake_tmp = self.tmp / "faketmp"
        fake_tmp.mkdir()
        with mock.patch.dict(os.environ), mock.patch("tempfile.gettempdir",
                                                     return_value=str(fake_tmp)):
            os.environ.pop("PRISM_HISTORY_DIR")
            target = fake_tmp / f"prism-{os.getuid()}"
            elsewhere = self.tmp / "elsewhere"
            elsewhere.mkdir(mode=0o700)
            target.symlink_to(elsewhere)
            self.assertIsNone(o._history_dir(create=True), "a symlink must be refused")
            target.unlink()
            target.mkdir()
            target.chmod(0o770)
            self.assertIsNone(o._history_dir(), "a group-writable directory must be refused")
            o._record_review_history("repo", "1", "/nx", "f", "d", SHA(1), review())
            self.assertEqual(list(target.iterdir()), [], "nothing is written into it")
            self.assertEqual(o._load_history(), [])
            target.chmod(0o700)
            self.assertEqual(o._history_dir(), target)

    @unittest.skipUnless(POSIX, "file modes are POSIX")
    def test_the_record_file_is_private(self):
        o._record_review_history("repo", "1", "/nx", "f", "d", SHA(1), review())
        mode = stat.S_IMODE((self.mem / "review_history.json").stat().st_mode)
        self.assertEqual(mode & 0o077, 0)

    def test_old_records_age_out(self):
        o._record_review_history("repo", "1", "/nx", "f", "d", SHA(1), review())
        path = self.mem / "review_history.json"
        data = json.loads(path.read_text())
        data["records"][0]["at"] = int(time.time()) - (o.HISTORY_MAX_AGE_DAYS + 1) * 86400
        path.write_text(json.dumps(data))
        self.assertEqual(o._load_history(), [])

    def test_a_review_is_recorded_with_its_commits_hunks_and_findings(self):
        o._record_review_history("repo", "10", "/nonexistent", "feat", "develop",
                                 SHA(1), review())
        (rec,) = o._load_history()
        self.assertEqual((rec["repo"], rec["pr_id"], rec["dest"], rec["commit"]),
                         ("repo", "10", "develop", SHA(1)))
        self.assertEqual(rec["commits"], [SHA(1)])        # no git: just the tip
        self.assertEqual(rec["hunks"], {})
        self.assertEqual(rec["verdict"], "request-changes")
        self.assertEqual(len(rec["findings"]), 1)

    def test_recording_the_same_review_twice_keeps_one_record(self):
        for _ in range(2):
            o._record_review_history("repo", "10", "/nonexistent", "f", "d", SHA(1), review())
        self.assertEqual(len(o._load_history()), 1)

    def test_a_new_commit_on_the_same_pr_is_a_new_record(self):
        o._record_review_history("repo", "10", "/nx", "f", "d", SHA(1), review())
        o._record_review_history("repo", "10", "/nx", "f", "d", SHA(2), review())
        self.assertEqual(len(o._load_history()), 2)

    def test_the_memory_is_bounded(self):
        for n in range(o.HISTORY_MAX_RECORDS + 20):
            o._record_review_history("repo", str(n), "/nx", "f", "d", SHA(n + 1), review())
        records = o._load_history()
        self.assertEqual(len(records), o.HISTORY_MAX_RECORDS)
        self.assertEqual(records[-1]["pr_id"], str(o.HISTORY_MAX_RECORDS + 19))

    def test_findings_stored_are_bounded(self):
        many = [f"- **[Low] x** {i}" for i in range(100)]
        o._record_review_history("repo", "1", "/nx", "f", "d", SHA(1), review(findings=many))
        self.assertEqual(len(o._load_history()[0]["findings"]), o.HISTORY_MAX_FINDINGS)

    def test_nothing_to_record_is_a_no_op(self):
        o._record_review_history("repo", "1", "/nx", "f", "d", "", review())
        o._record_review_history("repo", "1", "/nx", "f", "d", SHA(1), None)
        self.assertEqual(o._load_history(), [])

    def test_a_corrupt_file_reads_as_empty_and_is_replaced_on_next_write(self):
        self.mem.mkdir(parents=True)
        (self.mem / "review_history.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(o._load_history(), [])
        o._record_review_history("repo", "1", "/nx", "f", "d", SHA(1), review())
        self.assertEqual(len(o._load_history()), 1)

    def test_junk_shaped_files_are_ignored(self):
        self.mem.mkdir(parents=True)
        for junk in ('[]', '{"records": 5}', '{"records": [1, "x", null]}', "null",
                     '{"records": [{"at": "yesterday"}]}'):
            (self.mem / "review_history.json").write_text(junk, encoding="utf-8")
            self.assertEqual(o._load_history(), [])

    def test_an_unusable_location_never_raises(self):
        blocker = self.tmp / "afile"
        blocker.write_text("i am a file, not a directory")
        with mock.patch.dict(os.environ, {"PRISM_HISTORY_DIR": str(blocker)}):
            o._record_review_history("repo", "1", "/nx", "f", "d", SHA(1), review())
            self.assertEqual(o._load_history(), [])


def rec(pr, commits, at, repo="repo", hunks=None, **kw):
    return dict({"repo": repo, "pr_id": str(pr), "commits": [SHA(c) for c in commits],
                 "commit": SHA(commits[0]), "dest": "develop", "at": at,
                 "hunks": hunks or {}, "verdict_raw": "x", "findings": []}, **kw)


class Matching(unittest.TestCase):
    def match(self, records, commits=(), hunks=None, **kw):
        return o.related_reviews(records, "repo", commits=[SHA(c) for c in commits],
                                 hunks=hunks, **kw)

    def test_needs_a_shared_commit_or_enough_shared_code(self):
        self.assertEqual(self.match([rec(1, [1, 2], 1)], [3, 4]), [])
        self.assertEqual(self.match([rec(1, [1], 1, hunks={"a.py": ["h1"]})], [9],
                                    {"b.py": ["h2"]}), [])

    def test_other_repositories_never_match(self):
        self.assertEqual(self.match([rec(1, [1], 1, repo="other")], [1]), [])

    def test_same_code_matches_even_though_every_sha_differs(self):
        r = rec(1, [1, 2], 1, hunks={"a.py": ["h1", "h2"], "b.py": ["h3"]})
        (m,) = self.match([r], [50, 51], {"a.py": ["h1", "h2"], "b.py": ["h3"]})
        self.assertEqual((m["hunk_overlap"], m["hunk_total"], m["sha_overlap"]), (3, 3, 0))
        self.assertEqual(m["matched_files"], ["a.py", "b.py"])

    def test_a_pr_that_bundles_other_work_still_matches_each_earlier_review(self):
        a = rec(1, [1], 1, hunks={"a.py": ["h1", "h2"]})
        b = rec(2, [2], 2, hunks={"b.py": ["h3", "h4"]})
        got = self.match([a, b], [90], {"a.py": ["h1", "h2"], "b.py": ["h3", "h4"],
                                        "c.py": ["h5"]})
        self.assertEqual({m["record"]["pr_id"] for m in got}, {"1", "2"})

    def test_a_follow_up_fix_on_reviewed_code_still_matches(self):
        r = rec(1, [1], 1, hunks={"a.py": ["h1", "h2", "h3", "h4"]})
        (m,) = self.match([r], [90], {"a.py": ["h1", "h2", "h3", "NEW"]})
        self.assertEqual(m["hunk_overlap"], 3)

    def test_a_lone_shared_boilerplate_hunk_is_not_a_relationship(self):
        r = rec(1, [1], 1, hunks={"v.py": ["ver"] + [f"x{i}" for i in range(9)]})
        self.assertEqual(self.match([r], [90], {"v.py": ["ver"]}), [])

    def test_a_shared_commit_is_enough_even_without_hunks(self):
        (m,) = self.match([rec(1, [1, 2], 1)], [2])
        self.assertEqual((m["sha_overlap"], m["hunk_total"]), (1, 0))

    def test_a_review_wholly_inside_this_pr_outranks_a_barely_overlapping_one(self):
        records = [rec(1, [1, 2, 3, 4, 5, 6, 7, 8], 9), rec(2, [1, 2], 1)]
        order = [m["record"]["pr_id"] for m in self.match(records, [1, 2, 20])]
        self.assertEqual(order, ["2", "1"])

    def test_latest_review_per_pr_wins(self):
        records = [rec(1, [1, 2], 1, verdict_raw="old"), rec(1, [1, 2], 5, verdict_raw="new")]
        (only,) = self.match(records, [1])
        self.assertEqual(only["record"]["verdict_raw"], "new")

    def test_the_skipped_pr_is_left_out(self):
        self.assertEqual(self.match([rec(7, [1], 1)], [1], skip_pr="7"), [])

    def test_limit_and_empty_input(self):
        records = [rec(n, [1], n) for n in range(10)]
        self.assertEqual(len(self.match(records, [1])), o.HISTORY_RELATED_MAX)
        self.assertEqual(self.match(records, []), [])

    def test_garbage_records_are_tolerated(self):
        junk = [{"repo": "repo", "commits": "nope", "hunks": [1]},
                {"repo": "repo", "hunks": {"a": "notalist"}}]
        self.assertEqual(self.match(junk, [1], {"a.py": ["h"]}), [])


class Block(Isolated):
    def related(self, **kw):
        r = {"repo": "repo", "pr_id": "10", "commit": SHA(1), "dest": "develop",
             "verdict_raw": "✅ Approve with comments", "impact": "4",
             "hunks": {"a.py": ["h1"], "b.py": ["h2"]},
             "findings": ["- **[Medium] x — `a.py:3`** one", "- **[Low] y — `b.py:9`** two",
                          "- **[Low] z — `c.py:1`** three"]}
        r.update(kw)
        return [{"record": r, "score": 1.0, "at": 1, "hunk_overlap": 1, "hunk_total": 2,
                 "sha_overlap": 0, "sha_total": 1, "matched_files": ["a.py"]}]

    def test_empty_means_no_block_at_all(self):
        self.assertEqual(o.history_block([], "20"), "")

    def test_block_is_fenced_labelled_as_data_and_states_the_rules(self):
        text = o.history_block(self.related(), "20")
        for needle in ("REVIEW HISTORY", "<<<HISTORY", "HISTORY>>>", "PR #10", "→ develop",
                       "1 of its 2 changed blocks of code recur", "late find",
                       "NEVER an approval", "rather than by commit id"):
            self.assertIn(needle, text)

    def test_each_finding_says_whether_its_code_recurs(self):
        text = o.history_block(self.related(), "20")
        self.assertIn("one [same code in this PR]", text)
        self.assertIn("two [code has changed since]", text)
        self.assertIn("three", text)
        self.assertNotIn("three [", text)          # unknown file: no claim made

    def test_a_record_without_hunks_is_described_by_commits(self):
        rel = self.related(hunks={})
        rel[0]["hunk_total"] = 0
        rel[0]["sha_overlap"], rel[0]["sha_total"] = 2, 3
        self.assertIn("2 of its 3 commits are part of this PR", o.history_block(rel, "20"))

    def test_block_is_bounded(self):
        big = ["- **[High] x** " + "y" * 2000] * 25
        text = o.history_block(self.related(findings=big), "20")
        self.assertLess(len(text), o.HISTORY_PROMPT_CHARS + 2500)
        self.assertIn("more finding", text)


class RealGit(Isolated):
    """A real origin and clone. `app.py` has four far-apart regions so a change
    produces several separate hunks."""

    BASE = "".join(f"line {i}\n" for i in range(1, 81))

    def edit(self, path, n, text):
        lines = (self.clone / path).read_text().splitlines(True)
        lines[n - 1] = text + "\n"
        (self.clone / path).write_text("".join(lines))

    def setUp(self):
        super().setUp()
        self.origin = self.tmp / "origin.git"
        subprocess.run(["git", "init", "--bare", "-b", "develop", str(self.origin)],
                       check=True, capture_output=True)
        self.clone = self.tmp / "clone"
        subprocess.run(["git", "clone", str(self.origin), str(self.clone)],
                       check=True, capture_output=True)
        (self.clone / "app.py").write_text(self.BASE)
        (self.clone / "other.py").write_text(self.BASE)
        git(self.clone, "add", ".")
        git(self.clone, "commit", "-m", "base")
        git(self.clone, "push", "origin", "HEAD:develop")
        git(self.clone, "push", "origin", "HEAD:qa")
        self.base = git(self.clone, "rev-parse", "HEAD")

    def feature_a(self, branch="feature-a"):
        """Feature A: two commits touching four regions of app.py."""
        git(self.clone, "checkout", "-q", "-b", branch, self.base)
        self.edit("app.py", 5, "A first")
        self.edit("app.py", 25, "A second")
        git(self.clone, "commit", "-qam", "A part 1")
        self.edit("app.py", 45, "A third")
        self.edit("app.py", 70, "A fourth")
        git(self.clone, "commit", "-qam", "A part 2")
        git(self.clone, "push", "-q", "origin", branch)
        return git(self.clone, "rev-parse", "HEAD")

    def hunks(self, spec):
        git(self.clone, "fetch", "-q", "origin")
        return o._diff_hunks(str(self.clone), spec)

    def record(self, pr, dest, tip, src="feature-a", findings=None):
        git(self.clone, "fetch", "-q", "origin")
        o._record_review_history(
            "repo", pr, str(self.clone), src, dest, tip,
            review(findings=findings or ["- **[High] error-handling — `app.py:5`** swallowed"]))

    def related_for(self, dest, tip, skip=None):
        git(self.clone, "fetch", "-q", "origin")
        commits = o._range_commits(str(self.clone), f"origin/{dest}", tip)
        hunks = o._diff_hunks(str(self.clone), f"origin/{dest}...{tip}")
        return o.related_reviews(o._load_history(), "repo", commits=commits, hunks=hunks,
                                 skip_pr=skip)

    # ---- fingerprints -------------------------------------------------
    def test_fingerprints_cover_every_hunk_of_the_change(self):
        tip = self.feature_a()
        h = self.hunks(f"origin/qa...{tip}")
        self.assertEqual(list(h), ["app.py"])
        self.assertEqual(len(h["app.py"]), 4)

    def test_line_numbers_and_whitespace_do_not_matter(self):
        tip = self.feature_a()
        before = self.hunks(f"origin/qa...{tip}")
        # The same edits, one blank line lower, with different indentation.
        git(self.clone, "checkout", "-q", "-b", "shifted", self.base)
        (self.clone / "app.py").write_text("# header\n" + self.BASE)
        git(self.clone, "commit", "-qam", "shift everything down")
        git(self.clone, "push", "-q", "origin", "shifted")
        shifted_base = git(self.clone, "rev-parse", "HEAD")
        self.edit("app.py", 6, "  A   first ")
        self.edit("app.py", 26, "A second")
        self.edit("app.py", 46, "A third")
        self.edit("app.py", 71, "A fourth")
        git(self.clone, "commit", "-qam", "A reformatted")
        after = self.hunks(f"{shifted_base}..HEAD")
        self.assertEqual(sorted(before["app.py"]), sorted(after["app.py"]))

    def test_removed_lines_that_look_like_diff_headers_are_ordinary_lines(self):
        (self.clone / "q.sql").write_text("-- note one\nSELECT 1;\n-- note two\n")
        git(self.clone, "add", ".")
        git(self.clone, "commit", "-qm", "sql")
        git(self.clone, "push", "-q", "origin", "HEAD:develop")
        git(self.clone, "checkout", "-q", "-b", "drop-comments")
        (self.clone / "q.sql").write_text("SELECT 1;\n")
        git(self.clone, "commit", "-qam", "drop comments")
        git(self.clone, "push", "-q", "origin", "drop-comments")
        h = self.hunks("origin/develop...origin/drop-comments")
        self.assertEqual(list(h), ["q.sql"])
        self.assertEqual(len(h["q.sql"]), 2)

    def test_added_and_deleted_files_are_keyed_by_path(self):
        git(self.clone, "checkout", "-q", "-b", "files", self.base)
        (self.clone / "new.py").write_text("x = 1\n")
        git(self.clone, "rm", "-q", "other.py")
        git(self.clone, "add", ".")
        git(self.clone, "commit", "-qm", "add and delete")
        git(self.clone, "push", "-q", "origin", "files")
        h = self.hunks("origin/qa...origin/files")
        self.assertEqual(sorted(h), ["new.py", "other.py"])

    def test_the_hunk_cap_is_respected_and_failures_give_nothing(self):
        tip = self.feature_a()
        git(self.clone, "fetch", "-q", "origin")
        self.assertEqual(len(o._diff_hunks(str(self.clone), f"origin/qa...{tip}",
                                           limit=2)["app.py"]), 2)
        self.assertEqual(o._diff_hunks(str(self.clone), "nonexistent...alsonot"), {})
        self.assertEqual(o._diff_hunks("/does/not/exist", "a...b"), {})
        self.assertEqual(o._diff_hunks(str(self.clone), f"{tip}...{tip}"), {})

    # ---- the same change under different shas ---------------------------
    def test_a_rebase_changes_every_sha_but_the_review_still_matches(self):
        tip = self.feature_a()
        self.record("10", "qa", tip)
        # develop moves on, and the feature branch is rebased onto it.
        git(self.clone, "checkout", "-q", "-b", "newbase", self.base)
        self.edit("other.py", 40, "unrelated upstream change")
        git(self.clone, "commit", "-qam", "upstream")
        git(self.clone, "push", "-q", "origin", "newbase:develop")
        git(self.clone, "checkout", "-q", "feature-a")
        git(self.clone, "rebase", "-q", "newbase")
        git(self.clone, "push", "-q", "-f", "origin", "feature-a")
        new_tip = git(self.clone, "rev-parse", "HEAD")
        self.assertNotEqual(new_tip, tip)
        (m,) = self.related_for("develop", new_tip)
        self.assertEqual(m["sha_overlap"], 0, "every sha changed")
        self.assertEqual((m["hunk_overlap"], m["hunk_total"]), (4, 4))

    def test_a_squash_still_matches(self):
        tip = self.feature_a()
        self.record("10", "qa", tip)
        git(self.clone, "checkout", "-q", "-b", "squashed", self.base)
        git(self.clone, "merge", "--squash", "-q", "feature-a")
        git(self.clone, "commit", "-qm", "A squashed")
        git(self.clone, "push", "-q", "origin", "squashed")
        sq = git(self.clone, "rev-parse", "HEAD")
        (m,) = self.related_for("qa", sq)
        self.assertEqual((m["sha_overlap"], m["hunk_overlap"]), (0, 4))

    def test_a_cherry_pick_onto_another_branch_still_matches(self):
        tip = self.feature_a()
        self.record("10", "qa", tip)
        git(self.clone, "checkout", "-q", "-b", "staging-fix", self.base)
        git(self.clone, "cherry-pick", f"{self.base}..feature-a")
        git(self.clone, "push", "-q", "origin", "staging-fix")
        pick = git(self.clone, "rev-parse", "HEAD")
        (m,) = self.related_for("qa", pick)
        # (A cherry-pick made in the same second can reproduce the very same sha,
        # so only the content match is asserted.)
        self.assertEqual(m["hunk_overlap"], 4)

    def test_a_promotion_that_bundles_other_features_matches_each_earlier_review(self):
        a_tip = self.feature_a()
        self.record("10", "develop", a_tip)
        git(self.clone, "checkout", "-q", "-b", "feature-b", self.base)
        self.edit("other.py", 10, "B one")
        self.edit("other.py", 60, "B two")
        git(self.clone, "commit", "-qam", "B")
        git(self.clone, "push", "-q", "origin", "feature-b")
        b_tip = git(self.clone, "rev-parse", "HEAD")
        self.record("11", "develop", b_tip, src="feature-b", findings=["- **[Low] n — `other.py:10`** b"])
        # develop now holds both, rebuilt as new commits (e.g. merged then rebased).
        git(self.clone, "checkout", "-q", "-b", "develop-built", self.base)
        git(self.clone, "cherry-pick", f"{self.base}..feature-a")
        git(self.clone, "cherry-pick", f"{self.base}..feature-b")
        git(self.clone, "push", "-q", "origin", "develop-built:develop")
        promo = git(self.clone, "rev-parse", "HEAD")
        got = self.related_for("qa", promo)
        self.assertEqual({m["record"]["pr_id"] for m in got}, {"10", "11"})
        self.assertTrue(all(m["hunk_overlap"] == m["hunk_total"] for m in got))

    def test_a_follow_up_fix_keeps_the_match_and_marks_the_changed_finding(self):
        tip = self.feature_a()
        self.record("10", "qa", tip)
        self.edit("app.py", 5, "A first, fixed")
        git(self.clone, "commit", "-qam", "fix the first")
        git(self.clone, "push", "-q", "origin", "feature-a")
        fixed = git(self.clone, "rev-parse", "HEAD")
        (m,) = self.related_for("qa", fixed, skip=None)
        self.assertEqual((m["hunk_overlap"], m["hunk_total"]), (3, 4))
        self.assertIn("app.py", m["matched_files"])

    def test_matching_does_not_depend_on_branch_names_or_layout(self):
        """Reviewed against one branch, seen again against a differently named
        one (and again against the very same one): the code is what matches."""
        tip = self.feature_a()
        self.record("10", "qa", tip)
        for name in ("hotfix-line", "release/2026.10", "trunk", "qa"):
            git(self.clone, "push", "-q", "origin", f"{self.base}:refs/heads/{name}", "--force")
            (m,) = self.related_for(name, tip)
            self.assertEqual(m["hunk_overlap"], 4, name)

    def test_an_unrelated_pr_gets_nothing(self):
        tip = self.feature_a()
        self.record("10", "qa", tip)
        git(self.clone, "checkout", "-q", "-b", "other", self.base)
        self.edit("other.py", 33, "something else entirely")
        git(self.clone, "commit", "-qam", "unrelated")
        git(self.clone, "push", "-q", "origin", "other")
        self.assertEqual(self.related_for("qa", git(self.clone, "rev-parse", "HEAD")), [])

    # ---- the context the reviewer actually gets -------------------------
    def test_a_promotion_pr_gets_the_earlier_stages_findings(self):
        tip = self.feature_a()
        self.record("10", "qa", tip)
        logged = []
        block = o._review_history_context(
            "20", "repo", str(self.clone), "feature-a", "qa", tip,
            emit=lambda m, *a, **k: logged.append(m))
        self.assertIn("REVIEW HISTORY", block)
        self.assertIn("swallowed", block)
        self.assertIn("PR #10", block)
        self.assertIn("4 of its 4 changed blocks", block)
        self.assertTrue(any("earlier review" in m for m in logged))

    def test_the_same_pr_on_an_unchanged_commit_still_gets_its_own_history(self):
        tip = self.feature_a()
        self.record("10", "qa", tip, findings=["- **[High] x — `app.py:5`** boom"])
        block = o._review_history_context("10", "repo", str(self.clone), "feature-a", "qa",
                                          tip, skip_pr=None, emit=QUIET)
        self.assertIn("boom", block)

    def test_a_failed_fetch_means_no_history_rather_than_stale_history(self):
        tip = self.feature_a()
        self.record("10", "qa", tip)
        git(self.clone, "remote", "set-url", "origin", str(self.tmp / "gone.git"))
        self.assertEqual(o._review_history_context(
            "20", "repo", str(self.clone), "feature-a", "qa", tip, emit=QUIET), "")

    def test_never_raises_whatever_is_wrong(self):
        tip = self.feature_a()
        self.record("10", "qa", tip)
        self.assertEqual(o._review_history_context(
            "20", "repo", "/does/not/exist", "feature-a", "qa", tip, emit=QUIET), "")
        self.assertEqual(o._review_history_context(
            "20", "repo", str(self.clone), "", "", "", emit=QUIET), "")

    def test_the_reviewer_actually_receives_the_earlier_findings(self):
        """Real run_opencode_review, real git, real memory — only the model is stubbed."""
        tip = self.feature_a()
        self.record("10", "qa", tip)
        captured = {}
        o._incremental_context = lambda *a, **k: None
        o.ensure_bundled_agents = lambda *a, **k: None
        o.require_engine = lambda: "opencode"
        o.engine_supports_json = lambda exe: False
        o.get_pr = lambda *a, **k: {"sourceReference": "feature-a", "destinationReference": "qa",
                                    "sourceCommit": tip}

        def fake_stream(cmd, cwd, emit, control=None, json_mode=False, meter=None):
            captured["prompt"] = cmd[-1]
            return 0, "**Verdict:** OK Approve\n", "sid"
        o._run_stream_resilient = fake_stream
        o.run_opencode_review("20", "repo", str(self.clone), str(self.clone), emit=QUIET)
        prompt = captured["prompt"]
        self.assertIn("REVIEW HISTORY", prompt)
        self.assertIn("swallowed", prompt)
        self.assertLess(prompt.index("REVIEW HISTORY"), prompt.index("Review CodeCommit"))

    # ---- PRISM only reviews ---------------------------------------------
    def test_nothing_is_ever_written_to_the_repository(self):
        tip = self.feature_a()
        git(self.clone, "fetch", "-q", "origin")

        def snapshot():
            return (git(self.clone, "status", "--porcelain", "--ignored"),
                    git(self.clone, "for-each-ref", "refs/heads", "refs/tags"),
                    git(self.clone, "rev-parse", "HEAD"),
                    git(self.clone, "ls-remote", "origin"),
                    sorted(p.name for p in self.clone.iterdir()))
        before = snapshot()
        self.record("10", "qa", tip)
        o._review_history_context("20", "repo", str(self.clone), "feature-a", "qa", tip,
                                  emit=QUIET)
        self.assertEqual(snapshot(), before)
        self.assertTrue(str(self.mem).startswith(str(self.tmp)))
        self.assertFalse(str(self.mem).startswith(str(self.clone)))


class PromptWiring(Isolated):
    """The history block reaches the reviewer — and its absence changes nothing."""

    def prompt(self, history, incremental=None, instructions=""):
        captured = {}
        o._incremental_context = lambda *a, **k: incremental
        o.ensure_bundled_agents = lambda *a, **k: None
        o.require_engine = lambda: "opencode"
        o.engine_supports_json = lambda exe: False
        o.get_pr = lambda *a, **k: {"sourceReference": "feat/x", "destinationReference": "qa",
                                    "sourceCommit": SHA(9)}
        o._review_history_context = lambda *a, **k: (captured.update(skip=k.get("skip_pr"))
                                                     or history)

        def fake_stream(cmd, cwd, emit, control=None, json_mode=False, meter=None):
            captured["prompt"] = cmd[-1]
            return 0, "**Verdict:** OK Approve\n", "sid"
        o._run_stream_resilient = fake_stream
        o.run_opencode_review("7", "repo", "/tmp/repo", "/tmp/proj", emit=QUIET,
                              custom_instructions=instructions)
        return captured

    def test_block_is_in_the_prompt_after_instructions_and_before_the_task(self):
        got = self.prompt("REVIEW HISTORY block\n\n", instructions="focus on auth")
        p = got["prompt"]
        self.assertTrue(p.startswith("REVIEWER INSTRUCTIONS"))
        self.assertLess(p.index("focus on auth"), p.index("REVIEW HISTORY block"))
        self.assertLess(p.index("REVIEW HISTORY block"), p.index("Review CodeCommit pull request"))

    def test_no_history_leaves_the_prompt_exactly_as_before(self):
        p = self.prompt("")["prompt"]
        self.assertTrue(p.startswith("Review CodeCommit pull request"))
        self.assertNotIn("HISTORY", p)

    def test_an_incremental_review_does_not_repeat_its_own_pr(self):
        inc = {"commit": "old", "current_commit": "new", "verdict": "approve", "block": "- f"}
        got = self.prompt("H\n\n", incremental=inc)
        self.assertEqual(got["skip"], "7")
        self.assertIn("INCREMENTAL", got["prompt"])
        self.assertIn("H", got["prompt"])

    def test_a_non_incremental_review_does_not_skip_its_own_pr(self):
        self.assertIsNone(self.prompt("H\n\n")["skip"])


class LateFinds(Isolated):
    def test_counting(self):
        findings = ["- **[High] x — `a:1`** boom (late find — missed in PR #10)",
                    "- **[Low] y — `b:2`** nit", "- **[Medium] z** Late-Find, PR #3"]
        self.assertEqual(o.count_late_findings(findings), 2)
        self.assertEqual(o.count_late_findings(None), 0)

    def test_a_long_one_line_finding_with_a_same_pattern_note_survives_parsing(self):
        line = ("- **[High] error-handling — `a.py:3`** swallowed " + "x" * 900 +
                " Same pattern also at: b.py:1, c.py:2.")
        res = o.parse_review_output("**Verdict:** 🔴 Request changes\n"
                                    "**Impact score:** 5/10 — r\n" + line + "\n")
        (kept,) = res.findings
        self.assertIn("Same pattern also at: b.py:1, c.py:2.", kept)
        self.assertLessEqual(len(kept), o.FINDING_MAX_CHARS)

    def test_description_flags_late_finds_only_when_there_are_some(self):
        written = {}
        o.get_pr = lambda *a, **k: {"description": ""}
        o.aws_cli = lambda *a, **k: written.update(d=a[a.index("--description") + 1]) or {}
        o.update_description_direct("7", "Request changes", "5", ["- **[High] x** y"],
                                    verdict_key="request-changes", source_commit="abc",
                                    late=0)
        self.assertNotIn("late finding", written["d"])
        o.update_description_direct("7", "Request changes", "5", ["- **[High] x** y"],
                                    verdict_key="request-changes", source_commit="abc",
                                    late=2)
        self.assertIn("2 late finding(s)", written["d"])
        self.assertIn("<!-- prism:start -->", written["d"])
        self.assertIn("<!-- prism:end -->", written["d"])

    def test_old_callers_without_the_new_argument_still_work(self):
        o.get_pr = lambda *a, **k: {"description": ""}
        o.aws_cli = lambda *a, **k: {}
        o.update_description_direct("7", "Approve", "1", [], verdict_key="approve")


class PipelineIntegration(Isolated):
    """The pipeline records history and surfaces late finds, and a failure in
    either never changes what the pipeline decides."""

    def setUp(self):
        super().setUp()
        self.clone = str(self.tmp)
        self.merged = []
        self.logged = []
        o.ensure_bundled_agents = lambda *a, **k: ["pr-reviewer"]
        o.resolve_repo = lambda r, p, l: (r, self.clone)
        o.check_ff_mergeable = lambda *a, **k: (True, "ok")
        o.try_fast_forward_merge = lambda *a, **k: self.merged.append(1) or {}
        self.desc_kwargs = {}
        o.update_description_direct = lambda *a, **k: self.desc_kwargs.update(k) or ""
        o.get_pr = lambda *a, **k: {
            "status": "OPEN", "repositoryName": "repo", "destinationReference": "qa",
            "sourceReference": "feat", "description": "", "sourceCommit": SHA(5)}

    def run_with(self, report):
        o.run_opencode_review = lambda *a, **k: (report, "ses")
        return o.full_pipeline(self.clone, "repo", "7", local_repo=self.clone,
                               emit=lambda m, *a, **k: self.logged.append(m))

    APPROVE = "**Verdict:** ✅ Approve\n**Impact score:** 2/10 — small\n"

    def test_a_completed_review_is_recorded(self):
        self.run_with(self.APPROVE)
        (rec,) = o._load_history()
        self.assertEqual((rec["repo"], rec["pr_id"], rec["dest"], rec["commit"]),
                         ("repo", "7", "qa", SHA(5)))

    def test_late_finds_are_surfaced_and_passed_to_the_description(self):
        report = ("**Verdict:** 🔴 Request changes\n**Impact score:** 6/10 — r\n"
                  "- **[High] x — `a:1`** boom (late find — missed in PR #3)\n")
        self.run_with(report)
        self.assertEqual(self.desc_kwargs.get("late"), 1)
        self.assertTrue(any("late finding" in m for m in self.logged))

    def test_no_late_finds_means_no_warning(self):
        self.run_with(self.APPROVE)
        self.assertEqual(self.desc_kwargs.get("late"), 0)
        self.assertFalse(any("late finding" in m for m in self.logged))

    def test_a_broken_ledger_cannot_change_the_outcome(self):
        # Break the ledger from inside (git unavailable, disk full) ...
        o._range_commits = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no git"))
        (self.tmp / "afile").write_text("not a directory")
        o._REVIEWED_STORE_PATH = self.tmp / "afile" / "reviewed_commits.json"
        res = self.run_with(self.APPROVE)
        self.assertTrue(res["merged"])

    def test_even_a_raising_recorder_cannot_fail_the_pipeline(self):
        # ... and at the call site, should the recorder itself ever raise.
        o._record_review_history = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))
        res = self.run_with(self.APPROVE)
        self.assertTrue(res["merged"])

    def test_the_verdict_and_merge_decision_do_not_depend_on_history(self):
        res = self.run_with("**Verdict:** 🔴 Request changes\n**Impact score:** 4/10 — r\n"
                            "- **[Low] n — `a:1`** nit (late find — missed in PR #3)\n")
        self.assertFalse(res["merged"])
        self.assertEqual(res["stopped"], "verdict-blocks-merge")
        self.assertEqual(self.merged, [])


class NoAssumedBranchLayout(Isolated):
    """PRISM is used with many branching strategies. Wording that names a
    particular set of branches (or an order between them) quietly teaches the
    reviewer, and the reader, that one layout is the norm."""

    NAMES = __import__("re").compile(r"\b(develop|staging|qa)\b|lower\s+environment", __import__("re").I)
    ROOT = Path(__file__).resolve().parent.parent

    def top_changelog_sections(self, n=2):
        text = (self.ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        return "\n".join(text.split("\n## ")[1:1 + n])

    def test_product_text_names_no_branches(self):
        sources = {
            "agents/pr-reviewer.md": (self.ROOT / "agents" / "pr-reviewer.md").read_text("utf-8"),
            "docs/MANUAL.md": (self.ROOT / "docs" / "MANUAL.md").read_text("utf-8"),
            "docs/REVIEW_CONSISTENCY.md": (self.ROOT / "docs" / "REVIEW_CONSISTENCY.md").read_text("utf-8"),
            "docs/CODEGEN_INTEGRATION.md": (self.ROOT / "docs" / "CODEGEN_INTEGRATION.md").read_text("utf-8"),
            "CHANGELOG.md (latest two releases)": self.top_changelog_sections(),
        }
        for name, text in sources.items():
            hit = self.NAMES.search(text)
            self.assertIsNone(hit, f"{name} assumes a branch layout: {hit and hit.group(0)!r}")

    def test_messages_shown_to_users_and_the_reviewer_name_no_branches(self):
        related = [{"record": {"repo": "r", "pr_id": "1", "commit": "a" * 40, "dest": "main",
                               "verdict_raw": "x", "findings": []},
                    "score": 1.0, "at": 1, "hunk_overlap": 1, "hunk_total": 1,
                    "sha_overlap": 0, "sha_total": 0, "matched_files": []}]
        shown = [o.history_block(related, "2")]
        logged = []
        o.get_pr = lambda *a, **k: {"description": ""}
        written = {}
        o.aws_cli = lambda *a, **k: written.update(d=a[a.index("--description") + 1]) or {}
        o.update_description_direct("7", "Request changes", "5", ["- **[High] x** y"],
                                    verdict_key="request-changes", source_commit="abc", late=1)
        shown.append(written["d"])
        for text in shown + logged:
            self.assertIsNone(self.NAMES.search(text), text[:80])
        self.assertIn("test it again", written["d"])


class FirstReviewIsCheap(Isolated):
    """A first review of a repository must not pay for the history feature."""

    def test_no_git_or_network_work_without_an_earlier_review_of_this_repo(self):
        o._record_review_history("some-other-repo", "1", "/nx", "f", "d", SHA(1), review())
        self.assertEqual(len(o._load_history()), 1)
        calls = []
        real = o.subprocess.run
        o.subprocess.run = lambda *a, **k: calls.append(a) or real(*a, **k)
        try:
            self.assertEqual(o._review_history_context(
                "9", "this-repo", "/nonexistent", "feat", "main", SHA(2), emit=QUIET), "")
        finally:
            o.subprocess.run = real
        self.assertEqual(calls, [], "no git/network call for a repo with no history")

    def test_it_still_runs_when_this_repo_has_history(self):
        o._record_review_history("this-repo", "1", "/nx", "f", "d", SHA(1), review())
        calls = []
        real = o.subprocess.run
        o.subprocess.run = lambda *a, **k: calls.append(a) or real(*a, **k)
        try:
            o._review_history_context("9", "this-repo", "/nonexistent", "feat", "main",
                                      SHA(2), emit=QUIET)
        finally:
            o.subprocess.run = real
        self.assertTrue(calls, "a repo with history is looked up")


class ReviewerInstructions(unittest.TestCase):
    """The agent file is the other half of the fix; pin what must stay true."""

    @classmethod
    def setUpClass(cls):
        cls.text = (Path(o.AGENTS_DIR) / "pr-reviewer.md").read_text(encoding="utf-8")
        cls.front, _, cls.body = cls.text[4:].partition("\n---\n")

    def test_it_is_still_read_only(self):
        for denied in ('edit: deny', '"git push*": deny', '"git commit*": deny',
                       '"git merge*": deny', '"aws codecommit merge*": deny',
                       '"aws codecommit update*": deny', '"aws codecommit post*": deny'):
            self.assertIn(denied, self.front)

    def test_the_parsed_report_labels_are_unchanged(self):
        self.assertIn("**Verdict:** ✅ Approve | ⚠️ Approve with comments | "
                      "🔴 Request changes | ⛔ Block", self.body)
        self.assertIn("**Impact score:** <1-10>/10", self.body)
        self.assertIn("### Findings (most severe first)", self.body)

    def test_a_sample_report_in_the_documented_shape_still_parses(self):
        res = o.parse_review_output(
            "## PR #7 — repo (a → b)\n**Verdict:** 🔴 Request changes\n"
            "**Impact score:** 6/10 — shared module\n\n### Findings (most severe first)\n"
            "- **[High] error-handling — `svc.py:40`** exception swallowed. Same pattern also "
            "at: svc.py:88. (late find — missed in PR #3)\n\n"
            "### Summary\nDoes X.\nCoverage: all dimensions; skimmed docs.\n")
        self.assertEqual(res.verdict_key, "request-changes")
        self.assertEqual(res.impact_score, "6")
        self.assertEqual(len(res.findings), 1)
        self.assertEqual(o.count_late_findings(res.findings), 1)

    def test_the_first_pass_procedures_are_present(self):
        for needle in ("Step 0.6", "Failure handling and edge cases", "2a", "2b", "2c",
                       "Same pattern also at", "late find",
                       "Each finding must be a single line", "Coverage:",
                       "Impact of the change", "Scope and pace"):
            self.assertIn(needle, self.body, needle)

    def test_the_review_is_limited_to_the_diff_and_told_to_be_fast(self):
        for needle in ("Your scope is this pull request", "A review must be fast",
                       "Do not search the rest of the repository",
                       "Occurrences in code the PR\ndid not change are not reported"):
            self.assertIn(needle, self.body, needle)

    def test_no_instruction_sends_the_reviewer_across_the_repository(self):
        for banned in ("whole repository", "Outside this PR", "No other occurrences found",
                       "Sweep for the same defect elsewhere", "changed and unchanged",
                       "git grep -n '<pattern>'", "pre-existing occurrences"):
            self.assertNotIn(banned, self.body, banned)

    def test_history_is_never_an_approval(self):
        self.assertIn("lowers the bar", self.body)
        self.assertIn("independently", self.body)

    def test_a_high_or_critical_late_find_still_blocks(self):
        self.assertIn("Never use this to wave through a High or Critical issue", self.body)

    def test_it_installs_into_a_project(self):
        with tempfile.TemporaryDirectory() as proj:
            o.ensure_bundled_agents(proj, emit=QUIET)
            installed = (Path(proj) / ".opencode" / "agents" / "pr-reviewer.md").read_text("utf-8")
            self.assertEqual(installed, self.text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
