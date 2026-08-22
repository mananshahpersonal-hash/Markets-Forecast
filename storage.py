"""storage.py — persist the self-learning state across restarts / devices.

When deployed (e.g. on Streamlit Community Cloud) the local filesystem is wiped
on every restart, which would erase the model's track record. This module syncs
the per-asset state files in ./state with a **private GitHub Gist**, so the
learning genuinely persists and is the same whether you open the app on your
laptop or your phone.

Configure with two values (env vars, or Streamlit secrets bridged to env):
    GITHUB_TOKEN  - a token with ONLY the 'gist' scope
    GIST_ID       - the id of a (secret) gist that holds the state files

If either is missing, every function is a no-op and the app falls back to local
files — so nothing breaks when running locally without setup.
"""
from __future__ import annotations
import os
import glob
from pathlib import Path

try:
    import requests
except Exception:  # pragma: no cover
    requests = None

STATE_DIR = Path(__file__).resolve().parent / "state"
_API = "https://api.github.com/gists/"


def _token() -> str | None:
    return os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")


def _gist_id() -> str | None:
    return os.environ.get("GIST_ID")


def configured() -> bool:
    return bool(_token() and _gist_id() and requests is not None)


def backend_name() -> str:
    return "GitHub gist (persistent)" if configured() else "local only"


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_token()}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def pull(state_key: str) -> int:
    """Download this asset's state files from the gist into STATE_DIR.
    Returns the number of files written. Never raises."""
    if not configured():
        return 0
    try:
        r = requests.get(_API + _gist_id(), headers=_headers(), timeout=15)
        if r.status_code != 200:
            return 0
        files = r.json().get("files", {}) or {}
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        n = 0
        prefix = f"{state_key}_"
        for fname, meta in files.items():
            if not fname.startswith(prefix):
                continue
            content = meta.get("content")
            if (content is None or meta.get("truncated")) and meta.get("raw_url"):
                content = requests.get(meta["raw_url"], headers=_headers(),
                                       timeout=15).text
            if content is not None:
                (STATE_DIR / fname).write_text(content)
                n += 1
        return n
    except Exception:
        return 0


def delete(state_key: str) -> int:
    """Delete this asset's state files FROM the gist (sets them to null). Returns
    the count removed. Needed because push() never deletes, so a reset would
    otherwise be undone on the next pull. Never raises."""
    if not configured():
        return 0
    try:
        r = requests.get(_API + _gist_id(), headers=_headers(), timeout=15)
        if r.status_code != 200:
            return 0
        files = r.json().get("files", {}) or {}
        prefix = f"{state_key}_"
        payload = {name: None for name in files if name.startswith(prefix)}
        if not payload:
            return 0
        r2 = requests.patch(_API + _gist_id(), headers=_headers(),
                            json={"files": payload}, timeout=20)
        return len(payload) if r2.status_code == 200 else 0
    except Exception:
        return 0


def delete_all(keep_prefix: str = "") -> int:
    """Delete every state file from the gist (optionally keeping ones whose name
    starts with keep_prefix). Used for a one-time full reset. Never raises."""
    if not configured():
        return 0
    try:
        r = requests.get(_API + _gist_id(), headers=_headers(), timeout=15)
        if r.status_code != 200:
            return 0
        files = r.json().get("files", {}) or {}
        payload = {name: None for name in files
                   if not (keep_prefix and name.startswith(keep_prefix))}
        if not payload:
            return 0
        r2 = requests.patch(_API + _gist_id(), headers=_headers(),
                            json={"files": payload}, timeout=20)
        return len(payload) if r2.status_code == 200 else 0
    except Exception:
        return 0

    """Upload this asset's state files from STATE_DIR to the gist.
    Returns the number of files uploaded. Never raises."""
    if not configured():
        return 0
    try:
        payload = {}
        for path in glob.glob(str(STATE_DIR / f"{state_key}_*")):
            p = Path(path)
            try:
                text = p.read_text()
            except Exception:
                continue
            if text.strip():            # gist deletes a file if content is empty
                payload[p.name] = {"content": text}
        if not payload:
            return 0
        r = requests.patch(_API + _gist_id(), headers=_headers(),
                           json={"files": payload}, timeout=20)
        return len(payload) if r.status_code == 200 else 0
    except Exception:
        return 0
