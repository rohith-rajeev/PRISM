"""The update path, which replaces the application's own executable.

Everything here runs offline: the GitHub payloads are fixtures and the
archives are built in a temp directory, so the tests exercise the real parsing,
verification and swap code without a network or a published release.
"""

import io
import os
import re
import stat
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import updater as u  # noqa: E402


def payload(tag="v2.1", assets=("PRISM-linux-x86_64.tar.gz",
                                "PRISM-macos-arm64.zip",
                                "PRISM-windows-x86_64.zip",
                                "SHA256SUMS")):
    return {
        "tag_name": tag,
        "body": "Release notes here.",
        "html_url": "https://example.invalid/releases/" + tag,
        "assets": [{"name": n, "size": 10,
                    "browser_download_url": "https://example.invalid/" + n}
                   for n in assets],
    }


class Versions(unittest.TestCase):
    def test_numeric_comparison_not_lexical(self):
        """The bug every project hits on its tenth release."""
        self.assertTrue(u.is_newer("2.10", "2.9"))
        self.assertFalse(u.is_newer("2.9", "2.10"))

    def test_v_prefix_and_equal_versions(self):
        self.assertEqual(u.parse_version("v2.0"), u.parse_version("2.0"))
        self.assertFalse(u.is_newer("2.0", "2.0"))
        self.assertTrue(u.is_newer("2.0.1", "2.0"))

    def test_junk_degrades_instead_of_raising(self):
        self.assertEqual(u.parse_version(""), (0,))
        self.assertEqual(u.parse_version("not-a-version"), (0,))
        self.assertEqual(u.parse_version("2.1-rc1"), (2, 1))


class ReleaseParsing(unittest.TestCase):
    def test_older_or_equal_release_is_no_update(self):
        self.assertIsNone(u.release_from_payload(payload("v1.0"), current="2.0"))
        self.assertIsNone(u.release_from_payload(payload("v2.0"), current="2.0"))

    def test_picks_the_asset_for_this_platform(self):
        for key, expected in (("linux", "PRISM-linux-x86_64.tar.gz"),
                              ("macos", "PRISM-macos-arm64.zip"),
                              ("windows", "PRISM-windows-x86_64.zip")):
            rel = u.release_from_payload(payload(), current="2.0", key=key)
            with self.subTest(platform=key):
                self.assertEqual(rel.asset_name, expected)
                self.assertTrue(rel.sums_url.endswith("SHA256SUMS"))

    def test_checksums_file_is_never_offered_as_the_download(self):
        rel = u.release_from_payload(payload(assets=("SHA256SUMS",)),
                                     current="2.0", key="linux")
        self.assertFalse(rel.has_asset_for_this_platform)

    def test_a_release_with_no_build_for_this_platform(self):
        rel = u.release_from_payload(payload(assets=("PRISM-macos-arm64.zip",)),
                                     current="2.0", key="windows")
        self.assertFalse(rel.has_asset_for_this_platform)
        with self.assertRaises(u.UpdateError):
            u.download(rel, "/tmp")


class Checksums(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.f = Path(self.tmp.name, "PRISM-linux-x86_64.tar.gz")
        self.f.write_bytes(b"payload")
        self.digest = u.sha256(self.f)
        self.rel = u.release_from_payload(payload(), current="2.0", key="linux")

    def test_matching_digest_verifies(self):
        sums = f"{self.digest}  PRISM-linux-x86_64.tar.gz\n"
        self.assertTrue(u.verify(self.f, self.rel, sums_text=sums))

    def test_a_mismatch_refuses_rather_than_warning(self):
        sums = f"{'0' * 64}  PRISM-linux-x86_64.tar.gz\n"
        with self.assertRaises(u.UpdateError):
            u.verify(self.f, self.rel, sums_text=sums)

    def test_no_checksum_published_is_unverified_not_fatal(self):
        self.assertFalse(u.verify(self.f, self.rel, sums_text="")) 

    def test_parses_binary_marker_and_ignores_noise(self):
        sums = f"rubbish\n{self.digest} *PRISM-linux-x86_64.tar.gz\n"
        self.assertEqual(u.parse_sums(sums),
                         {"PRISM-linux-x86_64.tar.gz": self.digest})


needs_exec_bit = unittest.skipIf(
    os.name == "nt",
    "Windows has no POSIX executable bit - st_mode only carries read-only")


class Archives(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name)

    @needs_exec_bit
    def test_tar_keeps_the_executable_bit(self):
        exe = self.d / "PRISM"
        exe.write_bytes(b"#!/bin/sh\necho hi\n")
        os.chmod(exe, 0o755)
        archive = self.d / "PRISM-linux-x86_64.tar.gz"
        with tarfile.open(archive, "w:gz") as tf:
            tf.add(exe, arcname="PRISM")
        exe.unlink()
        out = u.extract(archive, self.d / "unpacked")
        self.assertTrue(os.stat(out / "PRISM").st_mode & stat.S_IXUSR,
                        "an unpacked build that cannot be executed is useless")

    @needs_exec_bit
    def test_zip_restores_the_executable_bit(self):
        """Calls the zip path directly: on macOS `extract` delegates to ditto,
        so going through it would test a different unpacker on each runner."""
        archive = self.d / "PRISM-windows-x86_64.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            info = zipfile.ZipInfo("PRISM")
            info.external_attr = 0o755 << 16
            zf.writestr(info, "#!/bin/sh\n")
        dest = self.d / "unpacked"
        dest.mkdir()
        u._extract_zip(archive, dest)
        self.assertTrue(os.stat(dest / "PRISM").st_mode & stat.S_IXUSR)

    def test_an_archive_escaping_its_folder_is_refused(self):
        """A downloaded archive is untrusted input like any other."""
        archive = self.d / "evil.tar.gz"
        with tarfile.open(archive, "w:gz") as tf:
            data = b"pwned"
            info = tarfile.TarInfo("../escaped")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        with self.assertRaises(u.UpdateError):
            u.extract(archive, self.d / "unpacked")
        self.assertFalse((self.d / "escaped").exists())

    def test_payload_is_found_even_when_nested(self):
        nest = self.d / "unpacked" / "inner"
        nest.mkdir(parents=True)
        (nest / "PRISM").write_text("x", encoding="utf-8")
        found = u.payload_in(self.d / "unpacked", Path("/anywhere/PRISM"))
        self.assertEqual(found.name, "PRISM")

    def test_a_download_missing_the_app_is_not_installed(self):
        (self.d / "unpacked").mkdir()
        with self.assertRaises(u.UpdateError):
            u.payload_in(self.d / "unpacked", Path("/anywhere/PRISM"))


class Swap(unittest.TestCase):
    """The irreversible part: replacing the copy the user is running."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name)
        self.installed = self.d / "PRISM"
        self.installed.write_text("old build", encoding="utf-8")
        os.chmod(self.installed, 0o755)
        self.new = self.d / "staged" / "PRISM"
        self.new.parent.mkdir()
        self.new.write_text("new build", encoding="utf-8")

    def test_swaps_and_keeps_the_previous_copy(self):
        u.install(self.new, self.installed)
        self.assertEqual(self.installed.read_text(encoding="utf-8"), "new build")
        self.assertEqual(u.backup_path(self.installed).read_text(encoding="utf-8"),
                         "old build")

    @needs_exec_bit
    def test_the_installed_copy_is_executable(self):
        u.install(self.new, self.installed)
        self.assertTrue(os.stat(self.installed).st_mode & stat.S_IXUSR)

    def test_cleanup_removes_the_previous_copy(self):
        u.install(self.new, self.installed)
        self.assertTrue(u.cleanup_previous(self.installed))
        self.assertFalse(u.backup_path(self.installed).exists())

    def test_a_failed_swap_puts_the_working_copy_back(self):
        missing = self.d / "staged" / "not-there"
        with self.assertRaises(u.UpdateError):
            u.install(missing, self.installed)
        self.assertTrue(self.installed.exists(), "rollback lost the user's app")
        self.assertEqual(self.installed.read_text(encoding="utf-8"), "old build")

    def test_a_second_update_replaces_the_old_backup(self):
        u.install(self.new, self.installed)
        newer = self.d / "staged2" / "PRISM"
        newer.parent.mkdir()
        newer.write_text("newer build", encoding="utf-8")
        u.install(newer, self.installed)
        self.assertEqual(self.installed.read_text(encoding="utf-8"), "newer build")
        self.assertEqual(u.backup_path(self.installed).read_text(encoding="utf-8"),
                         "new build")


class SourceCheckout(unittest.TestCase):
    def test_refuses_to_self_update_a_source_checkout(self):
        ok, reason = u.can_self_update()
        self.assertFalse(ok)
        self.assertIn("git pull", reason)


class ReleasePipelineContract(unittest.TestCase):
    """The workflow names the assets; the updater finds them by name.

    These two live in different files and nothing at runtime connects them, so
    renaming an asset in the workflow would quietly leave every installed copy
    unable to find its update. This is the seam that would fail silently.
    """

    WORKFLOW = ROOT / ".github" / "workflows" / "build-desktop.yml"

    def setUp(self):
        self.text = self.WORKFLOW.read_text(encoding="utf-8")
        # Just the step that assembles the release, so the CI artifact names
        # in the build job are not mistaken for published assets.
        step = self.text.split("Build the release assets", 1)[-1]
        step = step.split("action-gh-release", 1)[0]
        self.names = sorted(set(
            re.findall(r"(PRISM-[\w.\-]*?(?:\.tar\.gz|\.zip))", step)))

    def test_the_workflow_publishes_one_asset_per_platform(self):
        self.assertEqual(len(self.names), 3, f"found {self.names}")

    def test_every_published_asset_is_matched_by_the_updater(self):
        assets = [{"name": n, "size": 1, "browser_download_url": "https://x/" + n}
                  for n in self.names + ["SHA256SUMS"]]
        for key in ("linux", "macos", "windows"):
            rel = u.release_from_payload({"tag_name": "v99.0", "assets": assets},
                                         current="2.0", key=key)
            with self.subTest(platform=key):
                self.assertIn(rel.asset_name, self.names)
                self.assertIn(key, rel.asset_name)
                self.assertTrue(rel.sums_url, "no checksums to verify against")

    def test_the_release_publishes_checksums(self):
        self.assertIn("SHA256SUMS", self.text)
        self.assertIn("sha256sum", self.text)

    def test_the_tag_is_checked_against_the_version_module(self):
        """A build labelled differently from its tag breaks update checks."""
        self.assertIn("version.py", self.text)


class VersionModule(unittest.TestCase):
    def test_tag_matches_the_version(self):
        import version
        self.assertEqual(version.tag(), "v" + version.__version__)

    def test_the_version_is_parseable(self):
        import version
        self.assertGreater(u.parse_version(version.__version__), (0,))


class NothingPublishedYet(unittest.TestCase):
    """No releases at all must read as "up to date", not as a failure.

    Before a project's first release this is the normal answer to "is there an
    update", and surfacing it as an error told every user something was broken
    when nothing was.
    """

    def _check_against(self, raising):
        real = u.urllib.request.urlopen
        u.urllib.request.urlopen = raising
        self.addCleanup(setattr, u.urllib.request, "urlopen", real)
        return u.check(timeout=1)

    def test_a_missing_release_list_is_not_an_error(self):
        def raise_404(*a, **k):
            raise u.urllib.error.HTTPError("u", 404, "Not Found", {}, None)
        self.assertIsNone(self._check_against(raise_404))

    def test_a_real_failure_still_raises(self):
        def raise_500(*a, **k):
            raise u.urllib.error.HTTPError("u", 500, "Server Error", {}, None)
        with self.assertRaises(u.UpdateError):
            self._check_against(raise_500)

    def test_being_offline_still_raises(self):
        def refuse(*a, **k):
            raise u.urllib.error.URLError("Connection refused")
        with self.assertRaises(u.UpdateError):
            self._check_against(refuse)

    def test_no_user_facing_message_names_the_hosting_service(self):
        """The dialog shows these strings as-is; how it checks is not the
        user's concern, and naming a service invites 'is that my problem?'."""
        import inspect
        for fn in (u._open, u.check, u.download, u.verify, u.apply_update):
            src = inspect.getsource(fn)
            for line in src.splitlines():
                if "raise " in line or "UpdateError(" in line or '"' in line:
                    self.assertNotIn("GitHub", line, f"in {fn.__name__}: {line.strip()}")


class VersionSequence(unittest.TestCase):
    """One tenth per release, carrying into the whole number at .9."""

    def setUp(self):
        import version
        self.v = version

    def test_the_documented_sequence(self):
        seq, cur = [], "2.0"
        for _ in range(12):
            cur = self.v.next_version(cur)
            seq.append(cur)
        self.assertEqual(seq, ["2.1", "2.2", "2.3", "2.4", "2.5", "2.6", "2.7",
                               "2.8", "2.9", "3.0", "3.1", "3.2"])

    def test_the_carry_happens_at_nine_on_every_whole_number(self):
        for before, after in (("2.9", "3.0"), ("3.9", "4.0"), ("9.9", "10.0"),
                              ("0.9", "1.0")):
            with self.subTest(before=before):
                self.assertEqual(self.v.next_version(before), after)

    def test_a_tenth_minor_is_never_produced(self):
        """The scheme is a counter with one decimal place, so x.10 would both
        sort wrong as text and break the every-release-is-0.1 promise."""
        cur = "1.0"
        for _ in range(40):
            cur = self.v.next_version(cur)
            self.assertLessEqual(int(cur.split(".")[1]), 9, cur)

    def test_the_highest_candidate_wins(self):
        self.assertEqual(self.v.next_version("v2.3", "2.7", "v1.9"), "2.8")

    def test_junk_and_blanks_are_ignored_rather_than_fatal(self):
        self.assertEqual(self.v.next_version("", "2.4", "not-a-tag", None), "2.5")

    def test_tag_prefix_is_accepted_either_way(self):
        self.assertEqual(self.v.next_version("v2.4"), self.v.next_version("2.4"))

    def test_each_new_version_reads_as_newer_to_the_updater(self):
        """The two modules parse versions independently; if they ever
        disagreed, a release would ship that no installed copy would take."""
        cur = "1.8"
        for _ in range(15):
            nxt = self.v.next_version(cur)
            self.assertTrue(u.is_newer(nxt, cur), f"{nxt} not newer than {cur}")
            cur = nxt


class AutoTagWorkflow(unittest.TestCase):
    """The tagging is automation, so its wiring is worth pinning down."""

    def setUp(self):
        self.wf = (ROOT / ".github" / "workflows" / "build-desktop.yml").read_text(
            encoding="utf-8")

    def test_it_only_tags_from_main(self):
        self.assertIn("github.ref == 'refs/heads/main'", self.wf)

    def test_the_build_runs_from_the_bumped_commit(self):
        """Building the merge commit instead would ship a binary whose
        version.py still held the previous number."""
        self.assertIn("ref: ${{ needs.version.outputs.sha || github.sha }}", self.wf)

    def test_tagging_and_releasing_happen_in_one_run(self):
        """A push made with GITHUB_TOKEN starts no new workflow, so a tag that
        was expected to trigger the release would publish nothing."""
        self.assertIn("tag_name:", self.wf)
        self.assertIn("needs: [build, version]", self.wf)


if __name__ == "__main__":
    unittest.main(verbosity=2)
