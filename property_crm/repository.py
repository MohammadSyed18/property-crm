"""Property-scoped CRM records, optimistic updates, and indexed document retrieval."""

from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlparse
from uuid import uuid4


class Conflict(ValueError):
    pass


class NotFound(LookupError):
    pass


STAGES = {
    "new": {"qualified", "lost"},
    "qualified": {"tour", "lost"},
    "tour": {"offer", "lost"},
    "offer": {"won", "lost"},
    "won": set(),
    "lost": set(),
}


def text(value, name, maximum=10000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"invalid {name}")
    return value.strip()


class CRM:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS properties(
                    tenant TEXT NOT NULL, id TEXT NOT NULL, name TEXT NOT NULL,
                    PRIMARY KEY(tenant,id));
                CREATE TABLE IF NOT EXISTS leads(
                    id TEXT PRIMARY KEY, tenant TEXT NOT NULL, property_id TEXT NOT NULL,
                    name TEXT NOT NULL, email TEXT NOT NULL, stage TEXT NOT NULL DEFAULT 'new',
                    version INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL,
                    FOREIGN KEY(tenant,property_id) REFERENCES properties(tenant,id));
                CREATE TABLE IF NOT EXISTS documents(
                    id INTEGER PRIMARY KEY, tenant TEXT NOT NULL, property_id TEXT NOT NULL,
                    title TEXT NOT NULL, body TEXT NOT NULL, source TEXT NOT NULL, digest TEXT NOT NULL,
                    UNIQUE(tenant,property_id,digest),
                    FOREIGN KEY(tenant,property_id) REFERENCES properties(tenant,id));
                CREATE VIRTUAL TABLE IF NOT EXISTS document_search USING fts5(title,body);
                CREATE TABLE IF NOT EXISTS audit(
                    sequence INTEGER PRIMARY KEY, tenant TEXT NOT NULL, entity TEXT NOT NULL,
                    event TEXT NOT NULL, payload TEXT NOT NULL, occurred_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS leads_scope ON leads(tenant,property_id,stage);
            """
            )

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise

    def _property(self, db, tenant, property_id):
        if not db.execute(
            "SELECT 1 FROM properties WHERE tenant=? AND id=?", (tenant, property_id)
        ).fetchone():
            raise NotFound("property not found")

    def _audit(self, db, tenant, entity, event, payload):
        db.execute(
            "INSERT INTO audit(tenant,entity,event,payload,occurred_at) VALUES(?,?,?,?,?)",
            (
                tenant,
                entity,
                event,
                json.dumps(payload, sort_keys=True),
                datetime.now(timezone.utc).isoformat(),
            ),
        )

    def add_property(self, tenant, property_id, name):
        values = (
            text(tenant, "tenant", 100),
            text(property_id, "property id", 100),
            text(name, "name", 200),
        )
        with self.transaction() as db:
            db.execute("INSERT INTO properties VALUES(?,?,?)", values)

    def create_lead(self, tenant, property_id, name, email):
        name = text(name, "name", 200)
        email = text(email, "email", 254)
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
            raise ValueError("invalid email")
        lead_id = str(uuid4())
        now = datetime.now(timezone.utc).isoformat()
        with self.transaction() as db:
            self._property(db, tenant, property_id)
            db.execute(
                "INSERT INTO leads(id,tenant,property_id,name,email,updated_at) VALUES(?,?,?,?,?,?)",
                (lead_id, tenant, property_id, name, email, now),
            )
            self._audit(db, tenant, lead_id, "lead.created", {"property": property_id})
        return self.get_lead(tenant, lead_id)

    def get_lead(self, tenant, lead_id):
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM leads WHERE tenant=? AND id=?", (tenant, lead_id)
            ).fetchone()
        if row is None:
            raise NotFound("lead not found")
        return dict(row)

    def transition(self, tenant, lead_id, stage, expected_version):
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM leads WHERE tenant=? AND id=?", (tenant, lead_id)
            ).fetchone()
            if row is None:
                raise NotFound("lead not found")
            if row["version"] != expected_version:
                raise Conflict("lead version changed")
            if stage not in STAGES[row["stage"]]:
                raise Conflict("invalid pipeline transition")
            db.execute(
                "UPDATE leads SET stage=?,version=version+1,updated_at=? WHERE id=? AND tenant=? AND version=?",
                (
                    stage,
                    datetime.now(timezone.utc).isoformat(),
                    lead_id,
                    tenant,
                    expected_version,
                ),
            )
            self._audit(
                db,
                tenant,
                lead_id,
                "lead.stage_changed",
                {"from": row["stage"], "to": stage, "version": expected_version + 1},
            )
        return self.get_lead(tenant, lead_id)

    def add_document(self, tenant, property_id, title, body, source):
        title = text(title, "title", 300)
        body = text(body, "body", 1_000_000)
        source = text(source, "source", 2048)
        url = urlparse(source)
        if url.scheme not in ("https", "http") or not url.netloc:
            raise ValueError("invalid source URL")
        body = re.sub(r"\s+", " ", body)
        digest = sha256((title + "\n" + body + "\n" + source).encode()).hexdigest()
        with self.transaction() as db:
            self._property(db, tenant, property_id)
            existing = db.execute(
                "SELECT id FROM documents WHERE tenant=? AND property_id=? AND digest=?",
                (tenant, property_id, digest),
            ).fetchone()
            if existing:
                return existing["id"]
            cursor = db.execute(
                "INSERT INTO documents(tenant,property_id,title,body,source,digest) VALUES(?,?,?,?,?,?)",
                (tenant, property_id, title, body, source, digest),
            )
            doc_id = cursor.lastrowid
            db.execute(
                "INSERT INTO document_search(rowid,title,body) VALUES(?,?,?)",
                (doc_id, title, body),
            )
            self._audit(
                db,
                tenant,
                str(doc_id),
                "document.indexed",
                {"property": property_id, "digest": digest},
            )
            return doc_id

    def search(self, tenant, property_id, query, limit=5):
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("limit must be 1..50")
        tokens = re.findall(r"\w+", text(query, "query", 500), flags=re.UNICODE)
        if not tokens:
            return []
        match = " OR ".join('"' + token + '"' for token in dict.fromkeys(tokens[:30]))
        with self.connection() as db:
            self._property(db, tenant, property_id)
            rows = db.execute(
                """SELECT d.id,d.title,d.source,
                    snippet(document_search,1,'','', ' … ',32) AS passage,
                    bm25(document_search,2.0,1.0) AS score
                FROM document_search JOIN documents d ON d.id=document_search.rowid
                WHERE document_search MATCH ? AND d.tenant=? AND d.property_id=?
                ORDER BY score,d.id LIMIT ?""",
                (match, tenant, property_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def pipeline(self, tenant, property_id):
        with self.connection() as db:
            self._property(db, tenant, property_id)
            counts = {
                row["stage"]: row["count"]
                for row in db.execute(
                    "SELECT stage,count(*) AS count FROM leads WHERE tenant=? AND property_id=? GROUP BY stage",
                    (tenant, property_id),
                )
            }
        return {stage: counts.get(stage, 0) for stage in STAGES}
