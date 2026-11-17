from __future__ import annotations

from contextlib import contextmanager
import json
import sqlite3
from pathlib import Path

from .model import LakeError, now


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS dimension_versions (
 id INTEGER PRIMARY KEY, kind TEXT NOT NULL, external_id TEXT NOT NULL,
 name TEXT NOT NULL, valid_from TEXT NOT NULL, valid_to TEXT,
 attributes TEXT NOT NULL, version INTEGER NOT NULL, created_at TEXT NOT NULL,
 retracted_at TEXT,
 UNIQUE(kind,external_id,version)
);
CREATE INDEX IF NOT EXISTS ix_dim_time ON dimension_versions(kind,external_id,valid_from,valid_to);
CREATE TABLE IF NOT EXISTS dimension_revisions (
 id INTEGER PRIMARY KEY, kind TEXT NOT NULL, external_id TEXT NOT NULL,
 action TEXT NOT NULL, effective_from TEXT, retracts_id INTEGER,
 target_json TEXT NOT NULL, before_json TEXT NOT NULL, after_json TEXT NOT NULL,
 affected_fact_ids TEXT NOT NULL, changed_fact_ids TEXT NOT NULL,
 blocked_json TEXT NOT NULL DEFAULT '[]', request_json TEXT NOT NULL DEFAULT '{}',
 status TEXT NOT NULL, revision_key TEXT, reason TEXT,
 created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_revision_dim ON dimension_revisions(kind,external_id,id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_revision_key ON dimension_revisions(revision_key)
 WHERE revision_key IS NOT NULL;
CREATE TABLE IF NOT EXISTS fact_reassignments (
 id INTEGER PRIMARY KEY, fact_id INTEGER NOT NULL REFERENCES facts(id),
 revision_id INTEGER NOT NULL, old_street_id TEXT, old_community_id TEXT,
 new_street_id TEXT, new_community_id TEXT,
 old_dimensions TEXT NOT NULL, new_dimensions TEXT NOT NULL, at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_reassignment_fact ON fact_reassignments(fact_id,id);
CREATE TABLE IF NOT EXISTS ingestions (
 id INTEGER PRIMARY KEY, source TEXT NOT NULL, external_id TEXT NOT NULL,
 content_hash TEXT NOT NULL, raw_json TEXT NOT NULL, received_at TEXT NOT NULL,
 state TEXT NOT NULL, issue TEXT, normalized_id INTEGER,
 UNIQUE(source,external_id,content_hash)
);
CREATE INDEX IF NOT EXISTS ix_ingest_source ON ingestions(source,external_id,id);
CREATE TABLE IF NOT EXISTS facts (
 id INTEGER PRIMARY KEY, ingestion_id INTEGER NOT NULL UNIQUE REFERENCES ingestions(id),
 source TEXT NOT NULL, external_id TEXT NOT NULL, event_type TEXT NOT NULL,
 happened_at TEXT NOT NULL, day TEXT NOT NULL, batch_id TEXT NOT NULL,
 material_id TEXT, device_id TEXT, station_id TEXT, actor_id TEXT,
 street_id TEXT, community_id TEXT, weight_kg TEXT, payout_yuan TEXT,
 channel TEXT NOT NULL, dimensions_json TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'active'
);
CREATE INDEX IF NOT EXISTS ix_fact_batch ON facts(batch_id,event_type);
CREATE INDEX IF NOT EXISTS ix_fact_day ON facts(day,street_id,material_id);
CREATE TABLE IF NOT EXISTS audit (
 id INTEGER PRIMARY KEY, at TEXT NOT NULL, action TEXT NOT NULL,
 subject TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS releases (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, project_month TEXT NOT NULL,
 fingerprint TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL,
 supersedes_id INTEGER REFERENCES releases(id), reason TEXT
);
CREATE INDEX IF NOT EXISTS ix_release_month ON releases(project_month,id);
CREATE TABLE IF NOT EXISTS source_registry (
 source TEXT PRIMARY KEY, channel TEXT NOT NULL, active INTEGER NOT NULL,
 version INTEGER NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rate_cards (
 id INTEGER PRIMARY KEY, material_id TEXT NOT NULL, channel TEXT NOT NULL,
 valid_from TEXT NOT NULL, valid_to TEXT, rate_yuan_per_kg TEXT NOT NULL,
 version INTEGER NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(material_id,channel,version)
);
CREATE INDEX IF NOT EXISTS ix_rate_period ON rate_cards(material_id,channel,valid_from,valid_to);
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            self._migrate(conn)
            conn.executescript(SCHEMA)

    def _migrate(self, conn):
        """Add columns introduced after the first schema without rebuilding tables."""
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "dimension_versions" in tables:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(dimension_versions)")}
            if "retracted_at" not in columns:
                conn.execute("ALTER TABLE dimension_versions ADD COLUMN retracted_at TEXT")
        if "releases" in tables:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(releases)")}
            if "supersedes_id" not in columns:
                conn.execute("ALTER TABLE releases ADD COLUMN supersedes_id INTEGER")
            if "reason" not in columns:
                conn.execute("ALTER TABLE releases ADD COLUMN reason TEXT")
        if "dimension_revisions" in tables:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(dimension_revisions)")}
            if "blocked_json" not in columns:
                conn.execute("ALTER TABLE dimension_revisions ADD COLUMN blocked_json TEXT NOT NULL DEFAULT '[]'")
            if "request_json" not in columns:
                conn.execute("ALTER TABLE dimension_revisions ADD COLUMN request_json TEXT NOT NULL DEFAULT '{}'")

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=15000")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @contextmanager
    def write(self):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            yield conn

    @staticmethod
    def one(conn, sql: str, args=()):
        row = conn.execute(sql, args).fetchone()
        return dict(row) if row else None

    @staticmethod
    def all(conn, sql: str, args=()):
        return [dict(row) for row in conn.execute(sql, args)]

    @staticmethod
    def audit(conn, action: str, subject: str, payload: dict):
        conn.execute("INSERT INTO audit(at,action,subject,payload) VALUES(?,?,?,?)",
                     (now(), action, subject, json.dumps(payload, ensure_ascii=False, sort_keys=True)))

    def source(self, source: str):
        with self.connect() as conn:
            row = self.one(conn, "SELECT * FROM source_registry WHERE source=?", (source,))
        if row is None:
            raise LakeError("unknown_source", "source is not registered", 404)
        return row

    def dimension(self, kind: str, external_id: str, day: str):
        with self.connect() as conn:
            rows = self.all(conn, "SELECT * FROM dimension_versions WHERE kind=? AND external_id=? AND retracted_at IS NULL AND valid_from<=? AND (valid_to IS NULL OR valid_to>?) ORDER BY version DESC",
                            (kind, external_id, day, day))
        if not rows:
            raise LakeError("unknown_dimension", f"{kind}:{external_id} is not valid on {day}", 409)
        if len(rows) > 1:
            raise LakeError("ambiguous_dimension", f"{kind}:{external_id} has overlapping versions", 409)
        row = rows[0]
        row["attributes"] = json.loads(row["attributes"])
        return row

    def fact_revisions(self, fact_id: int):
        with self.connect() as conn:
            rows = self.all(conn, "SELECT * FROM fact_reassignments WHERE fact_id=? ORDER BY id", (fact_id,))
        for row in rows:
            row["old_dimensions"] = json.loads(row["old_dimensions"])
            row["new_dimensions"] = json.loads(row["new_dimensions"])
        return rows

    def revisions(self, kind: str | None = None, external_id: str | None = None):
        clause, args = "", []
        if kind is not None:
            clause, args = " WHERE kind=?", [kind]
            if external_id is not None:
                clause, args = " WHERE kind=? AND external_id=?", [kind, external_id]
        with self.connect() as conn:
            rows = self.all(conn, "SELECT * FROM dimension_revisions" + clause + " ORDER BY id", tuple(args))
        for row in rows:
            for field in ("target_json", "before_json", "after_json",
                          "affected_fact_ids", "changed_fact_ids", "blocked_json",
                          "request_json"):
                row[field] = json.loads(row[field])
        return rows

    def current_release(self, month: str):
        with self.connect() as conn:
            rows = self.all(conn, "SELECT id,name,project_month,fingerprint,supersedes_id,reason,created_at FROM releases WHERE project_month=? ORDER BY id", (month,))
        if not rows:
            return None
        superseded = {row["supersedes_id"] for row in rows if row["supersedes_id"]}
        heads = [row for row in rows if row["id"] not in superseded]
        return heads[-1] if heads else rows[-1]

    def fact(self, fact_id: int):
        with self.connect() as conn:
            row = self.one(conn, "SELECT * FROM facts WHERE id=?", (fact_id,))
        if row is None:
            raise LakeError("unknown_fact", "fact not found", 404)
        row["dimensions"] = json.loads(row.pop("dimensions_json"))
        return row

    def facts(self, where: str = "", args=()):
        with self.connect() as conn:
            rows = self.all(conn, "SELECT * FROM facts " + where + " ORDER BY id", args)
        for row in rows:
            row["dimensions"] = json.loads(row.pop("dimensions_json"))
        return rows

    def ingestion(self, ingestion_id: int):
        with self.connect() as conn:
            row = self.one(conn, "SELECT * FROM ingestions WHERE id=?", (ingestion_id,))
        if row is None:
            raise LakeError("unknown_ingestion", "ingestion not found", 404)
        row["raw"] = json.loads(row.pop("raw_json"))
        return row

    def release(self, identifier: int | str):
        if isinstance(identifier, int):
            clause, arg = "id=?", identifier
        else:
            clause, arg = "name=?", identifier
        with self.connect() as conn:
            row = self.one(conn, "SELECT * FROM releases WHERE " + clause, (arg,))
        if row is None:
            raise LakeError("unknown_release", "release not found", 404)
        row["content"] = json.loads(row["content"])
        row["superseded_by"] = self._superseded_by(row["id"])
        row["current"] = row["superseded_by"] is None
        return row

    def _superseded_by(self, release_id: int):
        with self.connect() as conn:
            row = self.one(conn, "SELECT id,name FROM releases WHERE supersedes_id=? ORDER BY id LIMIT 1",
                           (release_id,))
        return row

    def release_chain(self, release_id: int):
        chain = [self.release(release_id)]
        seen = {release_id}
        while chain[-1]["supersedes_id"]:
            parent_id = chain[-1]["supersedes_id"]
            if parent_id in seen:
                raise LakeError("release_cycle", "release supersession chain is cyclic", 409)
            seen.add(parent_id)
            chain.append(self.release(parent_id))
        return chain

    def audits(self, since: int = 0, limit: int = 100):
        if type(since) is not int or since < 0 or type(limit) is not int or not 1 <= limit <= 500:
            raise LakeError("invalid_page", "invalid audit pagination")
        with self.connect() as conn:
            rows = self.all(conn, "SELECT * FROM audit WHERE id>? ORDER BY id LIMIT ?", (since, limit))
        for row in rows:
            row["payload"] = json.loads(row["payload"])
        return rows
