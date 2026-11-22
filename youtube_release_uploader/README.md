# BhagvadGita Video Downloader

Independent repository for downloading the latest eligible Bhagavad Gita YouTube pravachan video and, in production mode, publishing it to this repository's `Friday` GitHub Release as the single asset `source.mp4`.

## Test first

Open **Actions → BhagvadGita Video Downloader → Run workflow**. The manual run defaults to **dry_run=true**. It searches the configured channel, downloads a qualifying video, validates MP4/H.264/AAC, duration, audio/video streams and 720p minimum, and reports the result. Dry run never reads, uploads, overwrites, or deletes Release assets.

## Production

The scheduled run is at 06:00 IST (00:30 UTC). It uses dry_run=false, checks duplicates using the YouTube video ID embedded in the MP4 comment metadata, uploads and verifies `source.mp4`, then removes other Release assets only after successful upload verification.

## Rules

- Exact case-sensitive title prefix: `BhagvadGita`.
- Upload must be no more than 36 hours old.
- Prefer H.264 1080p, accept 720p minimum; AAC audio; MP4 container.
- Remux without re-encoding, preserving original frame rate.
- No eligible video, duplicate, or failed validation means no Release modification.
- This repository is independent of the LIVE Schedule and Shorts Creator repositories.

## Configuration

Channel ID is currently `UC2sBCYCH6Kb9dgqQN3vVgFw`. GitHub Actions uses the repository's built-in token; no YouTube API key is required by yt-dlp for public channel videos.
