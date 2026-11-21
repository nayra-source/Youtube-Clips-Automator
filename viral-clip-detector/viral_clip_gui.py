#!/usr/bin/env python3
"""
viral_clip_gui.py - Desktop UI for the offline Viral Clip Detector.

Run:  python viral_clip_gui.py

Uses only tkinter (bundled with Python) + viral_clip_detector.py (same folder).
"""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import List

from viral_clip_detector import (
    VIDEO_EXTS,
    ClipResult,
    PipelineConfig,
    ViralPipeline,
    check_ffmpeg,
    collect_videos,
)

# ---- palette ---------------------------------------------------------------
BG = "#12141a"
PANEL = "#1b1f29"
FIELD = "#232937"
FG = "#e8ebf2"
MUTED = "#8b93a7"
ACCENT = "#7c5cff"
GOOD = "#2ecc71"
BAD = "#e5484d"


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Viral Clip Detector - 100% Offline")
        self.geometry("1120x780")
        self.minsize(960, 680)
        self.configure(bg=BG)

        # state
        self.sources: List[Path] = []
        self.msg_q: "queue.Queue[tuple]" = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.results: List[ClipResult] = []

        self.source_var = tk.StringVar(value="No input selected")
        self.output_var = tk.StringVar(value=str(Path.cwd() / "viral_clips"))
        self.threshold_var = tk.IntVar(value=75)
        self.whisper_var = tk.StringVar(value="base")
        self.ollama_var = tk.StringVar(value="qwen2.5:3b")
        self.use_llm_var = tk.BooleanVar(value=True)
        self.recursive_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="Ready.")

        self._style()
        self._build()
        self.after(100, self._poll_queue)

        ok, msg = check_ffmpeg()
        self._log(("FFmpeg OK: " + msg) if ok else ("WARNING: " + msg))

    # ------------------------------------------------------------------ style
    def _style(self) -> None:
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure(".", background=BG, foreground=FG, fieldbackground=FIELD, font=("Segoe UI", 10))
        s.configure("Panel.TFrame", background=PANEL)
        s.configure("TLabel", background=BG, foreground=FG)
        s.configure("Panel.TLabel", background=PANEL, foreground=FG)
        s.configure("Muted.TLabel", background=PANEL, foreground=MUTED)
        s.configure("Title.TLabel", background=BG, foreground=FG, font=("Segoe UI", 18, "bold"))
        s.configure("Sub.TLabel", background=BG, foreground=MUTED)
        s.configure("TButton", background=FIELD, foreground=FG, padding=(12, 6), borderwidth=0)
        s.map("TButton", background=[("active", "#2e3547"), ("disabled", "#1a1e28")])
        s.configure("Accent.TButton", background=ACCENT, foreground="white", font=("Segoe UI", 11, "bold"))
        s.map("Accent.TButton", background=[("active", "#6a4bf0"), ("disabled", "#3b3560")])
        s.configure("TCheckbutton", background=PANEL, foreground=FG)
        s.map("TCheckbutton", background=[("active", PANEL)])
        s.configure("TEntry", fieldbackground=FIELD, foreground=FG, insertcolor=FG, borderwidth=0)
        s.configure("TCombobox", fieldbackground=FIELD, background=FIELD, foreground=FG, arrowcolor=FG)
        s.configure("Horizontal.TProgressbar", troughcolor=FIELD, background=ACCENT, borderwidth=0)
        s.configure("Horizontal.TScale", background=PANEL, troughcolor=FIELD)
        s.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=FG,
                    rowheight=26, borderwidth=0)
        s.configure("Treeview.Heading", background=FIELD, foreground=FG, relief="flat",
                    font=("Segoe UI", 10, "bold"))
        s.map("Treeview", background=[("selected", "#34406b")])

    # ----------------------------------------------------------------- layout
    def _build(self) -> None:
        head = ttk.Frame(self)
        head.pack(fill="x", padx=18, pady=(14, 6))
        ttk.Label(head, text="Viral Clip Detector", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            head,
            text="Audio energy (35%) + Text hook (40%) + Visual dynamics (25%)  |  free, local, no API keys",
            style="Sub.TLabel",
        ).pack(anchor="w")

        # --- I/O panel
        io = ttk.Frame(self, style="Panel.TFrame", padding=14)
        io.pack(fill="x", padx=18, pady=6)
        io.columnconfigure(1, weight=1)

        ttk.Label(io, text="INPUT", style="Muted.TLabel").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(io, textvariable=self.source_var, state="readonly").grid(
            row=0, column=1, sticky="ew", padx=10, pady=4)
        btns = ttk.Frame(io, style="Panel.TFrame")
        btns.grid(row=0, column=2, sticky="e")
        ttk.Button(btns, text="Select Folder...", command=self.choose_input_folder).pack(side="left", padx=2)
        ttk.Button(btns, text="Select Videos...", command=self.choose_input_files).pack(side="left", padx=2)
        ttk.Button(btns, text="Clear", command=self.clear_input).pack(side="left", padx=2)

        ttk.Label(io, text="OUTPUT", style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(io, textvariable=self.output_var).grid(row=1, column=1, sticky="ew", padx=10, pady=4)
        btns2 = ttk.Frame(io, style="Panel.TFrame")
        btns2.grid(row=1, column=2, sticky="e")
        ttk.Button(btns2, text="Browse...", command=self.choose_output_folder).pack(side="left", padx=2)
        ttk.Button(btns2, text="Open Folder", command=self.open_output_folder).pack(side="left", padx=2)

        # --- settings panel
        st = ttk.Frame(self, style="Panel.TFrame", padding=14)
        st.pack(fill="x", padx=18, pady=6)

        ttk.Label(st, text="Threshold", style="Panel.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Scale(st, from_=0, to=100, orient="horizontal", variable=self.threshold_var,
                  command=lambda _v: self.threshold_lbl.config(text=str(int(self.threshold_var.get()))),
                  length=240).grid(row=0, column=1, padx=10)
        self.threshold_lbl = ttk.Label(st, text="75", style="Panel.TLabel", width=4)
        self.threshold_lbl.grid(row=0, column=2, sticky="w")

        ttk.Label(st, text="Whisper model", style="Panel.TLabel").grid(row=0, column=3, padx=(24, 6))
        ttk.Combobox(st, textvariable=self.whisper_var, width=10, state="readonly",
                     values=["tiny", "base", "small", "medium", "large-v3"]).grid(row=0, column=4)

        ttk.Label(st, text="Ollama model", style="Panel.TLabel").grid(row=0, column=5, padx=(24, 6))
        ttk.Entry(st, textvariable=self.ollama_var, width=16).grid(row=0, column=6)

        ttk.Checkbutton(st, text="Use Ollama LLM for hook scoring", variable=self.use_llm_var).grid(
            row=1, column=0, columnspan=4, sticky="w", pady=(10, 0))
        ttk.Checkbutton(st, text="Include sub-folders", variable=self.recursive_var,
                        command=self._refresh_source_label).grid(
            row=1, column=4, columnspan=3, sticky="w", pady=(10, 0))

        # --- action row
        act = ttk.Frame(self)
        act.pack(fill="x", padx=18, pady=(6, 2))
        self.start_btn = ttk.Button(act, text="Start Analysis", style="Accent.TButton", command=self.start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(act, text="Stop", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=8)
        ttk.Label(act, textvariable=self.status_var, style="Sub.TLabel").pack(side="left", padx=12)

        self.progress = ttk.Progressbar(self, mode="determinate")
        self.progress.pack(fill="x", padx=18, pady=(4, 8))

        # --- results table + details
        mid = ttk.Frame(self)
        mid.pack(fill="both", expand=True, padx=18)
        cols = ("score", "text", "audio", "visual", "wpm", "pass", "file")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", height=10)
        heads = {"score": ("SCORE", 70), "text": ("TEXT", 65), "audio": ("AUDIO", 65),
                 "visual": ("VISUAL", 70), "wpm": ("WPM", 60), "pass": ("PASS", 60), "file": ("FILE", 520)}
        for c in cols:
            self.tree.heading(c, text=heads[c][0])
            self.tree.column(c, width=heads[c][1], anchor="w" if c == "file" else "center")
        self.tree.tag_configure("pass", foreground=GOOD)
        self.tree.tag_configure("fail", foreground=MUTED)
        self.tree.tag_configure("error", foreground=BAD)
        vs = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vs.pack(side="left", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Double-1>", self._on_double_click)

        self.detail_var = tk.StringVar(value="Select a clip to see its opening hook. Double-click to open it.")
        ttk.Label(self, textvariable=self.detail_var, style="Sub.TLabel", wraplength=1060,
                  justify="left").pack(fill="x", padx=18, pady=6)

        # --- log
        self.log_box = tk.Text(self, height=7, bg=PANEL, fg=MUTED, relief="flat", wrap="word",
                               insertbackground=FG, font=("Consolas", 9))
        self.log_box.pack(fill="x", padx=18, pady=(0, 14))
        self.log_box.configure(state="disabled")

    # ------------------------------------------------- input/output functions
    def choose_input_folder(self) -> None:
        """Define the input source as a whole folder of clips."""
        folder = filedialog.askdirectory(title="Select folder with video clips")
        if folder:
            self.sources = [Path(folder)]
            self._refresh_source_label()

    def choose_input_files(self) -> None:
        """Define the input source as one or more individual video files."""
        exts = " ".join(f"*{e}" for e in sorted(VIDEO_EXTS))
        files = filedialog.askopenfilenames(
            title="Select video clips",
            filetypes=[("Video files", exts), ("All files", "*.*")],
        )
        if files:
            self.sources = [Path(f) for f in files]
            self._refresh_source_label()

    def clear_input(self) -> None:
        self.sources = []
        self._refresh_source_label()

    def choose_output_folder(self) -> None:
        """Define where the top-scoring clips and viral_report.csv are saved."""
        folder = filedialog.askdirectory(title="Select output folder")
        if folder:
            self.output_var.set(folder)

    def open_output_folder(self) -> None:
        out = Path(self.output_var.get()).expanduser()
        out.mkdir(parents=True, exist_ok=True)
        self._open_path(out)

    def _refresh_source_label(self) -> None:
        if not self.sources:
            self.source_var.set("No input selected")
            return
        vids = collect_videos(self.sources, self.recursive_var.get())
        if len(self.sources) == 1 and self.sources[0].is_dir():
            self.source_var.set(f"{self.sources[0]}   ({len(vids)} video(s))")
        else:
            self.source_var.set(f"{len(self.sources)} file(s) selected   ({len(vids)} valid video(s))")

    @staticmethod
    def _open_path(path: Path) -> None:
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Open failed", str(exc))

    # ---------------------------------------------------------------- run/stop
    def start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        if not self.sources:
            messagebox.showwarning("No input", "Select an input folder or video files first.")
            return
        out = Path(self.output_var.get()).expanduser()
        videos = collect_videos(self.sources, self.recursive_var.get(), exclude_dir=out)
        if not videos:
            messagebox.showwarning("No videos", "No supported video files found in the input.")
            return
        ok, msg = check_ffmpeg()
        if not ok:
            messagebox.showerror("FFmpeg missing", msg)
            return

        cfg = PipelineConfig(
            sources=self.sources,
            output_dir=out,
            threshold=int(self.threshold_var.get()),
            whisper_model=self.whisper_var.get(),
            ollama_model=self.ollama_var.get().strip() or "qwen2.5:3b",
            use_llm=self.use_llm_var.get(),
            recursive=self.recursive_var.get(),
        )
        self.tree.delete(*self.tree.get_children())
        self.results.clear()
        self.cancel_event.clear()
        self.progress.configure(maximum=len(videos), value=0)
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.status_var.set("Starting...")

        q = self.msg_q
        pipe = ViralPipeline(
            cfg,
            log_cb=lambda m: q.put(("log", m)),
            progress_cb=lambda d, t, n, s: q.put(("progress", d, t, n, s)),
            result_cb=lambda r: q.put(("result", r)),
            cancel_event=self.cancel_event,
        )

        def work() -> None:
            try:
                res = pipe.run(videos)
                q.put(("done", res))
            except Exception as exc:  # noqa: BLE001
                q.put(("error", str(exc)))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def stop(self) -> None:
        self.cancel_event.set()
        self.status_var.set("Stopping after current clip...")

    # ------------------------------------------------------------ queue pump
    def _poll_queue(self) -> None:
        try:
            while True:
                msg = self.msg_q.get_nowait()
                kind = msg[0]
                if kind == "log":
                    self._log(msg[1])
                elif kind == "progress":
                    _, done, total, name, stage = msg
                    base = done - 1 if stage != "Done" else done
                    self.progress.configure(value=max(base, 0))
                    self.status_var.set(f"[{done}/{total}] {name} - {stage}")
                elif kind == "result":
                    self._add_result(msg[1])
                elif kind == "done":
                    self._finish(msg[1])
                elif kind == "error":
                    self._log("ERROR: " + msg[1])
                    messagebox.showerror("Analysis failed", msg[1])
                    self._reset_buttons("Failed.")
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _add_result(self, r: ClipResult) -> None:
        self.results.append(r)
        self._insert_row(r)

    def _insert_row(self, r: ClipResult) -> None:
        tag = "error" if r.error else ("pass" if r.passed else "fail")
        self.tree.insert(
            "", "end", iid=str(id(r)), tags=(tag,),
            values=(r.final_score, f"{r.text_score:.0f}", f"{r.audio_score:.0f}",
                    f"{r.visual_score:.0f}", f"{r.wpm:.0f}",
                    "YES" if r.passed else ("ERR" if r.error else "no"), r.filename),
        )

    def _finish(self, results: List[ClipResult]) -> None:
        self.tree.delete(*self.tree.get_children())
        self.results = list(results)          # already sorted by score
        for r in self.results:
            self._insert_row(r)
        passed = sum(1 for r in results if r.passed)
        self.progress.configure(value=self.progress["maximum"])
        self._reset_buttons(f"Done - {passed}/{len(results)} clips passed. Report saved in output folder.")

    def _reset_buttons(self, status: str) -> None:
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self.status_var.set(status)

    # ----------------------------------------------------------- interactions
    def _selected_result(self) -> ClipResult | None:
        sel = self.tree.selection()
        if not sel:
            return None
        for r in self.results:
            if str(id(r)) == sel[0]:
                return r
        return None

    def _on_select(self, _e=None) -> None:
        r = self._selected_result()
        if r:
            hook = r.hook_text or "(no speech in first 5 seconds)"
            err = f"   |   Error: {r.error}" if r.error else ""
            self.detail_var.set(f'Hook ({r.hook_mode}): "{hook}"{err}')

    def _on_double_click(self, _e=None) -> None:
        r = self._selected_result()
        if r and r.path.exists():
            self._open_path(r.path)

    def _log(self, text: str) -> None:
        self.log_box.configure(state="normal")
        self.log_box.insert("end", text + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")


def main() -> None:
    App().mainloop()


if __name__ == "__main__":
    main()
