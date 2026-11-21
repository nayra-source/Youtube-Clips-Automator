# Video Clipper

A modern desktop app for cutting a precise clip out of a video and exporting it in **exactly the container format you choose** (`.mp4`, `.mov`, `.mkv`, `.avi` or `.webm`). Built with Python, CustomTkinter and FFmpeg. It runs fully offline on **macOS, Windows and Linux**.

## Key features

- **Strict output format.** Pick MP4, MOV, MKV, AVI or WEBM. The container, codecs and file extension always match your choice, and the finished file is re-opened and verified before it is saved. A wrong extension typed into the path box is corrected automatically.
- **Two cut modes.**
  - **Precise** is a frame-accurate re-encode: the clip starts and ends exactly where you chose.
  - **Fast copy** is lossless and near-instant, with no re-encode. The start snaps to the nearest earlier keyframe. If the source codecs cannot be stored in the chosen container (for example H.264 into WEBM), the app re-encodes automatically so the output is still a valid file of that format.
- **Audio/video stay in sync.** Timestamps are reset to zero and audio is aligned to the video clock. The source's own timing is preserved, never silently altered.
- **Clip length control.** Pick a quick length (30, 40, 50 or 60 s, or type your own) for a single clip, or use **Auto-split**: set a minimum and maximum (default **30-50 s**, any custom range) and the app divides your video into evenly sized, numbered clips that each fall inside that window. A live summary shows exactly how many clips you will get before you export.
- **Visual trimming.** Range timeline, start/end sliders, typed timestamps (`HH:MM:SS.mmm`, `MM:SS` or plain seconds) and +/-1 s nudge buttons.
- **Frame preview.** See the first and last frame of your selection before exporting.
- **Quality levels** that map to the right settings for each codec.
- **Never freezes.** All FFmpeg work runs in background threads, with a progress bar, ETA, status indicator, activity log and a Cancel button. Cancelling leaves no partial files.
- **Clean white and blue interface** with themed dialogs and clear error messages.
- Disk-space check and a log file at `~/.video_clipper/app.log`.

## Output formats

| Format | Video codec | Audio codec | Best for |
|--------|-------------|-------------|----------|
| `.mp4` | H.264 | AAC | Sharing, the web, maximum compatibility |
| `.mov` | H.264 | AAC | Apple apps and editing software |
| `.mkv` | H.264 | AAC | Archiving, flexible Matroska container |
| `.avi` | MPEG-4 | MP3 | Legacy players and software |
| `.webm` | VP9 | Opus | Open web format (slower to encode) |

Videos without an audio track are supported. Odd-sized frames are padded to even dimensions automatically when re-encoding.

## Prerequisites

1. **Python 3.9 or newer** with Tk support.
2. **FFmpeg** (`ffmpeg` and `ffprobe` on your `PATH`). A full build is recommended so that `libx264`, `libvpx-vp9`, `libopus` and `libmp3lame` are available.

### macOS

```bash
brew install ffmpeg python-tk
```

### Windows

```powershell
winget install Gyan.FFmpeg
```

Restart the terminal afterwards so `PATH` updates. Install Python from [python.org](https://www.python.org/downloads/) and keep the default "tcl/tk and IDLE" option ticked.

### Linux

Debian / Ubuntu:

```bash
sudo apt install ffmpeg python3-tk python3-venv
```

Fedora:

```bash
sudo dnf install ffmpeg python3-tkinter
```

Arch:

```bash
sudo pacman -S ffmpeg tk
```

Fedora needs the RPM Fusion repository for the full `ffmpeg` build.

Check it worked:

```bash
ffmpeg -version
ffprobe -version
```

## Installation

macOS / Linux:

```bash
git clone <your-repository-url> video-clipper
cd video-clipper
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Windows (PowerShell):

```powershell
git clone <your-repository-url> video-clipper
cd video-clipper
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Skip `git clone` if you simply downloaded the three project files into a folder.

## Usage

```bash
python3 video_clipper.py        # on Windows: py video_clipper.py
```

1. **Source video.** Click **Browse** (or paste a path and press Enter). The video's size, length and codecs appear.
2. **Trim range.** Drag the **Start** and **End** sliders, or type exact times. Use the +/-1s buttons for fine adjustment. Check the **Frame preview** to confirm your first and last frame.
3. **Clip length.** Choose how long the clip(s) should be:
   - **Single clip:** click 30 / 40 / 50 / 60 s (or type a custom length and press Enter) to set the end time from your start time.
   - **Auto-split:** enter a **Min** and **Max** length in seconds, or use a preset (30-50, 20-30, 45-60). The Trim range above is what gets split, so leave it at the full video to split everything. The summary shows the result, for example *"8 clips of 37.5 s each"*.
4. **Output.** Choose the **output format**, a **quality** level and a **cut mode**. Confirm where to save; the extension always follows the selected format. In Auto-split the name you choose becomes a base: `lecture_01.mp4`, `lecture_02.mp4`, and so on.
5. Press **Export Clip** (or **Export N Clips**). When it finishes, use **Play clip** (opens the first clip) or **Open folder**.

### How Auto-split chooses clip lengths

Clips are made **equal in length** and always inside your min-max window. For example, a 5-minute video with a 30-50 s window becomes 8 clips of 37.5 s. If the range cannot be divided evenly into that window (a 55 s range with 30-50 s clips, say), the app uses clips of the maximum length and tells you how many seconds at the end are left out. If the range is shorter than the minimum, it explains this instead of exporting. Each clip is exported and verified on its own; cancelling keeps the clips that are already finished.

### Tips

- For Auto-split, **Precise** gives every clip exactly the length shown. **Fast copy** snaps each clip to a keyframe, so lengths can vary.
- Use **Precise** when exact timing matters. Use **Fast copy** to trim long files in seconds with no quality loss, accepting that the start may begin slightly earlier than chosen.
- WEBM (VP9) is the slowest format to encode; MP4 is the quickest re-encode.
- On macOS and Linux the app adds common FFmpeg locations (Homebrew and MacPorts) to its search path, so it also works when launched from Finder or a desktop launcher.

## Troubleshooting

| Problem | Fix |
|---------|-----|
| "ffmpeg was not found" | Install FFmpeg (see Prerequisites) and restart the terminal or app. |
| "no encoder for ..." | Your FFmpeg build lacks a codec. Install a full build (`brew reinstall ffmpeg`, `winget install Gyan.FFmpeg`, or your distribution's `ffmpeg` package). |
| `ModuleNotFoundError: tkinter` | Install Tk: `brew install python-tk` (macOS) or `sudo apt install python3-tk` (Debian/Ubuntu). |
| Preview shows "No frame" | The file may be damaged near that position; export may still work. |
| Fast copy output is longer than chosen | Expected: the cut snaps to a keyframe. Switch to **Precise** for exact timing. |

Errors are also written to `~/.video_clipper/app.log`.

## Project structure

```
video-clipper/
├── video_clipper.py   # The whole application (UI + export engine)
├── requirements.txt   # Python dependencies
└── README.md
```

Inside `video_clipper.py`:

| Section | Purpose |
|---------|---------|
| Theme and `FORMATS` table | Colours, fonts, and one entry per output format (muxer, encoders, copy-compatible codecs) |
| Helpers | Time parsing/formatting, `ffprobe` wrapper, encoder discovery, preview frame grabber |
| Export engine | `plan_segments`, `build_command`, `run_ffmpeg`, `verify_output`, `run_batch`. No UI code, runs in a worker thread |
| UI widgets | Themed `Segmented` selector and `Dialog` |
| `App` | The window: layout, trimming logic, and a queue that carries worker updates back to the UI thread |

## Dependencies

- [CustomTkinter](https://github.com/TomSchimansky/CustomTkinter) for the interface
- [Pillow](https://python-pillow.org/) for preview images
- [FFmpeg](https://ffmpeg.org/) for all media processing
