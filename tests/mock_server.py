"""Shared mock of pexels.com search pages + DeepSeek /chat/completions for tests."""
import json
from http.server import BaseHTTPRequestHandler
from urllib.parse import unquote, urlparse

PORT = 8767
BASE = f"http://127.0.0.1:{PORT}"

DS_CALLS = {"n": 0, "bodies": []}
# test switches: CHALLENGE → first page visit shows a bot-check until a cookie is set;
# BLOCK_FILES → the video CDN refuses direct (non-browser) downloads
FLAGS = {"CHALLENGE": False, "BLOCK_FILES": False, "challenges_shown": 0, "browser_downloads": 0}
CHALLENGE_HTML = """<html><head><title>Just a moment...</title></head><body>
<p>Verify you are human by completing the action below.</p>
<script>setTimeout(function(){document.cookie='passed=1; path=/'; location.reload();}, 3000);</script>
</body></html>"""


def items_for(query):
    if "nothing" in query:
        return []
    base = abs(hash(query)) % 100000 * 10
    out = []
    for k in range(5):
        i = base + k
        dur = [3, 12, 25, 40, 8][k]
        files = [{"file_type": "video/mp4", "quality": "hd", "width": 1920, "height": 1080, "fps": 30,
                  "link": f"{BASE}/file/{i}.mp4", "download_link": f"{BASE}/download/{i}"},
                 {"file_type": "video/mp4", "quality": "sd", "width": 640, "height": 360, "fps": 30,
                  "link": f"{BASE}/file/{i}_sd.mp4"}]
        slug = query.replace(" ", "-")
        out.append({"id": str(i), "type": "video", "attributes": {
            "id": i, "slug": f"{slug}-{k}", "title": f"{query.title()} shot {k}", "width": 1920, "height": 1080,
            "aspect_ratio": 16 / 9, "duration": dur, "fps": 30, "tags": [{"name": query}],
            "user": {"first_name": "Ali", "last_name": "K", "username": "alik"},
            "video": {"thumbnail": f"https://img/{i}.jpg", "video_files": files}}})
    return out


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _send(self, code, body, ctype):
        b = body if isinstance(body, bytes) else body.encode()
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path.startswith("/file/"):
            if FLAGS["BLOCK_FILES"]:
                return self._send(403, "<html>blocked</html>", "text/html")
            return self._send(200, b"\x00" * 20000, "video/mp4")
        if u.path.startswith("/download/"):
            FLAGS["browser_downloads"] += 1
            b = b"\x00" * 20000
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Disposition", 'attachment; filename="clip.mp4"')
            self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
            return
        if u.path.startswith("/search/videos/") and FLAGS["CHALLENGE"] and "passed=1" not in (self.headers.get("Cookie") or ""):
            FLAGS["challenges_shown"] += 1
            return self._send(403, CHALLENGE_HTML, "text/html")
        if u.path.startswith("/search/videos/"):
            q = unquote(u.path.split("/search/videos/")[1].strip("/"))
            nd = {"props": {"pageProps": {"initialData": {"data": items_for(q),
                  "pagination": {"current_page": 1, "total_pages": 1, "total_results": 5}, "meta": {}}}}}
            return self._send(200, f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(nd)}</script>',
                              "text/html")
        self._send(404, "nope", "text/plain")

    def do_POST(self):
        if self.path != "/chat/completions":
            return self._send(404, "nope", "text/plain")
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.headers["Authorization"] != "Bearer sk-test":
            return self._send(401, json.dumps({"error": {"message": "Authentication Fails"}}), "application/json")
        DS_CALLS["n"] += 1; DS_CALLS["bodies"].append(body)
        if "response_format" not in body:                       # the GUI's "Test key" ping
            resp = {"choices": [{"message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}]}
            return self._send(200, json.dumps(resp), "application/json")
        assert body["response_format"] == {"type": "json_object"}
        user = body["messages"][1]["content"].split("SENTENCES:\n")[1]
        items = []
        for line in user.splitlines():
            n, text = line.split(". ", 1)
            if "verdict" in text.lower():
                qs = ["nothing here", "judge gavel", "courtroom"]       # 1st query has no results → fallback
            elif "rain" in text.lower():
                qs = ["rain window night", "rain"]
            else:
                qs = ["courtroom", "lawyer walking"]
            items.append({"i": int(n), "queries": qs, "avoid": ["cartoon"]})
        content = json.dumps({"items": items})
        resp = {"choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}]}
        self._send(200, json.dumps(resp), "application/json")


