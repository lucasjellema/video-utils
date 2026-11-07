import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import split_album as sa  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def load(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return json.load(f)


def make_tracks(*lengths):
    return [sa.Track(disc=1, number=i + 1, title=f"T{i + 1}", length=l, artist="A")
            for i, l in enumerate(lengths)]


# --------------------------------------------------------------------------- #
# MusicBrainz parsing
# --------------------------------------------------------------------------- #

def test_parse_release():
    r = sa.parse_release(load("release_wywh.json"))
    assert r.artist == "Pink Floyd"
    assert r.title == "Wish You Were Here"
    assert r.date == "1975-09-12" and r.year == "1975"   # original release date
    assert r.genre == "Progressive Rock"                   # highest vote count
    assert len(r.tracks) == 5 and r.disc_count == 1
    t = r.tracks[1]
    assert (t.number, t.title, t.length) == (2, "Welcome to the Machine", 446.333)
    assert t.recording_id and t.track_id and t.artist_ids == r.artist_ids
    assert r.total_length() == pytest.approx(2650.133)


def test_parse_release_multi_disc_and_track_artist():
    data = {"id": "x", "title": "Comp", "artist-credit": [
                {"name": "Various Artists", "artist": {"id": "va"}}],
            "release-group": {"id": "rg"},
            "media": [
                {"position": 2, "tracks": [{"position": 1, "title": "B1", "length": 1000,
                                            "artist-credit": [
                                                {"name": "X", "joinphrase": " feat. ",
                                                 "artist": {"id": "x1"}},
                                                {"name": "Y", "artist": {"id": "y1"}}]}]},
                {"position": 1, "tracks": [{"position": 1, "title": "A1", "length": None},
                                           {"position": 2, "title": "A2", "length": 2000}]}]}
    r = sa.parse_release(data)
    assert [(t.disc, t.number, t.title) for t in r.tracks] == [(1, 1, "A1"), (1, 2, "A2"),
                                                              (2, 1, "B1")]
    assert r.disc_count == 2 and r.tracks_on_disc(1) == 2
    assert r.tracks[2].artist == "X feat. Y" and r.tracks[2].artist_ids == ["x1", "y1"]
    assert r.tracks[0].artist == "Various Artists"
    assert r.tracks[0].length is None and r.total_length() is None
    assert r.genre == "" and r.date == ""


def test_rank_releases_prefers_official_album_then_earliest():
    cands = [
        {"id": "bootleg", "score": 100, "status": "Bootleg", "date": "1970",
         "release-group": {"primary-type": "Album"}},
        {"id": "late", "score": 100, "status": "Official", "date": "2011",
         "release-group": {"primary-type": "Album"}},
        {"id": "early", "score": 100, "status": "Official", "date": "1975",
         "release-group": {"primary-type": "Album"}},
        {"id": "single", "score": 100, "status": "Official", "date": "1960",
         "release-group": {"primary-type": "Single"}},
        {"id": "lowscore", "score": 80, "status": "Official", "date": "1950",
         "release-group": {"primary-type": "Album"}},
    ]
    assert [c["id"] for c in sa.rank_releases(cands)] == [
        "early", "late", "single", "bootleg", "lowscore"]


def test_search_fixture_ranks():
    best = sa.rank_releases(load("search_wywh.json")["releases"])[0]
    assert best["title"] == "Wish You Were Here"


# --------------------------------------------------------------------------- #
# Cut points
# --------------------------------------------------------------------------- #

def test_segments_cumulative_last_runs_to_end():
    assert sa.compute_segments(make_tracks(10, 20, 30), 62.5) == [
        (0, 10), (10, 30), (30, 62.5)]


def test_segments_last_track_length_may_be_missing():
    assert sa.compute_segments(make_tracks(10, None), 25) == [(0, 10), (10, 25)]


def test_segments_missing_length_midway_is_error():
    with pytest.raises(ValueError, match="no length"):
        sa.compute_segments(make_tracks(10, None, 5), 30)


def test_segments_file_too_short_is_error():
    with pytest.raises(ValueError, match="ends before track 3"):
        sa.compute_segments(make_tracks(10, 20, 30), 30)


def test_duration_mismatch():
    r = sa.Release("id", "t", "a", [], "", "", "", make_tracks(100, 100))
    assert sa.duration_mismatch(r, 203) is None      # within 5 s
    assert sa.duration_mismatch(r, 210) == 10
    assert sa.duration_mismatch(r, 190) == -10


# --------------------------------------------------------------------------- #
# Names and overview
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("raw, expected", [
    ("AC/DC: Live?", "AC_DC_ Live_"),
    ('a<b>c"d|e*f\\g', "a_b_c_d_e_f_g"),
    ("  trailing dots... ", "trailing dots"),
    ("tab\there", "tab_here"),
    ("", "_"),
])
def test_sanitize(raw, expected):
    assert sa.sanitize(raw) == expected


def test_sanitize_truncates():
    assert len(sa.sanitize("x" * 300)) == 120


def test_track_filename():
    t = sa.Track(disc=2, number=3, title="What?", length=1, artist="A")
    assert sa.track_filename(t, False, ".m4a") == "03 - What_.m4a"
    assert sa.track_filename(t, True, ".mp3") == "2-03 - What_.mp3"


def test_markdown_report_escapes_pipes():
    r = sa.Release("rid", "Alb", "Art", [], "1999", "Rock", "", make_tracks(60))
    r.tracks[0].title = "A | B"
    rows = sa.build_rows(r, [(0, 61)], ["01 - A _ B.m4a"], ["end"])
    md = sa.markdown_report(r, rows, "x/album.m4a", 61)
    assert "| 1 | A \\| B | 1:01 | +1.0s | end | 01 - A _ B.m4a | 0:00 |" in md
    assert "musicbrainz.org/release/rid" in md


def test_fmt_delta():
    assert sa.fmt_delta(3.2) == "+0:03"
    assert sa.fmt_delta(-70) == "-1:10"
    assert sa.fmt_delta(0.3) == "0:00"


# --------------------------------------------------------------------------- #
# Phase 2: editions, scaling, silence snapping
# --------------------------------------------------------------------------- #

def browse_release(rid, lengths_ms, status="Official", date="2000"):
    return {"id": rid, "status": status, "date": date,
            "media": [{"position": 1, "format": "CD", "track-count": len(lengths_ms),
                       "tracks": [{"length": l} for l in lengths_ms]}]}


def test_release_length():
    assert sa.release_length(browse_release("a", [1000, 2500])) == 3.5
    assert sa.release_length(browse_release("a", [1000, None])) is None
    assert sa.release_length({"id": "a", "media": []}) is None


def test_rank_by_duration():
    pool = [
        browse_release("far", [100_000, 100_000]),                  # 200 s
        browse_release("close-late", [150_000, 150_400], date="2010"),  # 300.4 s
        browse_release("close-early", [150_000, 150_200], date="1990"),  # 300.2 s
        browse_release("odd-count", [300_100]),                     # 300.1 s, 1 track
        browse_release("unknown", [150_000, None]),
    ]
    ranked = [r["id"] for r in sa.rank_by_duration(pool, 300.0)]
    # same whole-second difference -> common track count, then earliest date
    assert ranked == ["close-early", "close-late", "odd-count", "far"]
    assert sa.rank_by_duration([browse_release("u", [None])], 10) == []


def test_browse_group_releases_pages(monkeypatch):
    calls = []

    def fake_get(path, params):
        calls.append(params["offset"])
        batch = [{"id": f"r{params['offset'] + i}"} for i in range(3)]
        return {"release-count": 7, "releases": batch[:7 - params["offset"]]}

    monkeypatch.setattr(sa, "mb_get", fake_get)
    releases = sa.browse_group_releases("rg")
    assert calls == [0, 3, 6]
    assert [r["id"] for r in releases] == [f"r{i}" for i in range(7)]


def test_scale_factor():
    r = sa.Release("id", "t", "a", [], "", "", "", make_tracks(100, 100))
    assert sa.scale_factor(r, 204) == pytest.approx(1.02)
    assert sa.scale_factor(r, 210) == 1.0                     # > 3%: no scaling
    r.tracks[1].length = None
    assert sa.scale_factor(r, 200) == 1.0


def test_parse_silences():
    log = """
[silencedetect @ 0x1] silence_start: -0.01
[silencedetect @ 0x1] silence_end: 0.5 | silence_duration: 0.51
size=N/A time=00:00:10.00
[silencedetect @ 0x1] silence_start: 120.25
[silencedetect @ 0x1] silence_end: 121.75 | silence_duration: 1.5
[silencedetect @ 0x1] silence_start: 299.1
"""
    assert sa.parse_silences(log, 300.0) == [(0.0, 0.5), (120.25, 121.75), (299.1, 300.0)]


@pytest.mark.parametrize("expected, silences, result", [
    (100, [(99, 101)], 100),                     # inside: middle of the silence
    (100, [(97, 98), (103.5, 104.5)], 97.5),     # nearest silence wins
    (100, [(110, 111)], None),                   # outside the window
    (100, [(98, 400)], 101.5),                   # long silence: clamped to window
    (100, [(96, 97)], None),                     # would make the track too short
])
def test_snap_point(expected, silences, result):
    assert sa.snap_point(expected, silences, 5, lo=96.5, hi=1000) == result


def test_plan_cuts_snaps_relative_to_previous_cut():
    tracks = make_tracks(10, 10, 10)
    # real gaps at 12 and 24: each cut is found relative to the previous one
    segs, methods = sa.plan_cuts(tracks, 35, [(11.5, 12.5), (23.5, 24.5)], window=3)
    assert segs == [(0, 12), (12, 24), (24, 35)]
    assert methods == ["silence", "silence", "end"]


def test_plan_cuts_methods_without_silence():
    segs, methods = sa.plan_cuts(make_tracks(10, 10), 21, [], scale=1.05)
    assert segs == [(0, 10.5), (10.5, 21)]
    assert methods == ["scaled", "end"]
    assert sa.plan_cuts(make_tracks(10, 10), 21)[1] == ["metadata", "end"]


def test_build_rows_delta():
    r = sa.Release("rid", "Alb", "Art", [], "", "", "", make_tracks(10, None))
    rows = sa.build_rows(r, [(0, 10.4), (10.4, 20)], ["a", "b"], ["silence", "end"])
    assert [(x["delta"], x["cut"]) for x in rows] == [("+0.4s", "silence"), ("", "end")]


# --------------------------------------------------------------------------- #
# End to end on a synthetic album (MusicBrainz mocked)
#
# Audio: tone 0-2.5, silence 2.5-3.5, tone 3.5-7.5, silence 7.5-8.5, tone 8.5-12
# so the right cuts are at 3.0 and 8.0. MusicBrainz says 3.4 / 4.6 / 4.0 s.
# --------------------------------------------------------------------------- #

MB_LENGTHS = [3.4, 4.6, 4.0]


def synth_release(rid, lengths):
    return {
        "id": rid, "title": "Synth: Album", "date": "2020-01-01", "status": "Official",
        "artist-credit": [{"name": "Tone Makers", "artist": {"id": "art-1"}}],
        "release-group": {"id": "rg-1", "first-release-date": "2019-05-05",
                          "genres": [{"name": "electronic", "count": 3}]},
        "media": [{"position": 1, "format": "CD", "tracks": [
            {"id": f"trk-{i}", "position": i + 1, "title": f"Tone {i + 1}",
             "length": int(l * 1000), "recording": {"id": f"rec-{i}"}}
            for i, l in enumerate(lengths)]}],
    }


@pytest.fixture
def mock_mb(monkeypatch):
    editions = {"rel-1": synth_release("rel-1", MB_LENGTHS),          # 12 s: matches
                "rel-2": synth_release("rel-2", [5.0, 6.0, 9.0])}     # 20 s

    def fake_get(path, params):
        if path == "release/":                       # search: rel-2 ranks first
            return {"releases": [
                {"id": rid, "score": 100, "status": "Official",
                 "date": "1999" if rid == "rel-2" else "2005",
                 "release-group": {"id": "rg-1", "primary-type": "Album"}}
                for rid in editions]}
        if path == "release":                        # browse release group
            assert params["release-group"] == "rg-1"
            return {"release-count": 2, "releases": list(editions.values())}
        return editions[path.split("/")[1]]

    monkeypatch.setattr(sa, "mb_get", fake_get)


def make_album(path, codec_args):
    ffmpeg = sa.find_ffmpeg()
    expr = ("sin(2*PI*440*t)*0.5*(between(t,0,2.5)+between(t,3.5,7.5)"
            "+between(t,8.5,12))")
    subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", f"aevalsrc='{expr}':s=44100:d=12", *codec_args, path], check=True)


def duration_of(path):
    return sa.probe_audio(sa.find_ffmpeg(), path)[0]


@pytest.mark.parametrize("ext, codec_args", [
    (".m4a", ["-c:a", "aac", "-b:a", "128k"]),
    (".mp3", ["-c:a", "libmp3lame", "-b:a", "128k"]),
])
def test_end_to_end(tmp_path, mock_mb, ext, codec_args):
    src = str(tmp_path / f"album{ext}")
    make_album(src, codec_args)
    out = tmp_path / "out"
    sa.main([src, "--artist", "Tone Makers", "--album", "Synth: Album", "-o", str(out)])

    files = sorted(p.name for p in out.iterdir())
    assert files == [f"01 - Tone 1{ext}", f"02 - Tone 2{ext}", f"03 - Tone 3{ext}",
                     "tracks.md"]
    # cut in the middle of the silences, not at the MusicBrainz lengths
    for name, expected in zip(files, [3.0, 5.0, 4.0]):
        assert duration_of(str(out / name)) == pytest.approx(expected, abs=0.1)

    second = str(out / f"02 - Tone 2{ext}")
    if ext == ".m4a":
        from mutagen.mp4 import MP4
        tags = MP4(second).tags
        assert tags["\xa9nam"] == ["Tone 2"]
        assert tags["\xa9ART"] == ["Tone Makers"] and tags["aART"] == ["Tone Makers"]
        assert tags["\xa9alb"] == ["Synth: Album"]
        assert tags["trkn"] == [(2, 3)] and tags["disk"] == [(1, 1)]
        assert tags["\xa9day"] == ["2019-05-05"] and tags["\xa9gen"] == ["Electronic"]
        # the edition matching the file length was chosen, not the top search hit
        assert bytes(tags["----:com.apple.iTunes:MusicBrainz Album Id"][0]) == b"rel-1"
        assert bytes(tags["----:com.apple.iTunes:MusicBrainz Track Id"][0]) == b"rec-1"
    else:
        from mutagen.id3 import ID3
        tags = ID3(second)
        assert str(tags["TIT2"]) == "Tone 2" and str(tags["TPE2"]) == "Tone Makers"
        assert str(tags["TALB"]) == "Synth: Album"
        assert str(tags["TRCK"]) == "2/3" and str(tags["TPOS"]) == "1/1"
        assert str(tags["TDRC"]) == "2019-05-05" and str(tags["TCON"]) == "Electronic"
        assert str(tags["TXXX:MusicBrainz Album Id"]) == "rel-1"
        assert tags["UFID:http://musicbrainz.org"].data == b"rec-1"

    md = (out / "tracks.md").read_text(encoding="utf-8")
    assert "# Tone Makers - Synth: Album" in md
    assert f"| 1 | Tone 1 | 0:03 | -0.4s | silence | 01 - Tone 1{ext} | 0:00 |" in md
    assert f"| 2 | Tone 2 | 0:05 | +0.4s | silence | 02 - Tone 2{ext} | 0:03 |" in md
    assert f"| 3 | Tone 3 | 0:04 | 0.0s | end | 03 - Tone 3{ext} | 0:08 |" in md


def test_no_snap_cuts_at_metadata_lengths(tmp_path, mock_mb):
    src = str(tmp_path / "album.m4a")
    make_album(src, ["-c:a", "aac"])
    out = tmp_path / "out"
    sa.main([src, "--release-id", "rel-1", "-o", str(out), "--no-snap"])
    for name, expected in zip(sorted(os.listdir(out)), MB_LENGTHS):
        assert duration_of(str(out / name)) == pytest.approx(expected, abs=0.1)
    assert "| metadata |" in (out / "tracks.md").read_text(encoding="utf-8")


def test_dry_run_writes_nothing(tmp_path, mock_mb, capsys):
    src = str(tmp_path / "album.m4a")
    make_album(src, ["-c:a", "aac"])
    sa.main([src, "--artist", "x", "--album", "y", "--dry-run"])
    assert sorted(os.listdir(tmp_path)) == ["album.m4a"]
    out = capsys.readouterr().out
    assert "Release (closest in length of 2 editions): rel-1" in out
    assert "Cuts  : 2 of 2 moved to a silence" in out


def test_default_output_dir(tmp_path, mock_mb):
    src = str(tmp_path / "album.m4a")
    make_album(src, ["-c:a", "aac"])
    sa.main([src, "--release-id", "rel-1"])
    assert (tmp_path / "Tone Makers" / "2019 - Synth_ Album" / "tracks.md").is_file()

