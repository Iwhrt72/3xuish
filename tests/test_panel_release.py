"""Official panel release validation must finish before live service changes."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
import panel_release as release

NAME = "x-ui-linux-amd64.tar.gz"
TAG = "v3.11.0"
URL = release.DOWNLOAD_PREFIX + TAG + "/" + NAME
ELF = b"\x7fELF" + b"fixture-panel-binary"


def metadata(digest="sha256:" + "a" * 64):
    return {"tag_name": TAG, "draft": False, "prerelease": False,
            "assets": [{"name": NAME, "size": 100, "digest": digest, "browser_download_url": URL}]}


def archive_bytes(items=None):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as archive:
        for path, value, kind in items or [("x-ui/x-ui", ELF, tarfile.REGTYPE),
                                           ("x-ui/x-ui.sh", b"#!/bin/bash\nexit 0\n", tarfile.REGTYPE),
                                           ("x-ui/bin/xray-linux-amd64", b"preserved-core-not-installed", tarfile.REGTYPE)]:
            item = tarfile.TarInfo(path)
            item.type = kind
            item.mode = 0o7777
            if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                item.linkname = "/etc/passwd"
            item.size = len(value) if kind == tarfile.REGTYPE else 0
            archive.addfile(item, io.BytesIO(value) if kind == tarfile.REGTYPE else None)
    return data.getvalue()


class VersionsAndMetadata(unittest.TestCase):
    def test_numeric_versions_do_not_compare_lexically_or_downgrade(self):
        self.assertEqual(release.version_action("v3.9.9", "3.11.0"), "upgrade")
        self.assertEqual(release.version_action("3.11.0", "v3.11.0"), "noop")
        self.assertEqual(release.version_action("3.12.0", "v3.11.0"), "newer")
        for value in ("dev-latest", "v3.11.0-rc1", "3.11", "3.011.0", "3.11.0;exit", None):
            with self.subTest(value=value), self.assertRaises(release.ReleaseError):
                release.normalize_version(value)

    def test_architecture_mapping_matches_official_packages(self):
        for value, expected in (("x86_64", "amd64"), ("i686", "386"), ("aarch64", "arm64"),
                                ("armv8l", "arm64"), ("armv7l", "armv7"), ("armv6l", "armv6"),
                                ("armv5tel", "armv5"), ("s390x", "s390x")):
            self.assertEqual(release.architecture_name(value), expected)
        with self.assertRaises(release.ReleaseError):
            release.architecture_name("riscv64")

    def test_only_stable_unambiguous_official_assets_are_selected(self):
        selected = release.select_release(metadata(), "amd64")
        self.assertEqual(selected["version"], "3.11.0")
        self.assertEqual(selected["asset"]["sha256"], "a" * 64)
        cases = []
        for key in ("draft", "prerelease"):
            value = metadata(); value[key] = True; cases.append(value)
            value = metadata(); del value[key]; cases.append(value)
        value = metadata(); value["tag_name"] = "v3.11.0-rc1"; cases.append(value)
        value = metadata(); value["assets"] *= 2; cases.append(value)
        value = metadata(); value["assets"] = []; cases.append(value)
        value = metadata(); value["assets"][0]["size"] = release.MAX_ARCHIVE + 1; cases.append(value)
        value = metadata(); value["assets"][0]["size"] = True; cases.append(value)
        for value in cases:
            with self.subTest(value=value), self.assertRaises(release.ReleaseError):
                release.select_release(value, "amd64")

    def test_untrusted_asset_url_and_digest_are_rejected(self):
        for url in (URL.replace("https:", "http:"), URL.replace("github.com", "github.com.evil.test"),
                    URL.replace("MHSanaei", "somebody"), URL + "?redirect=1"):
            value = metadata(); value["assets"][0]["browser_download_url"] = url
            with self.subTest(url=url), self.assertRaises(release.ReleaseError):
                release.select_release(value, "amd64")
        for digest in ("md5:" + "a" * 32, "sha256:wrong", 123):
            with self.subTest(digest=digest), self.assertRaises(release.ReleaseError):
                release.select_release(metadata(digest), "amd64")

    def test_no_checksum_is_a_hard_failure_and_sidecar_is_exact(self):
        with self.assertRaisesRegex(release.ReleaseError, "checksum_missing"):
            release.select_release(metadata(None), "amd64")
        value = metadata(None)
        sidecar = ("b" * 64 + "  " + NAME + "\n").encode()
        value["assets"].append({"name": NAME + ".sha256", "size": len(sidecar),
                                "digest": "sha256:" + hashlib.sha256(sidecar).hexdigest(),
                                "browser_download_url": URL + ".sha256"})
        selected = release.select_release(value, "amd64")
        with mock.patch.object(release, "_read_url", return_value=sidecar):
            self.assertEqual(release._checksum(selected), "b" * 64)
        with mock.patch.object(release, "_read_url", return_value=sidecar.replace(b"bbbb", b"cccc", 1)):
            with self.assertRaisesRegex(release.ReleaseError, "checksum_digest_mismatch"):
                release._checksum(selected)
        selected["checksum_asset"]["sha256"] = None
        wrong = ("b" * 64 + "  another-artifact.tar.gz\n").encode()
        selected["checksum_asset"]["size"] = len(wrong)
        with mock.patch.object(release, "_read_url", return_value=wrong):
            with self.assertRaisesRegex(release.ReleaseError, "checksum_invalid"):
                release._checksum(selected)

    def test_redirects_never_downgrade_tls_or_go_to_unrelated_hosts(self):
        release._network_url("https://release-assets.githubusercontent.com/path?signature=value", redirect=True)
        for url in ("http://github.com/path", "https://evil.test/package", "https://github.com:444/path",
                    "https://user:secret@github.com/path", "https://release-assets.githubusercontent.com.evil.test/x"):
            with self.subTest(url=url), self.assertRaises(release.ReleaseError):
                release._network_url(url, redirect=True)

    def test_latest_metadata_is_bounded_and_has_safe_failure(self):
        with mock.patch.object(release, "_open_url", return_value=io.BytesIO(b"a" * 6)):
            with self.assertRaisesRegex(release.ReleaseError, "too_large"):
                release._read_url(release.RELEASE_API, 5)
        with mock.patch.object(release, "_read_url", return_value=b"not-json-secret"):
            with self.assertRaisesRegex(release.ReleaseError, "^release_metadata_invalid$"):
                release.latest_release("amd64")
        with mock.patch.object(release, "_read_url", return_value=json.dumps(metadata()).encode()):
            self.assertEqual(release.latest_release("amd64")["tag"], TAG)


class DownloadAndExtraction(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="panel-release-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_low_disk_stops_before_download_or_partial_file_creation(self):
        data = archive_bytes()
        target = self.root / "release.tar.gz"
        asset = {"url": URL, "size": len(data)}
        with mock.patch.object(release.shutil, "disk_usage",
                               return_value=SimpleNamespace(free=len(data) + release.STAGING_RESERVE - 1)), \
                mock.patch.object(release, "_open_url") as network:
            with self.assertRaisesRegex(release.ReleaseError, "^insufficient_release_space$"):
                release.download_asset(asset, target, hashlib.sha256(data).hexdigest())
        network.assert_not_called()
        self.assertFalse(target.exists())
        self.assertFalse(target.with_name(target.name + ".part").exists())

    def test_low_disk_stops_before_writing_any_expanded_member(self):
        data = archive_bytes([("x-ui/x-ui", ELF, tarfile.REGTYPE)])
        archive = self.root / "release.tar.gz"
        archive.write_bytes(data)
        stage = self.root / "stage"
        with mock.patch.object(release.shutil, "disk_usage",
                               return_value=SimpleNamespace(free=len(ELF) + release.STAGING_RESERVE - 1)), \
                mock.patch.object(release.subprocess, "run") as execute:
            with self.assertRaisesRegex(release.ReleaseError, "^insufficient_release_space$"):
                release.extract_archive(archive, stage)
        execute.assert_not_called()
        self.assertEqual(list(stage.iterdir()), [])
        self.assertEqual(archive.read_bytes(), data)

    def test_digest_and_size_are_checked_before_archive_promotion(self):
        data = archive_bytes()
        asset = {"url": URL, "size": len(data)}
        digest = hashlib.sha256(data).hexdigest()
        for suffix, expected_size, expected_hash, error in (
                ("wrong-hash", len(data), "0" * 64, "digest_mismatch"),
                ("truncated", len(data) + 1, digest, "size_mismatch"),
                ("oversized", len(data) - 1, digest, "size_mismatch")):
            target = self.root / suffix
            with mock.patch.object(release, "_open_url", return_value=io.BytesIO(data)):
                with self.assertRaisesRegex(release.ReleaseError, error):
                    release.download_asset({**asset, "size": expected_size}, target, expected_hash)
            self.assertFalse(target.exists())
            self.assertFalse(target.with_name(target.name + ".part").exists())
        with mock.patch.object(release, "_open_url", return_value=io.BytesIO(data)):
            release.download_asset(asset, self.root / "good.tar.gz", digest)
        self.assertEqual((self.root / "good.tar.gz").read_bytes(), data)

    def extract(self, data):
        path = self.root / "archive.tar.gz"
        path.write_bytes(data)
        with mock.patch.object(release.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)):
            return release.extract_archive(path, self.root / "stage")

    def test_regular_release_extracts_expected_paths_without_archive_permissions(self):
        package = self.extract(archive_bytes())
        self.assertEqual((package / "x-ui").read_bytes(), ELF)
        self.assertTrue((package / "x-ui.sh").is_file())
        self.assertEqual((package / "x-ui").stat().st_mode & 0o7000, 0)

    def test_paths_and_link_types_are_rejected_before_writing_any_member(self):
        for name, kind in (("../escape", tarfile.REGTYPE), ("x-ui/../escape", tarfile.REGTYPE),
                           ("/tmp/escape", tarfile.REGTYPE), ("C:/escape", tarfile.REGTYPE),
                           ("x-ui\\escape", tarfile.REGTYPE), ("x-ui/a\nb", tarfile.REGTYPE),
                           ("x-ui/link", tarfile.SYMTYPE), ("x-ui/link", tarfile.LNKTYPE),
                           ("x-ui/pipe", tarfile.FIFOTYPE), ("x-ui/device", tarfile.CHRTYPE)):
            with self.subTest(name=name, kind=kind), tempfile.TemporaryDirectory(dir=self.root) as temp:
                path = Path(temp) / "archive.tar.gz"
                path.write_bytes(archive_bytes([("x-ui/x-ui", ELF, tarfile.REGTYPE), (name, b"bad", kind)]))
                with self.assertRaises(release.ReleaseError):
                    release.extract_archive(path, Path(temp) / "stage")
                self.assertFalse((Path(temp) / "stage/x-ui/x-ui").exists())

    def test_duplicate_members_and_resource_limits_are_rejected(self):
        cases = [archive_bytes([("x-ui/x-ui", ELF, tarfile.REGTYPE)] * 2)]
        for data in cases:
            with self.assertRaisesRegex(release.ReleaseError, "duplicate"):
                self.extract(data)
        with mock.patch.object(release, "MAX_MEMBERS", 1):
            with self.assertRaisesRegex(release.ReleaseError, "too_many"):
                self.extract(archive_bytes())

    def test_tar_headers_and_seeks_cannot_expand_without_bounds(self):
        reader = release._BoundedTarReader(io.BytesIO(b"fixture"))
        with self.assertRaises(release.ReleaseError):
            reader.read(release.MAX_TAR_READ + 1)
        with self.assertRaises(release.ReleaseError):
            reader.seek(release.MAX_EXPANDED + 1)
        self.assertEqual(reader.tell(), 0)
        with mock.patch.object(release, "MAX_EXPANDED", 5):
            with self.assertRaisesRegex(release.ReleaseError, "expands_too_large"):
                self.extract(archive_bytes())

    def test_missing_empty_or_non_elf_panel_is_rejected(self):
        for items, error in (([("x-ui/other", b"other", tarfile.REGTYPE)], "binary_missing"),
                             ([("x-ui/x-ui", b"", tarfile.REGTYPE)], "binary_missing"),
                             ([("x-ui/x-ui", b"#!/bin/sh\n", tarfile.REGTYPE)], "binary_invalid")):
            with self.subTest(items=items), tempfile.TemporaryDirectory(dir=self.root) as temp:
                path = Path(temp) / "archive.tar.gz"; path.write_bytes(archive_bytes(items))
                with self.assertRaisesRegex(release.ReleaseError, error):
                    release.extract_archive(path, Path(temp) / "stage")

    def test_invalid_cli_is_rejected_before_candidate_can_be_used(self):
        path = self.root / "archive.tar.gz"; path.write_bytes(archive_bytes())
        with mock.patch.object(release.subprocess, "run", return_value=subprocess.CompletedProcess([], 2)):
            with self.assertRaisesRegex(release.ReleaseError, "cli_invalid"):
                release.extract_archive(path, self.root / "stage")

    def test_nonempty_destination_is_never_overwritten(self):
        stage = self.root / "stage"; stage.mkdir(); (stage / "existing").write_text("keep")
        with self.assertRaisesRegex(release.ReleaseError, "stage_not_empty"):
            self.extract(archive_bytes())
        self.assertEqual((stage / "existing").read_text(), "keep")

    def test_prepare_checks_actual_candidate_version_before_returning(self):
        data = archive_bytes()
        meta = metadata("sha256:" + hashlib.sha256(data).hexdigest())
        meta["assets"][0]["size"] = len(data)
        selected = release.select_release(meta, "amd64")
        for actual, succeeds in (("3.11.0", True), ("3.10.0", False)):
            with self.subTest(actual=actual), tempfile.TemporaryDirectory(dir=self.root) as temp:
                with mock.patch.object(release, "_open_url", return_value=io.BytesIO(data)), \
                     mock.patch.object(release, "installed_version", return_value=actual), \
                     mock.patch.object(release.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)):
                    if succeeds:
                        result = release.prepare_release(selected, Path(temp) / "release")
                        self.assertTrue(Path(result["binary"]).is_file())
                        self.assertTrue(Path(result["cli"]).is_file())
                        self.assertEqual(result["version"], "3.11.0")
                        self.assertEqual(result["sha256"], hashlib.sha256(data).hexdigest())
                    else:
                        with self.assertRaisesRegex(release.ReleaseError, "binary_version_mismatch"):
                            release.prepare_release(selected, Path(temp) / "release")

    def test_version_uses_read_only_v_flag_and_bounds_output(self):
        binary = self.root / "x-ui"; binary.write_bytes(ELF)
        def version_process(args, **kwargs):
            self.assertEqual(args, [str(binary), "-v"])
            self.assertEqual(kwargs["timeout"], release.VERSION_TIMEOUT)
            kwargs["stdout"].write(b"3.11.0\n")
            return subprocess.CompletedProcess(args, 0)
        with mock.patch.object(release.subprocess, "run", side_effect=version_process):
            self.assertEqual(release.installed_version(binary), "3.11.0")
        with mock.patch.object(release.subprocess, "run", side_effect=subprocess.TimeoutExpired([], 1)):
            with self.assertRaisesRegex(release.ReleaseError, "version_check_failed"):
                release.installed_version(binary)


if __name__ == "__main__":
    unittest.main()
