"""The prism-aws-alerts Lambdas, run offline with boto3 and Chat stubbed.

What matters: the merge and deploy alerts name the same person, and the
deployment card makes success and failure unmistakable without emoji.
"""
import importlib.util
import json
import os
import re
import sys
import types
import unittest
from pathlib import Path

# Importing index.py must not leave __pycache__ inside the Lambda source
# folders: Terraform zips those folders, so stray bytecode would change the
# deployed package (and show up as a spurious code change in the plan).
sys.dont_write_bytecode = True

SRC = Path(__file__).resolve().parent.parent / "prism-aws-alerts" / "src"


def load(name, **env):
    """Import one Lambda's index.py fresh, with stub AWS clients and env.

    Keyword arguments override the default env vars for this load only (the
    defaults are re-applied on every call, so overrides don't leak).
    """
    stub = types.ModuleType("boto3")
    stub.resource = lambda *a, **k: types.SimpleNamespace(Table=lambda n: FakeTable())
    stub.client = lambda *a, **k: FakeClient()
    sys.modules["boto3"] = stub
    os.environ.update({
        "DYNAMODB_TABLE_NAME": "t",
        "CHAT_TARGETS_JSON": json.dumps([{"name": "c", "webhook_url": "https://x/h",
                                          "branches": ["main"]}]),
        "PIPELINE_BRANCHES_JSON": json.dumps({"p-api-main": "main"}),
        "PIPELINE_REPOSITORIES_JSON": json.dumps({"p-api-main": "repo"}),
        "POST_UNLINKED_DEPLOYMENTS": "true",
    })
    os.environ.update(env)
    spec = importlib.util.spec_from_file_location(f"alerts_{name}", SRC / name / "index.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeTable:
    items = {}
    def put_item(self, Item):
        FakeTable.items[Item["commit_sha"]] = Item
    def get_item(self, Key):
        item = FakeTable.items.get(Key["commit_sha"])
        return {"Item": item} if item else {}


class FakeClient:
    def get_pull_request(self, pullRequestId):
        return {"pullRequest": {"title": "Fix login", "authorArn": "arn:aws:iam::1:user/alice",
                                "pullRequestTargets": [{"repositoryName": "repo",
                                                        "destinationReference": "refs/heads/main",
                                                        "mergeMetadata": {"mergeCommitId": "abc123def4567"}}]}}
    def get_commit(self, repositoryName, commitId):
        return {"commit": {"author": {"name": "Alice Git Name", "email": "a@x"}}}
    def get_pipeline_execution(self, pipelineName, pipelineExecutionId):
        return {"pipelineExecution": {"artifactRevisions": [{"revisionId": "abc123def4567"}]}}


class DeploymentCard(unittest.TestCase):
    def setUp(self):
        self.d = load("deployment_notifier")

    def widgets(self, payload):
        # Keyed by label; the PR row has none (it opens with "PR #<id>").
        return {w["decoratedText"].get("topLabel", "PR"): w["decoratedText"]["text"]
                for w in payload["cardsV2"][0]["card"]["sections"][0]["widgets"]}

    def test_success_and_failure_are_visually_distinct_without_emoji(self):
        ok = self.d._build_message("Production", "Api", "succeeded", "7", "abc123def4567",
                                   None, "alice", "repo", "Fix login")
        bad = self.d._build_message("Production", "Api", "failed", "7", "abc123def4567",
                                    None, "alice", "repo", "Fix login")
        ok_status, bad_status = self.widgets(ok)["Status"], self.widgets(bad)["Status"]
        self.assertIn("SUCCEEDED", ok_status)
        self.assertIn("#137333", ok_status)
        self.assertIn("FAILED", bad_status)
        self.assertIn("#C5221F", bad_status)
        self.assertNotEqual(ok_status, bad_status)
        self.assertIn("succeeded", ok["cardsV2"][0]["card"]["header"]["title"])
        self.assertIn("failed", bad["cardsV2"][0]["card"]["header"]["title"])
        for payload in (ok, bad):
            text = json.dumps(payload, ensure_ascii=False)
            self.assertFalse([c for c in text if ord(c) >= 0x2600], "no emoji/symbols")

    def test_card_has_the_merge_alerts_shape_and_rows(self):
        p = self.d._build_message("QA", "Api", "succeeded", "7", "abc123def4567",
                                  "spaces/S/threads/T", "alice", "repo", "Fix login")
        card = p["cardsV2"][0]["card"]
        self.assertEqual(card["header"]["title"], "QA deployment succeeded")
        self.assertEqual(card["header"]["subtitle"], "repo", "no Frontend/Backend repeat")
        w = self.widgets(p)
        self.assertEqual(w["PR"], "PR #7<br>Fix login")
        self.assertEqual(w["Commit"], "abc123def456")
        self.assertEqual(w["Author"], "alice")
        self.assertEqual(p["thread"], {"name": "spaces/S/threads/T"})

    def test_pr_title_is_escaped_for_chat_markup(self):
        p = self.d._build_message("QA", "", "succeeded", "7", "abc", None, None, "repo", "a <b> & c")
        self.assertEqual(self.widgets(p)["PR"], "PR #7<br>a &lt;b&gt; &amp; c")

    def test_pr_row_without_a_title_is_just_the_number(self):
        p = self.d._build_message("QA", "", "succeeded", "7", "abc", None, None, "repo")
        self.assertEqual(self.widgets(p)["PR"], "PR #7")

    def test_unlinked_deployment_omits_the_pr_row_and_thread(self):
        p = self.d._build_message("QA", "", "failed", None, "abc123def4567", None, None, "repo")
        self.assertNotIn("PR", self.widgets(p))
        self.assertNotIn("Author", self.widgets(p))
        self.assertNotIn("thread", p)
        self.assertEqual(p["cardsV2"][0]["card"]["header"]["subtitle"], "repo")
        self.assertNotIn("Api", json.dumps(p))

    def test_deploy_alert_uses_the_pr_author_recorded_by_the_merge_alert(self):
        posted = []
        self.d._post_to_chat = lambda url, payload, thread: posted.append(payload)
        self.d.table = FakeTable()
        FakeTable.items = {"abc123def4567": {
            "repository": "repo", "pull_request_id": "7", "pr_title": "Fix login",
            "author": "alice",
            "chat_threads": [{"chat_name": "c", "thread_name": "spaces/S/threads/T"}]}}
        self.d.CHAT_TARGETS_BY_NAME = {"c": {"name": "c", "webhook_url": "https://x/h"}}
        self.d.codepipeline = FakeClient()
        self.d.codecommit = FakeClient()
        self.d.handler({"detail": {"pipeline": "p-api-main", "execution-id": "e1",
                                   "state": "SUCCEEDED"}}, None)
        self.assertEqual(self.widgets(posted[0])["Author"], "alice")
        self.assertNotIn("Alice Git Name", json.dumps(posted[0]))

    def test_falls_back_to_the_commit_author_when_no_pr_was_recorded(self):
        posted = []
        self.d._post_to_chat = lambda url, payload, thread: posted.append(payload)
        self.d.table = FakeTable()
        FakeTable.items = {}
        self.d.codepipeline = FakeClient()
        self.d.codecommit = FakeClient()
        self.d.handler({"detail": {"pipeline": "p-api-main", "execution-id": "e1",
                                   "state": "FAILED"}}, None)
        self.assertEqual(self.widgets(posted[0])["Author"], "Alice Git Name")
        self.assertIn("FAILED", self.widgets(posted[0])["Status"])


class MergeRecordsTheAuthor(unittest.TestCase):
    def test_merge_alert_stores_the_same_author_it_displays(self):
        m = load("pr_merge_notifier")
        shown = []
        m._post_to_chat = lambda url, payload: shown.append(payload) or {"thread": {"name": "T"}}
        m.table = FakeTable()
        m.codecommit = FakeClient()
        FakeTable.items = {}
        m.handler({"detail": {"isMerged": "True", "repositoryNames": ["repo"],
                              "pullRequestId": "7",
                              "destinationReference": "refs/heads/main"}}, None)
        stored = FakeTable.items["abc123def4567"]
        self.assertEqual(stored["author"], "alice")
        widgets = shown[0]["cardsV2"][0]["card"]["sections"][0]["widgets"]
        self.assertIn("alice", json.dumps(widgets))


# --- FR-ALERT: the develop group --------------------------------------------

DEVELOP_HOOK = "https://chat.example/develop"
RELEASE_HOOK = "https://chat.example/release"
DEVELOP_ENV = {
    "CHAT_TARGETS_JSON": json.dumps([
        {"name": "chat-develop", "webhook_url": DEVELOP_HOOK, "branches": ["develop"]},
        {"name": "chat-release", "webhook_url": RELEASE_HOOK,
         "branches": ["qa", "staging", "main"]},
    ]),
    "PIPELINE_BRANCHES_JSON": json.dumps({
        "p-api-dev": "develop", "p-api-qa": "qa", "p-api-stg": "staging", "p-api-prod": "main",
    }),
    "PIPELINE_REPOSITORIES_JSON": json.dumps({
        "p-api-dev": "repo", "p-api-qa": "repo", "p-api-stg": "repo", "p-api-prod": "repo",
    }),
}


class DevelopGroup(unittest.TestCase):
    """A develop-only chat target next to the release targets."""

    def setUp(self):
        FakeTable.items = {}
        self.merge_posts = []   # (webhook, payload)
        self.deploy_posts = []  # (webhook, payload, thread)

    def merge(self, branch):
        m = load("pr_merge_notifier", **DEVELOP_ENV)
        m.table, m.codecommit = FakeTable(), FakeClient()
        m._post_to_chat = lambda url, payload: (
            self.merge_posts.append((url, payload)) or {"thread": {"name": f"{url}/threads/1"}})
        m.handler({"detail": {"isMerged": "True", "repositoryNames": ["repo"],
                              "pullRequestId": "7",
                              "destinationReference": f"refs/heads/{branch}"}}, None)

    def deploy(self, pipeline, state="SUCCEEDED", **env):
        d = load("deployment_notifier", **{**DEVELOP_ENV, **env})
        d.table, d.codepipeline, d.codecommit = FakeTable(), FakeClient(), FakeClient()
        d._post_to_chat = lambda url, payload, thread: self.deploy_posts.append(
            (url, payload, thread))
        d.handler({"detail": {"pipeline": pipeline, "execution-id": "e1",
                              "state": state}}, None)

    def test_develop_merge_posts_only_to_the_develop_group(self):
        self.merge("develop")
        self.assertEqual([u for u, _ in self.merge_posts], [DEVELOP_HOOK])
        header = self.merge_posts[0][1]["cardsV2"][0]["card"]["header"]
        self.assertEqual(header["title"], "Merged PR #7 \u2192 develop")
        stored = FakeTable.items["abc123def4567"]
        self.assertEqual(stored["branch"], "develop")
        self.assertEqual(stored["chat_threads"],
                         [{"chat_name": "chat-develop", "thread_name": f"{DEVELOP_HOOK}/threads/1"}])

    def test_develop_deployment_is_threaded_onto_the_develop_merge_only(self):
        self.merge("develop")
        for state, word in (("SUCCEEDED", "succeeded"), ("FAILED", "failed")):
            self.deploy_posts.clear()
            self.deploy("p-api-dev", state)
            self.assertEqual(len(self.deploy_posts), 1)
            url, payload, thread = self.deploy_posts[0]
            self.assertEqual(url, DEVELOP_HOOK)
            self.assertEqual(thread, f"{DEVELOP_HOOK}/threads/1")
            self.assertEqual(payload["thread"], {"name": thread})
            card = payload["cardsV2"][0]["card"]
            self.assertEqual(card["header"]["title"], f"Develop deployment {word}")
            rows = json.dumps(card["sections"][0]["widgets"])
            for expected in ("PR #7", "Fix login", "abc123def456", "alice", word.upper()):
                self.assertIn(expected, rows)

    def test_release_merges_never_reach_the_develop_group(self):
        for branch in ("qa", "staging", "main"):
            self.merge_posts.clear()
            self.merge(branch)
            self.assertEqual([u for u, _ in self.merge_posts], [RELEASE_HOOK], branch)

    def test_release_deployment_never_reaches_the_develop_group(self):
        self.merge("main")
        self.deploy("p-api-prod")
        self.assertEqual([u for u, _, _ in self.deploy_posts], [RELEASE_HOOK])

    def test_unlinked_develop_deployment_goes_standalone_to_develop_only(self):
        self.deploy("p-api-dev", "FAILED")
        self.assertEqual(len(self.deploy_posts), 1)
        url, payload, thread = self.deploy_posts[0]
        self.assertEqual(url, DEVELOP_HOOK)
        self.assertIsNone(thread)
        self.assertNotIn("thread", payload)
        self.assertEqual(payload["cardsV2"][0]["card"]["header"]["title"],
                         "Develop deployment failed")

    def test_unlinked_develop_deployment_is_dropped_when_the_setting_is_off(self):
        self.deploy("p-api-dev", POST_UNLINKED_DEPLOYMENTS="false")
        self.assertEqual(self.deploy_posts, [])

    def test_dev_pipelines_are_labelled_develop(self):
        d = load("deployment_notifier")
        self.assertEqual(d.ENVIRONMENT_LABELS["dev"], "Develop")
        self.assertEqual(d.ENVIRONMENT_LABELS["develop"], "Develop")
        self.assertEqual(d.ENVIRONMENT_LABELS["main"], "Production")


class ExampleConfigShowsTheDevelopGroup(unittest.TestCase):
    ROOT = SRC.parent

    def setUp(self):
        self.example = (self.ROOT / "terraform.tfvars.example").read_text()

    def targets(self):
        block = self.example[self.example.index("chat_targets = ["):]
        block = block[:block.index("\n]\n")]
        return [dict(re.findall(r'(\w+)\s*=\s*("[^"]*"|\[[^\]]*\])', t))
                for t in block.split("{")[1:]]

    def test_has_a_develop_only_target_separate_from_the_release_targets(self):
        targets = self.targets()
        names = [t["name"] for t in targets]
        self.assertEqual(len(names), len(set(names)), "target names are unique")
        develop = [t for t in targets if "develop" in t["branches"]]
        self.assertEqual(len(develop), 1)
        self.assertEqual(develop[0]["branches"], '["develop"]')
        for t in targets:
            if t is not develop[0]:
                self.assertNotIn("develop", t["branches"])
        release = {b for t in targets for b in json.loads(t["branches"])} - {"develop"}
        self.assertEqual(release, {"qa", "staging", "main"})

    def test_webhooks_are_placeholders(self):
        for t in self.targets():
            self.assertRegex(t["webhook_url"],
                             r'^"https://chat\.googleapis\.com/v1/spaces/([A-Z])\1+/messages'
                             r'\?key=XXXX&token=YYYY"$')

    def test_develop_pipelines_are_mapped_in_example_and_defaults(self):
        variables = (self.ROOT / "variables.tf").read_text()
        for text in (self.example, variables):
            self.assertRegex(text, r'"your-backend-pipeline-dev"\s*=\s*"develop"')
            self.assertRegex(text, r'"your-frontend-pipeline-dev"\s*=\s*"develop"')
            self.assertRegex(text, r'"your-backend-pipeline-dev"\s*=\s*"your-backend-repo"')
            self.assertRegex(text, r'"your-frontend-pipeline-dev"\s*=\s*"your-frontend-repo"')
        self.assertIn("pipeline_repositories = {", self.example)
        self.assertIn("Each chat_targets entry must have a unique name.", variables)

    def test_readme_documents_the_develop_group(self):
        readme = (self.ROOT / "README.md").read_text()
        self.assertIn("## The develop group", readme)
        self.assertIn('branches = ["develop"]', readme)
        self.assertIn("your-backend-pipeline-dev", readme)
        self.assertIn("GitLab", readme)


if __name__ == "__main__":
    unittest.main()
