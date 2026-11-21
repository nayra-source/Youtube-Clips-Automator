#!/usr/bin/env python3
"""
viral_clip_detector.py
======================
100% free, offline AI viral clip detector.

Scores every video clip (0-100) on:
    * Text Hook & Emotion  (40%)  - faster-whisper transcript + local Ollama LLM (or heuristic fallback)
    * Audio Energy/Pacing  (35%)  - librosa RMS / silence / variance / tempo + words-per-minute
    * Visual Dynamics      (25%)  - OpenCV motion, scene cuts, face presence / close-ups

Clips scoring >= threshold are copied to an output folder as  score_{score:02d}_{original_name}.

CLI example:
    python viral_clip_detector.py --input ./clips --output ./viral_clips --threshold 75 \
        --whisper-model base --ollama-model qwen2.5:3b --no-llm

The GUI (viral_clip_gui.py) imports and reuses everything in this file.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Sequence, Tuple

import numpy as np

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
VIDEO_EXTS = {
    ".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v",
    ".flv", ".wmv", ".mpg", ".mpeg", ".ts",
}
WEIGHT_TEXT = 0.40
WEIGHT_AUDIO = 0.35
WEIGHT_VISUAL = 0.25
HOOK_WINDOW_SEC = 5.0
FRAME_SAMPLE_SEC = 0.5
DEFAULT_OLLAMA_URL = "http://localhost:11434"

POWER_WORDS = {
    "secret", "secrets", "mistake", "mistakes", "never", "why", "stop", "insane",
    "crazy", "shocking", "truth", "nobody", "everyone", "warning", "hack", "hacks",
    "free", "proven", "biggest", "worst", "best", "fastest", "unbelievable", "wait",
    "watch", "exposed", "wrong", "dangerous", "banned", "illegal", "million",
    "millionaire", "money", "love", "hate", "fear", "dead", "killed", "destroyed",
    "changed", "win", "lose", "fail", "impossible", "unreal", "obsessed", "scary",
}
QUESTION_STARTERS = {
    "why", "how", "what", "did", "do", "does", "have", "can", "are", "is",
    "who", "ever", "imagine", "ready", "guess",
}

LogFn = Callable[[str], None]
ProgressFn = Callable[[int, int, str, str], None]   # done, total, clip name, stage


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def clamp01(x: float) -> float:
    return float(min(1.0, max(0.0, x)))


def norm(x: float, lo: float, hi: float) -> float:
    """Linearly map x from [lo, hi] to [0, 1] (clamped)."""
    if hi == lo:
        return 0.0
    return clamp01((x - lo) / (hi - lo))


def pace_score(wpm: float) -> float:
    """0-1 score for speaking pace. 130-190 WPM is ideal for short-form."""
    if wpm <= 0:
        return 0.0
    if wpm < 130:
        return wpm / 130.0
    if wpm <= 190:
        return 1.0
    return 1.0 - 0.4 * norm(wpm, 190, 260)


def check_ffmpeg() -> Tuple[bool, str]:
    """Pre-flight FFmpeg check. Returns (ok, message)."""
    exe = shutil.which("ffmpeg")
    if not exe:
        return False, (
            "FFmpeg was not found on your PATH. Install it first "
            "(Windows: winget install Gyan.FFmpeg | macOS: brew install ffmpeg | "
            "Linux: sudo apt install ffmpeg) and restart your terminal."
        )
    try:
        out = subprocess.run([exe, "-version"], capture_output=True, text=True, timeout=15)
        if out.returncode != 0:
            return False, "FFmpeg was found but failed to run."
        first = out.stdout.splitlines()[0] if out.stdout else "ffmpeg"
        return True, first
    except Exception as exc:  # noqa: BLE001
        return False, f"FFmpeg check failed: {exc}"


def detect_device(preference: str = "auto") -> Tuple[str, str]:
    """Return (device, compute_type) for faster-whisper."""
    if preference in ("auto", "cuda"):
        try:
            import ctranslate2  # installed with faster-whisper

            if ctranslate2.get_cuda_device_count() > 0:
                return "cuda", "float16"
        except Exception:  # noqa: BLE001
            pass
    # Apple Silicon and plain CPUs both run well with int8 on CPU.
    return "cpu", "int8"


def collect_videos(
    sources: Sequence[Path | str],
    recursive: bool = False,
    exclude_dir: Optional[Path] = None,
) -> List[Path]:
    """
    Turn a list of input sources (folders and/or individual video files)
    into a sorted, de-duplicated list of video files.
    """
    found: dict[str, Path] = {}
    excl = exclude_dir.resolve() if exclude_dir else None

    def _add(p: Path) -> None:
        if p.suffix.lower() not in VIDEO_EXTS or not p.is_file():
            return
        rp = p.resolve()
        if excl and (excl == rp.parent or excl in rp.parents):
            return
        found[str(rp)] = rp

    for src in sources:
        p = Path(src).expanduser()
        if p.is_dir():
            it = p.rglob("*") if recursive else p.iterdir()
            for child in it:
                _add(child)
        elif p.is_file():
            _add(p)
    return sorted(found.values(), key=lambda x: x.name.lower())


# --------------------------------------------------------------------------- #
# Data classes
# --------------------------------------------------------------------------- #
@dataclass
class Transcript:
    text: str = ""
    words: List[Tuple[float, float, str]] = field(default_factory=list)
    duration: float = 0.0
    wpm: float = 0.0
    hook_text: str = ""
    hook_word_count: int = 0
    first_word_start: Optional[float] = None
    language: str = ""


@dataclass
class ClipResult:
    path: Path
    filename: str
    final_score: int = 0
    text_score: float = 0.0
    audio_score: float = 0.0
    visual_score: float = 0.0
    wpm: float = 0.0
    hook_text: str = ""
    hook_mode: str = "heuristic"
    passed: bool = False
    copied_to: str = ""
    error: str = ""


@dataclass
class PipelineConfig:
    sources: List[Path]
    output_dir: Path = Path("viral_clips")
    threshold: int = 75
    whisper_model: str = "base"
    ollama_model: str = "qwen2.5:3b"
    ollama_url: str = DEFAULT_OLLAMA_URL
    use_llm: bool = True
    recursive: bool = False
    device: str = "auto"          # auto | cpu | cuda


# --------------------------------------------------------------------------- #
# Transcriber (faster-whisper, model loaded ONCE)
# --------------------------------------------------------------------------- #
class Transcriber:
    def __init__(self, model_name: str, device_pref: str, log: LogFn):
        self.model_name = model_name
        self.log = log
        self.model = None
        if device_pref == "cpu":
            self.device, self.compute = "cpu", "int8"
        else:
            self.device, self.compute = detect_device(device_pref)
        self._load()

    def _load(self) -> None:
        from faster_whisper import WhisperModel

        self.log(f"Loading Whisper '{self.model_name}' on {self.device} ({self.compute}) ...")
        try:
            self.model = WhisperModel(self.model_name, device=self.device, compute_type=self.compute)
        except Exception as exc:  # noqa: BLE001
            if self.device == "cuda":
                self.log(f"CUDA load failed ({exc}); falling back to CPU int8.")
                self.device, self.compute = "cpu", "int8"
                self.model = WhisperModel(self.model_name, device="cpu", compute_type="int8")
            else:
                raise

    def _run(self, wav_path: str):
        segments, info = self.model.transcribe(
            wav_path, beam_size=1, word_timestamps=True, vad_filter=True,
        )
        return list(segments), info

    def transcribe(self, wav_path: Path) -> Transcript:
        try:
            segments, info = self._run(str(wav_path))
        except RuntimeError as exc:
            msg = str(exc).lower()
            if self.device == "cuda" and any(k in msg for k in ("cuda", "cublas", "cudnn")):
                self.log("CUDA runtime problem during transcription; switching to CPU int8.")
                self.device, self.compute = "cpu", "int8"
                self._load()
                segments, info = self._run(str(wav_path))
            else:
                raise

        words: List[Tuple[float, float, str]] = []
        parts: List[str] = []
        for seg in segments:
            parts.append(seg.text.strip())
            for w in (seg.words or []):
                words.append((float(w.start), float(w.end), w.word))

        text = " ".join(p for p in parts if p).strip()
        duration = float(getattr(info, "duration", 0.0) or 0.0)

        wpm = 0.0
        hook_words = [w for w in words if w[0] < HOOK_WINDOW_SEC]
        hook_text = "".join(w[2] for w in hook_words).strip()
        first_start = words[0][0] if words else None
        if words:
            span = max(words[-1][1] - words[0][0], 3.0)
            wpm = len(words) / (span / 60.0)

        return Transcript(
            text=text, words=words, duration=duration, wpm=wpm, hook_text=hook_text,
            hook_word_count=len(hook_words), first_word_start=first_start,
            language=str(getattr(info, "language", "") or ""),
        )


# --------------------------------------------------------------------------- #
# Stage 1: Audio energy & pacing
# --------------------------------------------------------------------------- #
class AudioAnalyzer:
    SR = 16000

    @staticmethod
    def extract_wav(video: Path, log: LogFn) -> Optional[Path]:
        """Extract mono 16 kHz WAV with FFmpeg into a temp file. None if no audio."""
        fd, tmp = tempfile.mkstemp(suffix=".wav", prefix="viral_")
        os.close(fd)
        cmd = [
            "ffmpeg", "-y", "-v", "error", "-i", str(video),
            "-vn", "-ac", "1", "-ar", str(AudioAnalyzer.SR), "-f", "wav", tmp,
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode != 0 or not Path(tmp).exists() or Path(tmp).stat().st_size < 1000:
                tail = (proc.stderr or "").strip().splitlines()[-1:] or ["no audio stream"]
                log(f"  ! No usable audio in {video.name}: {tail[0]}")
                Path(tmp).unlink(missing_ok=True)
                return None
            return Path(tmp)
        except Exception:
            Path(tmp).unlink(missing_ok=True)
            raise

    def analyze(self, wav_path: Path, wpm: float) -> float:
        import librosa

        y, sr = librosa.load(str(wav_path), sr=self.SR, mono=True)
        if y.size < sr * 0.5:
            return 0.0
        duration = y.size / sr

        rms = librosa.feature.rms(y=y, frame_length=2048, hop_length=512)[0]
        peak = float(np.max(rms))
        if peak <= 1e-8:
            return 0.0
        rms_db = librosa.amplitude_to_db(rms, ref=peak)

        # Silence ratio (dead air)
        silent = rms_db < -40.0
        silence_ratio = float(np.mean(silent))
        silence_pts = 1.0 - norm(silence_ratio, 0.05, 0.60)

        active_db = rms_db[~silent] if np.sum(~silent) > 10 else rms_db
        loudness_pts = norm(float(np.mean(active_db)), -32.0, -12.0)
        variance_pts = norm(float(np.std(active_db)), 2.0, 10.0)

        # Loudness spikes per minute (laughs, hype, beat drops)
        thr = float(np.mean(rms) + 1.5 * np.std(rms))
        above = rms > thr
        rising = int(np.sum(above[1:] & ~above[:-1]))
        spikes_per_min = rising / max(duration / 60.0, 1e-6)
        spike_pts = norm(spikes_per_min, 2.0, 15.0)

        # Tempo
        try:
            tempo = float(np.atleast_1d(librosa.beat.beat_track(y=y, sr=sr)[0])[0])
            tempo_pts = norm(tempo, 70.0, 150.0)
        except Exception:  # noqa: BLE001
            tempo_pts = 0.5

        score = (
            0.20 * loudness_pts
            + 0.20 * variance_pts
            + 0.15 * spike_pts
            + 0.20 * silence_pts
            + 0.05 * tempo_pts
            + 0.20 * pace_score(wpm)
        )
        return round(100.0 * clamp01(score), 1)


# --------------------------------------------------------------------------- #
# Stage 3: Visual dynamics
# --------------------------------------------------------------------------- #
class VisualAnalyzer:
    def __init__(self) -> None:
        import cv2

        self.cv2 = cv2
        cascade_path = os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml")
        self.face = cv2.CascadeClassifier(cascade_path)
        if self.face.empty():
            raise RuntimeError("Could not load OpenCV Haar face cascade.")

    def analyze(self, video: Path) -> float:
        cv2 = self.cv2
        cap = cv2.VideoCapture(str(video))
        if not cap.isOpened():
            raise RuntimeError("OpenCV could not open the video.")
        try:
            fps = cap.get(cv2.CAP_PROP_FPS)
            if not fps or fps != fps or fps <= 0:
                fps = 30.0
            step = max(1, int(round(fps * FRAME_SAMPLE_SEC)))

            prev_gray = None
            prev_hist = None
            motions: List[float] = []
            cuts = 0
            sampled = 0
            face_frames = 0
            closeup_frames = 0
            idx = 0

            while True:
                if not cap.grab():
                    break
                if idx % step == 0:
                    ok, frame = cap.retrieve()
                    if ok and frame is not None:
                        h, w = frame.shape[:2]
                        scale = 320.0 / max(w, 1)
                        small = cv2.resize(frame, (320, max(1, int(h * scale))))
                        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
                        hist = cv2.calcHist([gray], [0], None, [64], [0, 256])
                        cv2.normalize(hist, hist)

                        if prev_gray is not None:
                            motions.append(float(np.mean(cv2.absdiff(gray, prev_gray))) / 255.0)
                            corr = cv2.compareHist(prev_hist, hist, cv2.HISTCMP_CORREL)
                            if (1.0 - corr) > 0.40:
                                cuts += 1

                        faces = self.face.detectMultiScale(
                            gray, scaleFactor=1.2, minNeighbors=5, minSize=(24, 24)
                        )
                        if len(faces) > 0:
                            face_frames += 1
                            biggest = max(fw * fh for (_, _, fw, fh) in faces)
                            if biggest / float(gray.shape[0] * gray.shape[1]) >= 0.03:
                                closeup_frames += 1

                        prev_gray, prev_hist = gray, hist
                        sampled += 1
                idx += 1
        finally:
            cap.release()

        if sampled == 0:
            return 0.0

        span_min = max((sampled - 1) * FRAME_SAMPLE_SEC / 60.0, 1e-6)
        motion_pts = norm(float(np.mean(motions)) if motions else 0.0, 0.01, 0.12)
        cut_pts = norm(cuts / span_min if sampled > 1 else 0.0, 3.0, 20.0)
        face_pts = face_frames / sampled
        close_pts = closeup_frames / sampled

        score = 0.30 * motion_pts + 0.25 * cut_pts + 0.25 * face_pts + 0.20 * close_pts
        return round(100.0 * clamp01(score), 1)


# --------------------------------------------------------------------------- #
# Stage 2: Text hook & emotion (Ollama or heuristic fallback)
# --------------------------------------------------------------------------- #
class HookAnalyzer:
    def __init__(self, use_llm: bool, model: str, url: str, log: LogFn):
        self.model = model
        self.url = url.rstrip("/")
        self.log = log
        self.llm_ready = False
        if use_llm:
            self.llm_ready = self._check_ollama()
        else:
            self.log("LLM disabled (--no-llm): using heuristic hook scorer.")

    def _check_ollama(self) -> bool:
        try:
            import requests

            r = requests.get(f"{self.url}/api/tags", timeout=3)
            r.raise_for_status()
            names = [m.get("name", "") for m in r.json().get("models", [])]
            base = self.model.split(":")[0]
            if not any(n == self.model or n.split(":")[0] == base for n in names):
                self.log(
                    f"WARNING: Ollama is running but model '{self.model}' is not installed "
                    f"(run: ollama pull {self.model}). Using heuristic hook scorer."
                )
                return False
            self.log(f"Ollama connected - using '{self.model}' for hook scoring.")
            return True
        except Exception:  # noqa: BLE001
            self.log(
                f"WARNING: Ollama not reachable at {self.url}. "
                "Falling back to local heuristic hook scorer."
            )
            return False

    # ---- LLM mode ---------------------------------------------------------
    def _llm_score(self, t: Transcript) -> float:
        import requests

        prompt = (
            "You are a short-form video virality expert. Rate the opening hook strength, "
            "emotional punch, and curiosity gap of this short-form video transcript from 1 to 10.\n\n"
            f'OPENING HOOK (first ~5 seconds): "{t.hook_text or "(silence)"}"\n\n'
            f'FULL TRANSCRIPT: "{t.text[:3000]}"\n\n'
            'Respond ONLY with JSON in exactly this form: '
            '{"hook_strength": <1-10>, "emotional_punch": <1-10>, "curiosity_gap": <1-10>}'
        )
        r = requests.post(
            f"{self.url}/api/generate",
            json={
                "model": self.model, "prompt": prompt, "stream": False,
                "format": "json", "options": {"temperature": 0.1},
            },
            timeout=120,
        )
        r.raise_for_status()
        raw = r.json().get("response", "")
        vals: List[float] = []
        try:
            data = json.loads(raw)
            for k in ("hook_strength", "emotional_punch", "curiosity_gap"):
                if k in data:
                    vals.append(float(data[k]))
        except (ValueError, TypeError):
            pass
        if not vals:
            vals = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", raw)[:3]]
        if not vals:
            raise ValueError(f"Could not parse LLM answer: {raw[:80]!r}")
        avg = min(10.0, max(1.0, sum(vals) / len(vals)))
        return round(avg * 10.0, 1)

    # ---- Heuristic mode ---------------------------------------------------
    @staticmethod
    def _heuristic_score(t: Transcript) -> float:
        if not t.text.strip():
            return 0.0
        hook = t.hook_text.lower()
        hook_tokens = re.findall(r"[a-z']+", hook)
        all_tokens = re.findall(r"[a-z']+", t.text.lower())

        # 30: power / curiosity words in the first 5 seconds
        triggers = sum(1 for w in hook_tokens if w in POWER_WORDS)
        trigger_pts = 30.0 * min(triggers, 3) / 3.0

        # 15: question hook
        is_q = "?" in hook or (hook_tokens and hook_tokens[0] in QUESTION_STARTERS)
        question_pts = 15.0 if is_q else 0.0

        # 5: concrete number in hook
        number_pts = 5.0 if re.search(r"\d", hook) else 0.0

        # 10: speech starts immediately
        fs = t.first_word_start
        if fs is None:
            early_pts = 0.0
        elif fs <= 1.0:
            early_pts = 10.0
        elif fs <= 2.5:
            early_pts = 6.0
        elif fs <= HOOK_WINDOW_SEC:
            early_pts = 3.0
        else:
            early_pts = 0.0

        # 15: sentence punchiness (short, punchy sentences ~4-12 words)
        sentences = [s for s in re.split(r"[.!?]+", t.text) if s.strip()]
        if sentences:
            avg_len = sum(len(re.findall(r"[A-Za-z']+", s)) for s in sentences) / len(sentences)
            dist = max(0.0, abs(avg_len - 8.0) - 4.0)
            punchy_pts = 15.0 * (1.0 - norm(dist, 0.0, 12.0))
        else:
            punchy_pts = 0.0

        # 15: pace (hook pace averaged with overall pace)
        hook_wpm = t.hook_word_count / (HOOK_WINDOW_SEC / 60.0)
        pace_pts = 15.0 * pace_score((hook_wpm + t.wpm) / 2.0)

        # 10: emotional word / exclamation density across the whole clip
        emo = sum(1 for w in all_tokens if w in POWER_WORDS) + t.text.count("!")
        density = emo / max(len(all_tokens), 1)
        emo_pts = 10.0 * norm(density, 0.0, 0.06)

        total = trigger_pts + question_pts + number_pts + early_pts + punchy_pts + pace_pts + emo_pts
        return round(min(100.0, total), 1)

    def score(self, t: Transcript) -> Tuple[float, str]:
        if not t.text.strip():
            return 0.0, "no-speech"
        if self.llm_ready:
            try:
                return self._llm_score(t), "ollama"
            except Exception as exc:  # noqa: BLE001
                self.log(f"  ! Ollama call failed ({exc}); using heuristic for this clip.")
        return self._heuristic_score(t), "heuristic"


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #
class ViralPipeline:
    def __init__(
        self,
        config: PipelineConfig,
        log_cb: Optional[LogFn] = None,
        progress_cb: Optional[ProgressFn] = None,
        result_cb: Optional[Callable[[ClipResult], None]] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> None:
        self.cfg = config
        self.log = log_cb or (lambda m: print(m, flush=True))
        self.progress = progress_cb or (lambda *_: None)
        self.on_result = result_cb or (lambda _r: None)
        self.cancel = cancel_event or threading.Event()

    # -- single clip --------------------------------------------------------
    def _process_clip(
        self, video: Path, transcriber: Transcriber, audio: AudioAnalyzer,
        visual: VisualAnalyzer, hook: HookAnalyzer, idx: int, total: int,
    ) -> ClipResult:
        res = ClipResult(path=video, filename=video.name)
        wav: Optional[Path] = None
        try:
            self.progress(idx, total, video.name, "Extracting audio")
            wav = audio.extract_wav(video, self.log)

            transcript = Transcript()
            audio_score = 0.0
            if wav is not None:
                self.progress(idx, total, video.name, "Transcribing")
                transcript = transcriber.transcribe(wav)
                self.progress(idx, total, video.name, "Analyzing audio")
                audio_score = audio.analyze(wav, transcript.wpm)

            self.progress(idx, total, video.name, "Scoring hook")
            text_score, mode = hook.score(transcript)

            self.progress(idx, total, video.name, "Analyzing visuals")
            visual_score = visual.analyze(video)

            res.text_score, res.audio_score, res.visual_score = text_score, audio_score, visual_score
            res.wpm = round(transcript.wpm, 1)
            res.hook_text = transcript.hook_text
            res.hook_mode = mode
            res.final_score = int(round(
                WEIGHT_TEXT * text_score + WEIGHT_AUDIO * audio_score + WEIGHT_VISUAL * visual_score
            ))
        except Exception as exc:  # noqa: BLE001
            res.error = str(exc)
            self.log(f"  ! Failed on {video.name}: {exc}")
        finally:
            if wav is not None:
                wav.unlink(missing_ok=True)   # always delete the temp WAV
        res.passed = (not res.error) and res.final_score >= self.cfg.threshold
        return res

    # -- whole batch --------------------------------------------------------
    def run(self, videos: Optional[Sequence[Path]] = None) -> List[ClipResult]:
        cfg = self.cfg
        ok, msg = check_ffmpeg()
        if not ok:
            raise RuntimeError(msg)
        self.log(f"FFmpeg OK: {msg}")

        out_dir = Path(cfg.output_dir).expanduser()
        if videos is None:
            videos = collect_videos(cfg.sources, cfg.recursive, exclude_dir=out_dir)
        videos = list(videos)
        if not videos:
            raise RuntimeError("No video files found in the selected input source.")
        out_dir.mkdir(parents=True, exist_ok=True)

        self.log(f"Found {len(videos)} clip(s). Threshold: {cfg.threshold}")
        try:
            transcriber = Transcriber(cfg.whisper_model, cfg.device, self.log)   # loaded ONCE
            hook = HookAnalyzer(cfg.use_llm, cfg.ollama_model, cfg.ollama_url, self.log)
            audio = AudioAnalyzer()
            visual = VisualAnalyzer()
        except ImportError as exc:
            raise RuntimeError(
                f"Missing Python package: {exc}. Run: pip install -r requirements.txt"
            ) from exc

        results: List[ClipResult] = []
        t0 = time.time()
        total = len(videos)
        for i, video in enumerate(videos, start=1):
            if self.cancel.is_set():
                self.log("Stopped by user.")
                break
            self.log(f"[{i}/{total}] {video.name}")
            res = self._process_clip(video, transcriber, audio, visual, hook, i, total)
            if res.passed:
                dest = out_dir / f"score_{res.final_score:02d}_{video.name}"
                try:
                    shutil.copy2(video, dest)
                    res.copied_to = str(dest)
                except Exception as exc:  # noqa: BLE001
                    res.error = f"copy failed: {exc}"
                    res.passed = False
            results.append(res)
            self.on_result(res)
            self.progress(i, total, video.name, "Done")

        results.sort(key=lambda r: r.final_score, reverse=True)
        report = write_report(results, out_dir / "viral_report.csv")
        passed = sum(1 for r in results if r.passed)
        self.log(
            f"Finished in {time.time() - t0:.0f}s - {passed}/{len(results)} clips passed. "
            f"Report: {report}"
        )
        return results


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def write_report(results: Iterable[ClipResult], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow([
            "filename", "final_score", "text_score", "audio_score", "visual_score",
            "wpm", "opening_hook", "passed_threshold", "hook_mode", "error",
        ])
        for r in results:
            w.writerow([
                r.filename, r.final_score, r.text_score, r.audio_score, r.visual_score,
                r.wpm, r.hook_text, "YES" if r.passed else "NO", r.hook_mode, r.error,
            ])
    return path


def format_summary(results: Sequence[ClipResult]) -> str:
    lines = [
        "",
        f"{'#':>3}  {'SCORE':>5}  {'TEXT':>5}  {'AUDIO':>5}  {'VISUAL':>6}  {'WPM':>5}  {'PASS':<4}  FILE",
        "-" * 84,
    ]
    for i, r in enumerate(sorted(results, key=lambda x: x.final_score, reverse=True), start=1):
        name = r.filename if len(r.filename) <= 36 else r.filename[:33] + "..."
        lines.append(
            f"{i:>3}  {r.final_score:>5}  {r.text_score:>5.0f}  {r.audio_score:>5.0f}  "
            f"{r.visual_score:>6.0f}  {r.wpm:>5.0f}  {'YES' if r.passed else 'no':<4}  {name}"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Free, offline AI viral clip detector (audio + text hook + visual dynamics).",
    )
    p.add_argument("--input", "-i", required=True, help="Folder of clips (or a single video file).")
    p.add_argument("--output", "-o", default="viral_clips", help="Output folder (default: ./viral_clips).")
    p.add_argument("--threshold", "-t", type=int, default=75, help="Min score to keep a clip (default: 75).")
    p.add_argument("--whisper-model", default="base", help="tiny | base | small | medium | large-v3 (default: base).")
    p.add_argument("--ollama-model", default="qwen2.5:3b", help="Local Ollama model (default: qwen2.5:3b).")
    p.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL, help="Ollama server URL.")
    p.add_argument("--no-llm", action="store_true", help="Skip Ollama; use fast heuristic hook scoring.")
    p.add_argument("--recursive", "-r", action="store_true", help="Also scan sub-folders.")
    p.add_argument("--device", choices=["auto", "cpu", "cuda"], default="auto", help="Whisper device.")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = PipelineConfig(
        sources=[Path(args.input)],
        output_dir=Path(args.output),
        threshold=args.threshold,
        whisper_model=args.whisper_model,
        ollama_model=args.ollama_model,
        ollama_url=args.ollama_url,
        use_llm=not args.no_llm,
        recursive=args.recursive,
        device=args.device,
    )

    def progress(done: int, total: int, name: str, stage: str) -> None:
        if stage != "Done":
            print(f"    ... {stage}", end="\r", flush=True)

    try:
        results = ViralPipeline(cfg, progress_cb=progress).run()
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1

    print(format_summary(results))
    print(f"\nTop clips copied to: {cfg.output_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
