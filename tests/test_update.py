"""Updater test: copy the app to a temp folder, serve a fake GitHub API + repo zip, run update."""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
PORT = 8768
SHA = "abc1234def5678900000000000000000deadbeef"


def blob_sha(data: bytes) -> str:
    import hashlib
    data = data.replace(b"\r\n", b"\n")
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


TREE = []


def make_zip():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for p in APP.rglob("*"):
            if p.is_file() and not ({".venv", "__pycache__", ".git"} & set(p.parts)):
                rel = p.relative_to(APP).as_posix()
                data = p.read_bytes()
                if rel == "README.md":
                    data += b"\nUPDATED-MARKER\n"
                if rel == "requirements.txt":
                    continue                                   # unchanged reqs → no pip run
                TREE.append({"path": rel, "type": "blob", "sha": blob_sha(data)})
                info = zipfile.ZipInfo(f"pexel-scrapper-main/{rel}")
                info.external_attr = (0o755 if rel.endswith(".command") else 0o644) << 16
                z.writestr(info, data)
        z.writestr("pexel-scrapper-main/new_feature.py", "print('new')\n")
        TREE.append({"path": "new_feature.py", "type": "blob", "sha": blob_sha(b"print('new')\n")})
        z.writestr("pexel-scrapper-main/broll_projects/should_not_overwrite.txt", "x")
    return buf.getvalue()


ZIP = make_zip()


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_GET(self):
        if "/git/trees/" in self.path:
            body = json.dumps({"sha": SHA, "tree": TREE}).encode()
            ctype = "application/json"
        elif self.path.startswith("/repos/"):
            body = json.dumps({"sha": SHA, "commit": {"committer": {"date": "2026-11-25T02:00:00Z"},
                                                      "message": "Add new feature\n\ndetails"}}).encode()
            ctype = "application/json"
        elif self.path.startswith("/zip"):
            body, ctype = ZIP, "application/zip"
        else:
            self.send_response(404); self.end_headers(); return
        self.send_response(200); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    tmp = Path(tempfile.mkdtemp()) / "app"
    shutil.copytree(APP, tmp, ignore=shutil.ignore_patterns(".venv", "__pycache__", ".git"))
    (tmp / "broll_projects").mkdir(exist_ok=True)
    (tmp / "broll_projects" / "my_project.txt").write_text("keep me")
    env = dict(os.environ, BROLL_UPDATE_API=f"http://127.0.0.1:{PORT}", BROLL_UPDATE_ZIP=f"http://127.0.0.1:{PORT}/zip",
               NO_PROXY="127.0.0.1", no_proxy="127.0.0.1")
    code = ("import updater, json; c = updater.check(); print(json.dumps(c)); "
            "updater.update(); print('after', json.dumps(updater.check()))")
    r = subprocess.run([sys.executable, "-c", code], cwd=tmp, env=env, capture_output=True, text=True)
    print(r.stdout, r.stderr[-500:])
    lines = r.stdout.strip().splitlines()
    first = json.loads(lines[0])
    assert first["available"] is True
    assert '"available": false' in lines[-1], "after updating, it should report up to date"
    assert "UPDATED-MARKER" in (tmp / "README.md").read_text()
    assert (tmp / "new_feature.py").exists()
    assert (tmp / "broll_projects" / "my_project.txt").read_text() == "keep me"
    assert not (tmp / "broll_projects" / "should_not_overwrite.txt").exists(), "user folders untouched"
    assert os.access(tmp / "Start (Mac).command", os.X_OK), "launcher stays executable"
    assert json.loads((tmp / ".installed_version.json").read_text())["sha"] == SHA
    # a fresh "Download ZIP" install that already matches GitHub must NOT say "update available"
    (tmp / ".installed_version.json").unlink()
    r2 = subprocess.run([sys.executable, "-c", "import updater, json; print(json.dumps(updater.check()))"],
                        cwd=tmp, env=env, capture_output=True, text=True)
    print("fresh install check:", r2.stdout.strip()[:80], r2.stderr[-300:])
    assert json.loads(r2.stdout)["available"] is False, "matching files → up to date, no false alarm"
    # and a changed local file → update offered again
    (tmp / "gui.py").write_text((tmp / "gui.py").read_text() + "\n# local edit\n")
    (tmp / ".installed_version.json").unlink()
    r3 = subprocess.run([sys.executable, "-c", "import updater, json; print(json.dumps(updater.check()))"],
                        cwd=tmp, env=env, capture_output=True, text=True)
    assert json.loads(r3.stdout)["available"] is True
    print("ALL UPDATE TESTS PASSED")
    srv.shutdown()


if __name__ == "__main__":
    main()
