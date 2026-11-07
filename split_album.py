#!/usr/bin/env python3
r"""
split_album.py - split one audio file holding a whole album (.m4a / .mp3) into
one file per track, with the right metadata on each file.

Usage:
    python split_album.py INPUT --artist ARTIST --album ALBUM [--release-id MBID] [-o DIR] [--dry-run]

python split_album.py "C:\Music\Pink Floyd - Wish You Were Here.m4a" --artist "Pink Floyd" --album "Wish You Were Here"

How it works:
    1. The album is looked up in MusicBrainz (free, no API key). When several
       releases match, the best-scoring official album release is used; the
       alternatives are printed so you can force one with --release-id.
    2. The file is cut at the cumulative track lengths from MusicBrainz. The
       last track runs to the end of the file.
    3. Cutting uses stream copy (no re-encoding, no quality loss) for AAC and
       MP3 input. Other codecs are re-encoded to AAC (.m4a, 256 kbit/s).
    4. Each track gets tags: title, artist, album artist, album, track and disc
       number, year, genre and the MusicBrainz IDs.
    5. tracks.md (overview: number, title, length, file name, start) is written
       next to the tracks and printed to the console.

Output: <input folder>/<Artist>/<Year> - <Album>/NN - Title.ext
        (D-NN - Title.ext for multi-disc albums). Use -o to choose the folder.
        Existing files with the same name are overwritten.

--dry-run shows the chosen release and the computed cut points without
writing anything.

Requires ffmpeg (see cut_video.py) and mutagen (pip install mutagen).
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from cut_video import find_ffmpeg, fmt

MB_API = "https://musicbrainz.org/ws/2"
USER_AGENT = "split_album/0.1 ( https://github.com/lucasjellema/video-utils )"

# Warn when the file duration and the album length from MusicBrainz differ more
# than this (seconds, or fraction of the album length - whichever is larger).
DURATION_TOLERANCE_S = 5.0
DURATION_TOLERANCE_FRAC = 0.01


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #

@dataclass
class Track:
    disc: int
    number: int              # position on the disc
    title: str
    length: float | None     # seconds; None when MusicBrainz has no length
    artist: str
    artist_ids: list[str] = field(default_factory=list)
    recording_id: str = ""
    track_id: str = ""


@dataclass
class Release:
    id: str
    title: str
    artist: str
    artist_ids: list[str]
    date: str                # YYYY, YYYY-MM or YYYY-MM-DD; may be empty
    genre: str
    release_group_id: str
    tracks: list[Track]

    @property
    def year(self) -> str:
        return self.date[:4]

    @property
    def disc_count(self) -> int:
        return max((t.disc for t in self.tracks), default=1)

    def tracks_on_disc(self, disc: int) -> int:
        return sum(1 for t in self.tracks if t.disc == disc)

    def total_length(self) -> float | None:
        if any(t.length is None for t in self.tracks):
            return None
        return sum(t.length for t in self.tracks)


# --------------------------------------------------------------------------- #
# MusicBrainz
# --------------------------------------------------------------------------- #

_last_mb_call = 0.0


def mb_get(path: str, params: dict) -> dict:
    """GET a MusicBrainz JSON resource; respects the 1 request/second limit."""
    global _last_mb_call
    query = urllib.parse.urlencode({**params, "fmt": "json"})
    url = f"{MB_API}/{path}?{query}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/json"})
    for attempt in range(3):
        wait = 1.0 - (time.monotonic() - _last_mb_call)
        if wait > 0:
            time.sleep(wait)
        _last_mb_call = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            if exc.code == 503 and attempt < 2:     # rate limited: back off
                time.sleep(2 * (attempt + 1))
                continue
            if exc.code == 404:
                raise LookupError(f"not found in MusicBrainz: {path}") from exc
            raise RuntimeError(f"MusicBrainz request failed ({exc.code}): {url}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"cannot reach MusicBrainz: {exc.reason}") from exc
    raise RuntimeError("MusicBrainz keeps refusing requests (rate limit); try again later")


def _lucene(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def search_releases(artist: str, album: str) -> list[dict]:
    query = f'release:"{_lucene(album)}" AND artist:"{_lucene(artist)}"'
    return mb_get("release/", {"query": query, "limit": 25}).get("releases", [])


def rank_releases(candidates: list[dict]) -> list[dict]:
    """Best first: search score, then official, then album, then earliest date."""
    def key(r):
        rg = r.get("release-group", {})
        return (-int(r.get("score", 0)),
                r.get("status") != "Official",
                rg.get("primary-type") != "Album",
                r.get("date") or "9999")
    return sorted(candidates, key=key)


def describe_candidate(r: dict) -> str:
    parts = [r.get("date") or "????", r.get("country") or "",
             "+".join(m.get("format") or "?" for m in r.get("media", [])),
             f"{r.get('track-count', '?')} tracks"]
    if r.get("disambiguation"):
        parts.append(r["disambiguation"])
    return f"{r['id']}  " + ", ".join(p for p in parts if p)


def fetch_release(release_id: str) -> dict:
    return mb_get(f"release/{release_id}",
                  {"inc": "recordings artist-credits release-groups genres"})


def _credit(credits: list[dict]) -> tuple[str, list[str]]:
    name = "".join(c.get("name", "") + c.get("joinphrase", "") for c in credits)
    ids = [c["artist"]["id"] for c in credits if c.get("artist", {}).get("id")]
    return name.strip(), ids


def parse_release(data: dict) -> Release:
    rg = data.get("release-group", {})
    artist, artist_ids = _credit(data.get("artist-credit", []))
    genres = data.get("genres") or rg.get("genres") or []
    genre = max(genres, key=lambda g: g.get("count", 0))["name"].title() if genres else ""

    tracks = []
    for medium in sorted(data.get("media", []), key=lambda m: m.get("position", 0)):
        for t in medium.get("tracks", []):
            rec = t.get("recording", {})
            length_ms = t.get("length") or rec.get("length")
            t_artist, t_ids = _credit(t.get("artist-credit") or rec.get("artist-credit") or [])
            tracks.append(Track(
                disc=medium.get("position", 1),
                number=t.get("position", len(tracks) + 1),
                title=t.get("title") or rec.get("title", ""),
                length=length_ms / 1000 if length_ms else None,
                artist=t_artist or artist,
                artist_ids=t_ids or artist_ids,
                recording_id=rec.get("id", ""),
                track_id=t.get("id", ""),
            ))
    if not tracks:
        raise ValueError(f"release {data.get('id')} has no tracks")

    return Release(
        id=data["id"], title=data.get("title", ""), artist=artist, artist_ids=artist_ids,
        date=rg.get("first-release-date") or data.get("date") or "",
        genre=genre, release_group_id=rg.get("id", ""), tracks=tracks)


# --------------------------------------------------------------------------- #
# Cut points
# --------------------------------------------------------------------------- #

def compute_segments(tracks: list[Track], duration: float) -> list[tuple[float, float]]:
    """(start, end) per track from the cumulative lengths; last track runs to the end."""
    segments, pos = [], 0.0
    for i, t in enumerate(tracks):
        last = i == len(tracks) - 1
        if pos >= duration:
            raise ValueError(f"the file ({fmt(duration)}) ends before track {i + 1} "
                             f"'{t.title}' starts ({fmt(pos)}); wrong album or release?")
        if last:
            end = duration
        elif t.length is None:
            raise ValueError(f"MusicBrainz has no length for track {i + 1} '{t.title}'; "
                             "try another release with --release-id")
        else:
            end = min(pos + t.length, duration)
        segments.append((pos, end))
        pos = end
    return segments


def duration_mismatch(release: Release, duration: float) -> float | None:
    """Seconds the file is longer (+) or shorter (-) than the album, if beyond tolerance."""
    total = release.total_length()
    if total is None:
        return None
    diff = duration - total
    if abs(diff) > max(DURATION_TOLERANCE_S, DURATION_TOLERANCE_FRAC * total):
        return diff
    return None


# --------------------------------------------------------------------------- #
# File names
# --------------------------------------------------------------------------- #

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize(name: str, max_len: int = 120) -> str:
    """Make a string safe as a Windows file or folder name."""
    name = _ILLEGAL.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip()
    name = name[:max_len].rstrip(" .")
    return name or "_"


def track_filename(track: Track, multi_disc: bool, ext: str) -> str:
    prefix = f"{track.disc}-{track.number:02d}" if multi_disc else f"{track.number:02d}"
    return sanitize(f"{prefix} - {track.title}") + ext


def default_output_dir(input_path: str, release: Release) -> str:
    album = f"{release.year} - {release.title}" if release.year else release.title
    return os.path.join(os.path.dirname(os.path.abspath(input_path)),
                        sanitize(release.artist), sanitize(album))


# --------------------------------------------------------------------------- #
# ffmpeg
# --------------------------------------------------------------------------- #

def probe_audio(ffmpeg: str, path: str) -> tuple[float, str]:
    """Return (duration_seconds, audio_codec) by parsing `ffmpeg -i` output."""
    result = subprocess.run([ffmpeg, "-hide_banner", "-i", path],
                            capture_output=True, text=True, errors="replace")
    info = result.stderr
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", info)
    if not m:
        sys.exit(f"error: could not read the duration of '{path}'\n{info}")
    duration = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    a = re.search(r"Stream #\S+.*?: Audio: (\w+)", info)
    if not a:
        sys.exit(f"error: '{path}' has no audio stream")
    return duration, a.group(1).lower()


def output_format(codec: str) -> tuple[str, bool]:
    """(extension, stream_copy) for the input codec."""
    if codec == "aac":
        return ".m4a", True
    if codec == "mp3":
        return ".mp3", True
    return ".m4a", False


def cut_track(ffmpeg: str, src: str, dst: str, start: float, end: float, copy: bool):
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
           "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", src,
           "-map", "0:a:0", "-vn", "-map_metadata", "-1", "-map_chapters", "-1"]
    cmd += ["-c:a", "copy"] if copy else ["-c:a", "aac", "-b:a", "256k"]
    if dst.lower().endswith(".m4a"):
        cmd += ["-movflags", "+faststart"]
    cmd.append(dst)
    subprocess.run(cmd, check=True)


# --------------------------------------------------------------------------- #
# Tags
# --------------------------------------------------------------------------- #

def tag_file(path: str, release: Release, track: Track):
    if path.lower().endswith(".mp3"):
        _tag_mp3(path, release, track)
    else:
        _tag_mp4(path, release, track)


def _mb_ids(release: Release, track: Track) -> dict[str, str]:
    ids = {
        "MusicBrainz Album Id": release.id,
        "MusicBrainz Release Group Id": release.release_group_id,
        "MusicBrainz Album Artist Id": "/".join(release.artist_ids),
        "MusicBrainz Artist Id": "/".join(track.artist_ids),
        "MusicBrainz Release Track Id": track.track_id,
    }
    return {k: v for k, v in ids.items() if v}


def _tag_mp4(path: str, release: Release, track: Track):
    from mutagen.mp4 import MP4, MP4FreeForm

    audio = MP4(path)
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags
    tags["\xa9nam"] = [track.title]
    tags["\xa9ART"] = [track.artist]
    tags["aART"] = [release.artist]
    tags["\xa9alb"] = [release.title]
    tags["trkn"] = [(track.number, release.tracks_on_disc(track.disc))]
    tags["disk"] = [(track.disc, release.disc_count)]
    if release.date:
        tags["\xa9day"] = [release.date]
    if release.genre:
        tags["\xa9gen"] = [release.genre]
    ids = _mb_ids(release, track)
    if track.recording_id:
        ids["MusicBrainz Track Id"] = track.recording_id
    for name, value in ids.items():
        tags[f"----:com.apple.iTunes:{name}"] = [MP4FreeForm(value.encode("utf-8"))]
    audio.save()


def _tag_mp3(path: str, release: Release, track: Track):
    from mutagen.id3 import (ID3, ID3NoHeaderError, TALB, TCON, TDRC, TIT2, TPE1,
                             TPE2, TPOS, TRCK, TXXX, UFID)

    try:
        tags = ID3(path)
        tags.delete(path)
    except ID3NoHeaderError:
        pass
    tags = ID3()
    tags.add(TIT2(encoding=3, text=track.title))
    tags.add(TPE1(encoding=3, text=track.artist))
    tags.add(TPE2(encoding=3, text=release.artist))
    tags.add(TALB(encoding=3, text=release.title))
    tags.add(TRCK(encoding=3, text=f"{track.number}/{release.tracks_on_disc(track.disc)}"))
    tags.add(TPOS(encoding=3, text=f"{track.disc}/{release.disc_count}"))
    if release.date:
        tags.add(TDRC(encoding=3, text=release.date))
    if release.genre:
        tags.add(TCON(encoding=3, text=release.genre))
    for name, value in _mb_ids(release, track).items():
        tags.add(TXXX(encoding=3, desc=name, text=value))
    if track.recording_id:
        tags.add(UFID(owner="http://musicbrainz.org", data=track.recording_id.encode()))
    tags.save(path, v2_version=4)


# --------------------------------------------------------------------------- #
# Overview
# --------------------------------------------------------------------------- #

def track_label(track: Track, multi_disc: bool) -> str:
    return f"{track.disc}-{track.number}" if multi_disc else str(track.number)


def build_rows(release: Release, segments, filenames) -> list[dict]:
    multi = release.disc_count > 1
    return [{"nr": track_label(t, multi), "title": t.title, "length": fmt(e - s),
             "file": f, "start": fmt(s)}
            for t, (s, e), f in zip(release.tracks, segments, filenames)]


COLUMNS = [("nr", "#"), ("title", "Title"), ("length", "Length"), ("file", "File"),
           ("start", "Start")]


def console_table(rows: list[dict]) -> str:
    widths = {k: max(len(h), *(len(r[k]) for r in rows)) for k, h in COLUMNS}
    line = lambda vals: "  ".join(v.ljust(widths[k]) for (k, _), v in zip(COLUMNS, vals))
    out = [line([h for _, h in COLUMNS]), line(["-" * widths[k] for k, _ in COLUMNS])]
    out += [line([r[k] for k, _ in COLUMNS]) for r in rows]
    return "\n".join(out)


def markdown_report(release: Release, rows: list[dict], source: str, duration: float) -> str:
    esc = lambda s: s.replace("|", "\\|")
    lines = [f"# {release.artist} - {release.title}", ""]
    if release.date:
        lines.append(f"- Year: {release.year}")
    if release.genre:
        lines.append(f"- Genre: {release.genre}")
    lines += [f"- Tracks: {len(rows)}, total {fmt(duration)}",
              f"- Source: `{os.path.basename(source)}`",
              f"- MusicBrainz release: https://musicbrainz.org/release/{release.id}",
              "",
              "| " + " | ".join(h for _, h in COLUMNS) + " |",
              "|" + "|".join("---" for _ in COLUMNS) + "|"]
    lines += ["| " + " | ".join(esc(r[k]) for k, _ in COLUMNS) + " |" for r in rows]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def lookup_release(artist: str | None, album: str | None, release_id: str | None) -> Release:
    if not release_id:
        candidates = rank_releases(search_releases(artist, album))
        if not candidates:
            raise LookupError(f"no release found in MusicBrainz for '{artist}' - '{album}'")
        release_id = candidates[0]["id"]
        print(f"Release: {describe_candidate(candidates[0])}")
        if len(candidates) > 1:
            print("  other matches (use --release-id to pick one):")
            for c in candidates[1:4]:
                print(f"    {describe_candidate(c)}")
    return parse_release(fetch_release(release_id))


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Split an album audio file into tagged per-track files.",
        epilog='Example: python split_album.py album.m4a --artist "Pink Floyd" '
               '--album "Wish You Were Here"')
    ap.add_argument("input", help="audio file with the whole album (.m4a, .mp3, ...)")
    ap.add_argument("--artist", help="album artist")
    ap.add_argument("--album", help="album title")
    ap.add_argument("--release-id", help="MusicBrainz release ID (skips the search)")
    ap.add_argument("-o", "--output", help="output folder "
                    "(default: <input folder>/<Artist>/<Year> - <Album>)")
    ap.add_argument("--dry-run", action="store_true",
                    help="show the release and cut points, write nothing")
    args = ap.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):        # titles may hold any character
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    if not os.path.isfile(args.input):
        sys.exit(f"error: input file not found: {args.input}")
    if not args.release_id and not (args.artist and args.album):
        sys.exit("error: give --artist and --album, or --release-id")

    ffmpeg = find_ffmpeg()
    duration, codec = probe_audio(ffmpeg, args.input)
    ext, copy = output_format(codec)

    try:
        release = lookup_release(args.artist, args.album, args.release_id)
    except (LookupError, RuntimeError, ValueError) as exc:
        sys.exit(f"error: {exc}")

    print(f"Album : {release.artist} - {release.title}"
          + (f" ({release.year})" if release.year else "")
          + f", {len(release.tracks)} tracks")
    print(f"Input : {args.input} ({fmt(duration)}, {codec})")

    diff = duration_mismatch(release, duration)
    if diff is not None:
        print(f"warning: the file is {fmt(abs(diff))} {'longer' if diff > 0 else 'shorter'} "
              f"than the album in MusicBrainz ({fmt(release.total_length())}); "
              "cut points may be off. Check the result or try another --release-id.",
              file=sys.stderr)

    try:
        segments = compute_segments(release.tracks, duration)
    except ValueError as exc:
        sys.exit(f"error: {exc}")

    multi = release.disc_count > 1
    filenames = [track_filename(t, multi, ext) for t in release.tracks]
    rows = build_rows(release, segments, filenames)
    outdir = args.output or default_output_dir(args.input, release)
    print(f"Output: {outdir}" + ("" if copy else f"  (re-encoding {codec} to AAC)"))
    print()
    print(console_table(rows))

    if args.dry_run:
        print("\nDry run: nothing written.")
        return

    os.makedirs(outdir, exist_ok=True)
    print()
    for i, (track, (start, end), name) in enumerate(zip(release.tracks, segments, filenames), 1):
        dst = os.path.join(outdir, name)
        print(f"[{i}/{len(filenames)}] {name}")
        try:
            cut_track(ffmpeg, args.input, dst, start, end, copy)
        except subprocess.CalledProcessError:
            sys.exit(f"error: ffmpeg failed on track {i}")
        tag_file(dst, release, track)

    report = os.path.join(outdir, "tracks.md")
    with open(report, "w", encoding="utf-8") as f:
        f.write(markdown_report(release, rows, args.input, duration))
    print(f"\nDone. Overview: {report}")


if __name__ == "__main__":
    main()
