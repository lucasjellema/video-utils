#!/usr/bin/env python3
r"""
cut_video.py - remove one or more sections from an .mp4 video.

Usage:
    python cut_video.py INPUT.mp4 "[1:45-2:10, 5:23-8:29]" [-o OUTPUT.mp4] [--fast]

python cut_video.py "C:\Users\lucas_j\Downloads\Trend - pitches a 3 min-20261105_135806-Meeting Recording.mp4"  "[0:01-2:56,1.14:11-1.26:11,2.27:23-2.42:35]"

python cut_video.py "C:\Users\lucas_j\Downloads\Trend - pitches a 3 min-20261105_135806-Meeting Recording 1.mp4"  "[4:03-44:40]" --fast

Timestamps:
    SS, MM:SS or HH:MM:SS. A '.' is accepted as separator too, so "8.29"
    means 8 min 29 s. Each pair START-END marks a section to remove.
    The surrounding square brackets are optional.

Output: it saves myvideo_cut.mp4 next to the original. Use -o other.mp4 to choose a different name.
Timestamps: you can write seconds, minutes:seconds, or hours:minutes:seconds. A dot works like a colon, so 8.29 means 8 min 29 s, as in your example. One catch: this means you can’t give fractions of a second.
Order and overlaps: ranges can be in any order. Overlapping ranges are merged, and ranges that run past the end of the video are trimmed, with a warning.
Input checks: it rejects invalid times like 2:70 and any set of ranges that would remove the whole video.

Two modes:
Default (accurate): cuts at exactly the times you give. It re-encodes the whole video at high quality, which took about 33 seconds for the 10-minute test video. Longer or higher-resolution videos will take longer.
--fast: doesn’t re-encode, so it’s very quick (about 1.5 seconds), but each cut moves to the nearest keyframe. In the test that kept about 9 extra seconds (6:38 instead of 6:29). Use it when a few seconds either way doesn’t matter.

Requires ffmpeg: uses `ffmpeg` on PATH if present, otherwise the binary
bundled with the `imageio-ffmpeg` pip package (pip install imageio-ffmpeg).


NB: aan elkaar plakken van mp4 bestanden:

 $ff = python -c "import imageio_ffmpeg as i; print(i.get_ffmpeg_exe())"
 "file 'C:/Users/lucas_j/Downloads/Trend - pitches a 3 min-20261105_135806-Meeting Recording_cut.mp4'", "file 'C:/Users/lucas_j/Downloads/Trend - pitches a 3 min-20261105_135806-Meeting Recording 1_cut.mp4'" | Set-Content list.txt
 & $ff -f concat -safe 0 -i list.txt -c copy samen.mp4

"""

import argparse
import os
import re
import shutil
import subprocess
import sys


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

def parse_timestamp(text: str) -> float:
    """'1:45' -> 105, '8.29' -> 509, '1:02:03' -> 3723, '90' -> 90."""
    text = text.strip()
    parts = re.split(r"[:.]", text)
    if not 1 <= len(parts) <= 3 or not all(p.isdigit() for p in parts):
        raise ValueError(f"invalid timestamp '{text}' (use SS, MM:SS or HH:MM:SS)")
    nums = [int(p) for p in parts]
    if len(nums) >= 2 and nums[-1] >= 60:
        raise ValueError(f"invalid timestamp '{text}': seconds must be < 60")
    if len(nums) == 3 and nums[1] >= 60:
        raise ValueError(f"invalid timestamp '{text}': minutes must be < 60")
    seconds = 0
    for n in nums:
        seconds = seconds * 60 + n
    return float(seconds)


def parse_ranges(text: str) -> list[tuple[float, float]]:
    """'[1:45-2:10, 5:23-8.29]' -> [(105, 130), (323, 509)], sorted and merged."""
    body = text.strip().strip("[]").strip()
    if not body:
        raise ValueError("no time ranges given")
    ranges = []
    for chunk in body.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        pieces = chunk.split("-")
        if len(pieces) != 2:
            raise ValueError(f"invalid range '{chunk}' (expected START-END)")
        start, end = parse_timestamp(pieces[0]), parse_timestamp(pieces[1])
        if end <= start:
            raise ValueError(f"invalid range '{chunk}': end must be after start")
        ranges.append((start, end))

    # sort and merge overlapping / touching ranges
    ranges.sort()
    merged = [ranges[0]]
    for start, end in ranges[1:]:
        if start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def keep_segments(cuts, duration):
    """Invert the cut ranges into the segments to keep."""
    keep, pos = [], 0.0
    for start, end in cuts:
        start, end = min(start, duration), min(end, duration)
        if start > pos:
            keep.append((pos, start))
        pos = max(pos, end)
    if pos < duration:
        keep.append((pos, duration))
    return [(s, e) for s, e in keep if e - s > 0.01]


def fmt(seconds: float) -> str:
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# --------------------------------------------------------------------------- #
# ffmpeg helpers
# --------------------------------------------------------------------------- #

def find_ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        sys.exit("error: ffmpeg not found. Install ffmpeg or run: pip install imageio-ffmpeg")


def probe(ffmpeg: str, path: str) -> tuple[float, bool]:
    """Return (duration_seconds, has_audio) by parsing `ffmpeg -i` output."""
    result = subprocess.run([ffmpeg, "-hide_banner", "-i", path],
                            capture_output=True, text=True, errors="replace")
    info = result.stderr
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", info)
    if not m:
        sys.exit(f"error: could not read video duration of '{path}'\n{info}")
    duration = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    has_audio = re.search(r"Stream #\S+.*: Audio:", info) is not None
    return duration, has_audio


def build_accurate_cmd(ffmpeg, src, dst, keep, has_audio):
    """Frame-accurate cut: trim + concat filter, re-encodes (H.264 / AAC)."""
    parts, labels = [], ""
    for i, (s, e) in enumerate(keep):
        parts.append(f"[0:v]trim=start={s}:end={e},setpts=PTS-STARTPTS[v{i}]")
        labels += f"[v{i}]"
        if has_audio:
            parts.append(f"[0:a]atrim=start={s}:end={e},asetpts=PTS-STARTPTS[a{i}]")
            labels += f"[a{i}]"
    a = 1 if has_audio else 0
    parts.append(f"{labels}concat=n={len(keep)}:v=1:a={a}[outv]" + ("[outa]" if has_audio else ""))

    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-stats", "-y", "-i", src,
           "-filter_complex", ";".join(parts), "-map", "[outv]"]
    if has_audio:
        cmd += ["-map", "[outa]", "-c:a", "aac", "-b:a", "192k"]
    cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "18",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", dst]
    return cmd


def run_fast(ffmpeg, src, dst, keep):
    """Fast cut: stream copy, no re-encode. Cuts snap to keyframes."""
    workdir = os.path.dirname(os.path.abspath(dst))
    base = os.path.splitext(os.path.basename(dst))[0]
    pieces = []
    try:
        for i, (s, e) in enumerate(keep):
            piece = os.path.join(workdir, f".{base}.part{i}.mp4")
            subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                            "-ss", str(s), "-to", str(e), "-i", src,
                            "-map", "0", "-c", "copy", "-avoid_negative_ts", "make_zero",
                            piece], check=True)
            pieces.append(piece)
        listfile = os.path.join(workdir, f".{base}.list.txt")
        with open(listfile, "w", encoding="utf-8") as f:
            for p in pieces:
                f.write("file '" + p.replace("\\", "/").replace("'", "'\\''") + "'\n")
        pieces.append(listfile)
        subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "concat", "-safe", "0", "-i", listfile,
                        "-c", "copy", "-movflags", "+faststart", dst], check=True)
    finally:
        for p in pieces:
            if os.path.exists(p):
                os.remove(p)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main():
    ap = argparse.ArgumentParser(
        description="Remove sections from an .mp4 video.",
        epilog='Example: python cut_video.py talk.mp4 "[1:45-2:10, 5:23-8:29]"')
    ap.add_argument("input", help="path to the source .mp4 video")
    ap.add_argument("ranges", help='sections to remove, e.g. "[1:45-2:10, 5:23-8:29]"')
    ap.add_argument("-o", "--output", help="output file (default: <input>_cut.mp4)")
    ap.add_argument("--fast", action="store_true",
                    help="no re-encoding: much faster, but cuts snap to the nearest keyframe")
    args = ap.parse_args()

    if not os.path.isfile(args.input):
        sys.exit(f"error: input file not found: {args.input}")
    try:
        cuts = parse_ranges(args.ranges)
    except ValueError as exc:
        sys.exit(f"error: {exc}")

    output = args.output or os.path.splitext(args.input)[0] + "_cut.mp4"
    if os.path.abspath(output) == os.path.abspath(args.input):
        sys.exit("error: output must differ from input")

    ffmpeg = find_ffmpeg()
    duration, has_audio = probe(ffmpeg, args.input)

    for s, e in cuts:
        if s >= duration:
            print(f"warning: range {fmt(s)}-{fmt(e)} starts after the end of the video "
                  f"({fmt(duration)}); ignored", file=sys.stderr)
        elif e > duration:
            print(f"warning: range {fmt(s)}-{fmt(e)} extends past the end "
                  f"({fmt(duration)}); trimmed", file=sys.stderr)

    keep = keep_segments(cuts, duration)
    if not keep:
        sys.exit("error: the given ranges remove the entire video")

    removed = duration - sum(e - s for s, e in keep)
    print(f"Input : {args.input} ({fmt(duration)})")
    print("Remove: " + ", ".join(f"{fmt(s)}-{fmt(min(e, duration))}" for s, e in cuts if s < duration))
    print(f"Output: {output} (~{fmt(duration - removed)})")

    try:
        if args.fast:
            run_fast(ffmpeg, args.input, output, keep)
        else:
            subprocess.run(build_accurate_cmd(ffmpeg, args.input, output, keep, has_audio),
                           check=True)
    except subprocess.CalledProcessError:
        sys.exit("error: ffmpeg failed")
    print("Done.")


if __name__ == "__main__":
    main()
