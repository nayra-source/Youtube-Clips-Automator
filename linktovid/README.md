# YT 4K Downloader

A desktop app that downloads a video in **4K (2160p) with the best audio** and saves it as a
QuickTime-ready **`.mov`** file, ready for QuickTime Player, Final Cut Pro and DaVinci Resolve.

It uses the same modern white-and-blue interface as **Video Clipper** and **Auto Captioner**:
stage indicator, progress bar with speed and ETA, collapsible activity log, themed dialogs and a
Cancel button. All network and FFmpeg work runs in a background thread, so the window never
freezes.

> Only download videos you own or have permission to download, and respect each site's terms of
> service.

## Features

- **Fetch Info preview**: title, channel, duration, maximum resolution (for example
  `3840x2160 @ 60fps`), source codec (`vp9`, `av01`, `avc1`), audio codec and estimated file size
- **True 4K**: `bestvideo[height<=2160]+bestaudio`, with a graceful fallback to the highest
  resolution the video offers
- **Three `.mov` modes**: Auto (recommended), Apple ProRes 422, Strict Stream Copy
- **Safe output**: file names are sanitized, existing files are never overwritten, temporary files
  are removed after every job, and the finished `.mov` is re-opened with `ffprobe` and verified
- **Cancel** at any moment: the download or FFmpeg process stops and temporary files are deleted
- **Startup check** for FFmpeg with a helpful install dialog, plus a disk-space check

## Prerequisites

| Requirement | Why | Install |
|---|---|---|
| **Python 3.9+** with tkinter | Runs the app | python.org installers include tkinter. Linux: `sudo apt install python3-tk` |
| **FFmpeg + ffprobe** on your `PATH` | Merges, remuxes and transcodes | macOS: `brew install ffmpeg`<br>Windows: `winget install Gyan.FFmpeg` (reopen the terminal)<br>Linux: `sudo apt install ffmpeg` |
| **Deno** (JavaScript runtime) | Current yt-dlp needs one for full YouTube support. Without it, 4K formats may be missing | macOS: `brew install deno`<br>Windows: `winget install DenoLand.Deno`<br>Linux/macOS: `curl -fsSL https://deno.land/install.sh \| sh` |

Use a **full FFmpeg build**. The app needs the `libx264` and `prores_ks` encoders, which the
builds above include.

## Installation

```bash
git clone <your-repo-url>
cd <your-repo>

python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

pip install -r requirements.txt
```

Run it:

```bash
python3 yt4k_download.py
```

## How to use

1. Paste a video link and press **Fetch Info** (optional: you can also go straight to step 4).
2. Check the **Stream info** card: resolution, codec and estimated sizes.
3. Choose a **.MOV mode** (see below) and the **output folder**. The default is
   `~/Movies/YT4K Downloads` on macOS and `~/Videos/YT4K Downloads` elsewhere.
4. Press **Download 4K .MOV**. The stages are *Fetch*, *Download*, *Remux/Encode*, *Complete*.
5. Use **Play .MOV** or **Open folder** when it finishes.

Files are saved as `Title [2160p].mov`.

## .MOV modes

| | **Auto (Recommended)** | **ProRes 422** | **Strict Stream Copy** |
|---|---|---|---|
| **Video** | H.264 / HEVC source: copied untouched (`-c:v copy`).<br>VP9 / AV1 source: transcoded with `libx264`, `-crf 14`, `-preset slow`, `yuv420p` | `prores_ks`, profile 3 (HQ) or 2 (Standard), `yuv422p10le` | Always `-c:v copy`, never re-encoded |
| **Audio** | Copied if `.mov` can hold it (AAC, MP3, AC-3...), otherwise AAC 320 kbps | PCM 24-bit | Copied if `.mov` can hold it, otherwise only the audio becomes AAC 320 kbps |
| **Speed** | Fast for copy, slow for 4K transcodes | Fast to medium | Fastest |
| **File size** | Moderate | Very large: about 3.5 to 10 GB per minute at 4K | Same as the source |
| **Opens in QuickTime / FCP / Resolve** | Yes | Yes, with zero-lag scrubbing | Only if the video is H.264 / HEVC |
| **Best for** | Almost everyone | Editing and color work | Keeping the original video bit-exact |

**HDR sources:** in Auto mode, an HDR VP9 / AV1 video is converted to ProRes 422 HQ instead of
8-bit H.264, so its 10-bit color is not flattened. The app tells you in the log.

### About "without quality loss"

- **Stream copy is bit-exact** for the video.
- **CRF 14 / preset slow** H.264 is *visually lossless*, not mathematically lossless. Converting
  VP9 / AV1 into any `.mov`-compatible codec needs a re-encode, and no setting can restore detail
  the source stream never had.
- **ProRes** is a high-quality editing intermediate, not a bit-exact copy.

### Strict Stream Copy and VP9 / AV1

FFmpeg's QuickTime muxer cannot store VP9 or AV1 (`vp9 only supported in MP4`), so a real `.mov`
can never contain a copied VP9 / AV1 stream. Most YouTube 4K videos are VP9 or AV1, so in this mode
the app downloads the **best H.264 / HEVC stream instead** (on YouTube that is usually 1080p) and
says so in the log and in the Stream info card. If a video offers no H.264 / HEVC stream at all,
the app stops before downloading and suggests Auto or ProRes. Use **Auto** when you want true 4K.

## Troubleshooting

- **"No 4K offered" but the video has 4K**: update yt-dlp (`pip install -U "yt-dlp[default]"`)
  and install Deno (see Prerequisites). YouTube changes often, so keep yt-dlp current.
- **"Sign in to confirm you're not a bot" / age-restricted / members-only**: this app downloads
  public videos and does not use browser cookies.
- **FFmpeg not found**: install it, then restart the app so the new `PATH` is picked up.
- **Not enough disk space**: the app estimates the download plus the `.mov` size before starting.
  ProRes at 4K needs a lot of free space.
- **Log file**: `~/.yt4k_download/app.log`

## Project files

```
yt4k_download.py   the application (GUI + yt-dlp + FFmpeg pipeline)
requirements.txt   Python dependencies
README.md          this file
```
