"""Transactional property tours with tenant isolation and optimistic cancellation."""

from datetime import datetime, timezone
from hashlib import sha256
import json
from uuid import uuid4
from .repository import Conflict, NotFound, text
from .followups import utc


def window(start, end):
    start, end = utc(start), utc(end)
    seconds = (
        datetime.fromisoformat(end) - datetime.fromisoformat(start)
    ).total_seconds()
    if not 900 <= seconds <= 4 * 3600:
        raise ValueError("tour must last 15 minutes to four hours")
    return start, end


class Tours:
    def __init__(self, crm):
        self.crm = crm
        with crm.connection() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS tours(
                  id TEXT PRIMARY KEY, tenant TEXT NOT NULL, lead_id TEXT NOT NULL,
                  property_id TEXT NOT NULL, agent TEXT NOT NULL, starts_at TEXT NOT NULL,
                  ends_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'booked',
                  version INTEGER NOT NULL DEFAULT 1, request_key TEXT NOT NULL,
                  digest TEXT NOT NULL, UNIQUE(tenant,request_key),
                  FOREIGN KEY(lead_id) REFERENCES leads(id));
                CREATE INDEX IF NOT EXISTS tours_calendar ON tours(tenant,status,starts_at,ends_at);
            """
            )

    def book(self, tenant, lead_id, agent, starts_at, ends_at, key):
        agent, key = text(agent, "agent", 100), text(key, "request key", 128)
        start, end = window(starts_at, ends_at)
        digest = sha256(json.dumps([lead_id, agent, start, end]).encode()).hexdigest()
        with self.crm.transaction() as db:
            previous = db.execute(
                "SELECT * FROM tours WHERE tenant=? AND request_key=?", (tenant, key)
            ).fetchone()
            if previous:
                if previous["digest"] != digest:
                    raise Conflict("request key already used for a different tour")
                return self._public(previous)
            lead = db.execute(
                "SELECT * FROM leads WHERE tenant=? AND id=?", (tenant, lead_id)
            ).fetchone()
            if lead is None:
                raise NotFound("lead not found")
            if lead["stage"] in {"won", "lost"}:
                raise Conflict("closed leads cannot book tours")
            overlap = db.execute(
                """SELECT 1 FROM tours WHERE tenant=? AND status='booked'
                AND starts_at<? AND ends_at>? AND (agent=? OR lead_id=? OR property_id=?)""",
                (tenant, end, start, agent, lead_id, lead["property_id"]),
            ).fetchone()
            if overlap:
                raise Conflict(
                    "agent, lead, or property already reserved in this window"
                )
            identifier = str(uuid4())
            db.execute(
                """INSERT INTO tours(id,tenant,lead_id,property_id,agent,starts_at,ends_at,request_key,digest)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    identifier,
                    tenant,
                    lead_id,
                    lead["property_id"],
                    agent,
                    start,
                    end,
                    key,
                    digest,
                ),
            )
            self.crm._audit(
                db,
                tenant,
                identifier,
                "tour.booked",
                {"lead": lead_id, "agent": agent, "start": start, "end": end},
            )
            return self._public(
                db.execute("SELECT * FROM tours WHERE id=?", (identifier,)).fetchone()
            )

    @staticmethod
    def _public(row):
        return {
            key: value
            for key, value in dict(row).items()
            if key not in {"digest", "request_key"}
        }

    def cancel(self, tenant, identifier, expected_version):
        with self.crm.transaction() as db:
            row = db.execute(
                "SELECT * FROM tours WHERE tenant=? AND id=?", (tenant, identifier)
            ).fetchone()
            if row is None:
                raise NotFound("tour not found")
            if row["status"] == "cancelled":
                return self._public(row)
            if type(expected_version) is not int or expected_version != row["version"]:
                raise Conflict("tour version changed")
            db.execute(
                "UPDATE tours SET status='cancelled',version=version+1 WHERE id=?",
                (identifier,),
            )
            self.crm._audit(
                db,
                tenant,
                identifier,
                "tour.cancelled",
                {"version": expected_version + 1},
            )
            return self._public(
                db.execute("SELECT * FROM tours WHERE id=?", (identifier,)).fetchone()
            )

    def calendar(self, tenant, starts_at, ends_at, *, agent=None):
        start, end = utc(starts_at), utc(ends_at)
        span = (
            datetime.fromisoformat(end) - datetime.fromisoformat(start)
        ).total_seconds()
        if not 0 < span <= 31 * 86400:
            raise ValueError("calendar window must be positive and at most 31 days")
        with self.crm.connection() as db:
            query = "SELECT * FROM tours WHERE tenant=? AND status='booked' AND starts_at<? AND ends_at>?"
            args = [tenant, end, start]
            if agent is not None:
                query += " AND agent=?"
                args.append(text(agent, "agent", 100))
            return [
                self._public(row)
                for row in db.execute(query + " ORDER BY starts_at,id LIMIT 1000", args)
            ]
