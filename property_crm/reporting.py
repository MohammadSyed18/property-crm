"""Read-only pipeline aging and formula-safe lead exports."""

import csv
from datetime import datetime, timezone
import io
from .followups import utc
from .repository import STAGES


def pipeline_aging(crm, tenant, property_id, *, now=None):
    clock = datetime.fromisoformat(utc(now or datetime.now(timezone.utc).isoformat()))
    with crm.connection() as db:
        crm._property(db, tenant, property_id)
        leads = db.execute(
            "SELECT stage,updated_at FROM leads WHERE tenant=? AND property_id=?",
            (tenant, property_id),
        ).fetchall()
    groups = {
        stage: {"leads": 0, "age_days_total": 0, "oldest_days": 0} for stage in STAGES
    }
    for lead in leads:
        age = max(
            0,
            (clock - datetime.fromisoformat(lead["updated_at"])).total_seconds()
            / 86400,
        )
        group = groups[lead["stage"]]
        group["leads"] += 1
        group["age_days_total"] += age
        group["oldest_days"] = max(group["oldest_days"], age)
    return {
        stage: {
            "leads": value["leads"],
            "mean_days_in_stage": (
                round(value["age_days_total"] / value["leads"], 2)
                if value["leads"]
                else 0
            ),
            "oldest_days": round(value["oldest_days"], 2),
        }
        for stage, value in groups.items()
    }


def export_leads_csv(crm, tenant, property_id):
    with crm.connection() as db:
        crm._property(db, tenant, property_id)
        rows = db.execute(
            "SELECT id,name,email,stage,version,updated_at FROM leads WHERE tenant=? AND property_id=? ORDER BY id",
            (tenant, property_id),
        ).fetchall()
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["id", "name", "email", "stage", "version", "updated_at"])

    def safe(value):
        value = str(value)
        return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value

    writer.writerows([safe(value) for value in row] for row in rows)
    return output.getvalue()
