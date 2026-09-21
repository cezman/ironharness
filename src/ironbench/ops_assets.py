"""Asset resolution for ops tasks (IH-104).

An ops asset is either a file shipped next to task.yaml or an external
download pinned by sha256 (the 1.7 MB firmware image stays out of the
wheel; the pin makes the run reproducible). Url assets are cached under
the run's asset cache keyed by the hash, so a warm cache never touches
the network and a cold cache fails loudly on a hash mismatch - a golden
that does not match its pin must stop the attempt, not degrade it.

The download sits behind an SSRF boundary in the same spirit as the LLM
endpoint check in agent.py: https only (file://localhost exists for
offline test fixtures), redirects forbidden, the resolved host must not
be metadata/link-local/multicast/loopback/private - a golden image is
public-internet data, and the URL travels from a YAML file, not from an
audited constant.
"""

from __future__ import annotations

import hashlib
import ipaddress
import socket
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from ironbench.ops_tasks import OpsAsset, OpsTask

DOWNLOAD_TIMEOUT_SEC = 300
# a hostile origin must not grow the process memory past this before the
# pin check - the same cap class as the judge's serial text cap
MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024
_ALWAYS_BLOCKED_IPS = frozenset({ipaddress.ip_address("169.254.169.254")})


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirects disabled", headers, fp)


_OPENER = urllib.request.build_opener(_NoRedirect)


def validate_asset_url(url: str) -> None:
    """SSRF boundary: https (or file://localhost for test fixtures), no
    redirect-following beyond this check, resolved IPs restricted."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme == "file":
        if parts.netloc not in ("", "localhost") or not parts.path.startswith("/"):
            raise ValueError(f"asset url must be file://localhost/abs/path, got {url!r}")
        return
    if parts.scheme != "https":
        raise ValueError(f"asset url must be https, got {url!r}")
    host = (parts.hostname or "").rstrip(".")
    if not host:
        raise ValueError(f"asset url has no host: {url!r}")
    if host.lower() in ("metadata.google.internal", "metadata"):
        raise ValueError(f"blocked metadata host: {host}")
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as e:
        raise ValueError(f"asset url host does not resolve: {host}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip in _ALWAYS_BLOCKED_IPS or ip.is_link_local or ip.is_multicast:
            raise ValueError(f"blocked address {host} -> {ip}")
        if ip.is_private or ip.is_loopback or ip.is_unspecified:
            raise ValueError(f"private/loopback address {host} -> {ip} is forbidden for assets")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def resolve_asset(asset: OpsAsset, *, task_dir: Path, cache_dir: Path) -> Path:
    """Returns a local path to the asset bytes, downloading when needed."""
    if asset.path is not None:
        path = task_dir / asset.path
        if not path.is_file():
            raise FileNotFoundError(f"asset {asset.name!r} file is missing: {path}")
        return path
    assert asset.url is not None and asset.sha256 is not None  # loader guarantees
    validate_asset_url(asset.url)
    cache_dir = Path(cache_dir)
    target = cache_dir / f"{asset.name}-{asset.sha256[:16]}"
    if target.is_file():
        # a warm cache is only trusted when it really matches the pin
        if _sha256(target.read_bytes()) != asset.sha256:
            raise ValueError(
                f"asset {asset.name!r}: cached file {target} does not match its sha256 pin"
            )
        return target
    cache_dir.mkdir(parents=True, exist_ok=True)
    # unique per target: two assets downloading concurrently must not share
    # one partial file
    partial = cache_dir / (target.name + ".part")
    if asset.url.startswith("file://"):
        data = Path(urllib.request.url2pathname(urllib.parse.urlsplit(asset.url).path)).read_bytes()
    else:
        with _OPENER.open(asset.url, timeout=DOWNLOAD_TIMEOUT_SEC) as resp:
            data = resp.read(MAX_DOWNLOAD_BYTES + 1)
        if len(data) > MAX_DOWNLOAD_BYTES:
            raise ValueError(f"asset {asset.name!r}: download exceeds {MAX_DOWNLOAD_BYTES} bytes")
    actual = _sha256(data)
    if actual != asset.sha256:
        raise ValueError(
            f"asset {asset.name!r}: downloaded {asset.url} has sha256 {actual}, "
            f"expected {asset.sha256} - refusing to flash a non-golden image"
        )
    partial.write_bytes(data)
    partial.replace(target)
    return target


def resolve_assets(task: OpsTask, *, task_dir: Path, cache_dir: Path) -> dict[str, Path]:
    return {
        asset.name: resolve_asset(asset, task_dir=task_dir, cache_dir=cache_dir)
        for asset in task.assets
    }
