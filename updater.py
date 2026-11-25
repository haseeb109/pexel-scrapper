"""
Self-update from GitHub (haseeb109/pexel-scrapper).

- check():  compares the installed commit with the latest commit on GitHub.
- update(): git checkout  → `git pull --ff-only`
            zip install   → downloads the repo zip and copies the files over this folder,
                            leaving .venv, your projects and settings alone.
            Then reinstalls requirements if they changed.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Callable, Optional

import requests

REPO = os.getenv("BROLL_UPDATE_REPO", "haseeb109/pexel-scrapper")
BRANCH = os.getenv("BROLL_UPDATE_BRANCH", "main")
API = os.getenv("BROLL_UPDATE_API", "https://api.github.com")
ZIP_URL = os.getenv("BROLL_UPDATE_ZIP", f"https://codeload.github.com/{REPO}/zip/refs/heads/{BRANCH}")
APP_DIR = Path(__file__).resolve().parent
STAMP = APP_DIR / ".installed_version.json"
# never overwritten or deleted by an update
KEEP = {".venv", ".git", "broll_projects", "pexels_downloads", ".installed_version.json", "__pycache__"}


def installed() -> dict:
    if (APP_DIR / ".git").exists():
        try:
            sha = subprocess.run(["git", "-C", str(APP_DIR), "rev-parse", "HEAD"],
                                 capture_output=True, text=True, timeout=10).stdout.strip()
            if sha:
                return {"sha": sha, "via": "git"}
        except Exception:  # noqa: BLE001
            pass
    try:
        return json.loads(STAMP.read_text())
    except Exception:  # noqa: BLE001
        return {"sha": "", "via": "zip"}


def version_label() -> str:
    info = installed()
    sha = info.get("sha", "")
    if not sha:
        return "version: unknown"
    date = info.get("date", "")[:10]
    return f"version {sha[:7]}" + (f" ({date})" if date else "")


def latest() -> dict:
    r = requests.get(f"{API}/repos/{REPO}/commits/{BRANCH}", timeout=15,
                     headers={"Accept": "application/vnd.github+json", "User-Agent": "pexels-broll-updater"})
    if r.status_code == 404:
        raise RuntimeError(f"GitHub repo {REPO} (branch {BRANCH}) not found or empty")
    if r.status_code == 403 and "rate limit" in r.text.lower():
        raise RuntimeError("GitHub rate limit hit — try again in an hour")
    r.raise_for_status()
    d = r.json()
    return {"sha": d["sha"], "date": d["commit"]["committer"]["date"],
            "message": d["commit"]["message"].splitlines()[0]}


def _git_blob_sha(path: Path) -> str:
    data = path.read_bytes().replace(b"\r\n", b"\n")       # repo stores LF; .bat is checked out as CRLF
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _files_match(commit_sha: str) -> bool:
    """True if every file of that GitHub commit is already here with identical content."""
    r = requests.get(f"{API}/repos/{REPO}/git/trees/{commit_sha}", params={"recursive": "1"}, timeout=15,
                     headers={"Accept": "application/vnd.github+json", "User-Agent": "pexels-broll-updater"})
    r.raise_for_status()
    blobs = [i for i in r.json().get("tree", []) if i.get("type") == "blob"]
    if not blobs:
        return False
    for item in blobs:
        local = APP_DIR / item["path"]
        if not local.is_file():
            return False
        try:
            if _git_blob_sha(local) != item["sha"]:
                return False
        except OSError:
            return False
    return True


def check() -> dict:
    """{'available': bool, 'current': sha, 'latest': {...}}"""
    cur = installed()
    new = latest()
    available = cur.get("sha") != new["sha"]
    if available and cur.get("via") != "git":
        # fresh "Download ZIP" installs have no version stamp yet: compare the actual files
        try:
            if _files_match(new["sha"]):
                STAMP.write_text(json.dumps({"sha": new["sha"], "date": new["date"], "via": "zip"}))
                cur, available = {"sha": new["sha"]}, False
        except Exception:  # noqa: BLE001 — fall back to "update available"
            pass
    return {"available": available, "current": cur.get("sha", ""), "latest": new}


def _req_hash() -> str:
    p = APP_DIR / "requirements.txt"
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else ""


def _pip_install(log: Callable[[str], None]):
    log("Installing updated packages…")
    r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--disable-pip-version-check",
                        "-r", str(APP_DIR / "requirements.txt")], capture_output=True, text=True)
    if r.returncode != 0:
        log(f"  pip warning: {r.stderr.strip()[-300:]}")


def update(log: Callable[[str], None] = print) -> dict:
    new = latest()
    before = _req_hash()
    if installed().get("via") == "git":
        log("Pulling latest changes with git…")
        r = subprocess.run(["git", "-C", str(APP_DIR), "pull", "--ff-only"], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"git pull failed: {r.stderr.strip()[-300:]}")
    else:
        log("Downloading latest version from GitHub…")
        r = requests.get(ZIP_URL, timeout=120, headers={"User-Agent": "pexels-broll-updater"})
        r.raise_for_status()
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        root = zf.namelist()[0].split("/")[0] + "/"          # "pexel-scrapper-main/"
        count = 0
        for member in zf.infolist():
            if not member.filename.startswith(root) or member.is_dir():
                continue
            rel = member.filename[len(root):]
            if not rel or rel.split("/")[0] in KEEP:
                continue
            dest = APP_DIR / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + ".new")
            with zf.open(member) as src, open(tmp, "wb") as out:
                shutil.copyfileobj(src, out)
            os.replace(tmp, dest)
            mode = (member.external_attr >> 16) & 0o777
            if mode & 0o111 or dest.suffix == ".command":
                dest.chmod(0o755)
            count += 1
        log(f"Updated {count} files.")
        STAMP.write_text(json.dumps({"sha": new["sha"], "date": new["date"], "via": "zip"}))
    if _req_hash() != before:
        _pip_install(log)
        try:                                             # keep the Mac launcher's cache in sync
            (APP_DIR / ".venv" / ".req_hash").write_text(hashlib.sha1((APP_DIR / "requirements.txt").read_bytes()).hexdigest())
        except OSError:
            pass
    log(f"✓ Now on {new['sha'][:7]} — {new['message']}")
    return new


def restart_app(script: Optional[str] = None):
    """Replace the running process with a fresh copy of the app."""
    script = script or sys.argv[0]
    os.execv(sys.executable, [sys.executable, script] + sys.argv[1:])


if __name__ == "__main__":
    info = check()
    print(json.dumps(info, indent=2))
    if info["available"] and "--yes" in sys.argv:
        update()
