#!/usr/bin/env python3
"""Publish and consume an integrity-checked static-site manifest.

The dashboard's source repository is private, but its GitHub Pages output is
public.  A second Pages repository therefore cannot clone ``gh-pages`` with its
own ``GITHUB_TOKEN``; it mirrors the public files listed by
``site-manifest.json`` instead.

The source deployment runs ``manifest`` after it has finished updating the
site.  The public repository downloads this script from the source site, runs
``sync`` into a clean directory, and deploys that directory as a Pages
artifact.  Every byte is checked against the manifest before deployment, stale
files disappear because the destination is rebuilt from scratch, and no
cross-repository personal access token is kept in either repository.

Usage:
    python dashboard/site_mirror.py manifest --site-dir site --publish-script
    python dashboard/site_mirror.py verify --site-dir site
    python site_mirror.py sync \
        --source-url https://rocm.github.io/sglang-ci/ \
        --output _site
"""

import argparse
import concurrent.futures
import hashlib
import json
import re
import shutil
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Optional

MANIFEST_NAME = "site-manifest.json"
PUBLISHED_SCRIPT = "mirror/site_mirror.py"
SCHEMA_VERSION = 1
USER_AGENT = "sglang-ci-dashboard-mirror/1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative_path(raw: str) -> PurePosixPath:
    """Validate an untrusted manifest path before joining it to a directory."""
    if not isinstance(raw, str) or not raw or "\\" in raw:
        raise ValueError(f"invalid manifest path: {raw!r}")
    components = raw.split("/")
    if any(part in ("", ".", "..") for part in components):
        raise ValueError(f"unsafe manifest path: {raw!r}")
    path = PurePosixPath(raw)
    if path.is_absolute() or ".git" in path.parts:
        raise ValueError(f"unsafe manifest path: {raw!r}")
    return path


def _manifest_payload(raw: bytes) -> dict:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid {MANIFEST_NAME}: {error}") from error

    if not isinstance(payload, dict):
        raise ValueError(f"{MANIFEST_NAME} must contain a JSON object")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(
            f"unsupported manifest schema: {payload.get('schema_version')!r}"
        )

    entries = payload.get("files")
    if not isinstance(entries, list):
        raise ValueError("manifest 'files' must be a list")

    seen = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("manifest file entries must be objects")
        path = _safe_relative_path(entry.get("path"))
        if str(path) == MANIFEST_NAME:
            raise ValueError(f"{MANIFEST_NAME} cannot contain itself")
        if path in seen:
            raise ValueError(f"duplicate manifest path: {path}")
        seen.add(path)
        if not isinstance(entry.get("size"), int) or entry["size"] < 0:
            raise ValueError(f"invalid size for {path}")
        if not isinstance(entry.get("sha256"), str) or not SHA256_RE.fullmatch(
            entry["sha256"]
        ):
            raise ValueError(f"invalid sha256 for {path}")

    return payload


def _site_files(site_dir: Path):
    # Hidden paths (.git, .nojekyll, the manifest's own temp file) are never
    # listed. actions/upload-pages-artifact@v4 leaves them out of the artifact,
    # so a Pages site published from a workflow answers 404 for them, and a
    # mirror syncing a manifest that names one fails outright. .nojekyll only
    # matters to the legacy branch build, which never reads this manifest.
    for path in sorted(site_dir.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(site_dir)
        if (
            any(part.startswith(".") for part in relative.parts)
            or relative.as_posix() == MANIFEST_NAME
        ):
            continue
        yield path, relative.as_posix()


def write_manifest(
    site_dir: Path,
    publish_script: bool = False,
) -> Path:
    """Write the hash/size manifest for a complete Pages tree."""
    site_dir = site_dir.resolve()
    if not site_dir.is_dir():
        raise ValueError(f"site directory not found: {site_dir}")

    if publish_script:
        script_dest = site_dir / PUBLISHED_SCRIPT
        script_dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(Path(__file__).resolve(), script_dest)

    output = site_dir / MANIFEST_NAME

    entries = []
    for path, relative in _site_files(site_dir):
        entries.append(
            {
                "path": relative,
                "size": path.stat().st_size,
                "sha256": _file_sha256(path),
            }
        )

    generated_at = None
    build_info = site_dir / "api" / "build-info.json"
    if build_info.is_file():
        try:
            generated_at = json.loads(build_info.read_text(encoding="utf-8")).get(
                "generated_at"
            )
        except (json.JSONDecodeError, OSError):
            pass

    payload = {
        "schema_version": SCHEMA_VERSION,
        "source_generated_at": generated_at,
        "files": entries,
    }
    encoded = (
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp")
    temporary.write_bytes(encoded)
    temporary.replace(output)
    print(
        f"Wrote {output} with {len(entries)} files "
        f"({sum(entry['size'] for entry in entries)} bytes)"
    )
    return output


def verify_site(site_dir: Path, manifest_bytes: Optional[bytes] = None) -> dict:
    """Verify that a local tree exactly matches its manifest."""
    site_dir = site_dir.resolve()
    if manifest_bytes is None:
        manifest_bytes = (site_dir / MANIFEST_NAME).read_bytes()
    payload = _manifest_payload(manifest_bytes)

    expected = set()
    for entry in payload["files"]:
        relative = _safe_relative_path(entry["path"])
        expected.add(str(relative))
        path = site_dir.joinpath(*relative.parts)
        if not path.is_file():
            raise ValueError(f"manifest file is missing: {relative}")
        if path.stat().st_size != entry["size"]:
            raise ValueError(f"size mismatch for {relative}")
        if _file_sha256(path) != entry["sha256"]:
            raise ValueError(f"sha256 mismatch for {relative}")

    actual = {relative for _, relative in _site_files(site_dir)}
    extra = sorted(actual - expected)
    missing = sorted(expected - actual)
    if extra or missing:
        raise ValueError(f"site tree mismatch: extra={extra}, missing={missing}")

    print(f"Verified {len(expected)} files in {site_dir}")
    return payload


def _download(url: str, attempts: int = 4) -> bytes:
    last_error = None
    for attempt in range(attempts):
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                if response.status != 200:
                    raise ValueError(f"HTTP {response.status} for {url}")
                return response.read()
        except (OSError, urllib.error.HTTPError, urllib.error.URLError) as error:
            last_error = error
            if attempt + 1 < attempts:
                time.sleep(2**attempt)
    raise ValueError(f"could not download {url}: {last_error}")


def sync_site(source_url: str, output: Path, workers: int = 16) -> dict:
    """Download and verify a complete site into a clean destination."""
    if workers < 1:
        raise ValueError("workers must be at least 1")
    source_url = source_url.rstrip("/") + "/"
    manifest_url = urllib.parse.urljoin(source_url, MANIFEST_NAME)
    manifest_url += f"?mirror={time.time_ns()}"
    manifest_bytes = _download(manifest_url)
    payload = _manifest_payload(manifest_bytes)
    manifest_digest = _sha256(manifest_bytes)[:16]

    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{output.name}-", dir=str(output.parent))
    )

    def fetch(entry):
        relative = _safe_relative_path(entry["path"])
        quoted = urllib.parse.quote(str(relative), safe="/")
        url = urllib.parse.urljoin(source_url, quoted)
        url += f"?manifest={manifest_digest}"
        body = _download(url)
        if len(body) != entry["size"]:
            raise ValueError(
                f"size mismatch for {relative}: "
                f"expected {entry['size']}, downloaded {len(body)}"
            )
        digest = _sha256(body)
        if digest != entry["sha256"]:
            raise ValueError(
                f"sha256 mismatch for {relative}: "
                f"expected {entry['sha256']}, downloaded {digest}"
            )
        destination = temporary.joinpath(*relative.parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(body)

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            list(executor.map(fetch, payload["files"]))

        # Keep the same manifest available on the mirror for parity checks and
        # for another mirror to consume without special-casing this site.
        (temporary / MANIFEST_NAME).write_bytes(manifest_bytes)
        verify_site(temporary, manifest_bytes)

        if output.exists():
            if output.is_dir():
                shutil.rmtree(output)
            else:
                output.unlink()
        temporary.replace(output)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise

    print(f"Mirrored {len(payload['files'])} files from {source_url} to {output}")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Publish or consume the dashboard's static-site manifest"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    manifest_parser = subparsers.add_parser("manifest")
    manifest_parser.add_argument("--site-dir", type=Path, required=True)
    manifest_parser.add_argument("--publish-script", action="store_true")

    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--site-dir", type=Path, required=True)

    sync_parser = subparsers.add_parser("sync")
    sync_parser.add_argument("--source-url", required=True)
    sync_parser.add_argument("--output", type=Path, required=True)
    sync_parser.add_argument("--workers", type=int, default=16)

    args = parser.parse_args()
    if args.command == "manifest":
        write_manifest(args.site_dir, args.publish_script)
        verify_site(args.site_dir)
    elif args.command == "verify":
        verify_site(args.site_dir)
    elif args.command == "sync":
        sync_site(args.source_url, args.output, workers=args.workers)


if __name__ == "__main__":
    main()
