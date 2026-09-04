import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import ANY, call, patch

# Prefer incremental worktree validators when present.
_WORKTREE = Path(__file__).resolve().parent.parent / "Chosen" / ".worktrees" / "skin-incremental"
if _WORKTREE.is_dir() and not os.environ.get("CHOSEN_ROOT", "").strip():
    os.environ["CHOSEN_ROOT"] = str(_WORKTREE)

from publish_skin_release import (  # noqa: E402
    DELTA_SIZE_RATIO_LIMIT,
    HK_DELTA_OBJECT_PREFIX,
    HK_OBJECT_PREFIX,
    ReleaseError,
    build_delta_zip,
    build_manifest,
    format_skin_delta_asset_name,
    hk_delta_public_url,
    hk_public_url,
    inspect_package,
    prepare_delta_package,
    publish,
    r2_object_key,
    r2_public_url,
    upload_to_r2,
    check_download_range,
)


class PublishSkinReleaseTests(unittest.TestCase):
    def make_zip(self, root_names=None, version="0.0.8", contents=None):
        root_names = root_names or ["resources/en/skin_ids.json", "skins/1/1001/1001.fantome", "version.json"]
        contents = contents or {}
        handle = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        handle.close()
        path = Path(handle.name)
        with zipfile.ZipFile(path, "w") as archive:
            for name in root_names:
                if name == "version.json":
                    archive.writestr(name, json.dumps({"version": version}))
                else:
                    archive.writestr(name, contents.get(name, b"test"))
        return path

    def make_named_zip(self, directory: Path, asset_name: str, *, version: str, files: dict[str, bytes]):
        path = directory / asset_name
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("version.json", json.dumps({"version": version}))
            for name, payload in files.items():
                archive.writestr(name, payload)
        return path

    def test_inspect_package_reads_version_and_metadata(self):
        path = self.make_zip()
        try:
            info = inspect_package(path)
            self.assertEqual(info.version, "0.0.8")
            self.assertEqual(info.asset_name, path.name)
            self.assertEqual(info.tag, "skin-v0.0.8")
            self.assertGreater(info.size, 0)
            self.assertEqual(len(info.sha256), 64)
        finally:
            path.unlink()

    def test_inspect_package_rejects_extra_outer_directory(self):
        path = self.make_zip([
            "skins0.08/resources/en/skin_ids.json",
            "skins0.08/skins/1/1001/1001.fantome",
            "skins0.08/version.json",
        ])
        try:
            with self.assertRaises(ReleaseError):
                inspect_package(path)
        finally:
            path.unlink()

    def test_inspect_package_accepts_backslash_entry_names(self):
        path = self.make_zip([
            r"resources\en\skin_ids.json",
            r"skins\1\1001\1001.fantome",
            "version.json",
        ])
        try:
            info = inspect_package(path)
            self.assertEqual(info.version, "0.0.8")
        finally:
            path.unlink()

    def test_r2_paths_use_versioned_asset_name(self):
        path = self.make_zip()
        try:
            info = inspect_package(path)
            self.assertEqual(r2_object_key(info), "woguaskins/" + path.name)
            self.assertEqual(r2_public_url(info), "https://cdn.chosen.cc.cd/woguaskins/" + path.name)
        finally:
            path.unlink()

    def test_upload_to_r2_uploads_and_checks_size(self):
        path = self.make_zip()
        try:
            info = inspect_package(path)

            class FakeR2:
                def __init__(self):
                    self.calls = []
                    self.head_calls = 0

                def upload_file(self, filename, bucket, key, ExtraArgs):
                    self.calls.append((filename, bucket, key, ExtraArgs))

                def head_object(self, Bucket, Key):
                    self.head_calls += 1
                    if self.head_calls == 1:
                        from botocore.exceptions import ClientError
                        raise ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject")
                    return {"ContentLength": info.size}

            fake = FakeR2()
            with patch.dict(os.environ, {"R2_BUCKET": "lolskin"}, clear=False):
                upload_to_r2(info, lambda message: None, client=fake)
            self.assertEqual(len(fake.calls), 1)
            self.assertEqual(fake.calls[0][1:], ("lolskin", "woguaskins/" + path.name, {"ContentType": "application/zip"}))
        finally:
            path.unlink()

    def test_proxy_accepts_valid_zip_header(self):
        path = self.make_zip()
        try:
            info = inspect_package(path)

            class FakeRaw:
                def read(self, size):
                    return b"PK\x03\x04" + b"test"

            class FakeResponse:
                status_code = 206
                headers = {"Content-Range": f"bytes 0-1048575/{info.size}"}
                raw = FakeRaw()

                def close(self):
                    pass

            with patch("publish_skin_release.requests.get", return_value=FakeResponse()):
                check_download_range(info, lambda message: None, url="https://example.test/skin.zip", label="测试")
        finally:
            path.unlink()

    def test_manifest_contains_proxy_and_fallback_urls(self):
        path = self.make_zip()
        try:
            info = inspect_package(path)
            manifest = build_manifest(info)
            self.assertEqual(manifest["version"], "0.0.8")
            self.assertEqual(manifest["release_tag"], "skin-v0.0.8")
            self.assertEqual(manifest["download_url"], hk_public_url(info))
            self.assertEqual(len(manifest["download_url_backup"]), 7)
            self.assertEqual(manifest["download_url_backup"][0], "https://cdn.chosen.cc.cd/woguaskins/" + path.name)
            self.assertTrue(any(url.startswith("https://gh-proxy.com/") for url in manifest["download_url_backup"]))
            self.assertTrue(any(url.startswith("https://ghproxy.net/") for url in manifest["download_url_backup"]))
            self.assertTrue(any(url.startswith("https://ghfast.top/") for url in manifest["download_url_backup"]))
            self.assertEqual(manifest["size"], info.size)
            self.assertEqual(manifest["sha256"], info.sha256)
            self.assertNotIn("delta_from", manifest)
        finally:
            path.unlink()

    def test_format_skin_delta_asset_name(self):
        self.assertEqual(
            format_skin_delta_asset_name("0.0.5", "0.0.4", "skins0.05.zip"),
            "skins0.05-from-0.04.zip",
        )

    def test_build_delta_zip_adds_modifies_and_deletes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            previous = root / "previous"
            new = root / "new"
            (previous / "skins" / "keep").mkdir(parents=True)
            (previous / "skins" / "gone").mkdir(parents=True)
            (previous / "resources").mkdir(parents=True)
            (previous / "skins" / "keep" / "a.bin").write_bytes(b"keep")
            (previous / "skins" / "gone" / "old.bin").write_bytes(b"old")
            (previous / "resources" / "shared.txt").write_bytes(b"shared-v1")
            (previous / "version.json").write_text(json.dumps({"version": "0.0.4"}), encoding="utf-8")

            (new / "skins" / "keep").mkdir(parents=True)
            (new / "skins" / "fresh").mkdir(parents=True)
            (new / "resources").mkdir(parents=True)
            (new / "skins" / "keep" / "a.bin").write_bytes(b"keep")
            (new / "skins" / "fresh" / "new.bin").write_bytes(b"new")
            (new / "resources" / "shared.txt").write_bytes(b"shared-v2")
            (new / "version.json").write_text(json.dumps({"version": "0.0.5"}), encoding="utf-8")

            dest = root / "skins0.05-from-0.04.zip"
            size, digest, deletes = build_delta_zip(previous, new, dest, "0.0.5")
            self.assertGreater(size, 0)
            self.assertEqual(len(digest), 64)
            self.assertEqual(deletes, ["skins/gone/old.bin"])

            with zipfile.ZipFile(dest) as archive:
                names = set(archive.namelist())
                self.assertIn("delete_list.json", names)
                self.assertIn("version.json", names)
                self.assertIn("skins/fresh/new.bin", names)
                self.assertIn("resources/shared.txt", names)
                self.assertNotIn("skins/keep/a.bin", names)
                self.assertEqual(json.loads(archive.read("delete_list.json")), ["skins/gone/old.bin"])
                self.assertEqual(json.loads(archive.read("version.json"))["version"], "0.0.5")
                self.assertEqual(archive.read("skins/fresh/new.bin"), b"new")
                self.assertEqual(archive.read("resources/shared.txt"), b"shared-v2")

    def test_size_gate_omits_delta(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prev = self.make_named_zip(
                root,
                "skins0.04.zip",
                version="0.0.4",
                files={
                    "resources/en/skin_ids.json": b"old-" + (b"x" * 2000),
                    "skins/1/1001/1001.fantome": b"old-skin",
                },
            )
            full = self.make_named_zip(
                root,
                "skins0.05.zip",
                version="0.0.5",
                files={
                    "resources/en/skin_ids.json": b"new-" + (b"y" * 2000),
                    "skins/1/1001/1001.fantome": b"new-skin-" + (b"z" * 2000),
                },
            )
            full_info = inspect_package(full)
            logs: list[str] = []
            with patch("publish_skin_release.DELTA_SIZE_RATIO_LIMIT", 0.0):
                delta_info, delta_from = prepare_delta_package(
                    full_info,
                    prev,
                    root / "work",
                    logs.append,
                )
            self.assertIsNone(delta_info)
            self.assertIsNone(delta_from)
            self.assertTrue(any("跳过 delta" in message for message in logs))
            self.assertEqual(DELTA_SIZE_RATIO_LIMIT, 0.7)

    def test_manifest_delta_backup_length_is_three(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shared = b"KEEP" + (b"k" * 50_000)
            prev = self.make_named_zip(
                root,
                "skins0.04.zip",
                version="0.0.4",
                files={
                    "resources/en/skin_ids.json": b"old",
                    "skins/1/1001/1001.fantome": shared,
                },
            )
            full = self.make_named_zip(
                root,
                "skins0.05.zip",
                version="0.0.5",
                files={
                    "resources/en/skin_ids.json": b"new",
                    "skins/1/1001/1001.fantome": shared,
                    "skins/2/2002/2002.fantome": b"added",
                },
            )
            full_info = inspect_package(full)
            delta_info, delta_from = prepare_delta_package(
                full_info,
                prev,
                root / "work",
                lambda message: None,
            )
            self.assertIsNotNone(delta_info)
            self.assertEqual(delta_from, "0.0.4")
            manifest = build_manifest(full_info, delta_info=delta_info, delta_from=delta_from)
            self.assertEqual(manifest["delta_from"], "0.0.4")
            self.assertEqual(manifest["delta_download_url"], hk_delta_public_url(delta_info))
            self.assertEqual(len(manifest["delta_download_url_backup"]), 3)
            self.assertEqual(
                manifest["delta_download_url_backup"],
                [
                    "https://gh-proxy.com/" + delta_info.github_url,
                    "https://cdn.gh-proxy.org/" + delta_info.github_url,
                    "https://v4.gh-proxy.org/" + delta_info.github_url,
                ],
            )
            self.assertTrue(manifest["delta_download_url"].endswith("/woguaskins-delta/" + delta_info.asset_name))

    def test_publish_does_not_upload_delta_to_r2(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shared = b"KEEP" + (b"k" * 50_000)
            prev = self.make_named_zip(
                root,
                "skins0.04.zip",
                version="0.0.4",
                files={
                    "resources/en/skin_ids.json": b"old",
                    "skins/1/1001/1001.fantome": shared,
                },
            )
            full = self.make_named_zip(
                root,
                "skins0.05.zip",
                version="0.0.5",
                files={
                    "resources/en/skin_ids.json": b"new",
                    "skins/1/1001/1001.fantome": shared,
                },
            )
            r2_calls = []

            def fake_r2(info, log, client=None):
                r2_calls.append(info.asset_name)
                return {"ContentLength": info.size}

            with patch("publish_skin_release.upload_to_r2", side_effect=fake_r2) as mock_r2, patch(
                "publish_skin_release.publish_release",
                return_value={"release": {}, "asset": {}, "delta_asset": {}},
            ) as mock_gh, patch("publish_skin_release.sync_to_hk_mirror") as mock_hk, patch(
                "publish_skin_release.check_download_range"
            ), patch(
                "publish_skin_release.write_and_push_manifest",
                return_value="abc123",
            ), patch("publish_skin_release.verify_remote"):
                result = publish(full, lambda message: None, previous_zip=prev)

            self.assertEqual(r2_calls, ["skins0.05.zip"])
            mock_r2.assert_called_once()
            delta_info = result["delta_info"]
            self.assertIsNotNone(delta_info)
            self.assertTrue(str(delta_info.asset_name).endswith("-from-0.04.zip"))
            self.assertIs(mock_gh.call_args.kwargs.get("delta_info"), delta_info)
            mock_hk.assert_has_calls(
                [
                    call(ANY, ANY, object_prefix=HK_OBJECT_PREFIX),
                    call(delta_info, ANY, object_prefix=HK_DELTA_OBJECT_PREFIX),
                ],
                any_order=False,
            )
            self.assertEqual(mock_hk.call_count, 2)


if __name__ == "__main__":
    unittest.main()
