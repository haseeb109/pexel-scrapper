#!/usr/bin/env python3
"""
Script → B-roll web app.

    pip install requests flask
    python app.py                 # opens http://127.0.0.1:5055 in your browser

Paste a script, press Find footage, watch each sentence get matched, then hit
"Download folder (ZIP)" or "Open folder".
"""
from __future__ import annotations

import json
import os
import platform
import subprocess
import threading
import time
import uuid
import webbrowser
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_file

import pexels_scraper as ps
import script_broll as sb

APP_DIR = Path(__file__).resolve().parent
OUT_ROOT = Path(os.getenv("BROLL_OUT", APP_DIR / "broll_projects"))
CONFIG = Path.home() / ".pexels_broll.json"
SITE_BASE = os.getenv("PEXELS_SITE", ps.SITE)       # override only for testing

app = Flask(__name__)
JOBS: dict[str, dict] = {}


def load_cfg() -> dict:
    try:
        return json.loads(CONFIG.read_text())
    except Exception:  # noqa: BLE001
        return {}


def saved_key() -> str:
    return os.getenv("DEEPSEEK_API_KEY") or load_cfg().get("deepseek_key", "")


def snapshot(results: list[sb.SentenceResult]) -> list[dict]:
    out = []
    for sr in results:
        out.append({"i": sr.index, "text": sr.text, "seconds": sr.seconds, "queries": sr.queries,
                    "used": sr.query_used, "status": sr.status,
                    "clips": [{"thumb": v.thumbnail, "url": v.pexels_url, "status": v.status,
                               "res": f"{v.chosen_file.width}x{v.chosen_file.height}" if v.chosen_file else "",
                               "dur": v.duration} for v in sr.clips]})
    return out


def worker(job_id: str, script: str, name: str, st: sb.Settings, key: str):
    job = JOBS[job_id]
    def log(m):
        job["log"].append(f"{time.strftime('%H:%M:%S')}  {m}")
    def upd(results):
        job["sentences"] = snapshot(results)
    try:
        job["result"] = sb.run_project(script, OUT_ROOT, name, st, key or None, SITE_BASE, log, upd)
    except Exception as e:  # noqa: BLE001
        log(f"✗ {e}")
        job["error"] = str(e)
    job["done"] = True


@app.get("/")
def index():
    return PAGE.replace("__HAS_KEY__", "true" if saved_key() else "false")


@app.post("/start")
def start():
    d = request.get_json(force=True)
    script = (d.get("script") or "").strip()
    if not script:
        return jsonify(error="Script is empty"), 400
    key = (d.get("deepseek_key") or "").strip() or saved_key()
    if d.get("remember") and d.get("deepseek_key"):
        cfg = load_cfg(); cfg["deepseek_key"] = d["deepseek_key"].strip()
        CONFIG.write_text(json.dumps(cfg)); CONFIG.chmod(0o600)
    aspect = {"16:9": 16 / 9, "9:16": 9 / 16, "1:1": 1.0, "any": None}[d.get("aspect", "16:9")]
    orient = {"16:9": "landscape", "9:16": "portrait", "1:1": "square", "any": None}[d.get("aspect", "16:9")]
    st = sb.Settings(orientation=orient, aspect=aspect, resolution=int(d.get("resolution", 1080)),
                     clips_per_sentence=max(1, min(4, int(d.get("clips", 1)))),
                     exclude=[w.strip() for w in (d.get("exclude") or "").split(",") if w.strip()],
                     use_browser=bool(d.get("use_browser", True)))
    name = (d.get("name") or "").strip() or time.strftime("project-%Y%m%d-%H%M")
    job_id = uuid.uuid4().hex[:10]
    JOBS[job_id] = {"log": [], "sentences": [], "done": False, "result": None, "error": None,
                    "name": name, "ai": bool(key)}
    threading.Thread(target=worker, args=(job_id, script, name, st, key), daemon=True).start()
    return jsonify(job=job_id)


@app.get("/status/<job_id>")
def status(job_id):
    job = JOBS.get(job_id) or abort(404)
    return jsonify(job)


@app.get("/download/<job_id>")
def download(job_id):
    job = JOBS.get(job_id) or abort(404)
    if not job.get("result"):
        abort(409)
    if not job.get("zip"):
        job["zip"] = sb.make_zip(job["result"]["folder"])      # built on first click, reused after
    z = Path(job["zip"])
    return send_file(z, as_attachment=True, download_name=z.name)


@app.post("/open/<job_id>")
def open_folder(job_id):
    job = JOBS.get(job_id) or abort(404)
    folder = (job.get("result") or {}).get("folder")
    if not folder:
        abort(409)
    sysname = platform.system()
    if sysname == "Darwin":
        subprocess.Popen(["open", folder])
    elif sysname == "Windows":
        os.startfile(folder)  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", folder])
    return jsonify(ok=True)


@app.get("/storyboard/<job_id>")
def storyboard(job_id):
    job = JOBS.get(job_id) or abort(404)
    folder = (job.get("result") or {}).get("folder") or abort(409)
    return send_file(Path(folder) / "storyboard.html")


PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><title>Script → B-roll</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#0f1115;--card:#171a21;--line:#262a33;--txt:#e7e9ee;--mut:#8b919e;--acc:#3ddc97;--warn:#ffb547;--bad:#ff6b6b}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--txt);font:14px/1.5 -apple-system,Segoe UI,Inter,sans-serif}
header{padding:20px 28px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:12px}
header h1{font-size:18px;margin:0}header span{color:var(--mut)}
main{display:grid;grid-template-columns:420px 1fr;gap:20px;padding:20px 28px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px}
label{display:block;color:var(--mut);font-size:12px;margin:12px 0 4px}
textarea,input,select{width:100%;background:#0b0d11;color:var(--txt);border:1px solid var(--line);border-radius:8px;padding:9px;font:inherit}
textarea{height:300px;resize:vertical}
.row{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px}
button{border:0;border-radius:8px;padding:11px 14px;font:600 14px inherit;cursor:pointer}
.go{background:var(--acc);color:#04130c;width:100%;margin-top:16px}.go:disabled{opacity:.5;cursor:wait}
.sec{background:#232834;color:var(--txt)}
#actions{display:none;gap:8px;margin-bottom:14px;flex-wrap:wrap}
#actions .dl{background:var(--acc);color:#04130c;font-size:15px;padding:13px 18px}
#summary{color:var(--mut);margin-bottom:12px}
.sent{display:grid;grid-template-columns:44px 1fr;gap:10px;padding:12px 0;border-bottom:1px solid var(--line)}
.num{color:var(--mut);font-variant-numeric:tabular-nums}.q{color:var(--mut);font-size:12px}
.st{font-size:11px;padding:2px 7px;border-radius:99px;background:#232834;margin-left:6px}
.found{color:var(--acc)}.nomatch,.blocked{color:var(--bad)}.searching{color:var(--warn)}
.thumbs{display:flex;gap:8px;margin-top:8px;flex-wrap:wrap}.thumbs a{color:var(--mut);font-size:11px;text-decoration:none}
.thumbs img{width:180px;aspect-ratio:16/9;object-fit:cover;border-radius:6px;display:block;background:#000}
#log{font:12px ui-monospace,monospace;color:var(--mut);max-height:160px;overflow:auto;white-space:pre-wrap;margin-top:12px}
.hint{color:var(--mut);font-size:12px;margin-top:6px}.chk{display:flex;gap:6px;align-items:center;margin-top:6px;color:var(--mut);font-size:12px}
.chk input{width:auto}
@media(max-width:900px){main{grid-template-columns:1fr}}
</style></head><body>
<header><h1>Script → B-roll</h1><span>Pexels footage for every sentence</span></header>
<main>
<section class="card">
  <label>Project name</label><input id="name" placeholder="e.g. docket-ep-14">
  <label>Script</label><textarea id="script" placeholder="Paste your narration script here…"></textarea>
  <div class="row">
    <div><label>Format</label><select id="aspect"><option value="16:9">16:9 YouTube</option><option value="9:16">9:16 Shorts</option><option value="1:1">1:1</option><option value="any">Any</option></select></div>
    <div><label>Quality</label><select id="resolution"><option value="1080">1080p</option><option value="720">720p</option><option value="1440">1440p</option><option value="2160">4K</option></select></div>
    <div><label>Clips / sentence</label><select id="clips"><option>1</option><option>2</option><option>3</option></select></div>
  </div>
  <label>Never use footage with these words (comma separated)</label><input id="exclude" placeholder="cartoon, wedding, kids">
  <label>DeepSeek API key (optional — better footage matches)</label>
  <input id="key" type="password" placeholder="sk-…">
  <div class="chk"><input type="checkbox" id="remember"> remember on this computer</div>
  <div class="hint" id="keyhint"></div>
  <button class="go" id="go">Find footage</button>
</section>
<section class="card">
  <div id="actions">
    <button class="dl" id="dl">⬇ Download folder (ZIP)</button>
    <button class="sec" id="open">Open folder</button>
    <button class="sec" id="board">Storyboard</button>
  </div>
  <div id="summary">Paste a script and press <b>Find footage</b>.</div>
  <div id="list"></div>
  <div id="log"></div>
</section>
</main>
<script>
const HAS_KEY = __HAS_KEY__;
const $ = id => document.getElementById(id);
$('keyhint').textContent = HAS_KEY ? 'A saved key was found — leave blank to use it.' : 'Without a key, simple keyword matching is used.';
let job = null, timer = null;
const esc = s => (s||'').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

$('go').onclick = async () => {
  const body = {script:$('script').value, name:$('name').value, aspect:$('aspect').value,
    resolution:$('resolution').value, clips:$('clips').value, exclude:$('exclude').value,
    deepseek_key:$('key').value, remember:$('remember').checked};
  if (!body.script.trim()) { alert('Paste a script first'); return; }
  $('go').disabled = true; $('go').textContent = 'Working…'; $('actions').style.display='none';
  const r = await fetch('/start', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
  const j = await r.json(); if (!r.ok) { alert(j.error); reset(); return; }
  job = j.job; poll();
};
function reset(){ $('go').disabled=false; $('go').textContent='Find footage'; }
async function poll(){
  const s = await (await fetch('/status/'+job)).json();
  render(s);
  if (!s.done) timer = setTimeout(poll, 1500); else { reset(); }
}
function render(s){
  const total = s.sentences.length, found = s.sentences.filter(x=>x.clips.length).length;
  const dl = s.sentences.reduce((a,x)=>a+x.clips.filter(c=>c.status==='downloaded'||c.status==='exists').length,0);
  let sum = total ? `${found}/${total} sentences matched · ${dl} clips downloaded · queries by ${s.ai?'DeepSeek':'keywords'}` : 'Splitting script…';
  if (s.done && s.result) sum = `✅ Done — ${s.result.clips} clips for ${s.result.sentences} sentences` + (s.result.missing.length?` · no match for #${s.result.missing.join(', #')}`:'') + (s.result.blocked?' · ⚠ Pexels blocked the search midway':'');
  if (s.error) sum = '✗ ' + s.error;
  $('summary').innerHTML = sum;
  $('list').innerHTML = s.sentences.map(x => {
    const cls = x.status.replace(' ','');
    const th = x.clips.map(c=>`<a href="${esc(c.url)}" target="_blank"><img src="${esc(c.thumb)}" loading="lazy">${esc(c.res)} · ${c.dur}s · ${esc(c.status)}</a>`).join('');
    return `<div class="sent"><div class="num">#${x.i}</div><div>${esc(x.text)}<span class="st ${cls}">${esc(x.status)}</span>
      <div class="q">${x.used?'<b>'+esc(x.used)+'</b> · ':''}${esc(x.queries.join(' · '))} · ~${x.seconds}s</div><div class="thumbs">${th}</div></div></div>`;
  }).join('');
  $('log').textContent = s.log.slice(-40).join('\n'); $('log').scrollTop = 1e9;
  if (s.done && s.result) $('actions').style.display='flex';
}
$('dl').onclick = () => location.href = '/download/'+job;
$('open').onclick = () => fetch('/open/'+job, {method:'POST'});
$('board').onclick = () => window.open('/storyboard/'+job, '_blank');
</script></body></html>"""


if __name__ == "__main__":
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    port = int(os.getenv("PORT", 5055))
    if not os.getenv("NO_BROWSER"):
        threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    print(f"Script → B-roll running at http://127.0.0.1:{port}  (Ctrl+C to stop)")
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
