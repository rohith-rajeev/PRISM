"""GitLab host detection, token handling and the REST client.

Fully offline: the client is handed a fake opener that records each request
and answers with canned JSON (or raises HTTPError with a JSON body), and
config is pointed at a temp directory so the real ~/.prism is never touched.
"""
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config as C  # noqa: E402
import gitlab as G  # noqa: E402

TOKEN = "glpat-SuperSecretToken12345"


class IsolatedConfig(unittest.TestCase):
    """Temp config file and a clean token/host environment for every test."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        orig_dir, orig_path = C.CONFIG_DIR, C.CONFIG_PATH
        C.CONFIG_DIR = Path(self._tmp.name) / ".prism"
        C.CONFIG_PATH = C.CONFIG_DIR / "config.json"
        self.addCleanup(setattr, C, "CONFIG_DIR", orig_dir)
        self.addCleanup(setattr, C, "CONFIG_PATH", orig_path)
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        for name in ("GITLAB_TOKEN", "PRISM_GITLAB_TOKEN", "PRISM_GITLAB_HOSTS"):
            os.environ.pop(name, None)


class FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self._raw = b"" if payload is None else json.dumps(payload).encode()

    def read(self):
        return self._raw

    def close(self):
        pass


class FakeOpener:
    """Records requests; answers from a list of (status, payload) or exceptions.

    A routes dict maps a URL path substring to a list of answers, consumed in
    order (the last one repeats), so a test can script a status changing.
    """

    def __init__(self, routes):
        self.routes = {k: list(v) if isinstance(v, list) else [v] for k, v in routes.items()}
        self.calls = []

    def __call__(self, req, timeout=None, context=None):
        body = json.loads(req.data.decode()) if req.data else None
        self.calls.append({"method": req.get_method(), "url": req.full_url,
                           "headers": {k.lower(): v for k, v in req.header_items()},
                           "body": body, "timeout": timeout})
        path = req.full_url.split("?", 1)[0]
        # Longest matching key wins, so ".../merge" beats ".../merge_requests/3".
        for key in sorted(self.routes, key=len, reverse=True):
            if path.endswith(key):
                answers = self.routes[key]
                answer = answers.pop(0) if len(answers) > 1 else answers[0]
                if isinstance(answer, BaseException):
                    raise answer
                if isinstance(answer, tuple) and answer and answer[0] == "http_error":
                    _, code, payload = answer
                    raise urllib.error.HTTPError(
                        req.full_url, code, "err", {},
                        io.BytesIO(json.dumps(payload).encode()))
                status, payload = answer
                return FakeResponse(status, payload)
        raise AssertionError(f"unexpected request {req.full_url}")


def client(routes, token=TOKEN):
    opener = FakeOpener(routes)
    c = G.Client("https://gitlab.example.com/", token=token, opener=opener,
                 sleep=lambda s: opener.calls.append({"sleep": s}))
    return c, opener


PROJ = "/projects/group%2Fsub%2Fproj"
MR = PROJ + "/merge_requests/3"


def mr_payload(**over):
    mr = {
        "iid": 3, "state": "opened", "title": "Add thing", "description": None,
        "source_branch": "feature", "target_branch": "main", "sha": "abc123",
        "diff_refs": {"base_sha": "base000", "start_sha": "start111", "head_sha": "abc123"},
        "author": {"username": "jdoe"}, "detailed_merge_status": "mergeable",
        "merge_status": "can_be_merged", "has_conflicts": False,
        "diverged_commits_count": 0, "source_project_id": 7, "target_project_id": 7,
        "web_url": "https://gitlab.example.com/group/sub/proj/-/merge_requests/3",
        "references": {"full": "group/sub/proj!3"}, "draft": False,
    }
    mr.update(over)
    return mr


def proj_payload(**over):
    p = {"id": 7, "path_with_namespace": "group/sub/proj", "merge_method": "ff",
         "squash_option": "default_off"}
    p.update(over)
    return p


# ---- classify_remote -----------------------------------------------------------

class ClassifyRemote(IsolatedConfig):
    def check(self, url, host, base="", project="", region="", **kw):
        self.assertEqual(G.classify_remote(url, **kw),
                         {"host": host, "base_url": base, "project": project,
                          "region": region}, url)

    def test_empty_is_legacy_codecommit(self):
        self.check("", G.HOST_CODECOMMIT)
        self.check(None, G.HOST_CODECOMMIT)

    def test_codecommit_grc_with_region_and_profile(self):
        self.check("codecommit::us-east-1://my-repo", G.HOST_CODECOMMIT,
                   project="my-repo", region="us-east-1")
        self.check("codecommit::eu-west-2://prof@my-repo", G.HOST_CODECOMMIT,
                   project="my-repo", region="eu-west-2")

    def test_codecommit_grc_without_region(self):
        self.check("codecommit://my-repo", G.HOST_CODECOMMIT, project="my-repo")
        self.check("codecommit://prof@my-repo", G.HOST_CODECOMMIT, project="my-repo")

    def test_codecommit_https_and_ssh_endpoints(self):
        self.check("https://git-codecommit.us-west-2.amazonaws.com/v1/repos/repo1",
                   G.HOST_CODECOMMIT, project="repo1", region="us-west-2")
        self.check("ssh://APKAEXAMPLE@git-codecommit.eu-central-1.amazonaws.com/v1/repos/r2",
                   G.HOST_CODECOMMIT, project="r2", region="eu-central-1")
        self.check("https://git-codecommit.cn-north-1.amazonaws.com.cn/v1/repos/r3",
                   G.HOST_CODECOMMIT, project="r3", region="cn-north-1")

    def test_gitlab_com_https(self):
        self.check("https://gitlab.com/group/project.git", G.HOST_GITLAB,
                   "https://gitlab.com", "group/project")
        self.check("https://gitlab.com/group/project", G.HOST_GITLAB,
                   "https://gitlab.com", "group/project")

    def test_nested_groups(self):
        self.check("https://gitlab.com/a/b/c/proj.git/", G.HOST_GITLAB,
                   "https://gitlab.com", "a/b/c/proj")

    def test_credentials_are_stripped(self):
        r = G.classify_remote(f"https://oauth2:{TOKEN}@gitlab.example.com/g/p.git")
        self.assertEqual(r["base_url"], "https://gitlab.example.com")
        self.assertEqual(r["project"], "g/p")
        self.assertNotIn(TOKEN, json.dumps(r))
        self.assertNotIn("oauth2", json.dumps(r))

    def test_http_keeps_scheme_and_port(self):
        self.check("http://user:pw@gitlab.internal:8080/team/svc.git", G.HOST_GITLAB,
                   "http://gitlab.internal:8080", "team/svc")

    def test_scp_like_ssh(self):
        self.check("git@gitlab.com:group/project.git", G.HOST_GITLAB,
                   "https://gitlab.com", "group/project")

    def test_ssh_url_drops_port(self):
        self.check("ssh://git@gitlab.example.com:2222/g/sub/p.git", G.HOST_GITLAB,
                   "https://gitlab.example.com", "g/sub/p")

    def test_hostname_label_rule(self):
        self.check("https://gitlab-ce.corp.io/g/p", G.HOST_GITLAB,
                   "https://gitlab-ce.corp.io", "g/p")
        self.check("https://GitLab.Example.com/g/p", G.HOST_GITLAB,
                   "https://gitlab.example.com", "g/p")
        # "gitlab" must start a label, not just appear in one.
        self.check("https://mygitlab.example.com/g/p", G.HOST_UNSUPPORTED)

    def test_single_segment_is_unsupported(self):
        self.check("https://gitlab.com/project.git", G.HOST_UNSUPPORTED)
        self.check("git@gitlab.com:project.git", G.HOST_UNSUPPORTED)

    def test_extra_hosts_argument(self):
        self.check("git@code.corp.example:g/p.git", G.HOST_UNSUPPORTED)
        self.check("git@code.corp.example:g/p.git", G.HOST_GITLAB,
                   "https://code.corp.example", "g/p", extra_hosts=["Code.Corp.Example"])

    def test_env_var_hosts(self):
        os.environ["PRISM_GITLAB_HOSTS"] = "other.example, code.corp.example"
        self.check("https://code.corp.example/g/p.git", G.HOST_GITLAB,
                   "https://code.corp.example", "g/p")

    def test_config_hosts(self):
        C.save({"gitlab_hosts": ["code.corp.example"]})
        self.check("ssh://git@code.corp.example/g/p.git", G.HOST_GITLAB,
                   "https://code.corp.example", "g/p")

    def test_unsupported(self):
        for url in ("https://github.com/o/r.git", "git@github.com:o/r.git",
                    "https://bitbucket.org/o/r.git", "/home/me/src/repo",
                    "../repo", "file:///srv/git/repo.git", "C:/src/repo",
                    "https://example.com/a/b.git"):
            self.check(url, G.HOST_UNSUPPORTED)


# ---- tokens --------------------------------------------------------------------

class Tokens(IsolatedConfig):
    def test_none_anywhere(self):
        self.assertEqual(G.get_token(), "")

    def test_config_fallback(self):
        C.save({"gitlab_token": "  from-config-token  "})
        self.assertEqual(G.get_token(), "from-config-token")

    def test_env_order(self):
        C.save({"gitlab_token": "cfg"})
        os.environ["PRISM_GITLAB_TOKEN"] = " prism "
        self.assertEqual(G.get_token(), "prism")
        os.environ["GITLAB_TOKEN"] = "gl"
        self.assertEqual(G.get_token("https://gitlab.com"), "gl")

    def test_non_string_config_is_ignored(self):
        C.save({"gitlab_token": 12345})
        self.assertEqual(G.get_token(), "")

    def test_set_token_saves_and_removes(self):
        C.save({"other": "keep"})
        G.set_token("  tok-123  ")
        self.assertEqual(C.load()["gitlab_token"], "tok-123")
        G.set_token("")
        self.assertNotIn("gitlab_token", C.load())
        self.assertEqual(C.load()["other"], "keep")

    def test_mask(self):
        self.assertEqual(G.mask(""), "")
        self.assertEqual(G.mask(None), "")
        self.assertEqual(G.mask("short"), "****")
        self.assertEqual(G.mask(TOKEN), "glpat-…2345")

    def test_redact_given_token_and_pattern(self):
        text = f"token {TOKEN} and plain abcdefgh and glpat-0123456789abc"
        out = G.redact(text, token="abcdefgh")
        self.assertNotIn("abcdefgh", out)
        self.assertNotIn(TOKEN, out)
        self.assertNotIn("glpat-0123456789abc", out)
        self.assertEqual(out.count("<redacted-token>"), 3)

    def test_redact_uses_configured_token(self):
        os.environ["GITLAB_TOKEN"] = "configured-secret"
        self.assertEqual(G.redact("x configured-secret y"), "x <redacted-token> y")

    def test_redact_new_style_token_with_routing_suffix(self):
        self.assertEqual(G.redact("glpat-abcdefghij0123.01.xyz9", token=""),
                         "<redacted-token>")


# ---- client --------------------------------------------------------------------

class ClientRequests(IsolatedConfig):
    def test_headers_url_and_encoding(self):
        c, op = client({PROJ: (200, proj_payload())})
        self.assertEqual(c.project("group/sub/proj")["id"], 7)
        call = op.calls[0]
        self.assertEqual(call["method"], "GET")
        self.assertEqual(call["url"], "https://gitlab.example.com/api/v4/projects/group%2Fsub%2Fproj")
        self.assertEqual(call["headers"]["private-token"], TOKEN)
        self.assertEqual(call["headers"]["accept"], "application/json")
        self.assertNotIn("content-type", call["headers"])

    def test_no_token_no_header(self):
        c, op = client({"/user": (200, {"username": "me"})}, token="")
        self.assertEqual(c.current_user()["username"], "me")
        self.assertNotIn("private-token", op.calls[0]["headers"])

    def test_token_defaults_from_get_token(self):
        os.environ["GITLAB_TOKEN"] = "env-token-value"
        self.assertEqual(G.Client("https://gitlab.com").token, "env-token-value")

    def test_repr_masks_token(self):
        c, _ = client({})
        self.assertNotIn(TOKEN, repr(c))

    def test_merge_request_params(self):
        c, op = client({MR: (200, mr_payload())})
        c.merge_request("group/sub/proj", 3)
        self.assertIn("include_diverged_commits_count=true", op.calls[0]["url"])
        self.assertNotIn("with_merge_status_recheck", op.calls[0]["url"])
        c.merge_request("group/sub/proj", 3, recheck=True)
        self.assertIn("with_merge_status_recheck=true", op.calls[1]["url"])

    def test_update_description(self):
        c, op = client({MR: (200, mr_payload(description="new"))})
        c.update_description("group/sub/proj", 3, "new")
        call = op.calls[0]
        self.assertEqual(call["method"], "PUT")
        self.assertEqual(call["body"], {"description": "new"})
        self.assertEqual(call["headers"]["content-type"], "application/json")

    def test_merge_sends_sha_and_no_squash(self):
        c, op = client({MR + "/merge": (200, mr_payload(state="merged"))})
        c.merge("group/sub/proj", 3, "abc123")
        call = op.calls[0]
        self.assertEqual(call["method"], "PUT")
        self.assertTrue(call["url"].endswith(MR + "/merge"))
        self.assertEqual(call["body"], {"sha": "abc123", "squash": False,
                                        "should_remove_source_branch": False})

    def test_merge_rejects_empty_sha(self):
        c, op = client({})
        for sha in ("", None, "  "):
            with self.assertRaises(ValueError):
                c.merge("group/sub/proj", 3, sha)
        self.assertEqual(op.calls, [])


class ClientErrors(IsolatedConfig):
    def fail(self, code, payload):
        c, _ = client({MR: ("http_error", code, payload)})
        with self.assertRaises(G.GitLabError) as ctx:
            c.merge_request("group/sub/proj", 3)
        err = ctx.exception
        self.assertEqual(err.status, code)
        for text in (str(err), repr(err), str(err.args)):
            self.assertNotIn(TOKEN, text)
        self.assertIsNone(err.__cause__)
        return str(err)

    def test_401(self):
        msg = self.fail(401, {"message": "401 Unauthorized"})
        self.assertIn("401 Unauthorized", msg)
        self.assertIn(f"GitLab GET {MR} failed (401)", msg)
        self.assertNotIn("?", msg)  # query string left out
        for word in ("missing", "wrong", "expired", "api scope"):
            self.assertIn(word, msg)

    def test_403(self):
        msg = self.fail(403, {"message": "403 Forbidden"})
        self.assertIn("403 Forbidden", msg)
        self.assertIn("lacks permission", msg)

    def test_404(self):
        msg = self.fail(404, {"message": "404 Project Not Found"})
        self.assertIn("404 Project Not Found", msg)

    def test_error_field_and_structured_message(self):
        self.assertIn("insufficient_scope", self.fail(403, {"error": "insufficient_scope"}))
        msg = self.fail(400, {"message": {"description": ["is too long"]}})
        self.assertIn("description: is too long", msg)
        msg = self.fail(422, {"message": ["first", "second"]})
        self.assertIn("first; second", msg)

    def test_host_text_echoing_the_token_is_redacted(self):
        msg = self.fail(400, {"message": f"bad token {TOKEN}"})
        self.assertIn("<redacted-token>", msg)

    def test_unreachable(self):
        c, _ = client({MR: urllib.error.URLError("Name or service not known")})
        with self.assertRaises(G.GitLabError) as ctx:
            c.merge_request("group/sub/proj", 3)
        self.assertIsNone(ctx.exception.status)
        self.assertIn("Name or service not known", str(ctx.exception))

    def test_timeout(self):
        c, _ = client({MR: TimeoutError("timed out")})
        with self.assertRaises(G.GitLabError) as ctx:
            c.merge_request("group/sub/proj", 3)
        self.assertIsNone(ctx.exception.status)


class GetPr(IsolatedConfig):
    def test_normalised_keys(self):
        c, _ = client({MR: (200, mr_payload(diverged_commits_count=2))})
        pr = c.get_pr("group/sub/proj", 3)
        expected = {
            "repositoryName": "group/sub/proj", "sourceReference": "feature",
            "destinationReference": "main", "sourceCommit": "abc123",
            "destinationCommit": "start111", "mergeBase": "base000",
            "isMerged": False, "title": "Add thing", "description": "",
            "status": "OPEN", "authorArn": "jdoe",
            "detailedMergeStatus": "mergeable", "hasConflicts": False,
            "divergedCommitsCount": 2, "sourceProjectId": 7, "targetProjectId": 7,
            "webUrl": "https://gitlab.example.com/group/sub/proj/-/merge_requests/3",
            "draft": False,
        }
        for key, value in expected.items():
            self.assertEqual(pr[key], value, key)
        self.assertEqual(pr["raw"]["iid"], 3)

    def test_merged_and_fallbacks(self):
        payload = mr_payload(state="merged", references=None, draft=None,
                             work_in_progress=True, detailed_merge_status=None,
                             diverged_commits_count=None)
        c, _ = client({MR: (200, payload)})
        pr = c.get_pr("group/sub/proj", 3)
        self.assertEqual(pr["status"], "MERGED")
        self.assertTrue(pr["isMerged"])
        self.assertEqual(pr["repositoryName"], "group/sub/proj")  # from web_url
        self.assertTrue(pr["draft"])
        self.assertEqual(pr["detailedMergeStatus"], "can_be_merged")
        self.assertIsNone(pr["divergedCommitsCount"])

    def test_closed_and_locked(self):
        for state, want in (("closed", "CLOSED"), ("locked", "LOCKED")):
            c, _ = client({MR: (200, mr_payload(state=state))})
            self.assertEqual(c.get_pr("group/sub/proj", 3)["status"], want)


class FfMergeable(IsolatedConfig):
    def run_ff(self, project=None, mrs=None):
        mrs = mrs if isinstance(mrs, list) else [mrs or mr_payload()]
        c, op = client({PROJ: (200, project or proj_payload()),
                        MR: [(200, m) for m in mrs]})
        return c.ff_mergeable("group/sub/proj", 3), op

    def test_clean_ff(self):
        (ok, why), op = self.run_ff()
        self.assertEqual((ok, why), (True, "fast-forward possible"))
        self.assertIn("with_merge_status_recheck=true", op.calls[1]["url"])

    def test_merge_commit_method(self):
        (ok, why), _ = self.run_ff(project=proj_payload(merge_method="merge"))
        self.assertFalse(ok)
        self.assertIn("'merge' (merge commit); PRISM only fast-forwards", why)

    def test_rebase_merge_method(self):
        (ok, why), _ = self.run_ff(project=proj_payload(merge_method="rebase_merge"))
        self.assertFalse(ok)
        self.assertIn("rebase_merge", why)

    def test_squash_always(self):
        (ok, why), _ = self.run_ff(project=proj_payload(squash_option="always"))
        self.assertFalse(ok)
        self.assertIn("always", why)

    def test_diverged(self):
        (ok, why), _ = self.run_ff(mrs=mr_payload(diverged_commits_count=3,
                                                  detailed_merge_status="need_rebase"))
        self.assertEqual((ok, why), (False, "the source branch is 3 commit(s) behind the target"))

    def test_conflicts(self):
        (ok, why), _ = self.run_ff(mrs=mr_payload(has_conflicts=True,
                                                  detailed_merge_status="conflict"))
        self.assertFalse(ok)
        self.assertIn("conflicts", why)

    def test_closed(self):
        (ok, why), op = self.run_ff(mrs=mr_payload(state="closed"))
        self.assertFalse(ok)
        self.assertIn("closed", why)

    def test_not_approved(self):
        (ok, why), _ = self.run_ff(mrs=mr_payload(detailed_merge_status="not_approved"))
        self.assertEqual((ok, why), (False, "GitLab says: not_approved"))

    def test_missing_diverged_count_fails_closed(self):
        (ok, _), _ = self.run_ff(mrs=mr_payload(diverged_commits_count=None))
        self.assertFalse(ok)

    def test_checking_becomes_mergeable_after_polling(self):
        mrs = [mr_payload(detailed_merge_status="checking"),
               mr_payload(detailed_merge_status="checking"),
               mr_payload(detailed_merge_status="mergeable")]
        (ok, why), op = self.run_ff(mrs=mrs)
        self.assertEqual((ok, why), (True, "fast-forward possible"))
        self.assertEqual(sum(1 for c in op.calls if "sleep" in c), 2)
        # Only the first fetch asks for a recheck; polls just read the result.
        mr_calls = [c for c in op.calls if "url" in c and "/merge_requests/" in c["url"]]
        self.assertEqual(len(mr_calls), 3)
        self.assertTrue(all("with_merge_status_recheck" not in c["url"] for c in mr_calls[1:]))

    def test_never_finishes_checking(self):
        (ok, why), op = self.run_ff(mrs=mr_payload(detailed_merge_status="checking"))
        self.assertFalse(ok)
        self.assertIn("still checking", why)
        self.assertEqual(sum(1 for c in op.calls if "sleep" in c), 5)

    def test_merge_method_allows_ff(self):
        c, _ = client({PROJ: (200, proj_payload())})
        self.assertTrue(c.merge_method_allows_ff("group/sub/proj")[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
