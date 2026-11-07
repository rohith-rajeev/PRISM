"""Reviewer personas and optional requirements for document review.

Three personas (Business Analyst, Architect, QA) read a document through different
lenses; requirements are optional and change what the review centres on; security,
stability and scalability are always checked. All of it is built by PRISM into the
reviewer's brief, so it is tested here as text and end to end (real git, canned
reviewer), including that each persona's reviews are tracked separately.
"""
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import cli_jobs as CJ  # noqa: E402
import config as C  # noqa: E402
import docreview as D  # noqa: E402
import orchestrator as O  # noqa: E402
import prism_cli as P  # noqa: E402
from test_cli import Isolated  # noqa: E402
from test_cli_docs import args, fixture, sh  # noqa: E402
from test_docreview import make_remote_and_clone  # noqa: E402

# A review that states its conclusions on the three always-checked areas.
FULL = """**Verdict:** ⚠️ Approve with comments
**Impact score:** 4/10 — small

<<<PRISM-REVIEW>>>
### Summary
Reviewed as the persona asked.

### Major
- **[Major] DR-{n}** · Pattern · §2 — diverges from how the codebase does it. Evidence: src/x.py.

### Minor
- **[Minor] DR-{m}** · Ambiguity · §3 — vague.

### Coverage
Read the document and the code.
- **Security:** nothing found; checked the permission checks in src/x.py.
- **Stability:** nothing found.
- **Scalability:** nothing found.
<<<END-PRISM-REVIEW>>>
"""


class PersonaDefinitions(unittest.TestCase):
    def test_names_and_aliases_resolve_to_a_persona(self):
        for text, key in (("ba", "ba"), ("BA", "ba"), ("Business Analyst", "ba"),
                          ("business-analyst", "ba"), ("business_analyst", "ba"),
                          ("architect", "architect"), ("ARCHITECT", "architect"),
                          ("arch", "architect"), ("qa", "qa"), ("QA", "qa"), ("quality", "qa"),
                          ("", "ba"), (None, "ba"), ("  ", "ba")):
            with self.subTest(text):
                self.assertEqual(D.normalise_persona(text), key)

    def test_an_unknown_persona_names_the_choices(self):
        with self.assertRaises(ValueError) as cm:
            D.normalise_persona("designer")
        for word in ("ba", "architect", "qa", "Business Analyst"):
            self.assertIn(word, str(cm.exception))

    def test_each_lens_is_about_its_own_side(self):
        lens = {k: v["lens"].lower() for k, v in D.PERSONAS.items()}
        self.assertEqual(len(set(lens.values())), 3)
        self.assertIn("functional", lens["ba"])
        self.assertIn("align", lens["ba"])
        self.assertIn("existing technical patterns", lens["architect"])
        self.assertIn("security loopholes", lens["architect"])
        self.assertIn("inconsistencies", lens["architect"])
        self.assertIn("break anything", lens["qa"])
        self.assertIn("quality", lens["qa"])
        self.assertIn("testable", lens["qa"])

    def test_the_spec_carries_and_shows_the_persona(self):
        t = D.DocTarget("repo", "r", "/r", "b", ("a.md",))
        s = D.DocJobSpec("/p", (t,), persona="architect")
        self.assertIn("Architect", s.label)
        self.assertIn("Architect review", s.summary_bits())
        self.assertEqual(s.pipeline_kwargs()["persona"], "architect")
        default = D.DocJobSpec("/p", (t,))
        self.assertEqual((default.persona, default.requirements_path), ("ba", ""))
        self.assertIn("no requirements", default.summary_bits())
        self.assertNotIn("no requirements", D.DocJobSpec("/p", (t,), requirements_path="r")
                         .summary_bits())


class TheBrief(unittest.TestCase):
    def brief(self, persona="ba", req_folder=None):
        t = D.DocTarget("repo", "r", "/r", "b", ("a.md",))
        return D.build_prompt(t, "a.md", "/wt/a.md", req_folder,
                              [Path("/wt/reqs/one.md")] if req_folder else [], False, [], "/wt",
                              [], "", "", 1, persona=persona)

    def test_without_requirements_the_review_is_of_the_document_against_the_codebase(self):
        p = self.brief(req_folder=None)
        self.assertIn("NO REQUIREMENTS WERE PROVIDED", p)
        self.assertIn("do not report missing requirements", p)
        self.assertIn("consistent with the codebase and its existing patterns", p)
        self.assertNotIn("REQUIREMENTS PROVIDED.", p)
        self.assertNotIn("Requirement documents", p)
        self.assertNotIn("Requirement files", p)

    def test_with_requirements_the_review_centres_on_satisfying_all_of_them(self):
        p = self.brief(req_folder="/wt/reqs")
        self.assertIn("REQUIREMENTS PROVIDED.", p)
        self.assertIn("ENTIRE requirement set", p)
        self.assertIn("/wt/reqs", p)
        self.assertIn("one.md", p)
        self.assertNotIn("NO REQUIREMENTS WERE PROVIDED", p)

    def test_every_persona_gets_its_lens_and_the_checks_that_are_never_skipped(self):
        for key, info in D.PERSONAS.items():
            for req in (None, "/wt/reqs"):
                with self.subTest(persona=key, requirements=bool(req)):
                    p = self.brief(key, req)
                    self.assertIn(f"PERSONA: {info['label']}.", p)
                    self.assertIn(info["lens"], p)
                    for word in ("security", "stability", "scalability", "ALWAYS CHECKED"):
                        self.assertIn(word, p)
                    for other, other_info in D.PERSONAS.items():
                        if other != key:
                            self.assertNotIn(other_info["lens"], p)

    def test_the_agent_file_explains_all_three_ideas(self):
        text = (HERE.parent / "agents" / "doc-reviewer.md").read_text(encoding="utf-8")
        for needle in ("Business Analyst", "Architect", "QA", "Whether requirements were provided",
                       "Not provided", "never skipped",
                       "security", "stability", "scalability", "Pattern", "Regression"):
            self.assertIn(needle, text)

    def test_the_always_checks_are_noticed_when_a_review_skips_one(self):
        self.assertEqual(D.missing_always_checks(FULL), [])
        self.assertEqual(D.missing_always_checks("Security: ok. Stability: ok."), ["scalab"])
        self.assertEqual(D.missing_always_checks("nothing"), ["security", "stability", "scalab"])
        self.assertEqual(D.ALWAYS_NAMES["scalab"], "scalability")


class Pipeline(unittest.TestCase):
    """Real git; the reviewer is a canned reply that records what it was asked."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.origin, self.clone = make_remote_and_clone(self.tmp.name)
        self.project = Path(self.tmp.name) / "proj"
        self.project.mkdir()
        saved = (O.ensure_bundled_agents, D._run_agent)
        O.ensure_bundled_agents = lambda *a, **k: ["doc-reviewer"]
        D._run_agent = self.fake_agent
        self.addCleanup(lambda: (setattr(O, "ensure_bundled_agents", saved[0]),
                                 setattr(D, "_run_agent", saved[1])))
        self.prompts, self.log, self.reply = [], [], FULL

    def fake_agent(self, prompt, project_dir, emit, model, control, meter):
        import re
        self.prompts.append(prompt)
        n = int(re.search(r"Start new comments at DR-(\d+)", prompt).group(1))
        return 0, self.reply.format(n=n, m=n + 1), "sess"

    def run_job(self, **kw):
        t = D.DocTarget("repo", "clone", str(self.clone), "feat/x", ("docs/spec.md",))
        return D._pipeline(project_dir=str(self.project), targets=[t],
                           emit=lambda m, *a, **k: self.log.append(m), **kw)

    @staticmethod
    def mentions(prompt, *parts):
        """A path in the brief, spelt the way this platform spells it (docs\\requirements on
        Windows, docs/requirements elsewhere)."""
        return "\\".join(parts) in prompt or "/".join(parts) in prompt

    def remote(self):
        return sh(self.origin, "show", "feat/x:docs/spec.md")

    def subjects(self):
        return sh(self.origin, "log", "--format=%s", "feat/x").splitlines()

    # ----- requirements are optional -----
    def test_no_requirements_given_is_a_normal_review_even_if_a_requirements_folder_exists(self):
        res = self.run_job()                       # docs/requirements exists in the repo ...
        self.assertTrue(res["pushed"])
        (prompt,) = self.prompts
        self.assertIn("NO REQUIREMENTS WERE PROVIDED", prompt)    # ... but was not asked for
        self.assertFalse(self.mentions(prompt, "docs", "requirements"))
        self.assertTrue(any("No requirements given" in m for m in self.log))

    def test_requirements_given_are_used(self):
        self.run_job(requirements_path="docs/requirements")
        (prompt,) = self.prompts
        self.assertIn("REQUIREMENTS PROVIDED.", prompt)
        self.assertTrue(self.mentions(prompt, "docs", "requirements"))
        self.assertIn("pay.md", prompt)

    def test_a_path_that_is_given_but_missing_is_an_error_not_silently_ignored(self):
        with self.assertRaises(RuntimeError) as cm:
            self.run_job(requirements_path="no/such/dir")
        self.assertIn("leave the requirements empty", str(cm.exception))
        self.assertEqual(self.prompts, [])

    # ----- personas -----
    def test_the_persona_reaches_the_reviewer_and_the_document(self):
        self.run_job(persona="architect")
        self.assertIn("PERSONA: Architect.", self.prompts[0])
        text = self.remote()
        self.assertIn("Review v1 · Architect · run", text)
        _b, runs, _a = D.split_document(text)
        self.assertEqual((runs[0].meta["persona"], runs[0].meta["persona_label"]),
                         ("architect", "Architect"))
        self.assertIn("Architect review comments v1", self.subjects()[0])

    def test_an_unknown_persona_fails_before_any_work(self):
        with self.assertRaises(ValueError):
            self.run_job(persona="designer")
        self.assertEqual(self.prompts, [])

    def test_a_review_that_skips_security_stability_or_scalability_is_flagged(self):
        self.reply = FULL.replace("- **Scalability:** nothing found.\n", "")
        self.run_job()
        self.assertTrue(any("did not state a conclusion on scalability" in m for m in self.log))
        self.log.clear()
        self.reply = FULL
        self.run_job()
        self.assertFalse(any("did not state a conclusion" in m for m in self.log))

    # ----- each persona's reviews are tracked separately -----
    def test_another_persona_starts_its_own_review_and_the_ids_keep_counting(self):
        self.run_job(persona="ba")
        self.run_job(persona="architect")
        self.assertNotIn("PREVIOUS REVIEW", self.prompts[1])        # a first Architect review
        self.assertIn("first Architect review", " ".join(self.log))
        self.assertIn("Start new comments at DR-3", self.prompts[1])
        _b, runs, _a = D.split_document(self.remote())
        self.assertEqual([(r.meta["v"], r.meta["persona"]) for r in runs],
                         [(2, "architect"), (1, "ba")])

    def test_a_persona_follows_up_its_own_earlier_comments_not_anothers(self):
        self.run_job(persona="ba")                 # v1, DR-1 DR-2
        self.run_job(persona="architect")          # v2, DR-3 DR-4
        ba_id = D.split_document(self.remote())[1][1].meta["id"]
        arch_id = D.split_document(self.remote())[1][0].meta["id"]
        self.run_job(persona="ba")                 # v3: follows v1
        prompt = self.prompts[2]
        self.assertIn("PREVIOUS REVIEW", prompt)
        self.assertIn(f"run {ba_id}", prompt)
        self.assertNotIn(f"run {arch_id}", prompt)
        self.assertIn("DR-1", prompt)
        self.assertNotIn("DR-3", prompt)                            # the Architect's comment
        self.run_job(persona="architect")          # v4: follows v2
        prompt = self.prompts[3]
        self.assertIn(f"run {arch_id}", prompt)
        self.assertIn("DR-3", prompt)
        self.assertNotIn("DR-1 ", prompt)

    def test_a_review_from_before_personas_counts_as_the_earlier_review_for_any_persona(self):
        legacy = D.Run({"id": "old", "v": 1, "date": "d", "commit": "", "verdict": "x"},
                       "- **[Major] DR-1** · Gap · s — a legacy comment")
        runs = [legacy]
        for persona in D.PERSONAS:
            self.assertIs(D.latest_run(runs, persona), legacy)
        self.assertEqual(D.open_findings(runs, "qa")[0][1], "DR-1")
        block = D.build_previous_block(runs, "", D.open_findings(runs, "architect"), "architect")
        self.assertIn("PREVIOUS REVIEW", block)
        self.assertIn("DR-1", block)

    def test_latest_run_picks_the_newest_of_that_persona(self):
        runs = [D.Run({"v": 4, "persona": "qa"}, ""), D.Run({"v": 3, "persona": "ba"}, ""),
                D.Run({"v": 2, "persona": "qa"}, ""), D.Run({"v": 1, "persona": "architect"}, "")]
        self.assertEqual(D.latest_run(runs, "qa").meta["v"], 4)
        self.assertEqual(D.latest_run(runs, "ba").meta["v"], 3)
        self.assertEqual(D.latest_run(runs, "architect").meta["v"], 1)
        self.assertIsNone(D.latest_run(runs[:3], "architect"))
        self.assertEqual(D.latest_run(runs, None).meta["v"], 4)     # no persona: the newest


class CommandLine(Isolated):
    def setUp(self):
        super().setUp()
        self.origin, self.clone = fixture(self.home / "one")

    def spec(self, **kw):
        return P.build_docs_spec(args(files=["docs/spec.md"], **kw), cwd=self.clone)

    def test_the_persona_flag(self):
        self.assertEqual(self.spec().persona, "ba")
        for given, key in (("architect", "architect"), ("QA", "qa"), ("Business-Analyst", "ba"),
                           ("arch", "architect")):
            self.assertEqual(self.spec(persona=given).persona, key)

    def test_an_unknown_persona_is_a_usage_error_naming_the_choices(self):
        with self.assertRaises(P.UsageError) as cm:
            self.spec(persona="designer")
        self.assertIn("architect", str(cm.exception))

    def test_a_saved_persona_is_used_and_a_flag_beats_it(self):
        C.set_cli(persona="qa")
        self.assertEqual(self.spec().persona, "qa")
        self.assertEqual(self.spec(persona="architect").persona, "architect")

    def test_config_stores_the_canonical_persona_and_refuses_nonsense(self):
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            self.assertEqual(P.main(["config", "set", "persona", "Business Analyst"]), 0)
            self.assertEqual(C.get_cli()["persona"], "ba")
            self.assertEqual(P.main(["config", "set", "persona", "architect"]), 0)
        with mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stderr", io.StringIO()):
            self.assertEqual(P.main(["config", "set", "persona", "designer"]), 2)
        self.assertEqual(C.get_cli()["persona"], "architect")          # unchanged

    def test_requirements_are_optional_and_nothing_is_assumed(self):
        self.assertEqual(self.spec().requirements_path, "")
        C.set_cli(requirements="from-config")
        self.assertEqual(self.spec().requirements_path, "from-config")
        self.assertEqual(self.spec(requirements="docs/requirements").requirements_path,
                         "docs/requirements")

    def test_the_persona_survives_a_record_and_an_old_record_still_loads(self):
        spec = self.spec(persona="architect")
        back = CJ.spec_from_dict(CJ.spec_to_dict(spec))
        self.assertEqual((back.persona, back.requirements_path), ("architect", ""))
        old = {"kind": "docs", "project_dir": str(self.clone), "requirements_path": "docs/requirements",
               "targets": [{"role": "repo", "repo_name": "clone", "local_repo": str(self.clone),
                            "branch": "feat/x", "files": ["docs/spec.md"]}]}
        loaded = CJ.spec_from_dict(old)                # a v5.1 record: no persona key
        self.assertEqual((loaded.persona, loaded.requirements_path), ("ba", "docs/requirements"))
        old["persona"] = "from-the-future"
        self.assertEqual(CJ.spec_from_dict(old).persona, "ba")        # unreadable: the default

    def test_a_run_from_the_command_line_uses_the_persona_end_to_end(self):
        prompts = []
        saved = (O.ensure_bundled_agents, D._run_agent)
        O.ensure_bundled_agents = lambda *a, **k: ["doc-reviewer"]

        def agent(prompt, project_dir, emit, model, control, meter):
            import re
            prompts.append(prompt)
            n = int(re.search(r"Start new comments at DR-(\d+)", prompt).group(1))
            return 0, FULL.format(n=n, m=n + 1), "s"
        D._run_agent = agent
        self.addCleanup(lambda: (setattr(O, "ensure_bundled_agents", saved[0]),
                                 setattr(D, "_run_agent", saved[1])))
        import os
        old = os.getcwd()
        os.chdir(str(self.clone))
        out = io.StringIO()
        try:
            with mock.patch.object(sys, "stdout", out), mock.patch.object(sys.stdin, "isatty",
                                                                          return_value=False):
                code = P.main(["docs", "docs/spec.md", "--persona", "qa"])
                P.main(["jobs", "--all"])
        finally:
            os.chdir(old)
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("PERSONA: QA.", prompts[0])
        self.assertIn("NO REQUIREMENTS WERE PROVIDED", prompts[0])
        self.assertIn("Review v1 · QA · run", sh(self.origin, "show", "feat/x:docs/spec.md"))
        self.assertIn("Docs · QA ·", out.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
