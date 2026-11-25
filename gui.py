#!/usr/bin/env python3
"""
Pexels B-roll Finder — desktop app.

Double-click "Start (Mac).command" or "Start (Windows).bat" — or run:  python gui.py
"""
from __future__ import annotations

import warnings
warnings.filterwarnings("ignore", message=".*OpenSSL.*")      # Apple's Python 3.9 + urllib3 v2 notice

import json
import os
import platform
import queue
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import pexels_scraper as ps
import script_broll as sb
import updater

APP_NAME = "Pexels B-roll Finder"
CONFIG = Path.home() / ".pexels_broll.json"
DEFAULT_OUT = Path.home() / "Pexels B-roll"
SITE_BASE = os.getenv("PEXELS_SITE", ps.SITE)          # override only for testing
IS_MAC = platform.system() == "Darwin"

FORMATS = {"16:9  YouTube": ("landscape", 16 / 9), "9:16  Shorts / Reels": ("portrait", 9 / 16),
           "1:1  Square": ("square", 1.0), "Any shape": (None, None)}
QUALITIES = {"1080p Full HD": 1080, "720p HD": 720, "1440p 2K": 1440, "2160p 4K": 2160}


def load_cfg() -> dict:
    try:
        return json.loads(CONFIG.read_text())
    except Exception:  # noqa: BLE001
        return {}


def save_cfg(cfg: dict):
    try:
        CONFIG.write_text(json.dumps(cfg, indent=2))
        CONFIG.chmod(0o600)
    except Exception:  # noqa: BLE001
        pass


def open_path(p: str):
    if IS_MAC:
        subprocess.Popen(["open", p])
    elif platform.system() == "Windows":
        os.startfile(p)  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", p])


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1280x820")
        self.minsize(1000, 640)
        self.cfg = load_cfg()
        self.q: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.result: dict | None = None
        self.rows: dict[int, dict] = {}
        self._style()
        self._build()
        self.after(150, self._drain)
        self.after(1500, lambda: self._check_updates(silent=True))
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ UI
    def _style(self):
        st = ttk.Style(self)
        if not IS_MAC and "clam" in st.theme_names() and platform.system() != "Windows":
            st.theme_use("clam")
        base = ("Helvetica Neue", 13) if IS_MAC else ("Segoe UI", 10)
        self.option_add("*Font", base)
        st.configure("Title.TLabel", font=(base[0], base[1] + 6, "bold"))
        st.configure("Muted.TLabel", foreground="#777")
        st.configure("Go.TButton", font=(base[0], base[1] + 1, "bold"), padding=(14, 8))
        st.configure("Big.TButton", font=(base[0], base[1] + 1, "bold"), padding=(12, 7))
        st.configure("Treeview", rowheight=26)
        st.configure("Horizontal.TProgressbar", background="#2ecc71", troughcolor="#e6e6e6")
        st.configure("Treeview.Heading", font=(base[0], base[1], "bold"))

    def _build(self):
        outer = ttk.Frame(self, padding=14)
        outer.pack(fill="both", expand=True)
        head = ttk.Frame(outer)
        head.pack(fill="x", pady=(0, 10))
        ttk.Label(head, text=APP_NAME, style="Title.TLabel").pack(side="left")
        ttk.Label(head, text="   paste a script → footage for every sentence", style="Muted.TLabel").pack(side="left")
        self.upd_btn = ttk.Button(head, text="⟳  Check for updates", command=self._update_clicked)
        self.upd_btn.pack(side="right")
        self.ver_lbl = ttk.Label(head, text=updater.version_label(), style="Muted.TLabel")
        self.ver_lbl.pack(side="right", padx=10)
        self._update_info = None

        panes = ttk.PanedWindow(outer, orient="horizontal")
        panes.pack(fill="both", expand=True)
        left = ttk.Frame(panes, padding=(0, 0, 12, 0))
        right = ttk.Frame(panes, padding=(12, 0, 0, 0))
        panes.add(left, weight=2)
        panes.add(right, weight=3)
        self.after(200, lambda: panes.sashpos(0, int(self.winfo_width() * 0.40)))

        # ---- left: inputs
        g = ttk.Frame(left)
        g.pack(fill="x")
        g.columnconfigure(1, weight=1)
        r = 0
        ttk.Label(g, text="Project name").grid(row=r, column=0, sticky="w", pady=3)
        self.name_var = tk.StringVar(value=time.strftime("project-%Y%m%d-%H%M"))
        ttk.Entry(g, textvariable=self.name_var).grid(row=r, column=1, columnspan=2, sticky="ew", pady=3)
        r += 1
        ttk.Label(g, text="Save to").grid(row=r, column=0, sticky="w", pady=3)
        self.out_var = tk.StringVar(value=self.cfg.get("out_dir", str(DEFAULT_OUT)))
        ttk.Entry(g, textvariable=self.out_var).grid(row=r, column=1, sticky="ew", pady=3)
        ttk.Button(g, text="Browse…", command=self._pick_out).grid(row=r, column=2, padx=(6, 0))
        r += 1

        opts = ttk.Frame(g)
        opts.grid(row=r, column=0, columnspan=3, sticky="ew", pady=(8, 3))
        for c in range(4):
            opts.columnconfigure(c, weight=1)
        ttk.Label(opts, text="Format").grid(row=0, column=0, sticky="w")
        ttk.Label(opts, text="Quality").grid(row=0, column=1, sticky="w")
        ttk.Label(opts, text="Clips / sentence").grid(row=0, column=2, sticky="w")
        self.fmt_var = tk.StringVar(value=self.cfg.get("format", "16:9  YouTube"))
        self.q_var = tk.StringVar(value=self.cfg.get("quality", "1080p Full HD"))
        self.clips_var = tk.StringVar(value=str(self.cfg.get("clips", 1)))
        ttk.Combobox(opts, textvariable=self.fmt_var, values=list(FORMATS), state="readonly", width=18)\
            .grid(row=1, column=0, sticky="ew", padx=(0, 6))
        ttk.Combobox(opts, textvariable=self.q_var, values=list(QUALITIES), state="readonly", width=14)\
            .grid(row=1, column=1, sticky="ew", padx=(0, 6))
        ttk.Spinbox(opts, from_=1, to=4, textvariable=self.clips_var, width=5, state="readonly")\
            .grid(row=1, column=2, sticky="w")
        r += 1
        ttk.Label(g, text="Never use footage with").grid(row=r, column=0, sticky="w", pady=3)
        self.excl_var = tk.StringVar(value=self.cfg.get("exclude", ""))
        ttk.Entry(g, textvariable=self.excl_var).grid(row=r, column=1, columnspan=2, sticky="ew", pady=3)
        r += 1
        ttk.Label(g, text="(comma separated, e.g. cartoon, wedding, kids)", style="Muted.TLabel")\
            .grid(row=r, column=1, columnspan=2, sticky="w")
        r += 1
        self.chrome_var = tk.BooleanVar(value=self.cfg.get("use_chrome", True))
        ttk.Checkbutton(g, text="Use Chrome for Pexels  (recommended)", variable=self.chrome_var)\
            .grid(row=r, column=0, columnspan=3, sticky="w", pady=(6, 0))
        r += 1

        # DeepSeek
        ds = ttk.LabelFrame(left, text="  DeepSeek API key  (smarter footage matching)  ", padding=10)
        ds.pack(fill="x", pady=(10, 8))
        ds.columnconfigure(0, weight=1)
        self.key_var = tk.StringVar(value=self.cfg.get("deepseek_key", os.getenv("DEEPSEEK_API_KEY", "")))
        self.key_entry = ttk.Entry(ds, textvariable=self.key_var, show="•")
        self.key_entry.grid(row=0, column=0, sticky="ew")
        self.show_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(ds, text="Show", variable=self.show_var,
                        command=lambda: self.key_entry.config(show="" if self.show_var.get() else "•"))\
            .grid(row=0, column=1, padx=6)
        ttk.Button(ds, text="Test key", command=self._test_key).grid(row=0, column=2)
        self.remember_var = tk.BooleanVar(value=self.cfg.get("remember_key", True))
        ttk.Checkbutton(ds, text="Remember on this computer", variable=self.remember_var)\
            .grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.key_status = ttk.Label(ds, text="Get a key at platform.deepseek.com → API keys. "
                                             "Leave empty to use simple keyword matching.",
                                    style="Muted.TLabel", wraplength=420)
        self.key_status.grid(row=2, column=0, columnspan=3, sticky="w", pady=(4, 0))

        # Script
        sh = ttk.Frame(left)
        sh.pack(fill="x")
        ttk.Label(sh, text="Script").pack(side="left")
        ttk.Button(sh, text="Clear", command=lambda: (self.script.delete("1.0", "end"), self._count())).pack(side="right")
        ttk.Button(sh, text="Load .txt…", command=self._load_txt).pack(side="right", padx=6)
        tf = ttk.Frame(left)
        tf.pack(fill="both", expand=True, pady=(4, 8))
        self.script = tk.Text(tf, wrap="word", undo=True, relief="solid", borderwidth=1,
                              padx=8, pady=8, height=12)
        sb_ = ttk.Scrollbar(tf, command=self.script.yview)
        self.script.configure(yscrollcommand=sb_.set)
        self.script.pack(side="left", fill="both", expand=True)
        sb_.pack(side="right", fill="y")
        self.script.bind("<<Modified>>", self._on_modified)
        self.count_lbl = ttk.Label(left, text="", style="Muted.TLabel")
        self.count_lbl.pack(anchor="w", pady=(0, 6))

        btns = ttk.Frame(left)
        btns.pack(fill="x")
        self.go_btn = ttk.Button(btns, text="▶  Find footage", style="Go.TButton", command=self._start)
        self.go_btn.pack(side="left", fill="x", expand=True)
        self.stop_btn = ttk.Button(btns, text="■ Stop", command=self._stop, state="disabled")
        self.stop_btn.pack(side="left", padx=(8, 0))

        # ---- right: progress
        top = ttk.Frame(right)
        top.pack(fill="x")
        self.summary = ttk.Label(top, text="Paste a script on the left and press Find footage.",
                                 wraplength=640)
        self.summary.pack(side="left", fill="x", expand=True)
        self.pvar = tk.DoubleVar(value=0)
        self.pbar = ttk.Progressbar(right, mode="determinate", maximum=1000, variable=self.pvar)
        self.pbar.pack(fill="x", pady=8)

        self.actions = ttk.Frame(right)
        self.actions.pack(fill="x", pady=(0, 8))
        self.dl_btn = ttk.Button(self.actions, text="⬇  Download folder (ZIP)", style="Big.TButton",
                                 command=self._save_zip, state="disabled")
        self.dl_btn.pack(side="left")
        self.open_btn = ttk.Button(self.actions, text="Open folder", style="Big.TButton",
                                   command=self._open_folder, state="disabled")
        self.open_btn.pack(side="left", padx=8)
        self.board_btn = ttk.Button(self.actions, text="Storyboard", style="Big.TButton",
                                    command=self._open_board, state="disabled")
        self.board_btn.pack(side="left")

        tv_frame = ttk.Frame(right)
        tv_frame.pack(fill="both", expand=True)
        cols = ("n", "sentence", "query", "clips", "status")
        self.tv = ttk.Treeview(tv_frame, columns=cols, show="headings", selectmode="browse")
        for c, t, w, st in (("n", "#", 36, False), ("sentence", "Sentence", 240, True),
                            ("query", "Footage search", 140, False), ("clips", "Clips", 140, False),
                            ("status", "Status", 80, False)):
            self.tv.heading(c, text=t, anchor="w")
            self.tv.column(c, width=w, stretch=st, anchor="w")
        self.tv.tag_configure("found", foreground="#1a8f5a")
        self.tv.tag_configure("bad", foreground="#c0392b")
        self.tv.tag_configure("busy", foreground="#b9770e")
        vs = ttk.Scrollbar(tv_frame, command=self.tv.yview)
        self.tv.configure(yscrollcommand=vs.set)
        self.tv.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        self.tv.bind("<Double-1>", self._open_row)
        ttk.Label(right, text="Double-click a row to play its clip (or open it on Pexels).",
                  style="Muted.TLabel").pack(anchor="w", pady=(4, 2))

        self.log = tk.Text(right, height=7, wrap="word", relief="flat", background="#f4f4f4",
                           foreground="#555", padx=6, pady=4, state="disabled",
                           font=("Menlo", 11) if IS_MAC else ("Consolas", 9))
        self.log.pack(fill="x", pady=(4, 0))

    # -------------------------------------------------------------- helpers
    def _on_modified(self, _=None):
        self.script.edit_modified(False)
        self._count()

    def _count(self):
        txt = self.script.get("1.0", "end").strip()
        if not txt:
            self.count_lbl.config(text="")
            return
        n = len(sb.split_script(txt))
        words = len(txt.split())
        self.count_lbl.config(text=f"{n} sentences · {words} words · ~{words / 150:.1f} min narration")

    def _pick_out(self):
        d = filedialog.askdirectory(initialdir=self.out_var.get() or str(Path.home()))
        if d:
            self.out_var.set(d)

    def _load_txt(self):
        f = filedialog.askopenfilename(filetypes=[("Text", "*.txt *.md"), ("All files", "*.*")])
        if f:
            self.script.delete("1.0", "end")
            self.script.insert("1.0", Path(f).read_text(encoding="utf-8", errors="ignore"))
            if self.name_var.get().startswith("project-"):
                self.name_var.set(Path(f).stem)
            self._count()

    def _log(self, msg: str):
        self.log.config(state="normal")
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.log.config(state="disabled")

    def _persist(self):
        self.cfg.update(out_dir=self.out_var.get(), format=self.fmt_var.get(), quality=self.q_var.get(),
                        clips=int(self.clips_var.get() or 1), exclude=self.excl_var.get(),
                        use_chrome=bool(self.chrome_var.get()))
        self.cfg["remember_key"] = bool(self.remember_var.get())
        if self.remember_var.get() and self.key_var.get().strip():
            self.cfg["deepseek_key"] = self.key_var.get().strip()
        elif not self.remember_var.get():
            self.cfg.pop("deepseek_key", None)
        save_cfg(self.cfg)

    # -------------------------------------------------------------- DeepSeek test
    def _test_key(self):
        key = self.key_var.get().strip()
        self.key_status.config(text="Testing…", foreground="#777")
        def run():
            ok, msg = sb.test_deepseek_key(key)
            self.q.put(("key", (ok, msg)))
        threading.Thread(target=run, daemon=True).start()

    # -------------------------------------------------------------- run
    def _start(self):
        script = self.script.get("1.0", "end").strip()
        if not script:
            messagebox.showinfo(APP_NAME, "Paste your script first.")
            return
        out_dir = Path(self.out_var.get()).expanduser()
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror(APP_NAME, f"Can't use that folder:\n{e}")
            return
        self._persist()
        orient, aspect = FORMATS[self.fmt_var.get()]
        st = sb.Settings(orientation=orient, aspect=aspect, resolution=QUALITIES[self.q_var.get()],
                         clips_per_sentence=int(self.clips_var.get() or 1),
                         exclude=[w.strip() for w in self.excl_var.get().split(",") if w.strip()],
                         use_browser=bool(self.chrome_var.get()))
        key = self.key_var.get().strip()
        name = self.name_var.get().strip() or time.strftime("project-%Y%m%d-%H%M")
        self._job_folder = out_dir / sb.slug(name, 50)

        self.result = None
        self.rows.clear()
        self.tv.delete(*self.tv.get_children())
        for b in (self.dl_btn, self.open_btn, self.board_btn):
            b.config(state="disabled")
        self.go_btn.config(state="disabled", text="Working…")
        self.stop_btn.config(state="normal")
        self.stop_event.clear()
        self.pvar.set(0)
        self.summary.config(text=("Asking DeepSeek for search ideas…" if key else "Analysing script…"))
        self._log(f"── {name} ──")

        def log(m):
            self.q.put(("log", m))

        def upd(results):
            snap = [(sr.index, sr.text, sr.query_used or (sr.queries[0] if sr.queries else ""), sr.status,
                     [(v.status, v.local_path, v.pexels_url,
                       f"{v.chosen_file.width}x{v.chosen_file.height}" if v.chosen_file else "", v.duration)
                      for v in sr.clips]) for sr in results]
            self.q.put(("update", snap))

        def work():
            try:
                res = sb.run_project(script, out_dir, name, st, key or None, SITE_BASE, log, upd, self.stop_event)
                self.q.put(("done", res))
            except sb.Cancelled:
                self.q.put(("stopped", None))
            except Exception as e:  # noqa: BLE001
                self.q.put(("error", str(e)))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _stop(self):
        self.stop_event.set()
        self.stop_btn.config(state="disabled")
        self.summary.config(text="Stopping after the current step…")

    # -------------------------------------------------------------- queue → UI
    def _drain(self):
        try:
            while True:
                kind, data = self.q.get_nowait()
                getattr(self, f"_on_{kind}")(data)
        except queue.Empty:
            pass
        self.after(150, self._drain)

    def _on_log(self, msg):
        self._log(msg)
        if msg.startswith("⚠"):
            self.summary.config(text=msg, foreground="#b9770e")
            self.bell()
        elif msg.startswith("✓ Check passed"):
            self.summary.config(text="Check passed — searching…", foreground="")

    def _on_key(self, data):
        ok, msg = data
        self.key_status.config(text=msg, foreground="#1a8f5a" if ok else "#c0392b")

    def _on_update(self, snap):
        total = len(snap)
        searched = sum(1 for s in snap if s[3] not in ("pending", "searching"))
        clips = [c for s in snap for c in s[4]]
        done_dl = sum(1 for c in clips if c[0] in ("downloaded", "exists"))
        # progress: searching = first 60%, downloading = last 40%
        frac = 0.6 * searched / max(total, 1) + (0.4 * done_dl / max(len(clips), 1) if searched == total else 0)
        self.pvar.set(frac * 1000)
        stage = "Downloading" if searched == total and clips else "Searching"
        self.summary.config(foreground="")
        self.summary.config(text=f"{stage}…  {searched}/{total} sentences searched · "
                                 f"{sum(1 for s in snap if s[4])} matched · {done_dl}/{len(clips)} clips downloaded")
        for idx, text, query, status, cl in snap:
            got = sum(1 for c in cl if c[0] in ("downloaded", "exists"))
            clips_txt = (f"{got}/{len(cl)} ↓  {cl[0][3]}" if cl else "—")
            tag = "found" if cl else ("bad" if status in ("no match", "blocked") or status.startswith("error")
                                      else "busy" if status == "searching" else "")
            vals = (idx, text, query, clips_txt, status)
            iid = str(idx)
            if self.tv.exists(iid):
                self.tv.item(iid, values=vals, tags=(tag,))
            else:
                self.tv.insert("", "end", iid=iid, values=vals, tags=(tag,))
            self.rows[idx] = {"clips": cl}
            if status == "searching":
                self.tv.see(iid)

    def _finish(self):
        self.go_btn.config(state="normal", text="▶  Find footage")
        self.stop_btn.config(state="disabled")

    def _on_done(self, res):
        self._finish()
        self.result = res
        self.pvar.set(1000)
        txt = f"✅ Done — {res['clips']} clips for {res['sentences']} sentences."
        if res["missing"]:
            txt += f"  No footage for #{', #'.join(map(str, res['missing']))}."
        if res.get("blocked"):
            txt += "  ⚠ Pexels blocked the search partway — try again later."
        self.summary.config(text=txt)
        for b in (self.dl_btn, self.open_btn, self.board_btn):
            b.config(state="normal")
        self.bell()

    def _on_stopped(self, _):
        self._finish()
        self.summary.config(text="Stopped. Clips already downloaded are in the project's clips folder.")
        folder = getattr(self, "_job_folder", None)
        if folder and folder.exists():
            self.result = {"folder": str(folder)}
            self.open_btn.config(state="normal")

    def _on_error(self, msg):
        self._finish()
        self.summary.config(text=f"✗ {msg}")
        messagebox.showerror(APP_NAME, msg)

    # -------------------------------------------------------------- updates
    def _check_updates(self, silent: bool):
        def run():
            try:
                self.q.put(("upd_check", (updater.check(), silent)))
            except Exception as e:  # noqa: BLE001
                self.q.put(("upd_check", ({"error": str(e)}, silent)))
        threading.Thread(target=run, daemon=True).start()

    def _on_upd_check(self, data):
        info, silent = data
        self._update_info = info
        if info.get("error"):
            self.upd_btn.config(text="⟳  Check for updates", style="TButton")
            if not silent:
                messagebox.showwarning(APP_NAME, f"Couldn't check for updates:\n{info['error']}")
            return
        if info["available"]:
            self.upd_btn.config(text="⬆  Update available", style="Go.TButton")
            if not silent:
                self._update_clicked()
        else:
            self.upd_btn.config(text="✓  Up to date", style="TButton")
            if not silent:
                messagebox.showinfo(APP_NAME, "You already have the latest version.")

    def _update_clicked(self):
        info = self._update_info
        if not info or info.get("error") or not info.get("available"):
            self.upd_btn.config(text="Checking…")
            self._check_updates(silent=False)
            return
        if self.worker and self.worker.is_alive():
            messagebox.showinfo(APP_NAME, "Wait for the current job to finish (or Stop it) before updating.")
            return
        new = info["latest"]
        if not messagebox.askyesno(APP_NAME, f"Install the latest version?\n\n{new['message']}\n"
                                             f"({new['sha'][:7]}, {new['date'][:10]})\n\n"
                                             "Your projects, settings and DeepSeek key are kept. "
                                             "The app restarts afterwards."):
            return
        self.upd_btn.config(state="disabled", text="Updating…")
        self.go_btn.config(state="disabled")
        def run():
            try:
                updater.update(log=lambda m: self.q.put(("log", m)))
                self.q.put(("upd_done", None))
            except Exception as e:  # noqa: BLE001
                self.q.put(("upd_failed", str(e)))
        threading.Thread(target=run, daemon=True).start()

    def _on_upd_done(self, _):
        self._persist()
        if os.getenv("BROLL_NO_RESTART"):
            self.upd_btn.config(state="normal", text="✓  Updated")
            self.go_btn.config(state="normal")
            return
        messagebox.showinfo(APP_NAME, "Update installed. The app will now restart.")
        self.destroy()
        updater.restart_app(str(Path(__file__).resolve()))

    def _on_upd_failed(self, msg):
        self.upd_btn.config(state="normal", text="⟳  Check for updates")
        self.go_btn.config(state="normal")
        messagebox.showerror(APP_NAME, f"Update failed:\n{msg}\n\nIf the app acts strangely, download a fresh copy from github.com/haseeb109/pexel-scrapper.")

    # -------------------------------------------------------------- outputs
    def _save_zip(self):
        if not self.result or not self.result.get("folder"):
            return
        folder = Path(self.result["folder"])
        dest = filedialog.asksaveasfilename(initialfile=folder.name + ".zip", defaultextension=".zip",
                                            initialdir=str(Path.home() / "Downloads"),
                                            filetypes=[("ZIP", "*.zip")])
        if not dest:
            return
        self.dl_btn.config(state="disabled", text="Zipping…")
        def run():
            try:
                out = sb.make_zip(folder, dest, progress=lambda n, t: self.q.put(("zip_progress", (n, t))))
                self.q.put(("zip_done", out))
            except Exception as e:  # noqa: BLE001
                self.q.put(("zip_failed", str(e)))
        threading.Thread(target=run, daemon=True).start()

    def _on_zip_progress(self, data):
        n, t = data
        self.dl_btn.config(text=f"Zipping… {n}/{t}")

    def _on_zip_done(self, path):
        self.dl_btn.config(state="normal", text="⬇  Download folder (ZIP)")
        self._log(f"Saved ZIP → {path}")
        if messagebox.askyesno(APP_NAME, "ZIP saved. Show it in the folder?"):
            open_path(str(Path(path).parent))

    def _on_zip_failed(self, msg):
        self.dl_btn.config(state="normal", text="⬇  Download folder (ZIP)")
        messagebox.showerror(APP_NAME, f"Couldn't create the ZIP:\n{msg}")

    def _open_folder(self):
        if self.result and self.result.get("folder"):
            open_path(self.result["folder"])

    def _open_board(self):
        if self.result and self.result.get("folder"):
            webbrowser.open(Path(self.result["folder"], "storyboard.html").as_uri())

    def _open_row(self, _):
        sel = self.tv.selection()
        if not sel:
            return
        row = self.rows.get(int(sel[0]))
        if not row or not row["clips"]:
            return
        status, path, url, *_ = row["clips"][0]
        if path and Path(path).exists():
            open_path(path)
        elif url:
            webbrowser.open(url)

    def _on_close(self):
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno(APP_NAME, "A job is still running. Quit anyway?"):
                return
            self.stop_event.set()
        self._persist()
        self.destroy()


ERROR_LOG = Path.home() / "pexels_broll_error.log"


def _log_crash(text: str):
    try:
        with open(ERROR_LOG, "a", encoding="utf-8") as fh:
            fh.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n{text}\n")
    except OSError:
        pass


def main():
    import traceback
    if sys.stderr is None:                      # Windows pythonw: keep errors somewhere readable
        sys.stderr = open(ERROR_LOG, "a", encoding="utf-8")
    try:
        app = App()
    except Exception:  # noqa: BLE001
        _log_crash(traceback.format_exc())
        raise

    def report(exc, val, tb):
        text = "".join(traceback.format_exception(exc, val, tb))
        _log_crash(text)
        messagebox.showerror(APP_NAME, f"Something went wrong:\n{val}\n\nDetails were saved to {ERROR_LOG}")
    app.report_callback_exception = report
    app.mainloop()


if __name__ == "__main__":
    main()
