"""Tenant-scoped follow-up tasks with optimistic edits and audited completion."""

from datetime import datetime, timezone
from uuid import uuid4
from .repository import Conflict, NotFound, text


def utc(value):
    if not isinstance(value, str):
        raise ValueError("timestamp must be text")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return parsed.astimezone(timezone.utc).isoformat()


class Followups:
    def __init__(self, crm):
        self.crm = crm
        with crm.connection() as db:
            db.executescript(
                """CREATE TABLE IF NOT EXISTS followups(
                id TEXT PRIMARY KEY, tenant TEXT NOT NULL, lead_id TEXT NOT NULL,
                title TEXT NOT NULL, due_at TEXT NOT NULL, completed_at TEXT,
                version INTEGER NOT NULL DEFAULT 1,
                FOREIGN KEY(lead_id) REFERENCES leads(id));
                CREATE INDEX IF NOT EXISTS followups_due ON followups(tenant,completed_at,due_at);"""
            )

    def create(self, tenant, lead_id, title, due_at):
        title, due_at = text(title, "follow-up title", 500), utc(due_at)
        identifier = str(uuid4())
        with self.crm.transaction() as db:
            if not db.execute(
                "SELECT 1 FROM leads WHERE tenant=? AND id=?", (tenant, lead_id)
            ).fetchone():
                raise NotFound("lead not found")
            db.execute(
                "INSERT INTO followups(id,tenant,lead_id,title,due_at) VALUES(?,?,?,?,?)",
                (identifier, tenant, lead_id, title, due_at),
            )
            self.crm._audit(
                db,
                tenant,
                identifier,
                "followup.created",
                {"lead": lead_id, "due_at": due_at},
            )
            return dict(
                db.execute(
                    "SELECT * FROM followups WHERE id=?", (identifier,)
                ).fetchone()
            )

    def _get(self, db, tenant, identifier):
        row = db.execute(
            "SELECT * FROM followups WHERE tenant=? AND id=?", (tenant, identifier)
        ).fetchone()
        if row is None:
            raise NotFound("follow-up not found")
        return row

    def complete(self, tenant, identifier, expected_version, *, at=None):
        timestamp = utc(at or datetime.now(timezone.utc).isoformat())
        with self.crm.transaction() as db:
            row = self._get(db, tenant, identifier)
            if row["completed_at"] is not None:
                return dict(row)
            if type(expected_version) is not int or row["version"] != expected_version:
                raise Conflict("follow-up version changed")
            db.execute(
                "UPDATE followups SET completed_at=?,version=version+1 WHERE id=?",
                (timestamp, identifier),
            )
            self.crm._audit(
                db, tenant, identifier, "followup.completed", {"at": timestamp}
            )
            return dict(self._get(db, tenant, identifier))

    def reschedule(self, tenant, identifier, expected_version, due_at):
        due_at = utc(due_at)
        with self.crm.transaction() as db:
            row = self._get(db, tenant, identifier)
            if row["completed_at"] is not None:
                raise Conflict("completed follow-up cannot be rescheduled")
            if type(expected_version) is not int or row["version"] != expected_version:
                raise Conflict("follow-up version changed")
            db.execute(
                "UPDATE followups SET due_at=?,version=version+1 WHERE id=?",
                (due_at, identifier),
            )
            self.crm._audit(
                db,
                tenant,
                identifier,
                "followup.rescheduled",
                {"from": row["due_at"], "to": due_at},
            )
            return dict(self._get(db, tenant, identifier))

    def due(self, tenant, through, *, property_id=None, limit=100):
        through = utc(through)
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit must be 1..1000")
        with self.crm.connection() as db:
            clauses = ["f.tenant=?", "f.completed_at IS NULL", "f.due_at<=?"]
            arguments = [tenant, through]
            if property_id is not None:
                self.crm._property(db, tenant, property_id)
                clauses.append("l.property_id=?")
                arguments.append(property_id)
            rows = db.execute(
                "SELECT f.*,l.name AS lead_name,l.property_id FROM followups f "
                "JOIN leads l ON l.id=f.lead_id AND l.tenant=f.tenant WHERE "
                + " AND ".join(clauses)
                + " ORDER BY f.due_at,f.id LIMIT ?",
                arguments + [limit],
            ).fetchall()
            return [dict(row) for row in rows]
