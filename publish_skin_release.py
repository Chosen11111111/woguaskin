from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from tkinter import END, BOTH, LEFT, RIGHT, X, Button, Entry, Frame, Label, StringVar, Tk, filedialog, messagebox
from tkinter.scrolledtext import ScrolledText
from typing import Callable
from urllib.parse import quote

try:
    import requests
except ImportError as exc:
    raise SystemExit("缺少 requests，请使用 E:\\ChosenSkin2.0\\Chosen\\.venv\\Scripts\\python.exe 运行") from exc

try:
    import truststore
except ImportError:
    truststore = None

ROOT = Path(__file__).resolve().parent
CHOSEN_ROOT = (
    Path(os.environ["CHOSEN_ROOT"]).expanduser().resolve()
    if os.environ.get("CHOSEN_ROOT", "").strip()
    else (ROOT.parent / "Chosen")
)
MANIFEST_PATH = ROOT / "skin-manifest.json"
OWNER = "Chosen11111111"
REPOSITORY = "woguaskin"
BRANCH = "main"
PROXY_PREFIX = "https://v4.gh-proxy.org/"
BACKUP_PREFIXES = (
    "https://cdn.gh-proxy.org/",
    "https://v6.gh-proxy.org/",
    "https://gh-proxy.com/",
    "https://ghproxy.net/",
    "https://ghfast.top/",
)
DELTA_SIZE_RATIO_LIMIT = 0.7
STABLE_DELTA_PROXY_PREFIXES = (
    "https://gh-proxy.com/",
    "https://cdn.gh-proxy.org/",
    "https://v4.gh-proxy.org/",
)
API_BASE = "https://api.github.com"
RAW_MANIFEST_URL = "https://raw.githubusercontent.com/Chosen11111111/woguaskin/main/skin-manifest.json"
R2_PUBLIC_BASE_URL = "https://cdn.chosen.cc.cd/woguaskins"
R2_OBJECT_PREFIX = "woguaskins"
HK_PUBLIC_BASE_URL = "https://download.52015002.xyz"
HK_REMOTE_ROOT = "/opt/1panel/www/sites/download.52015002.xyz/index"
HK_OBJECT_PREFIX = "woguaskins"
HK_DELTA_OBJECT_PREFIX = "woguaskins-delta"
HK_DEFAULT_HOST = "207.57.125.16"
HK_DEFAULT_USER = "root"
HK_DEFAULT_SSH_KEY = str(Path.home() / ".ssh" / "chosen_hk")

if str(CHOSEN_ROOT) not in sys.path:
    sys.path.insert(0, str(CHOSEN_ROOT))
try:
    from src.core.skin_updater import (
        validate_skin_archive,
        validate_skin_delta_archive,
        validate_skin_manifest,
    )
except ImportError as exc:
    validate_skin_archive = None
    validate_skin_delta_archive = None
    validate_skin_manifest = None
    PRODUCTION_VALIDATOR_ERROR = exc
else:
    PRODUCTION_VALIDATOR_ERROR = None


class ReleaseError(RuntimeError):
    pass


@dataclass(frozen=True)
class PackageInfo:
    path: Path
    version: str
    asset_name: str
    size: int
    sha256: str
    tag: str
    github_url: str
    proxy_url: str


def _enable_system_tls() -> None:
    if truststore is not None:
        truststore.inject_into_ssl()


def _safe_json_response(response: requests.Response) -> dict:
    try:
        payload = response.json()
    except ValueError as exc:
        raise ReleaseError(f"GitHub 返回的不是 JSON: HTTP {response.status_code}") from exc
    if not isinstance(payload, dict):
        raise ReleaseError("GitHub 返回的数据格式错误")
    return payload


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def iter_tree_files(root: Path) -> dict[str, Path]:
    root = Path(root).resolve()
    mapping: dict[str, Path] = {}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        mapping[path.relative_to(root).as_posix()] = path
    return mapping


def _version_compact(version: str) -> str:
    parts = version.strip().split(".")
    if len(parts) >= 3 and parts[0] == "0":
        return "0." + "".join(parts[1:])
    return version.strip()


def format_skin_delta_asset_name(new_version: str, prev_version: str, full_asset_name: str) -> str:
    del new_version  # stem already encodes the new package version
    stem = Path(full_asset_name).stem
    return f"{stem}-from-{_version_compact(prev_version)}.zip"


def _validate_zip_names(names: list[str]) -> None:
    seen: set[str] = set()
    roots: set[str] = set()
    for raw_name in names:
        name = raw_name.replace("\\", "/")
        if not name or name.endswith("/"):
            continue
        parts = name.split("/")
        if name.startswith("/") or ":" in parts[0] or ".." in parts:
            raise ReleaseError(f"ZIP 存在不安全路径: {raw_name}")
        key = name.casefold()
        if key in seen:
            raise ReleaseError(f"ZIP 存在重复路径: {raw_name}")
        seen.add(key)
        roots.add(parts[0].casefold())
        if key in {"chosen.exe", "chosenupdater.exe", "_internal", "chosendata", ".git", "skin-manifest.json"} or key.endswith(".zip"):
            raise ReleaseError(f"ZIP 包含禁止内容: {raw_name}")
    if roots != {"resources", "skins", "version.json"}:
        raise ReleaseError(f"ZIP 根目录必须只有 resources、skins、version.json，实际为: {sorted(roots)}")


def _package_info_from_path(zip_path: Path, version: str, asset_name: str | None = None) -> PackageInfo:
    zip_path = Path(zip_path).expanduser().resolve()
    name = asset_name or zip_path.name
    digest = file_sha256(zip_path)
    size = zip_path.stat().st_size
    tag = f"skin-v{version}"
    github_url = f"https://github.com/{OWNER}/{REPOSITORY}/releases/download/{tag}/{name}"
    return PackageInfo(zip_path, version, name, size, digest, tag, github_url, PROXY_PREFIX + github_url)


def inspect_package(zip_path: Path) -> PackageInfo:
    zip_path = Path(zip_path).expanduser().resolve()
    if not zip_path.is_file() or zip_path.suffix.casefold() != ".zip":
        raise ReleaseError("请选择一个存在的 ZIP 文件")
    try:
        with zipfile.ZipFile(zip_path) as archive:
            names = archive.namelist()
            _validate_zip_names(names)
            try:
                version_payload = json.loads(archive.read("version.json").decode("utf-8-sig"))
            except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ReleaseError("ZIP 根目录的 version.json 无法读取") from exc
            version = version_payload.get("version") if isinstance(version_payload, dict) else None
            if not isinstance(version, str) or not re.fullmatch(r"\d+(?:\.\d+)+", version):
                raise ReleaseError("version.json 的 version 不是有效版本号")
            if archive.testzip() is not None:
                raise ReleaseError("ZIP 存在损坏文件")
    except zipfile.BadZipFile as exc:
        raise ReleaseError("文件不是有效 ZIP") from exc
    if PRODUCTION_VALIDATOR_ERROR is not None or validate_skin_archive is None:
        raise ReleaseError("无法加载 Chosen 生产 ZIP 校验器，请使用项目 Python 环境运行") from PRODUCTION_VALIDATOR_ERROR
    holder = tempfile.TemporaryDirectory(prefix="skin-release-check-")
    try:
        validate_skin_archive(zip_path, Path(holder.name) / "extract", version)
    except Exception as exc:
        raise ReleaseError(f"生产 ZIP 校验失败: {exc}") from exc
    finally:
        holder.cleanup()
    return _package_info_from_path(zip_path, version)


def _extract_skin_tree(zip_path: Path, dest: Path) -> Path:
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            name = info.filename.replace("\\", "/")
            if not name or name.endswith("/"):
                if name:
                    (dest / name).mkdir(parents=True, exist_ok=True)
                continue
            parts = name.split("/")
            if name.startswith("/") or ":" in parts[0] or ".." in parts:
                raise ReleaseError(f"ZIP 存在不安全路径: {info.filename}")
            target = dest.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
    return dest


def build_delta_zip(previous_root: Path, new_root: Path, dest_zip: Path, new_version: str) -> tuple[int, str, list[str]]:
    previous_root = Path(previous_root)
    new_root = Path(new_root)
    dest_zip = Path(dest_zip)
    prev_files = iter_tree_files(previous_root)
    new_files = iter_tree_files(new_root)
    if "version.json" not in new_files:
        raise ReleaseError("新皮肤树缺少 version.json")
    prev_hashes = {rel: file_sha256(path) for rel, path in prev_files.items()}
    new_hashes = {rel: file_sha256(path) for rel, path in new_files.items()}

    deletes = sorted(
        rel
        for rel in prev_hashes
        if rel not in new_hashes and (rel.startswith("skins/") or rel.startswith("resources/"))
    )
    include = {
        rel
        for rel, digest in new_hashes.items()
        if rel == "version.json" or prev_hashes.get(rel) != digest
    }
    include.add("version.json")

    version_payload = json.loads(new_files["version.json"].read_text(encoding="utf-8-sig"))
    package_version = version_payload.get("version") if isinstance(version_payload, dict) else None
    if package_version != new_version:
        raise ReleaseError(f"新皮肤树 version.json ({package_version}) 与目标版本 {new_version} 不一致")

    dest_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("delete_list.json", json.dumps(deletes, ensure_ascii=False, indent=2) + "\n")
        for rel in sorted(include):
            archive.write(new_files[rel], arcname=rel)

    size = dest_zip.stat().st_size
    digest = file_sha256(dest_zip)
    return size, digest, deletes


def prepare_delta_package(
    full_info: PackageInfo,
    previous_zip: Path,
    work_dir: Path,
    log: Callable[[str], None],
) -> tuple[PackageInfo | None, str | None]:
    previous_zip = Path(previous_zip).expanduser().resolve()
    prev_info = inspect_package(previous_zip)
    if prev_info.version == full_info.version:
        raise ReleaseError("上一版 ZIP 与当前 ZIP 版本相同，无法构建增量包")
    log(f"上一版皮肤: {prev_info.version} ({prev_info.asset_name})")

    prev_root = _extract_skin_tree(previous_zip, Path(work_dir) / "previous")
    new_root = _extract_skin_tree(full_info.path, Path(work_dir) / "new")
    asset_name = format_skin_delta_asset_name(full_info.version, prev_info.version, full_info.asset_name)
    dest_zip = Path(work_dir) / asset_name
    size, digest, deletes = build_delta_zip(prev_root, new_root, dest_zip, full_info.version)
    log(f"增量包已构建: {asset_name}，{size} bytes，删除 {len(deletes)} 个路径")

    if size >= DELTA_SIZE_RATIO_LIMIT * full_info.size:
        log(
            f"增量包过大（{size} >= {DELTA_SIZE_RATIO_LIMIT:.0%} * {full_info.size}），跳过 delta 发布"
        )
        return None, None

    if PRODUCTION_VALIDATOR_ERROR is not None or validate_skin_delta_archive is None:
        raise ReleaseError("无法加载 Chosen 增量 ZIP 校验器，请设置 CHOSEN_ROOT 指向含增量校验的源码") from PRODUCTION_VALIDATOR_ERROR
    check_dir = Path(work_dir) / "delta-check"
    try:
        validate_skin_delta_archive(dest_zip, check_dir, full_info.version)
    except Exception as exc:
        raise ReleaseError(f"增量 ZIP 校验失败: {exc}") from exc

    delta_info = _package_info_from_path(dest_zip, full_info.version, asset_name)
    if delta_info.sha256 != digest or delta_info.size != size:
        raise ReleaseError("增量包元数据不一致")
    return delta_info, prev_info.version


def r2_object_key(info: PackageInfo) -> str:
    prefix = os.environ.get("R2_OBJECT_PREFIX", R2_OBJECT_PREFIX).strip("/")
    return f"{prefix}/{info.asset_name}" if prefix else info.asset_name


def r2_public_url(info: PackageInfo) -> str:
    base = os.environ.get("R2_PUBLIC_BASE_URL", R2_PUBLIC_BASE_URL).rstrip("/")
    return f"{base}/{quote(info.asset_name, safe='')}"


def hk_public_url(info: PackageInfo, object_prefix: str = HK_OBJECT_PREFIX) -> str:
    base = os.environ.get("HK_PUBLIC_BASE_URL", HK_PUBLIC_BASE_URL).rstrip("/")
    return f"{base}/{object_prefix}/{quote(info.asset_name, safe='')}"


def hk_delta_public_url(info: PackageInfo) -> str:
    return hk_public_url(info, HK_DELTA_OBJECT_PREFIX)


def hk_remote_path(info: PackageInfo, object_prefix: str = HK_OBJECT_PREFIX) -> str:
    root = os.environ.get("HK_REMOTE_ROOT", HK_REMOTE_ROOT).rstrip("/")
    return f"{root}/{object_prefix}/{info.asset_name}"


def build_manifest(
    info: PackageInfo,
    delta_info: PackageInfo | None = None,
    delta_from: str | None = None,
) -> dict:
    manifest = {
        "version": info.version,
        "download_url": hk_public_url(info),
        "download_url_backup": [
            r2_public_url(info),
            PROXY_PREFIX + info.github_url,
            *[prefix + info.github_url for prefix in BACKUP_PREFIXES],
        ],
        "size": info.size,
        "sha256": info.sha256,
        "release_tag": info.tag,
    }
    if delta_info is not None:
        if not isinstance(delta_from, str) or not delta_from.strip():
            raise ReleaseError("发布增量包时缺少 delta_from")
        manifest["delta_from"] = delta_from.strip()
        manifest["delta_download_url"] = hk_delta_public_url(delta_info)
        manifest["delta_download_url_backup"] = [
            prefix + delta_info.github_url for prefix in STABLE_DELTA_PROXY_PREFIXES
        ]
        manifest["delta_size"] = delta_info.size
        manifest["delta_sha256"] = delta_info.sha256
    return manifest


def _hk_ssh_settings() -> tuple[str, str, Path]:
    host = os.environ.get("HK_DOWNLOAD_HOST", HK_DEFAULT_HOST).strip()
    user = os.environ.get("HK_DOWNLOAD_USER", HK_DEFAULT_USER).strip()
    key = Path(os.environ.get("HK_DOWNLOAD_SSH_KEY", HK_DEFAULT_SSH_KEY)).expanduser()
    if not host or not user:
        raise ReleaseError("香港下载节点 SSH 主机或用户未配置")
    if not key.is_file():
        raise ReleaseError(f"香港下载节点 SSH 私钥不存在: {key}")
    return host, user, key


def sync_to_hk_mirror(
    info: PackageInfo,
    log: Callable[[str], None],
    *,
    object_prefix: str = HK_OBJECT_PREFIX,
) -> None:
    """GitHub Release 发布后，让香港机 wget 到固定目录。"""
    host, user, key = _hk_ssh_settings()
    remote_file = hk_remote_path(info, object_prefix)
    remote_dir = remote_file.rsplit("/", 1)[0]
    tmp_file = remote_file + ".tmp"
    github_url = info.github_url
    proxy_url = PROXY_PREFIX + github_url
    remote_script = " && ".join(
        [
            f"mkdir -p {shlex.quote(remote_dir)}",
            f"rm -f {shlex.quote(tmp_file)}",
            (
                f"(wget -c --timeout=30 --tries=3 -O {shlex.quote(tmp_file)} {shlex.quote(github_url)} "
                f"|| wget -c --timeout=30 --tries=3 -O {shlex.quote(tmp_file)} {shlex.quote(proxy_url)})"
            ),
            f"test \"$(stat -c%s {shlex.quote(tmp_file)})\" = {shlex.quote(str(info.size))}",
            f"mv -f {shlex.quote(tmp_file)} {shlex.quote(remote_file)}",
        ]
    )
    command = [
        "ssh",
        "-i",
        str(key),
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        f"{user}@{host}",
        remote_script,
    ]
    log(f"香港节点开始同步: {hk_public_url(info, object_prefix)}")
    try:
        result = subprocess.run(command, text=True, capture_output=True, check=False, timeout=1200)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReleaseError(f"香港节点同步失败: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[-400:]
        raise ReleaseError(f"香港节点同步失败: {detail or f'exit {result.returncode}'}")
    log(f"香港节点同步完成: {remote_file}")


def _r2_client():
    required = ("R2_ENDPOINT", "R2_BUCKET", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")
    missing = [name for name in required if not os.environ.get(name, "").strip()]
    if missing:
        raise ReleaseError("缺少 R2 环境变量: " + ", ".join(missing))
    try:
        import boto3
    except ImportError as exc:
        raise ReleaseError("缺少 boto3，请在项目 Python 环境安装 boto3") from exc
    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"].strip().rstrip("/"),
        region_name="auto",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"].strip(),
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"].strip(),
    )


def upload_to_r2(info: PackageInfo, log: Callable[[str], None], client=None) -> dict:
    try:
        from botocore.exceptions import ClientError
    except ImportError as exc:
        raise ReleaseError("缺少 boto3，请在项目 Python 环境安装 boto3") from exc
    bucket = os.environ.get("R2_BUCKET", "").strip()
    if not bucket:
        raise ReleaseError("缺少 R2_BUCKET 环境变量")
    client = client or _r2_client()
    key = r2_object_key(info)
    try:
        existing = client.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code not in {"404", "NoSuchKey", "NotFound"}:
            raise ReleaseError(f"检查 R2 对象失败: {code}") from exc
    else:
        if int(existing.get("ContentLength", -1)) != info.size:
            raise ReleaseError(f"R2 已存在同名但大小不同的对象: {key}")
        log(f"R2 对象已存在，大小匹配，保留不覆盖: {key}")
        return existing
    try:
        client.upload_file(
            str(info.path),
            bucket,
            key,
            ExtraArgs={"ContentType": "application/zip"},
        )
        uploaded = client.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        detail = exc.response.get("Error", {})
        raise ReleaseError(f"上传 R2 失败: {detail.get('Code', 'unknown')} {detail.get('Message', '')}") from exc
    if int(uploaded.get("ContentLength", -1)) != info.size:
        raise ReleaseError(f"R2 上传后大小不匹配: {key}")
    log(f"R2 上传完成: {key} ({info.size} bytes)")
    return uploaded


def _credential_token() -> str:
    request = "protocol=https\\nhost=github.com\\n\\n"
    try:
        result = subprocess.run(["git", "credential", "fill"], input=request, text=True, capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ReleaseError("无法读取 GitHub 凭据，请先登录 Git Credential Manager") from exc
    values = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    token = values.get("password", "")
    if not token:
        raise ReleaseError("GitHub 凭据中没有可用 token")
    return token


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}


def _raise_http(response: requests.Response, action: str) -> None:
    if response.ok:
        return
    try:
        detail = response.json().get("message", "")
    except ValueError:
        detail = response.text[:200]
    raise ReleaseError(f"{action}失败: HTTP {response.status_code} {detail}")


def _get_release(token: str, tag: str) -> dict | None:
    response = requests.get(f"{API_BASE}/repos/{OWNER}/{REPOSITORY}/releases/tags/{tag}", headers=_headers(token), timeout=30)
    if response.status_code == 404:
        return None
    _raise_http(response, "查询 Release")
    return _safe_json_response(response)


def _get_tag(token: str, tag: str) -> bool:
    response = requests.get(f"{API_BASE}/repos/{OWNER}/{REPOSITORY}/git/ref/tags/{tag}", headers=_headers(token), timeout=30)
    if response.status_code == 404:
        return False
    _raise_http(response, "查询 Git tag")
    return True


def _upload_github_asset(info: PackageInfo, upload_url: str, headers: dict[str, str], log: Callable[[str], None]) -> dict:
    with info.path.open("rb") as stream:
        response = requests.post(
            f"{upload_url}?name={quote(info.asset_name)}",
            headers={**headers, "Content-Type": "application/zip"},
            data=stream,
            timeout=(30, 900),
        )
    _raise_http(response, f"上传 ZIP 资产 {info.asset_name}")
    asset = _safe_json_response(response)
    if asset.get("state") != "uploaded" or int(asset.get("size", -1)) != info.size:
        raise ReleaseError(f"GitHub 资产状态或大小校验失败: {info.asset_name}")
    log(f"ZIP 已上传: {asset.get('name')} ({asset.get('size')} bytes)")
    return asset


def publish_release(
    info: PackageInfo,
    log: Callable[[str], None],
    delta_info: PackageInfo | None = None,
) -> dict:
    _enable_system_tls()
    token = _credential_token()
    release = _get_release(token, info.tag)
    if release is not None or _get_tag(token, info.tag):
        raise ReleaseError(f"Release 或 Git tag 已存在: {info.tag}，为避免覆盖已停止")
    headers = _headers(token)
    body = {
        "tag_name": info.tag,
        "target_commitish": BRANCH,
        "name": f"Skin {info.version}",
        "body": (
            f"Skin package release {info.version} (full + delta)."
            if delta_info is not None
            else f"Skin package release {info.version}."
        ),
        "draft": False,
        "prerelease": False,
    }
    response = requests.post(f"{API_BASE}/repos/{OWNER}/{REPOSITORY}/releases", headers=headers, json=body, timeout=30)
    _raise_http(response, "创建 Release")
    release = _safe_json_response(response)
    log(f"Release 已创建: {release.get('html_url', info.tag)}")
    upload_url = str(release.get("upload_url", "")).replace("{?name,label}", "")
    if not upload_url:
        raise ReleaseError("GitHub 没有返回资产上传地址")
    asset = _upload_github_asset(info, upload_url, headers, log)
    delta_asset = None
    if delta_info is not None:
        delta_asset = _upload_github_asset(delta_info, upload_url, headers, log)
    return {"release": release, "asset": asset, "delta_asset": delta_asset}


def check_download_range(info: PackageInfo, log: Callable[[str], None], url: str | None = None, label: str = "GitHub 代理") -> None:
    _enable_system_tls()
    started = time.monotonic()
    response = requests.get(url or info.proxy_url, headers={"Range": "bytes=0-1048575", "Accept-Encoding": "identity"}, stream=True, allow_redirects=False, verify=True, timeout=(20, 60))
    try:
        if response.status_code != 206:
            raise ReleaseError(f"{label}测试状态码不是 206，而是 {response.status_code}")
        content_range = response.headers.get("Content-Range", "")
        if not content_range.endswith(f"/{info.size}"):
            raise ReleaseError(f"{label}返回总大小不匹配: {content_range}")
        data = response.raw.read(1024 * 1024)
    finally:
        response.close()
    if data[:4] != b"PK\x03\x04":
        raise ReleaseError(f"{label}返回内容不是 ZIP")
    elapsed = max(time.monotonic() - started, 0.001)
    log(f"{label}测试通过: 206, {len(data)} bytes, {len(data) / 1024 / 1024 / elapsed:.2f} MB/s")


def _run_git(args: list[str], action: str) -> str:
    try:
        result = subprocess.run(["git", *args], cwd=ROOT, text=True, capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        raise ReleaseError(f"{action}失败: {detail.strip()[-400:]}") from exc
    return result.stdout.strip()


def write_and_push_manifest(
    info: PackageInfo,
    log: Callable[[str], None],
    delta_info: PackageInfo | None = None,
    delta_from: str | None = None,
) -> str:
    manifest = build_manifest(info, delta_info=delta_info, delta_from=delta_from)
    if validate_skin_manifest is not None:
        validate_skin_manifest(manifest)
    MANIFEST_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    staged_before = _run_git(["diff", "--cached", "--name-only"], "检查 Git 暂存区")
    if staged_before:
        raise ReleaseError("Git 暂存区已有其他变更，未自动处理")
    _run_git(["add", "--", "skin-manifest.json"], "暂存 manifest")
    staged = _run_git(["diff", "--cached", "--name-only"], "检查 manifest 暂存区")
    if staged != "skin-manifest.json":
        _run_git(["reset"], "清理错误暂存")
        raise ReleaseError("暂存区不是只有 skin-manifest.json")
    _run_git(["diff", "--cached", "--check"], "检查 manifest 格式")
    _run_git(["commit", "-m", f"chore: publish skin package {info.version} manifest"], "提交 manifest")
    _run_git(["push", "origin", BRANCH], "推送 manifest")
    log("manifest 已推送到 main")
    return _run_git(["rev-parse", "HEAD"], "读取提交哈希")


def verify_remote(
    info: PackageInfo,
    log: Callable[[str], None],
    delta_info: PackageInfo | None = None,
    delta_from: str | None = None,
) -> None:
    response = requests.get(RAW_MANIFEST_URL + f"?version={info.version}", timeout=30)
    _raise_http(response, "读取远程 manifest")
    remote = _safe_json_response(response)
    expected = build_manifest(info, delta_info=delta_info, delta_from=delta_from)
    keys = ["version", "download_url", "size", "sha256", "release_tag"]
    if delta_info is not None:
        keys.extend(
            [
                "delta_from",
                "delta_download_url",
                "delta_download_url_backup",
                "delta_size",
                "delta_sha256",
            ]
        )
    for key in keys:
        if remote.get(key) != expected[key]:
            raise ReleaseError(f"远程 manifest 字段不匹配: {key}")
    log("远程 manifest 验证通过")


def publish(
    zip_path: Path,
    log: Callable[[str], None],
    previous_zip: Path | None = None,
) -> dict:
    info = inspect_package(zip_path)
    log(f"ZIP 校验通过: {info.asset_name}")
    log(f"版本: {info.version}，大小: {info.size} bytes")
    log(f"SHA-256: {info.sha256}")

    delta_info: PackageInfo | None = None
    delta_from: str | None = None
    holder: tempfile.TemporaryDirectory[str] | None = None
    try:
        if previous_zip is not None:
            holder = tempfile.TemporaryDirectory(prefix="skin-delta-build-")
            delta_info, delta_from = prepare_delta_package(info, previous_zip, Path(holder.name), log)
            if delta_info is None:
                log("本次仅发布全量包（无 delta_* 字段）")
            else:
                log(f"增量包就绪: {delta_info.asset_name}，delta_from={delta_from}")

        upload_to_r2(info, log)
        check_download_range(info, log, url=r2_public_url(info), label="R2")
        publish_release(info, log, delta_info=delta_info)
        check_download_range(info, log)
        if delta_info is not None:
            check_download_range(delta_info, log, url=delta_info.proxy_url, label="GitHub 增量代理")
        sync_to_hk_mirror(info, log, object_prefix=HK_OBJECT_PREFIX)
        check_download_range(info, log, url=hk_public_url(info), label="香港节点")
        if delta_info is not None:
            sync_to_hk_mirror(delta_info, log, object_prefix=HK_DELTA_OBJECT_PREFIX)
            check_download_range(
                delta_info,
                log,
                url=hk_delta_public_url(delta_info),
                label="香港增量节点",
            )
        commit = write_and_push_manifest(info, log, delta_info=delta_info, delta_from=delta_from)
        verify_remote(info, log, delta_info=delta_info, delta_from=delta_from)
        return {"info": info, "delta_info": delta_info, "delta_from": delta_from, "commit": commit}
    finally:
        if holder is not None:
            holder.cleanup()


class SkinReleaseApp(Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Chosen 皮肤包发布")
        self.geometry("760x560")
        self.minsize(620, 440)
        self.path_var = StringVar()
        self.previous_path_var = StringVar()
        self.status_var = StringVar(value="请选择皮肤 ZIP")
        self.events: queue.Queue[tuple[str, str]] = queue.Queue()
        self.running = False
        self._build_ui()
        self.after(100, self._drain_events)

    def _build_ui(self) -> None:
        top = Frame(self)
        top.pack(fill=X, padx=12, pady=12)
        Label(top, text="皮肤 ZIP：").pack(side=LEFT)
        Entry(top, textvariable=self.path_var).pack(side=LEFT, fill=X, expand=True, padx=8)
        Button(top, text="选择 ZIP", command=self.choose_zip).pack(side=RIGHT)

        previous = Frame(self)
        previous.pack(fill=X, padx=12, pady=(0, 8))
        Label(previous, text="上一版全量 ZIP（可选）：").pack(side=LEFT)
        Entry(previous, textvariable=self.previous_path_var).pack(side=LEFT, fill=X, expand=True, padx=8)
        Button(previous, text="选择上一版", command=self.choose_previous_zip).pack(side=RIGHT)

        action = Frame(self)
        action.pack(fill=X, padx=12)
        self.start_button = Button(action, text="开始发布", command=self.start_publish)
        self.start_button.pack(side=LEFT)
        Label(action, textvariable=self.status_var, anchor="w").pack(side=LEFT, fill=X, expand=True, padx=12)
        self.output = ScrolledText(self, state="disabled", wrap="word")
        self.output.pack(fill=BOTH, expand=True, padx=12, pady=(8, 12))

    def choose_zip(self) -> None:
        path = filedialog.askopenfilename(title="选择皮肤 ZIP", filetypes=[("ZIP 文件", "*.zip"), ("所有文件", "*.*")])
        if not path:
            return
        self.path_var.set(path)
        try:
            info = inspect_package(Path(path))
            self.status_var.set(f"版本 {info.version}，{info.size:,} bytes，校验通过")
            self._log(f"已选择: {info.path}")
            self._log(f"版本 {info.version}，SHA-256 {info.sha256}")
        except Exception as exc:
            self.status_var.set("ZIP 校验失败")
            self._log(f"错误: {exc}")
            messagebox.showerror("ZIP 不可用", str(exc))

    def choose_previous_zip(self) -> None:
        path = filedialog.askopenfilename(
            title="选择上一版全量皮肤 ZIP（可选）",
            filetypes=[("ZIP 文件", "*.zip"), ("所有文件", "*.*")],
        )
        if not path:
            return
        self.previous_path_var.set(path)
        try:
            info = inspect_package(Path(path))
            self._log(f"已选择上一版: {info.path}（版本 {info.version}）")
            self.status_var.set(f"上一版 {info.version} 校验通过；未选上一版则仅发全量")
        except Exception as exc:
            self.previous_path_var.set("")
            self._log(f"上一版 ZIP 错误: {exc}")
            messagebox.showerror("上一版 ZIP 不可用", str(exc))

    def start_publish(self) -> None:
        if self.running:
            return
        path = self.path_var.get().strip()
        if not path:
            messagebox.showwarning("缺少文件", "请先选择 ZIP 文件")
            return
        previous_raw = self.previous_path_var.get().strip()
        previous_zip = Path(previous_raw) if previous_raw else None
        self.running = True
        self.start_button.configure(state="disabled")
        self.status_var.set("发布中，请勿关闭窗口")
        threading.Thread(target=self._worker, args=(Path(path), previous_zip), daemon=True).start()

    def _worker(self, path: Path, previous_zip: Path | None) -> None:
        try:
            result = publish(path, self._queue_log, previous_zip=previous_zip)
            info = result["info"]
            delta_info = result.get("delta_info")
            extra = f"\n增量: {delta_info.asset_name}" if delta_info is not None else "\n增量: 无"
            self.events.put(("done", f"发布完成：{info.tag}{extra}\n提交：{result['commit']}"))
        except Exception as exc:
            self.events.put(("error", str(exc)))

    def _queue_log(self, message: str) -> None:
        self.events.put(("log", message))

    def _log(self, message: str) -> None:
        self.output.configure(state="normal")
        self.output.insert(END, message + "\n")
        self.output.see(END)
        self.output.configure(state="disabled")

    def _drain_events(self) -> None:
        try:
            while True:
                kind, message = self.events.get_nowait()
                self._log(message)
                if kind == "done":
                    self.running = False
                    self.start_button.configure(state="normal")
                    self.status_var.set("发布完成")
                    messagebox.showinfo("发布完成", message)
                elif kind == "error":
                    self.running = False
                    self.start_button.configure(state="normal")
                    self.status_var.set("发布失败，manifest 未必已推送")
                    messagebox.showerror("发布失败", message)
        except queue.Empty:
            pass
        self.after(100, self._drain_events)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        SkinReleaseApp().mainloop()
        return 0

    parser = argparse.ArgumentParser(description="Publish Chosen skin full package and optional delta")
    parser.add_argument("full_zip", type=Path, help="全量皮肤 ZIP")
    parser.add_argument("--previous-zip", type=Path, default=None, help="上一版全量 ZIP；省略则不发 delta")
    args = parser.parse_args(argv)

    def log(message: str) -> None:
        print(message, flush=True)

    result = publish(args.full_zip, log, previous_zip=args.previous_zip)
    info = result["info"]
    delta_info = result.get("delta_info")
    print(f"发布完成: {info.tag}")
    print(f"全量: {info.asset_name}")
    if delta_info is not None:
        print(f"增量: {delta_info.asset_name} (from {result.get('delta_from')})")
    else:
        print("增量: 无")
    print(f"manifest commit: {result['commit']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
