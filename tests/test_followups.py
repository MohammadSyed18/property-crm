import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from property_crm.repository import CRM, Conflict, NotFound
from property_crm.followups import Followups
from property_crm.reporting import export_leads_csv, pipeline_aging


class FollowupTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.crm = CRM(Path(self.directory.name) / "crm.db")
        self.crm.add_property("one", "p", "Office")
        self.lead = self.crm.create_lead("one", "p", "=FORMULA()", "a@example.org")
        self.tasks = Followups(self.crm)

    def test_scoped_retry_and_version_conflicts(self):
        task = self.tasks.create(
            "one", self.lead["id"], "Arrange tour", "2026-08-01T12:00:00Z"
        )
        self.assertEqual(len(self.tasks.due("one", "2026-08-02T00:00:00Z")), 1)
        self.assertEqual(self.tasks.due("other", "2026-08-02T00:00:00Z"), [])
        with self.assertRaises(NotFound):
            self.tasks.complete("other", task["id"], 1)
        revised = self.tasks.reschedule("one", task["id"], 1, "2026-08-03T12:00:00Z")
        with self.assertRaises(Conflict):
            self.tasks.complete("one", task["id"], 1)
        done = self.tasks.complete("one", task["id"], revised["version"])
        self.assertEqual(
            done, self.tasks.complete("one", task["id"], revised["version"])
        )
        self.assertEqual(self.tasks.due("one", "2026-09-01T00:00:00Z"), [])

    def test_reports_and_time_validation(self):
        self.assertIn("'=FORMULA()", export_leads_csv(self.crm, "one", "p"))
        self.assertEqual(pipeline_aging(self.crm, "one", "p")["new"]["leads"], 1)
        with self.assertRaises(ValueError):
            self.tasks.create("one", self.lead["id"], "Task", "2026-08-01")
