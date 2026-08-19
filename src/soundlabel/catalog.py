"""The label's system of record: artists, tracks, batches. SQLite, one file.

Design notes:

- Track ids hash the *audio content*, not the storage path or batch — a
  location-derived id gives the same song different identities in different
  environments, and every cross-environment copy then needs an id remap.
  Content addressing makes identity portable for free.
- ``add_track`` requires a score and a verdict. There is no way to put an
  unscored track in the catalog; "every track is scored before a human hears
  it" is enforced at the schema boundary, not in the calling code's goodwill.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS artists (
    slug TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    language TEXT DEFAULT 'en',
    sonic_profile TEXT DEFAULT '',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS batches (
    id TEXT PRIMARY KEY,
    artist_slug TEXT NOT NULL REFERENCES artists(slug),
    backend TEXT NOT NULL,
    brief_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    manifest_path TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tracks (
    id TEXT PRIMARY KEY,
    artist_slug TEXT NOT NULL REFERENCES artists(slug),
    batch_id TEXT REFERENCES batches(id),
    title TEXT NOT NULL,
    audio_path TEXT NOT NULL,
    score REAL NOT NULL,
    verdict TEXT NOT NULL,
    score_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS room_scores (
    track_id TEXT NOT NULL REFERENCES tracks(id),
    room TEXT NOT NULL,
    listener TEXT NOT NULL,
    score REAL NOT NULL,
    comment TEXT DEFAULT '',
    created_at REAL NOT NULL,
    PRIMARY KEY (track_id, room, listener)
);
"""


def track_id(audio_path: str | Path) -> str:
    """Content-addressed id: sha256 of the audio bytes, 12 hex chars."""
    digest = hashlib.sha256(Path(audio_path).read_bytes()).hexdigest()
    return f"trk_{digest[:12]}"


@dataclass
class Artist:
    slug: str
    name: str
    language: str = "en"
    sonic_profile: str = ""


class Catalog:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)

    # -- artists ----------------------------------------------------------
    def add_artist(self, artist: Artist) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO artists VALUES (?,?,?,?,?)",
            (artist.slug, artist.name, artist.language, artist.sonic_profile, time.time()),
        )
        self._conn.commit()

    def get_artist(self, slug: str) -> Artist | None:
        row = self._conn.execute("SELECT * FROM artists WHERE slug=?", (slug,)).fetchone()
        if row is None:
            return None
        return Artist(row["slug"], row["name"], row["language"], row["sonic_profile"])

    def artists(self) -> list[Artist]:
        rows = self._conn.execute("SELECT * FROM artists ORDER BY created_at").fetchall()
        return [Artist(r["slug"], r["name"], r["language"], r["sonic_profile"]) for r in rows]

    # -- batches ----------------------------------------------------------
    def open_batch(self, batch_id: str, artist_slug: str, backend: str, brief_json: str) -> None:
        self._conn.execute(
            "INSERT INTO batches (id, artist_slug, backend, brief_json, created_at) VALUES (?,?,?,?,?)",
            (batch_id, artist_slug, backend, brief_json, time.time()),
        )
        self._conn.commit()

    def close_batch(self, batch_id: str, status: str, manifest_path: str | None = None) -> None:
        self._conn.execute(
            "UPDATE batches SET status=?, manifest_path=? WHERE id=?",
            (status, manifest_path, batch_id),
        )
        self._conn.commit()

    def batches(self, limit: int = 10) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM batches ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()

    # -- tracks -----------------------------------------------------------
    def add_track(
        self,
        artist_slug: str,
        title: str,
        audio_path: str | Path,
        score: float,
        verdict: str,
        score_detail: dict | None = None,
        batch_id: str | None = None,
    ) -> str:
        """Insert a scored track. A track without a score cannot exist here."""
        tid = track_id(audio_path)
        self._conn.execute(
            "INSERT OR REPLACE INTO tracks VALUES (?,?,?,?,?,?,?,?,?)",
            (
                tid, artist_slug, batch_id, title, str(audio_path),
                float(score), verdict,
                json.dumps(score_detail or {}, ensure_ascii=False), time.time(),
            ),
        )
        self._conn.commit()
        return tid

    def tracks(self, artist_slug: str | None = None) -> list[sqlite3.Row]:
        if artist_slug:
            return self._conn.execute(
                "SELECT * FROM tracks WHERE artist_slug=? ORDER BY created_at", (artist_slug,)
            ).fetchall()
        return self._conn.execute("SELECT * FROM tracks ORDER BY created_at").fetchall()

    # -- room scores ------------------------------------------------------
    def add_room_score(self, track_id: str, room: str, listener: str,
                       score: float, comment: str = "") -> None:
        """Record one listener's room score. Re-scoring the same track in the
        same room overwrites (last listen wins) — a listener changing their
        mind is signal, padding the count is not."""
        if self._conn.execute("SELECT 1 FROM tracks WHERE id=?", (track_id,)).fetchone() is None:
            raise KeyError(f"track {track_id!r} not in the catalog")
        self._conn.execute(
            "INSERT OR REPLACE INTO room_scores VALUES (?,?,?,?,?,?)",
            (track_id, room, listener, float(score), comment, time.time()),
        )
        self._conn.commit()

    def room_reception(self, track_id: str | None = None) -> dict[str, dict]:
        """Aggregate human reception per track: {track_id: {avg, n}}."""
        sql = "SELECT track_id, AVG(score) AS avg, COUNT(*) AS n FROM room_scores"
        params: tuple = ()
        if track_id:
            sql += " WHERE track_id=?"
            params = (track_id,)
        rows = self._conn.execute(sql + " GROUP BY track_id", params).fetchall()
        return {r["track_id"]: {"avg": round(r["avg"], 2), "n": r["n"]} for r in rows}

    def history(self, artist_slug: str) -> dict:
        """What the A&R agent reads: style distribution + recent verdicts."""
        rows = self.tracks(artist_slug)
        verdicts = [r["verdict"] for r in rows]
        styles: dict[str, int] = {}
        for r in rows:
            for tag in json.loads(r["score_json"]).get("style_tags", []):
                styles[tag] = styles.get(tag, 0) + 1
        reception = self.room_reception()
        return {"n_tracks": len(rows), "style_counts": styles,
                "recent_verdicts": verdicts[-5:],
                "room_reception": {r["id"]: reception[r["id"]]
                                   for r in rows if r["id"] in reception}}

    def close(self) -> None:
        self._conn.close()
