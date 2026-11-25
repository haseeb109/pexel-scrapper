"""End-to-end test against a local mock of pexels.com search pages (__NEXT_DATA__ shape
captured from the live site). Run:  python tests/test_e2e.py
"""
import json
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pexels_scraper as ps  # noqa: E402

PORT = 8766
BASE = f"http://127.0.0.1:{PORT}"
STATE = {"sent_429": False, "pages": 0}


def item(i, w, h, dur, fps, title, slug, files, tags=()):
    return {"id": str(i), "type": "video", "attributes": {
        "id": i, "slug": slug, "title": title, "description": "", "width": w, "height": h,
        "aspect_ratio": w / h, "duration": dur, "fps": fps, "created_at": "2025-01-01T00:00:00Z",
        "tags": [{"name": t, "search_term": t} for t in tags],
        "user": {"first_name": "Jane", "last_name": "Doe", "username": "janedoe"},
        "video": {"thumbnail": {"large": f"https://img/{i}.jpg"}, "preview_src": f"https://prev/{i}.mp4",
                  "video_files": [{"file_type": ft, "quality": q, "width": fw, "height": fh, "fps": fps,
                                   "link": f"{BASE}/file/{i}_{fw}x{fh}.mp4",
                                   "download_link": f"{BASE}/download/{i}"} for q, ft, fw, fh in files]}}}


L4K = [("sd", "video/mp4", 640, 360), ("hd", "video/mp4", 1920, 1080), ("uhd", "video/mp4", 3840, 2160)]
L720 = [("sd", "video/mp4", 640, 360), ("hd", "video/mp4", 1280, 720)]
VERT = [("sd", "video/mp4", 540, 960), ("hd", "video/mp4", 1080, 1920)]
NULLQ = [(None, "video/mp4", 1280, 720), (None, "video/mp4", 1920, 1080)]   # seen live

PAGES = {
    1: [item(1, 3840, 2160, 20, 59.94, "Ocean Waves Crashing on Rocks", "ocean-waves-crashing-on-rocks", L4K),
        item(2, 1280, 720, 15, 25, "Calm Sea", "calm-sea", L720),
        item(3, 1080, 1920, 12, 30, "Vertical Ocean Waves", "vertical-ocean-waves", VERT),
        item(4, 1920, 1080, 200, 30, "Ocean Waves Long Take", "ocean-waves-long-take", NULLQ)],
    2: [item(5, 1920, 1080, 25, 29.97, "Ocean Waves at Sunset", "ocean-waves-at-sunset", NULLQ, ["sunset"]),
        item(1, 3840, 2160, 20, 59.94, "Ocean Waves Crashing on Rocks", "ocean-waves-crashing-on-rocks", L4K),
        item(6, 1920, 1080, 30, 24, "", "people-surfing-ocean-waves", NULLQ)],   # empty title → from slug
}


def page_html(page):
    nd = {"props": {"pageProps": {"initialData": {
        "data": PAGES.get(page, []),
        "pagination": {"current_page": page, "total_pages": 2, "total_results": 7},
        "meta": {"total_results": 7, "related_searches": [{"name": "sea waves"}]}}}}}
    return f'<html><body><script id="__NEXT_DATA__" type="application/json">{json.dumps(nd)}</script></body></html>'


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _send(self, code, body: bytes, ctype="text/html", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items(): self.send_header(k, v)
        self.end_headers(); self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path); q = parse_qs(u.query)
        if u.path.startswith("/file/"):
            data = b"\x00" * 50_000
            rng = self.headers.get("Range")
            start = int(rng.split("=")[1].split("-")[0]) if rng else 0
            return self._send(206 if rng else 200, data[start:], "video/mp4")
        if u.path.startswith("/search/videos/blocked"):
            return self._send(403, b"<html><title>Just a moment...</title></html>")
        if u.path.startswith("/search/videos/"):
            if not STATE["sent_429"]:
                STATE["sent_429"] = True
                return self._send(429, b"slow down", extra={"Retry-After": "1"})
            STATE["pages"] += 1
            return self._send(200, page_html(int(q.get("page", ["1"])[0])).encode())
        self._send(404, b"nope")


def run(args):
    return ps.main(args + ["--site", BASE, "--delay", "0"])


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    out = Path(tempfile.mkdtemp())
    try:
        rc = run(["ocean waves", "-n", "3", "-o", str(out), "--aspect", "16:9", "--max-duration", "60",
                  "--resolution", "1080", "--strict-resolution", "--workers", "2"])
        rows = [json.loads(l) for l in (out / "manifest.jsonl").read_text().splitlines()]
        ids = [r["id"] for r in rows]
        print("\nselected:", ids, "rc:", rc)
        assert rc == 0
        assert ids[0] == 1, "4K 60fps clip should rank first"
        assert 2 not in ids, "720p-only clip skipped under --strict-resolution"
        assert 3 not in ids, "portrait dropped by --aspect 16:9"
        assert 4 not in ids, "200s clip dropped by --max-duration 60"
        assert ids.count(1) == 1, "duplicates removed"
        assert all(r["chosen_file"].startswith("1920x1080") for r in rows), "1080p rendition picked"
        assert all(Path(r["local_path"]).stat().st_size == 50_000 for r in rows)
        assert rows[0]["pexels_url"].endswith("/video/ocean-waves-crashing-on-rocks-1/")
        assert rows[0]["author_name"] == "Jane Doe"
        assert next(r for r in rows if r["id"] == 6)["title"] == "People surfing ocean waves"
        assert (out / "results.csv").exists() and (out / "CREDITS.txt").exists()

        # resume: second run must not re-download the same ids
        run(["ocean waves", "-n", "3", "-o", str(out), "--aspect", "16:9", "--max-duration", "60", "--quiet"])
        rows2 = [json.loads(l) for l in (out / "manifest.jsonl").read_text().splitlines()][len(rows):]
        assert not {r["id"] for r in rows2} & set(ids), "resume skips downloaded ids"

        # bot-check page is detected and reported, not crashed on
        rc3 = run(["blocked", "-o", str(out), "--quiet"])
        assert rc3 == 0
        print("ALL TESTS PASSED")
    finally:
        srv.shutdown(); shutil.rmtree(out, ignore_errors=True)


if __name__ == "__main__":
    main()
