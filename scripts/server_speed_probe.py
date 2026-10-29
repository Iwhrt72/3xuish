#!/usr/bin/env python3
"""Bounded curl download probes; never change services or save downloaded data."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import math
import os
import re
import subprocess
import time
from urllib.parse import urlsplit


DEFAULT_URL = "https://lil1-rbx.speedtest.network.ovh.net/files/100Mb.dat"
MIN_SAMPLE_BYTES = 64 * 1024
MAX_SAMPLE_BYTES = 100 * 1024 * 1024
MIME_LABELS = {"application/octet-stream", "binary/octet-stream", "application/x-binary",
               "application/zip", "application/x-download", "text/html", "text/plain",
               "application/json", "application/xml", "text/xml"}


def validate_target(url, allow_http=False):
    try:
        parsed = urlsplit(url)
        allowed = {"https", "http"} if allow_http else {"https"}
        if (parsed.scheme not in allowed or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.fragment or "\r" in url or "\n" in url):
            raise ValueError()
        parsed.port
    except (TypeError, ValueError):
        raise ValueError("invalid_download_target") from None


def validate_proxy(proxy):
    if proxy is None:
        return
    try:
        parsed = urlsplit(proxy)
        if (parsed.scheme != "socks5h" or parsed.hostname != "127.0.0.1"
                or parsed.port is None or not 1 <= parsed.port <= 65535
                or parsed.username is not None or parsed.password is not None
                or parsed.path or parsed.query or parsed.fragment):
            raise ValueError()
    except (TypeError, ValueError):
        raise ValueError("invalid_loopback_proxy") from None


def _label(label):
    if not isinstance(label, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", label):
        raise ValueError("invalid_probe_label")
    return label


def _bounded_integer(value, minimum, maximum, code):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(code)


def _failed(code, **fields):
    return {"status": "failed", "http_code": 0, "bytes": 0, "elapsed_seconds": 0,
            "curl_exit": None, "content_type": "unknown", "error": code, **fields}


def _sample(url, seconds, curl, proxy, allow_http):
    protocols = "=http,https" if allow_http else "=https"
    args = [str(curl), "-q", "--silent", "--show-error", "--location", "--max-redirs", "3",
            "--connect-timeout", str(min(seconds, 5)), "--max-time", str(seconds),
            "--proto", protocols, "--proto-redir", protocols,
            "--range", "0-" + str(MAX_SAMPLE_BYTES - 1),
            "--max-filesize", str(MAX_SAMPLE_BYTES), "--output", os.devnull,
            "--write-out", "%{http_code}\t%{size_download}\t%{time_total}\t%{content_type}"]
    if proxy is None:
        args += ["--noproxy", "*"]
    else:
        args += ["--proxy", proxy, "--noproxy", ""]
    args += ["--url", url]
    environment = {key: value for key, value in os.environ.items()
                   if key.lower() not in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}}
    started = time.monotonic()
    try:
        result = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=seconds + 5, env=environment)
    except FileNotFoundError:
        return _failed("curl_not_found")
    except subprocess.TimeoutExpired:
        return _failed("curl_process_timeout", elapsed_seconds=round(time.monotonic() - started, 6))
    except OSError:
        return _failed("curl_process_failed")
    try:
        status, length, elapsed, mime = result.stdout.strip().split("\t", 3)
        code = int(status)
        amount, duration = float(length), float(elapsed)
        if (not math.isfinite(amount) or amount < 0 or not amount.is_integer()
                or not math.isfinite(duration) or duration < 0):
            raise ValueError()
        amount = int(amount)
        mime = mime.split(";", 1)[0].strip().lower()
        sample = {"status": "failed", "http_code": code, "bytes": amount,
                  "elapsed_seconds": round(duration, 6), "curl_exit": result.returncode,
                  "content_type": mime if mime in MIME_LABELS else "other"}
    except (ValueError, TypeError):
        return _failed("invalid_curl_metrics", curl_exit=result.returncode)
    if code not in {200, 206}:
        sample["error"] = "http_response_rejected"
    elif (mime.startswith("text/") or "html" in mime or "json" in mime
          or "xml" in mime or not mime):
        sample["error"] = "non_binary_response"
    elif result.returncode not in {0, 28}:
        sample["error"] = "transfer_limit_exceeded" if result.returncode == 63 else "curl_transfer_failed"
    elif amount < MIN_SAMPLE_BYTES:
        sample["error"] = "sample_too_small"
    elif amount > MAX_SAMPLE_BYTES:
        sample["error"] = "transfer_limit_exceeded"
    else:
        sample["status"] = "timed_sample" if result.returncode == 28 else "ok"
    return sample


def probe_download(url, *, label="source", connections=1, seconds=10, curl="curl",
                   proxy=None, allow_http=False):
    """Return sanitized aggregate bytes / elapsed wall time, in decimal Mbps.

    A SOCKS proxy is accepted only on loopback for temporary local node clients.
    ``allow_http`` exists for isolated fixture servers; public probes use HTTPS.
    The 100 MiB cap applies per connection, so four samples use at most 400 MiB.
    """
    validate_target(url, allow_http)
    validate_proxy(proxy)
    label = _label(label)
    _bounded_integer(connections, 1, 4, "connections_must_be_1_to_4")
    _bounded_integer(seconds, 1, 15, "seconds_must_be_1_to_15")
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=connections) as pool:
        samples = list(pool.map(lambda _: _sample(url, seconds, curl, proxy, allow_http), range(connections)))
    elapsed = max(time.monotonic() - started, 0.000001)
    accepted = [sample for sample in samples if sample["status"] in {"ok", "timed_sample"}]
    amount = sum(sample["bytes"] for sample in accepted)
    valid = len(accepted)
    return {"label": label, "status": "ok" if valid == connections else "partial" if valid else "failed",
            "connections": connections, "valid_connections": valid, "seconds_limit": seconds,
            "bytes": amount, "elapsed_seconds": round(elapsed, 6),
            "mbps": round(amount * 8 / elapsed / 1_000_000, 3) if valid else None,
            "samples": samples}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, default=10)
    parser.add_argument("--connections", type=int, choices=(1, 4), default=1)
    parser.add_argument("--url", default=DEFAULT_URL)
    args = parser.parse_args(argv)
    try:
        result = probe_download(args.url, connections=args.connections, seconds=args.seconds)
    except ValueError as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
