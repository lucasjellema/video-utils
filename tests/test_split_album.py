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
    rows = sa.build_rows(r, [(0, 61)], ["01 - A _ B.m4a"])
    md = sa.markdown_report(r, rows, "x/album.m4a", 61)
    assert "| 1 | A \\| B | 1:01 | 01 - A _ B.m4a | 0:00 |" in md
    assert "musicbrainz.org/release/rid" in md


# --------------------------------------------------------------------------- #
# End to end on a synthetic album (MusicBrainz mocked)
# --------------------------------------------------------------------------- #

def synth_release(lengths):
    return {
        "id": "rel-1", "title": "Synth: Album", "date": "2020-01-01",
        "artist-credit": [{"name": "Tone Makers", "artist": {"id": "art-1"}}],
        "release-group": {"id": "rg-1", "first-release-date": "2019-05-05",
                          "genres": [{"name": "electronic", "count": 3}]},
        "media": [{"position": 1, "tracks": [
            {"id": f"trk-{i}", "position": i + 1, "title": f"Tone {i + 1}",
             "length": int(l * 1000), "recording": {"id": f"rec-{i}"}}
            for i, l in enumerate(lengths)]}],
    }


@pytest.fixture
def mock_mb(monkeypatch):
    lengths = [3.0, 4.0, 5.0]

    def fake_get(path, params):
        if path == "release/":
            return {"releases": [{"id": "rel-1", "score": 100, "status": "Official",
                                  "release-group": {"primary-type": "Album"}}]}
        assert path == "release/rel-1"
        return synth_release(lengths)

    monkeypatch.setattr(sa, "mb_get", fake_get)
    return lengths


def make_album(path, total, codec_args):
    ffmpeg = sa.find_ffmpeg()
    subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", f"sine=frequency=440:duration={total}", *codec_args, path],
                   check=True)


def duration_of(path):
    return sa.probe_audio(sa.find_ffmpeg(), path)[0]


@pytest.mark.parametrize("ext, codec_args", [
    (".m4a", ["-c:a", "aac", "-b:a", "128k"]),
    (".mp3", ["-c:a", "libmp3lame", "-b:a", "128k"]),
])
def test_end_to_end(tmp_path, mock_mb, ext, codec_args):
    src = str(tmp_path / f"album{ext}")
    make_album(src, 12.0, codec_args)
    out = tmp_path / "out"
    sa.main([src, "--artist", "Tone Makers", "--album", "Synth: Album", "-o", str(out)])

    files = sorted(p.name for p in out.iterdir())
    assert files == [f"01 - Tone 1{ext}", f"02 - Tone 2{ext}", f"03 - Tone 3{ext}",
                     "tracks.md"]
    for name, expected in zip(files, mock_mb):
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
    assert f"| 3 | Tone 3 | 0:05 | 03 - Tone 3{ext} | 0:07 |" in md


def test_dry_run_writes_nothing(tmp_path, mock_mb, capsys):
    src = str(tmp_path / "album.m4a")
    make_album(src, 12.0, ["-c:a", "aac"])
    sa.main([src, "--artist", "x", "--album", "y", "--dry-run"])
    assert sorted(os.listdir(tmp_path)) == ["album.m4a"]


def test_default_output_dir(tmp_path, mock_mb):
    src = str(tmp_path / "album.m4a")
    make_album(src, 12.0, ["-c:a", "aac"])
    sa.main([src, "--release-id", "rel-1"])
    assert (tmp_path / "Tone Makers" / "2019 - Synth_ Album" / "tracks.md").is_file()
