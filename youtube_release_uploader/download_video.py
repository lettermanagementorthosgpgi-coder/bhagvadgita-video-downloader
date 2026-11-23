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
COOKIES_FILE = os.getenv("YOUTUBE_COOKIES_FILE")
# Let yt-dlp and the installed PO-token provider negotiate supported YouTube clients.
# Prefer clients supported by the installed PO-token provider. The mweb
# client is the current yt-dlp recommendation for GVS PO-token requests;
# tv and web_safari remain fallbacks if YouTube rejects that client.
YT_DLP = [
    "yt-dlp", "--js-runtimes", "deno",
    "--extractor-args", "youtube:player-client=mweb,tv,web_safari",
]
if COOKIES_FILE:
    if not Path(COOKIES_FILE).is_file():
        raise RuntimeError("Configured YouTube cookies file is missing.")
    YT_DLP += ["--cookies", COOKIES_FILE]


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

    # /streams is newest-first. Skip scheduled placeholders and non-BhagvadGita
    # entries, then inspect only the first actual BhagvadGita live/replay.
    # Never fall back to older videos if this newest candidate fails.
    for item in entries:
        title = item.get("title") or ""
        if not title.startswith(PREFIX):
            continue

        video_id = item.get("id")
        if not video_id:
            print(f"Newest BhagvadGita entry has no video ID: {title!r}; stopping.",
                  flush=True)
            return None

        listing_status = item.get("live_status")
        if listing_status not in ("is_live", "was_live"):
            print(f"Skipping non-live/scheduled entry {video_id} "
                  f"(live_status={listing_status!r}); looking for the newest actual replay.",
                  flush=True)
            continue

        url = f"https://www.youtube.com/watch?v={video_id}"
        info = run(YT_DLP + ["--ignore-no-formats-error", "--skip-download",
                             "--dump-single-json", url],
                   capture=True, check=False)
        if info.returncode != 0 or not info.stdout.strip():
            detail = (info.stderr or "").strip().splitlines()
            reason = detail[-1] if detail else "no metadata returned"
            print(f"Newest BhagvadGita live/replay {video_id} could not be inspected: "
                  f"{reason}. Stopping; older videos will not be checked.", flush=True)
            return None

        try:
            meta = json.loads(info.stdout)
        except json.JSONDecodeError:
            print(f"YouTube returned invalid metadata for newest candidate {video_id}; "
                  "stopping without checking older videos.", flush=True)
            return None

        watch_status = meta.get("live_status")
        if watch_status in ("is_live", "was_live"):
            confirmed_live_status = watch_status
        elif watch_status is None and listing_status in ("is_live", "was_live"):
            confirmed_live_status = listing_status
            print(f"Watch-page live_status is missing for {video_id}; "
                  f"using /streams status {listing_status!r} to confirm replay.",
                  flush=True)
        else:
            print(f"Newest candidate {video_id} has watch-page status "
                  f"{watch_status!r}, not an actual livestream. Stopping.", flush=True)
            return None

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
            print(f"Newest candidate {video_id} has no reliable upload time; stopping.",
                  flush=True)
            return None

        age_seconds = now - float(timestamp)
        if not 0 <= age_seconds <= MAX_AGE_HOURS * 3600:
            print(f"Newest BhagvadGita live/replay {video_id} is outside the "
                  f"{MAX_AGE_HOURS}-hour window; stopping without checking older videos.",
                  flush=True)
            return None

        selected_title = meta.get("title") or title
        print(f"Selected newest BhagvadGita live/replay: {selected_title!r} "
              f"({video_id}; {confirmed_live_status}).", flush=True)
        return (float(timestamp), video_id, selected_title)

    print("No actual BhagvadGita live/replay was found in the /streams listing.")
    return None


def diagnose_youtube_access(video_url):
    """Compare anonymous and cookie-authenticated access without exposing cookie contents."""
    import socket
    import urllib.error
    import urllib.request

    print("\\n=== YOUTUBE ACCESS DIAGNOSTICS (NO DOWNLOAD / NO RELEASE WRITES) ===", flush=True)
    print(f"Runner OS: {os.getenv('RUNNER_OS', 'unknown')} | Python: {sys.version.split()[0]}", flush=True)
    try:
        version = run(["yt-dlp", "--version"], capture=True).stdout.strip()
        print(f"yt-dlp version: {version}", flush=True)
    except Exception as exc:
        print(f"yt-dlp version check failed: {type(exc).__name__}: {exc}", flush=True)

    for host in ("www.youtube.com", "youtubei.googleapis.com", "googlevideo.com"):
        try:
            addresses = sorted({x[4][0] for x in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)})
            print(f"DNS {host}: OK ({', '.join(addresses[:3])})", flush=True)
        except Exception as exc:
            print(f"DNS {host}: FAIL ({type(exc).__name__}: {exc})", flush=True)

    for url in ("https://www.youtube.com/robots.txt", video_url):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=20) as response:
                print(f"HTTP {url.split('/')[2]}: {response.status}", flush=True)
        except urllib.error.HTTPError as exc:
            print(f"HTTP {url.split('/')[2]}: HTTP_ERROR {exc.code}", flush=True)
        except Exception as exc:
            print(f"HTTP {url.split('/')[2]}: NETWORK_ERROR {type(exc).__name__}: {exc}", flush=True)

    if COOKIES_FILE:
        try:
            lines = Path(COOKIES_FILE).read_text(errors="replace").splitlines()
            records = [line for line in lines if line and not line.startswith("#") and "\\t" in line]
            domains = sorted({line.split("\\t")[0] for line in records})
            print(f"Cookie file: present; bytes={Path(COOKIES_FILE).stat().st_size}; records={len(records)}; youtube_domain_present={any('youtube.com' in d for d in domains)}. Values are not displayed.", flush=True)
        except Exception as exc:
            print(f"Cookie file inspection: FAIL ({type(exc).__name__}: {exc})", flush=True)
    else:
        print("Cookie file: not configured", flush=True)

    def classify(output, code):
        value = (output or "").lower()
        if "login_required" in value or "sign in to confirm you're not a bot" in value:
            return "LOGIN_REQUIRED_OR_IP_CHALLENGE"
        if "http error 403" in value or "http error 429" in value:
            return "HTTP_403_OR_429"
        if "po token" in value and any(x in value for x in ("missing", "required", "unable")):
            return "PO_TOKEN_MISSING_OR_REJECTED"
        if "requested format is not available" in value or "no video formats found" in value:
            return "PLAYER_RESPONSE_WITHOUT_USABLE_FORMATS"
        if "video unavailable" in value:
            return "VIDEO_UNAVAILABLE_OR_NOT_PUBLIC"
        return "OK_FORMATS_AVAILABLE" if code == 0 else "OTHER_YTDLP_ERROR"

    anonymous = [x for x in YT_DLP if x != "--cookies" and x != COOKIES_FILE]
    probes = [("ANONYMOUS_NO_COOKIES", anonymous)]
    if COOKIES_FILE:
        probes.append(("CONFIGURED_COOKIES", YT_DLP))
    for label, command in probes:
        print(f"\\n--- {label} ---", flush=True)
        result = run(command + ["--verbose", "--no-playlist", "--list-formats", video_url],
                     capture=True, check=False)
        combined = (result.stdout or "") + "\\n" + (result.stderr or "")
        print(f"RESULT {label}: {classify(combined, result.returncode)} (exit={result.returncode})", flush=True)
        for marker in ("PO Token Providers:", "LOGIN_REQUIRED", "Sign in to confirm you're not a bot",
                       "HTTP Error 403", "Requested format is not available", "Available formats"):
            for line in [x.strip() for x in combined.splitlines() if marker.lower() in x.lower()][:2]:
                if "cookie" not in line.lower() and "authorization" not in line.lower():
                    print("DIAG: " + line[:400], flush=True)
    print("=== END DIAGNOSTICS. No video downloaded; Release unchanged. ===", flush=True)


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
    video_url = f"https://www.youtube.com/watch?v={video_id}"
    if os.getenv("DIAGNOSTIC_MODE", "false").lower() == "true":
        diagnose_youtube_access(video_url)
        return

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
    video_url = f"https://www.youtube.com/watch?v={video_id}"
    print("Checking formats available for the selected livestream:", flush=True)
    # Verbose mode records whether bgutil providers were loaded and whether
    # a PO token was actually requested/generated for the selected client.
    run(YT_DLP + ["--verbose", "--no-playlist", "--list-formats", video_url])
    run(YT_DLP + ["--no-playlist", "--format", format_selector,
         "--merge-output-format", "mp4", "--output", str(output),
         video_url])
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
