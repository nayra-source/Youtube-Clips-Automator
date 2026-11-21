#!/usr/bin/env python3
"""
Auto Captioner (GUI)
====================
Free, offline auto-captioning for videos. No API keys, no cloud, no subscriptions.

How it works
  1. Load a video (ffprobe reads its length, codecs and size)
  2. Choose what to create: burned-in captions, an .srt subtitle file, or both
  3. Pick the Whisper model size, spoken language and caption style (live preview)
  4. Generate: ffmpeg extracts the audio, faster-whisper transcribes it on this computer,
     an .srt is written and/or the captions are burned into a new video with ffmpeg

Features
  * Same modern white and blue UI as Video Clipper: progress bar with ETA, status
    indicator, activity log, themed dialogs and a Cancel button
  * Local speech-to-text with faster-whisper (tiny, base, small, medium); the model is
    downloaded once (free, no account) and then everything runs fully offline
  * Word-timed captions grouped into short, readable 1 or 2 line cues
  * Burned-in style controls: font size, text color, outline or background box, position
  * Caption style presets (Modern Bold, Minimalist Clean, Cinema Classic, Neon Glow) that
    adapt to each video's real resolution (16:9 landscape and 9:16 vertical), or Custom
    to keep the manual controls
  * Batch captioning: queue several videos or a whole folder. The Whisper model is loaded
    once, clips are processed one after another, and one failed clip never stops the batch
  * The GUI never freezes: all ffmpeg and Whisper work runs in a background thread
  * Error handling (missing FFmpeg, missing libass, no audio, disk space), log file
    (~/.auto_captioner/app.log)

Run:
    python3 auto_captioner.py
Pre-download a model (for machines that will be offline later):
    python3 auto_captioner.py --download-model small
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from collections import deque
from dataclasses import dataclass
from functools import lru_cache
from logging.handlers import RotatingFileHandler
from pathlib import Path
from tkinter import filedialog

# Keep Hugging Face quiet: progress bars crash apps that have no console (pythonw).
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

try:
    import customtkinter as ctk
except ImportError:
    sys.exit("ERROR: customtkinter is not installed.\nRun:  pip install -r requirements.txt")

try:
    from PIL import Image  # noqa: F401  (customtkinter needs Pillow for its widgets)
except ImportError:
    sys.exit("ERROR: Pillow is not installed.\nRun:  pip install -r requirements.txt")

# Apps launched from Finder/Explorer often miss Homebrew's PATH; add it.
os.environ["PATH"] = os.pathsep.join(
    ["/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin", os.environ.get("PATH", "")]
)

APP_NAME = "Auto Captioner"
APP_TAGLINE = "Free captions  |  Whisper runs on your computer  |  no API keys, no cloud"
VIDEO_TYPES = "*.mp4 *.mov *.mkv *.avi *.webm *.m4v *.flv *.wmv *.ts *.mpg *.mpeg *.3gp"
POPEN_KW = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform.startswith("win") else {}
PREVIEW_W, PREVIEW_H = 400, 225     # caption style preview canvas

# --------------------------------------------------------------------- theme --
WHITE, BG, BORDER = "#FFFFFF", "#F8FAFC", "#E2E8F0"
TEXT, MUTED, FAINT = "#0F172A", "#64748B", "#94A3B8"
NAVY, BLUE, BLUE_HOVER, BLUE_SOFT = "#1E3A8A", "#2563EB", "#1D4ED8", "#93C5FD"
LAV_1, LAV_2, LAV_3, LAV_4, LAV_5 = "#E6E1F9", "#D5CDF3", "#CDC2F5", "#C6B7FE", "#BBA8FF"
OK, WARN, ERR = "#16A34A", "#D97706", "#DC2626"

if sys.platform == "darwin":
    UI_FONT, MONO_FONT = "Helvetica Neue", "Menlo"
elif sys.platform.startswith("win"):
    UI_FONT, MONO_FONT = "Segoe UI", "Consolas"
else:
    UI_FONT, MONO_FONT = "DejaVu Sans", "DejaVu Sans Mono"

# Font used by ffmpeg/libass for burned-in captions (falls back automatically if missing).
CAPTION_FONT = "DejaVu Sans" if sys.platform.startswith("linux") else "Arial"


def F(size: int = 13, weight: str = "normal") -> "ctk.CTkFont":
    return ctk.CTkFont(family=UI_FONT, size=size, weight=weight)


# ------------------------------------------------------------------- options --
MODELS = {
    "tiny": ("~75 MB", "Fastest, lowest accuracy. Good for quick drafts."),
    "base": ("~145 MB", "Fast and decent on clear speech."),
    "small": ("~480 MB", "Recommended: accurate and reasonably fast."),
    "medium": ("~1.5 GB", "Most accurate, but slow on a CPU."),
}
DEFAULT_MODEL = "small"
MODEL_FILES = ["config.json", "preprocessor_config.json", "model.bin", "tokenizer.json",
               "vocabulary.*"]

LANGUAGES = {
    "Auto-detect": None, "English": "en", "Telugu": "te", "Hindi": "hi", "Tamil": "ta",
    "Spanish": "es", "French": "fr", "German": "de", "Portuguese": "pt", "Italian": "it",
    "Arabic": "ar", "Chinese": "zh", "Japanese": "ja", "Korean": "ko", "Russian": "ru",
}
TASK_TRANSCRIBE, TASK_TRANSLATE = "Transcribe", "Translate to English"
DEVICE_CPU, DEVICE_GPU = "CPU (works everywhere)", "GPU (NVIDIA CUDA)"

MODE_BURN, MODE_SRT, MODE_BOTH = "Burned-in", "SRT file", "Both"
MODE_HINTS = {
    MODE_BURN: "Captions are drawn directly onto the video and re-encoded into a new file.",
    MODE_SRT: "Only a .srt subtitle file is written (works with any player or editor). "
              "The video is not touched.",
    MODE_BOTH: "Creates the captioned video and a matching .srt file next to it.",
}

FORMATS = {
    ".mp4": dict(label="MP4", muxer="mp4", faststart=True,
                 copy_a={"aac", "mp3", "ac3", "eac3", "alac"},
                 desc="H.264 video + AAC audio. Plays everywhere; best for sharing and the web."),
    ".mov": dict(label="MOV", muxer="mov", faststart=True,
                 copy_a={"aac", "mp3", "ac3", "eac3", "alac", "pcm_s16le", "pcm_s24le"},
                 desc="H.264 video + AAC audio in a QuickTime container. Great for Apple apps."),
    ".mkv": dict(label="MKV", muxer="matroska", faststart=False,
                 copy_a={"aac", "mp3", "opus", "vorbis", "flac", "ac3", "eac3", "alac",
                         "pcm_s16le", "dts"},
                 desc="H.264 video + AAC audio in Matroska. Flexible, ideal for archiving."),
}
KNOWN_EXTS = set(FORMATS) | {".srt", ".avi", ".webm", ".m4v", ".flv", ".wmv", ".ts", ".mpg",
                             ".mpeg", ".3gp"}

QUALITY_LEVELS = ["Balanced - smaller file", "High - recommended", "Maximum - largest file"]
DEFAULT_QUALITY = 1
X264_CRF = (21, 18, 14)

TEXT_COLORS = {"White": "#FFFFFF", "Yellow": "#FFE600", "Cyan": "#4DE1FF", "Green": "#7CF08A"}
STYLE_OUTLINE, STYLE_BOX = "Outline", "Background box"
POS_BOTTOM, POS_CENTER, POS_TOP = "Bottom", "Center", "Top"
POS_ALIGN = {POS_BOTTOM: 2, POS_CENTER: 5, POS_TOP: 8}      # ASS numpad alignment
LINES_1, LINES_2 = "1 line", "2 lines"
DEFAULT_FONT_SIZE = 40            # measured on a 1080 px tall video, scaled for other sizes
SAMPLE_1 = "Your captions will look like this"
SAMPLE_2 = "Your captions will look\nlike this"

# ------------------------------------------------------- caption style presets --
PRESET_CUSTOM = "Custom (manual settings)"
VIDEO_EXTS = {p.lstrip("*").lower() for p in VIDEO_TYPES.split()}
BATCH_SYMBOLS = {"queued": "\u2022", "running": "\u25b6", "done": "\u2713",
                 "failed": "\u2717", "skipped": "\u2013"}


def _pf(win: tuple, mac: tuple, linux: tuple) -> tuple:
    """(font family, ASS bold flag) for this platform. Bold: -1 = on, 0 = off.
    libass falls back to a similar installed font if one is missing."""
    if sys.platform.startswith("win"):
        return win
    return mac if sys.platform == "darwin" else linux


# Sizes are for a frame whose SHORT side is 1080 px (1920x1080 and 1080x1920 both qualify) and
# are scaled to the real resolution. Tuples are (landscape 16:9, vertical 9:16 / square).
CAPTION_PRESETS = {
    "Modern Bold (Shorts / Reels)": dict(
        hint="Heavy uppercase text with a thick dark outline and drop shadow. Punchy and "
             "high contrast for Shorts, Reels and TikTok. Sits above the app buttons.",
        font=_pf(("Arial Black", 0), ("Helvetica Neue", -1), ("DejaVu Sans", -1)),
        size=66, portrait_boost=1.12, upper=True, spacing=0.01,
        primary="#FFFFFF", outline="#000000", outline_alpha=0, outline_frac=0.11,
        shadow_frac=0.05, back="#000000", back_alpha=0x50, box=False, glow=None,
        margin_v=(0.10, 0.22), margin_lr=(0.06, 0.08), chars=(28, 16)),
    "Minimalist Clean (YouTube / Creator)": dict(
        hint="Sleek sans-serif on a soft semi-transparent dark box with balanced padding "
             "and a light shadow. Easy to read on any footage.",
        font=_pf(("Segoe UI", 0), ("Helvetica Neue", 0), ("DejaVu Sans", 0)),
        size=44, portrait_boost=1.10, upper=False, spacing=0.0,
        primary="#FFFFFF", outline="#000000", outline_alpha=0x66, outline_frac=0.26,
        shadow_frac=0.03, back="#000000", back_alpha=0xB0, box=True, glow=None,
        margin_v=(0.08, 0.18), margin_lr=(0.06, 0.08), chars=(40, 26)),
    "Cinema / Netflix Classic": dict(
        hint="Medium-weight off-white text with a subtle drop shadow in the bottom safe "
             "zone. Calm, film-style subtitles.",
        font=_pf(("Arial", 0), ("Helvetica Neue", 0), ("DejaVu Sans", 0)),
        size=42, portrait_boost=1.10, upper=False, spacing=0.0,
        primary="#F5F5F0", outline="#000000", outline_alpha=0x30, outline_frac=0.035,
        shadow_frac=0.06, back="#000000", back_alpha=0x60, box=False, glow=None,
        margin_v=(0.07, 0.16), margin_lr=(0.08, 0.08), chars=(42, 26)),
    "Neon / Tech Glow": dict(
        hint="Geometric bold text with a crisp cyan outline and a soft glow. High energy "
             "for music, gaming and tech clips.",
        font=_pf(("Bahnschrift", -1), ("Avenir Next", -1), ("DejaVu Sans", -1)),
        size=58, portrait_boost=1.10, upper=False, spacing=0.03,
        primary="#FFFFFF", outline="#00B8D9", outline_alpha=0, outline_frac=0.045,
        shadow_frac=0.0, back="#000000", back_alpha=0xFF, box=False, glow="#00E5FF",
        margin_v=(0.10, 0.20), margin_lr=(0.06, 0.08), chars=(30, 18)),
}

MAX_LINE_CHARS = 38               # characters per caption line
MAX_CUE_SECONDS = 6.5             # longest a single caption stays on screen
MIN_CUE_SECONDS = 0.5             # shortest a caption stays on screen
SENTENCE_END = (".", "?", "!", "\u2026", "\u3002", "\uff1f", "\uff01", "\u0964")

MODELS_DIR = Path(os.environ.get("AUTO_CAPTIONER_MODELS")
                  or Path.home() / ".auto_captioner" / "models")

# ----------------------------------------------------------------- logging --
try:
    LOG_DIR = Path.home() / ".auto_captioner"
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    _handler: logging.Handler = RotatingFileHandler(LOG_DIR / "app.log", maxBytes=500_000,
                                                    backupCount=2, encoding="utf-8")
except OSError:  # read-only home directory: log to the console instead
    LOG_DIR = Path(".")
    _handler = logging.StreamHandler()
log = logging.getLogger("auto_captioner")
log.setLevel(logging.INFO)
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
log.addHandler(_handler)


class CaptionerError(Exception):
    """Error whose message can be shown directly to the user."""


class Cancelled(Exception):
    """User pressed Cancel."""


# -------------------------------------------------------------- data types --
@dataclass
class JobConfig:
    video: Path
    video_out: Path | None        # burned-in video (None = SRT only)
    srt_out: Path | None          # subtitle file (None = burned-in only)
    ext: str
    quality: int
    model: str
    language: str | None
    translate: bool
    device: str                   # "cpu" or "cuda"
    max_lines: int
    font_size: int
    color_hex: str
    box: bool
    position: str
    preset: str = PRESET_CUSTOM   # caption style preset (PRESET_CUSTOM = manual controls)


@dataclass
class Cue:
    start: float
    end: float
    text: str                     # lines separated by "\n"


# ----------------------------------------------------------------- helpers --
def fmt_hms(t: float) -> str:
    s = int(round(max(t, 0.0)))
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def fmt_eta(seconds: float) -> str:
    s = max(int(seconds), 0)
    h, m, s = s // 3600, s % 3600 // 60, s % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def srt_time(t: float) -> str:
    """Seconds -> HH:MM:SS,mmm"""
    ms = round(max(t, 0.0) * 1000)
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def ass_time(t: float) -> str:
    """Seconds -> H:MM:SS.cc (ASS uses centiseconds)"""
    cs = round(max(t, 0.0) * 100)
    h, rem = divmod(cs, 360_000)
    m, rem = divmod(rem, 6_000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def ass_color(hex_color: str, alpha: int = 0) -> str:
    """'#RRGGBB' -> ASS '&HAABBGGRR' (alpha 0 = opaque, 255 = transparent)."""
    h = hex_color.lstrip("#")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def force_ext(path: Path, ext: str) -> Path:
    """Return path with the selected extension (replacing a different known extension)."""
    suffix = path.suffix.lower()
    if suffix == ext:
        return path
    if suffix in KNOWN_EXTS:
        return path.with_suffix(ext)
    return path.with_name(path.name + ext)


def natural_key(path: Path) -> list:
    """Sort clip_2.mp4 before clip_10.mp4."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", path.name)]


# ------------------------------------------------------------ caption cues --
def wrap_text(text: str, max_chars: int, max_lines: int) -> str:
    """Break a caption into at most `max_lines` balanced lines."""
    if max_lines <= 1 or len(text) <= max_chars:
        return text
    mid = len(text) / 2
    spaces = [i for i, c in enumerate(text) if c == " "]
    if spaces:
        i = min(spaces, key=lambda k: abs(k - mid))
        first, second = text[:i], text[i + 1:]
    else:  # languages written without spaces
        i = len(text) // 2
        first, second = text[:i], text[i:]
    return first.strip() + "\n" + second.strip()


def build_cues(words: list, max_chars: int = MAX_LINE_CHARS, max_lines: int = 2) -> list:
    """Group (raw_text, start, end) words into short, readable caption cues."""
    cap = max_chars * max_lines
    cues: list[Cue] = []
    cur: list = []

    def joined(items) -> str:
        return re.sub(r"\s+", " ", "".join(w[0] for w in items)).strip()

    def flush() -> None:
        if cur:
            text = joined(cur)
            if text:
                cues.append(Cue(cur[0][1], cur[-1][2], wrap_text(text, max_chars, max_lines)))
            cur.clear()

    for raw, start, end in words:
        if not raw.strip():
            continue
        if cur:
            too_long = len(joined(cur + [(raw, start, end)])) > cap
            gap = start - cur[-1][2]
            if too_long or gap > 1.0 or end - cur[0][1] > MAX_CUE_SECONDS:
                flush()
        cur.append((raw, start, end))
        if raw.strip().endswith(SENTENCE_END) and len(joined(cur)) >= 15:
            flush()
    flush()
    return fix_timing(cues)


def fix_timing(cues: list) -> list:
    """Make captions readable: minimum duration, no overlaps, no flicker between neighbours."""
    for i, cue in enumerate(cues):
        nxt = cues[i + 1].start if i + 1 < len(cues) else None
        if cue.end - cue.start < MIN_CUE_SECONDS:
            cue.end = cue.start + MIN_CUE_SECONDS
        if nxt is not None:
            if cue.end > nxt:
                cue.end = max(nxt, cue.start + 0.05)
            elif nxt - cue.end < 0.25:
                cue.end = nxt
    return cues


def write_srt(cues: list, path: Path) -> None:
    lines: list[str] = []
    for i, cue in enumerate(cues, 1):
        lines += [str(i), f"{srt_time(cue.start)} --> {srt_time(cue.end)}", cue.text, ""]
    partial = path.with_name(f".{path.name}.partial")
    partial.write_text("\n".join(lines), encoding="utf-8")
    os.replace(partial, path)


def write_ass(cues: list, path: Path, width: int, height: int, cfg: JobConfig) -> None:
    """Write an ASS subtitle file whose style is fully defined here (resolution independent)."""
    if cfg.preset in CAPTION_PRESETS:
        return write_ass_preset(cues, path, width, height, CAPTION_PRESETS[cfg.preset])
    fs = max(10, round(cfg.font_size * height / 1080))
    if cfg.box:
        border_style, outline, shadow = 3, max(2, round(fs * 0.22)), 0
        outline_col = back_col = ass_color("#000000", 0x66)       # semi-transparent black box
    else:
        border_style, outline, shadow = 1, round(max(1.5, fs * 0.065), 2), round(fs * 0.02, 2)
        outline_col, back_col = ass_color("#000000", 0), ass_color("#000000", 0x80)
    margin_lr = round(width * 0.06)
    margin_v = 0 if cfg.position == POS_CENTER else round(height * 0.06)
    primary = ass_color(cfg.color_hex)
    out = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {width}", f"PlayResY: {height}",
        "WrapStyle: 0", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Default,{CAPTION_FONT},{fs},{primary},{primary},{outline_col},{back_col},"
        f"-1,0,0,0,100,100,0,0,{border_style},{outline},{shadow},{POS_ALIGN[cfg.position]},"
        f"{margin_lr},{margin_lr},{margin_v},1", "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for cue in cues:
        text = cue.text.replace("{", "(").replace("}", ")").replace("\n", "\\N")
        out.append(f"Dialogue: 0,{ass_time(cue.start)},{ass_time(cue.end)},Default,,0,0,0,,{text}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def preset_metrics(spec: dict, width: int, height: int) -> dict:
    """Pixel sizes of a preset for a video of this resolution (16:9 landscape or 9:16 vertical).
    Sizes follow the SHORT side of the frame, so a preset looks equally big on a 1920x1080 clip
    and on a 1080x1920 Short. Narrow frames get a size boost, shorter lines and larger margins."""
    narrow = height >= width
    i = 1 if narrow else 0
    scale = min(width, height) / 1080
    fs = max(10, round(spec["size"] * scale * (spec["portrait_boost"] if narrow else 1.0)))
    return {"fs": fs, "narrow": narrow, "chars": spec["chars"][i],
            "margin_lr": round(width * spec["margin_lr"][i]),
            "margin_v": round(height * spec["margin_v"][i])}


def write_ass_preset(cues: list, path: Path, width: int, height: int, spec: dict) -> None:
    """ASS file for a style preset. PlayResX/PlayResY equal the video's real resolution, so
    font sizes, outlines and margins are exact (never stretched or tiny)."""
    m = preset_metrics(spec, width, height)
    fs = m["fs"]
    font, bold = spec["font"]
    outline = round(max(1.0, fs * spec["outline_frac"]), 2)   # for a box: the padding
    shadow = round(fs * spec["shadow_frac"], 2)
    spacing = round(fs * spec["spacing"], 2)
    primary = ass_color(spec["primary"])
    line_col = ass_color(spec["outline"], spec["outline_alpha"])
    back_col = ass_color(spec["back"], spec["back_alpha"])

    def style(name, fill, line, back, border_style, bord, shad) -> str:
        return (f"Style: {name},{font},{fs},{fill},{fill},{line},{back},{bold},0,0,0,100,100,"
                f"{spacing},0,{border_style},{bord},{shad},2,{m['margin_lr']},{m['margin_lr']},"
                f"{m['margin_v']},1")

    styles = [style("Default", primary, line_col, back_col, 3 if spec["box"] else 1,
                    outline, shadow)]
    glow = spec.get("glow")
    blur = round(fs * 0.12, 1)
    if glow:   # wide, blurred, colored layer drawn underneath the crisp text
        g = ass_color(glow)
        styles.append(style("Glow", g, g, ass_color("#000000", 0xFF), 1, round(fs * 0.16, 2), 0))
    out = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {width}", f"PlayResY: {height}",
        "WrapStyle: 0", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        *styles, "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for cue in cues:
        text = cue.text.upper() if spec["upper"] else cue.text
        text = text.replace("{", "(").replace("}", ")").replace("\n", "\\N")
        start, end = ass_time(cue.start), ass_time(cue.end)
        if glow:
            out.append(f"Dialogue: 0,{start},{end},Glow,,0,0,0,,{{\\blur{blur}}}{text}")
        out.append(f"Dialogue: 1,{start},{end},Default,,0,0,0,,{text}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


# ------------------------------------------------------------ ffmpeg tools --
def require_tools() -> tuple[str, str]:
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise CaptionerError(
            "FFmpeg was not found.\nmacOS: brew install ffmpeg\n"
            "Windows: winget install Gyan.FFmpeg (restart the terminal)\n"
            "Linux: sudo apt install ffmpeg\n\nThen restart Auto Captioner (see the README).")
    return ffmpeg, ffprobe


def ffmpeg_version(ffmpeg: str) -> str:
    try:
        out = subprocess.run([ffmpeg, "-version"], capture_output=True, text=True, timeout=15,
                             **POPEN_KW).stdout
        m = re.search(r"version\s+(\S+)", out)
        return m.group(1)[:10] if m else ""
    except (subprocess.SubprocessError, OSError):
        return ""


@lru_cache(maxsize=2)
def available_encoders(ffmpeg: str) -> frozenset:
    try:
        out = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True,
                             text=True, timeout=30, **POPEN_KW).stdout
    except (subprocess.SubprocessError, OSError):
        return frozenset()
    return frozenset(m.group(1) for m in
                     re.finditer(r"^\s*[VAS][.FSXBD]{5}\s+(\S+)", out, re.M))


@lru_cache(maxsize=2)
def has_ass_filter(ffmpeg: str) -> bool:
    """Burning captions needs an ffmpeg build with libass (the 'ass' filter)."""
    try:
        out = subprocess.run([ffmpeg, "-hide_banner", "-filters"], capture_output=True,
                             text=True, timeout=30, **POPEN_KW).stdout
    except (subprocess.SubprocessError, OSError):
        return False
    return re.search(r"^\s*[A-Z.]{2,3}\s+ass\s", out, re.M) is not None


def pick_encoder(candidates: list, encoders, kind: str, label: str) -> str:
    for name in candidates:
        if name in encoders:
            return name
    raise CaptionerError(
        f"Your ffmpeg build has no {kind} encoder for {label} ({' or '.join(candidates)}).\n"
        "Install a full ffmpeg build:\nmacOS: brew reinstall ffmpeg\n"
        "Windows: winget install Gyan.FFmpeg\nLinux: sudo apt install ffmpeg")


def _num(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def probe(ffprobe: str, path: Path) -> dict:
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=60, encoding="utf-8", errors="replace",
            **POPEN_KW)
        data = json.loads(out.stdout or "{}")
    except (subprocess.SubprocessError, OSError, ValueError):
        data = {}
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if not video:
        raise CaptionerError("This file does not contain a readable video stream.")
    width, height = int(video.get("width") or 0), int(video.get("height") or 0)
    rotation = video.get("tags", {}).get("rotate")
    for side in video.get("side_data_list", []):
        if "rotation" in side:
            rotation = side["rotation"]
    try:
        if rotation is not None and abs(int(float(rotation))) % 180 == 90:
            width, height = height, width  # phone videos: frame is rotated on playback
    except ValueError:
        pass
    fmt = data.get("format", {})
    duration = _num(fmt.get("duration")) or _num(video.get("duration"))
    return {"width": width, "height": height, "duration": duration,
            "has_audio": audio is not None,
            "video_codec": video.get("codec_name", ""),
            "audio_codec": (audio or {}).get("codec_name", ""),
            "format_name": fmt.get("format_name", ""),
            "size": path.stat().st_size}


def run_ffmpeg(cmd, total, on_frac, cancel, proc_box, cwd=None) -> None:
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace", bufsize=1, cwd=cwd, **POPEN_KW)
    proc_box["p"] = proc
    tail: deque[str] = deque(maxlen=12)
    for line in proc.stdout:  # type: ignore[union-attr]
        line = line.strip()
        if line.startswith(("out_time_us=", "out_time_ms=")):
            try:
                on_frac(min(max(int(line.split("=")[1]), 0) / 1_000_000 / max(total, 0.1), 1.0))
            except ValueError:
                pass
        elif line and not re.match(r"^[a-z_0-9]+=\S*$", line):
            tail.append(line)
    code = proc.wait()
    proc_box["p"] = None
    if cancel.is_set():
        raise Cancelled()
    if code != 0:
        raise CaptionerError("ffmpeg reported an error:\n" + "\n".join(tail))


def extract_audio(ffmpeg: str, video: Path, wav: Path, duration: float, on_frac, cancel,
                  proc_box) -> None:
    """16 kHz mono WAV, the format Whisper works on. Audio is aligned to the video clock so
    caption timestamps match the picture."""
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(video),
           "-vn", "-map", "0:a:0", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
           "-af", "aresample=async=1:first_pts=0", "-progress", "pipe:1", "-nostats", str(wav)]
    log.info("audio: %s", " ".join(cmd))
    run_ffmpeg(cmd, duration, on_frac, cancel, proc_box)
    if not wav.exists() or wav.stat().st_size < 1000:
        raise CaptionerError("Audio extraction produced no sound. Does the video have audio?")


def build_burn_command(ffmpeg: str, cfg: JobConfig, info: dict, encoders, partial: Path,
                       subs_name: str, odd_size: bool) -> list:
    spec = FORMATS[cfg.ext]
    venc = pick_encoder(["libx264"], encoders, "video", spec["label"])
    filters = []
    if odd_size:   # H.264 needs even dimensions
        filters.append("scale=trunc(iw/2)*2:trunc(ih/2)*2")
    filters += [f"ass={subs_name}", "format=yuv420p"]
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
           "-i", str(cfg.video.resolve()), "-map", "0:v:0", "-map", "0:a:0?", "-sn",
           "-vf", ",".join(filters),
           "-c:v", venc, "-preset", "medium", "-crf", str(X264_CRF[cfg.quality])]
    if info["has_audio"]:
        if info["audio_codec"] in spec["copy_a"]:
            cmd += ["-c:a", "copy"]
        else:
            cmd += ["-c:a", "aac", "-b:a", "192k"]
    if spec["faststart"]:
        cmd += ["-movflags", "+faststart"]
    cmd += ["-progress", "pipe:1", "-nostats", "-f", spec["muxer"], str(partial)]
    return cmd


def container_signature_ok(path: Path, ext: str) -> bool:
    """Check the file's own header bytes so the container really matches the extension."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(64)
    except OSError:
        return False
    if ext == ".mp4":
        return head[4:8] == b"ftyp" and head[8:12] != b"qt  "
    if ext == ".mov":
        return head[4:8] == b"ftyp" and head[8:12] == b"qt  "
    if ext == ".mkv":
        return head[:4] == b"\x1a\x45\xdf\xa3"
    return False


def verify_output(ffprobe: str, path: Path, ext: str, info: dict):
    """Re-open the finished video and confirm it is what we expect."""
    try:
        out = probe(ffprobe, path)
    except CaptionerError:
        raise CaptionerError("The captioned video could not be read back. Please try again.")
    if not container_signature_ok(path, ext):
        raise CaptionerError(f"The exported container does not match {FORMATS[ext]['label']}. "
                             "The file was not saved.")
    if info["has_audio"] and not out["has_audio"]:
        raise CaptionerError("The captioned video has no audio track. The file was not saved.")
    if out["duration"] <= 0:
        raise CaptionerError("The captioned video has no duration. The file was not saved.")
    warnings: list[str] = []
    if abs(out["duration"] - info["duration"]) > max(1.0, info["duration"] * 0.02):
        warnings.append(f"Output is {out['duration']:.1f}s, source is {info['duration']:.1f}s.")
    return out, warnings


# ------------------------------------------------------- local speech-to-text --
def whisper_available() -> bool:
    return importlib.util.find_spec("faster_whisper") is not None


def model_repo(name: str) -> str:
    return f"Systran/faster-whisper-{name}"


def model_is_cached(name: str) -> bool:
    folder = MODELS_DIR / f"models--Systran--faster-whisper-{name}" / "snapshots"
    try:
        return any(folder.glob("*/model.bin"))
    except OSError:
        return False


def locate_model(name: str, emit=None) -> str:
    """Return the local folder of a faster-whisper model. Uses the cached copy when there is
    one (zero network access); otherwise downloads it once, for free, with no account."""
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        raise CaptionerError("faster-whisper is not installed.\nRun:  pip install -r "
                             "requirements.txt")
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    kw = dict(repo_id=model_repo(name), cache_dir=str(MODELS_DIR), allow_patterns=MODEL_FILES)
    try:
        path = snapshot_download(local_files_only=True, **kw)
        if (Path(path) / "model.bin").exists():
            return path
    except Exception:  # noqa: BLE001  (not cached yet)
        pass
    size = MODELS[name][0]
    if emit:
        emit("status", f"Downloading the '{name}' model (one time only, {size})...")
        emit("log", f"The '{name}' model is not on this computer yet. Downloading {size} "
                    "from Hugging Face (free, no account). This happens once; after that the "
                    "app works fully offline.")
    try:
        return snapshot_download(**kw)
    except Exception as exc:  # noqa: BLE001
        log.exception("model download failed")
        raise CaptionerError(
            f"The '{name}' model is not on this computer and could not be downloaded "
            f"({type(exc).__name__}).\n\nConnect to the internet once and try again, or "
            f"prepare it on another computer with:\n  python auto_captioner.py "
            f"--download-model {name}\nand copy the folder\n  {MODELS_DIR}\nto this computer.")


def cuda_available() -> bool:
    try:
        import ctranslate2
        return ctranslate2.get_cuda_device_count() > 0
    except Exception:  # noqa: BLE001
        return False


def segment_words(seg) -> list:
    """(raw_text, start, end) for every word of a Whisper segment."""
    words = getattr(seg, "words", None)
    if words:
        return [(w.word, float(w.start), float(w.end)) for w in words if w.word.strip()]
    tokens = seg.text.split()          # no word timings: spread the words evenly
    if not tokens:
        return []
    step = max(float(seg.end) - float(seg.start), 0.1) / len(tokens)
    return [(" " + t, float(seg.start) + i * step, float(seg.start) + (i + 1) * step)
            for i, t in enumerate(tokens)]


def transcribe_audio(cfg: JobConfig, wav: Path, duration: float, model_path: str, emit,
                     cancel, p0: float, p1: float, cache=None):
    """Run faster-whisper locally. Returns ([(raw_text, start, end), ...], language)."""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise CaptionerError("faster-whisper is not installed.\nRun:  pip install -r "
                             "requirements.txt")
    attempts = [("cpu", "int8")]
    if cfg.device == "cuda" and not (cache is not None and "cuda" in cache.bad):
        if cuda_available():
            attempts.insert(0, ("cuda", "float16"))
        else:
            if cache is not None:
                cache.bad.add("cuda")
            emit("log", "No NVIDIA GPU was detected. Using the CPU instead.")

    for i, (device, ctype) in enumerate(attempts):
        words: list = []
        got_any = False
        try:
            model = cache.models.get((device, ctype)) if cache is not None else None
            if model is None:
                emit("status", "Loading local Whisper model...")
                emit("log", f"Model: {cfg.model}  |  device: {device.upper()}  |  "
                            f"precision: {ctype}")
                model = WhisperModel(model_path, device=device, compute_type=ctype)
                if cache is not None:
                    cache.models[(device, ctype)] = model
            emit("progress", p0)
            emit("status", "Transcribing...")
            segments, info = model.transcribe(
                str(wav), language=cfg.language,
                task="translate" if cfg.translate else "transcribe",
                beam_size=5, vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 500},
                word_timestamps=True, condition_on_previous_text=False)
            lang = info.language
            if cfg.language is None:
                emit("log", f"Detected language: {lang} "
                            f"({info.language_probability * 100:.0f}% sure)")
            total = max(duration, float(getattr(info, "duration", 0) or 0), 0.1)
            for seg in segments:
                if cancel.is_set():
                    raise Cancelled()
                got_any = True
                words += segment_words(seg)
                emit("progress", p0 + (p1 - p0) * min(float(seg.end) / total, 1.0))
                snippet = seg.text.strip()
                if snippet:
                    emit("log", f"[{fmt_hms(float(seg.start))}] "
                                f"{snippet[:90]}{'...' if len(snippet) > 90 else ''}")
            return words, lang
        except (Cancelled, CaptionerError):
            raise
        except Exception as exc:  # noqa: BLE001
            if device == "cuda" and not got_any and i + 1 < len(attempts):
                log.warning("GPU failed, falling back to CPU: %s", exc)
                if cache is not None:
                    cache.bad.add("cuda")
                    cache.models.pop((device, ctype), None)
                emit("log", f"GPU could not be used ({exc}). Falling back to the CPU.")
                continue
            raise
    return [], None


# ----------------------------------------------------------------- pipeline --
class ModelCache:
    """Keeps loaded Whisper models in memory so a batch loads the model only once."""

    def __init__(self) -> None:
        self.models: dict = {}
        self.bad: set = set()      # devices that failed (e.g. "cuda"): not tried again


def preload_model(cfg: JobConfig, model_path: str, emit, cache: ModelCache) -> None:
    """Load the Whisper model once, at the start of a batch."""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise CaptionerError("faster-whisper is not installed.\nRun:  pip install -r "
                             "requirements.txt")
    attempts = [("cpu", "int8")]
    if cfg.device == "cuda":
        if cuda_available():
            attempts.insert(0, ("cuda", "float16"))
        else:
            cache.bad.add("cuda")
            emit("log", "No NVIDIA GPU was detected. Using the CPU instead.")
    for device, ctype in attempts:
        try:
            emit("status", "Loading local Whisper model (once for the whole batch)...")
            emit("log", f"Model: {cfg.model}  |  device: {device.upper()}  |  precision: {ctype}")
            cache.models[(device, ctype)] = WhisperModel(model_path, device=device,
                                                         compute_type=ctype)
            return
        except Exception as exc:  # noqa: BLE001
            if device == "cuda":
                log.warning("GPU failed, falling back to CPU: %s", exc)
                cache.bad.add("cuda")
                emit("log", f"GPU could not be used ({exc}). Falling back to the CPU.")
                continue
            raise


LIBASS_HELP = ("Your ffmpeg build cannot draw subtitles (libass is missing).\n"
               "Install a full ffmpeg build:\nmacOS: brew reinstall ffmpeg\n"
               "Windows: winget install Gyan.FFmpeg\nLinux: sudo apt install ffmpeg\n\n"
               "Tip: choose 'SRT file' to create subtitles without burning them in.")


def process_video(cfg: JobConfig, emit, cancel, proc_box, cache=None, model_path=None) -> dict:
    """Audio extraction -> local Whisper -> .srt and/or burned-in video for ONE video.
    Returns a result dict; raises CaptionerError / Cancelled. Temporary files are always
    cleaned up. `cache` + `model_path` let a batch reuse one loaded Whisper model."""
    tmp: Path | None = None
    partial: Path | None = None
    saved: list[Path] = []
    try:
        ffmpeg, ffprobe = require_tools()
        video = cfg.video.resolve()
        if not video.is_file():
            raise CaptionerError(f"Video file not found:\n{video}")
        for out in (cfg.video_out, cfg.srt_out):
            if out is not None and out.resolve() == video:
                raise CaptionerError("The output file must be different from the source video.")
        info = probe(ffprobe, video)
        if info["duration"] <= 0:
            raise CaptionerError("Could not read the video duration. Is the file corrupted?")
        if not info["has_audio"]:
            raise CaptionerError("This video has no audio track, so there is nothing to "
                                 "transcribe.")
        emit("log", f"Source: {info['width']}x{info['height']}, {info['duration']:.2f}s, "
                    f"{info['video_codec']}/{info['audio_codec']}")

        burn = cfg.video_out is not None
        encoders = frozenset()
        if burn:
            if not has_ass_filter(ffmpeg):
                raise CaptionerError(LIBASS_HELP)
            encoders = available_encoders(ffmpeg)
            pick_encoder(["libx264"], encoders, "video", FORMATS[cfg.ext]["label"])

        tmp = Path(tempfile.mkdtemp(prefix="auto_captioner_"))
        wav_bytes = info["duration"] * 16000 * 2
        if shutil.disk_usage(tmp).free < wav_bytes * 1.2 + 50e6:
            raise CaptionerError("Not enough free disk space for the temporary audio file.")
        for out in (cfg.video_out, cfg.srt_out):
            if out is not None:
                out.parent.mkdir(parents=True, exist_ok=True)
        if burn and shutil.disk_usage(cfg.video_out.parent).free < max(info["size"] * 1.5, 50e6):
            raise CaptionerError("Not enough free disk space in the output folder.")

        p_audio, p_model, p_tx = 0.05, 0.10, (0.5 if burn else 0.97)

        # 1) audio
        emit("status", "Extracting audio...")
        wav = tmp / "audio.wav"
        extract_audio(ffmpeg, video, wav, info["duration"],
                      lambda f: emit("progress", p_audio * f), cancel, proc_box)
        emit("progress", p_audio)
        if cancel.is_set():
            raise Cancelled()

        # 2) model (cached = no network) and 3) transcription
        model_path = model_path or locate_model(cfg.model, emit)
        words, lang = transcribe_audio(cfg, wav, info["duration"], model_path, emit, cancel,
                                       p_model, p_tx, cache)
        if cancel.is_set():
            raise Cancelled()
        emit("status", "Building captions...")
        line_chars = MAX_LINE_CHARS
        if cfg.preset in CAPTION_PRESETS:     # presets pick line length for the frame shape
            line_chars = preset_metrics(CAPTION_PRESETS[cfg.preset], info["width"] or 1920,
                                        info["height"] or 1080)["chars"]
        cues = build_cues(words, line_chars, cfg.max_lines)
        if not cues:
            raise CaptionerError("No speech was detected in this video, so no captions were "
                                 "created.\nCheck the language setting or try a larger model.")
        emit("log", f"Created {len(cues)} captions.")

        # 4) subtitle file
        if cfg.srt_out is not None:
            write_srt(cues, cfg.srt_out)
            saved.append(cfg.srt_out)
            emit("log", f"Saved subtitles: {cfg.srt_out}")
        emit("progress", p_tx)

        # 5) burn into the video
        result_size = cfg.srt_out.stat().st_size if cfg.srt_out else 0
        if burn:
            width, height = info["width"] or 1920, info["height"] or 1080
            odd = bool(width % 2 or height % 2)
            if odd:
                width, height = width - width % 2, height - height % 2
            write_ass(cues, tmp / "subs.ass", width, height, cfg)
            partial = cfg.video_out.with_name(f".{cfg.video_out.stem}.partial{cfg.ext}")
            cmd = build_burn_command(ffmpeg, cfg, info, encoders, partial, "subs.ass", odd)
            emit("status", "Rendering final video...")
            log.info("burn: %s", " ".join(cmd))
            run_ffmpeg(cmd, info["duration"],
                       lambda f: emit("progress", p_tx + (1 - p_tx) * 0.97 * f),
                       cancel, proc_box, cwd=str(tmp))
            emit("status", "Verifying the captioned video...")
            out, warnings = verify_output(ffprobe, partial, cfg.ext, info)
            for w in warnings:
                emit("log", "Warning: " + w)
            os.replace(partial, cfg.video_out)
            partial = None
            saved.append(cfg.video_out)
            result_size = cfg.video_out.stat().st_size
            emit("log", f"Saved video: {cfg.video_out}")

        emit("progress", 1.0)
        return {"video": cfg.video_out, "srt": cfg.srt_out, "count": len(cues),
                "language": lang, "size": result_size, "burned": burn}
    except Cancelled as exc:
        exc.saved = len(saved)  # type: ignore[attr-defined]
        raise
    finally:
        if partial and partial.exists():
            partial.unlink(missing_ok=True)
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)


def run_job(cfg: JobConfig, emit, cancel, proc_box) -> None:
    """Single video, in a background thread: reports done / cancelled / error to the UI."""
    try:
        result = process_video(cfg, emit, cancel, proc_box)
        emit("done", result)
    except Cancelled as exc:
        emit("cancelled", getattr(exc, "saved", 0))
    except CaptionerError as exc:
        log.error("%s", exc)
        emit("error", str(exc))
    except Exception as exc:  # noqa: BLE001
        log.exception("unexpected error")
        emit("error", f"Unexpected error: {exc}\nDetails: {LOG_DIR / 'app.log'}")


def run_batch(cfgs: list, indices: list, emit, cancel, proc_box) -> None:
    """Caption many videos in a background thread. The Whisper model is loaded ONCE; clips are
    processed one after another; a clip that fails is logged and the batch carries on."""
    ok: list[dict] = []
    failed: list[tuple] = []
    last: Path | None = None
    try:
        ffmpeg, _ffprobe = require_tools()
        if any(c.video_out is not None for c in cfgs) and not has_ass_filter(ffmpeg):
            raise CaptionerError(LIBASS_HELP)
        cache = ModelCache()
        model_path = locate_model(cfgs[0].model, emit)
        preload_model(cfgs[0], model_path, emit, cache)

        n = len(cfgs)
        sizes = []
        for c in cfgs:
            try:
                sizes.append(max(c.video.stat().st_size, 1))
            except OSError:
                sizes.append(1)
        total = float(sum(sizes))
        spans = [s / total for s in sizes]                 # share of the overall progress bar
        starts = [sum(spans[:k]) for k in range(n)]

        for k, cfg in enumerate(cfgs):
            if cancel.is_set():
                raise Cancelled()
            name = cfg.video.name
            emit("qstatus", (indices[k], "running"))
            emit("log", f"--- Video {k + 1} of {n}: {name} ---")
            emit("status", f"Processing video {k + 1} of {n}: {name}")
            emit("progress", starts[k])

            def vemit(kind, payload=None, k=k, name=name):
                if kind == "progress":
                    emit("progress", starts[k] + spans[k] * min(max(payload, 0.0), 1.0))
                elif kind == "status":
                    emit("status", f"Processing video {k + 1} of {n}: {name} \u2014 {payload}")
                else:
                    emit(kind, payload)

            try:
                res = process_video(cfg, vemit, cancel, proc_box, cache=cache,
                                    model_path=model_path)
                ok.append(res)
                last = res["video"] or res["srt"]
                emit("qstatus", (indices[k], "done"))
                emit("log", f"Finished {name}: {res['count']} captions.")
            except Cancelled:
                emit("qstatus", (indices[k], "queued"))
                raise
            except CaptionerError as exc:
                log.error("batch: %s failed: %s", name, exc)
                failed.append((name, str(exc)))
                emit("qstatus", (indices[k], "failed"))
                emit("log", f"FAILED: {name} - {str(exc).splitlines()[0]}  (continuing)")
            except Exception as exc:  # noqa: BLE001
                log.exception("batch: unexpected error on %s", name)
                failed.append((name, f"Unexpected error: {exc}"))
                emit("qstatus", (indices[k], "failed"))
                emit("log", f"FAILED: {name} - unexpected error: {exc}  (continuing)")
            emit("progress", starts[k] + spans[k])

        emit("batch_done", {"ok": len(ok), "failed": failed, "total": n, "last": last})
    except Cancelled:
        emit("batch_cancelled", {"ok": len(ok), "last": last})
    except CaptionerError as exc:
        log.error("%s", exc)
        emit("error", str(exc))
    except Exception as exc:  # noqa: BLE001
        log.exception("unexpected batch error")
        emit("error", f"Unexpected error: {exc}\nDetails: {LOG_DIR / 'app.log'}")


def open_path(path: Path) -> None:
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", str(path)])
        elif sys.platform.startswith("win"):
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            subprocess.run(["xdg-open", str(path)])
    except OSError as exc:
        raise CaptionerError(f"Could not open it: {exc}")


# ---------------------------------------------------------------- UI widgets --
def primary_button(parent, text, command, width=120, height=40, **kw):
    return ctk.CTkButton(parent, text=text, command=command, width=width, height=height,
                         corner_radius=8, fg_color=BLUE, hover_color=BLUE_HOVER,
                         text_color=WHITE, text_color_disabled="#EFF6FF",
                         font=F(13, "bold"), **kw)


def secondary_button(parent, text, command, width=100, height=38, **kw):
    return ctk.CTkButton(parent, text=text, command=command, width=width, height=height,
                         corner_radius=8, fg_color=LAV_1, hover_color=LAV_2, text_color=NAVY,
                         text_color_disabled=FAINT, font=F(13, "bold"), **kw)


def text_entry(parent, var, placeholder="", **kw):
    return ctk.CTkEntry(parent, textvariable=var, placeholder_text=placeholder, height=38,
                        corner_radius=8, fg_color=BG, border_color=BORDER, border_width=1,
                        text_color=TEXT, placeholder_text_color=FAINT, font=F(13), **kw)


def option_menu(parent, values, command=None, height=40):
    return ctk.CTkOptionMenu(
        parent, values=values, command=command, height=height, corner_radius=8,
        fg_color=LAV_1, button_color=LAV_4, button_hover_color=LAV_5, text_color=TEXT,
        text_color_disabled=FAINT, anchor="w", dropdown_fg_color=WHITE,
        dropdown_hover_color=LAV_1, dropdown_text_color=TEXT, font=F(13), dropdown_font=F(13))


class Segmented(ctk.CTkFrame):
    """Pill-style selector: the selected option is solid blue on a soft lavender track."""

    def __init__(self, parent, values, command=None, height=40):
        super().__init__(parent, fg_color=LAV_2, corner_radius=10)
        self._seg_command = command
        self._seg_value = values[0]
        self._seg_buttons: dict = {}
        for i, value in enumerate(values):
            self.grid_columnconfigure(i, weight=1, uniform="seg")
            btn = ctk.CTkButton(self, text=value, height=height, corner_radius=8,
                                border_width=0, font=F(13, "bold"),
                                command=lambda v=value: self.set(v, notify=True))
            btn.grid(row=0, column=i, sticky="ew", padx=3, pady=3)
            self._seg_buttons[value] = btn
        self.set(self._seg_value)

    def set(self, value: str, notify: bool = False) -> None:
        self._seg_value = value
        for v, btn in self._seg_buttons.items():
            if v == value:
                btn.configure(fg_color=BLUE, hover_color=BLUE_HOVER, text_color=WHITE,
                              text_color_disabled=WHITE)
            else:
                btn.configure(fg_color="transparent", hover_color=LAV_3, text_color=NAVY,
                              text_color_disabled=FAINT)
        if notify and self._seg_command:
            self._seg_command(value)

    def get(self) -> str:
        return self._seg_value

    def set_enabled(self, enabled: bool) -> None:
        for btn in self._seg_buttons.values():
            btn.configure(state="normal" if enabled else "disabled")


class Dialog(ctk.CTkToplevel):
    """Themed message box (info / warning / error) with optional confirm / cancel."""
    KIND_COLORS = {"info": BLUE, "warning": WARN, "error": ERR}

    def __init__(self, parent, title: str, message: str, kind: str = "info",
                 confirm: bool = False, yes_text: str = "OK", no_text: str = "Cancel"):
        super().__init__(parent)
        self.result = False
        self.title(title)
        self.configure(fg_color=WHITE)
        self.resizable(False, False)
        self.transient(parent)
        accent = self.KIND_COLORS.get(kind, BLUE)

        ctk.CTkFrame(self, fg_color=accent, height=5, corner_radius=0).pack(fill="x")
        ctk.CTkLabel(self, text=title, font=F(17, "bold"), anchor="w",
                     text_color=NAVY if kind == "info" else accent).pack(
            fill="x", padx=24, pady=(18, 6))
        if len(message) > 260 or message.count("\n") > 5:
            box = ctk.CTkTextbox(self, width=440, height=170, fg_color=BG, border_width=1,
                                 border_color=BORDER, text_color=TEXT, font=F(12), wrap="word")
            box.insert("1.0", message)
            box.configure(state="disabled")
            box.pack(padx=24, pady=(0, 8))
        else:
            ctk.CTkLabel(self, text=message, font=F(13), text_color=TEXT, justify="left",
                         anchor="w", wraplength=440).pack(fill="x", padx=24, pady=(0, 8))
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", padx=24, pady=(10, 20))
        primary_button(row, yes_text, lambda: self._finish(True), width=110).pack(side="right")
        if confirm:
            secondary_button(row, no_text, lambda: self._finish(False), width=110,
                             height=40).pack(side="right", padx=(0, 8))

        self.protocol("WM_DELETE_WINDOW", lambda: self._finish(False))
        self.bind("<Escape>", lambda _e: self._finish(False))
        self.update_idletasks()
        try:  # size to content and center over the parent
            w, h = self.winfo_reqwidth(), self.winfo_reqheight()
            x = parent.winfo_rootx() + (parent.winfo_width() - w) // 2
            y = parent.winfo_rooty() + (parent.winfo_height() - h) // 3
            self.wm_geometry(f"{w}x{h}+{max(x, 0)}+{max(y, 0)}")
        except Exception:  # noqa: BLE001
            pass
        self.after(200, self._grab)

    def _grab(self) -> None:
        try:
            self.lift()
            self.grab_set()
            self.focus_force()
        except Exception:  # noqa: BLE001  (window not viewable yet: dialog still works)
            pass

    def _finish(self, result: bool) -> None:
        self.result = result
        try:
            self.grab_release()
        except Exception:  # noqa: BLE001
            pass
        self.destroy()


# ---------------------------------------------------------------------- UI --
class App(ctk.CTk):
    STATUS_COLORS = {"idle": MUTED, "busy": BLUE, "ok": OK, "warn": WARN, "error": ERR}

    def __init__(self) -> None:
        ctk.set_appearance_mode("light")
        ctk.set_default_color_theme("blue")
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1140x880")
        self.minsize(1020, 760)
        self.configure(fg_color=BG)

        self.q: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.proc_box: dict = {"p": None}
        self.running = False
        self.result: Path | None = None
        self.tools: tuple[str, str] | None = None
        self.info: dict | None = None
        self.loaded: Path | None = None
        self._auto_out = ""
        self._t0 = 0.0
        self._general_widgets: list = []     # disabled while a job runs
        self._general_segs: list = []
        self._style_widgets: list = []       # only used when burning captions into the video
        self._style_segs: list = []
        self._manual_widgets: list = []      # manual style controls (Custom preset only)
        self._manual_segs: list = []
        self.batch_files: list[Path] = []    # batch queue
        self.batch_state: dict = {}          # queue index -> queued/running/done/failed/skipped
        self._batch_skipped = 0

        self._build()
        self._refresh_tools_chip()
        self._on_model(self.model_menu.get())
        self._on_format(self.fmt.get())
        self._on_mode(self.mode.get())
        self._on_style()
        self._on_preset()
        self._update_batch_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._poll)

    # ------------------------------------------------------------- layout --
    def _card(self, parent, number, title: str):
        card = ctk.CTkFrame(parent, fg_color=WHITE, corner_radius=14, border_width=1,
                            border_color=BORDER)
        card.pack(fill="x", pady=(0, 14))
        head = ctk.CTkFrame(card, fg_color="transparent")
        head.pack(fill="x", padx=18, pady=(14, 8))
        if number:
            ctk.CTkLabel(head, text=str(number), width=26, height=26, corner_radius=13,
                         fg_color=LAV_1, text_color=NAVY, font=F(12, "bold")).pack(
                side="left", padx=(0, 10))
        ctk.CTkLabel(head, text=title, font=F(15, "bold"), text_color=NAVY).pack(side="left")
        body = ctk.CTkFrame(card, fg_color="transparent")
        body.pack(fill="x", padx=18, pady=(0, 16))
        return body

    @staticmethod
    def _section_label(parent, text: str):
        return ctk.CTkLabel(parent, text=text, font=F(11, "bold"), text_color=MUTED, anchor="w")

    def _build(self) -> None:
        # ---- header
        header = ctk.CTkFrame(self, fg_color=NAVY, corner_radius=0, height=88)
        header.pack(fill="x")
        header.pack_propagate(False)
        title_box = ctk.CTkFrame(header, fg_color="transparent")
        title_box.pack(side="left", padx=28)
        ctk.CTkLabel(title_box, text=APP_NAME, font=F(26, "bold"), text_color=WHITE,
                     anchor="w").pack(anchor="w", pady=(16, 0))
        ctk.CTkLabel(title_box, text=APP_TAGLINE, font=F(12), text_color=LAV_2,
                     anchor="w").pack(anchor="w")
        self.ff_chip = ctk.CTkLabel(header, text="", width=150, height=28, corner_radius=14,
                                    fg_color=LAV_1, text_color=NAVY, font=F(12, "bold"))
        self.ff_chip.pack(side="right", padx=(10, 28))
        self.wh_chip = ctk.CTkLabel(header, text="", width=150, height=28, corner_radius=14,
                                    fg_color=LAV_1, text_color=NAVY, font=F(12, "bold"))
        self.wh_chip.pack(side="right")
        ctk.CTkFrame(self, fg_color=LAV_5, height=3, corner_radius=0).pack(fill="x")

        # ---- fixed bottom bar (packed before the scroll area so it is never hidden)
        bar = ctk.CTkFrame(self, fg_color=WHITE, corner_radius=0)
        bar.pack(side="bottom", fill="x")
        ctk.CTkFrame(bar, fg_color=BORDER, height=1, corner_radius=0).pack(fill="x")
        inner = ctk.CTkFrame(bar, fg_color="transparent")
        inner.pack(fill="x", padx=28, pady=(14, 16))
        self.progress = ctk.CTkProgressBar(inner, height=10, corner_radius=5, fg_color=LAV_1,
                                           progress_color=BLUE)
        self.progress.set(0)
        self.progress.pack(fill="x")
        row = ctk.CTkFrame(inner, fg_color="transparent")
        row.pack(fill="x", pady=(12, 0))
        self.dot = ctk.CTkFrame(row, width=10, height=10, corner_radius=5, fg_color=MUTED)
        self.dot.pack(side="left", padx=(0, 8))
        self.status = ctk.CTkLabel(row, text="Ready", font=F(13, "bold"), text_color=TEXT,
                                   anchor="w")
        self.status.pack(side="left")
        self.pct = ctk.CTkLabel(row, text="", font=F(12), text_color=MUTED, anchor="w")
        self.pct.pack(side="left", padx=14)
        self.start_btn = primary_button(row, "Generate Captions", self._start, width=190,
                                        height=44)
        self.start_btn.pack(side="right")
        self.cancel_btn = secondary_button(row, "Cancel", self._cancel, width=100, height=44)
        self.cancel_btn.pack(side="right", padx=(0, 10))
        self.folder_btn = secondary_button(
            row, "Open folder", lambda: self._open(self.result.parent if self.result else None),
            width=120, height=44)
        self.folder_btn.pack(side="right", padx=(0, 10))
        self.play_btn = secondary_button(
            row, "Open result", lambda: self._open(self.result), width=120, height=44)
        self.play_btn.pack(side="right", padx=(0, 10))

        # ---- scrollable two-column body
        scroll = ctk.CTkScrollableFrame(self, fg_color=BG, corner_radius=0,
                                        scrollbar_button_color=LAV_3,
                                        scrollbar_button_hover_color=LAV_5)
        scroll.pack(fill="both", expand=True, padx=(14, 6), pady=(14, 0))
        scroll.grid_columnconfigure((0, 1), weight=1, uniform="cols")
        left = ctk.CTkFrame(scroll, fg_color="transparent")
        left.grid(row=0, column=0, sticky="new", padx=(6, 8))
        right = ctk.CTkFrame(scroll, fg_color="transparent")
        right.grid(row=0, column=1, sticky="new", padx=(8, 6))

        self._build_source(left)
        self._build_output(left)
        self._build_recognition(left)
        self._build_style(right)
        self._build_activity(right)

    def _build_source(self, parent) -> None:
        body = self._card(parent, 1, "Source video")
        body.grid_columnconfigure(0, weight=1)
        self.video_var = ctk.StringVar()
        self.video_entry = text_entry(body, self.video_var, "Choose a video file, or paste a path")
        self.video_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.video_entry.bind("<Return>", lambda _e: self._load_video(self.video_var.get()))
        self.browse_btn = primary_button(body, "Browse", self._browse_video, width=100, height=38)
        self.browse_btn.grid(row=0, column=1)
        self.info_lbl = ctk.CTkLabel(body, text="No video loaded", font=F(12),
                                     text_color=MUTED, anchor="w")
        self.info_lbl.grid(row=1, column=0, columnspan=2, sticky="w", pady=(10, 0))

        # ---- batch queue (optional): several videos or a whole folder
        self._section_label(body, "BATCH QUEUE (OPTIONAL)").grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(16, 0))
        btns = ctk.CTkFrame(body, fg_color="transparent")
        btns.grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.add_btn = secondary_button(btns, "Add videos", self._add_videos, width=118)
        self.add_btn.pack(side="left")
        self.folder_add_btn = secondary_button(btns, "Add folder", self._add_folder, width=112)
        self.folder_add_btn.pack(side="left", padx=(8, 0))
        self.clear_btn = secondary_button(btns, "Clear queue", self._clear_queue, width=112)
        self.clear_btn.pack(side="left", padx=(8, 0))
        self.queue_lbl = ctk.CTkLabel(body, text="", font=F(12, "bold"), text_color=MUTED,
                                      anchor="w", justify="left", wraplength=440)
        self.queue_lbl.grid(row=4, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self.queue_box = ctk.CTkTextbox(body, height=116, state="disabled", fg_color=BG,
                                        border_width=1, border_color=BORDER, text_color=TEXT,
                                        corner_radius=8, wrap="none",
                                        font=ctk.CTkFont(family=MONO_FONT, size=12))
        self.queue_box.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.queue_box.grid_remove()
        self._general_widgets += [self.video_entry, self.browse_btn, self.add_btn,
                                  self.folder_add_btn]

    def _build_output(self, parent) -> None:
        body = self._card(parent, 2, "Caption output")
        body.grid_columnconfigure(0, weight=1)

        self._section_label(body, "WHAT TO CREATE").grid(row=0, column=0, sticky="w")
        self.mode = Segmented(body, [MODE_BURN, MODE_SRT, MODE_BOTH], command=self._on_mode,
                              height=40)
        self.mode.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        self.mode_hint = ctk.CTkLabel(body, text="", font=F(12), text_color=MUTED, anchor="w",
                                      justify="left", wraplength=440)
        self.mode_hint.grid(row=2, column=0, sticky="w", pady=(8, 14))

        self._section_label(body, "VIDEO FORMAT (BURNED-IN VIDEO)").grid(row=3, column=0,
                                                                        sticky="w")
        self.fmt = Segmented(body, list(FORMATS), command=self._on_format, height=38)
        self.fmt.grid(row=4, column=0, sticky="ew", pady=(6, 0))
        self.fmt_desc = ctk.CTkLabel(body, text="", font=F(12), text_color=MUTED, anchor="w",
                                     justify="left", wraplength=440)
        self.fmt_desc.grid(row=5, column=0, sticky="w", pady=(8, 14))

        self._section_label(body, "VIDEO QUALITY").grid(row=6, column=0, sticky="w")
        self.quality = option_menu(body, QUALITY_LEVELS)
        self.quality.set(QUALITY_LEVELS[DEFAULT_QUALITY])
        self.quality.grid(row=7, column=0, sticky="ew", pady=(6, 14))

        self.save_lbl = self._section_label(body, "SAVE TO")
        self.save_lbl.grid(row=8, column=0, sticky="w")
        pathrow = ctk.CTkFrame(body, fg_color="transparent")
        pathrow.grid(row=9, column=0, sticky="ew", pady=(6, 0))
        pathrow.grid_columnconfigure(0, weight=1)
        self.out_var = ctk.StringVar()
        self.out_entry = text_entry(pathrow, self.out_var, "Where to save the result")
        self.out_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.saveas_btn = secondary_button(pathrow, "Save as", self._browse_out, width=100)
        self.saveas_btn.grid(row=0, column=1)

        self.pathrow = pathrow

        # batch: one output folder for every clip (shown instead of the single-file row)
        self.bsave_lbl = self._section_label(
            body, "SAVE ALL TO FOLDER   (blank = next to each original, with _captioned)")
        self.bsave_lbl.grid(row=10, column=0, sticky="w")
        self.bpathrow = ctk.CTkFrame(body, fg_color="transparent")
        self.bpathrow.grid(row=11, column=0, sticky="ew", pady=(6, 0))
        self.bpathrow.grid_columnconfigure(0, weight=1)
        self.bout_var = ctk.StringVar()
        self.bout_entry = text_entry(self.bpathrow, self.bout_var, "Output folder (optional)")
        self.bout_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.bfolder_btn = secondary_button(self.bpathrow, "Choose", self._browse_batch_out,
                                            width=100)
        self.bfolder_btn.grid(row=0, column=1)
        self.bsave_lbl.grid_remove()
        self.bpathrow.grid_remove()

        self._general_widgets += [self.out_entry, self.saveas_btn, self.bout_entry,
                                  self.bfolder_btn]
        self._general_segs.append(self.mode)
        self._style_widgets.append(self.quality)
        self._style_segs.append(self.fmt)

    def _build_recognition(self, parent) -> None:
        body = self._card(parent, 3, "Speech recognition (runs locally)")
        body.grid_columnconfigure((0, 1), weight=1, uniform="rec")

        self._section_label(body, "WHISPER MODEL").grid(row=0, column=0, sticky="w")
        self._section_label(body, "SPOKEN LANGUAGE").grid(row=0, column=1, sticky="w",
                                                         padx=(8, 0))
        self.model_menu = option_menu(body, list(MODELS), command=self._on_model)
        self.model_menu.set(DEFAULT_MODEL)
        self.model_menu.grid(row=1, column=0, sticky="ew", pady=(6, 0), padx=(0, 8))
        self.lang_menu = option_menu(body, list(LANGUAGES))
        self.lang_menu.set("Auto-detect")
        self.lang_menu.grid(row=1, column=1, sticky="ew", pady=(6, 0), padx=(8, 0))
        self.model_hint = ctk.CTkLabel(body, text="", font=F(12), text_color=MUTED, anchor="w",
                                       justify="left", wraplength=440)
        self.model_hint.grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 14))

        self._section_label(body, "TASK").grid(row=3, column=0, columnspan=2, sticky="w")
        self.task = Segmented(body, [TASK_TRANSCRIBE, TASK_TRANSLATE], height=36)
        self.task.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(6, 14))

        self._section_label(body, "PROCESSOR").grid(row=5, column=0, sticky="w")
        self._section_label(body, "CAPTION LENGTH").grid(row=5, column=1, sticky="w",
                                                        padx=(8, 0))
        self.device = option_menu(body, [DEVICE_CPU, DEVICE_GPU])
        self.device.set(DEVICE_CPU)
        self.device.grid(row=6, column=0, sticky="ew", pady=(6, 0), padx=(0, 8))
        self.lines_seg = Segmented(body, [LINES_1, LINES_2], command=lambda _v: self._on_style(),
                                   height=34)
        self.lines_seg.set(LINES_2)
        self.lines_seg.grid(row=6, column=1, sticky="ew", pady=(6, 0), padx=(8, 0))

        self._general_widgets += [self.model_menu, self.lang_menu, self.device]
        self._general_segs += [self.task, self.lines_seg]

    def _build_style(self, parent) -> None:
        body = self._card(parent, 4, "Caption style (burned-in video)")
        body.grid_columnconfigure((0, 1), weight=1, uniform="sty")

        self._section_label(body, "STYLE PRESET").grid(row=0, column=0, columnspan=2,
                                                      sticky="w")
        self.preset_menu = option_menu(body, list(CAPTION_PRESETS) + [PRESET_CUSTOM],
                                       command=self._on_preset)
        self.preset_menu.set(PRESET_CUSTOM)
        self.preset_menu.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.preset_hint = ctk.CTkLabel(body, text="", font=F(12), text_color=MUTED,
                                        anchor="w", justify="left", wraplength=440)
        self.preset_hint.grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 14))

        top = ctk.CTkFrame(body, fg_color="transparent")
        top.grid(row=3, column=0, columnspan=2, sticky="ew")
        top.grid_columnconfigure(0, weight=1)
        self._section_label(top, "FONT SIZE").grid(row=0, column=0, sticky="w")
        self.size_lbl = ctk.CTkLabel(top, text="", font=F(12, "bold"), text_color=NAVY)
        self.size_lbl.grid(row=0, column=1, sticky="e")
        self.size_slider = ctk.CTkSlider(
            body, from_=16, to=80, number_of_steps=64, height=18,
            command=lambda _v: self._on_style(), progress_color=BLUE, button_color=BLUE,
            button_hover_color=NAVY, fg_color=BORDER)
        self.size_slider.set(DEFAULT_FONT_SIZE)
        self.size_slider.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(6, 14))

        self._section_label(body, "TEXT COLOR").grid(row=5, column=0, sticky="w")
        self._section_label(body, "BACKGROUND").grid(row=5, column=1, sticky="w", padx=(8, 0))
        self.color_menu = option_menu(body, list(TEXT_COLORS), command=lambda _v: self._on_style())
        self.color_menu.set("White")
        self.color_menu.grid(row=6, column=0, sticky="ew", pady=(6, 0), padx=(0, 8))
        self.style_seg = Segmented(body, [STYLE_OUTLINE, STYLE_BOX],
                                   command=lambda _v: self._on_style(), height=34)
        self.style_seg.grid(row=6, column=1, sticky="ew", pady=(6, 0), padx=(8, 0))

        self._section_label(body, "POSITION").grid(row=7, column=0, columnspan=2, sticky="w",
                                                  pady=(14, 0))
        self.pos_seg = Segmented(body, [POS_BOTTOM, POS_CENTER, POS_TOP],
                                 command=lambda _v: self._on_style(), height=34)
        self.pos_seg.grid(row=8, column=0, columnspan=2, sticky="ew", pady=(6, 14))

        self.preview = tk.Canvas(body, width=PREVIEW_W, height=PREVIEW_H, bg="#475569",
                                 highlightthickness=1, highlightbackground=BORDER)
        self.preview.grid(row=9, column=0, columnspan=2)
        ctk.CTkLabel(body, text="Approximate preview. Final size scales with your video's "
                                "resolution.", font=F(11), text_color=MUTED).grid(
            row=10, column=0, columnspan=2, pady=(8, 0))

        self._style_widgets += [self.preset_menu]
        self._manual_widgets += [self.size_slider, self.color_menu]
        self._manual_segs += [self.style_seg, self.pos_seg]

    def _build_activity(self, parent) -> None:
        body = self._card(parent, None, "Activity")
        self.logbox = ctk.CTkTextbox(body, height=250, state="disabled", fg_color=BG,
                                     border_width=1, border_color=BORDER, text_color=TEXT,
                                     corner_radius=8, wrap="word",
                                     font=ctk.CTkFont(family=MONO_FONT, size=12))
        self.logbox.pack(fill="x")

    # -------------------------------------------------------- tools / chips --
    def _refresh_tools_chip(self) -> None:
        try:
            self.tools = require_tools()
            ver = ffmpeg_version(self.tools[0])
            self.ff_chip.configure(text=f"FFmpeg {ver}".strip() + "  ready", fg_color=LAV_1,
                                   text_color=NAVY)
        except CaptionerError:
            self.tools = None
            self.ff_chip.configure(text="FFmpeg not found", fg_color="#FEE2E2", text_color=ERR)
            self._log("FFmpeg was not found. Install it, then restart the app (see README).")
        if whisper_available():
            self.wh_chip.configure(text="Whisper  ready", fg_color=LAV_1, text_color=NAVY)
        else:
            self.wh_chip.configure(text="Whisper missing", fg_color="#FEE2E2", text_color=ERR)
            self._log("faster-whisper is not installed. Run: pip install -r requirements.txt")

    # ----------------------------------------------------------- dialogs --
    def _dialog(self, title, message, kind="info", confirm=False, yes="OK", no="Cancel") -> bool:
        dlg = Dialog(self, title, message, kind, confirm, yes, no)
        self.wait_window(dlg)
        return dlg.result

    def _warn(self, title: str, message: str) -> None:
        self._dialog(title, message, "warning")

    def _error(self, title: str, message: str) -> None:
        self._dialog(title, message, "error")

    # ------------------------------------------------------ option callbacks --
    def _on_model(self, name: str) -> None:
        size, desc = MODELS[name]
        state = ("Ready on this computer (works offline)." if model_is_cached(name)
                 else f"One-time download of {size} the first time you use it.")
        self.model_hint.configure(text=f"{desc}  {state}")

    def _on_format(self, ext: str | None = None) -> None:
        self.fmt_desc.configure(text=FORMATS[self.fmt.get()]["desc"])
        self._retarget_output()

    def _on_mode(self, _value=None) -> None:
        mode = self.mode.get()
        self.mode_hint.configure(text=MODE_HINTS[mode])
        self.save_lbl.configure(text={
            MODE_BURN: "SAVE VIDEO TO",
            MODE_SRT: "SAVE SUBTITLES TO",
            MODE_BOTH: "SAVE VIDEO TO   (the .srt is saved next to it)"}[mode])
        self._retarget_output()
        self._refresh_controls()

    def _on_style(self) -> None:
        self.size_lbl.configure(text=f"{int(self.size_slider.get())} pt")
        self._draw_preview()

    def _on_preset(self, _value=None) -> None:
        spec = CAPTION_PRESETS.get(self.preset_menu.get())
        self.preset_hint.configure(
            text=spec["hint"] if spec else
            "Manual: use the font size, text color, background and position controls below.")
        self._refresh_controls()
        self._draw_preview()

    def _draw_preview(self) -> None:
        c = self.preview
        c.delete("all")
        w, h = PREVIEW_W, PREVIEW_H
        c.create_rectangle(0, 0, w, h // 2, fill="#64748B", outline="")
        c.create_rectangle(0, h // 2, w, h, fill="#475569", outline="")
        text = SAMPLE_2 if self.lines_seg.get() == LINES_2 else SAMPLE_1
        spec = CAPTION_PRESETS.get(self.preset_menu.get())
        x = w // 2
        glow = None
        shadow = 0
        if spec is None:                                   # Custom: the manual controls
            px = max(7, round(int(self.size_slider.get()) * h / 1080 * 1.15))
            weight = "bold"
            color = TEXT_COLORS.get(self.color_menu.get(), "#FFFFFF")
            margin, pos = round(h * 0.06), self.pos_seg.get()
            box, line_col, line_frac, box_fill = (self.style_seg.get() == STYLE_BOX, "#000000",
                                                  0.07, "#0B1220")
        else:                                              # a style preset
            px = max(7, round(spec["size"] * h / 1080 * 1.15))
            weight = "bold" if spec["font"][1] else "normal"
            color = spec["primary"]
            margin, pos = round(h * spec["margin_v"][0]), POS_BOTTOM
            box, line_col, line_frac, box_fill = spec["box"], spec["outline"], \
                spec["outline_frac"], "#1E293B"
            shadow = round(px * spec["shadow_frac"]) if spec["shadow_frac"] else 0
            glow = spec.get("glow")
            if spec["upper"]:
                text = text.upper()
            if line_col == "#000000":
                line_col = "#0B1220"
        font = (UI_FONT, -px, weight)
        if pos == POS_BOTTOM:
            y, anchor = h - margin, "s"
        elif pos == POS_TOP:
            y, anchor = margin, "n"
        else:
            y, anchor = h // 2, "center"
        if box:
            item = c.create_text(x, y, text=text, font=font, fill=color, anchor=anchor,
                                 justify="center")
            x1, y1, x2, y2 = c.bbox(item)
            pad = max(2, round(px * (0.22 if spec is None else spec["outline_frac"] * 0.85)))
            rect = c.create_rectangle(x1 - pad, y1 - pad, x2 + pad, y2 + pad, fill=box_fill,
                                      outline="")
            c.tag_lower(rect, item)
        else:
            if glow:                                       # soft halo around the text
                for ring, col in ((max(3, round(px * 0.16)), "#2F6F82"),
                                  (max(2, round(px * 0.09)), "#2B93AD")):
                    for dx in (-ring, 0, ring):
                        for dy in (-ring, 0, ring):
                            if dx or dy:
                                c.create_text(x + dx, y + dy, text=text, font=font, fill=col,
                                              anchor=anchor, justify="center")
            if shadow:
                c.create_text(x + shadow, y + shadow, text=text, font=font, fill="#111827",
                              anchor=anchor, justify="center")
            d = max(1, round(px * line_frac))
            for dx in (-d, 0, d):
                for dy in (-d, 0, d):
                    if dx or dy:
                        c.create_text(x + dx, y + dy, text=text, font=font, fill=line_col,
                                      anchor=anchor, justify="center")
            c.create_text(x, y, text=text, font=font, fill=color, anchor=anchor,
                          justify="center")

    # ------------------------------------------------------------ actions --
    def _browse_video(self) -> None:
        path = filedialog.askopenfilename(
            title="Choose a video", filetypes=[("Video files", VIDEO_TYPES), ("All files", "*.*")])
        if path:
            self._load_video(path)

    def _load_video(self, path_text: str) -> bool:
        path = Path(path_text.strip().strip("'\"")).expanduser()
        if not path.is_file():
            self._warn("Video not found", "Please choose a valid video file first.")
            return False
        try:
            self.tools = require_tools()
            info = probe(self.tools[1], path)
            if info["duration"] <= 0:
                raise CaptionerError("Could not read the video duration. Is the file corrupted?")
        except CaptionerError as exc:
            self.info_lbl.configure(text=str(exc).splitlines()[0], text_color=ERR)
            self._refresh_tools_chip()
            self._error("Cannot open video", str(exc))
            return False

        if self.batch_files:                       # a single video replaces the batch queue
            self._clear_queue(log=False)
            self._log("Batch queue cleared: single-video mode.")
        self.loaded, self.info = path, info
        self.video_var.set(str(path))
        audio = info["audio_codec"] or "no audio"
        self.info_lbl.configure(
            text=f"{info['width']}x{info['height']}  |  {fmt_hms(info['duration'])}  |  "
                 f"{info['size'] / 1e6:.1f} MB  |  {info['video_codec']} / {audio}",
            text_color=MUTED if info["has_audio"] else WARN)
        self._retarget_output(force=True)
        self._refresh_controls()
        if info["has_audio"]:
            self._set_status("Video loaded. Choose your options and press Generate Captions.",
                             "idle")
        else:
            self._set_status("This video has no audio track.", "warn")
            self._log("Warning: no audio track found, so there is nothing to transcribe.")
        self._log(f"Loaded: {path.name}")
        return True

    def _default_output(self) -> str:
        if self.loaded is None:
            return ""
        if self.mode.get() == MODE_SRT:
            return str(self.loaded.with_name(f"{self.loaded.stem}.srt"))
        return str(self.loaded.with_name(f"{self.loaded.stem}_captioned{self.fmt.get()}"))

    def _retarget_output(self, force: bool = False) -> None:
        if self.loaded is None:
            return
        current = self.out_var.get().strip()
        new_auto = self._default_output()
        if force or not current or current == self._auto_out:
            self._auto_out = new_auto
            self.out_var.set(new_auto)
        else:
            ext = ".srt" if self.mode.get() == MODE_SRT else self.fmt.get()
            self.out_var.set(str(force_ext(Path(current), ext)))

    def _browse_out(self) -> None:
        src = self.loaded
        if self.mode.get() == MODE_SRT:
            ext, kinds, name = ".srt", [("SRT subtitles", "*.srt")], (src.stem if src else "captions")
            initial = f"{name}.srt"
        else:
            ext = self.fmt.get()
            kinds = [(f"{FORMATS[ext]['label']} video", f"*{ext}")]
            initial = f"{src.stem}_captioned{ext}" if src else f"captioned{ext}"
        path = filedialog.asksaveasfilename(
            title="Save as", defaultextension=ext, initialfile=initial,
            initialdir=str(src.parent) if src else None, filetypes=kinds)
        if path:
            self.out_var.set(str(force_ext(Path(path), ext)))

    def _make_cfg(self, video: Path, video_out, srt_out, ext: str) -> JobConfig:
        """Job settings from the current UI state (shared by single and batch runs)."""
        return JobConfig(
            video=video, video_out=video_out, srt_out=srt_out, ext=ext,
            quality=QUALITY_LEVELS.index(self.quality.get()), model=self.model_menu.get(),
            language=LANGUAGES[self.lang_menu.get()],
            translate=self.task.get() == TASK_TRANSLATE,
            device="cuda" if self.device.get() == DEVICE_GPU else "cpu",
            max_lines=2 if self.lines_seg.get() == LINES_2 else 1,
            font_size=int(self.size_slider.get()),
            color_hex=TEXT_COLORS[self.color_menu.get()],
            box=self.style_seg.get() == STYLE_BOX, position=self.pos_seg.get(),
            preset=self.preset_menu.get())

    # ------------------------------------------------------------ batch queue --
    def _add_videos(self) -> None:
        paths = filedialog.askopenfilenames(
            title="Choose videos",
            filetypes=[("Video files", VIDEO_TYPES), ("All files", "*.*")])
        if isinstance(paths, str):
            paths = self.tk.splitlist(paths)
        if paths:
            self._queue_add([Path(p) for p in paths])

    def _add_folder(self) -> None:
        folder = filedialog.askdirectory(title="Choose a folder of videos")
        if not folder:
            return
        try:
            entries = sorted(Path(folder).iterdir(), key=natural_key)
        except OSError as exc:
            return self._error("Cannot read folder", str(exc))
        files = [p for p in entries if p.is_file() and not p.name.startswith(".")
                 and p.suffix.lower() in VIDEO_EXTS]
        earlier = [p for p in files if p.stem.lower().endswith("_captioned")]
        files = [p for p in files if p not in earlier]
        if earlier:
            self._log(f"Skipped {len(earlier)} file(s) that already look captioned "
                      f"(name ends with _captioned).")
        if not files:
            return self._warn("No videos found", "That folder has no video files "
                              "(subfolders are not scanned).")
        self._queue_add(files)

    def _queue_add(self, paths: list) -> None:
        known = set()
        for p in self.batch_files:
            try:
                known.add(p.resolve())
            except OSError:
                pass
        added = 0
        for p in paths:
            try:
                key = p.resolve()
            except OSError:
                continue
            if key in known or not p.is_file():
                continue
            known.add(key)
            self.batch_files.append(p)
            added += 1
        if not added:
            self._log("Those videos are already in the queue.")
            return
        self.batch_state = {i: "queued" for i in range(len(self.batch_files))}
        n = len(self.batch_files)
        self._log(f"Added {added} video{'s' if added != 1 else ''} to the batch queue "
                  f"({n} queued).")
        self._update_batch_ui()
        self._set_status(f"{n} video{'s' if n != 1 else ''} queued. Choose your options and "
                         f"press Caption.", "idle")

    def _clear_queue(self, log: bool = True) -> None:
        had = len(self.batch_files)
        self.batch_files, self.batch_state = [], {}
        self._update_batch_ui()
        if had and log:
            self._log("Batch queue cleared.")
            self._set_status("Queue cleared.", "idle")

    def _update_batch_ui(self) -> None:
        batch = bool(self.batch_files)
        for w in (self.save_lbl, self.pathrow):
            (w.grid if not batch else w.grid_remove)()
        for w in (self.bsave_lbl, self.bpathrow):
            (w.grid if batch else w.grid_remove)()
        n = len(self.batch_files)
        self.start_btn.configure(
            text=f"Caption {n} video{'s' if n != 1 else ''}" if batch else "Generate Captions")
        self._render_queue()
        self._refresh_controls()

    def _render_queue(self, focus: int | None = None) -> None:
        n = len(self.batch_files)
        if n == 0:
            self.queue_lbl.configure(text="Add several videos or a whole folder to caption "
                                          "them all in one run.", text_color=MUTED)
            self.queue_box.grid_remove()
            return
        states = list(self.batch_state.values())
        text = f"{n} video{'s' if n != 1 else ''} queued"
        done, bad = states.count("done"), states.count("failed")
        if done or bad:
            text += f"   |   {done} done" + (f", {bad} failed" if bad else "")
        self.queue_lbl.configure(text=text, text_color=ERR if bad and not self.running
                                 else NAVY)
        lines = [f" {BATCH_SYMBOLS[self.batch_state.get(i, 'queued')]}  {i + 1:>2}.  {p.name}"
                 for i, p in enumerate(self.batch_files)]
        self.queue_box.configure(state="normal")
        self.queue_box.delete("1.0", "end")
        self.queue_box.insert("1.0", "\n".join(lines))
        if focus is not None:
            self.queue_box.see(f"{focus + 1}.0")
        self.queue_box.configure(state="disabled")
        self.queue_box.grid()

    def _browse_batch_out(self) -> None:
        folder = filedialog.askdirectory(title="Choose the output folder")
        if folder:
            self.bout_var.set(folder)

    def _begin_run(self) -> None:
        self.cancel.clear()
        self.result = None
        self.running = True
        self._refresh_controls()
        self.progress.set(0)
        self.pct.configure(text="")
        self._t0 = time.monotonic()

    def _start_batch(self) -> None:
        if self.tools is None:
            try:
                self.tools = require_tools()
            except CaptionerError as exc:
                self._refresh_tools_chip()
                return self._error("FFmpeg is missing", str(exc))
        if not whisper_available():
            return self._error("Whisper is missing", "faster-whisper is not installed.\n"
                               "Run:  pip install -r requirements.txt\nthen restart the app.")
        mode, ext = self.mode.get(), self.fmt.get()
        out_text = self.bout_var.get().strip().strip("'\"")
        out_dir = Path(out_text).expanduser() if out_text else None
        if out_dir is not None and out_dir.exists() and not out_dir.is_dir():
            return self._warn("Choose a folder", "The output location must be a folder, "
                              "not a file.")

        # plan: one (queue index, video, video_out, srt_out) per clip; names never collide
        plan, used = [], set()
        for i, video in enumerate(self.batch_files):
            if not video.is_file():
                self.batch_state[i] = "failed"
                self._log(f"Not found, skipped: {video}")
                continue
            folder = out_dir or video.parent
            if mode == MODE_SRT:
                target = force_ext(folder / video.stem, ".srt")
            else:
                target = force_ext(folder / f"{video.stem}_captioned", ext)
            n = 2
            while str(target).lower() in used:
                target = target.with_name(f"{target.stem.rsplit('_dup', 1)[0]}_dup{n}"
                                          f"{target.suffix}")
                n += 1
            used.add(str(target).lower())
            if mode == MODE_SRT:
                video_out, srt_out = None, target
            else:
                video_out = target
                srt_out = target.with_suffix(".srt") if mode == MODE_BOTH else None
            plan.append((i, video, video_out, srt_out))
        if not plan:
            self._render_queue()
            return self._warn("Nothing to do", "None of the queued videos could be found.")

        existing = [e for e in plan if any(t is not None and t.exists() for t in e[2:])]
        skip_existing = False
        if existing:
            names = "\n".join(e[1].name for e in existing[:6])
            more = f"\n... and {len(existing) - 6} more" if len(existing) > 6 else ""
            replace = self._dialog(
                "Replace files?", f"{len(existing)} of {len(plan)} videos already have output "
                f"files:\n{names}{more}\n\nReplace them, or skip those videos?", "warning",
                confirm=True, yes="Replace", no="Skip existing")
            skip_existing = not replace
        jobs = []
        self.batch_state = {i: "queued" for i in range(len(self.batch_files))}
        self._batch_skipped = 0
        for entry in plan:
            if skip_existing and entry in existing:
                self.batch_state[entry[0]] = "skipped"
                self._batch_skipped += 1
            else:
                jobs.append(entry)
        if not jobs:
            self._render_queue()
            return self._warn("Nothing to do", "All videos already have captions, so they "
                              "were skipped.")

        cfgs = [self._make_cfg(v, vo, so, ext) for _i, v, vo, so in jobs]
        self._begin_run()
        self._render_queue()
        self._set_status(f"Starting batch: {len(jobs)} videos...", "busy")
        self._log(f"Start batch: {len(jobs)} videos  |  {mode}  |  model {cfgs[0].model}  |  "
                  f"{self.lang_menu.get()}  |  {self.task.get()}  |  "
                  f"style: {self.preset_menu.get()}")
        if self._batch_skipped:
            self._log(f"Skipping {self._batch_skipped} video(s) that already have output.")
        emit = lambda kind, payload=None: self.q.put((kind, payload))  # noqa: E731
        threading.Thread(target=run_batch, daemon=True,
                         args=(cfgs, [e[0] for e in jobs], emit, self.cancel,
                               self.proc_box)).start()

    def _open(self, path) -> None:
        if path is None:
            return
        try:
            open_path(Path(path))
        except CaptionerError as exc:
            self._error(APP_NAME, str(exc))

    def _start(self) -> None:
        if self.batch_files:
            return self._start_batch()
        video_text = self.video_var.get().strip().strip("'\"")
        if not video_text or not Path(video_text).expanduser().is_file():
            return self._warn("Choose a video", "Please choose a valid video file first.")
        video = Path(video_text).expanduser()
        if self.loaded is None or video.resolve() != self.loaded.resolve():
            if not self._load_video(video_text):
                return
        if self.tools is None:
            try:
                self.tools = require_tools()
            except CaptionerError as exc:
                self._refresh_tools_chip()
                return self._error("FFmpeg is missing", str(exc))
        if not whisper_available():
            return self._error("Whisper is missing", "faster-whisper is not installed.\n"
                               "Run:  pip install -r requirements.txt\nthen restart the app.")
        if self.info is not None and not self.info["has_audio"]:
            return self._warn("No audio", "This video has no audio track, so there is nothing "
                                          "to transcribe.")

        mode, ext = self.mode.get(), self.fmt.get()
        out_text = self.out_var.get().strip()
        if not out_text:
            self._retarget_output(force=True)
            out_text = self.out_var.get().strip()
        out = Path(out_text).expanduser()
        if mode == MODE_SRT:
            video_out, srt_out = None, force_ext(out, ".srt")
        else:
            video_out = force_ext(out, ext)
            srt_out = video_out.with_suffix(".srt") if mode == MODE_BOTH else None
        targets = [p for p in (video_out, srt_out) if p is not None]
        for target in targets:
            if target.resolve() == video.resolve():
                return self._warn("Choose another file",
                                  "The output must be a different file than the source video.")
        shown = video_out or srt_out
        if str(shown) != out_text:
            self.out_var.set(str(shown))
        existing = [p for p in targets if p.exists()]
        if existing and not self._dialog(
                "Replace files?", "These files already exist:\n" +
                "\n".join(p.name for p in existing) + "\n\nReplace them?", "warning",
                confirm=True, yes="Replace", no="Cancel"):
            return

        cfg = self._make_cfg(video, video_out, srt_out, ext)

        self.cancel.clear()
        self.result = None
        self.running = True
        self._refresh_controls()
        self.progress.set(0)
        self.pct.configure(text="")
        self._t0 = time.monotonic()
        self._set_status("Starting...", "busy")
        self._log(f"Start: {video.name}  |  {mode}  |  model {cfg.model}  |  "
                  f"{self.lang_menu.get()}  |  {self.task.get()}")
        emit = lambda kind, payload=None: self.q.put((kind, payload))  # noqa: E731
        threading.Thread(target=run_job, daemon=True,
                         args=(cfg, emit, self.cancel, self.proc_box)).start()

    def _cancel(self) -> None:
        self.cancel.set()
        proc = self.proc_box.get("p")
        if proc and proc.poll() is None:
            proc.terminate()
        if self.running:
            self._set_status("Cancelling...", "warn")

    def _on_close(self) -> None:
        if self.running and not self._dialog("Quit?", "Captions are being generated. Quit "
                                             "anyway?", "warning", confirm=True, yes="Quit",
                                             no="Keep working"):
            return
        self._cancel()
        self.destroy()

    # ------------------------------------------------------------ ui state --
    def _set_status(self, text: str, state: str = "idle") -> None:
        self.dot.configure(fg_color=self.STATUS_COLORS[state])
        self.status.configure(text=text)

    def _refresh_controls(self) -> None:
        busy = self.running
        burn = self.mode.get() != MODE_SRT
        for w in self._general_widgets:
            w.configure(state="disabled" if busy else "normal")
        for w in self._style_widgets:
            w.configure(state="normal" if burn and not busy else "disabled")
        for seg in self._general_segs:
            seg.set_enabled(not busy)
        for seg in self._style_segs:
            seg.set_enabled(burn and not busy)
        manual = burn and not busy and self.preset_menu.get() == PRESET_CUSTOM
        for w in self._manual_widgets:
            w.configure(state="normal" if manual else "disabled")
        for seg in self._manual_segs:
            seg.set_enabled(manual)
        self.clear_btn.configure(state="normal" if self.batch_files and not busy else "disabled")
        self.start_btn.configure(state="disabled" if busy else "normal",
                                 fg_color=BLUE_SOFT if busy else BLUE)
        self.cancel_btn.configure(state="normal" if busy else "disabled")
        has_result = self.result is not None and not busy
        self.play_btn.configure(state="normal" if has_result else "disabled")
        self.folder_btn.configure(state="normal" if has_result else "disabled")

    def _log(self, text: str) -> None:
        self.logbox.configure(state="normal")
        self.logbox.insert("end", text + "\n")
        self.logbox.see("end")
        self.logbox.configure(state="disabled")

    def _on_progress(self, frac: float) -> None:
        self.progress.set(frac)
        text = f"{int(frac * 100)}%"
        elapsed = time.monotonic() - self._t0
        if frac > 0.12 and elapsed > 5:
            text += f"   |   about {fmt_eta(elapsed * (1 - frac) / frac)} left"
        self.pct.configure(text=text)

    def _poll(self) -> None:
        try:
            while True:
                kind, data = self.q.get_nowait()
                if kind == "log":
                    self._log(data)
                elif kind == "status":
                    self._set_status(data, "busy")
                elif kind == "progress":
                    self._on_progress(data)
                elif kind == "done":
                    self.running = False
                    self.result = data["video"] or data["srt"]
                    self._refresh_controls()
                    self._on_model(self.model_menu.get())
                    self.pct.configure(text="100%")
                    what = {(True, True): "Captioned video and subtitles saved.",
                            (True, False): "Captioned video saved.",
                            (False, True): "Subtitle file saved."}[
                        (data["video"] is not None, data["srt"] is not None)]
                    self._set_status("Done! " + what, "ok")
                    self._log(f"Result: {data['count']} captions  |  language: "
                              f"{data['language'] or 'n/a'}  |  {data['size'] / 1e6:.2f} MB")
                elif kind == "qstatus":
                    idx, state = data
                    self.batch_state[idx] = state
                    self._render_queue(idx if state == "running" else None)
                elif kind == "batch_done":
                    self.running = False
                    self.result = data["last"]
                    self._refresh_controls()
                    self._on_model(self.model_menu.get())
                    self._render_queue()
                    self.progress.set(1.0)
                    self.pct.configure(text="100%")
                    ok, bad, total = data["ok"], data["failed"], data["total"]
                    if bad:
                        self._set_status(f"Finished: {ok} of {total} captioned, "
                                         f"{len(bad)} failed", "warn")
                    else:
                        self._set_status(f"Done! {ok} video{'s' if ok != 1 else ''} "
                                         f"processed.", "ok")
                    self._log(f"Batch finished: {ok} succeeded, {len(bad)} failed, "
                              f"{self._batch_skipped} skipped.")
                    for name, msg in bad:
                        self._log(f"  FAILED: {name} - {msg.splitlines()[0] if msg else '?'}")
                    if bad:
                        shown = "\n".join(f"{n}: {m.splitlines()[0] if m else '?'}"
                                          for n, m in bad[:8])
                        more = f"\n... and {len(bad) - 8} more" if len(bad) > 8 else ""
                        self._warn("Some videos failed",
                                   f"{len(bad)} of {total} videos could not be captioned:\n\n"
                                   f"{shown}{more}\n\nThe others finished. See the Activity "
                                   f"log for details.")
                elif kind == "batch_cancelled":
                    self.running = False
                    if data["last"] is not None:
                        self.result = data["last"]
                    self._refresh_controls()
                    self._render_queue()
                    self.progress.set(0)
                    self.pct.configure(text="")
                    self._set_status("Cancelled", "warn")
                    self._log(f"Batch cancelled. {data['ok']} video(s) were finished and "
                              f"kept; the rest were not processed.")
                elif kind == "cancelled":
                    self.running = False
                    self._refresh_controls()
                    self.progress.set(0)
                    self.pct.configure(text="")
                    self._set_status("Cancelled", "warn")
                    self._log("Cancelled. No file was written." if not data else
                              "Cancelled. The subtitle file that was already finished was kept.")
                elif kind == "error":
                    self.running = False
                    self._refresh_controls()
                    self.progress.set(0)
                    self.pct.configure(text="")
                    self._set_status("Captioning failed", "error")
                    self._log("ERROR: " + data)
                    self._error("Captioning failed", data)
        except queue.Empty:
            pass
        self.after(100, self._poll)


# --------------------------------------------------------------------- main --
def cli_download(name: str) -> int:
    """Download a Whisper model once so the app can later run with no internet at all."""
    os.environ.pop("HF_HUB_DISABLE_PROGRESS_BARS", None)
    print(f"Preparing the '{name}' model ({MODELS[name][0]}) in {MODELS_DIR} ...")
    try:
        path = locate_model(name, lambda kind, text=None: print(text) if kind == "log" else None)
    except CaptionerError as exc:
        print("ERROR:", exc)
        return 1
    print(f"Done. Model ready at: {path}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Auto Captioner: free, offline video captions.")
    parser.add_argument("--download-model", choices=list(MODELS), metavar="SIZE",
                        help="download a Whisper model (tiny, base, small or medium) and exit")
    args = parser.parse_args()
    if args.download_model:
        sys.exit(cli_download(args.download_model))
    App().mainloop()


if __name__ == "__main__":
    main()