#!/usr/bin/env python3
r"""
split_album.py - split one audio file holding a whole album (.m4a / .mp3) into
one file per track, with the right metadata on each file.

Usage:
    python split_album.py INPUT [--artist ARTIST] [--album ALBUM] [--release-id MBID]
                          [--tracklist FILE] [-o DIR] [--order auto|album|LIST]
                          [--acoustid-key KEY] [--no-snap] [--snap-window S]
                          [--silence-db DB] [--no-cover] [--cover-size N]
                          [--format md,csv,json] [--force] [--dry-run]

python split_album.py "C:\Music\Pink Floyd - Wish You Were Here.m4a"
python split_album.py album.m4a --artist "Pink Floyd" --album "Wish You Were Here"
python split_album.py album.m4a --tracklist album.cue

How it works:
    0. Which album: --artist/--album, else the file's tags (album artist or
       artist + album), else a title tag or file name like
       "Artist - Album (1975) [Full Album]". --release-id names a MusicBrainz
       release directly. --tracklist uses a local file instead of MusicBrainz:
         .cue   cue sheet: cut exactly at the INDEX 01 times
         .txt   one track per line, with a start time before the title
                ("0:00 Title", as in many video descriptions) or a length after
                it ("Title 3:45"); optional "Artist: / Album: / Year: / Genre:"
                header lines
         .json  {"artist", "album", "year", "genre", "tracks": [{"title",
                "length" or "start", "artist", "disc"}]}
       Tracklists give no MusicBrainz IDs and no cover art.
    1. The album is looked up in MusicBrainz (free, no API key). Of all
       editions (releases) of the album, the one whose total length is closest
       to the duration of the file is used; ties go to the official, earliest
       edition. The runners-up are printed so you can force one with
       --release-id. When even the best edition differs more than 5% in
       length from the file, the script stops (--force splits anyway).
    2. Cut points: when the file and the edition differ in length by at most
       3%, the track lengths are scaled to fit. Each cut point is then moved to
       the middle of the nearest silence within --snap-window seconds (default
       5). Where no silence is found (gapless or live albums) the point from
       the metadata is kept. Each cut is placed relative to the previous one,
       so a gap between tracks does not shift all later cuts. The last track
       runs to the end of the file. --no-snap cuts purely at the track lengths
       from MusicBrainz (no scaling, no silence detection).
       Track order: when fewer than 3 in 4 cuts hit a silence, the file may hold
       the tracks in another order than the album. The script then splits the
       file at silences into pieces whose lengths match the tracks (in any
       order) and assigns each piece to the track of that length. Tracks of
       almost the same length (within 2 s) cannot be told apart reliably: they
       get the closest length match and a warning names them. With an AcoustID API key
       (--acoustid-key or env ACOUSTID_API_KEY; free at
       https://acoustid.org/new-application) each piece is also recognised by
       its audio fingerprint, which settles such cases. --order 3,1,2 gives the
       order in the file yourself (disc-track for multi-disc albums: 2-1,1-1),
       --order album turns detection off. Files are always named and tagged in
       album order; the overview shows each track's position in the file.
    3. Cutting uses stream copy (no re-encoding, no quality loss) for AAC and
       MP3 input. Other codecs are re-encoded to AAC (.m4a, 256 kbit/s).
    4. Each track gets tags: title, artist, album artist, album, track and disc
       number, year, genre, the MusicBrainz IDs, a compilation flag for
       various-artists albums, and the front cover from the Cover Art Archive
       (also saved as cover.jpg; --no-cover to skip, --cover-size to choose
       250, 500 (default), 1200 or full).
       Multi-disc albums are expected as one file with all discs in order;
       video discs (DVD, Blu-ray) of a release are ignored.
    5. An overview (number, title, artist for compilations, length, difference
       with the length in the metadata, how the end of the track was found,
       file name, start, and position in the file when the order differs) is
       printed and written next to the tracks as tracks.md; --format
       md,csv,json picks the file format(s).

Output: <input folder>/<Artist>/<Year> - <Album>/NN - Title.ext
        (D-NN - Title.ext for multi-disc albums). Use -o to choose the folder.
        Existing files with the same name are overwritten.

--dry-run shows the chosen release and the computed cut points without
writing anything.

Requires ffmpeg (see cut_video.py) and mutagen (pip install mutagen).
"""

import argparse
import http.client
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

# Stop (unless --force) when file and album differ more than this fraction.
MAX_MISMATCH_FRAC = 0.05
# Scale the track lengths to the file when they differ at most this fraction.
SCALE_MAX_FRAC = 0.03
# Release groups (from the search results) whose editions are compared.
MAX_RELEASE_GROUPS = 5
# Silence detection: search this far (seconds) around each expected cut point,
# for stretches quieter than SILENCE_DB lasting at least SILENCE_MIN_S.
SNAP_WINDOW_S = 5.0
SILENCE_DB = -40.0
SILENCE_MIN_S = 0.3
# A snapped cut never makes a track shorter than this (seconds).
MIN_TRACK_S = 1.0
# Max pages (each up to 100 releases) fetched when comparing editions.
MAX_BROWSE_PAGES = 5

CAA_API = "https://coverartarchive.org"
COVER_SIZES = ("250", "500", "1200", "full")
VARIOUS_ARTISTS_ID = "89ad4ac3-39f7-470e-963a-56509c546377"
# Media with one of these words in their format hold video, not album audio.
VIDEO_FORMAT_WORDS = ("dvd", "blu-ray", "vhs", "vcd", "laserdisc", "umd", "video")
REPORT_FORMATS = ("md", "csv", "json")


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
    compilation: bool = False    # various artists
    source: str = "MusicBrainz"  # where the track data came from
    starts: list[float] | None = None    # start times from a tracklist
    exact_starts: bool = False   # starts are exact (cue sheet): cut there, no snapping

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


# A timeout while reading a response is a bare TimeoutError, not a URLError, and a
# dropped connection is an http.client exception; neither may crash the program.
NETWORK_ERRORS = (OSError, http.client.HTTPException)


def net_reason(exc: Exception) -> str:
    """Short description of a network failure."""
    return str(getattr(exc, "reason", None) or exc) or type(exc).__name__


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
        except NETWORK_ERRORS as exc:
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            raise RuntimeError(f"cannot reach MusicBrainz: {net_reason(exc)}") from exc
    raise RuntimeError("MusicBrainz keeps refusing requests (rate limit); try again later")


def _lucene(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def search_releases(artist: str, album: str) -> list[dict]:
    query = f'release:"{_lucene(album)}" AND artist:"{_lucene(artist)}"'
    return mb_get("release/", {"query": query, "limit": 25}).get("releases", [])


# Loose search: minimum similarity (0..1) of title and artist with what was asked.
FUZZY_MIN_TITLE = 0.75
FUZZY_MIN_ARTIST = 0.75
_LUCENE_SPECIAL = re.compile(r'([+\-&|!(){}\[\]^"~*?:\\/])')


def similarity(a: str, b: str) -> float:
    from difflib import SequenceMatcher
    norm = lambda s: re.sub(r"[\W_]+", " ", s.casefold().replace("’", "'")).strip()
    return SequenceMatcher(None, norm(a), norm(b)).ratio()


def search_releases_fuzzy(artist: str, album: str) -> list[dict]:
    """Search on the separate words, keep releases whose title and artist look alike.

    For tags that differ a little from MusicBrainz ('Songs from ...' vs 'Songs of ...').
    Best match first.
    """
    words = lambda s: " ".join(_LUCENE_SPECIAL.sub(r"\\\1", w) for w in s.split())
    query = f"release:({words(album)}) AND artist:({words(artist)})"
    found = mb_get("release/", {"query": query, "limit": 50}).get("releases", [])
    scored = []
    for r in found:
        credit, _ = _credit(r.get("artist-credit", []))
        t, a = similarity(r.get("title", ""), album), similarity(credit, artist)
        if t >= FUZZY_MIN_TITLE and a >= FUZZY_MIN_ARTIST:
            scored.append((round(t, 2), round(a, 2), r))
    best = {}
    for t, a, r in scored:
        best.setdefault((t, a), []).append(r)
    return [r for key in sorted(best, reverse=True) for r in rank_releases(best[key])]


def rank_releases(candidates: list[dict]) -> list[dict]:
    """Best first: search score, then official, then album, then earliest date."""
    def key(r):
        rg = r.get("release-group", {})
        return (-int(r.get("score", 0)),
                r.get("status") != "Official",
                rg.get("primary-type") != "Album",
                r.get("date") or "9999")
    return sorted(candidates, key=key)


def _track_count(r: dict):
    if "track-count" in r:
        return r["track-count"]
    media = r.get("media", [])
    return sum(m.get("track-count", len(m.get("tracks", []))) for m in media) if media else "?"


def describe_candidate(r: dict, duration: float | None = None) -> str:
    parts = [r.get("date") or "????", r.get("country") or "",
             "+".join(m.get("format") or "?" for m in r.get("media", [])),
             f"{_track_count(r)} tracks"]
    if r.get("disambiguation"):
        parts.append(r["disambiguation"])
    length = release_length(r)
    if length is not None:
        parts.append(f"length {fmt(length)}")
        if duration is not None:
            parts.append(f"file {fmt_delta(duration - length)}")
    return f"{r['id']}  " + ", ".join(p for p in parts if p)


def browse_group_releases(release_group_id: str) -> list[dict]:
    """All releases of a release group, with their tracks (paged)."""
    releases, offset = [], 0
    for _ in range(MAX_BROWSE_PAGES):
        page = mb_get("release", {"release-group": release_group_id,
                                  "inc": "recordings", "limit": 100, "offset": offset})
        batch = page.get("releases", [])
        releases += batch
        offset += len(batch)
        if not batch or offset >= page.get("release-count", 0):
            break
    return releases


def is_audio_medium(medium: dict) -> bool:
    fmt_name = (medium.get("format") or "").lower()
    return not any(word in fmt_name for word in VIDEO_FORMAT_WORDS)


def audio_media(r: dict) -> list[dict]:
    """The audio discs of a release (no DVD / Blu-ray), in disc order."""
    return sorted((m for m in r.get("media", []) if is_audio_medium(m)),
                  key=lambda m: m.get("position", 0))


def release_length(r: dict) -> float | None:
    """Total audio length (seconds) of a release from a browse result, if every track has one."""
    lengths = [t.get("length") for m in audio_media(r) for t in m.get("tracks", [])]
    if not lengths or any(not l for l in lengths):
        return None
    return sum(lengths) / 1000


def rank_by_duration(releases: list[dict], duration: float) -> list[dict]:
    """Releases with a known length, best match with the file duration first.

    Differences are compared in whole seconds; ties go to the most common
    track count (filters odd editions), then official, then earliest.
    """
    known = [(r, release_length(r)) for r in releases]
    known = [(r, l) for r, l in known if l is not None]
    if not known:
        return []
    counts = [_track_count(r) for r, _ in known]
    common = max(set(counts), key=counts.count)

    def key(item):
        r, length = item
        return (round(abs(duration - length)), _track_count(r) != common,
                r.get("status") != "Official", r.get("date") or "9999")
    return [r for r, _ in sorted(known, key=key)]


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
    # discs are numbered 1..n over the audio discs only (a DVD in a box set is skipped)
    for disc, medium in enumerate(audio_media(data), 1):
        for t in medium.get("tracks", []):
            rec = t.get("recording", {})
            length_ms = t.get("length") or rec.get("length")
            t_artist, t_ids = _credit(t.get("artist-credit") or rec.get("artist-credit") or [])
            tracks.append(Track(
                disc=disc,
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
        genre=genre, release_group_id=rg.get("id", ""), tracks=tracks,
        compilation=(VARIOUS_ARTISTS_ID in artist_ids
                     or ("Compilation" in rg.get("secondary-types", [])
                         and len({t.artist for t in tracks}) > 1)))


# --------------------------------------------------------------------------- #
# Cover art (Cover Art Archive)
# --------------------------------------------------------------------------- #

HTTP_ATTEMPTS = 2
HTTP_RETRY_WAIT_S = 2


def http_get_bytes(url: str) -> bytes | None:
    """GET a URL (following redirects); None on 404. Retries once on a temporary failure."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(HTTP_ATTEMPTS):
        retry = attempt < HTTP_ATTEMPTS - 1
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            if exc.code < 500 or not retry:
                raise RuntimeError(f"download failed ({exc.code}): {url}") from exc
        except NETWORK_ERRORS as exc:
            if not retry:
                raise RuntimeError(f"cannot reach {url}: {net_reason(exc)}") from exc
        time.sleep(HTTP_RETRY_WAIT_S)
    return None


def image_type(data: bytes) -> str | None:
    """'jpeg' or 'png' from the file signature, else None."""
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    return None


def fetch_cover(release: Release, size: str = "500") -> bytes | None:
    """Front cover of the release, else of the release group; None when there is none."""
    suffix = "front" if size == "full" else f"front-{size}"
    urls = [f"{CAA_API}/release/{release.id}/{suffix}"]
    if release.release_group_id:
        urls.append(f"{CAA_API}/release-group/{release.release_group_id}/{suffix}")
    error = None
    for url in urls:
        try:
            data = http_get_bytes(url)
        except RuntimeError as exc:        # the next URL may still work
            error = exc
            continue
        if data and image_type(data):
            return data
    if error:
        raise error
    return None


# --------------------------------------------------------------------------- #
# Cut points
# --------------------------------------------------------------------------- #

def fmt_delta(seconds: float) -> str:
    """Signed difference: '+0:03', '-1:10', '0:00'."""
    sign = "+" if seconds >= 0.5 else "-" if seconds <= -0.5 else ""
    return sign + fmt(abs(seconds))


def scale_factor(release: Release, duration: float) -> float:
    """duration / album length when they differ at most SCALE_MAX_FRAC, else 1."""
    total = release.total_length()
    if not total or abs(duration - total) > SCALE_MAX_FRAC * total:
        return 1.0
    return duration / total


def parse_silences(ffmpeg_log: str, duration: float) -> list[tuple[float, float]]:
    """(start, end) of each silence reported by ffmpeg's silencedetect filter."""
    silences, start = [], None
    for line in ffmpeg_log.splitlines():
        m = re.search(r"silence_start:\s*(-?[\d.]+)", line)
        if m:
            start = max(0.0, float(m.group(1)))
            continue
        m = re.search(r"silence_end:\s*([\d.]+)", line)
        if m and start is not None:
            silences.append((start, float(m.group(1))))
            start = None
    if start is not None:                       # silent until the end of the file
        silences.append((start, duration))
    return silences


def detect_silences(ffmpeg: str, path: str, duration: float,
                    noise_db: float = SILENCE_DB) -> list[tuple[float, float]]:
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-nostats", "-i", path, "-map", "0:a:0", "-vn",
         "-af", f"silencedetect=noise={noise_db}dB:d={SILENCE_MIN_S}", "-f", "null", "-"],
        capture_output=True, text=True, errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"silence detection failed:\n{result.stderr[-2000:]}")
    return parse_silences(result.stderr, duration)


def snap_point(expected: float, silences, window: float, lo: float, hi: float):
    """Middle of the silence nearest to `expected` (within `window`), kept inside (lo, hi).

    Returns the point, or None when no usable silence is near.
    """
    best = None
    for s, e in silences:
        a, b = max(s, expected - window), min(e, expected + window)
        if a > b:
            continue                            # silence outside the window
        point = (a + b) / 2
        if not lo < point < hi:
            continue
        dist = 0.0 if s <= expected <= e else min(abs(s - expected), abs(e - expected))
        if best is None or dist < best[0]:
            best = (dist, point)
    return None if best is None else best[1]


def plan_cuts(tracks: list[Track], duration: float, silences=(),
              window: float = SNAP_WINDOW_S, scale: float = 1.0):
    """Cut the file into tracks.

    Returns (segments, methods): (start, end) per track, and per track how its
    end was found: 'silence', 'scaled', 'metadata' or 'end' (last track).
    Each expected end is the previous cut plus the (scaled) track length.
    """
    segments, methods, pos = [], [], 0.0
    for i, t in enumerate(tracks):
        if pos >= duration:
            raise ValueError(f"the file ({fmt(duration)}) ends before track {i + 1} "
                             f"'{t.title}' starts ({fmt(pos)}); wrong album or release?")
        if i == len(tracks) - 1:
            end, method = duration, "end"
        elif t.length is None:
            raise ValueError(f"MusicBrainz has no length for track {i + 1} '{t.title}'; "
                             "try another release with --release-id")
        else:
            expected = min(pos + t.length * scale, duration)
            snapped = snap_point(expected, silences, window,
                                 pos + MIN_TRACK_S, duration - MIN_TRACK_S)
            if snapped is not None:
                end, method = snapped, "silence"
            else:
                end, method = expected, "scaled" if scale != 1.0 else "metadata"
        segments.append((pos, end))
        methods.append(method)
        pos = end
    return segments, methods


def compute_segments(tracks: list[Track], duration: float) -> list[tuple[float, float]]:
    """(start, end) per track from the plain cumulative lengths (no snapping)."""
    return plan_cuts(tracks, duration)[0]


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

def check_mutagen():
    """Stop early, with the exact install command, when mutagen is missing."""
    try:
        import mutagen  # noqa: F401
    except ImportError:
        exe = f'"{sys.executable}"' if " " in sys.executable else sys.executable
        sys.exit("error: the 'mutagen' package (needed to write the tags) is not installed "
                 f"for this Python ({sys.executable}).\nInstall it with:\n\n"
                 f"    {exe} -m pip install mutagen\n")


def tag_file(path: str, release: Release, track: Track, cover: bytes | None = None):
    if path.lower().endswith(".mp3"):
        _tag_mp3(path, release, track, cover)
    else:
        _tag_mp4(path, release, track, cover)


def _mb_ids(release: Release, track: Track) -> dict[str, str]:
    ids = {
        "MusicBrainz Album Id": release.id,
        "MusicBrainz Release Group Id": release.release_group_id,
        "MusicBrainz Album Artist Id": "/".join(release.artist_ids),
        "MusicBrainz Artist Id": "/".join(track.artist_ids),
        "MusicBrainz Release Track Id": track.track_id,
    }
    return {k: v for k, v in ids.items() if v}


def _tag_mp4(path: str, release: Release, track: Track, cover: bytes | None):
    from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm

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
    if release.compilation:
        tags["cpil"] = True
    if cover and image_type(cover):
        kind = MP4Cover.FORMAT_PNG if image_type(cover) == "png" else MP4Cover.FORMAT_JPEG
        tags["covr"] = [MP4Cover(cover, imageformat=kind)]
    audio.save()


def _tag_mp3(path: str, release: Release, track: Track, cover: bytes | None):
    from mutagen.id3 import (APIC, ID3, ID3NoHeaderError, TALB, TCMP, TCON, TDRC, TIT2,
                             TPE1, TPE2, TPOS, TRCK, TXXX, UFID)

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
    if release.compilation:
        tags.add(TCMP(encoding=3, text="1"))
    if cover and image_type(cover):
        tags.add(APIC(encoding=3, mime=f"image/{image_type(cover)}", type=3,
                      desc="Cover", data=cover))
    tags.save(path, v2_version=4)


# --------------------------------------------------------------------------- #
# Overview
# --------------------------------------------------------------------------- #

def track_label(track: Track, multi_disc: bool) -> str:
    return f"{track.disc}-{track.number}" if multi_disc else str(track.number)


def build_rows(release: Release, segments, filenames, methods=None,
               positions=None) -> list[dict]:
    """One row per track (album order): display strings plus raw values for csv/json.

    `positions` is the 1-based position of each track in the file.
    """
    multi = release.disc_count > 1
    methods = methods or ["metadata"] * len(segments)
    positions = positions or list(range(1, len(segments) + 1))
    rows = []
    for t, (s, e), f, m, pos in zip(release.tracks, segments, filenames, methods, positions):
        if t.length is None:
            delta, delta_s = "", None
        else:
            delta_s = round((e - s) - t.length, 3)
            d = round(delta_s, 1)
            delta = "0.0s" if d == 0 else f"{d:+.1f}s"
        rows.append({"nr": track_label(t, multi), "title": t.title, "artist": t.artist,
                     "length": fmt(e - s), "delta": delta, "cut": m, "file": f,
                     "start": fmt(s), "pos": str(pos), "position": pos,
                     # raw values
                     "track": t, "start_s": round(float(s), 3), "end_s": round(float(e), 3),
                     "delta_s": delta_s})
    return rows


# delta: length in the file minus the length in MusicBrainz
# cut:   how the end of the track was found
COLUMNS = [("nr", "#"), ("title", "Title"), ("length", "Length"), ("delta", "Δ"),
           ("cut", "Cut"), ("file", "File"), ("start", "Start")]


def columns_for(release: Release, reordered: bool = False) -> list[tuple[str, str]]:
    """COLUMNS, plus Artist when tracks have different artists, and the position in
    the file when the file holds the tracks in another order."""
    cols = list(COLUMNS)
    if release.compilation or any(t.artist != release.artist for t in release.tracks):
        cols.insert(2, ("artist", "Artist"))
    if reordered:
        cols.append(("pos", "In file"))
    return cols


def console_table(rows: list[dict], columns=COLUMNS) -> str:
    widths = {k: max(len(h), *(len(r[k]) for r in rows)) for k, h in columns}
    line = lambda vals: "  ".join(v.ljust(widths[k]) for (k, _), v in zip(columns, vals))
    out = [line([h for _, h in columns]), line(["-" * widths[k] for k, _ in columns])]
    out += [line([r[k] for k, _ in columns]) for r in rows]
    return "\n".join(out)


def markdown_report(release: Release, rows: list[dict], source: str, duration: float,
                    columns=None, cover_file: str | None = None) -> str:
    columns = columns or COLUMNS
    esc = lambda s: s.replace("|", "\\|")
    lines = [f"# {release.artist} - {release.title}", ""]
    if cover_file:
        lines += [f"![cover]({urllib.parse.quote(cover_file)})", ""]
    if release.date:
        lines.append(f"- Year: {release.year}")
    if release.genre:
        lines.append(f"- Genre: {release.genre}")
    discs = f" on {release.disc_count} discs" if release.disc_count > 1 else ""
    lines += [f"- Tracks: {len(rows)}{discs}, total {fmt(duration)}"]
    if release.compilation:
        lines.append("- Compilation (various artists)")
    lines.append(f"- Source: `{os.path.basename(source)}`")
    if release.id:
        lines.append(f"- MusicBrainz release: https://musicbrainz.org/release/{release.id}")
    else:
        lines.append(f"- Track data: {release.source}")
    if any(r["position"] != i for i, r in enumerate(rows, 1)):
        lines.append("- The file holds the tracks in another order (see In file)")
    lines += ["",
              f"Δ: length in the file minus the length in {release.source}. "
              "Cut: how the end of the track was found (silence = moved to the nearest "
              "silence, scaled = track lengths scaled to the file, metadata = track "
              f"length from {release.source}, cue = cue sheet, end = end of the file).",
              "",
              "| " + " | ".join(h for _, h in columns) + " |",
              "|" + "|".join("---" for _ in columns) + "|"]
    lines += ["| " + " | ".join(esc(r[k]) for k, _ in columns) + " |" for r in rows]
    return "\n".join(lines) + "\n"


CSV_FIELDS = ["disc", "number", "title", "artist", "length", "length_seconds", "start",
              "start_seconds", "metadata_length_seconds", "delta_seconds", "cut", "file",
              "file_position"]


def track_record(row: dict) -> dict:
    t = row["track"]
    return {"disc": t.disc, "number": t.number, "title": t.title, "artist": t.artist,
            "length": row["length"], "length_seconds": round(row["end_s"] - row["start_s"], 3),
            "start": row["start"], "start_seconds": row["start_s"],
            "end_seconds": row["end_s"], "metadata_length_seconds": t.length,
            "delta_seconds": row["delta_s"], "cut": row["cut"], "file": row["file"],
            "file_position": row["position"],
            "musicbrainz_recording_id": t.recording_id or None,
            "musicbrainz_track_id": t.track_id or None}


def csv_report(rows: list[dict]) -> str:
    import csv
    import io
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_FIELDS, extrasaction="ignore",
                            lineterminator="\n")
    writer.writeheader()
    for row in rows:
        rec = track_record(row)
        writer.writerow({k: "" if rec[k] is None else rec[k] for k in CSV_FIELDS})
    return buf.getvalue()


def json_report(release: Release, rows: list[dict], source: str, duration: float,
                cover_file: str | None = None) -> str:
    data = {
        "album": {
            "artist": release.artist, "title": release.title, "date": release.date or None,
            "year": release.year or None, "genre": release.genre or None,
            "compilation": release.compilation, "discs": release.disc_count,
            "tracks": len(rows), "duration_seconds": round(duration, 3),
            "source": os.path.basename(source), "cover": cover_file,
            "metadata_source": release.source,
            "musicbrainz_release_id": release.id or None,
            "musicbrainz_release_group_id": release.release_group_id or None,
        },
        "tracks": [track_record(r) for r in rows],
    }
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def write_reports(outdir: str, formats: list[str], release: Release, rows: list[dict],
                  source: str, duration: float, cover_file: str | None) -> list[str]:
    columns = columns_for(release, any(r["position"] != i for i, r in enumerate(rows, 1)))
    content = {
        "md": lambda: markdown_report(release, rows, source, duration, columns, cover_file),
        "csv": lambda: csv_report(rows),
        "json": lambda: json_report(release, rows, source, duration, cover_file),
    }
    paths = []
    for f in formats:
        path = os.path.join(outdir, f"tracks.{f}")
        # BOM for csv so Excel reads it as UTF-8
        with open(path, "w", encoding="utf-8-sig" if f == "csv" else "utf-8",
                  newline="") as fh:
            fh.write(content[f]())
        paths.append(path)
    return paths


def parse_formats(text: str) -> list[str]:
    formats = [f.strip().lower() for f in text.split(",") if f.strip()]
    bad = [f for f in formats if f not in REPORT_FORMATS]
    if bad or not formats:
        raise argparse.ArgumentTypeError(
            f"unknown format {', '.join(bad) or '(none)'}; choose from {', '.join(REPORT_FORMATS)}")
    return list(dict.fromkeys(formats))


# --------------------------------------------------------------------------- #
# Artist / album from the file itself
# --------------------------------------------------------------------------- #

def probe_tags(ffmpeg: str, path: str) -> dict[str, str]:
    """Container-level tags of a file (lower-case keys), from `ffmpeg -i` output."""
    result = subprocess.run([ffmpeg, "-hide_banner", "-i", path],
                            capture_output=True, text=True, errors="replace")
    tags, inside = {}, False
    for line in result.stderr.splitlines():
        if re.match(r"^  Metadata:\s*$", line):
            inside = True
            continue
        if inside:
            m = re.match(r"^    (\S[^:]*?)\s*: (.*)$", line)
            if not m:
                break                       # end of the first (container) block
            tags.setdefault(m.group(1).strip().lower(), m.group(2).strip())
    return tags


# "(Full Album)", "[Official Audio]", "(1975)", "[HQ]", "(Remastered 2011)" ...
_NOISE = re.compile(r"\s*[(\[][^)\]]*(full\s*album|official|audio|lyrics|\bhq\b|\bhd\b|"
                    r"remaster|kbps|flac|\b(?:19|20)\d\d\b)[^)\]]*[)\]]", re.I)


def split_artist_album(text: str) -> tuple[str | None, str | None]:
    """'Pink Floyd - Wish You Were Here (1975) [Full Album]' -> ('Pink Floyd', 'Wish You Were Here')."""
    text = _NOISE.sub("", text.replace("_", " ")).strip()
    m = re.match(r"^(.+?)\s+[-–—]\s+(.+)$", text)
    if not m:
        return None, None
    album = re.sub(r"^(?:19|20)\d\d\s*[-–—]\s*", "", m.group(2)).strip()
    return m.group(1).strip(), album or None


def guess_artist_album(tags: dict, path: str) -> tuple[str | None, str | None, str]:
    """(artist, album, where it came from) from the tags, the title tag or the file name."""
    artist = tags.get("album_artist") or tags.get("artist")
    if artist and tags.get("album"):
        return artist, tags["album"], "tags"
    if tags.get("title"):
        a, b = split_artist_album(tags["title"])
        if a and b:
            return a, b, "title tag"
    a, b = split_artist_album(os.path.splitext(os.path.basename(path))[0])
    if a and b:
        return a, b, "file name"
    return None, None, ""


# --------------------------------------------------------------------------- #
# Tracklist files (offline: .json, .cue, .txt)
# --------------------------------------------------------------------------- #

def parse_time(text) -> float:
    """'3:45' -> 225, '1:02:03.5' -> 3723.5, '225' / 225 -> 225."""
    if isinstance(text, (int, float)):
        return float(text)
    parts = str(text).strip().split(":")
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        raise ValueError(f"invalid time '{text}'") from None
    if not 1 <= len(nums) <= 3 or any(n < 0 for n in nums) or \
            (len(nums) > 1 and any(n >= 60 for n in nums[1:])):
        raise ValueError(f"invalid time '{text}'")
    seconds = 0.0
    for n in nums:
        seconds = seconds * 60 + n
    return seconds


@dataclass
class Tracklist:
    artist: str = ""
    album: str = ""
    date: str = ""
    genre: str = ""
    tracks: list[dict] = field(default_factory=list)   # title, artist, disc, start|length
    exact: bool = False      # start times are exact (cue sheet): no snapping

    @property
    def has_starts(self) -> bool:
        return bool(self.tracks) and all("start" in t for t in self.tracks)


def _read_text(path: str) -> str:
    raw = open(path, "rb").read()
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def parse_cue(text: str) -> Tracklist:
    tl, cur, files = Tracklist(exact=True), None, 0
    unquote = lambda s: s.strip().strip('"')
    for line in text.splitlines():
        words = line.strip().split(None, 1)
        if not words:
            continue
        key, rest = words[0].upper(), (words[1] if len(words) > 1 else "")
        if key == "FILE":
            files += 1
        elif key == "TRACK":
            cur = {"title": "", "artist": ""}
            tl.tracks.append(cur)
        elif key == "TITLE":
            if cur is None:
                tl.album = unquote(rest)
            else:
                cur["title"] = unquote(rest)
        elif key == "PERFORMER":
            if cur is None:
                tl.artist = unquote(rest)
            else:
                cur["artist"] = unquote(rest)
        elif key == "INDEX" and cur is not None:
            num, _, t = rest.partition(" ")
            if num == "01":
                mm, ss, ff = (int(x) for x in t.strip().split(":"))
                cur["start"] = mm * 60 + ss + ff / 75
        elif key == "REM" and cur is None:
            sub, _, val = rest.partition(" ")
            if sub.upper() == "DATE":
                tl.date = unquote(val)
            elif sub.upper() == "GENRE":
                tl.genre = unquote(val)
    if files > 1:
        raise ValueError("cue sheets with more than one FILE are not supported")
    if not tl.tracks or not tl.has_starts:
        raise ValueError("cue sheet without tracks or without INDEX 01 lines")
    return tl


_TIME = r"\d{1,2}(?::\d{2}){1,2}(?:\.\d+)?"
_TRACKNO = r"(?:\d{1,3}\s*[.)\-–]\s*)?"


def parse_txt(text: str) -> Tracklist:
    """Plain text: one track per line, a start time before or a length after the title.

    '0:00 Shine On You Crazy Diamond' / '01. Have a Cigar - 5:08'. Optional header
    lines 'Artist: ...', 'Album: ...', 'Year: ...', 'Genre: ...'.
    """
    tl, kinds = Tracklist(), set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        h = re.match(r"^(artist|album|year|date|genre)\s*:\s*(.+)$", line, re.I)
        if h:
            key = h.group(1).lower()
            setattr(tl, {"year": "date"}.get(key, key), h.group(2).strip())
            continue
        m = re.match(rf"^{_TRACKNO}[(\[]?({_TIME})[)\]]?\s*[-–—.:|]?\s*(.+)$", line)
        if m:
            tl.tracks.append({"title": m.group(2).strip(), "start": parse_time(m.group(1))})
            kinds.add("start")
            continue
        m = re.match(rf"^{_TRACKNO}(.+?)\s*[-–—|(\[]?\s*({_TIME})[)\]]?$", line)
        if m:
            tl.tracks.append({"title": m.group(1).strip(), "length": parse_time(m.group(2))})
            kinds.add("length")
            continue
        raise ValueError(f"cannot read tracklist line: {line!r}")
    if len(kinds) > 1:
        raise ValueError("tracklist mixes start times and lengths; use one of them")
    if not tl.tracks:
        raise ValueError("tracklist has no tracks")
    return tl


def parse_json_tracklist(text: str) -> Tracklist:
    data = json.loads(text)
    tl = Tracklist(artist=data.get("artist", ""), album=data.get("album", ""),
                   date=str(data.get("year") or data.get("date") or ""),
                   genre=data.get("genre", ""))
    for i, t in enumerate(data.get("tracks", []), 1):
        entry = {"title": t.get("title") or f"Track {i}", "artist": t.get("artist", ""),
                 "disc": int(t.get("disc", 1))}
        if "start" in t:
            entry["start"] = parse_time(t["start"])
        elif t.get("length") is not None:
            entry["length"] = parse_time(t["length"])
        elif i != len(data["tracks"]):
            raise ValueError(f"track {i} has no length or start")
        tl.tracks.append(entry)
    if not tl.tracks:
        raise ValueError("tracklist has no tracks")
    if any("start" in t for t in tl.tracks) and not tl.has_starts:
        raise ValueError("give a start time for every track, or lengths only")
    return tl


def read_tracklist(path: str) -> Tracklist:
    text = _read_text(path)
    ext = os.path.splitext(path)[1].lower()
    if ext == ".cue":
        return parse_cue(text)
    if ext == ".json":
        return parse_json_tracklist(text)
    return parse_txt(text)


def release_from_tracklist(tl: Tracklist, artist: str | None, album: str | None) -> Release:
    """A Release (without MusicBrainz IDs) from a tracklist; CLI artist/album win."""
    artist = artist or tl.artist
    album = album or tl.album
    if not (artist and album):
        raise ValueError("the tracklist has no artist/album: give --artist and --album")
    if tl.has_starts:
        starts = [t["start"] for t in tl.tracks]
        if any(b <= a for a, b in zip(starts, starts[1:])):
            raise ValueError("tracklist start times must increase")
        lengths = [b - a for a, b in zip(starts, starts[1:])] + [None]
    else:
        lengths = [t.get("length") for t in tl.tracks]
    tracks, per_disc = [], {}
    for t, length in zip(tl.tracks, lengths):
        disc = t.get("disc", 1)
        per_disc[disc] = per_disc.get(disc, 0) + 1
        tracks.append(Track(disc=disc, number=per_disc[disc], title=t["title"],
                            length=length, artist=t.get("artist") or artist))
    return Release(id="", title=album, artist=artist, artist_ids=[], date=tl.date,
                   genre=tl.genre, release_group_id="", tracks=tracks,
                   compilation=len({t.artist for t in tracks}) > 1,
                   source="tracklist",
                   starts=[t["start"] for t in tl.tracks] if tl.has_starts else None,
                   exact_starts=tl.exact)


# --------------------------------------------------------------------------- #
# Track order in the file
# --------------------------------------------------------------------------- #

# A segment matches a track when their lengths differ at most this much.
ORDER_TOL_S = 4.0
ORDER_TOL_FRAC = 0.015
# Try to detect another order when fewer than this share of cuts hit a silence.
ORDER_CHECK_RATIO = 0.75
# Two tracks count as interchangeable when swapping them changes the total
# length mismatch by less than this (seconds).
AMBIGUOUS_S = 2.0
# Order search: states kept per step. The n-1 longest silences are most likely
# the gaps between tracks; a cut in a shorter silence costs this many seconds
# of length difference per second it is shorter.
ORDER_BEAM = 300
SHORT_SILENCE_WEIGHT = 4.0


def order_tolerance(length: float) -> float:
    return max(ORDER_TOL_S, ORDER_TOL_FRAC * length)


def parse_order(text: str, tracks: list[Track]) -> list[int]:
    """'3,1,2' or '2-1,1-1,...' (disc-track) -> track indexes in file order."""
    multi = len({t.disc for t in tracks}) > 1
    by_label = {track_label(t, True): i for i, t in enumerate(tracks)}
    order = []
    for item in (x.strip() for x in text.split(",") if x.strip()):
        if re.fullmatch(r"\d+-\d+", item) and item in by_label:
            order.append(by_label[item])
        elif item.isdigit() and 1 <= int(item) <= len(tracks):
            order.append(int(item) - 1)
        else:
            raise ValueError(f"unknown track '{item}' in --order"
                             + (" (use disc-track, e.g. 2-1, or 1..N)" if multi else ""))
    if sorted(order) != list(range(len(tracks))):
        raise ValueError(f"--order must name each of the {len(tracks)} tracks exactly once")
    return order


@dataclass
class OrderResult:
    order: list[int]                         # track index per file position
    segments: list[tuple[float, float]]      # in file order
    ambiguous: list[tuple[int, int]]         # track index pairs that could be swapped
    identified: int = 0                      # segments placed by AcoustID


def segment_by_lengths(lengths: list[float], duration: float, silences,
                       beam: int = ORDER_BEAM):
    """Split the file at silences into one piece per track, each track used once.

    Beam search: step by step, the next piece is one of the unused tracks, ending
    at a silence near (previous cut + track length). Cost = sum of length
    differences, plus a penalty for cutting in a silence shorter than the n-1
    longest ones (those are most likely the gaps between tracks).
    Returns (cuts, order) - cut points and the track index per piece - or None.
    """
    import bisect
    n = len(lengths)
    cand = sorted({(round((s + e) / 2, 3), e - s) for s, e in silences
                   if s > MIN_TRACK_S and e < duration - MIN_TRACK_S})
    points = [p for p, _ in cand]
    longest = sorted((d for _, d in cand), reverse=True)
    ref = longest[min(n - 2, len(longest) - 1)] if longest else 0.0
    penalty = [SHORT_SILENCE_WEIGHT * max(0.0, ref - d) for _, d in cand]
    full = (1 << n) - 1
    # state key: (index of the last cut point, -1 = file start; set of used tracks)
    # value: (cost, path) with path a linked tuple (previous path, track, point index)
    states = {(-1, 0): (0.0, None)}
    for _ in range(n - 1):
        nxt = {}
        for (k, used), (cost, path) in states.items():
            start = points[k] if k >= 0 else 0.0
            for i in range(n):
                if used >> i & 1:
                    continue
                target, tol = start + lengths[i], order_tolerance(lengths[i])
                for j in range(bisect.bisect_left(points, target - tol),
                               bisect.bisect_right(points, target + tol)):
                    c = cost + abs(points[j] - target) + penalty[j]
                    key = (j, used | 1 << i)
                    if key not in nxt or c < nxt[key][0]:
                        nxt[key] = (c, (path, i, j))
        if not nxt:
            return None
        states = dict(sorted(nxt.items(), key=lambda kv: kv[1][0])[:beam])

    best = None
    for (k, used), (cost, path) in states.items():
        i = (full & ~used).bit_length() - 1           # the one track left
        d = duration - (points[k] if k >= 0 else 0.0)
        if abs(d - lengths[i]) <= order_tolerance(lengths[i]):
            c = cost + abs(d - lengths[i])
            if best is None or c < best[0]:
                best = (c, path, i)
    if best is None:
        return None
    _, path, last = best
    order, cuts = [last], []
    while path is not None:
        path, i, j = path
        order.append(i)
        cuts.append(points[j])
    return cuts[::-1], order[::-1]


def assign_segments(seg_lengths: list[float], lengths: list[float],
                    fixed: dict[int, int] | None = None):
    """Match segments to tracks by length (optimal in 1-D: sort both).

    `fixed` maps segment -> track for segments already identified otherwise.
    Returns (order, ambiguous pairs) or None when a segment fits no track.
    """
    fixed = fixed or {}
    free_s = sorted((l, p) for p, l in enumerate(seg_lengths) if p not in fixed)
    free_t = sorted((l, i) for i, l in enumerate(lengths) if i not in fixed.values())
    order = [None] * len(seg_lengths)
    for p, i in fixed.items():
        order[p] = i
    for (sl, p), (tl, i) in zip(free_s, free_t):
        order[p] = i
    cost = lambda p, i: abs(seg_lengths[p] - lengths[i])
    fits = lambda p, i: cost(p, i) <= order_tolerance(lengths[i])
    if not all(fits(p, i) for p, i in enumerate(order)):
        return None
    # pairs that would fit (almost) as well swapped: keep the best fit, report them
    ambiguous = set()
    free_pos = [p for p in range(len(order)) if p not in fixed]
    for a in free_pos:
        for b in free_pos:
            i, j = order[a], order[b]
            if a < b and fits(a, j) and fits(b, i) and \
                    cost(a, j) + cost(b, i) - cost(a, i) - cost(b, j) < AMBIGUOUS_S:
                ambiguous.add((min(i, j), max(i, j)))
    return order, sorted(ambiguous)


def detect_order(tracks: list[Track], duration: float, silences, scale: float = 1.0,
                 identify=None) -> OrderResult | None:
    """Find which track sits where in the file, from silences and track lengths.

    `identify(start, end)` may return a recording ID (AcoustID) for a segment.
    """
    if len(tracks) < 2 or any(t.length is None for t in tracks) or not silences:
        return None
    lengths = [t.length * scale for t in tracks]
    found = segment_by_lengths(lengths, duration, silences)
    if found is None:
        return None
    cuts, _ = found
    bounds = [0.0] + cuts + [duration]
    segments = list(zip(bounds, bounds[1:]))
    seg_lengths = [e - s for s, e in segments]

    fixed = {}
    if identify:
        by_recording = {t.recording_id: i for i, t in enumerate(tracks) if t.recording_id}
        for p, (s, e) in enumerate(segments):
            i = by_recording.get(identify(s, e) or "")
            if i is not None and i not in fixed.values() and \
                    abs(seg_lengths[p] - lengths[i]) <= 3 * order_tolerance(lengths[i]):
                fixed[p] = i
    result = assign_segments(seg_lengths, lengths, fixed)
    if result is None:
        return None
    order, ambiguous = result
    return OrderResult(order, segments, ambiguous, len(fixed))


# --------------------------------------------------------------------------- #
# AcoustID (optional, needs an API key: https://acoustid.org/new-application)
# --------------------------------------------------------------------------- #

ACOUSTID_API = "https://api.acoustid.org/v2/lookup"
ACOUSTID_FP_SECONDS = 120
_last_acoustid_call = 0.0


def fingerprint(ffmpeg: str, path: str, start: float, end: float) -> str | None:
    """Chromaprint fingerprint (compressed, base64) of up to 120 s from `start`."""
    end = min(end, start + ACOUSTID_FP_SECONDS)
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", f"{start:.3f}",
         "-to", f"{end:.3f}", "-i", path, "-map", "0:a:0", "-vn",
         "-f", "chromaprint", "-fp_format", "base64", "-"],
        capture_output=True, text=True, errors="replace")
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def acoustid_lookup(key: str, fp: str, duration: float) -> list[tuple[float, str]]:
    """[(score, recording_id)], best first."""
    global _last_acoustid_call
    wait = 0.34 - (time.monotonic() - _last_acoustid_call)      # max 3 requests/s
    if wait > 0:
        time.sleep(wait)
    _last_acoustid_call = time.monotonic()
    body = urllib.parse.urlencode({"client": key, "duration": int(round(duration)),
                                   "fingerprint": fp, "meta": "recordingids",
                                   "format": "json"}).encode()
    req = urllib.request.Request(ACOUSTID_API, data=body,
                                 headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as exc:
        try:
            data = json.load(exc)
        except Exception:
            raise RuntimeError(f"AcoustID request failed ({exc.code})") from exc
    except NETWORK_ERRORS as exc:
        raise RuntimeError(f"cannot reach AcoustID: {net_reason(exc)}") from exc
    if data.get("status") != "ok":
        raise RuntimeError("AcoustID: " + data.get("error", {}).get("message", "lookup failed"))
    hits = [(r.get("score", 0.0), rec["id"]) for r in data.get("results", [])
            for rec in r.get("recordings", []) if rec.get("id")]
    return sorted(hits, reverse=True)


def acoustid_identifier(ffmpeg: str, path: str, key: str, tracks: list[Track]):
    """identify(start, end) -> recording ID of the release that AcoustID recognises."""
    wanted = {t.recording_id for t in tracks if t.recording_id}

    def identify(start: float, end: float) -> str | None:
        fp = fingerprint(ffmpeg, path, start, end)
        if not fp:
            return None
        for score, rec in acoustid_lookup(key, fp, end - start):
            if rec in wanted and score >= 0.5:
                return rec
        return None
    return identify


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def choose_release(artist: str, album: str, duration: float | None) -> str:
    """Release ID of the edition that best fits the file; prints the choice."""
    candidates = rank_releases(search_releases(artist, album))
    if not candidates:
        candidates = search_releases_fuzzy(artist, album)
        if candidates:
            credit, _ = _credit(candidates[0].get("artist-credit", []))
            print(f"Note  : no exact match for '{artist}' - '{album}'; using the closest: "
                  f"'{credit}' - '{candidates[0].get('title')}'")
    if not candidates:
        raise LookupError(f"no release found in MusicBrainz for '{artist}' - '{album}'. "
                          "Check the spelling, give --artist/--album yourself, or look the "
                          "album up on musicbrainz.org and use --release-id")

    ranked, how = [], "best search match"
    if duration is not None:
        # all editions of the best-matching release groups (compilation series
        # often have a separate release group per country)
        groups = []
        for c in candidates:
            rg = c.get("release-group", {}).get("id")
            if rg and rg not in groups:
                groups.append(rg)
        pool = [r for rg in groups[:MAX_RELEASE_GROUPS] for r in browse_group_releases(rg)]
        ranked = rank_by_duration(pool, duration)
        how = f"closest in length of {len(pool)} editions"
    if not ranked:
        ranked = candidates

    print(f"Release ({how}): {describe_candidate(ranked[0], duration)}")
    if len(ranked) > 1:
        print("  runners-up (use --release-id to pick one):")
        for c in ranked[1:4]:
            print(f"    {describe_candidate(c, duration)}")
    return ranked[0]["id"]


def source_name(release: Release) -> str:
    """'MusicBrainz' or 'the tracklist', for messages."""
    return release.source if release.id else f"the {release.source}"


def lookup_release(artist: str | None, album: str | None, release_id: str | None,
                   duration: float | None = None) -> Release:
    if not release_id:
        release_id = choose_release(artist, album, duration)
    return parse_release(fetch_release(release_id))


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Split an album audio file into tagged per-track files.",
        epilog='Example: python split_album.py album.m4a --artist "Pink Floyd" '
               '--album "Wish You Were Here"')
    ap.add_argument("input", help="audio file with the whole album (.m4a, .mp3, ...)")
    ap.add_argument("--artist", help="album artist (default: from the tags or file name)")
    ap.add_argument("--album", help="album title (default: from the tags or file name)")
    ap.add_argument("--release-id", help="MusicBrainz release ID (skips the search)")
    ap.add_argument("--tracklist", metavar="FILE",
                    help="use a tracklist file (.cue, .json or .txt) instead of MusicBrainz")
    ap.add_argument("-o", "--output", help="output folder "
                    "(default: <input folder>/<Artist>/<Year> - <Album>)")
    ap.add_argument("--order", default="auto", metavar="auto|album|LIST",
                    help="order of the tracks in the file: auto (default: album order, "
                         "unless the cuts do not line up with silences and another order "
                         "fits), album, or track numbers in file order, e.g. 3,1,2 "
                         "(disc-track for multi-disc albums: 2-1,1-1,...)")
    ap.add_argument("--acoustid-key", default=os.environ.get("ACOUSTID_API_KEY"),
                    metavar="KEY",
                    help="AcoustID API key (default: env ACOUSTID_API_KEY); used to "
                         "recognise tracks when the order has to be detected")
    ap.add_argument("--no-snap", action="store_true",
                    help="cut purely at the track lengths "
                         "(no scaling, no silence detection, no order detection)")
    ap.add_argument("--snap-window", type=float, default=SNAP_WINDOW_S, metavar="S",
                    help=f"search for silence this many seconds around each cut "
                         f"(default {SNAP_WINDOW_S:g})")
    ap.add_argument("--silence-db", type=float, default=SILENCE_DB, metavar="DB",
                    help=f"volume below which audio counts as silence (default {SILENCE_DB:g})")
    ap.add_argument("--no-cover", action="store_true",
                    help="do not download and embed the cover art")
    ap.add_argument("--cover-size", choices=COVER_SIZES, default="500",
                    help="cover size in pixels, or full (default 500)")
    ap.add_argument("--format", type=parse_formats, default=["md"], metavar="LIST",
                    help="overview file format(s), comma separated: md, csv, json "
                         "(default md)")
    ap.add_argument("--force", action="store_true",
                    help=f"split even when file and album differ more than "
                         f"{MAX_MISMATCH_FRAC * 100:g}%% in length")
    ap.add_argument("--dry-run", action="store_true",
                    help="show the release and cut points, write nothing")
    args = ap.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):        # titles may hold any character
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    if not os.path.isfile(args.input):
        sys.exit(f"error: input file not found: {args.input}")
    if not args.dry_run:
        check_mutagen()

    ffmpeg = find_ffmpeg()
    duration, codec = probe_audio(ffmpeg, args.input)
    ext, copy = output_format(codec)

    # ---- what album is it -------------------------------------------------
    artist, album, tracklist = args.artist, args.album, None
    if args.tracklist:
        try:
            tracklist = read_tracklist(args.tracklist)
        except (OSError, ValueError) as exc:
            sys.exit(f"error: tracklist {args.tracklist}: {exc}")
        artist, album = artist or tracklist.artist or None, album or tracklist.album or None
    if not args.release_id and not (artist and album):
        g_artist, g_album, where = guess_artist_album(probe_tags(ffmpeg, args.input),
                                                      args.input)
        artist, album = artist or g_artist, album or g_album
        if not (artist and album):
            sys.exit("error: cannot tell the artist and album from the tags or the file "
                     "name; give --artist and --album, --release-id or --tracklist")
        print(f"Search: {artist} - {album} (from the {where})")

    try:
        if tracklist:
            release = release_from_tracklist(tracklist, artist, album)
        else:
            release = lookup_release(artist, album, args.release_id, duration)
    except (LookupError, RuntimeError, ValueError) as exc:
        sys.exit(f"error: {exc}")

    discs = f" on {release.disc_count} discs" if release.disc_count > 1 else ""
    print(f"Album : {release.artist} - {release.title}"
          + (f" ({release.year})" if release.year else "")
          + f", {len(release.tracks)} tracks{discs}"
          + (", compilation" if release.compilation else "")
          + ("" if release.id else f" [{release.source}]"))
    print(f"Input : {args.input} ({fmt(duration)}, {codec})")

    diff = duration_mismatch(release, duration)
    if diff is not None:
        total = release.total_length()
        msg = (f"the file is {fmt(abs(diff))} {'longer' if diff > 0 else 'shorter'} "
               f"than the album in {source_name(release)} ({fmt(total)})")
        if abs(diff) > MAX_MISMATCH_FRAC * total and not args.force:
            sys.exit(f"error: {msg}; probably the wrong edition or album. Pick another "
                     "with --release-id, or use --force to split anyway.")
        print(f"warning: {msg}; cut points may be off. Check the result or try another "
              "--release-id.", file=sys.stderr)

    # ---- where to cut -----------------------------------------------------
    tracks, n = release.tracks, len(release.tracks)
    identity = list(range(n))
    fixed_order = release.starts is not None       # start times describe the file
    snap = not args.no_snap and not release.exact_starts

    order = identity
    if args.order not in ("auto", "album"):
        if fixed_order:
            print("warning: --order is ignored: the tracklist start times give the order",
                  file=sys.stderr)
        else:
            try:
                order = parse_order(args.order, tracks)
            except ValueError as exc:
                sys.exit(f"error: {exc}")

    scale, silences = 1.0, []
    if snap:
        scale = scale_factor(release, duration)
        if abs(scale - 1.0) >= 0.0001:
            print(f"Scale : track lengths x {scale:.4f} to fit the file")
        print("Detecting silences...", flush=True)
        try:
            silences = detect_silences(ffmpeg, args.input, duration, args.silence_db)
        except RuntimeError as exc:
            sys.exit(f"error: {exc}")

    def plan(file_order):
        return plan_cuts([tracks[i] for i in file_order], duration, silences,
                         args.snap_window, scale)

    try:
        if release.exact_starts:
            starts = release.starts
            if starts[-1] >= duration:
                sys.exit(f"error: the cue sheet's last track starts ({fmt(starts[-1])}) "
                         f"after the end of the file ({fmt(duration)})")
            seg_f = list(zip(starts, starts[1:] + [duration]))
            meth_f = ["cue"] * (n - 1) + ["end"]
        else:
            seg_f, meth_f = plan(order)
    except ValueError as exc:
        sys.exit(f"error: {exc}")

    if snap and n > 1:
        print(f"Cuts  : {meth_f.count('silence')} of {n - 1} moved to a silence "
              f"({len(silences)} silences found)")
        if not silences:
            print("note: no silences in the file (gapless or live album?); cutting at the "
                  f"track lengths from {source_name(release)}. Try --silence-db -30 if the "
                  "gaps between tracks hold some noise.", file=sys.stderr)

    if args.order == "auto" and snap and silences and not fixed_order and n >= 3 \
            and meth_f.count("silence") < ORDER_CHECK_RATIO * (n - 1):
        print("Order : checking whether the file holds the tracks in another order...")
        identify = None
        if args.acoustid_key:
            identify = acoustid_identifier(ffmpeg, args.input, args.acoustid_key, tracks)
        try:
            found = detect_order(tracks, duration, silences, scale, identify)
        except RuntimeError as exc:
            print(f"warning: {exc}; ordering by track lengths only", file=sys.stderr)
            found = detect_order(tracks, duration, silences, scale)
        multi = release.disc_count > 1
        if found and found.order != identity:
            order = found.order
            seg_f, meth_f = plan(order)
            print("Order : the file holds the tracks in this order: "
                  + ", ".join(track_label(tracks[i], multi) for i in order))
            print(f"Cuts  : {meth_f.count('silence')} of {n - 1} moved to a silence")
            if found.identified:
                print(f"        {found.identified} of {n} tracks recognised by AcoustID")
            for a, b in found.ambiguous:
                print(f"warning: tracks {track_label(tracks[a], multi)} and "
                      f"{track_label(tracks[b], multi)} have about the same length and may "
                      "be swapped; listen to them, or use --order"
                      + ("" if args.acoustid_key else " or an AcoustID key"),
                      file=sys.stderr)
        elif found:
            print("Order : album order")
        else:
            print("warning: the cuts do not line up with silences and no other track order "
                  "fits. The file may be gapless, or hold other versions; if you know the "
                  "order, give it with --order (e.g. 3,1,2,...).", file=sys.stderr)

    # back to album order
    segments, methods, positions = [None] * n, [None] * n, [None] * n
    for p, i in enumerate(order):
        segments[i], methods[i], positions[i] = seg_f[p], meth_f[p], p + 1
    reordered = order != identity

    multi = release.disc_count > 1
    filenames = [track_filename(t, multi, ext) for t in tracks]
    rows = build_rows(release, segments, filenames, methods, positions)
    outdir = args.output or default_output_dir(args.input, release)
    print(f"Output: {outdir}" + ("" if copy else f"  (re-encoding {codec} to AAC)"))
    print()
    print(console_table(rows, columns_for(release, reordered)))

    if args.dry_run:
        print("\nDry run: nothing written.")
        return

    # ---- write ------------------------------------------------------------
    os.makedirs(outdir, exist_ok=True)
    cover, cover_file = None, None
    if not args.no_cover and release.id:
        try:
            cover = fetch_cover(release, args.cover_size)
        except RuntimeError as exc:
            print(f"warning: no cover art: {exc}", file=sys.stderr)
        if cover:
            cover_file = f"cover.{'png' if image_type(cover) == 'png' else 'jpg'}"
            with open(os.path.join(outdir, cover_file), "wb") as f:
                f.write(cover)
            print(f"\nCover : {cover_file} ({len(cover) // 1024} KB)")
        else:
            print("\nCover : none in the Cover Art Archive")

    print()
    for i, (track, (start, end), name) in enumerate(zip(tracks, segments, filenames), 1):
        dst = os.path.join(outdir, name)
        print(f"[{i}/{n}] {name}")
        try:
            cut_track(ffmpeg, args.input, dst, start, end, copy)
        except subprocess.CalledProcessError:
            sys.exit(f"error: ffmpeg failed on track {i}")
        tag_file(dst, release, track, cover)

    reports = write_reports(outdir, args.format, release, rows, args.input, duration,
                            cover_file)
    print("\nDone. Overview: " + ", ".join(reports))


if __name__ == "__main__":
    main()
