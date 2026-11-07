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


def test_parse_release_skips_video_discs_and_renumbers():
    data = {"id": "box", "title": "Box", "artist-credit": [{"name": "A", "artist": {"id": "a"}}],
            "release-group": {"id": "rg"},
            "media": [
                {"position": 3, "format": "CD", "tracks": [{"position": 1, "title": "C1",
                                                            "length": 1000}]},
                {"position": 1, "format": "DVD-Video", "tracks": [{"position": 1,
                                                                   "title": "Film",
                                                                   "length": 9_000_000}]},
                {"position": 2, "format": "CD", "tracks": [{"position": 1, "title": "B1",
                                                            "length": 1000}]},
                {"position": 4, "format": "Blu-ray", "tracks": [{"position": 1,
                                                                 "title": "Hi-res",
                                                                 "length": 2000}]}]}
    r = sa.parse_release(data)
    assert [(t.disc, t.title) for t in r.tracks] == [(1, "B1"), (2, "C1")]
    assert r.disc_count == 2
    assert sa.release_length(data) == 2.0


def test_compilation_flag():
    def release(artists, secondary=()):
        return {"id": "c", "title": "C",
                "artist-credit": [{"name": "Various Artists",
                                   "artist": {"id": sa.VARIOUS_ARTISTS_ID}}]
                if artists == "va" else [{"name": "Solo", "artist": {"id": "s"}}],
                "release-group": {"id": "rg", "secondary-types": list(secondary)},
                "media": [{"position": 1, "format": "CD", "tracks": [
                    {"position": i + 1, "title": f"T{i}", "length": 1000,
                     "artist-credit": [{"name": n, "artist": {"id": n}}]}
                    for i, n in enumerate(["X", "Y"] if artists != "solo" else ["Solo"] * 2)]}]}
    assert sa.parse_release(release("va")).compilation
    assert sa.parse_release(release("mixed", ["Compilation"])).compilation
    # a best-of by one artist is not a various-artists compilation
    assert not sa.parse_release(release("solo", ["Compilation"])).compilation
    assert not sa.parse_release(release("mixed")).compilation


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
# Phase 3: cover art, compilations, csv/json
# --------------------------------------------------------------------------- #

def test_image_type():
    assert sa.image_type(b"\xff\xd8\xff\xe0rest") == "jpeg"
    assert sa.image_type(b"\x89PNG\r\n\x1a\nrest") == "png"
    assert sa.image_type(b"<html>") is None


def test_fetch_cover_falls_back_to_release_group(monkeypatch):
    urls = []

    def fake_get(url):
        urls.append(url)
        return None if "/release/" in url else b"\x89PNG\r\n\x1a\nimg"

    monkeypatch.setattr(sa, "http_get_bytes", fake_get)
    r = sa.Release("rid", "t", "a", [], "", "", "rgid", make_tracks(1))
    assert sa.fetch_cover(r, "1200") == b"\x89PNG\r\n\x1a\nimg"
    assert urls == ["https://coverartarchive.org/release/rid/front-1200",
                    "https://coverartarchive.org/release-group/rgid/front-1200"]
    urls.clear()
    sa.fetch_cover(r, "full")
    assert urls[0].endswith("/release/rid/front")


def test_fetch_cover_ignores_non_images(monkeypatch):
    monkeypatch.setattr(sa, "http_get_bytes", lambda url: b"<html>error</html>")
    assert sa.fetch_cover(sa.Release("r", "t", "a", [], "", "", "", make_tracks(1))) is None


def test_artist_column_only_when_artists_differ():
    r = sa.Release("rid", "Alb", "A", [], "", "", "", make_tracks(10, 10))
    assert ("artist", "Artist") not in sa.columns_for(r)
    r.tracks[1].artist = "B"
    cols = sa.columns_for(r)
    assert cols[2] == ("artist", "Artist")
    rows = sa.build_rows(r, [(0, 10), (10, 20)], ["a", "b"], ["metadata", "end"])
    table = sa.console_table(rows, cols)
    assert table.splitlines()[0].split() == ["#", "Title", "Artist", "Length", "Δ",
                                             "Cut", "File", "Start"]


def compilation_release():
    tracks = [sa.Track(1, 1, "Song, One", 100.0, "X", ["x"], "rec-a", "trk-a"),
              sa.Track(2, 1, "Two", None, "Y", ["y"], "", "")]
    return sa.Release("rid", "Hits", "Various Artists", [sa.VARIOUS_ARTISTS_ID],
                      "2001-02-03", "Pop", "rgid", tracks, compilation=True)


def test_csv_report():
    r = compilation_release()
    rows = sa.build_rows(r, [(0, 100.25), (100.25, 160)], ["1-01 - Song, One.m4a",
                                                           "2-01 - Two.m4a"],
                         ["silence", "end"])
    lines = sa.csv_report(rows).splitlines()
    assert lines[0] == ",".join(sa.CSV_FIELDS)
    assert lines[1] == ('1,1,"Song, One",X,1:40,100.25,0:00,0.0,100.0,0.25,silence,'
                        '"1-01 - Song, One.m4a",1')
    assert lines[2] == "2,1,Two,Y,1:00,59.75,1:40,100.25,,,end,2-01 - Two.m4a,2"


def test_json_report():
    r = compilation_release()
    rows = sa.build_rows(r, [(0, 100.25), (100.25, 160)], ["a.m4a", "b.m4a"],
                         ["silence", "end"])
    data = json.loads(sa.json_report(r, rows, "C:/x/hits.m4a", 160, "cover.png"))
    assert data["album"] == {
        "artist": "Various Artists", "title": "Hits", "date": "2001-02-03", "year": "2001",
        "genre": "Pop", "compilation": True, "discs": 2, "tracks": 2,
        "duration_seconds": 160, "source": "hits.m4a", "cover": "cover.png", "metadata_source": "MusicBrainz",
        "musicbrainz_release_id": "rid", "musicbrainz_release_group_id": "rgid"}
    assert data["tracks"][0] == {
        "disc": 1, "number": 1, "title": "Song, One", "artist": "X", "length": "1:40",
        "length_seconds": 100.25, "start": "0:00", "start_seconds": 0,
        "end_seconds": 100.25, "metadata_length_seconds": 100.0,
        "delta_seconds": 0.25, "cut": "silence", "file": "a.m4a", "file_position": 1,
        "musicbrainz_recording_id": "rec-a", "musicbrainz_track_id": "trk-a"}
    assert data["tracks"][1]["musicbrainz_recording_id"] is None


def test_markdown_report_compilation():
    r = compilation_release()
    rows = sa.build_rows(r, [(0, 100), (100, 160)], ["a", "b"], ["metadata", "end"])
    md = sa.markdown_report(r, rows, "hits.m4a", 160, sa.columns_for(r), "cover.jpg")
    assert "- Tracks: 2 on 2 discs, total 2:40" in md
    assert "- Compilation (various artists)" in md
    assert "| 1-1 | Song, One | X | 1:40 | 0.0s | metadata | a | 0:00 |" in md


def test_parse_formats():
    assert sa.parse_formats("md, CSV,json,md") == ["md", "csv", "json"]
    with pytest.raises(Exception, match="unknown format xml"):
        sa.parse_formats("md,xml")


def test_tags_compilation_and_png_cover(tmp_path):
    from mutagen.id3 import ID3
    from mutagen.mp4 import MP4, MP4Cover
    ffmpeg = sa.find_ffmpeg()
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    r = compilation_release()
    for ext, args in ((".m4a", ["-c:a", "aac"]), (".mp3", ["-c:a", "libmp3lame"])):
        path = str(tmp_path / f"t{ext}")
        subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                        "-i", "sine=duration=1", *args, path], check=True)
        sa.tag_file(path, r, r.tracks[1], png)
        if ext == ".m4a":
            tags = MP4(path).tags
            assert tags["cpil"] is True or tags["cpil"] == [True] or tags["cpil"]
            assert tags["covr"][0].imageformat == MP4Cover.FORMAT_PNG
            assert tags["disk"] == [(2, 2)] and tags["aART"] == ["Various Artists"]
        else:
            tags = ID3(path)
            assert str(tags["TCMP"]) == "1"
            assert tags.getall("APIC")[0].mime == "image/png"
            assert str(tags["TPOS"]) == "2/2" and str(tags["TPE1"]) == "Y"


def test_no_cover_option(tmp_path, mock_mb, monkeypatch):
    def boom(*a):
        raise AssertionError("cover must not be fetched")
    monkeypatch.setattr(sa, "fetch_cover", boom)
    src = str(tmp_path / "album.m4a")
    make_album(src, ["-c:a", "aac"])
    out = tmp_path / "out"
    sa.main([src, "--release-id", "rel-1", "-o", str(out), "--no-cover"])
    assert not (out / "cover.jpg").exists()
    assert "![cover]" not in (out / "tracks.md").read_text(encoding="utf-8")


def test_large_mismatch_stops_unless_forced(tmp_path, mock_mb):
    src = str(tmp_path / "album.m4a")
    make_album(src, ["-c:a", "aac"])                 # 12 s; rel-2 is 20 s
    out = tmp_path / "out"
    with pytest.raises(SystemExit, match="wrong edition or album"):
        sa.main([src, "--release-id", "rel-2", "-o", str(out)])
    assert not out.exists()
    sa.main([src, "--release-id", "rel-2", "-o", str(out), "--force"])
    assert (out / "tracks.md").is_file()


def test_choose_release_compares_several_release_groups(monkeypatch, capsys):
    browsed = []

    def fake_get(path, params):
        if path == "release/":
            return {"releases": [
                {"id": f"s{i}", "score": 100 - i, "status": "Official",
                 "release-group": {"id": f"rg{i}", "primary-type": "Album"}}
                for i in range(7)]}
        browsed.append(params["release-group"])
        n = int(params["release-group"][2:])
        # only the 4th group holds an edition that matches the file
        return {"release-count": 1,
                "releases": [browse_release(f"r{n}", [600_000 if n == 3 else 100_000])]}

    monkeypatch.setattr(sa, "mb_get", fake_get)
    assert sa.choose_release("VA", "Hits", 600.0) == "r3"
    assert browsed == [f"rg{i}" for i in range(sa.MAX_RELEASE_GROUPS)]


def test_cover_download_error_is_a_warning(tmp_path, mock_mb, monkeypatch, capsys):
    def fail(*a):
        raise RuntimeError("cannot reach coverartarchive.org")
    monkeypatch.setattr(sa, "fetch_cover", fail)
    src = str(tmp_path / "album.m4a")
    make_album(src, ["-c:a", "aac"])
    sa.main([src, "--release-id", "rel-1", "-o", str(tmp_path / "out")])
    assert "warning: no cover art" in capsys.readouterr().err
    assert (tmp_path / "out" / "tracks.md").is_file()


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
    monkeypatch.setattr(sa, "fetch_cover", lambda release, size: FAKE_JPEG)


FAKE_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64 + b"\xff\xd9"


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
    sa.main([src, "--artist", "Tone Makers", "--album", "Synth: Album", "-o", str(out),
             "--format", "md,csv,json"])

    files = sorted(p.name for p in out.iterdir())
    assert files == [f"01 - Tone 1{ext}", f"02 - Tone 2{ext}", f"03 - Tone 3{ext}",
                     "cover.jpg", "tracks.csv", "tracks.json", "tracks.md"]
    assert (out / "cover.jpg").read_bytes() == FAKE_JPEG
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
        assert bytes(tags["covr"][0]) == FAKE_JPEG
        assert "cpil" not in tags
    else:
        from mutagen.id3 import ID3
        tags = ID3(second)
        assert str(tags["TIT2"]) == "Tone 2" and str(tags["TPE2"]) == "Tone Makers"
        assert str(tags["TALB"]) == "Synth: Album"
        assert str(tags["TRCK"]) == "2/3" and str(tags["TPOS"]) == "1/1"
        assert str(tags["TDRC"]) == "2019-05-05" and str(tags["TCON"]) == "Electronic"
        assert str(tags["TXXX:MusicBrainz Album Id"]) == "rel-1"
        assert tags["UFID:http://musicbrainz.org"].data == b"rec-1"
        apic = tags.getall("APIC")[0]
        assert apic.data == FAKE_JPEG and apic.mime == "image/jpeg" and apic.type == 3
        assert not tags.getall("TCMP")

    data = json.loads((out / "tracks.json").read_text(encoding="utf-8"))
    assert data["album"]["musicbrainz_release_id"] == "rel-1"
    assert data["album"]["cover"] == "cover.jpg"
    assert [t["file"] for t in data["tracks"]] == files[:3]
    assert data["tracks"][1]["start_seconds"] == pytest.approx(3.0, abs=0.1)

    csv_text = (out / "tracks.csv").read_text(encoding="utf-8-sig")
    assert csv_text.splitlines()[0] == ",".join(sa.CSV_FIELDS)
    assert csv_text.splitlines()[2].startswith("1,2,Tone 2,Tone Makers,0:05,")

    md = (out / "tracks.md").read_text(encoding="utf-8")
    assert "# Tone Makers - Synth: Album" in md
    assert "![cover](cover.jpg)" in md
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



# --------------------------------------------------------------------------- #
# Phase 4: album from tags / file name, tracklists, track order, AcoustID
# --------------------------------------------------------------------------- #

def test_probe_tags(tmp_path):
    path = str(tmp_path / "t.m4a")
    subprocess.run([sa.find_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-f",
                    "lavfi", "-i", "sine=duration=1", "-metadata", "album_artist=Pink Floyd",
                    "-metadata", "album=Wish You Were Here", "-metadata", "title=Song: X",
                    path], check=True)
    tags = sa.probe_tags(sa.find_ffmpeg(), path)
    assert tags["album_artist"] == "Pink Floyd"
    assert tags["album"] == "Wish You Were Here"
    assert tags["title"] == "Song: X"


@pytest.mark.parametrize("text, expected", [
    ("Pink Floyd - Wish You Were Here", ("Pink Floyd", "Wish You Were Here")),
    ("Pink Floyd - Wish You Were Here (1975) [Full Album]",
     ("Pink Floyd", "Wish You Were Here")),
    ("Pink_Floyd_-_Wish_You_Were_Here_(Official_Audio)", ("Pink Floyd", "Wish You Were Here")),
    ("Pink Floyd – 1975 – Wish You Were Here", ("Pink Floyd", "Wish You Were Here")),
    ("AC-DC - Back in Black (Remastered)", ("AC-DC", "Back in Black")),
    ("Wish You Were Here", (None, None)),
])
def test_split_artist_album(text, expected):
    assert sa.split_artist_album(text) == expected


def test_guess_artist_album_precedence():
    tags = {"artist": "A", "album": "B", "title": "X - Y"}
    assert sa.guess_artist_album(tags, "C - D.m4a") == ("A", "B", "tags")
    assert sa.guess_artist_album({"title": "X - Y [Full Album]"}, "C - D.m4a") == (
        "X", "Y", "title tag")
    assert sa.guess_artist_album({}, "dir/C - D.m4a") == ("C", "D", "file name")
    assert sa.guess_artist_album({}, "album.m4a") == (None, None, "")


@pytest.mark.parametrize("text, seconds", [
    ("3:45", 225), ("1:02:03", 3723), ("225", 225), (225, 225), ("3:45.5", 225.5)])
def test_parse_time(text, seconds):
    assert sa.parse_time(text) == seconds


@pytest.mark.parametrize("bad", ["3:75", "abc", "1:2:3:4", "-1"])
def test_parse_time_rejects(bad):
    with pytest.raises(ValueError):
        sa.parse_time(bad)


CUE = '''REM GENRE "Progressive Rock"
REM DATE 1975
PERFORMER "Pink Floyd"
TITLE "Wish You Were Here"
FILE "album.wav" WAVE
  TRACK 01 AUDIO
    TITLE "Shine On"
    INDEX 01 00:00:00
  TRACK 02 AUDIO
    TITLE "Welcome to the Machine"
    PERFORMER "Pink Floyd"
    INDEX 00 13:29:00
    INDEX 01 13:31:37
'''


def test_parse_cue():
    tl = sa.parse_cue(CUE)
    assert (tl.artist, tl.album, tl.date, tl.genre) == (
        "Pink Floyd", "Wish You Were Here", "1975", "Progressive Rock")
    assert tl.exact and tl.has_starts
    assert [t["title"] for t in tl.tracks] == ["Shine On", "Welcome to the Machine"]
    assert tl.tracks[1]["start"] == pytest.approx(13 * 60 + 31 + 37 / 75)


def test_parse_cue_rejects_multiple_files():
    with pytest.raises(ValueError, match="more than one FILE"):
        sa.parse_cue(CUE + 'FILE "b.wav" WAVE\n  TRACK 03 AUDIO\n    INDEX 01 00:00:00\n')


def test_parse_txt_start_times():
    tl = sa.parse_txt("Artist: Pink Floyd\n"
                      "Album: Wish You Were Here\n"
                      "Year: 1975\n"
                      "# comment\n"
                      "0:00 Shine On You Crazy Diamond (Parts I-V)\n"
                      "13:31 - Welcome to the Machine\n"
                      "2. 20:58 Have a Cigar\n"
                      "[26:06] Wish You Were Here\n")
    assert (tl.artist, tl.album, tl.date) == ("Pink Floyd", "Wish You Were Here", "1975")
    assert [(t["title"], t["start"]) for t in tl.tracks] == [
        ("Shine On You Crazy Diamond (Parts I-V)", 0), ("Welcome to the Machine", 811),
        ("Have a Cigar", 1258), ("Wish You Were Here", 1566)]
    assert tl.has_starts and not tl.exact


def test_parse_txt_lengths():
    tl = sa.parse_txt("01. Speak to Me 1:30\n02 - Breathe (In the Air) - 2:43\n"
                      "Time (6:53)\n99 Luftballons 3:52\n")
    assert [(t["title"], t["length"]) for t in tl.tracks] == [
        ("Speak to Me", 90), ("Breathe (In the Air)", 163), ("Time", 413),
        ("99 Luftballons", 232)]


def test_parse_txt_errors():
    with pytest.raises(ValueError, match="mixes"):
        sa.parse_txt("0:00 A\nB 3:00\n")
    with pytest.raises(ValueError, match="cannot read"):
        sa.parse_txt("just a title\n")


def test_parse_json_tracklist():
    tl = sa.parse_json_tracklist(json.dumps({
        "artist": "VA", "album": "Hits", "year": 2001, "tracks": [
            {"title": "A", "length": "3:00", "artist": "X"},
            {"title": "B", "length": 200, "disc": 2},
            {"title": "C"}]}))
    assert tl.date == "2001" and not tl.has_starts
    assert tl.tracks[0] == {"title": "A", "artist": "X", "disc": 1, "length": 180}
    assert tl.tracks[1]["disc"] == 2 and "length" not in tl.tracks[2]
    with pytest.raises(ValueError, match="track 1 has no length"):
        sa.parse_json_tracklist('{"tracks": [{"title": "A"}, {"title": "B"}]}')
    with pytest.raises(ValueError, match="start time for every track"):
        sa.parse_json_tracklist('{"tracks": [{"start": 0}, {"length": 3}]}')


def test_release_from_tracklist():
    tl = sa.parse_txt("0:00 A\n1:00 B\n2:30 C\n")
    with pytest.raises(ValueError, match="no artist/album"):
        sa.release_from_tracklist(tl, None, None)
    r = sa.release_from_tracklist(tl, "Art", "Alb")
    assert [t.length for t in r.tracks] == [60, 90, None]
    assert r.starts == [0, 60, 150] and not r.exact_starts
    assert r.id == "" and r.source == "tracklist" and not r.compilation
    assert [t.artist for t in r.tracks] == ["Art"] * 3

    tl = sa.parse_json_tracklist(json.dumps({"artist": "VA", "album": "Hits", "tracks": [
        {"title": "A", "length": 1, "artist": "X"}, {"title": "B", "length": 1, "disc": 2},
        {"title": "C", "length": 1, "disc": 2}]}))
    r = sa.release_from_tracklist(tl, None, None)
    assert [(t.disc, t.number) for t in r.tracks] == [(1, 1), (2, 1), (2, 2)]
    assert r.compilation and r.starts is None
    with pytest.raises(ValueError, match="must increase"):
        sa.release_from_tracklist(sa.parse_txt("0:00 A\n0:00 B\n"), "a", "b")


def test_parse_order():
    tracks = make_tracks(1, 2, 3)
    assert sa.parse_order("3, 1,2", tracks) == [2, 0, 1]
    with pytest.raises(ValueError, match="exactly once"):
        sa.parse_order("1,1,2", tracks)
    with pytest.raises(ValueError, match="unknown track '4'"):
        sa.parse_order("1,2,4", tracks)
    multi = make_tracks(1, 2, 3)
    multi[2].disc, multi[2].number = 2, 1
    assert sa.parse_order("2-1,1-1,1-2", multi) == [2, 0, 1]
    assert sa.parse_order("3,1,2", multi) == [2, 0, 1]


def shuffled(lengths, file_order, gap=1.0):
    """Silences (gap long) between the tracks as they sit in the file."""
    silences, pos = [], 0.0
    for i in file_order[:-1]:
        pos += lengths[i]
        silences.append((pos - gap / 2, pos + gap / 2))
    return silences, pos + lengths[file_order[-1]]


def test_detect_order_from_lengths():
    lengths = [200.0, 150.0, 320.0, 245.0, 180.0]
    tracks = make_tracks(*lengths)
    silences, duration = shuffled(lengths, [2, 0, 4, 1, 3])
    silences += [(50, 51), (400, 400.8)]               # quiet bits inside tracks
    found = sa.detect_order(tracks, duration, silences)
    assert found.order == [2, 0, 4, 1, 3] and found.ambiguous == []
    assert [round(s) for s, _ in found.segments] == [0, 320, 520, 700, 850]


def test_detect_order_flags_lookalikes():
    lengths = [200.0, 200.5, 300.0]
    silences, duration = shuffled(lengths, [2, 1, 0])
    found = sa.detect_order(make_tracks(*lengths), duration, silences)
    assert found.order == [2, 1, 0]                    # closest length match
    assert found.ambiguous == [(0, 1)]                 # ... but 0 and 1 are flagged


def test_assign_segments_lookalikes_beyond_margin_are_not_flagged():
    # 3 s apart: swapping costs 6 s more, clearly distinguishable
    order, ambiguous = sa.assign_segments([203.0, 200.0], [200.0, 203.0])
    assert order == [1, 0] and ambiguous == []


def test_detect_order_with_identifier():
    lengths = [200.0, 200.5, 300.0]
    tracks = make_tracks(*lengths)
    for i, t in enumerate(tracks):
        t.recording_id = f"rec{i}"
    silences, duration = shuffled(lengths, [2, 1, 0])
    truth = {0.0: "rec2", 300.0: "rec1", 500.5: "rec0"}    # segment start -> recording
    identify = lambda s, e: truth[min(truth, key=lambda b: abs(b - s))]
    found = sa.detect_order(tracks, duration, silences, identify=identify)
    assert found.order == [2, 1, 0] and found.identified == 3


def test_detect_order_gives_up():
    lengths = [200.0, 150.0, 320.0]
    assert sa.detect_order(make_tracks(*lengths), 670, []) is None    # no silences
    silences, duration = shuffled([100.0, 400.0, 170.0], [0, 1, 2])   # other album
    assert sa.detect_order(make_tracks(*lengths), duration, silences) is None
    t = make_tracks(*lengths)
    t[0].length = None
    assert sa.detect_order(t, 670, [(199, 201)]) is None


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_acoustid_lookup(monkeypatch):
    import urllib.parse
    sent = {}

    def fake_urlopen(req, timeout):
        sent.update(urllib.parse.parse_qs(req.data.decode()))
        return FakeResponse({"status": "ok", "results": [
            {"score": 0.6, "recordings": [{"id": "low"}]},
            {"score": 0.95, "recordings": [{"id": "high"}]}]})

    monkeypatch.setattr(sa.urllib.request, "urlopen", fake_urlopen)
    assert sa.acoustid_lookup("KEY", "FP", 245.6) == [(0.95, "high"), (0.6, "low")]
    assert sent["client"] == ["KEY"] and sent["duration"] == ["246"]
    assert sent["meta"] == ["recordingids"] and sent["fingerprint"] == ["FP"]

    monkeypatch.setattr(sa.urllib.request, "urlopen", lambda req, timeout: FakeResponse(
        {"status": "error", "error": {"message": "invalid API key"}}))
    with pytest.raises(RuntimeError, match="invalid API key"):
        sa.acoustid_lookup("BAD", "FP", 10)


def test_acoustid_identifier(monkeypatch):
    tracks = make_tracks(10, 10)
    tracks[0].recording_id, tracks[1].recording_id = "rec-a", "rec-b"
    monkeypatch.setattr(sa, "fingerprint", lambda ff, path, s, e: f"fp{s:g}")
    answers = {"fp0": [(0.9, "other"), (0.8, "rec-b")],     # first hit not on the album
               "fp10": [(0.3, "rec-a")]}                     # too unsure
    monkeypatch.setattr(sa, "acoustid_lookup", lambda key, fp, d: answers[fp])
    identify = sa.acoustid_identifier("ffmpeg", "x.m4a", "KEY", tracks)
    assert identify(0, 10) == "rec-b"
    assert identify(10, 20) is None


def test_fingerprint(tmp_path):
    path = str(tmp_path / "t.m4a")
    subprocess.run([sa.find_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-f",
                    "lavfi", "-i", "sine=frequency=500:duration=20", path], check=True)
    fp = sa.fingerprint(sa.find_ffmpeg(), path, 0, 20)
    assert fp and all(c.isalnum() or c in "-_" for c in fp)


# ---- end to end: tracks in another order ----------------------------------

ORDER_LENGTHS = [30.0, 45.0, 60.0]


@pytest.fixture
def mock_mb_order(monkeypatch):
    rel = synth_release("rel-o", ORDER_LENGTHS)
    monkeypatch.setattr(sa, "mb_get", lambda path, params: rel)
    monkeypatch.setattr(sa, "fetch_cover", lambda release, size: None)


def make_shuffled_album(path, file_order):
    """Tones with a 1 s silence between tracks, laid out in `file_order`."""
    silences, total = shuffled(ORDER_LENGTHS, file_order)
    gate = "+".join(f"between(t,{s:.3f},{e:.3f})" for s, e in silences)
    expr = f"sin(2*PI*440*t)*0.5*(1-({gate}))"
    subprocess.run([sa.find_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-f",
                    "lavfi", "-i", f"aevalsrc='{expr}':s=22050:d={total}", "-c:a", "aac",
                    path], check=True)


def test_end_to_end_detects_other_order(tmp_path, mock_mb_order, capsys):
    src = str(tmp_path / "album.m4a")
    make_shuffled_album(src, [2, 0, 1])                # file: track 3, 1, 2
    out = tmp_path / "out"
    sa.main([src, "--release-id", "rel-o", "-o", str(out), "--format", "md,json"])
    printed = capsys.readouterr().out
    assert "the file holds the tracks in this order: 3, 1, 2" in printed
    for name, expected in [("01 - Tone 1.m4a", 30), ("02 - Tone 2.m4a", 45),
                           ("03 - Tone 3.m4a", 60)]:
        assert duration_of(str(out / name)) == pytest.approx(expected, abs=0.6)
    data = json.loads((out / "tracks.json").read_text(encoding="utf-8"))
    assert [t["file_position"] for t in data["tracks"]] == [2, 3, 1]
    assert data["tracks"][0]["start_seconds"] == pytest.approx(60, abs=0.6)
    md = (out / "tracks.md").read_text(encoding="utf-8")
    assert "In file |" in md and "another order" in md


def test_explicit_order_matches_detection(tmp_path, mock_mb_order):
    src = str(tmp_path / "album.m4a")
    make_shuffled_album(src, [2, 0, 1])
    out = tmp_path / "out"
    sa.main([src, "--release-id", "rel-o", "-o", str(out), "--order", "3,1,2",
             "--format", "json"])
    data = json.loads((out / "tracks.json").read_text(encoding="utf-8"))
    assert [t["file_position"] for t in data["tracks"]] == [2, 3, 1]
    assert [t["cut"] for t in data["tracks"]] == ["silence", "end", "silence"]


def test_order_album_disables_detection(tmp_path, mock_mb_order, capsys):
    src = str(tmp_path / "album.m4a")
    make_shuffled_album(src, [2, 0, 1])
    sa.main([src, "--release-id", "rel-o", "--order", "album", "--dry-run"])
    printed = capsys.readouterr().out
    assert "Order :" not in printed and "In file" not in printed


def test_album_in_order_is_not_reordered(tmp_path, mock_mb_order, capsys):
    src = str(tmp_path / "album.m4a")
    make_shuffled_album(src, [0, 1, 2])
    sa.main([src, "--release-id", "rel-o", "--dry-run"])
    printed = capsys.readouterr().out
    assert "Cuts  : 2 of 2 moved to a silence" in printed and "Order :" not in printed


# ---- end to end: album from file name, tracklists ---------------------------

def test_album_from_file_name(tmp_path, mock_mb, monkeypatch, capsys):
    queries = []
    real = sa.mb_get

    def spy(path, params):
        queries.append(params.get("query"))
        return real(path, params)

    monkeypatch.setattr(sa, "mb_get", spy)
    src = str(tmp_path / "Tone Makers - Synth Album (2019) [Full Album].m4a")
    make_album(src, ["-c:a", "aac"])
    sa.main([src, "--dry-run"])
    assert 'release:"Synth Album" AND artist:"Tone Makers"' in queries
    assert "(from the file name)" in capsys.readouterr().out


def test_no_album_info_is_an_error(tmp_path, mock_mb):
    src = str(tmp_path / "album.m4a")
    make_album(src, ["-c:a", "aac"])
    with pytest.raises(SystemExit, match="cannot tell the artist and album"):
        sa.main([src])


def no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("no network calls expected")
    monkeypatch.setattr(sa, "mb_get", boom)
    monkeypatch.setattr(sa, "fetch_cover", boom)


def test_end_to_end_cue_sheet(tmp_path, monkeypatch):
    no_network(monkeypatch)
    src = str(tmp_path / "album.mp3")
    make_album(src, ["-c:a", "libmp3lame"])
    cue = tmp_path / "album.cue"
    cue.write_text('PERFORMER "Cue Band"\nTITLE "Cue Album"\nREM DATE 2001\n'
                   'FILE "album.wav" WAVE\n'
                   '  TRACK 01 AUDIO\n    TITLE "One"\n    INDEX 01 00:00:00\n'
                   '  TRACK 02 AUDIO\n    TITLE "Two"\n    INDEX 01 00:02:60\n'   # 2.8 s
                   '  TRACK 03 AUDIO\n    TITLE "Three"\n    INDEX 01 00:08:15\n',  # 8.2 s
                   encoding="utf-8")
    out = tmp_path / "out"
    sa.main([src, "--tracklist", str(cue), "-o", str(out), "--format", "md,json"])
    data = json.loads((out / "tracks.json").read_text(encoding="utf-8"))
    assert [t["start_seconds"] for t in data["tracks"]] == [0, 2.8, 8.2]
    assert [t["cut"] for t in data["tracks"]] == ["cue", "cue", "end"]
    assert data["album"]["musicbrainz_release_id"] is None
    assert data["album"]["metadata_source"] == "tracklist"
    from mutagen.id3 import ID3
    tags = ID3(str(out / "02 - Two.mp3"))
    assert str(tags["TIT2"]) == "Two" and str(tags["TPE1"]) == "Cue Band"
    assert str(tags["TALB"]) == "Cue Album" and str(tags["TDRC"]) == "2001"
    assert not tags.getall("TXXX") and not tags.getall("UFID")
    md = (out / "tracks.md").read_text(encoding="utf-8")
    assert "Track data: tracklist" in md and "musicbrainz.org" not in md


def test_end_to_end_txt_tracklist_snaps(tmp_path, monkeypatch):
    no_network(monkeypatch)
    src = str(tmp_path / "album.m4a")
    make_album(src, ["-c:a", "aac"])                     # real cuts at 3.0 and 8.0
    txt = tmp_path / "tracks.txt"
    txt.write_text("0:00 Intro\n0:04 Middle\n0:07 Outro\n", encoding="utf-8")
    out = tmp_path / "out"
    sa.main([src, "--tracklist", str(txt), "--artist", "Me", "--album", "Mine",
             "-o", str(out), "--format", "json"])
    data = json.loads((out / "tracks.json").read_text(encoding="utf-8"))
    assert [t["start_seconds"] for t in data["tracks"]] == pytest.approx([0, 3.0, 8.0],
                                                                         abs=0.05)
    assert [t["cut"] for t in data["tracks"]] == ["silence", "silence", "end"]


# --------------------------------------------------------------------------- #
# Loose search when the tags differ slightly from MusicBrainz
# --------------------------------------------------------------------------- #

def test_similarity():
    assert sa.similarity("Songs of Our Native Daughters",
                         "Songs from Our Native Daughters") > 0.9
    assert sa.similarity("Daughters", "Songs from Our Native Daughters") < 0.6
    assert sa.similarity("Don’t Stop", "DON'T STOP!") == 1.0


def test_search_releases_fuzzy_filters_and_ranks(monkeypatch):
    queries = []

    def fake_get(path, params):
        queries.append(params["query"])
        return {"releases": [
            {"id": "other", "title": "Daughters", "score": 60,
             "artist-credit": [{"name": "Daughters"}]},
            {"id": "close", "title": "Songs of Our Native Daughters", "score": 100,
             "status": "Official", "artist-credit": [{"name": "Our Native Daughters"}]},
            {"id": "wrong-artist", "title": "Songs of Our Native Daughters", "score": 90,
             "artist-credit": [{"name": "Someone Else"}]}]}

    monkeypatch.setattr(sa, "mb_get", fake_get)
    found = sa.search_releases_fuzzy("Our Native Daughters (feat. X)",
                                     "Songs from Our Native Daughters")
    assert [r["id"] for r in found] == ["close"]
    # Lucene special characters are escaped, words are not quoted as one phrase
    assert queries == ["release:(Songs from Our Native Daughters) AND "
                       r"artist:(Our Native Daughters \(feat. X\))"]


def test_choose_release_falls_back_to_fuzzy(monkeypatch, capsys):
    def fake_get(path, params):
        if path == "release/" and params["query"].startswith('release:"'):
            return {"releases": []}                       # exact phrase: nothing
        if path == "release/":
            return {"releases": [{"id": "close", "title": "Songs of Our Native Daughters",
                                  "score": 100, "artist-credit": [
                                      {"name": "Our Native Daughters"}],
                                  "release-group": {"id": "rg"}}]}
        return {"release-count": 1, "releases": [browse_release("close", [600_000])]}

    monkeypatch.setattr(sa, "mb_get", fake_get)
    assert sa.choose_release("Our Native Daughters", "Songs from Our Native Daughters",
                             600.0) == "close"
    assert "no exact match" in capsys.readouterr().out


def test_choose_release_nothing_similar(monkeypatch):
    monkeypatch.setattr(sa, "mb_get", lambda path, params: {"releases": [
        {"id": "x", "title": "Something Else", "artist-credit": [{"name": "Other"}]}]
        if "release:(" in params["query"] else []})
    with pytest.raises(LookupError, match="--release-id"):
        sa.choose_release("Artist", "Album", 100.0)


def test_missing_mutagen_stops_before_any_work(tmp_path, monkeypatch):
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "mutagen":
            raise ImportError("No module named 'mutagen'")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    no_network(monkeypatch)
    src = tmp_path / "album.m4a"
    src.write_bytes(b"not audio")                      # never read: check comes first
    with pytest.raises(SystemExit, match="-m pip install mutagen"):
        sa.main([str(src), "--artist", "a", "--album", "b"])
