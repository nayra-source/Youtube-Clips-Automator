#!/usr/bin/env python3
"""
YT Video Downloader (GUI)
=========================
Download a video at the quality you choose (Best Available up to 8K, 4K, 1440p, 1080p, 720p or
480p) with the best audio and save it as a QuickTime-ready .mov file.

How it works
  1. Paste a video link and press Fetch Info (title, offered resolutions, codec, size preview)
  2. Choose the Video Quality, the .mov mode (Auto, Apple ProRes 422 or Strict Stream Copy) and,
     when a conversion is needed, the Encoding speed
  3. Pick the output folder
  4. Download: yt-dlp fetches the best video stream at (or below) the chosen resolution + the best
     audio, then ffmpeg remuxes or transcodes it into a .mov that is re-opened with ffprobe and
     verified

Speed notes (what makes this version fast)
  * The video page is analysed ONCE. Fetch Info, a quality change and the final download all
    reuse the same result instead of asking YouTube again (each lookup costs 5 to 20 seconds).
  * Quality changes are re-evaluated offline, so they are instant.
  * H.264 streams are preferred when a video offers them at the same resolution, so they can be
    remuxed losslessly in seconds instead of being re-encoded.
  * Encoding speed selector: Fast (hardware H.264 via VideoToolbox / NVENC when available, with
    an automatic software fallback), Balanced (libx264 fast, CRF 16) or Best quality (libx264
    slow, CRF 14).
  * 8 parallel fragment downloads.

Features
  * Modern white and blue UI: stage indicator, progress bar with speed and ETA, status dot,
    collapsible activity log, themed dialogs, Cancel button
  * Video Quality selector with graceful fallback when a resolution is not offered
  * Fetch Info lists the resolutions the link really offers and what will be downloaded
  * Auto mode: lossless remux for H.264 / HEVC, transcode for VP9 / AV1 (a .mov cannot hold them)
  * ProRes 422 mode (prores_ks profile 2 or 3, 10-bit) for zero-lag editing
  * Strict Stream Copy mode: the video stream is never re-encoded
  * The GUI never freezes: all network and ffmpeg work runs in a background thread
  * Your folder, quality, mode and speed are remembered between runs
  * Safe file names, temporary files removed after every job, disk-space check, startup check
    for ffmpeg, log file (~/.yt4k_download/app.log)

Run:
    python3 ytvid_download.py
"""

from __future__ import annotations

import copy
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import unicodedata
from collections import deque
from dataclasses import dataclass, field
from functools import lru_cache
from logging.handlers import RotatingFileHandler
from pathlib import Path
from tkinter import filedialog

try:
    import customtkinter as ctk
except ImportError:
    sys.exit("ERROR: customtkinter is not installed.\nRun:  pip install -r requirements.txt")

try:
    from PIL import Image  # noqa: F401  (customtkinter needs Pillow for its widgets)
except ImportError:
    sys.exit("ERROR: Pillow is not installed.\nRun:  pip install -r requirements.txt")

try:
    import yt_dlp
    from yt_dlp.utils import DownloadError
except ImportError:
    sys.exit("ERROR: yt-dlp is not installed.\nRun:  pip install -r requirements.txt")

try:
    from yt_dlp.version import __version__ as YTDLP_VERSION
except Exception:  # noqa: BLE001
    YTDLP_VERSION = ""

# Apps launched from Finder/Explorer often miss Homebrew's PATH; add it (and Deno's default dir).
os.environ["PATH"] = os.pathsep.join(
    ["/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin",
     os.path.join(os.path.expanduser("~"), ".deno", "bin"), os.environ.get("PATH", "")]
)

APP_NAME = "YT Video Downloader"
APP_VERSION = "2.0"
APP_TAGLINE = ("Choose your quality, up to 8K  |  best audio  |  QuickTime-ready .mov  |  "
               "powered by yt-dlp and FFmpeg")
POPEN_KW = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform.startswith("win") else {}

# --------------------------------------------------------------------- theme --
WHITE, BG, BORDER = "#FFFFFF", "#F8FAFC", "#E2E8F0"
TEXT, MUTED, FAINT = "#0F172A", "#64748B", "#94A3B8"
NAVY, BLUE, BLUE_HOVER, BLUE_SOFT = "#1E3A8A", "#2563EB", "#1D4ED8", "#93C5FD"
LAV_1, LAV_2, LAV_3, LAV_4, LAV_5 = "#E6E1F9", "#D5CDF3", "#CDC2F5", "#C6B7FE", "#BBA8FF"
OK, WARN, ERR = "#16A34A", "#D97706", "#DC2626"
OK_SOFT, WARN_SOFT, ERR_SOFT = "#DCFCE7", "#FEF3C7", "#FEE2E2"

if sys.platform == "darwin":
    UI_FONT, MONO_FONT = "Helvetica Neue", "Menlo"
elif sys.platform.startswith("win"):
    UI_FONT, MONO_FONT = "Segoe UI", "Consolas"
else:
    UI_FONT, MONO_FONT = "DejaVu Sans", "DejaVu Sans Mono"


def F(size: int = 13, weight: str = "normal") -> "ctk.CTkFont":
    return ctk.CTkFont(family=UI_FONT, size=size, weight=weight)


# ------------------------------------------------------------------ settings --
# Video quality choices. The value is a maximum height in pixels (None = no cap at all).
QUALITY_BEST = "Best Available (Auto Max — 8K/4K/Highest)"
QUALITY_OPTIONS = {
    QUALITY_BEST: None,
    "4K Ultra HD (2160p)": 2160,
    "2K QHD (1440p)": 1440,
    "Full HD (1080p)": 1080,
    "HD (720p)": 720,
    "SD (480p)": 480,
}
# A tier counts as "offered" when the video has a stream taller than the floor and not taller than
# the cap (so 1920x1080 is offered for Full HD, and a 1080p-only video does not offer 4K).
TIER_FLOOR = {2160: 1440, 1440: 1080, 1080: 720, 720: 480, 480: 360}
# yt-dlp sort order for every tier: tallest, highest frame rate (60 fps), HDR, then H.264 when the
# video offers it at that resolution (it remuxes into .mov with no re-encode), audio channels /
# codec, and finally bitrate. yt-dlp keeps "lang" and "quality" first, so the original-language
# audio track is still preferred over auto-dubbed ones.
FORMAT_SORT = ["res", "fps", "hdr:12", "vcodec:h264", "channels", "acodec", "br", "size"]
QUALITY_HINT = ("Every option downloads the best video stream plus the best audio at or below "
                "that resolution, preferring 60 fps. Press Fetch Info to see what this video "
                "offers.")
INFO_MAX_AGE = 3 * 3600   # stream links stay valid for hours; look the video up again after this


def build_selector(cap: int | None) -> str:
    """Separate best video + best audio up to the cap; pre-muxed streams are only a fallback."""
    if not cap:
        return "bestvideo+bestaudio/best"
    return (f"bestvideo[height<={cap}]+bestaudio/best[height<={cap}]"
            "/bestvideo+bestaudio/best")


FORMAT_SELECTOR = build_selector(None)

MODE_AUTO, MODE_PRORES, MODE_COPY = "Auto (Recommended)", "ProRes 422", "Stream Copy"
MODE_HINTS = {
    MODE_AUTO: "Lossless remux (video copied untouched) when the source is H.264 or HEVC. "
               "VP9 and AV1, which is what 4K videos usually use, are converted to H.264 "
               "(see Encoding speed): the .mov opens natively in QuickTime, Final Cut Pro and "
               "DaVinci Resolve.",
    MODE_PRORES: "Transcodes to Apple ProRes 422 (prores_ks, 10-bit) inside .mov for zero-lag "
                 "timeline editing. Files are very large: about 3.5 to 10 GB per minute at 4K.",
    MODE_COPY: "Remuxes the video into .mov with -c:v copy and never re-encodes it: fastest and "
               "bit-exact. A .mov cannot hold VP9 / AV1, so when the 4K stream is VP9 / AV1 "
               "(most YouTube 4K) the best H.264 / HEVC stream is used instead, often 1080p. "
               "Use Auto for true 4K.",
}
PRORES_PROFILES = {"422 HQ  (profile 3)": 3, "422 Standard  (profile 2)": 2}
PRORES_NAMES = {3: "ProRes 422 HQ", 2: "ProRes 422"}
PRORES_MBPS_2160P30 = {3: 707, 2: 471}      # Apple data rates for 3840x2160 at 29.97 fps

# Encoding speed: applies only when a VP9 / AV1 source has to be converted to H.264.
SPEED_FAST, SPEED_BALANCED, SPEED_BEST = "Fast", "Balanced", "Best quality"
SPEED_HINTS = {
    SPEED_FAST: "Fastest. Uses your graphics chip (VideoToolbox on Mac, NVENC on NVIDIA) when "
                "ffmpeg offers it, otherwise a fast software preset. If the chip fails, the "
                "app retries in software automatically.",
    SPEED_BALANCED: "Software H.264 (preset fast, CRF 16): very close to visually lossless and "
                    "much faster than Best quality.",
    SPEED_BEST: "Software H.264 (preset slow, CRF 14): the highest quality per file size, but "
                "a 4K conversion can take longer than the video itself.",
}
# (x264 preset, CRF, approximate Mbit/s at 3840x2160 and 30 fps used for the size estimate)
H264_SOFTWARE = {SPEED_FAST: ("veryfast", "18", 40), SPEED_BALANCED: ("fast", "16", 55),
                 SPEED_BEST: ("slow", "14", 80)}
AAC_BITRATE = "320k"
MOV_COPY_AUDIO = {"aac", "alac", "mp3", "ac3", "eac3", "pcm_s16le", "pcm_s24le"}
# ffmpeg's QuickTime muxer refuses VP9 and AV1 ("only supported in MP4"), so those can never be
# stream-copied into a real .mov. Strict Stream Copy therefore uses the best H.264 / HEVC stream.
MOV_COPY_VIDEO = {"h264", "hevc", "prores", "mpeg4", "mjpeg"}


def build_copy_selector(cap: int | None) -> str:
    """Strict Stream Copy: best H.264 / HEVC stream (what a .mov can hold) up to the cap."""
    v = "[vcodec~='^(avc|hev|hvc)']"
    h = f"[height<={cap}]" if cap else ""
    return f"bestvideo{v}{h}+bestaudio/best{v}{h}"


DEFAULT_FOLDER_NAME = "YT4K Downloads"

# ----------------------------------------------------------------- logging --
try:
    LOG_DIR = Path.home() / ".yt4k_download"
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    _handler: logging.Handler = RotatingFileHandler(LOG_DIR / "app.log", maxBytes=500_000,
                                                    backupCount=2, encoding="utf-8")
except OSError:  # read-only home directory: log to the console instead
    LOG_DIR = Path(".")
    _handler = logging.StreamHandler()
log = logging.getLogger("yt4k_download")
log.setLevel(logging.INFO)
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
log.addHandler(_handler)
SETTINGS_FILE = LOG_DIR / "settings.json"


def load_settings() -> dict:
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(data: dict) -> None:
    try:
        SETTINGS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError as exc:
        log.warning("could not save settings: %s", exc)


class Yt4kError(Exception):
    """Error whose message can be shown directly to the user."""


class Cancelled(Exception):
    """User pressed Cancel."""


# -------------------------------------------------------------- data types --
@dataclass
class StreamInfo:
    url: str = ""
    video_id: str = ""
    title: str = ""
    channel: str = ""
    duration: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    vcodec_raw: str = ""
    vfamily: str = ""
    acodec_raw: str = ""
    afamily: str = ""
    has_audio: bool = True
    hdr: bool = False
    size: float = 0.0
    size_exact: bool = False
    max_width: int = 0
    max_height: int = 0
    n_streams: int = 1
    selector: str = FORMAT_SELECTOR
    copy_width: int = 0          # best H.264 / HEVC stream (what Strict Stream Copy can use)
    copy_height: int = 0
    copy_family: str = ""
    cap: int | None = None       # height cap that was requested (None = best available)
    available: tuple = ()        # ((height, max_fps), ...) offered by the link, tallest first
    raw: dict | None = field(default=None, repr=False, compare=False)   # yt-dlp result, reused
    fetched_at: float = 0.0      # when the video page was analysed (epoch seconds)

    @property
    def is_4k(self) -> bool:
        return self.height >= 2160 or self.width >= 3840

    def fresh(self, url: str) -> bool:
        """True when the cached yt-dlp result can be reused for this link."""
        return bool(self.raw) and self.url == url and time.time() - self.fetched_at < INFO_MAX_AGE


@dataclass
class JobConfig:
    url: str
    out_dir: Path
    mode: str
    profile: int = 3
    height_cap: int | None = None     # None = Best Available
    speed: str = SPEED_FAST
    cached: StreamInfo | None = None  # result of an earlier Fetch Info, reused when still fresh


@dataclass(frozen=True)
class Plan:
    video: str            # "copy" | "x264" | "prores"
    audio: str            # "copy" | "aac" | "pcm" | "none"
    profile: int = 3      # ProRes profile (only used when video == "prores")
    hdr_forced: bool = False
    speed: str = SPEED_FAST   # H.264 encoding speed (only used when video == "x264")


# ----------------------------------------------------------------- helpers --
def fmt_bytes(n: float) -> str:
    if n <= 0:
        return "unknown"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def fmt_eta(seconds: float) -> str:
    s = max(int(seconds), 0)
    h, m, s = s // 3600, s % 3600 // 60, s % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_hms(t: float) -> str:
    s = int(round(max(t, 0.0)))
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_RESERVED_NAMES = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
                   *(f"lpt{i}" for i in range(1, 10))}
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def sanitize_filename(name: str, fallback: str = "video", max_len: int = 120) -> str:
    """Make a title safe as a file name on Windows, macOS and Linux."""
    s = unicodedata.normalize("NFKC", name or "")
    s = "".join(ch for ch in s if unicodedata.category(ch) not in ("Cc", "Cf"))
    s = _INVALID_CHARS.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip(" .")
    if len(s) > max_len:
        s = s[:max_len].rstrip(" .")
    if not s:
        s = _INVALID_CHARS.sub("_", fallback).strip(" .") or "video"
    if s.split(".")[0].strip().lower() in _RESERVED_NAMES:
        s = "_" + s
    return s


def unique_path(path: Path) -> Path:
    """Never overwrite: add ' (1)', ' (2)' ... when the file already exists."""
    if not path.exists():
        return path
    for i in range(1, 1000):
        cand = path.with_name(f"{path.stem} ({i}){path.suffix}")
        if not cand.exists():
            return cand
    return path.with_name(f"{path.stem} ({int(time.time())}){path.suffix}")


def normalize_url(text: str) -> str:
    """Return a usable http(s) URL, or '' when the text does not look like a link."""
    t = text.strip()
    if re.match(r"^https?://\S+$", t, re.I):
        return t
    if re.match(r"^(www\.)?[\w-]+(\.[\w-]+)*\.[a-z]{2,}(/\S*)?$", t, re.I):
        return "https://" + t
    return ""


def default_folder() -> Path:
    base = Path.home() / ("Movies" if sys.platform == "darwin" else "Videos")
    return base / DEFAULT_FOLDER_NAME


def video_family(codec: str) -> str:
    """Normalise yt-dlp codec strings (avc1.640033, vp09.00.51.08, av01...) and ffprobe names."""
    c = (codec or "").lower()
    if c.startswith(("avc", "h264")):
        return "h264"
    if c.startswith(("hev", "hvc", "h265")):
        return "hevc"
    if c.startswith(("vp09", "vp9")):
        return "vp9"
    if c.startswith(("av01", "av1")):
        return "av1"
    if c.startswith(("vp08", "vp8")):
        return "vp8"
    return c.split(".")[0] if c else "unknown"


def audio_family(codec: str) -> str:
    c = (codec or "").lower()
    if c.startswith("mp4a.6b") or c == "mp3":
        return "mp3"
    if c.startswith(("mp4a", "aac")):
        return "aac"
    if c.startswith("ec-3"):
        return "eac3"
    if c.startswith("ac-3"):
        return "ac3"
    return c.split(".")[0] if c else ""


def family_name(family: str) -> str:
    return {"h264": "H.264", "hevc": "HEVC", "vp9": "VP9", "av1": "AV1"}.get(family,
                                                                             family.upper())


def codec_label(raw: str, family: str) -> str:
    names = {"h264": "avc1 (H.264)", "hevc": "hvc1 (HEVC)", "vp9": "vp9", "av1": "av01 (AV1)"}
    return names.get(family, raw or family or "unknown")


def quality_name(cap: int | None) -> str:
    return "best available" if not cap else f"{cap}p"


def tier_offered(heights, cap: int) -> bool:
    floor = TIER_FLOOR.get(cap, 0)
    return any(floor < h <= cap for h in heights)


def predict_quality(available, cap: int | None):
    """The resolution the selector will pick: (height, fps, offered), or None if unknown."""
    if not available:
        return None
    heights = sorted({h for h, _ in available}, reverse=True)
    if cap is None:
        h, offered = heights[0], True
    else:
        under = [x for x in heights if x <= cap]
        h = under[0] if under else heights[-1]
        offered = tier_offered(heights, cap)
    fps = max(f for x, f in available if x == h)
    return h, fps, offered


def format_available(available) -> str:
    parts = [f"{h}p{int(round(fps))}" if fps > 30.5 else f"{h}p" for h, fps in available]
    return ", ".join(parts) if parts else "-"


def friendly_dl_error(exc: Exception) -> str:
    msg = _ANSI.sub("", str(exc)).strip()
    msg = re.sub(r"^ERROR:\s*", "", msg)
    low = msg.lower()
    hint = ""
    if "sign in" in low or "confirm your age" in low or "age-restricted" in low \
            or "not a bot" in low or "members-only" in low:
        hint = ("This video needs a signed-in session (age-restricted, members-only or a bot "
                "check). This app downloads public videos without cookies.")
    elif "private video" in low or "video unavailable" in low or "has been removed" in low:
        hint = "The video is private, removed or not available in your region."
    elif "requested format is not available" in low or "403" in low:
        hint = ("Update yt-dlp (pip install -U \"yt-dlp[default]\") and make sure a JavaScript "
                "runtime such as Deno is installed. See the README.")
    elif any(k in low for k in ("getaddrinfo", "urlopen error", "timed out", "connection")):
        hint = "Check your internet connection and try again."
    return msg + ("\n\n" + hint if hint else "")


def require_tools() -> tuple[str, str]:
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise Yt4kError(
            "ffmpeg was not found.\nmacOS: brew install ffmpeg\n"
            "Windows: winget install Gyan.FFmpeg (restart the terminal)\n"
            "Linux: sudo apt install ffmpeg")
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


def pick_encoder(candidates: list, encoders, kind: str, label: str) -> str:
    for name in candidates:
        if name in encoders:
            return name
    raise Yt4kError(
        f"Your ffmpeg build has no {kind} encoder for {label} ({' or '.join(candidates)}).\n"
        "Install a full ffmpeg build:\nmacOS: brew reinstall ffmpeg\n"
        "Windows: winget install Gyan.FFmpeg\nLinux: sudo apt install ffmpeg")


def js_runtime_found() -> bool:
    return bool(shutil.which("deno"))


def open_path(path: Path) -> None:
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", str(path)])
        elif sys.platform.startswith("win"):
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            subprocess.run(["xdg-open", str(path)])
    except OSError as exc:
        raise Yt4kError(f"Could not open it: {exc}")


# --------------------------------------------------------------- stream info --
class YDLLogger:
    """Routes yt-dlp messages into the activity log (only the useful ones)."""

    def __init__(self, emit) -> None:
        self.emit = emit
        self._seen: set = set()

    def debug(self, msg: str) -> None:
        msg = _ANSI.sub("", msg)
        if msg.startswith("[info]"):
            self.emit("log", msg)

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        msg = re.sub(r"^WARNING:\s*", "", _ANSI.sub("", msg)).strip()
        if msg and msg not in self._seen:
            self._seen.add(msg)
            self.emit("log", "Warning: " + msg[:300])

    def error(self, msg: str) -> None:
        log.error("yt-dlp: %s", _ANSI.sub("", msg))


def _has(fmt: dict, key: str) -> bool:
    return (fmt.get(key) or "none") != "none"


def parse_info(url: str, raw: dict | None, selector: str = FORMAT_SELECTOR,
               cap: int | None = None) -> StreamInfo:
    if not raw:
        raise Yt4kError("No video information could be read from this link.")
    if raw.get("_type") in ("playlist", "multi_video"):
        raise Yt4kError("This link is a playlist. Paste the link of a single video.")
    if raw.get("is_live"):
        raise Yt4kError("Live streams cannot be downloaded. Wait until the stream ends.")
    reqs = raw.get("requested_formats") or [raw]
    vfmt = next((f for f in reqs if _has(f, "vcodec")), None)
    if vfmt is None:
        raise Yt4kError("No video stream was found for this link.")
    afmt = next((f for f in reqs if _has(f, "acodec")), None)
    duration = float(raw.get("duration") or 0)

    size, exact = 0.0, True
    for f in reqs:
        s = f.get("filesize")
        if not s:
            exact = False
            s = f.get("filesize_approx") or (f.get("tbr") or 0) * 125 * duration
        size += float(s or 0)

    max_w = max_h = 0
    best_copy = None
    offered: dict[int, float] = {}
    for f in raw.get("formats") or []:
        if not _has(f, "vcodec"):
            continue
        h = int(f.get("height") or 0)
        if h > 0:
            offered[h] = max(offered.get(h, 0.0), float(f.get("fps") or 0))
        if h >= max_h:
            max_h, max_w = h, int(f.get("width") or 0)
        fam = video_family(str(f.get("vcodec") or ""))
        if fam in ("h264", "hevc") and 0 < h <= (cap or 10 ** 6):
            key = (h, f.get("fps") or 0, f.get("tbr") or 0)
            if best_copy is None or key > best_copy[0]:
                best_copy = (key, int(f.get("width") or 0), h, fam)

    vcodec = str(vfmt.get("vcodec") or "")
    acodec = str((afmt or {}).get("acodec") or "")
    return StreamInfo(
        url=url, video_id=str(raw.get("id") or ""),
        title=str(raw.get("title") or raw.get("id") or "video"),
        channel=str(raw.get("channel") or raw.get("uploader") or ""),
        duration=duration,
        width=int(vfmt.get("width") or 0), height=int(vfmt.get("height") or 0),
        fps=float(vfmt.get("fps") or 0),
        vcodec_raw=vcodec, vfamily=video_family(vcodec),
        acodec_raw=acodec, afamily=audio_family(acodec),
        has_audio=afmt is not None,
        hdr=str(vfmt.get("dynamic_range") or "SDR").upper() != "SDR",
        size=size, size_exact=exact,
        max_width=max_w, max_height=max_h, n_streams=len(reqs), selector=selector,
        copy_width=best_copy[1] if best_copy else 0,
        copy_height=best_copy[2] if best_copy else 0,
        copy_family=best_copy[3] if best_copy else "",
        cap=cap, available=tuple(sorted(offered.items(), reverse=True)),
        raw=raw, fetched_at=time.time())


def fetch_info(url: str, emit, selector: str = FORMAT_SELECTOR,
               cap: int | None = None) -> StreamInfo:
    """Analyse the video page (network, slow: 5 to 20 seconds)."""
    opts = {"format": selector, "format_sort": FORMAT_SORT, "noplaylist": True, "quiet": True,
            "no_color": True, "socket_timeout": 30, "extractor_retries": 3,
            "logger": YDLLogger(emit)}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            raw = ydl.extract_info(url, download=False)
    except DownloadError as exc:
        raise Yt4kError(friendly_dl_error(exc))
    return parse_info(url, raw, selector, cap)


def select_offline(url: str, raw: dict, selector: str, cap: int | None) -> StreamInfo:
    """Pick formats again from an already fetched result: no network, finishes instantly."""
    opts = {"format": selector, "format_sort": FORMAT_SORT, "noplaylist": True, "quiet": True,
            "no_color": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        processed = ydl.process_ie_result(copy.deepcopy(raw), download=False)
    info = parse_info(url, processed, selector, cap)
    info.raw, info.fetched_at = raw, 0.0
    return info


def reuse_or_fetch(url: str, emit, selector: str, cap: int | None,
                   cached: StreamInfo | None, announce: bool = True) -> StreamInfo:
    """Reuse the earlier lookup when possible, otherwise ask the website."""
    if cached is not None and cached.fresh(url):
        try:
            info = select_offline(url, cached.raw, selector, cap)   # type: ignore[arg-type]
            info.fetched_at = cached.fetched_at
            if announce:
                emit("log", "Reusing the video information fetched earlier (no new lookup).")
            return info
        except Exception as exc:  # noqa: BLE001
            log.warning("offline format selection failed, fetching again: %s", exc)
    return fetch_info(url, emit, selector, cap)


# ------------------------------------------------------------------- probing --
def _num(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _rate(text: str) -> float:
    try:
        a, _, b = (text or "0/1").partition("/")
        return float(a) / float(b or 1)
    except (ValueError, ZeroDivisionError):
        return 0.0


def probe(ffprobe: str, path: Path, fallback_duration: float = 0.0) -> dict:
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=120, encoding="utf-8", errors="replace",
            **POPEN_KW)
        data = json.loads(out.stdout or "{}")
    except (subprocess.SubprocessError, OSError, ValueError):
        data = {}
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if not video:
        raise Yt4kError("The downloaded file does not contain a readable video stream.")
    fmt = data.get("format", {})
    duration = (_num(fmt.get("duration")) or _num(video.get("duration")) or fallback_duration)
    fps = _rate(video.get("avg_frame_rate")) or _rate(video.get("r_frame_rate"))
    return {"width": int(video.get("width") or 0), "height": int(video.get("height") or 0),
            "fps": fps, "duration": duration, "has_audio": audio is not None,
            "video_codec": video.get("codec_name", ""),
            "audio_codec": (audio or {}).get("codec_name", ""),
            "pix_fmt": video.get("pix_fmt", ""),
            "hdr": video.get("color_transfer") in ("smpte2084", "arib-std-b67"),
            "format_name": fmt.get("format_name", ""),
            "size": path.stat().st_size}


# ------------------------------------------------------------------ the plan --
def make_plan(mode: str, vfamily: str, afamily: str, has_audio: bool, hdr: bool,
              profile: int = 3, speed: str = SPEED_FAST) -> Plan:
    """Decide what happens to the video and audio for the chosen .mov mode."""
    if mode == MODE_PRORES:
        return Plan("prores", "pcm" if has_audio else "none", profile, speed=speed)
    audio = ("copy" if afamily in MOV_COPY_AUDIO else "aac") if has_audio else "none"
    if mode == MODE_COPY or vfamily in ("h264", "hevc"):
        return Plan("copy", audio, profile, speed=speed)
    if hdr:  # 8-bit H.264 would flatten HDR colors: ProRes keeps 10-bit
        return Plan("prores", "pcm" if has_audio else "none", profile, hdr_forced=True,
                    speed=speed)
    return Plan("x264", audio, profile, speed=speed)


def h264_choice(speed: str, encoders, width: int, height: int, fps: float,
                software_only: bool = False) -> tuple[str, list, bool]:
    """Pick the H.264 encoder for the speed setting: (label, ffmpeg args, is_hardware)."""
    preset, crf, _mbps = H264_SOFTWARE.get(speed, H264_SOFTWARE[SPEED_BALANCED])
    if speed == SPEED_FAST and not software_only:
        bitrate = int(min(max(max(width, 1) * max(height, 1) * (fps or 30) * 0.16, 8e6), 120e6))
        if "h264_videotoolbox" in encoders:
            return ("H.264 hardware encode (VideoToolbox)",
                    ["-c:v", "h264_videotoolbox", "-b:v", str(bitrate), "-profile:v", "high",
                     "-pix_fmt", "yuv420p"], True)
        if "h264_nvenc" in encoders:
            return ("H.264 hardware encode (NVENC)",
                    ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "19",
                     "-b:v", "0", "-profile:v", "high", "-pix_fmt", "yuv420p"], True)
    return (f"H.264 transcode (libx264, CRF {crf}, {preset}, yuv420p)",
            ["-c:v", "libx264", "-preset", preset, "-crf", crf, "-pix_fmt", "yuv420p"], False)


def describe_plan(plan: Plan, encoders=frozenset()) -> str:
    video = {"copy": "video copied untouched (-c:v copy)",
             "x264": h264_choice(plan.speed, encoders, 3840, 2160, 30)[0],
             "prores": f"{PRORES_NAMES.get(plan.profile, 'ProRes 422')} (10-bit)"}[plan.video]
    audio = {"copy": "audio copied", "aac": f"audio to AAC {AAC_BITRATE}",
             "pcm": "audio to PCM 24-bit", "none": "no audio track"}[plan.audio]
    return f"{video}; {audio}"


def estimate_output_bytes(plan: Plan, info: StreamInfo) -> float:
    if plan.video == "copy":
        return info.size
    dur = info.duration
    if dur <= 0:
        return info.size * 4
    scale = (max(info.width, 1) * max(info.height, 1) / (3840 * 2160)) * ((info.fps or 30) / 29.97)
    if plan.video == "prores":
        return dur * PRORES_MBPS_2160P30.get(plan.profile, 707) * 1e6 / 8 * scale
    mbps = H264_SOFTWARE.get(plan.speed, H264_SOFTWARE[SPEED_BALANCED])[2]
    return max(info.size, dur * mbps * 1e6 / 8 * scale)


def build_command(ffmpeg: str, src: Path, partial: Path, plan: Plan, src_info: dict,
                  title: str, encoders, software_only: bool = False) -> list:
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-progress", "pipe:1",
           "-nostats", "-y", "-i", str(src),
           "-map", "0:v:0", "-map", "0:a:0?", "-map_metadata", "0", "-map_chapters", "-1"]
    vf: list = []
    if plan.video == "copy":
        cmd += ["-c:v", "copy"]
        if src_info["video_codec"] == "hevc":
            cmd += ["-tag:v", "hvc1"]       # lets QuickTime / Apple devices play H.265
    elif plan.video == "x264":
        _label, args, _hw = h264_choice(plan.speed, encoders, src_info["width"],
                                        src_info["height"], src_info["fps"], software_only)
        if args[1] == "libx264":
            pick_encoder(["libx264"], encoders, "video", "H.264")
        cmd += args
        if src_info["width"] % 2 or src_info["height"] % 2:  # 4:2:0 needs even dimensions
            vf.append("scale=trunc(iw/2)*2:trunc(ih/2)*2")
    else:
        pick_encoder(["prores_ks"], encoders, "video", "Apple ProRes")
        cmd += ["-c:v", "prores_ks", "-profile:v", str(plan.profile), "-vendor", "apl0",
                "-pix_fmt", "yuv422p10le"]
    if vf:
        cmd += ["-vf", ",".join(vf)]
    if not src_info["has_audio"] or plan.audio == "none":
        cmd += ["-an"]
    elif plan.audio == "copy":
        cmd += ["-c:a", "copy"]
    elif plan.audio == "pcm":
        cmd += ["-c:a", "pcm_s24le"]
    else:
        cmd += ["-c:a", "aac", "-b:a", AAC_BITRATE]
    if title:
        cmd += ["-metadata", f"title={title}"]
    if plan.video != "prores":              # faststart would rewrite multi-GB ProRes files
        cmd += ["-movflags", "+faststart"]
    # -f forces the QuickTime container, so the file really is a .mov whatever its name is
    cmd += ["-f", "mov", str(partial)]
    return cmd


def run_ffmpeg(cmd, total, on_frac, cancel, proc_box) -> None:
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace", bufsize=1, **POPEN_KW)
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
        raise Yt4kError("ffmpeg reported an error:\n" + "\n".join(tail))


def container_signature_ok(path: Path) -> bool:
    """A real QuickTime .mov starts with an 'ftyp' box whose brand is 'qt  '."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(64)
    except OSError:
        return False
    return head[4:8] == b"ftyp" and head[8:12] == b"qt  "


def verify_output(ffprobe: str, path: Path, plan: Plan, src: dict):
    """Re-open the finished .mov and confirm it is what was asked for."""
    try:
        out = probe(ffprobe, path, fallback_duration=src["duration"])
    except Yt4kError:
        raise Yt4kError("The finished .mov could not be read back. Please try again.")
    warnings: list[str] = []
    expected = {"copy": {src["video_codec"]}, "x264": {"h264"}, "prores": {"prores"}}[plan.video]
    if out["video_codec"] not in expected:
        raise Yt4kError(f"The video codec in the .mov ({out['video_codec']}) is not what "
                        "was requested. The file was not saved.")
    if "mov" not in out["format_name"] or not container_signature_ok(path):
        raise Yt4kError(f"The container ({out['format_name']}) is not a QuickTime .mov. "
                        "The file was not saved.")
    if src["has_audio"] and plan.audio != "none" and not out["has_audio"]:
        raise Yt4kError("The finished .mov has no audio track. The file was not saved.")
    if out["duration"] <= 0:
        raise Yt4kError("The finished .mov has no duration. The file was not saved.")
    if abs(out["width"] - src["width"]) > 1 or abs(out["height"] - src["height"]) > 1:
        raise Yt4kError(f"The .mov size ({out['width']}x{out['height']}) differs from the "
                        f"source ({src['width']}x{src['height']}). The file was not saved.")
    if src["duration"] > 0:
        drift = abs(out["duration"] - src["duration"])
        if drift > max(2.0, src["duration"] * 0.05):
            raise Yt4kError(f"The .mov is {out['duration']:.1f}s long but the source is "
                            f"{src['duration']:.1f}s. The file was not saved.")
        if drift > 0.5:
            warnings.append(f"Length differs by {drift:.2f}s from the source.")
    return out, warnings


# ------------------------------------------------------------- download engine --
def download_streams(url: str, info: StreamInfo, tmp: Path, ffmpeg: str, emit, cancel,
                     dl_start: float, dl_end: float) -> Path:
    """Download the best video (up to the chosen height) + best audio into tmp."""
    files: dict[str, list] = {}
    last = [0.0]

    def on_progress(d: dict) -> None:
        if cancel.is_set():
            raise Cancelled()
        status = d.get("status")
        key = str(d.get("filename") or d.get("tmpfilename") or "?")
        if status == "downloading":
            done = float(d.get("downloaded_bytes") or 0)
            total = float(d.get("total_bytes") or d.get("total_bytes_estimate") or 0)
            files[key] = [done, max(total, done)]
        elif status == "finished":
            done = float(d.get("total_bytes") or d.get("downloaded_bytes")
                         or files.get(key, [0, 0])[0])
            files[key] = [done, done]
        else:
            return
        now = time.monotonic()
        if status == "downloading" and now - last[0] < 0.12:
            return
        last[0] = now
        done_sum = sum(v[0] for v in files.values())
        seen_total = sum(v[1] for v in files.values())
        est = max(info.size, seen_total)
        if info.size <= 0 and len(files) < info.n_streams:   # audio not started yet: guess
            est = seen_total * (1 + 0.1 * (info.n_streams - len(files)))
        frac = min(done_sum / est, 0.995) if est > 0 else 0.0
        speed = float(d.get("speed") or 0)
        text = f"{int(frac * 100)}%"
        if speed > 0:
            text += f"   |   {fmt_bytes(speed)}/s"
            if est > done_sum:
                text += f"   |   ETA {fmt_eta((est - done_sum) / speed)}"
        emit("progress", {"frac": dl_start + frac * (dl_end - dl_start), "text": text})
        emit("detail", f"Downloaded {fmt_bytes(done_sum)} of about {fmt_bytes(est)}")

    def on_post(d: dict) -> None:
        if d.get("status") == "started" and d.get("postprocessor") == "Merger":
            emit("status", "Downloading video & audio... (merging streams)")

    opts = {"format": info.selector, "format_sort": FORMAT_SORT, "noplaylist": True,
            "quiet": True, "no_color": True,
            "logger": YDLLogger(emit), "ffmpeg_location": ffmpeg,
            "merge_output_format": "mkv", "outtmpl": str(tmp / "source.%(ext)s"),
            "retries": 8, "fragment_retries": 8, "extractor_retries": 3,
            "socket_timeout": 30, "concurrent_fragment_downloads": 8,
            "overwrites": True, "windowsfilenames": True,
            "progress_hooks": [on_progress], "postprocessor_hooks": [on_post]}
    with yt_dlp.YoutubeDL(opts) as ydl:
        result = None
        if info.fresh(url):
            # Reuse the page analysis: saves a full second lookup before the download starts.
            try:
                result = ydl.process_ie_result(copy.deepcopy(info.raw), download=True)
            except DownloadError as exc:
                if cancel.is_set():
                    raise
                log.warning("cached stream links failed, fetching again: %s", exc)
                emit("log", "The saved stream links were rejected: looking the video up again.")
        if result is None:
            result = ydl.extract_info(url, download=True)
    if cancel.is_set():
        raise Cancelled()

    candidates: list[Path] = []
    for rd in (result or {}).get("requested_downloads") or []:
        fp = rd.get("filepath") or rd.get("_filename")
        if fp and Path(fp).is_file():
            candidates.append(Path(fp))
    if not candidates:
        skip = re.compile(r"^source\.f[\w-]+\.\w+$")
        candidates = [p for p in tmp.glob("source.*")
                      if p.is_file() and not skip.match(p.name)
                      and not p.name.endswith((".part", ".ytdl", ".temp"))]
    if not candidates:
        raise Yt4kError("The download finished but no video file was found.")
    return max(candidates, key=lambda p: p.stat().st_size)


def run_fetch(url: str, emit, cancel, cap: int | None = None,
              cached: StreamInfo | None = None) -> None:
    try:
        emit("stage", 0)
        emit("status", "Fetching streams...")
        info = reuse_or_fetch(url, emit, build_selector(cap), cap, cached)
        if cancel.is_set():
            raise Cancelled()
        emit("info", info)
        emit("fetched", info)
    except Cancelled:
        emit("cancelled", None)
    except Yt4kError as exc:
        emit("error", str(exc))
    except DownloadError as exc:
        emit("error", friendly_dl_error(exc))
    except Exception as exc:  # noqa: BLE001
        log.exception("unexpected error while fetching info")
        emit("error", f"Unexpected error: {exc}\nDetails: {LOG_DIR / 'app.log'}")


def run_download(cfg: JobConfig, emit, cancel, proc_box) -> None:
    """Fetch -> download -> remux/transcode -> verify. Temporary files are always removed."""
    tmp: Path | None = None
    partial: Path | None = None
    cap = cfg.height_cap

    def cleanup() -> None:
        if partial is not None:
            partial.unlink(missing_ok=True)
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)

    try:
        ffmpeg, ffprobe = require_tools()
        encoders = available_encoders(ffmpeg)
        emit("stage", 0)
        emit("status", "Fetching streams...")
        emit("progress", {"frac": 0.0, "text": ""})
        emit("detail", "")
        cfg.out_dir.mkdir(parents=True, exist_ok=True)
        if not os.access(cfg.out_dir, os.W_OK):
            raise Yt4kError(f"The output folder is not writable:\n{cfg.out_dir}")

        info = reuse_or_fetch(cfg.url, emit, build_selector(cap), cap, cfg.cached)
        if cancel.is_set():
            raise Cancelled()
        strict_fallback = False
        if cfg.mode == MODE_COPY and info.vfamily not in MOV_COPY_VIDEO:
            if info.copy_height <= 0:
                raise Yt4kError(
                    f"The best stream of this video is {family_name(info.vfamily)}, and no "
                    "H.264 or HEVC stream is offered. A .mov cannot hold VP9 / AV1 without "
                    "re-encoding, "
                    "so Strict Stream Copy is not possible for this video.\n\n"
                    "Choose Auto or ProRes 422 instead.")
            emit("log", f"Note: the {info.width}x{info.height} {family_name(info.vfamily)} stream "
                        "cannot be stored in .mov without re-encoding, so Strict Stream Copy "
                        f"uses the best {family_name(info.copy_family)} stream "
                        f"({info.copy_width}x{info.copy_height}) instead. Use Auto for the "
                        "full resolution.")
            info = reuse_or_fetch(cfg.url, emit, build_copy_selector(cap), cap, info,
                                  announce=False)
            strict_fallback = True
            if cancel.is_set():
                raise Cancelled()
        emit("info", info)
        emit("log", f"Found: {info.title}")
        emit("log", f"Stream: {info.width}x{info.height} @ {info.fps:g} fps, "
                    f"{codec_label(info.vcodec_raw, info.vfamily)}, audio "
                    f"{info.acodec_raw or 'none'}")
        offered_heights = [h for h, _ in info.available]
        if strict_fallback:
            pass                                   # already explained in the note above
        elif cap is None:
            emit("log", f"Best available quality selected: {info.width}x{info.height}.")
        elif info.height > cap:
            emit("log", f"No stream at or below {cap}p is offered: using the lowest available "
                        f"({info.width}x{info.height}).")
        elif tier_offered(offered_heights, cap):
            emit("log", f"{cap}p quality selected: downloading {info.width}x{info.height}.")
        else:
            emit("log", f"{cap}p is not offered by this video: falling back to the highest "
                        f"available ({info.width}x{info.height}).")
        if (cap is None or cap >= 2160) and info.height < 2160 and not js_runtime_found():
            emit("log", "Tip: YouTube may hide 4K formats without a JavaScript runtime. "
                        "Install Deno and update yt-dlp if this video should have 4K "
                        "(see README).")

        plan0 = make_plan(cfg.mode, info.vfamily, info.afamily, info.has_audio, info.hdr,
                          cfg.profile, cfg.speed)
        needed = info.size * 1.25 + estimate_output_bytes(plan0, info) * 1.1 + 200e6
        free = shutil.disk_usage(cfg.out_dir).free
        if free < needed:
            raise Yt4kError(
                "Not enough free disk space in the output folder.\n"
                f"Needed: about {fmt_bytes(needed)}   Free: {fmt_bytes(free)}")

        dl_end = 0.85 if plan0.video == "copy" else 0.5
        tmp = cfg.out_dir / f".yt4k_tmp_{os.getpid()}_{int(time.time())}"
        tmp.mkdir(parents=True, exist_ok=True)

        emit("stage", 1)
        emit("status", f"Downloading {info.width}x{info.height} video & audio...")
        emit("progress", {"frac": 0.02, "text": "0%"})
        src = download_streams(cfg.url, info, tmp, ffmpeg, emit, cancel, 0.02, dl_end)
        emit("progress", {"frac": dl_end, "text": "Download complete"})
        emit("detail", f"Downloaded {fmt_bytes(src.stat().st_size)}")
        if cancel.is_set():
            raise Cancelled()

        src_info = probe(ffprobe, src, fallback_duration=info.duration)
        if src_info["duration"] <= 0:
            raise Yt4kError("Could not read the video duration. Is the download corrupted?")
        plan = make_plan(cfg.mode, video_family(src_info["video_codec"]),
                         audio_family(src_info["audio_codec"]), src_info["has_audio"],
                         src_info["hdr"], cfg.profile, cfg.speed)
        emit("log", f"Downloaded: {src_info['width']}x{src_info['height']}, "
                    f"{src_info['video_codec']}/{src_info['audio_codec'] or 'no audio'}, "
                    f"{src_info['duration']:.1f}s")
        emit("log", f"Quality: {quality_name(cap)} requested, "
                    f"{src_info['width']}x{src_info['height']} downloaded.")
        emit("log", "Mode: " + describe_plan(plan, encoders))
        if plan.hdr_forced:
            emit("log", "Note: HDR source detected, so ProRes 422 is used to keep 10-bit color.")
        if plan.video == "copy" and video_family(src_info["video_codec"]) not in MOV_COPY_VIDEO:
            raise Yt4kError(
                f"The downloaded video codec ({src_info['video_codec']}) cannot be stored in "
                ".mov without re-encoding. Choose Auto or ProRes 422 instead.")
        if plan.audio == "aac":
            emit("log", "Note: the audio codec cannot be stored in .mov as-is, so only the "
                        f"audio is converted to AAC {AAC_BITRATE}.")

        height = src_info["height"] or info.height
        base = sanitize_filename(info.title, fallback=info.video_id or "video")
        output = unique_path(cfg.out_dir / f"{base} [{height}p].mov")
        if output.stem != f"{base} [{height}p]":
            emit("log", f"A file with that name exists: saving as {output.name}")
        partial = cfg.out_dir / f".yt4k_{os.getpid()}.partial.mov"

        emit("stage", 2)
        emit("status", "Remuxing to .mov..." if plan.video == "copy"
             else "Transcoding to .mov...")
        emit("detail", "Writing " + output.name)
        t0 = [time.monotonic()]

        def on_frac(f: float) -> None:
            text = f"{int(f * 100)}%"
            elapsed = time.monotonic() - t0[0]
            if f > 0.03 and elapsed > 2:
                text += f"   |   about {fmt_eta(elapsed * (1 - f) / f)} left"
            emit("progress", {"frac": dl_end + (1 - dl_end) * 0.97 * f, "text": text})

        def encode(software_only: bool) -> None:
            cmd = build_command(ffmpeg, src, partial, plan, src_info, info.title, encoders,
                                software_only)
            log.info("ffmpeg: %s", " ".join(cmd))
            t0[0] = time.monotonic()
            run_ffmpeg(cmd, src_info["duration"], on_frac, cancel, proc_box)

        try:
            encode(False)
        except Yt4kError as exc:
            uses_hw = plan.video == "x264" and h264_choice(
                plan.speed, encoders, src_info["width"], src_info["height"],
                src_info["fps"])[2]
            if not uses_hw:
                raise
            log.warning("hardware encode failed: %s", exc)
            emit("log", "The hardware encoder failed on this system: retrying with software "
                        "H.264 (libx264).")
            partial.unlink(missing_ok=True)
            encode(True)

        emit("status", "Verifying the .mov file...")
        out, warnings = verify_output(ffprobe, partial, plan, src_info)
        for w in warnings:
            emit("log", "Warning: " + w)
        os.replace(partial, output)
        partial = None
        cleanup()
        emit("log", "Temporary files removed.")
        emit("stage", 3)
        emit("progress", {"frac": 1.0, "text": "100%"})
        emit("done", {"path": output, "size": output.stat().st_size,
                      "vcodec": out["video_codec"], "acodec": out["audio_codec"],
                      "duration": out["duration"], "width": out["width"],
                      "height": out["height"], "plan": plan})
    except Cancelled:
        cleanup()
        emit("cancelled", None)
    except Yt4kError as exc:
        log.error("%s", exc)
        cleanup()
        emit("error", str(exc))
    except DownloadError as exc:
        cleanup()
        if cancel.is_set():
            emit("cancelled", None)
        else:
            log.error("yt-dlp: %s", exc)
            emit("error", friendly_dl_error(exc))
    except Exception as exc:  # noqa: BLE001
        cleanup()
        if cancel.is_set():
            emit("cancelled", None)
        else:
            log.exception("unexpected error")
            emit("error", f"Unexpected error: {exc}\nDetails: {LOG_DIR / 'app.log'}")
    finally:
        cleanup()


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


def option_menu(parent, values, command=None):
    return ctk.CTkOptionMenu(
        parent, values=list(values), height=40, corner_radius=8, fg_color=LAV_1,
        button_color=LAV_4, button_hover_color=LAV_5, text_color=TEXT, anchor="w",
        dropdown_fg_color=WHITE, dropdown_hover_color=LAV_1, dropdown_text_color=TEXT,
        font=F(13), dropdown_font=F(13), command=command)


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
        try:  # size to content and center over the parent (real pixels: no DPI double-scaling)
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
    STAGES = ("Fetch", "Download", "Remux/Encode", "Complete")

    def __init__(self) -> None:
        ctk.set_appearance_mode("light")
        ctk.set_default_color_theme("blue")
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1140x900")
        self.minsize(1020, 760)
        self.configure(fg_color=BG)

        self.q: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.proc_box: dict = {"p": None}
        self.worker: threading.Thread | None = None
        self.running = False
        self.result: Path | None = None
        self.tools: tuple[str, str] | None = None
        self.info: StreamInfo | None = None
        self._log_visible = True
        self._general_widgets: list = []

        self._build()
        self._apply_settings(load_settings())
        self._refresh_tools_chip()
        self._on_mode(self.mode.get())
        self._on_speed(self.speed_menu.get())
        self._refresh_controls()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._poll)
        self.after(500, self._startup_check)

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
        self.yt_chip = ctk.CTkLabel(header, text=f"yt-dlp {YTDLP_VERSION}".strip(), width=130,
                                    height=28, corner_radius=14, fg_color=LAV_1,
                                    text_color=NAVY, font=F(12, "bold"))
        self.yt_chip.pack(side="right")
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
        self.start_btn = primary_button(row, "Download .MOV", self._start, width=180,
                                        height=44)
        self.start_btn.pack(side="right")
        self.cancel_btn = secondary_button(row, "Cancel", self._cancel, width=100, height=44)
        self.cancel_btn.pack(side="right", padx=(0, 10))
        self.folder_btn = secondary_button(
            row, "Open folder", lambda: self._open(self.result.parent if self.result else None),
            width=120, height=44)
        self.folder_btn.pack(side="right", padx=(0, 10))
        self.play_btn = secondary_button(
            row, "Play .MOV", lambda: self._open(self.result), width=110, height=44)
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
        self._build_info(left)
        self._build_progress(left)
        self._build_mode(right)
        self._build_output(right)
        self._build_activity(right)

    def _build_source(self, parent) -> None:
        body = self._card(parent, 1, "Video link")
        body.grid_columnconfigure(0, weight=1)
        self.url_var = ctk.StringVar()
        self.url_entry = text_entry(body, self.url_var, "Paste a video link (https://...)")
        self.url_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.url_entry.bind("<Return>", lambda _e: self._fetch())
        self.paste_btn = secondary_button(body, "Paste", self._paste, width=72, height=38)
        self.paste_btn.grid(row=0, column=1, padx=(0, 8))
        self.fetch_btn = primary_button(body, "Fetch Info", self._fetch, width=104, height=38)
        self.fetch_btn.grid(row=0, column=2)
        ctk.CTkLabel(body, text="Fetch Info previews the stream first; the download then reuses "
                                "it, so nothing is looked up twice. Or press Download .MOV to "
                                "go straight through.",
                     font=F(11), text_color=MUTED, anchor="w", justify="left",
                     wraplength=440).grid(row=1, column=0, columnspan=3, sticky="w",
                                          pady=(10, 0))
        self.url_var.trace_add("write", lambda *_a: self._on_url_changed())
        self._general_widgets += [self.url_entry, self.paste_btn, self.fetch_btn]

    def _build_info(self, parent) -> None:
        body = self._card(parent, 2, "Stream info")
        body.grid_columnconfigure(1, weight=1)
        self.info_title = ctk.CTkLabel(body, text="No video loaded", font=F(15, "bold"),
                                       text_color=TEXT, anchor="w", justify="left",
                                       wraplength=440)
        self.info_title.grid(row=0, column=0, columnspan=2, sticky="w")
        self.res_chip = ctk.CTkLabel(body, text="Paste a link, then press Fetch Info", height=28,
                                     corner_radius=8, fg_color=LAV_1, text_color=NAVY,
                                     font=F(12, "bold"))
        self.res_chip.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(10, 10))
        rows = (("channel", "Channel"), ("duration", "Duration"),
                ("resolution", "Selected stream"), ("available", "Offered"),
                ("vcodec", "Video codec"),
                ("acodec", "Audio"), ("size", "Est. size"), ("plan", "Output plan"))
        self.info_vals: dict = {}
        for i, (key, label) in enumerate(rows, start=2):
            ctk.CTkLabel(body, text=label.upper(), font=F(11, "bold"), text_color=MUTED,
                         anchor="w", width=108).grid(row=i, column=0, sticky="nw", pady=3)
            val = ctk.CTkLabel(body, text="-", font=F(13), text_color=TEXT, anchor="w",
                               justify="left", wraplength=330)
            val.grid(row=i, column=1, sticky="w", padx=(8, 0), pady=3)
            self.info_vals[key] = val

    def _build_progress(self, parent) -> None:
        body = self._card(parent, None, "Progress")
        body.grid_columnconfigure((0, 1, 2, 3), weight=1, uniform="stage")
        self.stage_chips: list = []
        for i, name in enumerate(self.STAGES):
            chip = ctk.CTkLabel(body, text=f"{i + 1} {name}", height=30, corner_radius=15,
                                fg_color=LAV_1, text_color=MUTED, font=F(11, "bold"))
            chip.grid(row=0, column=i, sticky="ew",
                      padx=(0 if i == 0 else 3, 0 if i == len(self.STAGES) - 1 else 3))
            self.stage_chips.append(chip)
        self.detail_lbl = ctk.CTkLabel(body, text="Waiting for a link...", font=F(12),
                                       text_color=MUTED, anchor="w", justify="left",
                                       wraplength=440)
        self.detail_lbl.grid(row=1, column=0, columnspan=4, sticky="w", pady=(10, 0))

    def _build_mode(self, parent) -> None:
        body = self._card(parent, 3, "Quality & .MOV mode")
        body.grid_columnconfigure(0, weight=1)
        self._section_label(body, "VIDEO QUALITY").grid(row=0, column=0, sticky="w")
        self.quality_menu = option_menu(body, QUALITY_OPTIONS, self._on_quality)
        self.quality_menu.set(QUALITY_BEST)
        self.quality_menu.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        self.quality_note = ctk.CTkLabel(body, text=QUALITY_HINT, font=F(12), text_color=MUTED,
                                         anchor="w", justify="left", wraplength=440)
        self.quality_note.grid(row=2, column=0, sticky="w", pady=(8, 16))
        self._general_widgets.append(self.quality_menu)
        self._section_label(body, ".MOV MODE").grid(row=3, column=0, sticky="w")
        self.mode = Segmented(body, [MODE_AUTO, MODE_PRORES, MODE_COPY], command=self._on_mode,
                              height=40)
        self.mode.grid(row=4, column=0, sticky="ew", pady=(6, 0))
        self.mode_hint = ctk.CTkLabel(body, text="", font=F(12), text_color=MUTED, anchor="w",
                                      justify="left", wraplength=440)
        self.mode_hint.grid(row=5, column=0, sticky="w", pady=(10, 14))
        self._section_label(body, "PRORES PROFILE (ProRes 422 mode)").grid(row=6, column=0,
                                                                          sticky="w")
        self.profile_menu = option_menu(body, PRORES_PROFILES,
                                        lambda _v: self._refresh_plan())
        self.profile_menu.set(next(iter(PRORES_PROFILES)))
        self.profile_menu.grid(row=7, column=0, sticky="ew", pady=(6, 0))
        self._section_label(body, "ENCODING SPEED (when VP9 / AV1 must be converted)").grid(
            row=8, column=0, sticky="w", pady=(16, 0))
        self.speed_menu = option_menu(body, SPEED_HINTS, self._on_speed)
        self.speed_menu.set(SPEED_FAST)
        self.speed_menu.grid(row=9, column=0, sticky="ew", pady=(6, 0))
        self.speed_hint = ctk.CTkLabel(body, text="", font=F(12), text_color=MUTED, anchor="w",
                                       justify="left", wraplength=440)
        self.speed_hint.grid(row=10, column=0, sticky="w", pady=(8, 0))
        self._general_widgets.append(self.speed_menu)

    def _build_output(self, parent) -> None:
        body = self._card(parent, 4, "Output folder")
        body.grid_columnconfigure(0, weight=1)
        self._section_label(body, "SAVE TO").grid(row=0, column=0, columnspan=2, sticky="w")
        self.out_var = ctk.StringVar(value=str(default_folder()))
        self.out_entry = text_entry(body, self.out_var, "Where to save the .mov file")
        self.out_entry.grid(row=1, column=0, sticky="ew", padx=(0, 8), pady=(6, 0))
        self.browse_btn = secondary_button(body, "Browse", self._browse_out, width=100)
        self.browse_btn.grid(row=1, column=1, pady=(6, 0))
        ctk.CTkLabel(body, text="Files are saved as  Title [1080p].mov  and never overwrite "
                                "existing files. Temporary files are removed automatically.",
                     font=F(11), text_color=MUTED, anchor="w", justify="left",
                     wraplength=440).grid(row=2, column=0, columnspan=2, sticky="w",
                                          pady=(10, 0))
        self._general_widgets += [self.out_entry, self.browse_btn]

    def _build_activity(self, parent) -> None:
        body = self._card(parent, None, "Activity")
        top = ctk.CTkFrame(body, fg_color="transparent")
        top.pack(fill="x", pady=(0, 8))
        self._section_label(top, "STATUS LOG").pack(side="left")
        self.log_toggle = secondary_button(top, "Hide log", self._toggle_log, width=84,
                                           height=28)
        self.log_toggle.pack(side="right")
        self.logbox = ctk.CTkTextbox(body, height=250, state="disabled", fg_color=BG,
                                     border_width=1, border_color=BORDER, text_color=TEXT,
                                     corner_radius=8, wrap="word",
                                     font=ctk.CTkFont(family=MONO_FONT, size=12))
        self.logbox.pack(fill="x")

    def _toggle_log(self) -> None:
        self._log_visible = not self._log_visible
        if self._log_visible:
            self.logbox.pack(fill="x")
            self.log_toggle.configure(text="Hide log")
        else:
            self.logbox.pack_forget()
            self.log_toggle.configure(text="Show log")

    # ----------------------------------------------------------- settings --
    def _apply_settings(self, s: dict) -> None:
        if isinstance(s.get("out_dir"), str) and s["out_dir"].strip():
            self.out_var.set(s["out_dir"])
        if s.get("quality") in QUALITY_OPTIONS:
            self.quality_menu.set(s["quality"])
        if s.get("mode") in MODE_HINTS:
            self.mode.set(s["mode"])
        if s.get("profile") in PRORES_PROFILES:
            self.profile_menu.set(s["profile"])
        if s.get("speed") in SPEED_HINTS:
            self.speed_menu.set(s["speed"])

    def _save_settings(self) -> None:
        save_settings({"out_dir": self.out_var.get().strip(), "quality": self._quality_key(),
                       "mode": self.mode.get(), "profile": self.profile_menu.get(),
                       "speed": self.speed_menu.get()})

    # -------------------------------------------------------- tools / chip --
    def _refresh_tools_chip(self) -> None:
        try:
            self.tools = require_tools()
            ver = ffmpeg_version(self.tools[0])
            self.ff_chip.configure(text=f"FFmpeg {ver}".strip() + "  ready", fg_color=LAV_1,
                                   text_color=NAVY)
        except Yt4kError:
            self.tools = None
            self.ff_chip.configure(text="FFmpeg not found", fg_color=ERR_SOFT, text_color=ERR)

    def _startup_check(self) -> None:
        if self.tools is None:
            self._log("FFmpeg was not found. Install it, then restart the app (see README).")
            self._dialog(
                "FFmpeg is required",
                "FFmpeg (and ffprobe) were not found on your system PATH. They are needed to "
                "create the .mov file.\n\nInstall FFmpeg, then restart this app:\n"
                "macOS: brew install ffmpeg\n"
                "Windows: winget install Gyan.FFmpeg (then reopen the terminal)\n"
                "Linux: sudo apt install ffmpeg\n\n"
                "You can still use Fetch Info, but downloads are disabled until FFmpeg is "
                "installed.", "warning")
        if not js_runtime_found():
            self._log("Tip: Deno (a JavaScript runtime) was not found. Without it YouTube may "
                      "hide 4K formats. See the README for a one-line install.")

    # ----------------------------------------------------------- dialogs --
    def _dialog(self, title, message, kind="info", confirm=False, yes="OK", no="Cancel") -> bool:
        dlg = Dialog(self, title, message, kind, confirm, yes, no)
        self.wait_window(dlg)
        return dlg.result

    def _warn(self, title: str, message: str) -> None:
        self._dialog(title, message, "warning")

    def _error(self, title: str, message: str) -> None:
        self._dialog(title, message, "error")

    # ------------------------------------------------------------- inputs --
    def _paste(self) -> None:
        try:
            text = self.clipboard_get()
        except tk.TclError:
            return
        self.url_var.set(text.strip())

    # ------------------------------------------------------------ quality --
    def _quality_key(self) -> str:
        """Menu entries may carry an '[available]' marker: match them back to the base label."""
        value = self.quality_menu.get()
        for key in QUALITY_OPTIONS:
            if value.startswith(key):
                return key
        return QUALITY_BEST

    def _quality_cap(self) -> int | None:
        return QUALITY_OPTIONS[self._quality_key()]

    def _update_quality_labels(self, info: StreamInfo | None) -> None:
        """Mark every tier as available / not offered once a link has been fetched."""
        key = self._quality_key()
        heights = [h for h, _ in info.available] if info else []
        labels = []
        for base, cap in QUALITY_OPTIONS.items():
            if heights and cap is not None:
                base += "  [available]" if tier_offered(heights, cap) else "  [not offered]"
            labels.append(base)
        self.quality_menu.configure(values=labels)
        self.quality_menu.set(labels[list(QUALITY_OPTIONS).index(key)])

    def _refresh_quality_note(self) -> None:
        info = self.info
        pred = predict_quality(info.available, self._quality_cap()) if info else None
        if pred is None:
            self.quality_note.configure(text=QUALITY_HINT, text_color=MUTED)
            return
        h, fps, offered = pred
        cap = self._quality_cap()
        fps_txt = f" @ {fps:g}fps" if fps else ""
        if cap is None:
            self.quality_note.configure(
                text=f"Highest offered: {h}p{fps_txt} will be downloaded.", text_color=OK)
        elif offered:
            self.quality_note.configure(
                text=f"{h}p{fps_txt} is offered and will be downloaded.", text_color=OK)
        elif h > cap:
            self.quality_note.configure(
                text=f"Nothing at or below {cap}p is offered: the lowest one ({h}p{fps_txt}) "
                     "will be used.", text_color=WARN)
        else:
            self.quality_note.configure(
                text=f"{cap}p is not offered by this video: it will fall back to the highest "
                     f"available ({h}p{fps_txt}).", text_color=WARN)

    def _on_quality(self, _value: str) -> None:
        self._refresh_quality_note()
        if self.info is not None and not self.running:
            self._fetch()       # re-selects from the saved lookup: instant, no network

    def _on_speed(self, value: str) -> None:
        self.speed_hint.configure(text=SPEED_HINTS.get(value, ""))
        self._refresh_plan()

    def _on_url_changed(self) -> None:
        if self.info is not None and normalize_url(self.url_var.get()) != self.info.url:
            self.info = None
            self._clear_info()

    def _read_url(self) -> str | None:
        url = normalize_url(self.url_var.get())
        if not url:
            self._warn("Paste a video link", "Paste the full link of a video "
                       "(starting with https://), then try again.")
            return None
        if url != self.url_var.get().strip():
            self.url_var.set(url)
        return url

    def _browse_out(self) -> None:
        current = Path(self.out_var.get().strip() or str(default_folder())).expanduser()
        start = current if current.is_dir() else current.parent
        path = filedialog.askdirectory(title="Choose the output folder",
                                       initialdir=str(start) if start.exists() else None)
        if path:
            self.out_var.set(str(Path(path)))

    def _open(self, path) -> None:
        if path is None:
            return
        try:
            open_path(Path(path))
        except Yt4kError as exc:
            self._error(APP_NAME, str(exc))

    # ---------------------------------------------------------- info card --
    def _clear_info(self) -> None:
        self.info_title.configure(text="No video loaded")
        self.res_chip.configure(text="Paste a link, then press Fetch Info", fg_color=LAV_1,
                                text_color=NAVY)
        for lbl in self.info_vals.values():
            lbl.configure(text="-")
        self._update_quality_labels(None)
        self._refresh_quality_note()
        if not self.running:
            self.detail_lbl.configure(text="Waiting for a link...")

    def _show_info(self, info: StreamInfo) -> None:
        self.info = info
        self.info_title.configure(text=info.title)
        mw, mh = info.max_width, info.max_height
        if mh >= 4320 or mw >= 7680:
            self.res_chip.configure(text="8K (4320p) is available", fg_color=OK_SOFT,
                                    text_color=OK)
        elif mh >= 2160 or mw >= 3840:
            self.res_chip.configure(text="4K (2160p) is available", fg_color=OK_SOFT,
                                    text_color=OK)
        else:
            self.res_chip.configure(text=f"No 4K offered  -  best is {mw}x{mh}",
                                    fg_color=WARN_SOFT, text_color=WARN)
        self._update_quality_labels(info)
        self._refresh_quality_note()
        self.info_vals["available"].configure(text=format_available(info.available))
        fps = f" @ {info.fps:g}fps" if info.fps else ""
        audio = info.acodec_raw if info.has_audio else "no audio track"
        self.info_vals["channel"].configure(text=info.channel or "-")
        self.info_vals["duration"].configure(text=fmt_hms(info.duration) if info.duration
                                             else "unknown")
        self.info_vals["resolution"].configure(text=f"{info.width}x{info.height}{fps}"
                                               + ("  (HDR)" if info.hdr else ""))
        self.info_vals["vcodec"].configure(text=codec_label(info.vcodec_raw, info.vfamily))
        self.info_vals["acodec"].configure(text=audio)
        self._refresh_plan()

    def _refresh_plan(self) -> None:
        info = getattr(self, "info", None)
        if info is None or not hasattr(self, "speed_menu"):
            return
        if self.mode.get() == MODE_COPY and info.vfamily not in MOV_COPY_VIDEO:
            if info.copy_height > 0:
                self.info_vals["plan"].configure(
                    text=f"{family_name(info.vfamily)} cannot go into .mov without re-encoding, so "
                         f"the best {family_name(info.copy_family)} stream "
                         f"({info.copy_width}x{info.copy_height}) is used; video copied untouched")
                self.info_vals["size"].configure(text="Depends on the H.264 / HEVC stream")
            else:
                self.info_vals["plan"].configure(
                    text="Not possible: no H.264 / HEVC stream is offered. Use Auto or ProRes 422")
                self.info_vals["size"].configure(text="-")
            return
        plan = make_plan(self.mode.get(), info.vfamily, info.afamily, info.has_audio, info.hdr,
                         PRORES_PROFILES[self.profile_menu.get()], self.speed_menu.get())
        encoders = available_encoders(self.tools[0]) if self.tools else frozenset()
        out_est = estimate_output_bytes(plan, info)
        dl = ("unknown" if info.size <= 0
              else ("" if info.size_exact else "~") + fmt_bytes(info.size))
        self.info_vals["size"].configure(
            text=f"Download {dl}   ->   .MOV ~{fmt_bytes(out_est)}")
        text = describe_plan(plan, encoders)
        if plan.hdr_forced:
            text += " (HDR source: ProRes keeps 10-bit color)"
        self.info_vals["plan"].configure(text=text)

    def _on_mode(self, value: str) -> None:
        self.mode_hint.configure(text=MODE_HINTS[value])
        self.profile_menu.configure(
            state="normal" if value == MODE_PRORES and not self.running else "disabled")
        self._refresh_plan()

    # ------------------------------------------------------------ actions --
    def _begin_job(self, status: str) -> None:
        self.cancel.clear()
        self.result = None
        self.running = True
        self._refresh_controls()
        self.progress.set(0)
        self.pct.configure(text="")
        self._set_stage(-1)
        self._set_status(status, "busy")

    def _emit(self):
        return lambda kind, payload=None: self.q.put((kind, payload))

    def _fetch(self) -> None:
        if self.running:
            return
        url = self._read_url()
        if url is None:
            return
        self._begin_job("Fetching streams...")
        self._log(f"Fetching: {url}")
        self.worker = threading.Thread(target=run_fetch, daemon=True,
                                       args=(url, self._emit(), self.cancel,
                                             self._quality_cap(), self.info))
        self.worker.start()

    def _start(self) -> None:
        if self.running:
            return
        url = self._read_url()
        if url is None:
            return
        self._refresh_tools_chip()
        if self.tools is None:
            return self._error("FFmpeg is required",
                               "FFmpeg was not found. Install it and restart the app:\n"
                               "macOS: brew install ffmpeg\n"
                               "Windows: winget install Gyan.FFmpeg\n"
                               "Linux: sudo apt install ffmpeg")
        folder_text = self.out_var.get().strip() or str(default_folder())
        out_dir = Path(folder_text).expanduser()
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return self._warn("Output folder", f"The folder could not be created:\n{exc}")
        if not os.access(out_dir, os.W_OK):
            return self._warn("Output folder", "The folder is not writable. Choose another one.")
        self.out_var.set(str(out_dir))
        self._save_settings()

        cfg = JobConfig(url=url, out_dir=out_dir, mode=self.mode.get(),
                        profile=PRORES_PROFILES[self.profile_menu.get()],
                        height_cap=self._quality_cap(), speed=self.speed_menu.get(),
                        cached=self.info)
        self._begin_job("Fetching streams...")
        self._log(f"Start: {url}  |  quality: {quality_name(cfg.height_cap)}  |  "
                  f"mode: {cfg.mode}  |  speed: {cfg.speed}")
        self.worker = threading.Thread(target=run_download, daemon=True,
                                       args=(cfg, self._emit(), self.cancel, self.proc_box))
        self.worker.start()

    def _cancel(self) -> None:
        self.cancel.set()
        proc = self.proc_box.get("p")
        if proc and proc.poll() is None:
            proc.terminate()
        if self.running:
            self._set_status("Cancelling...", "warn")

    def _on_close(self) -> None:
        if self.running and not self._dialog("Quit?", "A job is running. Quit anyway?",
                                             "warning", confirm=True, yes="Quit",
                                             no="Keep working"):
            return
        self._save_settings()
        self._cancel()
        if self.worker is not None and self.worker.is_alive():
            self.worker.join(timeout=4)   # let the worker remove its temporary files
        self.destroy()

    # ------------------------------------------------------------ ui state --
    def _set_status(self, text: str, state: str = "idle") -> None:
        self.dot.configure(fg_color=self.STATUS_COLORS[state])
        self.status.configure(text=text)

    def _set_stage(self, k: int) -> None:
        """Stages before k are done, stage k is active, later stages wait. k=4: all done."""
        for i, chip in enumerate(self.stage_chips):
            if i < k:
                chip.configure(fg_color=OK_SOFT, text_color=OK)
            elif i == k:
                chip.configure(fg_color=BLUE, text_color=WHITE)
            else:
                chip.configure(fg_color=LAV_1, text_color=MUTED)

    def _refresh_controls(self) -> None:
        busy = self.running
        for w in self._general_widgets:
            w.configure(state="disabled" if busy else "normal")
        self.mode.set_enabled(not busy)
        self.profile_menu.configure(
            state="normal" if (not busy and self.mode.get() == MODE_PRORES) else "disabled")
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

    def _finish_idle(self) -> None:
        self.running = False
        self._refresh_controls()

    def _poll(self) -> None:
        try:
            while True:
                kind, data = self.q.get_nowait()
                if kind == "log":
                    self._log(data)
                elif kind == "status":
                    self._set_status(data, "busy")
                elif kind == "stage":
                    self._set_stage(data)
                elif kind == "progress":
                    self.progress.set(data["frac"])
                    self.pct.configure(text=data["text"])
                elif kind == "detail":
                    self.detail_lbl.configure(text=data)
                elif kind == "info":
                    self._show_info(data)
                elif kind == "fetched":
                    self._finish_idle()
                    self._set_stage(-1)
                    self.pct.configure(text="")
                    self.detail_lbl.configure(text="Stream info ready. Choose a mode and "
                                                   "press Download .MOV.")
                    self._set_status("Stream info ready", "ok")
                    self._log(f"Found: {data.title}  ({data.width}x{data.height}, "
                              f"{codec_label(data.vcodec_raw, data.vfamily)})")
                    if data.max_height < 2160 and not js_runtime_found():
                        self._log("Tip: no 4K offered. Install Deno and update yt-dlp if this "
                                  "video should have 4K (see README).")
                elif kind == "done":
                    self.running = False
                    self.result = data["path"]
                    self._refresh_controls()
                    self._set_stage(4)
                    self.progress.set(1.0)
                    self.pct.configure(text="100%")
                    self.detail_lbl.configure(text=f"Saved {data['path'].name}")
                    self._set_status("Complete", "ok")
                    self._log(f"Saved: {data['path']}")
                    self._log(f"Result: {data['width']}x{data['height']}  |  {data['vcodec']} + "
                              f"{data['acodec'] or 'no audio'}  |  {data['duration']:.1f}s  |  "
                              f"{fmt_bytes(data['size'])}  |  verified .mov")
                elif kind == "cancelled":
                    self._finish_idle()
                    self.progress.set(0)
                    self.pct.configure(text="")
                    self._set_stage(-1)
                    self.detail_lbl.configure(text="Cancelled.")
                    self._set_status("Cancelled", "warn")
                    self._log("Cancelled. Temporary files were removed.")
                elif kind == "error":
                    self._finish_idle()
                    self.progress.set(0)
                    self.pct.configure(text="")
                    self._set_stage(-1)
                    self.detail_lbl.configure(text="Something went wrong. See the log.")
                    self._set_status("Download failed", "error")
                    self._log("ERROR: " + data)
                    self._error("Download failed", data)
        except queue.Empty:
            pass
        self.after(100, self._poll)


def main() -> None:
    log.info("%s %s starting (yt-dlp %s)", APP_NAME, APP_VERSION, YTDLP_VERSION)
    App().mainloop()


if __name__ == "__main__":
    main()
