"""Acquire and verify an official stable 3x-ui release before stopping services."""
from __future__ import annotations

import hashlib
import gzip
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request


RELEASE_API = "https://api.github.com/repos/MHSanaei/3x-ui/releases/latest"
DOWNLOAD_PREFIX = "https://github.com/MHSanaei/3x-ui/releases/download/"
MAX_METADATA = 4 * 1024 * 1024
MAX_ARCHIVE = 512 * 1024 * 1024
MAX_EXPANDED = 2 * 1024 * 1024 * 1024
MAX_MEMBERS = 4096
MAX_TAR_READ = 8 * 1024 * 1024
STAGING_RESERVE = 32 * 1024 * 1024
NETWORK_TIMEOUT = 20
DOWNLOAD_TIMEOUT = 300
VERSION_TIMEOUT = 15
ARCHITECTURES = {"amd64", "386", "arm64", "armv5", "armv6", "armv7", "s390x"}


class ReleaseError(RuntimeError):
    """Fixed identifiers; never include downloaded output or local secrets."""


def normalize_version(value):
    if not isinstance(value, str):
        raise ReleaseError("invalid_stable_version")
    match = re.fullmatch(r"v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", value.strip())
    if not match:
        raise ReleaseError("invalid_stable_version")
    return ".".join(match.groups())


def version_action(current, target):
    current_parts = tuple(map(int, normalize_version(current).split(".")))
    target_parts = tuple(map(int, normalize_version(target).split(".")))
    return "upgrade" if target_parts > current_parts else "noop" if target_parts == current_parts else "newer"


def architecture_name(value=None):
    value = platform.machine() if value is None else value
    if not isinstance(value, str):
        raise ReleaseError("unsupported_panel_architecture")
    value = value.lower()
    if value in ("x86_64", "x64", "amd64"):
        return "amd64"
    if value in ("x86", "386") or re.fullmatch(r"i[3-6]86", value):
        return "386"
    if value in ("arm64", "aarch64") or value.startswith("armv8"):
        return "arm64"
    if value == "arm" or value.startswith("armv7"):
        return "armv7"
    for name in ("armv5", "armv6"):
        if value.startswith(name):
            return name
    if value == "s390x":
        return value
    raise ReleaseError("unsupported_panel_architecture")


def installed_version(binary):
    """The official read-only CLI is x-ui -v, not x-ui version."""
    binary = Path(binary).absolute()
    if not binary.is_file() or binary.is_symlink():
        raise ReleaseError("panel_binary_missing")
    try:
        # Bound captured output even when an executable exits unexpectedly.
        with tempfile.TemporaryFile() as output:
            result = subprocess.run([str(binary), "-v"], cwd=str(binary.parent),
                stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                timeout=VERSION_TIMEOUT, check=False)
            output.seek(0)
            value = output.read(8193)
        if result.returncode or len(value) > 8192:
            raise ReleaseError("panel_version_check_failed")
        return normalize_version(value.decode("utf-8").strip())
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        raise ReleaseError("panel_version_check_failed") from None


read_binary_version = installed_version


def _network_url(url, redirect=False):
    try:
        parts = urllib.parse.urlsplit(url)
        if (parts.scheme != "https" or parts.username is not None or parts.password is not None
                or parts.port not in (None, 443) or parts.fragment):
            raise ValueError
        if redirect:
            if parts.hostname not in {"github.com", "api.github.com", "release-assets.githubusercontent.com",
                                      "objects.githubusercontent.com"}:
                raise ValueError
        elif url != RELEASE_API and not re.fullmatch(
                re.escape(DOWNLOAD_PREFIX) + r"v?[0-9]+\.[0-9]+\.[0-9]+/x-ui-linux-(?:amd64|386|arm64|armv5|armv6|armv7|s390x)\.tar\.gz(?:\.sha256)?", url):
            raise ValueError
    except (TypeError, ValueError):
        raise ReleaseError("untrusted_release_url") from None
    return url


class _OfficialRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        _network_url(newurl, redirect=True)
        return super().redirect_request(request, fp, code, message, headers, newurl)


def _open_url(url):
    _network_url(url)
    request = urllib.request.Request(url, headers={"User-Agent": "3xuish-panel-upgrade",
                                                   "Accept": "application/vnd.github+json" if url == RELEASE_API else "application/octet-stream"})
    return urllib.request.build_opener(_OfficialRedirects()).open(request, timeout=NETWORK_TIMEOUT)


def _read_url(url, limit):
    try:
        deadline = time.monotonic() + 60
        chunks = []
        total = 0
        with _open_url(url) as response:
            read = getattr(response, "read1", response.read)
            while True:
                if time.monotonic() > deadline:
                    raise ReleaseError("release_download_timed_out")
                chunk = read(min(65536, limit + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > limit:
                    raise ReleaseError("release_response_too_large")
        return b"".join(chunks)
    except (OSError, urllib.error.URLError):
        raise ReleaseError("release_download_failed") from None


def _asset(asset, expected_name, tag, maximum):
    if not isinstance(asset, dict) or asset.get("name") != expected_name:
        raise ReleaseError("release_asset_invalid")
    expected_url = DOWNLOAD_PREFIX + tag + "/" + expected_name
    if asset.get("browser_download_url") != expected_url:
        raise ReleaseError("untrusted_release_url")
    _network_url(expected_url)
    size = asset.get("size")
    if type(size) is not int or not 0 < size <= maximum:
        raise ReleaseError("release_asset_size_invalid")
    digest = asset.get("digest")
    sha256 = None
    if digest:
        if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest):
            raise ReleaseError("release_digest_invalid")
        sha256 = digest[7:].lower()
    return {"name": expected_name, "url": expected_url, "size": size, "sha256": sha256}


def select_release(metadata, architecture=None):
    arch = architecture_name(architecture)
    if (not isinstance(metadata, dict) or metadata.get("draft") is not False
            or metadata.get("prerelease") is not False):
        raise ReleaseError("release_is_not_stable")
    tag = metadata.get("tag_name")
    version = normalize_version(tag)
    # No surrounding whitespace can become part of a release asset URL.
    if tag != tag.strip():
        raise ReleaseError("invalid_stable_version")
    assets = metadata.get("assets")
    if not isinstance(assets, list):
        raise ReleaseError("release_assets_missing")
    name = f"x-ui-linux-{arch}.tar.gz"
    candidates = [item for item in assets if isinstance(item, dict) and item.get("name") == name]
    if len(candidates) != 1:
        raise ReleaseError("release_asset_missing_or_ambiguous")
    asset = _asset(candidates[0], name, tag, MAX_ARCHIVE)
    result = {"version": version, "tag": tag, "architecture": arch, "asset": asset}
    if asset["sha256"] is None:
        checksums = [item for item in assets if isinstance(item, dict) and item.get("name") == name + ".sha256"]
        if len(checksums) != 1:
            raise ReleaseError("release_checksum_missing")
        result["checksum_asset"] = _asset(checksums[0], name + ".sha256", tag, 65536)
    return result


def latest_release(architecture=None):
    try:
        metadata = json.loads(_read_url(RELEASE_API, MAX_METADATA))
    except (ValueError, UnicodeError):
        raise ReleaseError("release_metadata_invalid") from None
    return select_release(metadata, architecture)


def _checksum(release):
    asset = release["asset"]
    if asset["sha256"]:
        return asset["sha256"]
    checksum = release["checksum_asset"]
    checksum = _asset({"name": checksum["name"], "size": checksum["size"],
                       "browser_download_url": checksum["url"],
                       "digest": "sha256:" + checksum["sha256"] if checksum["sha256"] else None},
                      asset["name"] + ".sha256", release["tag"], 65536)
    data = _read_url(checksum["url"], 65536)
    if len(data) != checksum["size"]:
        raise ReleaseError("release_checksum_size_mismatch")
    if checksum["sha256"] and hashlib.sha256(data).hexdigest() != checksum["sha256"]:
        raise ReleaseError("release_checksum_digest_mismatch")
    try:
        line = data.decode("ascii").strip()
    except UnicodeError:
        raise ReleaseError("release_checksum_invalid") from None
    match = re.fullmatch(r"([0-9a-fA-F]{64})[ \t]+\*?" + re.escape(asset["name"]), line)
    if not match:
        raise ReleaseError("release_checksum_invalid")
    return match.group(1).lower()


def _private_directory(path):
    path = Path(path).absolute()
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ReleaseError("symlinked_release_stage")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.is_dir():
        raise ReleaseError("invalid_release_stage")
    os.chmod(path, 0o700)
    return path


def _require_staging_space(path, size):
    try:
        free = shutil.disk_usage(path).free
    except OSError:
        raise ReleaseError("release_space_check_failed") from None
    if free < size + STAGING_RESERVE:
        raise ReleaseError("insufficient_release_space")


def download_asset(asset, destination, expected_digest):
    """Never leave an unverified archive at its final staging name."""
    destination = Path(destination)
    partial = destination.with_name(destination.name + ".part")
    if destination.exists() or destination.is_symlink() or partial.exists() or partial.is_symlink():
        raise ReleaseError("release_stage_not_empty")
    # Staging happens while the old panel is still serving requests. Leave
    # headroom for its database and logs before beginning any download.
    _require_staging_space(destination.parent, asset["size"])
    total = 0
    digest = hashlib.sha256()
    deadline = time.monotonic() + DOWNLOAD_TIMEOUT
    try:
        fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as target, _open_url(asset["url"]) as source:
            read = getattr(source, "read1", source.read)
            while True:
                if time.monotonic() > deadline:
                    raise ReleaseError("release_download_timed_out")
                chunk = read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > asset["size"] or total > MAX_ARCHIVE:
                    raise ReleaseError("release_archive_size_mismatch")
                target.write(chunk)
                digest.update(chunk)
            target.flush()
            os.fsync(target.fileno())
        if total != asset["size"]:
            raise ReleaseError("release_archive_size_mismatch")
        if digest.hexdigest() != expected_digest:
            raise ReleaseError("release_archive_digest_mismatch")
        os.replace(partial, destination)
    except (OSError, urllib.error.URLError):
        raise ReleaseError("release_download_failed") from None
    finally:
        if partial.exists():
            partial.unlink()
    return destination


class _BoundedTarReader:
    """Bound gzip expansion and oversized tar extended headers before parsing."""
    def __init__(self, source):
        self.source = source

    def tell(self):
        return self.source.tell()

    def read(self, size):
        if size < 0 or size > MAX_TAR_READ or self.tell() + size > MAX_EXPANDED:
            raise ReleaseError("release_archive_expands_too_large")
        return self.source.read(size)

    def seek(self, offset, whence=0):
        if whence == 1:
            offset += self.tell()
        elif whence != 0:
            raise ReleaseError("release_archive_read_failed")
        if not 0 <= offset <= MAX_EXPANDED:
            raise ReleaseError("release_archive_expands_too_large")
        return self.source.seek(offset)


def extract_archive(archive, destination):
    """Extract only regular files/directories below x-ui; ignore archive modes."""
    destination = _private_directory(destination)
    if any(destination.iterdir()):
        raise ReleaseError("release_stage_not_empty")
    seen = set()
    total = 0
    try:
        with open(archive, "rb") as compressed, gzip.GzipFile(fileobj=compressed) as expanded, \
                tarfile.open(fileobj=_BoundedTarReader(expanded), mode="r:") as package:
            # Validate the whole archive before writing any member.
            members = []
            for member in package:
                if len(members) >= MAX_MEMBERS:
                    raise ReleaseError("release_archive_too_many_files")
                name = member.name
                raw_parts = name.rstrip("/").split("/")
                path = PurePosixPath(name)
                if (not name or len(name) > 2048 or "\\" in name or ":" in name
                        or any(ord(char) < 32 or ord(char) == 127 for char in name)
                        or path.is_absolute() or any(part in ("", ".", "..") for part in raw_parts)
                        or raw_parts[0] != "x-ui" or (len(raw_parts) == 1 and not member.isdir())):
                    raise ReleaseError("unsafe_release_archive_path")
                normalized = "/".join(raw_parts)
                if normalized in seen:
                    raise ReleaseError("duplicate_release_archive_member")
                seen.add(normalized)
                if not (member.isfile() or member.isdir()) or member.issparse():
                    raise ReleaseError("unsafe_release_archive_type")
                if member.size < 0:
                    raise ReleaseError("release_archive_size_invalid")
                total += member.size
                if total > MAX_EXPANDED:
                    raise ReleaseError("release_archive_expands_too_large")
                members.append((member, raw_parts))
            # The downloaded archive already consumes disk space. Reserve the
            # full expanded payload against the current free space before any
            # archive member is written.
            _require_staging_space(destination, total)
            for member, parts in members:
                target = destination.joinpath(*parts)
                if member.isdir():
                    target.mkdir(mode=0o700, parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                stream = package.extractfile(member)
                if stream is None:
                    raise ReleaseError("release_archive_read_failed")
                with stream, target.open("xb") as output:
                    shutil.copyfileobj(stream, output, length=1024 * 1024)
                os.chmod(target, 0o600)
    except (OSError, tarfile.TarError, EOFError):
        raise ReleaseError("release_archive_read_failed") from None
    binary = destination / "x-ui/x-ui"
    if not binary.is_file() or not binary.stat().st_size:
        raise ReleaseError("release_panel_binary_missing")
    with binary.open("rb") as handle:
        if handle.read(4) != b"\x7fELF":
            raise ReleaseError("release_panel_binary_invalid")
    os.chmod(binary, 0o700)
    cli = destination / "x-ui/x-ui.sh"
    if cli.exists():
        if not cli.is_file() or not cli.stat().st_size:
            raise ReleaseError("release_cli_invalid")
        os.chmod(cli, 0o700)
        try:
            result = subprocess.run(["bash", "-n", str(cli)], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise ReleaseError("release_cli_invalid") from None
        if result.returncode:
            raise ReleaseError("release_cli_invalid")
    return destination / "x-ui"


def prepare_release(release, stage):
    """Download, verify, unpack and check the candidate; never touch live paths."""
    stage = _private_directory(stage)
    asset = release["asset"]
    # Revalidate even cached/serialized plans before obtaining executable bytes.
    metadata_asset = {"name": asset["name"], "size": asset["size"], "browser_download_url": asset["url"],
                      "digest": "sha256:" + asset["sha256"] if asset["sha256"] else None}
    validated = _asset(metadata_asset, f"x-ui-linux-{architecture_name(release['architecture'])}.tar.gz",
                       release["tag"], MAX_ARCHIVE)
    if normalize_version(release["tag"]) != normalize_version(release["version"]):
        raise ReleaseError("release_version_mismatch")
    digest = _checksum(release)
    archive = download_asset(validated, stage / validated["name"], digest)
    package_dir = extract_archive(archive, stage / "extracted")
    binary = package_dir / "x-ui"
    if installed_version(binary) != normalize_version(release["version"]):
        raise ReleaseError("release_binary_version_mismatch")
    cli = package_dir / "x-ui.sh"
    return {"version": normalize_version(release["version"]), "tag": release["tag"],
            "architecture": release["architecture"], "binary": str(binary),
            "cli": str(cli) if cli.is_file() else None, "package_dir": str(package_dir),
            "archive": str(archive), "sha256": digest, "asset_url": asset["url"]}
