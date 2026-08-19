"""Room feedback-loop tests — queue picking and score ingestion, offline.

The LiveKit transport is exercised elsewhere (and only when a server is
configured); everything here is the catalog side of the loop: what a
session should audition, and what happens when its exported scores come
back. No network, no extras.
"""

import json

import pytest

from soundlabel.catalog import Artist, Catalog
from soundlabel.cli import main
from soundlabel.rooms import ingest_session, pick_queue


def _catalog_with_tracks(tmp_path, n=3):
    ws = tmp_path / "label"
    catalog = Catalog(ws / "catalog.db")
    catalog.add_artist(Artist("ivy", "Ivy"))
    tids = []
    for i in range(n):
        wav = tmp_path / f"t{i}.wav"
        wav.write_bytes(b"RIFF" + bytes([i]))  # content-addressed → distinct ids
        tids.append(catalog.add_track("ivy", f"Song {i}", wav, 6.0 + i, "accept"))
    return ws, catalog, tids


def test_pick_queue_prefers_unheard(tmp_path):
    ws, catalog, tids = _catalog_with_tracks(tmp_path)
    catalog.add_room_score(tids[2], "night", "zoe", 8)   # newest track already heard
    queue = [r["id"] for r in pick_queue(catalog, limit=3)]
    assert queue[-1] == tids[2]                          # heard sinks to the back
    assert set(queue[:2]) == {tids[0], tids[1]}          # unheard first
    catalog.close()


def test_pick_queue_released_only(tmp_path):
    ws, catalog, tids = _catalog_with_tracks(tmp_path, n=1)
    wav = tmp_path / "redo.wav"
    wav.write_bytes(b"RIFFx")
    catalog.add_track("ivy", "Not Released", wav, 4.0, "redo")
    assert [r["id"] for r in pick_queue(catalog)] == tids
    catalog.close()


def test_ingest_session_round_trip(tmp_path):
    ws, catalog, tids = _catalog_with_tracks(tmp_path, n=1)
    session = {"room": "release-night", "scores": [
        {"track_id": tids[0], "identity": "zoe", "score": 8, "comment": "keeper"},
        {"track_id": tids[0], "identity": "kai", "score": 6},
        {"track_id": "trk_nope", "identity": "zoe", "score": 9},
    ]}
    result = ingest_session(catalog, session)
    assert result == {"ingested": 2, "skipped": ["trk_nope"]}
    assert catalog.room_reception()[tids[0]] == {"avg": 7.0, "n": 2}
    # A&R sees human reception through history
    assert catalog.history("ivy")["room_reception"][tids[0]]["n"] == 2
    catalog.close()


def test_rescore_overwrites_not_pads(tmp_path):
    ws, catalog, tids = _catalog_with_tracks(tmp_path, n=1)
    catalog.add_room_score(tids[0], "night", "zoe", 4)
    catalog.add_room_score(tids[0], "night", "zoe", 9)   # changed their mind
    assert catalog.room_reception()[tids[0]] == {"avg": 9.0, "n": 1}
    catalog.close()


def test_room_score_requires_known_track(tmp_path):
    ws, catalog, _ = _catalog_with_tracks(tmp_path, n=1)
    with pytest.raises(KeyError):
        catalog.add_room_score("trk_missing", "night", "zoe", 7)
    catalog.close()


def test_cli_queue_ingest_catalog_flow(tmp_path, capsys):
    ws, catalog, tids = _catalog_with_tracks(tmp_path, n=2)
    catalog.close()

    main(["-w", str(ws), "room", "queue"])
    out = capsys.readouterr().out
    assert "[unheard]" in out and tids[0] in out

    session_file = tmp_path / "session.json"
    session_file.write_text(json.dumps({"room": "release-night", "scores": [
        {"track_id": tids[0], "identity": "zoe", "score": 8},
    ]}))
    main(["-w", str(ws), "room", "ingest", str(session_file)])
    out = capsys.readouterr().out
    assert "ingested 1 score(s)" in out and "room 8.0×1" in out

    # reception lands in the catalog listing and in state.json for ops
    main(["-w", str(ws), "catalog"])
    assert "room 8.0×1" in capsys.readouterr().out
    state = json.loads((ws / "state.json").read_text())
    by_id = {t["id"]: t for t in state["tracks"]["recent"]}
    assert by_id[tids[0]]["room"] == {"avg": 8.0, "n": 1}
    assert by_id[tids[1]]["room"] is None


# ---------------------------------------------------------- A&R follows the room

from soundlabel.agents.anr import ANRAgent


def _history(style_reception, styles_seen=("pop", "ballad", "rnb")):
    return {"n_tracks": 3, "style_counts": {s: 1 for s in styles_seen},
            "recent_verdicts": ["accept"], "style_reception": style_reception}


def test_anr_avoids_cold_style():
    """A style the room scores below 6 (over 2+ scores) never gets briefed."""
    artist = Artist("ivy", "Ivy", sonic_profile="pop, ballad, rnb")
    history = _history({"ballad": {"avg": 4.5, "n": 3}})
    for seed in range(20):
        brief = ANRAgent().write_brief(artist, history, seed=seed)
        assert brief.style_tags != ["ballad"]


def test_anr_leans_into_loved_style():
    """A style the room scores >= 8 (over 2+ scores) wins the next brief."""
    artist = Artist("ivy", "Ivy", sonic_profile="pop, ballad, rnb")
    history = _history({"rnb": {"avg": 8.6, "n": 4}})
    for seed in range(5):
        assert ANRAgent().write_brief(artist, history, seed=seed).style_tags == ["rnb"]


def test_one_listener_is_not_a_trend():
    """n=1 reception, however extreme, changes nothing."""
    artist = Artist("ivy", "Ivy", sonic_profile="pop, ballad, rnb")
    cold_but_thin = _history({"ballad": {"avg": 1.0, "n": 1}})
    picked = {tuple(ANRAgent().write_brief(artist, cold_but_thin, seed=s).style_tags)
              for s in range(30)}
    assert ("ballad",) in picked


def test_loved_dominant_beats_anti_rut():
    """Measured demand overrides presumed fatigue: a dominant style the room
    loves is briefed again even though anti-rut alone would drop it."""
    artist = Artist("ivy", "Ivy", sonic_profile="pop, ballad, rnb")
    history = {"n_tracks": 6, "style_counts": {"pop": 5, "ballad": 1},
               "recent_verdicts": ["accept"],
               "style_reception": {"pop": {"avg": 9.0, "n": 3}}}
    assert ANRAgent().write_brief(artist, history).style_tags == ["pop"]
    # ...and without the room signal, anti-rut does drop it
    no_room = {**history, "style_reception": {}}
    for seed in range(20):
        assert ANRAgent().write_brief(artist, no_room, seed=seed).style_tags != ["pop"]


def test_style_reception_rollup(tmp_path):
    """Catalog.history aggregates track-level room scores to style level."""
    ws = tmp_path / "label"
    catalog = Catalog(ws / "catalog.db")
    catalog.add_artist(Artist("ivy", "Ivy"))
    tids = []
    for i, style in enumerate(["pop", "pop"]):
        wav = tmp_path / f"s{i}.wav"
        wav.write_bytes(b"RIFF" + bytes([i]))
        tids.append(catalog.add_track("ivy", f"S{i}", wav, 6.0, "accept",
                                      score_detail={"style_tags": [style]}))
    catalog.add_room_score(tids[0], "night", "zoe", 9)
    catalog.add_room_score(tids[1], "night", "zoe", 5)
    catalog.add_room_score(tids[1], "night", "kai", 6)
    rollup = catalog.history("ivy")["style_reception"]
    assert rollup == {"pop": {"avg": round((9 + 5 + 6) / 3, 2), "n": 3}}
    catalog.close()
