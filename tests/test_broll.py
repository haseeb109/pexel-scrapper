"""Script → B-roll test: mock Pexels site + mock DeepSeek API + the Flask UI endpoints.
Run:  python tests/test_broll.py
"""
import json
import os
import sys
import tempfile
import threading
import time
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path

PORT = 8767
BASE = f"http://127.0.0.1:{PORT}"
os.environ["DEEPSEEK_BASE_URL"] = BASE
os.environ["PEXELS_SITE"] = BASE
os.environ["BROLL_OUT"] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import script_broll as sb  # noqa: E402
import app as webapp       # noqa: E402
from mock_server import H, DS_CALLS  # noqa: E402

SCRIPT = """[INTRO MUSIC]
The courtroom fell silent as the defendant stood up.
Outside, rain hammered against the windows of the old building.
Then the judge read the verdict. Guilty.
Nobody expected what the lawyer did next.
"""


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        # --- splitting
        sents = sb.split_script(SCRIPT)
        print("sentences:", sents)
        assert len(sents) == 4, "cue removed, 'Guilty.' glued onto the verdict sentence"
        assert sents[2].endswith("Guilty.")

        # --- offline keyword fallback produces something usable
        kq = sb.keyword_queries(sents[1], SCRIPT)
        print("keyword queries:", kq)
        assert kq and all(isinstance(q, str) and q for q in kq)

        # --- full flow through the web UI
        client = webapp.app.test_client()
        assert client.get("/").status_code == 200
        r = client.post("/start", json={"script": SCRIPT, "name": "Docket Ep 1", "aspect": "16:9",
                                        "resolution": 1080, "clips": 2, "deepseek_key": "sk-test",
                                        "exclude": "wedding", "use_browser": False})
        job = r.get_json()["job"]
        sb_delay_patch = None  # settings use default 2s delay; shorten for tests
        for _ in range(240):
            s = client.get(f"/status/{job}").get_json()
            if s["done"]:
                break
            time.sleep(0.5)
        print("\n".join(s["log"]))
        assert s["done"] and not s["error"], s["error"]
        res = s["result"]
        assert DS_CALLS["n"] == 1 and DS_CALLS["bodies"][0]["model"] == "deepseek-flash"
        assert res["sentences"] == 4 and res["clips"] == 8 and not res["missing"], res

        verdict = next(x for x in s["sentences"] if x["i"] == 3)
        assert verdict["used"] == "judge gavel", "should fall back past the empty query"
        all_ids = [c["url"] for x in s["sentences"] for c in x["clips"]]
        assert len(all_ids) == len(set(all_ids)), "the same clip must not be reused across sentences"

        folder = Path(res["folder"])
        clips = sorted(p.name for p in (folder / "clips").glob("*.mp4"))
        print("clips:", clips)
        assert clips[0].startswith("001a_") and clips[1].startswith("001b_") and clips[-1].startswith("004b_")
        assert all("1920x1080" in c for c in clips)
        assert (folder / "storyboard.csv").exists() and (folder / "storyboard.html").exists()

        z = client.get(f"/download/{job}")
        assert z.status_code == 200
        zf = zipfile.ZipFile(Path(res["zip"]))
        names = zf.namelist()
        assert any(n.endswith("storyboard.html") for n in names) and sum(n.endswith(".mp4") for n in names) == 8
        assert client.get(f"/storyboard/{job}").status_code == 200

        # --- bad DeepSeek key falls back to keywords instead of failing the job
        out = sb.run_project("A quiet street at night. Rain falls on the road.", Path(os.environ["BROLL_OUT"]),
                             "fallback-test", sb.Settings(delay=0, use_browser=False), deepseek_key="sk-wrong", site_base=BASE,
                             log=lambda m: None)
        assert out["sentences"] == 2
        print("ALL BROLL TESTS PASSED")
    finally:
        srv.shutdown()


if __name__ == "__main__":
    main()
