# Auto Captioner

A free desktop tool that turns the speech in any video into captions. It transcribes with
[faster-whisper](https://github.com/SYSTRAN/faster-whisper) (OpenAI's open-source Whisper
model) **on your own computer**. No API keys, no accounts, no usage fees, no cloud.

It uses the same white-and-blue interface as Video Clipper.

## What it does

1. Pick a video (`.mp4 .mov .mkv .avi .webm ...`).
2. Choose **Burned-in**, **SRT file**, or **Both**.
3. Pick a Whisper model, the spoken language and a caption style (live preview).
4. Press **Generate Captions**. The app extracts the audio, transcribes it locally, writes
   the `.srt` and/or burns the captions into a new video with FFmpeg. The window stays
   responsive and you can **Cancel** at any time.

Options: model size (`tiny`, `base`, `small`, `medium`), language (auto-detect or pick one,
including Telugu, Hindi and Tamil), transcribe or translate to English, CPU or NVIDIA GPU,
1 or 2 lines per caption, font size, text color, outline or background box, and
bottom / center / top position.

## Setup

### 1. Install Python 3.9 or newer

Download from <https://www.python.org/downloads/>. On Windows, tick **Add python.exe to PATH**.

- **Linux only:** Tkinter is a separate package: `sudo apt install python3-tk`
- **macOS (Homebrew Python):** `brew install python-tk`

### 2. Install FFmpeg (free)

The app needs `ffmpeg` and `ffprobe` on your PATH. Burning captions also needs an FFmpeg build
with libass, which every standard build below includes.

| System  | Command                                           |
|---------|---------------------------------------------------|
| Windows | `winget install Gyan.FFmpeg` (then reopen the terminal) |
| macOS   | `brew install ffmpeg`                             |
| Linux   | `sudo apt install ffmpeg`                         |

Check it worked: `ffmpeg -version`. The top-right chip in the app shows
**FFmpeg ... ready** when it is found. If it says **FFmpeg not found**, the app tells you
what to install; restart it afterwards.

### 3. Install the Python packages

Open a terminal in this folder and run:

```bash
python -m venv .venv
# Windows:   .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

### 4. Run it

```bash
python auto_captioner.py
```

## Running completely offline

Whisper needs its model file once. It is a free download from Hugging Face (no account, no
key). After that, nothing ever touches the network.

- **Online computer:** just use the app. The first run with a model shows
  *"Downloading the 'small' model (one time only)..."* and caches it.
- **Computer that will never be online:** prepare the model on any connected computer:

  ```bash
  python auto_captioner.py --download-model small
  ```

  Then copy the folder `~/.auto_captioner/models` (on Windows `C:\Users\<you>\.auto_captioner\models`)
  to the same place on the offline computer. You can also set the environment variable
  `AUTO_CAPTIONER_MODELS` to use another folder.

The model hint in the app says *"Ready on this computer (works offline)"* once a model is cached.

### Which model should I pick?

| Model  | Download | Notes                                              |
|--------|----------|----------------------------------------------------|
| tiny   | ~75 MB   | Fastest, lowest accuracy                           |
| base   | ~145 MB  | Fast, fine for clear speech                        |
| small  | ~480 MB  | **Default.** Good accuracy and speed on a CPU      |
| medium | ~1.5 GB  | Most accurate, slow on a CPU (a GPU helps a lot)   |

For languages other than English, such as Telugu, `small` or `medium` give much better results
than `tiny` or `base`. Selecting the language manually is more reliable than auto-detect.

## Output

- **SRT file:** `yourvideo.srt` next to the video (UTF-8, works in VLC, YouTube, editors).
- **Burned-in video:** `yourvideo_captioned.mp4` (or `.mov` / `.mkv`). Video is re-encoded
  with H.264. Audio is copied unchanged when the container allows it, otherwise AAC.
- You can change the save location with **Save as**. Existing files are never replaced without asking.

## Optional: NVIDIA GPU

Choose **GPU (NVIDIA CUDA)** under *Processor*. This needs an NVIDIA GPU and CUDA 12 libraries
(`pip install nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*"`, see `requirements.txt`).
If the GPU cannot be used, the app logs it and falls back to the CPU automatically.

## Troubleshooting

| Problem | Fix |
|---|---|
| *FFmpeg not found* | Install FFmpeg (step 2), reopen the terminal, restart the app. |
| *libass is missing* | Install a full FFmpeg build (see the table above), or use **SRT file** mode. |
| *Model could not be downloaded* | Connect to the internet once, or follow the offline steps above. |
| *No speech was detected* | Set the language manually, try a larger model, check the audio track. |
| Captions are slightly early or late | Re-run with a larger model; timing comes from word-level Whisper timestamps. |
| Slow on a long video | Use `base`, or a GPU. CPU speed depends on your processor. |
| App crashes on start (Linux) | `sudo apt install python3-tk` |

Logs are written to `~/.auto_captioner/app.log`.

## Files

- `auto_captioner.py`: the application (single file)
- `requirements.txt`: Python dependencies
- `README.md`: this guide

## Licenses

All components are free and open source: Whisper (MIT), faster-whisper (MIT),
CustomTkinter (MIT), Pillow (HPND), FFmpeg (LGPL/GPL depending on build).
