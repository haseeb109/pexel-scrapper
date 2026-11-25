"""Drive the desktop GUI against the mock servers.
Run (Linux needs a display):  xvfb-run python tests/test_gui.py [screenshot.png]
"""
import os
import subprocess
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent)); sys.path.insert(0, str(HERE))
from mock_server import BASE, PORT, H, DS_CALLS  # noqa: E402

os.environ["PEXELS_SITE"] = BASE
os.environ["DEEPSEEK_BASE_URL"] = BASE
os.environ["HOME"] = tempfile.mkdtemp()          # keep the config file out of the real home
import gui  # noqa: E402

SCRIPT = """[INTRO MUSIC]
The courtroom fell silent as the defendant stood up.
Outside, rain hammered against the windows of the old building.
Then the judge read the verdict. Guilty.
Nobody expected what the lawyer did next.
"""


def pump(app, cond, timeout=120):
    t0 = time.time()
    while not cond():
        app.update()
        time.sleep(0.05)
        if time.time() - t0 > timeout:
            raise TimeoutError("GUI did not finish in time")


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    out = tempfile.mkdtemp()
    app = gui.App()
    try:
        app.update()
        # Test-key button: good and bad keys
        app.key_var.set("sk-wrong"); app._test_key()
        pump(app, lambda: "401" in app.key_status.cget("text"))
        print("bad key  →", app.key_status.cget("text"))
        app.key_var.set("sk-test"); app._test_key()
        pump(app, lambda: "works" in app.key_status.cget("text"))
        print("good key →", app.key_status.cget("text"))

        app.name_var.set("Docket Ep 2")
        app.out_var.set(out)
        app.clips_var.set("2")
        app.chrome_var.set(False)          # Chrome mode has its own test (test_chrome.py)
        app.script.insert("1.0", SCRIPT)
        app._count(); app.update()
        print("counter  →", app.count_lbl.cget("text"))
        assert app.count_lbl.cget("text").startswith("4 sentences")

        app._start()
        pump(app, lambda: app.result is not None)
        pump(app, lambda: str(app.dl_btn.cget("state")) == "normal")
        app.update()
        print("summary  →", app.summary.cget("text"))
        res = app.result
        assert app.pvar.get() == 1000, "progress bar full when done"
        assert res["clips"] == 8 and res["sentences"] == 4 and not res["missing"]
        rows = [app.tv.item(i, "values") for i in app.tv.get_children()]
        for r in rows:
            print("   row:", r)
        assert len(rows) == 4 and all(r[4] == "found" for r in rows)
        assert all(str(r[3]).startswith("2/2") for r in rows)
        assert len(list(Path(res["folder"], "clips").glob("*.mp4"))) == 8
        zpath = Path(out) / "export.zip"
        sb_zip = gui.sb.make_zip(res["folder"], zpath)
        import zipfile
        names = zipfile.ZipFile(sb_zip).namelist()
        assert sum(n.endswith(".mp4") for n in names) == 8 and any(n.endswith("storyboard.html") for n in names)
        assert gui.load_cfg().get("deepseek_key") == "sk-test", "key remembered"

        if len(sys.argv) > 1:
            app.lift(); app.update(); time.sleep(0.5); app.update()
            subprocess.run(["import", "-window", "root", sys.argv[1]], check=True)
            print("screenshot →", sys.argv[1])

        # Stop button: start a second run and stop it right away
        app.name_var.set("stop-test")
        app._start(); app._stop()
        pump(app, lambda: str(app.go_btn.cget("state")) == "normal")
        print("stop     →", app.summary.cget("text"))
        assert "Stop" in app.summary.cget("text")
        print("ALL GUI TESTS PASSED")
    finally:
        app.destroy()
        srv.shutdown()


if __name__ == "__main__":
    main()
