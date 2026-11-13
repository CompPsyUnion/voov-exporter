# link2video

Turn Tencent Meeting cloud recording links (or local exported recording assets) into a self-contained local `.mp4` / `.mkv` video with embedded soft Chinese subtitles (`.srt`) and clickable chapters (`;FFMETADATA1`).

## Prerequisites

- Python 3.9+ (zero third-party Python dependencies)
- `ffmpeg` and `ffprobe` on `PATH`, via `FFMPEG_DIR`, or via `--ffmpeg-dir`

## Usage

### 1. From a Tencent Meeting Share URL

```bash
# Default: fast lossless soft subtitles (mov_text) + chapters
link2video "Tencent link here" -o ./meeting.mp4 --ffmpeg-dir "Your_ffmpeg_route_here"

# Burn subtitles directly into video frames (hardsub, visible in every player)
link2video "Tencent link here" -o ./meeting_hardsub.mp4 --burn --ffmpeg-dir "Your_ffmpeg_route_here"
```

- If the recording is password-protected and `--password` is omitted, `link2video` prompts you in the terminal to enter the password (with retry on wrong password).
- Optional flags:
  - `--burn` / `--hard-sub` (burn subtitles directly into video frames via `libx264`; optional `--font-size`, `--font-name`, `--preset`, `--crf`)
  - `--track {composite,screen,speaker,audio}` (default: `composite`)
  - `-p, --password <pwd>`
  - `-c, --cookie <cookie>` (for login-restricted shares)
  - `--keep-temp` (keep downloaded raw MP4, transcript, timeline, `.srt`, and `.ffmeta`)

### 2. Local File Fallback Mode

```bash
link2video --local \
  -v "recording.mp4" \
  -t "transcript.md" \
  -l "timeline.md" \
  -o "./merged.mp4" \
  --ffmpeg-dir "D:\ffmpeg-9.0-full_build"
```

### 3. Standalone Modules

- Part 1 (Transcript + Timeline -> `.srt` + `.ffmeta`): `python -m link2video.processor ...`
- Part 2 (Lossless FFmpeg Muxer): `python -m link2video.muxer ...`
- Part 3 (Tencent Meeting Downloader): `python -m link2video.downloader ...`
