#!/usr/bin/env python3
"""
Video Clipper (GUI)
===================
Cut a precise clip out of a video and export it in the exact container format you choose.

How it works
  1. Load a video (ffprobe reads its length, codecs and size)
  2. Pick the start and end with the sliders or by typing timestamps
  3. Check the first and last frame of your selection in the preview
  4. Choose the output format (.mp4 .mov .mkv .avi .webm), quality and cut mode
  5. Export: ffmpeg writes the clip, then the result is re-opened with ffprobe and verified

Features
  * Modern white and blue UI: range timeline, start/end frame preview, progress bar with ETA,
    status indicator, activity log, themed dialogs and a Cancel button
  * Strict output format: container, codecs and file extension always match the format selector
  * Precise mode (frame-accurate re-encode) or Fast copy mode (lossless, no re-encode)
  * Audio and video stay in sync (timestamps reset to zero, audio aligned to the video clock)
  * Aspect ratio: keep the original shape or export Square (1:1) / Vertical (4:5, 9:16) videos,
    either by cropping to fill the frame or by fitting the whole picture with bars
  * The GUI never freezes: all ffmpeg work runs in background threads
  * Error handling, disk-space check, log file (~/.video_clipper/app.log)

Run:
    python3 video_clipper.py
"""

from __future__ import annotations

import io
import json
import logging
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, replace
from functools import lru_cache
from logging.handlers import RotatingFileHandler
from pathlib import Path
from tkinter import filedialog

try:
    import customtkinter as ctk
except ImportError:
    sys.exit("ERROR: customtkinter is not installed.\nRun:  pip install -r requirements.txt")

try:
    from PIL import Image
except ImportError:
    sys.exit("ERROR: Pillow is not installed.\nRun:  pip install -r requirements.txt")

# Apps launched from Finder/Explorer often miss Homebrew's PATH; add it.
os.environ["PATH"] = os.pathsep.join(
    ["/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin", os.environ.get("PATH", "")]
)

APP_NAME = "Video Clipper"
APP_TAGLINE = "Precise clips  |  your exact output format  |  runs fully offline"
VIDEO_TYPES = "*.mp4 *.mov *.mkv *.avi *.webm *.m4v *.flv *.wmv *.ts *.mpg *.mpeg *.3gp"
POPEN_KW = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform.startswith("win") else {}
MIN_CLIP = 0.1                      # shortest clip in seconds
PREVIEW_W, PREVIEW_H = 200, 113     # thumbnail size (frames are fetched at 2x for sharpness)

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


def F(size: int = 13, weight: str = "normal") -> "ctk.CTkFont":
    return ctk.CTkFont(family=UI_FONT, size=size, weight=weight)


# -------------------------------------------------------------------- formats --
# Every output format is described once here. "vencs"/"aencs" are encoder candidates in order
# of preference; "vnames" is what ffprobe must report for a re-encoded result; "copy_v"/"copy_a"
# are the source codecs that may be stream-copied into that container without re-encoding.
FORMATS = {
    ".mp4": dict(label="MP4", muxer="mp4", token="mp4", faststart=True,
                 vencs=["libx264"], aencs=["aac"], vnames={"h264"},
                 copy_v={"h264", "hevc", "mpeg4", "av1"},
                 copy_a={"aac", "mp3", "ac3", "eac3", "alac"},
                 desc="H.264 video + AAC audio. Plays everywhere; best for sharing and the web."),
    ".mov": dict(label="MOV", muxer="mov", token="mov", faststart=True,
                 vencs=["libx264"], aencs=["aac"], vnames={"h264"},
                 copy_v={"h264", "hevc", "mpeg4", "prores", "mjpeg"},
                 copy_a={"aac", "mp3", "ac3", "eac3", "alac", "pcm_s16le", "pcm_s24le"},
                 desc="H.264 video + AAC audio in a QuickTime container. Great for Apple apps and editors."),
    ".mkv": dict(label="MKV", muxer="matroska", token="matroska", faststart=False,
                 vencs=["libx264"], aencs=["aac"], vnames={"h264"},
                 copy_v={"h264", "hevc", "vp8", "vp9", "av1", "mpeg4", "mpeg2video"},
                 copy_a={"aac", "mp3", "opus", "vorbis", "flac", "ac3", "eac3", "alac",
                         "pcm_s16le", "dts"},
                 desc="H.264 video + AAC audio in Matroska. Flexible and robust, ideal for archiving."),
    ".avi": dict(label="AVI", muxer="avi", token="avi", faststart=False,
                 vencs=["mpeg4"], aencs=["libmp3lame", "ac3", "pcm_s16le"], vnames={"mpeg4"},
                 copy_v={"mpeg4", "mjpeg"},
                 copy_a={"mp3", "ac3", "pcm_s16le"},
                 desc="MPEG-4 video + MP3 audio. Legacy format for older players and software."),
    ".webm": dict(label="WEBM", muxer="webm", token="webm", faststart=False,
                  vencs=["libvpx-vp9"], aencs=["libopus", "libvorbis"], vnames={"vp9"},
                  copy_v={"vp8", "vp9", "av1"},
                  copy_a={"opus", "vorbis"},
                  desc="VP9 video + Opus audio. Open web format; encoding is slower than the others."),
}
KNOWN_EXTS = set(FORMATS) | {".m4v", ".flv", ".wmv", ".ts", ".mpg", ".mpeg", ".3gp"}

QUALITY_LEVELS = ["Balanced - smaller file", "High - recommended", "Maximum - largest file"]
DEFAULT_QUALITY = 1
X264_CRF = (21, 18, 14)
VP9_CRF = (33, 29, 23)
MPEG4_Q = (5, 3, 2)

AUDIO_ARGS = {
    "aac": ["-c:a", "aac", "-b:a", "192k"],
    "libmp3lame": ["-c:a", "libmp3lame", "-q:a", "2", "-ac", "2"],
    "ac3": ["-c:a", "ac3", "-b:a", "192k"],
    "libopus": ["-c:a", "libopus", "-b:a", "160k", "-ac", "2"],
    "libvorbis": ["-c:a", "libvorbis", "-q:a", "5"],
    "pcm_s16le": ["-c:a", "pcm_s16le"],
}

MODE_PRECISE, MODE_COPY = "Precise", "Fast copy"
MODE_HINTS = {
    MODE_PRECISE: "Frame-accurate. Re-encodes the clip so it starts and ends exactly where you "
                  "chose, with audio and video in sync.",
    MODE_COPY: "No re-encode: fastest and lossless. The start snaps to the nearest earlier "
               "keyframe, so it may begin slightly before your chosen time.",
}

PLAN_SINGLE, PLAN_SPLIT = "Single clip", "Auto-split"
LENGTH_PRESETS = (30, 40, 50, 60)                   # quick clip lengths in seconds
SPLIT_PRESETS = ((30, 50), (20, 30), (45, 60))      # (min, max) seconds for auto-split
DEFAULT_SPLIT = (30, 50)

# Aspect ratio: keep the original shape, or reframe to Square (1:1) / Vertical (4:5, 9:16).
ASPECT_ORIGINAL = "Original"
ASPECTS = {ASPECT_ORIGINAL: None, "1:1 Square": (1, 1), "4:5 Vertical": (4, 5),
           "9:16 Vertical": (9, 16)}
FIT_CROP, FIT_BARS = "Crop to fill", "Fit with bars"
FIT_HINTS = {
    FIT_CROP: "Fills the whole frame by trimming the sides (or top and bottom), centered.",
    FIT_BARS: "Keeps the whole picture and adds black bars above/below or left/right.",
}
FIT_MAX_SIDE = 1080     # "Fit with bars" canvases are limited to 1080 px on their short side

# ----------------------------------------------------------------- logging --
try:
    LOG_DIR = Path.home() / ".video_clipper"
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    _handler: logging.Handler = RotatingFileHandler(LOG_DIR / "app.log", maxBytes=500_000,
                                                    backupCount=2, encoding="utf-8")
except OSError:  # read-only home directory: log to the console instead
    LOG_DIR = Path(".")
    _handler = logging.StreamHandler()
log = logging.getLogger("video_clipper")
log.setLevel(logging.INFO)
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
log.addHandler(_handler)


class ClipperError(Exception):
    """Error whose message can be shown directly to the user."""


class Cancelled(Exception):
    """User pressed Cancel."""


# -------------------------------------------------------------- data types --
@dataclass
class JobConfig:
    video: Path
    output: Path
    ext: str
    start: float
    end: float
    fast_copy: bool
    quality: int
    aspect: str = ASPECT_ORIGINAL
    fit: str = FIT_CROP


# ----------------------------------------------------------------- helpers --
def fmt_time(t: float) -> str:
    """Seconds -> HH:MM:SS.mmm"""
    ms = round(max(t, 0.0) * 1000)
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def fmt_hms(t: float) -> str:
    s = int(round(max(t, 0.0)))
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def fmt_eta(seconds: float) -> str:
    s = max(int(seconds), 0)
    h, m, s = s // 3600, s % 3600 // 60, s % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


_NUM = re.compile(r"^\d+(?:\.\d*)?$|^\.\d+$")


def parse_time(text: str) -> float:
    """Accepts SS, SS.mmm, MM:SS, MM:SS.mmm, HH:MM:SS and HH:MM:SS.mmm. Raises ValueError."""
    s = text.strip().replace(",", ".")
    parts = s.split(":")
    if not s or len(parts) > 3 or not all(_NUM.match(p) for p in parts):
        raise ValueError(text)
    values = [float(p) for p in parts]
    if len(values) > 1 and any(v >= 60 for v in values[1:]):
        raise ValueError(text)
    total = 0.0
    for v in values:
        total = total * 60 + v
    return total


def force_ext(path: Path, ext: str) -> Path:
    """Return path with the selected extension (replacing a different video extension)."""
    suffix = path.suffix.lower()
    if suffix == ext:
        return path
    if suffix in KNOWN_EXTS:
        return path.with_suffix(ext)
    return path.with_name(path.name + ext)


def plan_segments(start: float, end: float, min_len: float, max_len: float):
    """Split [start, end] into equal clips whose lengths all lie within [min_len, max_len].

    Returns (segments, leftover_seconds). When no equal split fits the window (for example a
    55 s range with 30-50 s clips) the clips use max_len and the remainder is left over.
    Returns ([], total) when the range is shorter than min_len."""
    total = end - start
    if min_len <= 0 or max_len < min_len or total <= 0:
        raise ValueError("invalid clip length window")
    n_min = max(1, math.ceil(total / max_len - 1e-9))
    n_max = math.floor(total / min_len + 1e-9)
    if n_max < 1:
        return [], total
    if n_max >= n_min:
        n = max(n_min, min(n_max, round(total / ((min_len + max_len) / 2))))
        length, leftover = total / n, 0.0
    else:
        n, length = n_max, max_len
        leftover = total - n * max_len
    segs = [(round(start + i * length, 3), round(start + (i + 1) * length, 3))
            for i in range(n)]
    if leftover == 0.0:
        segs[-1] = (segs[-1][0], round(end, 3))
    return segs, leftover


def require_tools() -> tuple[str, str]:
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise ClipperError(
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
    raise ClipperError(
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
        raise ClipperError("This file does not contain a readable video stream.")
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
        raise ClipperError("ffmpeg reported an error:\n" + "\n".join(tail))


def grab_frame(ffmpeg: str, video: Path, t: float, pre_vf: str = "") -> bytes | None:
    """One PNG frame at time t (for the preview). Retries slightly earlier near the very end.
    pre_vf: optional ffmpeg filters (the aspect ratio crop/pad) applied before the scaling."""
    w, h = PREVIEW_W * 2, PREVIEW_H * 2
    vf = (pre_vf + "," if pre_vf else "") + f"scale={w}:{h}:force_original_aspect_ratio=decrease"
    for back in (0.0, 0.6, 1.6):
        at = max(t - back, 0.0)
        try:
            res = subprocess.run(
                [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-ss", f"{at:.3f}",
                 "-i", str(video), "-an", "-frames:v", "1",
                 "-vf", vf,
                 "-f", "image2pipe", "-vcodec", "png", "pipe:1"],
                capture_output=True, timeout=30, **POPEN_KW)
        except (subprocess.SubprocessError, OSError):
            return None
        if res.returncode == 0 and res.stdout.startswith(b"\x89PNG"):
            return res.stdout
        if at <= 0.0:
            break
    return None


# ------------------------------------------------------------- export engine --
def copy_compatible(ext: str, info: dict) -> bool:
    """Can the source streams be stored in this container without re-encoding?"""
    spec = FORMATS[ext]
    return (info["video_codec"] in spec["copy_v"]
            and (not info["has_audio"] or info["audio_codec"] in spec["copy_a"]))


def reframe_plan(aspect: str, fit: str, width: int, height: int):
    """Filters that turn a width x height video into the chosen aspect ratio.

    Returns (ffmpeg filter list, output width, output height). The list is empty when nothing
    has to change (Original, unknown size, or a video that already has that shape).
    "Crop to fill" trims the picture, centered. "Fit with bars" keeps the whole picture, scales
    it to at most 1080 px on the short side of the canvas and pads the rest with black."""
    ratio = ASPECTS.get(aspect)
    if not ratio or width <= 0 or height <= 0:
        return [], width, height
    r = ratio[0] / ratio[1]
    if abs(width / height - r) / r < 0.005:
        return [], width, height
    even = lambda v: max(2, int(v) // 2 * 2)             # noqa: E731  (4:2:0 needs even sizes)
    center = lambda outer, inner: ((outer - inner) // 2) // 2 * 2   # noqa: E731

    if fit == FIT_BARS:
        # smallest canvas of the target shape that holds the whole picture ...
        cw0, ch0 = (width, width / r) if width / height > r else (height * r, height)
        k = min(1.0, FIT_MAX_SIDE / min(cw0, ch0))       # ... limited to 1080 px, never upscaled
        cw, ch = even(cw0 * k), even(ch0 * k)
        sw, sh = min(even(width * k), cw), min(even(height * k), ch)
        filters = [f"scale={sw}:{sh}"] if (sw, sh) != (width, height) else []
        filters.append(f"pad={cw}:{ch}:{center(cw, sw)}:{center(ch, sh)}:color=black")
        return filters, cw, ch

    if width / height > r:                               # too wide: trim left and right
        cw, ch = even(height * r), even(height)
    else:                                                # too tall: trim top and bottom
        cw, ch = even(width), even(width / r)
    cw, ch = min(cw, width), min(ch, height)
    return [f"crop={cw}:{ch}:{center(width, cw)}:{center(height, ch)}"], cw, ch


def video_args(encoder: str, quality: int) -> list:
    if encoder == "libx264":
        return ["-c:v", "libx264", "-preset", "medium", "-crf", str(X264_CRF[quality]),
                "-pix_fmt", "yuv420p"]
    if encoder == "libvpx-vp9":
        return ["-c:v", "libvpx-vp9", "-b:v", "0", "-crf", str(VP9_CRF[quality]),
                "-row-mt", "1", "-deadline", "good", "-cpu-used", "2", "-pix_fmt", "yuv420p"]
    if encoder == "mpeg4":
        return ["-c:v", "mpeg4", "-q:v", str(MPEG4_Q[quality]), "-vtag", "xvid",
                "-pix_fmt", "yuv420p"]
    raise ClipperError(f"Unsupported video encoder: {encoder}")


def build_command(ffmpeg: str, cfg: JobConfig, info: dict, encoders, partial: Path):
    """Return (ffmpeg command, mode 'copy'|'encode', notes for the log)."""
    spec = FORMATS[cfg.ext]
    notes: list[str] = []
    mode = "encode"
    reframe, out_w, out_h = reframe_plan(cfg.aspect, cfg.fit, info["width"], info["height"])
    if cfg.fast_copy:
        if reframe:
            notes.append("Changing the aspect ratio needs the video to be re-encoded, so "
                         "Precise mode is used.")
        elif copy_compatible(cfg.ext, info):
            mode = "copy"
        else:
            notes.append(
                f"Source codecs ({info['video_codec']} / {info['audio_codec'] or 'no audio'}) "
                f"cannot be stored in {spec['label']} without re-encoding, so Precise mode is "
                "used to keep the output a valid " + spec["label"] + " file.")
    if reframe:
        notes.append(f"Aspect ratio {cfg.aspect} ({cfg.fit.lower()}): "
                     f"{info['width']}x{info['height']} -> {out_w}x{out_h}")

    # -ss before -i seeks fast and stays frame-accurate when re-encoding; -t is the clip length.
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-progress", "pipe:1",
           "-nostats", "-y", "-ss", f"{cfg.start:.3f}", "-i", str(cfg.video),
           "-t", f"{cfg.end - cfg.start:.3f}",
           "-map", "0:v:0", "-map", "0:a:0?", "-map_metadata", "0", "-map_chapters", "-1"]
    if mode == "copy":
        cmd += ["-c", "copy"]
        if info["video_codec"] == "hevc" and cfg.ext in (".mp4", ".mov"):
            cmd += ["-tag:v", "hvc1"]  # makes H.265 play in QuickTime / Apple devices
    else:
        cmd += video_args(pick_encoder(spec["vencs"], encoders, "video", spec["label"]),
                          cfg.quality)
        if reframe:  # crop / pad to the chosen aspect ratio (always produces even dimensions)
            cmd += ["-vf", ",".join(reframe)]
        elif info["width"] % 2 or info["height"] % 2:  # 4:2:0 formats need even dimensions
            cmd += ["-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2"]
        if info["has_audio"]:
            aenc = pick_encoder(spec["aencs"], encoders, "audio", spec["label"])
            # aresample keeps the audio locked to the video clock: no drift, no desync
            cmd += AUDIO_ARGS[aenc] + ["-af", "aresample=async=1:first_pts=0"]
    if spec["faststart"]:
        cmd += ["-movflags", "+faststart"]
    # -f forces the container, so the file really is the selected format whatever its name is
    cmd += ["-avoid_negative_ts", "make_zero", "-f", spec["muxer"], str(partial)]
    return cmd, mode, notes


def container_signature_ok(path: Path, ext: str) -> bool:
    """Check the file's own header bytes. ffprobe cannot tell MP4 from MOV or MKV from WEBM
    (it reports the shared demuxer family), but the headers differ, so this is exact."""
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
        return head[:4] == b"\x1a\x45\xdf\xa3" and b"matroska" in head
    if ext == ".webm":
        return head[:4] == b"\x1a\x45\xdf\xa3" and b"webm" in head
    if ext == ".avi":
        return head[:4] == b"RIFF" and head[8:12] == b"AVI "
    return False


def verify_output(ffprobe: str, path: Path, cfg: JobConfig, info: dict, mode: str,
                  clip_dur: float):
    """Re-open the finished file and confirm it is what the user asked for."""
    spec = FORMATS[cfg.ext]
    try:
        out = probe(ffprobe, path)
    except ClipperError:
        raise ClipperError("The exported file could not be read back. Please try again.")
    warnings: list[str] = []
    allowed = spec["copy_v"] if mode == "copy" else spec["vnames"]
    if out["video_codec"] not in allowed:
        raise ClipperError(
            f"The exported video codec ({out['video_codec']}) does not match "
            f"{spec['label']}. The file was not saved.")
    if spec["token"] not in out["format_name"] or not container_signature_ok(path, cfg.ext):
        raise ClipperError(
            f"The exported container ({out['format_name']}) does not match "
            f"{spec['label']}. The file was not saved.")
    if info["has_audio"] and not out["has_audio"]:
        raise ClipperError("The exported file has no audio track. The file was not saved.")
    if out["duration"] <= 0:
        raise ClipperError("The exported file has no duration. The file was not saved.")
    ratio = ASPECTS.get(cfg.aspect)
    if ratio and out["width"] > 0 and out["height"] > 0:
        want = ratio[0] / ratio[1]
        if abs(out["width"] / out["height"] - want) / want > 0.03:
            raise ClipperError(
                f"The exported size ({out['width']}x{out['height']}) does not match the "
                f"{cfg.aspect} aspect ratio. The file was not saved.")
    drift = abs(out["duration"] - clip_dur)
    if drift > max(0.5, clip_dur * 0.02):
        if mode == "copy":
            warnings.append(f"Fast copy cut at a keyframe: clip is {out['duration']:.2f}s "
                            f"instead of {clip_dur:.2f}s. Use Precise for exact timing.")
        else:
            warnings.append(f"Clip length is {out['duration']:.2f}s (expected {clip_dur:.2f}s).")
    return out, warnings


def run_batch(cfgs: list, emit, cancel, proc_box) -> None:
    """Export one or more clips of the same video. Each clip is verified before it is saved."""
    results: list[dict] = []
    total = len(cfgs)
    partial: Path | None = None
    try:
        ffmpeg, ffprobe = require_tools()
        video = cfgs[0].video.resolve()
        if not video.is_file():
            raise ClipperError(f"Video file not found:\n{video}")
        info = probe(ffprobe, video)
        if info["duration"] <= 0:
            raise ClipperError("Could not read the video duration. Is the file corrupted?")
        emit("log", f"Source: {info['width']}x{info['height']}, {info['duration']:.2f}s, "
                    f"{info['video_codec']}/{info['audio_codec'] or 'no audio'}")
        encoders = available_encoders(ffmpeg)

        for idx, cfg in enumerate(cfgs):
            if cancel.is_set():
                raise Cancelled()
            if cfg.output.resolve() == video:
                raise ClipperError("The output file must be different from the source video.")
            cfg = replace(cfg, end=min(cfg.end, info["duration"]))
            clip_dur = cfg.end - cfg.start
            if cfg.start < 0 or clip_dur < MIN_CLIP:
                raise ClipperError("The selected range is empty. Choose an end time after the "
                                   "start.")
            tag = f"[{idx + 1}/{total}] " if total > 1 else ""
            emit("log", f"{tag}Clip: {fmt_time(cfg.start)} -> {fmt_time(cfg.end)}  "
                        f"({clip_dur:.2f}s)")

            cfg.output.parent.mkdir(parents=True, exist_ok=True)
            needed = max(info["size"] * (clip_dur / info["duration"]) * 1.5, 20e6)
            if shutil.disk_usage(cfg.output.parent).free < needed:
                raise ClipperError("Not enough free disk space in the output folder.")

            spec = FORMATS[cfg.ext]
            partial = cfg.output.with_name(f".{cfg.output.stem}.partial{cfg.ext}")
            cmd, mode, notes = build_command(ffmpeg, cfg, info, encoders, partial)
            if idx == 0:
                for note in notes:
                    emit("log", "Note: " + note)
                emit("log", "Mode: " + ("stream copy (no re-encode)" if mode == "copy"
                                        else "precise re-encode") + f"  |  format: {spec['label']}")
            emit("status", f"Exporting clip {idx + 1} of {total}..." if total > 1
                 else f"Exporting {spec['label']} clip...")
            log.info("export: %s", " ".join(cmd))
            run_ffmpeg(cmd, clip_dur, lambda f, i=idx: emit("progress", (i + 0.97 * f) / total),
                       cancel, proc_box)

            emit("status", "Verifying the exported file..." if total == 1
                 else f"Verifying clip {idx + 1} of {total}...")
            out, warnings = verify_output(ffprobe, partial, cfg, info, mode, clip_dur)
            for w in warnings:
                emit("log", tag + "Warning: " + w)
            os.replace(partial, cfg.output)
            partial = None
            emit("progress", (idx + 1) / total)
            if total > 1:
                emit("log", f"{tag}Saved: {cfg.output.name}  ({out['duration']:.1f}s)")
            results.append({"path": cfg.output, "duration": out["duration"],
                            "size": cfg.output.stat().st_size,
                            "vcodec": out["video_codec"], "acodec": out["audio_codec"],
                            "mode": mode, "label": spec["label"]})

        emit("progress", 1.0)
        emit("done", {"path": results[0]["path"], "paths": [r["path"] for r in results],
                      "count": len(results), "label": results[0]["label"],
                      "mode": results[0]["mode"],
                      "duration": sum(r["duration"] for r in results),
                      "size": sum(r["size"] for r in results),
                      "vcodec": results[0]["vcodec"], "acodec": results[0]["acodec"]})
    except Cancelled:
        emit("cancelled", len(results))
    except ClipperError as exc:
        log.error("%s", exc)
        msg = str(exc)
        if results:
            msg += f"\n\n{len(results)} of {total} clips were saved before this error."
        emit("error", msg)
    except Exception as exc:  # noqa: BLE001
        log.exception("unexpected error")
        emit("error", f"Unexpected error: {exc}\nDetails: {LOG_DIR / 'app.log'}")
    finally:
        if partial and partial.exists():
            partial.unlink(missing_ok=True)


def run_job(cfg: JobConfig, emit, cancel, proc_box) -> None:
    run_batch([cfg], emit, cancel, proc_box)


def open_path(path: Path) -> None:
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", str(path)])
        elif sys.platform.startswith("win"):
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            subprocess.run(["xdg-open", str(path)])
    except OSError as exc:
        raise ClipperError(f"Could not open it: {exc}")


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

    def __init__(self) -> None:
        ctk.set_appearance_mode("light")
        ctk.set_default_color_theme("blue")
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1140x860")
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
        self.duration = 0.0
        self.t_start = 0.0
        self.t_end = 0.0
        self._auto_out = ""
        self._preview_job = None
        self._preview_gen = 0
        self._t0 = 0.0
        self._trim_widgets: list = []
        self._general_widgets: list = []
        self._blank = ctk.CTkImage(light_image=Image.new("RGBA", (2, 2), (0, 0, 0, 0)),
                                   size=(2, 2))

        self._build()
        self._refresh_tools_chip()
        self._on_format(self.fmt.get(), initial=True)
        self._on_plan(self.plan_seg.get())
        self._refresh_controls()
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
        self.ff_chip.pack(side="right", padx=28)
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
        self.start_btn = primary_button(row, "Export Clip", self._start, width=150, height=44)
        self.start_btn.pack(side="right")
        self.cancel_btn = secondary_button(row, "Cancel", self._cancel, width=100, height=44)
        self.cancel_btn.pack(side="right", padx=(0, 10))
        self.folder_btn = secondary_button(
            row, "Open folder", lambda: self._open(self.result.parent if self.result else None),
            width=120, height=44)
        self.folder_btn.pack(side="right", padx=(0, 10))
        self.play_btn = secondary_button(
            row, "Play clip", lambda: self._open(self.result), width=110, height=44)
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
        self._build_trim(left)
        self._build_length(left)
        self._build_output(right)
        self._build_aspect(right)
        self._build_preview(right)
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
        self._general_widgets += [self.video_entry, self.browse_btn]

    def _build_trim(self, parent) -> None:
        body = self._card(parent, 2, "Trim range")
        body.grid_columnconfigure(1, weight=1)

        track = ctk.CTkFrame(body, height=14, fg_color=BORDER, corner_radius=7)
        track.grid(row=0, column=0, columnspan=5, sticky="ew", pady=(2, 0))
        self.sel_bar = ctk.CTkFrame(track, fg_color=LAV_5, corner_radius=7)
        self.sel_bar.place(relx=0, rely=0, relwidth=0.004, relheight=1)
        ticks = ctk.CTkFrame(body, fg_color="transparent")
        ticks.grid(row=1, column=0, columnspan=5, sticky="ew", pady=(3, 8))
        self.tick_l = ctk.CTkLabel(ticks, text="00:00:00", font=F(11), text_color=MUTED)
        self.tick_l.pack(side="left")
        self.tick_r = ctk.CTkLabel(ticks, text="00:00:00", font=F(11), text_color=MUTED)
        self.tick_r.pack(side="right")

        self.start_var = ctk.StringVar(value=fmt_time(0))
        self.end_var = ctk.StringVar(value=fmt_time(0))
        self.start_slider = self._slider(body, self._on_start_slider)
        self.end_slider = self._slider(body, self._on_end_slider)
        rows = ((2, "Start", self.start_var, "start", self.start_slider),
                (3, "End", self.end_var, "end", self.end_slider))
        for r, name, var, which, slider in rows:
            ctk.CTkLabel(body, text=name, font=F(13, "bold"), text_color=TEXT, width=44,
                         anchor="w").grid(row=r, column=0, sticky="w", pady=6)
            slider.grid(row=r, column=1, sticky="ew", padx=(4, 12), pady=6)
            entry = text_entry(body, var, width=124)
            entry.grid(row=r, column=2, pady=6)
            entry.bind("<Return>", lambda _e, w=which: self._commit(w))
            entry.bind("<FocusOut>", lambda _e, w=which: self._commit(w))
            minus = secondary_button(body, "-1s", lambda w=which: self._nudge(w, -1.0),
                                     width=42, height=32)
            minus.grid(row=r, column=3, padx=(8, 4), pady=6)
            plus = secondary_button(body, "+1s", lambda w=which: self._nudge(w, 1.0),
                                    width=42, height=32)
            plus.grid(row=r, column=4, pady=6)
            self._trim_widgets += [slider, entry, minus, plus]

        chips = ctk.CTkFrame(body, fg_color="transparent")
        chips.grid(row=4, column=0, columnspan=5, sticky="ew", pady=(8, 0))
        self.len_chip = ctk.CTkLabel(chips, text="Clip length  00:00:00.000", width=230,
                                     height=28, corner_radius=8, fg_color=LAV_1,
                                     text_color=NAVY, font=F(12, "bold"))
        self.len_chip.pack(side="left")
        self.trim_hint = ctk.CTkLabel(body, text="Tip: type times as HH:MM:SS.mmm, MM:SS or "
                                                 "plain seconds, then press Enter.",
                                      font=F(11), text_color=MUTED, anchor="w")
        self.trim_hint.grid(row=5, column=0, columnspan=5, sticky="w", pady=(8, 0))

    @staticmethod
    def _slider(parent, command):
        return ctk.CTkSlider(parent, from_=0, to=1, height=18, command=command,
                             progress_color=BLUE, button_color=BLUE, button_hover_color=NAVY,
                             fg_color=BORDER)

    def _build_length(self, parent) -> None:
        body = self._card(parent, 3, "Clip length")
        self.plan_seg = Segmented(body, [PLAN_SINGLE, PLAN_SPLIT], command=self._on_plan,
                                  height=38)
        self.plan_seg.pack(fill="x")
        holder = ctk.CTkFrame(body, fg_color="transparent")
        holder.pack(fill="x", pady=(14, 0))

        # -- single clip: quick lengths measured from the start time
        self.single_box = ctk.CTkFrame(holder, fg_color="transparent")
        self._section_label(self.single_box, "QUICK LENGTH, COUNTED FROM THE START TIME").pack(
            anchor="w")
        row = ctk.CTkFrame(self.single_box, fg_color="transparent")
        row.pack(fill="x", pady=(6, 0))
        for sec in LENGTH_PRESETS:
            btn = secondary_button(row, f"{sec} s", lambda v=sec: self._set_length(v),
                                   width=58, height=34)
            btn.pack(side="left", padx=(0, 6))
            self._trim_widgets.append(btn)
        self.len_var = ctk.StringVar()
        len_entry = text_entry(row, self.len_var, "Custom s", width=96, )
        len_entry.pack(side="left", padx=(6, 0))
        len_entry.bind("<Return>", lambda _e: self._commit_length())
        self._trim_widgets.append(len_entry)
        self.len_hint = ctk.CTkLabel(self.single_box, text="Pick a length, or type seconds "
                                     "(for example 45) and press Enter.", font=F(11),
                                     text_color=MUTED, anchor="w")
        self.len_hint.pack(anchor="w", pady=(8, 0))

        # -- auto-split: cut the trim range into several clips within a min/max length
        self.split_box = ctk.CTkFrame(holder, fg_color="transparent")
        self._section_label(self.split_box, "EACH CLIP LASTS BETWEEN (SECONDS)").pack(anchor="w")
        row = ctk.CTkFrame(self.split_box, fg_color="transparent")
        row.pack(fill="x", pady=(6, 0))
        self.min_var = ctk.StringVar(value=f"{DEFAULT_SPLIT[0]}")
        self.max_var = ctk.StringVar(value=f"{DEFAULT_SPLIT[1]}")
        ctk.CTkLabel(row, text="Min", font=F(13, "bold"), text_color=TEXT).pack(side="left")
        min_entry = text_entry(row, self.min_var, width=64)
        min_entry.pack(side="left", padx=(6, 14))
        ctk.CTkLabel(row, text="Max", font=F(13, "bold"), text_color=TEXT).pack(side="left")
        max_entry = text_entry(row, self.max_var, width=64)
        max_entry.pack(side="left", padx=(6, 0))
        for entry in (min_entry, max_entry):
            entry.bind("<Return>", lambda _e: self._update_plan())
            entry.bind("<FocusOut>", lambda _e: self._update_plan())
            self._trim_widgets.append(entry)
        presets = ctk.CTkFrame(self.split_box, fg_color="transparent")
        presets.pack(fill="x", pady=(8, 0))
        for lo, hi in SPLIT_PRESETS:
            btn = secondary_button(presets, f"{lo}-{hi} s",
                                   lambda a=lo, b=hi: self._set_limits(a, b), width=76, height=32)
            btn.pack(side="left", padx=(0, 6))
            self._trim_widgets.append(btn)
        self.plan_lbl = ctk.CTkLabel(self.split_box, text="", font=F(12, "bold"),
                                     text_color=NAVY, fg_color=LAV_1, corner_radius=8,
                                     anchor="w", justify="left", wraplength=400, height=34)
        self.plan_lbl.pack(fill="x", pady=(10, 0))
        ctk.CTkLabel(self.split_box, text="The Trim range above is what gets split. Leave it "
                     "at the full video to split everything.", font=F(11), text_color=MUTED,
                     anchor="w", justify="left", wraplength=400).pack(anchor="w", pady=(8, 0))
        self.plan: list = []
        self.single_box.pack(fill="x")

    def _build_aspect(self, parent) -> None:
        body = self._card(parent, 5, "Aspect ratio")
        body.grid_columnconfigure(0, weight=1)
        self._section_label(body, "SHAPE").grid(row=0, column=0, sticky="w")
        self.aspect = Segmented(body, list(ASPECTS), command=self._on_aspect, height=38)
        self.aspect.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        self._section_label(body, "HOW TO FIT THE PICTURE").grid(row=2, column=0, sticky="w",
                                                                 pady=(14, 0))
        self.fit = Segmented(body, [FIT_CROP, FIT_BARS], command=self._on_aspect, height=34)
        self.fit.grid(row=3, column=0, sticky="ew", pady=(6, 0))
        self.aspect_hint = ctk.CTkLabel(body, text="", font=F(12), text_color=MUTED,
                                        anchor="w", justify="left", wraplength=400)
        self.aspect_hint.grid(row=4, column=0, sticky="w", pady=(8, 0))
        self._update_aspect_hint()

    def _on_aspect(self, _value=None) -> None:
        self._update_aspect_hint()
        self._refresh_controls()      # the fit selector only applies to a changed shape
        self._schedule_preview()      # the frame previews show the reframed picture

    def _update_aspect_hint(self) -> None:
        aspect, fit = self.aspect.get(), self.fit.get()
        if ASPECTS[aspect] is None:
            self.aspect_hint.configure(text="Keeps the original shape of the video.")
            return
        text = FIT_HINTS[fit]
        if self.info is not None:
            w, h = self.info["width"], self.info["height"]
            plan, out_w, out_h = reframe_plan(aspect, fit, w, h)
            if plan:
                text += (f"\nOutput size: {out_w}x{out_h} (source {w}x{h}). The clip is "
                         "re-encoded, so Fast copy is not used.")
            else:
                text += "\nThis video already has that shape, so it is left unchanged."
        else:
            text += "\nThe clip is re-encoded, so Fast copy is not used."
        self.aspect_hint.configure(text=text)

    def _build_preview(self, parent) -> None:
        body = self._card(parent, None, "Frame preview")
        body.grid_columnconfigure((0, 1), weight=1, uniform="pv")
        self.prev: dict = {}
        for col, (which, title) in enumerate((("start", "Start frame"), ("end", "End frame"))):
            cell = ctk.CTkFrame(body, fg_color="transparent")
            cell.grid(row=0, column=col, sticky="n")
            holder = ctk.CTkFrame(cell, width=PREVIEW_W, height=PREVIEW_H, fg_color=TEXT,
                                  corner_radius=8)
            holder.pack()
            holder.pack_propagate(False)
            lbl = ctk.CTkLabel(holder, text="No preview", text_color=FAINT, font=F(12),
                               image=self._blank)
            lbl.pack(expand=True, fill="both")
            cap = ctk.CTkLabel(cell, text=f"{title}  {fmt_time(0)}", font=F(12, "bold"),
                               text_color=MUTED)
            cap.pack(pady=(8, 0))
            self.prev[which] = (lbl, cap, title)

    def _build_output(self, parent) -> None:
        body = self._card(parent, 4, "Output")
        body.grid_columnconfigure((0, 1), weight=1, uniform="out")

        self._section_label(body, "OUTPUT FORMAT").grid(row=0, column=0, columnspan=2,
                                                        sticky="w")
        self.fmt = Segmented(body, list(FORMATS), command=self._on_format, height=42)
        self.fmt.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.fmt_desc = ctk.CTkLabel(body, text="", font=F(12), text_color=MUTED, anchor="w",
                                     justify="left", wraplength=400)
        self.fmt_desc.grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 14))

        self._section_label(body, "QUALITY").grid(row=3, column=0, sticky="w")
        self._section_label(body, "CUT MODE").grid(row=3, column=1, sticky="w", padx=(8, 0))
        self.quality = ctk.CTkOptionMenu(
            body, values=QUALITY_LEVELS, height=40, corner_radius=8, fg_color=LAV_1,
            button_color=LAV_4, button_hover_color=LAV_5, text_color=TEXT, anchor="w",
            dropdown_fg_color=WHITE, dropdown_hover_color=LAV_1, dropdown_text_color=TEXT,
            font=F(13), dropdown_font=F(13))
        self.quality.set(QUALITY_LEVELS[DEFAULT_QUALITY])
        self.quality.grid(row=4, column=0, sticky="ew", pady=(6, 0), padx=(0, 8))
        self.mode = Segmented(body, [MODE_PRECISE, MODE_COPY], command=self._on_mode, height=34)
        self.mode.grid(row=4, column=1, sticky="ew", pady=(6, 0), padx=(8, 0))
        self.mode_hint = ctk.CTkLabel(body, text=MODE_HINTS[MODE_PRECISE], font=F(12),
                                      text_color=MUTED, anchor="w", justify="left",
                                      wraplength=400)
        self.mode_hint.grid(row=5, column=0, columnspan=2, sticky="w", pady=(8, 14))

        self.save_lbl = self._section_label(body, "SAVE TO")
        self.save_lbl.grid(row=6, column=0, columnspan=2, sticky="w")
        pathrow = ctk.CTkFrame(body, fg_color="transparent")
        pathrow.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        pathrow.grid_columnconfigure(0, weight=1)
        self.out_var = ctk.StringVar()
        self.out_entry = text_entry(pathrow, self.out_var, "Where to save the clip")
        self.out_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.saveas_btn = secondary_button(pathrow, "Save as", self._browse_out, width=100)
        self.saveas_btn.grid(row=0, column=1)
        self._general_widgets += [self.quality, self.out_entry, self.saveas_btn]

    def _build_activity(self, parent) -> None:
        body = self._card(parent, None, "Activity")
        self.logbox = ctk.CTkTextbox(body, height=250, state="disabled", fg_color=BG,
                                     border_width=1, border_color=BORDER, text_color=TEXT,
                                     corner_radius=8, wrap="word",
                                     font=ctk.CTkFont(family=MONO_FONT, size=12))
        self.logbox.pack(fill="x")

    # -------------------------------------------------------- tools / chip --
    def _refresh_tools_chip(self) -> None:
        try:
            self.tools = require_tools()
            ver = ffmpeg_version(self.tools[0])
            self.ff_chip.configure(text=f"FFmpeg {ver}".strip() + "  ready", fg_color=LAV_1,
                                   text_color=NAVY)
        except ClipperError:
            self.tools = None
            self.ff_chip.configure(text="FFmpeg not found", fg_color="#FEE2E2", text_color=ERR)
            self._log("FFmpeg was not found. Install it, then restart the app (see README).")

    # ----------------------------------------------------------- dialogs --
    def _dialog(self, title, message, kind="info", confirm=False, yes="OK", no="Cancel") -> bool:
        dlg = Dialog(self, title, message, kind, confirm, yes, no)
        self.wait_window(dlg)
        return dlg.result

    def _warn(self, title: str, message: str) -> None:
        self._dialog(title, message, "warning")

    def _error(self, title: str, message: str) -> None:
        self._dialog(title, message, "error")

    # --------------------------------------------------------- range logic --
    def _set_range(self, start=None, end=None) -> None:
        if self.duration <= 0:
            return
        old = (self.t_start, self.t_end)
        s = self.t_start if start is None else start
        e = self.t_end if end is None else end
        if start is not None and end is None:        # moving the start: keep the end fixed
            s = max(0.0, min(s, e - MIN_CLIP))
        elif end is not None and start is None:      # moving the end: keep the start fixed
            e = min(self.duration, max(e, s + MIN_CLIP))
        else:
            s = max(0.0, min(s, self.duration - MIN_CLIP))
            e = min(self.duration, max(e, s + MIN_CLIP))
        self.t_start, self.t_end = round(s, 3), round(e, 3)

        self.start_slider.set(self.t_start)
        self.end_slider.set(self.t_end)
        self.start_var.set(fmt_time(self.t_start))
        self.end_var.set(fmt_time(self.t_end))
        rel_w = max((self.t_end - self.t_start) / self.duration, 0.004)
        self.sel_bar.place(relx=self.t_start / self.duration, rely=0, relwidth=rel_w,
                           relheight=1)
        label = "Range to split" if self.plan_seg.get() == PLAN_SPLIT else "Clip length"
        self.len_chip.configure(text=f"{label}  {fmt_time(self.t_end - self.t_start)}")
        self.prev["start"][1].configure(text=f"{self.prev['start'][2]}  {fmt_time(self.t_start)}")
        self.prev["end"][1].configure(text=f"{self.prev['end'][2]}  {fmt_time(self.t_end)}")
        self.trim_hint.configure(text="Tip: type times as HH:MM:SS.mmm, MM:SS or plain "
                                      "seconds, then press Enter.", text_color=MUTED)
        self._update_plan()
        if (self.t_start, self.t_end) != old:
            self._schedule_preview()

    def _on_start_slider(self, value) -> None:
        self._set_range(start=float(value))

    def _on_end_slider(self, value) -> None:
        self._set_range(end=float(value))

    def _nudge(self, which: str, delta: float) -> None:
        if which == "start":
            self._set_range(start=self.t_start + delta)
        else:
            self._set_range(end=self.t_end + delta)

    def _commit(self, which: str) -> None:
        var = self.start_var if which == "start" else self.end_var
        current = self.t_start if which == "start" else self.t_end
        if self.duration <= 0:
            var.set(fmt_time(0))
            return
        try:
            value = parse_time(var.get())
        except ValueError:
            var.set(fmt_time(current))
            self.trim_hint.configure(text="That time is not valid. Use HH:MM:SS.mmm, MM:SS or "
                                          "seconds.", text_color=ERR)
            return
        limited = value > self.duration + 0.0005
        if which == "start":
            self._set_range(start=value)
        else:
            self._set_range(end=value)
        if limited:  # shown after _set_range, which resets the hint line
            self.trim_hint.configure(
                text=f"The video is only {fmt_time(self.duration)} long; the value was limited.",
                text_color=WARN)

    # ------------------------------------------------------- clip length --
    def _on_plan(self, value: str) -> None:
        split = value == PLAN_SPLIT
        if split:
            self.single_box.pack_forget()
            self.split_box.pack(fill="x")
            self.save_lbl.configure(text="SAVE TO   (clips are numbered: name_01, name_02, ...)")
        else:
            self.split_box.pack_forget()
            self.single_box.pack(fill="x")
            self.save_lbl.configure(text="SAVE TO")
        self._set_range()      # refreshes the length chip label
        self._update_plan()

    def _read_limits(self):
        """(min, max) clip length in seconds from the entries, or None when invalid."""
        try:
            lo, hi = parse_time(self.min_var.get()), parse_time(self.max_var.get())
        except ValueError:
            return None
        return (lo, hi) if lo > 0 and hi >= lo else None

    def _set_limits(self, lo: float, hi: float) -> None:
        self.min_var.set(f"{lo:g}")
        self.max_var.set(f"{hi:g}")
        self._update_plan()

    def _update_plan(self) -> None:
        if self.plan_seg.get() != PLAN_SPLIT:
            self.plan = []
            self.start_btn.configure(text="Export Clip")
            return
        self.start_btn.configure(text="Export Clips")
        self.plan = []
        limits = self._read_limits()
        if limits is None:
            self.plan_lbl.configure(text="Enter valid seconds. The max must be at least the min.",
                                    text_color=ERR, fg_color="#FEE2E2")
            return
        if self.duration <= 0:
            self.plan_lbl.configure(text="Load a video to see how it will be split.",
                                    text_color=NAVY, fg_color=LAV_1)
            return
        lo, hi = limits
        segs, leftover = plan_segments(self.t_start, self.t_end, lo, hi)
        if not segs:
            self.plan_lbl.configure(
                text=f"The range is only {self.t_end - self.t_start:.1f} s, shorter than the "
                     f"{lo:g} s minimum. Widen the range or lower the minimum.",
                text_color=ERR, fg_color="#FEE2E2")
            return
        self.plan = segs
        n, lengths = len(segs), [e - s for s, e in segs]
        noun = "clip" if n == 1 else "clips"
        text = (f"{n} {noun} of {lengths[0]:.1f} s each" if n == 1 or
                max(lengths) - min(lengths) < 0.05 else
                f"{n} {noun}, {min(lengths):.1f} to {max(lengths):.1f} s each")
        color, fill = NAVY, LAV_1
        if leftover > 0.05:
            text += (f"  |  {leftover:.1f} s at the end is left out (no whole clip fits within "
                     f"{lo:g}-{hi:g} s)")
            color, fill = WARN, "#FEF3C7"
        self.plan_lbl.configure(text=text, text_color=color, fg_color=fill)
        self.start_btn.configure(text="Export Clip" if n == 1 else f"Export {n} Clips")

    def _set_length(self, seconds: float) -> None:
        """Make the single clip `seconds` long, counted from the start time."""
        if self.duration <= 0:
            self.len_hint.configure(text="Load a video first.", text_color=WARN)
            return
        if seconds <= 0:
            self.len_hint.configure(text="The length must be more than 0 seconds.",
                                    text_color=ERR)
            return
        note = f"Clip set to {seconds:g} s."
        if seconds >= self.duration:
            self._set_range(start=0.0, end=self.duration)
            note = f"The video is only {self.duration:.1f} s long, so the whole video is selected."
        elif self.t_start + seconds > self.duration:
            self._set_range(start=self.duration - seconds, end=self.duration)
            note = f"Clip set to {seconds:g} s; the start moved earlier so it fits."
        else:
            self._set_range(start=self.t_start, end=self.t_start + seconds)
        self.len_hint.configure(text=note, text_color=MUTED)

    def _commit_length(self) -> None:
        try:
            seconds = parse_time(self.len_var.get())
        except ValueError:
            self.len_hint.configure(text="That is not a valid length. Use seconds, for "
                                         "example 45, or MM:SS.", text_color=ERR)
            return
        self._set_length(seconds)

    # ------------------------------------------------------------ preview --
    def _schedule_preview(self) -> None:
        if self._preview_job is not None:
            self.after_cancel(self._preview_job)
        self._preview_job = self.after(350, self._request_preview)

    def _request_preview(self) -> None:
        self._preview_job = None
        if self.loaded is None or self.tools is None:
            return
        self._preview_gen += 1
        pre_vf = ""
        if self.info is not None:
            plan, _w, _h = reframe_plan(self.aspect.get(), self.fit.get(),
                                        self.info["width"], self.info["height"])
            pre_vf = ",".join(plan)
        threading.Thread(target=self._preview_worker, daemon=True,
                         args=(self._preview_gen, self.tools[0], self.loaded,
                               self.t_start, self.t_end, pre_vf)).start()

    def _preview_worker(self, gen, ffmpeg, video, start, end, pre_vf="") -> None:
        for which, t in (("start", start), ("end", max(end - 0.05, 0.0))):
            self.q.put(("frame", (gen, which, grab_frame(ffmpeg, video, t, pre_vf))))

    def _show_frame(self, which: str, data: bytes | None) -> None:
        lbl = self.prev[which][0]
        if not data:
            lbl.configure(image=self._blank, text="No frame")
            return
        try:
            img = Image.open(io.BytesIO(data))
            img.load()
        except Exception:  # noqa: BLE001
            lbl.configure(image=self._blank, text="No frame")
            return
        size = (max(img.width // 2, 1), max(img.height // 2, 1))
        lbl.configure(image=ctk.CTkImage(light_image=img, size=size), text="")

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
                raise ClipperError("Could not read the video duration. Is the file corrupted?")
        except ClipperError as exc:
            self.info_lbl.configure(text=str(exc).splitlines()[0], text_color=ERR)
            self._refresh_tools_chip()
            self._error("Cannot open video", str(exc))
            return False

        self.loaded, self.info, self.duration = path, info, info["duration"]
        self.video_var.set(str(path))
        audio = info["audio_codec"] or "no audio"
        self.info_lbl.configure(
            text=f"{info['width']}x{info['height']}  |  {fmt_hms(self.duration)}  |  "
                 f"{info['size'] / 1e6:.1f} MB  |  {info['video_codec']} / {audio}",
            text_color=MUTED)
        for slider in (self.start_slider, self.end_slider):
            slider.configure(from_=0, to=self.duration)
        self.tick_r.configure(text=fmt_hms(self.duration))
        self.t_start, self.t_end = 0.0, self.duration
        self._set_range(start=0.0, end=self.duration)
        self._schedule_preview()
        self._suggest_output()
        self._update_mode_hint()
        self._update_aspect_hint()
        self._refresh_controls()
        self._set_status("Video loaded. Choose your range and format.", "idle")
        self._log(f"Loaded: {path.name}")
        return True

    def _suggest_output(self) -> None:
        if self.loaded is None:
            return
        ext = self.fmt.get()
        current = self.out_var.get().strip()
        if not current or current == self._auto_out:
            self._auto_out = str(self.loaded.with_name(f"{self.loaded.stem}_clip{ext}"))
            self.out_var.set(self._auto_out)

    def _on_format(self, ext: str, initial: bool = False) -> None:
        self.fmt_desc.configure(text=FORMATS[ext]["desc"])
        current = self.out_var.get().strip()
        if current:
            try:
                new = str(force_ext(Path(current), ext))
            except ValueError:
                new = current
            if current == self._auto_out:
                self._auto_out = new
            self.out_var.set(new)
        self._update_mode_hint()

    def _on_mode(self, _value: str) -> None:
        self._update_mode_hint()

    def _update_mode_hint(self) -> None:
        mode, ext = self.mode.get(), self.fmt.get()
        text = MODE_HINTS[mode]
        if mode == MODE_COPY and self.info is not None and not copy_compatible(ext, self.info):
            text = (f"This video's codecs can't be stored in {FORMATS[ext]['label']} without "
                    "re-encoding, so Precise mode will be used for this format.")
        self.mode_hint.configure(text=text)

    def _browse_out(self) -> None:
        ext, label = self.fmt.get(), FORMATS[self.fmt.get()]["label"]
        src = self.loaded
        path = filedialog.asksaveasfilename(
            title="Save clip as", defaultextension=ext,
            initialfile=f"{src.stem}_clip{ext}" if src else f"clip{ext}",
            initialdir=str(src.parent) if src else None,
            filetypes=[(f"{label} video", f"*{ext}")])
        if path:
            self.out_var.set(str(force_ext(Path(path), ext)))

    def _open(self, path) -> None:
        if path is None:
            return
        try:
            open_path(Path(path))
        except ClipperError as exc:
            self._error(APP_NAME, str(exc))

    def _parse_entries(self):
        try:
            start, end = parse_time(self.start_var.get()), parse_time(self.end_var.get())
        except ValueError:
            self._warn("Invalid time", "Start and end must look like HH:MM:SS.mmm, MM:SS or "
                                       "plain seconds, for example 00:01:30.500")
            return None
        end = min(end, self.duration)
        if start < 0 or end - start < MIN_CLIP:
            self._warn("Invalid range", "The end time must be at least 0.1 seconds after the "
                                        "start time.")
            return None
        return start, end

    def _start(self) -> None:
        video_text = self.video_var.get().strip().strip("'\"")
        if not video_text or not Path(video_text).expanduser().is_file():
            return self._warn("Choose a video", "Please choose a valid video file first.")
        video = Path(video_text).expanduser()
        if self.loaded is None or video.resolve() != self.loaded.resolve():
            self._load_video(video_text)   # new path typed in: load it, let the user set a range
            return
        parsed = self._parse_entries()
        if parsed is None:
            return
        start, end = parsed
        self._set_range(start=start, end=end)

        ext = self.fmt.get()
        out_text = self.out_var.get().strip()
        if not out_text:
            self._suggest_output()
            out_text = self.out_var.get().strip()
        output = force_ext(Path(out_text).expanduser(), ext)
        if str(output) != out_text:
            self.out_var.set(str(output))
            self._log(f"Output extension set to {ext} to match the selected format.")
        if output.resolve() == video.resolve():
            return self._warn("Choose another file",
                              "The output must be a different file than the source video.")

        fast, quality = self.mode.get() == MODE_COPY, QUALITY_LEVELS.index(self.quality.get())
        aspect, fit = self.aspect.get(), self.fit.get()
        if self.plan_seg.get() == PLAN_SPLIT:
            if self._read_limits() is None:
                return self._warn("Invalid clip length", "Enter the minimum and maximum clip "
                                  "length in seconds (the maximum must not be smaller than the "
                                  "minimum).")
            if not self.plan:
                return self._warn("Range too short", "The trim range is shorter than the minimum "
                                  "clip length. Widen the range or lower the minimum.")
            width = max(2, len(str(len(self.plan))))
            outputs = [output.with_name(f"{output.stem}_{i:0{width}d}{ext}")
                       for i in range(1, len(self.plan) + 1)]
            cfgs = [JobConfig(video=video, output=o, ext=ext, start=s, end=e, fast_copy=fast,
                              quality=quality, aspect=aspect, fit=fit)
                    for o, (s, e) in zip(outputs, self.plan)]
            existing = [o for o in outputs if o.exists()]
            if existing and not self._dialog(
                    "Replace files?", f"{len(existing)} of the {len(outputs)} clip files already "
                    "exist in that folder. Replace them?", "warning", confirm=True,
                    yes="Replace", no="Cancel"):
                return
            summary = f"{len(cfgs)} clips ({self.min_var.get()}-{self.max_var.get()} s)"
        else:
            if output.exists() and not self._dialog(
                    "Replace file?", f"{output.name} already exists. Replace it?", "warning",
                    confirm=True, yes="Replace", no="Cancel"):
                return
            cfgs = [JobConfig(video=video, output=output, ext=ext, start=start, end=end,
                              fast_copy=fast, quality=quality, aspect=aspect, fit=fit)]
            summary = f"[{fmt_time(start)} -> {fmt_time(end)}]"

        self.cancel.clear()
        self.result = None
        self.running = True
        self._refresh_controls()
        self.progress.set(0)
        self.pct.configure(text="")
        self._t0 = time.monotonic()
        self._set_status("Starting...", "busy")
        self._log(f"Start: {video.name}  {summary}  as {FORMATS[ext]['label']}")
        emit = lambda kind, payload=None: self.q.put((kind, payload))  # noqa: E731
        threading.Thread(target=run_batch, daemon=True,
                         args=(cfgs, emit, self.cancel, self.proc_box)).start()

    def _cancel(self) -> None:
        self.cancel.set()
        proc = self.proc_box.get("p")
        if proc and proc.poll() is None:
            proc.terminate()
        if self.running:
            self._set_status("Cancelling...", "warn")

    def _on_close(self) -> None:
        if self.running and not self._dialog("Quit?", "An export is running. Quit anyway?",
                                             "warning", confirm=True, yes="Quit", no="Keep working"):
            return
        self._cancel()
        self.destroy()

    # ------------------------------------------------------------ ui state --
    def _set_status(self, text: str, state: str = "idle") -> None:
        self.dot.configure(fg_color=self.STATUS_COLORS[state])
        self.status.configure(text=text)

    def _refresh_controls(self) -> None:
        busy, loaded = self.running, self.loaded is not None
        for w in self._trim_widgets:
            w.configure(state="normal" if loaded and not busy else "disabled")
        for w in self._general_widgets:
            w.configure(state="disabled" if busy else "normal")
        self.fmt.set_enabled(not busy)
        self.mode.set_enabled(not busy)
        self.plan_seg.set_enabled(not busy)
        self.aspect.set_enabled(not busy)
        self.fit.set_enabled(not busy and ASPECTS[self.aspect.get()] is not None)
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
        if frac > 0.03 and elapsed > 2:
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
                elif kind == "frame":
                    gen, which, png = data
                    if gen == self._preview_gen:
                        self._show_frame(which, png)
                elif kind == "done":
                    self.running = False
                    self.result = data["path"]
                    self._refresh_controls()
                    self.pct.configure(text="100%")
                    n = data["count"]
                    self._set_status(f"Done! {n} {data['label']} clips saved." if n > 1 else
                                     f"Done! {data['label']} clip saved.", "ok")
                    self._log(f"Saved: {data['path'].parent if n > 1 else data['path']}")
                    self._log(f"Result: {n} x {data['label']}  |  {data['vcodec']} + "
                              f"{data['acodec'] or 'no audio'}  |  {data['duration']:.2f}s total"
                              f"  |  {data['size'] / 1e6:.1f} MB  |  verified")
                elif kind == "cancelled":
                    self.running = False
                    self._refresh_controls()
                    self.progress.set(0)
                    self.pct.configure(text="")
                    self._set_status("Cancelled", "warn")
                    self._log("Cancelled. No file was written." if not data else
                              f"Cancelled. The {data} clip(s) already finished were kept.")
                elif kind == "error":
                    self.running = False
                    self._refresh_controls()
                    self.progress.set(0)
                    self.pct.configure(text="")
                    self._set_status("Export failed", "error")
                    self._log("ERROR: " + data)
                    self._error("Export failed", data)
        except queue.Empty:
            pass
        self.after(100, self._poll)


if __name__ == "__main__":
    App().mainloop()
