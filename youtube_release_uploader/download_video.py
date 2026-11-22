#!/usr/bin/env python3
"""Download the latest eligible BhagvadGita upload and keep only source.mp4 in Release."""
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

CHANNEL_ID = os.environ["YOUTUBE_CHANNEL_ID"]
RELEASE_TAG = os.getenv("RELEASE_TAG", "Friday")
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
REPO = os.environ["GITHUB_REPOSITORY"]
PREFIX = "BhagvadGita"  # Exact, case-sensitive title prefix
MAX_AGE_HOURS = 36
MIN_BYTES = 1_000_000
WORK = Path("work")
WORK.mkdir(exist_ok=True)
YT_DLP = ["yt-dlp", "--extractor-args", "youtube:player_client=mweb,web_safari"]


def run(args, *, capture=False, check=True):
    print("+", " ".join(map(str, args)), flush=True)
    return subprocess.run(
        list(map(str, args)), check=check,
        text=True, capture_output=capture
    )


def release_assets():
    data = run(["gh", "release", "view", RELEASE_TAG, "--repo", REPO,
                "--json", "assets"], capture=True)
    return json.loads(data.stdout).get("assets", [])


def download_asset(name, destination):
    result = run(["gh", "release", "download", RELEASE_TAG, "--repo", REPO,
                  "--pattern", name, "--dir", str(destination.parent)],
                 check=False)
    candidate = destination.parent / name
    if result.returncode != 0 or not candidate.is_file():
        return False
    if candidate != destination:
        candidate.replace(destination)
    return True


def probe_video(path):
    if not path.is_file() or path.stat().st_size < MIN_BYTES:
        raise RuntimeError("Downloaded video is empty or implausibly small.")
    result = run([
        "ffprobe", "-v", "error", "-show_entries",
        "format=format_name,duration:format_tags=comment:stream=codec_type,codec_name,width,height",
        "-of", "json", str(path)
    ], capture=True)
    data = json.loads(result.stdout)
    fmt = data.get("format", {})
    if "mp4" not in fmt.get("format_name", ""):
        raise RuntimeError("Downloaded file is not a valid MP4 container.")
    try:
        if float(fmt.get("duration", 0)) <= 0:
            raise RuntimeError("Video duration is missing or zero.")
    except (TypeError, ValueError):
        raise RuntimeError("Video duration is invalid.")
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if not video or not audio:
        raise RuntimeError("Both video and audio streams are required.")
    if video.get("codec_name") != "h264" or audio.get("codec_name") != "aac":
        raise RuntimeError("Only H.264 video + AAC audio is accepted.")
    if min(int(video.get("width", 0)), int(video.get("height", 0))) < 720:
        raise RuntimeError("Resolution is below the approved 720p minimum.")
    print(f"Validated MP4: {video.get('width')}x{video.get('height')}, "
          f"H.264/AAC, {path.stat().st_size} bytes", flush=True)
    return data


def embedded_video_id(path):
    """Read the YouTube ID stored in the MP4 comment metadata."""
    if not path.is_file():
        return None
    result = run([
        "ffprobe", "-v", "error", "-show_entries", "format_tags=comment",
        "-of", "json", str(path)
    ], capture=True, check=False)
    if result.returncode != 0:
        return None
    try:
        comment = json.loads(result.stdout).get("format", {}).get("tags", {}).get("comment", "")
    except (json.JSONDecodeError, AttributeError):
        return None
    marker = "youtube_video_id="
    for part in str(comment).split(";"):
        if part.startswith(marker):
            return part[len(marker):]
    return None


def current_release_video_id():
    asset = next((a for a in release_assets() if a.get("name") == "source.mp4"), None)
    if not asset:
        return None
    local = WORK / "existing_source.mp4"
    if not download_asset("source.mp4", local):
        raise RuntimeError("Existing source.mp4 could not be read; refusing to risk Release cleanup.")
    if local.stat().st_size != int(asset.get("size", -1)):
        raise RuntimeError("Existing source.mp4 size mismatch; refusing to risk Release cleanup.")
    return embedded_video_id(local)


def get_candidate():
    channel_url = f"https://www.youtube.com/channel/{CHANNEL_ID}/streams"
    listing = run(YT_DLP + ["--flat-playlist", "--playlist-end", "50",
                   "--dump-single-json", channel_url], capture=True)
    entries = json.loads(listing.stdout).get("entries", [])
    now = dt.datetime.now(dt.timezone.utc).timestamp()
    candidates = []

    # A channel may list a scheduled/live placeholder before the previous
    # completed livestream. Such entries can have no playable metadata yet.
    # Skip that one entry and continue checking older BhagvadGita uploads.
    for item in entries:
        title = item.get("title") or ""
        if not title.startswith(PREFIX):
            continue
        video_id = item.get("id")
        if not video_id:
            print(f"Skipping BhagvadGita entry without a video ID: {title!r}", flush=True)
            continue

        # The /streams tab also contains upcoming scheduled broadcasts.
        # Do not even request their watch-page metadata: only actual live
        # broadcasts and completed livestream replays are eligible.
        listing_status = item.get("live_status")
        if listing_status not in ("is_live", "was_live"):
            print(f"Skipping non-live/scheduled entry {video_id} "
                  f"(live_status={listing_status!r}); only actual livestreams are checked.",
                  flush=True)
            continue

        url = f"https://www.youtube.com/watch?v={video_id}"
        info = run(YT_DLP + ["--skip-download", "--dump-single-json", url],
                   capture=True, check=False)
        if info.returncode != 0 or not info.stdout.strip():
            detail = (info.stderr or "").strip().splitlines()
            reason = detail[-1] if detail else "no metadata returned"
            print(f"Skipping live entry {video_id} ({title!r}); yt-dlp error: {reason}",
                  flush=True)
            continue
        try:
            meta = json.loads(info.stdout)
        except json.JSONDecodeError:
            print(f"Skipping entry {video_id}: YouTube returned invalid metadata.", flush=True)
            continue

        # Reconfirm the status from the full watch-page metadata before selecting.
        if meta.get("live_status") not in ("is_live", "was_live"):
            print(f"Skipping entry {video_id}: watch-page status is "
                  f"{meta.get('live_status')!r}, not an actual livestream.", flush=True)
            continue

        timestamp = meta.get("release_timestamp") or meta.get("timestamp")
        if not timestamp:
            upload_date = meta.get("upload_date") or item.get("upload_date")
            if upload_date:
                try:
                    timestamp = dt.datetime.strptime(upload_date, "%Y%m%d").replace(
                        tzinfo=dt.timezone.utc).timestamp()
                except ValueError:
                    timestamp = None

        if not timestamp:
            print(f"Skipping entry {video_id}: no reliable upload time.", flush=True)
            continue

        age_seconds = now - float(timestamp)
        if 0 <= age_seconds <= MAX_AGE_HOURS * 3600:
            candidates.append((float(timestamp), video_id, meta.get("title") or title))
        else:
            print(f"Skipping entry {video_id}: outside the {MAX_AGE_HOURS}-hour window.",
                  flush=True)

    selected = max(candidates, default=None)
    if selected:
        print(f"Selected latest playable BhagvadGita upload: {selected[2]!r} "
              f"({selected[1]}).", flush=True)
    else:
        print("No playable BhagvadGita upload found within the last 36 hours.", flush=True)
    return selected


def remove_non_source_assets():
    """After the new source.mp4 is verified, delete every other Release asset."""
    for asset in release_assets():
        name = asset.get("name", "")
        if name == "source.mp4":
            continue
        run(["gh", "release", "delete-asset", RELEASE_TAG, name,
             "--repo", REPO, "--yes"])
        print(f"Removed old Release asset: {name}", flush=True)

    remaining = sorted(a.get("name") for a in release_assets())
    if remaining != ["source.mp4"]:
        raise RuntimeError(f"Release cleanup verification failed; remaining assets: {remaining}")


def main():
    candidate = get_candidate()
    if not candidate:
        print("No BhagvadGita-prefixed upload found within the last 36 hours. Release unchanged.")
        return
    _, video_id, title = candidate

    if not DRY_RUN:
        existing_id = current_release_video_id()
        if video_id == existing_id:
            print(f"Already present in Friday Release; skipping duplicate: {video_id}")
            return
    else:
        print("DRY RUN: skipping Release duplicate check; no Release API will be called.")

    output = WORK / "downloaded.%(ext)s"
    format_selector = (
        "bv*[height<=1080][height>=720][vcodec^=avc1]+"
        "ba[acodec^=mp4a]/"
        "b[height<=1080][height>=720][ext=mp4]"
    )
    run(YT_DLP + ["--no-playlist", "--format", format_selector,
         "--merge-output-format", "mp4", "--output", str(output),
         f"https://www.youtube.com/watch?v={video_id}"])
    downloaded = next(WORK.glob("downloaded.*"), None)
    if not downloaded:
        raise RuntimeError("yt-dlp completed without producing a file.")
    probe_video(downloaded)

    # Embed the source ID without re-encoding, so duplicate checks need no JSON asset.
    source = WORK / "source.mp4"
    run(["ffmpeg", "-y", "-i", str(downloaded), "-map", "0", "-c", "copy",
         "-metadata", f"comment=youtube_video_id={video_id}",
         "-movflags", "+faststart", str(source)])
    probe_video(source)

    if DRY_RUN:
        print(f"DRY RUN SUCCESS: downloaded and validated {title!r} ({video_id}); "
              f"source.mp4 is {source.stat().st_size} bytes. Release unchanged.", flush=True)
        return

    # Replace source only after the new file is fully downloaded and validated.
    run(["gh", "release", "upload", RELEASE_TAG, str(source),
         "--repo", REPO, "--clobber"])
    uploaded = next((a for a in release_assets() if a.get("name") == "source.mp4"), None)
    if not uploaded or int(uploaded.get("size", -1)) != source.stat().st_size:
        raise RuntimeError("Uploaded source.mp4 could not be verified; old assets were not cleaned.")

    # Keep the Release strictly to one asset. Cleanup happens only after source.mp4 is verified.
    remove_non_source_assets()
    print(f"SUCCESS: uploaded {title!r} ({video_id}) as the only Release asset: source.mp4",
          flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        raise
