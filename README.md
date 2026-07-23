# Property CRM

A property-focused lead pipeline and document-search service. The portfolio implementation uses anonymized records and local SQLite storage.

- Tenant and property boundaries on lead and document queries
- Full-text retrieval with ranked passages and source links
- Content-hash deduplication during document ingestion
- Optimistic version checks for pipeline transitions
- Transactional audit events and rollback on failed updates
- Authenticated REST endpoints for a configured tenant

## Run

Python 3.12+ with SQLite FTS5 support; no Python packages are required.

```sh
python -m unittest discover -s tests -v
export CRM_API_TOKEN=your-long-local-development-secret
python -m property_crm.server
```

Use `CRM.add_property()` and `CRM.add_document()` to populate the database. `GET /search?property=office&q=parking` returns ranked source passages; `POST /leads` creates a lead; `POST /leads/stage` requires its current version. Send the configured token as a Bearer authorization header. The server binds to localhost.

## Follow-up workflows and reporting

[followups.py](property_crm/followups.py) provides property/tenant-scoped lead tasks, timezone-aware due dates, optimistic rescheduling, idempotent completion, and audit events. [reporting.py](property_crm/reporting.py) reports pipeline aging and exports scoped leads with CSV formula escaping.

The authenticated API also supports `GET /followups?through=<ISO-time>&property=<id>`, `POST /followups` with `{lead_id,title,due_at}`, `POST /followups/complete` with `{id,version}`, and `POST /followups/reschedule` with `{id,version,due_at}`. Completion retries return the completed task without recording the event twice.

Tests cover tenant isolation, stale versions, completion retries, invalid timestamps, scoped reporting, and safe CSV fields.

## Tour reservations

The authenticated service exposes `POST /tours` with `{lead_id, agent, start, end}` and an `Idempotency-Key` header. `GET /tours?start=...&end=...&agent=...` returns the tenant's booked tours; agent is optional. `POST /tours/cancel` accepts `{id, version}`. Tour windows last 15 minutes to four hours and require timezone-aware timestamps.

`tours.py` reserves the agent, lead, and property exclusively in one SQLite write transaction. Adjacent windows are allowed; overlapping windows conflict. Retries with the same key and content return the existing reservation, while changed content conflicts. Closed leads cannot book. Cancellation checks the version and releases the window; repeating a completed cancellation is safe. Creation and cancellation emit tenant-scoped audit events. Tenant boundaries apply to calendars, leads, cancellation, and request keys. This local system does not send calendar invitations or messages.
