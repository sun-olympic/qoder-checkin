"""Interactive browser authentication helpers for Qoder.

The browser is only used as a human-assisted login surface.  Credentials are
captured from requests to Qoder's own API hosts and are never printed.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse
import json
from pathlib import Path
import tempfile
import os
import subprocess
import sys
import importlib.util


API_HOSTS = {
    "global": {"openapi.qoder.sh"},
    "cn": {"gateway.qoder.com.cn", "openapi.qoder.com.cn"},
}
_BEARER = re.compile(r"^Bearer\s+([^\s]+)$", re.I)


def bootstrap_command(script: Path, argv: list[str]) -> list[str] | None:
    """Prepare a private Playwright environment like workbuddy-checkin."""
    if os.environ.get("QODER_PLAYWRIGHT_READY") == "1":
        return None
    if importlib.util.find_spec("playwright") is not None:
        return None
    venv = script.parent / ".qoder-browser-venv"
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if python.exists():
        probe = subprocess.run([str(python), '-c', 'from playwright.sync_api import sync_playwright'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        if probe.returncode == 0:
            return [str(python), str(script), *argv]
    if not python.exists():
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    subprocess.run([str(python), "-m", "pip", "install", "--disable-pip-version-check", "playwright"], check=True)
    subprocess.run([str(python), "-m", "playwright", "install", "chromium"], check=True)
    return [str(python), str(script), *argv]


def bearer_from_headers(headers: dict, region: str) -> str | None:
    """Return a valid bearer value from a request header mapping."""
    if not isinstance(headers, dict):
        return None
    value = next((v for k, v in headers.items() if str(k).lower() == "authorization"), None)
    if not isinstance(value, str):
        return None
    match = _BEARER.fullmatch(value.strip())
    if not match:
        return None
    token = match.group(1)
    if len(token) > 16384 or any(ord(c) < 33 or ord(c) > 126 for c in token):
        return None
    return token


def is_qoder_api_url(url: str, region: str) -> bool:
    try:
        parsed = urlparse(url)
    except (TypeError, ValueError):
        return False
    return parsed.scheme == "https" and parsed.hostname in API_HOSTS.get(region, set())


def capture_token_from_request(url: str, headers: dict, region: str) -> str | None:
    if not is_qoder_api_url(url, region):
        return None
    return bearer_from_headers(headers, region)


def _launch_login_context(playwright, profile_name: str):
    """Keep the WeChat browser profile across interactive binding attempts."""
    profile = Path.home() / ".qoder-checkin" / profile_name
    profile.mkdir(parents=True, exist_ok=True)
    try:
        return playwright.chromium.launch_persistent_context(
            str(profile), channel="chrome", headless=False,
            args=["--start-maximized"], no_viewport=True,
        )
    except Exception:
        return playwright.chromium.launch_persistent_context(
            str(profile), headless=False, args=["--start-maximized"], no_viewport=True,
        )


def browser_login(region, output, timeout_seconds=300, expected_account_id=None, isolated=False):
    from qoder_cli_auth import browser_login as official_login
    return official_login(region, output, timeout_seconds, expected_account_id, isolated=isolated)
