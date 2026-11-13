# voov-exporter

Turn Tencent Meeting cloud recording links (or local exported recording assets) into a self-contained local `.mp4` / `.mkv` video with embedded soft Chinese subtitles (`.srt`) and clickable chapters (`;FFMETADATA1`).

## Prerequisites

- Python 3.9+ (zero third-party Python dependencies)
- `ffmpeg` and `ffprobe` on `PATH`, via `FFMPEG_DIR`, or via `--ffmpeg-dir`

## Usage

### 1. From a Tencent Meeting Share URL

```bash
# Default: fast lossless soft subtitles (mov_text) + chapters
voov-exporter "Tencent link here" -o ./meeting.mp4 --ffmpeg-dir "Your_ffmpeg_route_here"

# Burn subtitles directly into video frames (hardsub, visible in every player)
voov-exporter "Tencent link here" -o ./meeting_hardsub.mp4 --burn --ffmpeg-dir "Your_ffmpeg_route_here"
```

- Only public (no-password, no-login) recordings are fetched directly without cookies; if a link fails because it requires a password or account login, pass browser cookies via `--cookie` or download the assets manually and use `--local`.
- Optional flags:
  - `--burn` / `--hard-sub` (burn subtitles directly into video frames via `libx264`; optional `--font-size`, `--font-name`, `--preset`, `--crf`)
  - `--track {composite,screen,speaker,audio}` (default: `composite`)
  - `-c, --cookie <cookie>` (for login-restricted shares)
  - `--keep-temp` (keep downloaded raw MP4, transcript, timeline, `.srt`, and `.ffmeta`)

### 2. Local File Fallback Mode

```bash
voov-exporter --local \
  -v "recording.mp4" \
  -t "transcript.md" \
  -l "timeline.md" \
  -o "./merged.mp4" \
  --ffmpeg-dir "D:\ffmpeg-9.0-full_build"
```

### 3. Standalone Modules

- Part 1 (Transcript + Timeline -> `.srt` + `.ffmeta`): `python -m voov_exporter.processor ...`
- Part 2 (Lossless FFmpeg Muxer): `python -m voov_exporter.muxer ...`
- Part 3 (Tencent Meeting Downloader): `python -m voov_exporter.downloader ...`
