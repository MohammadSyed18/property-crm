import tempfile, unittest
from pathlib import Path
from property_crm import CRM, Conflict, NotFound


class CRMTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.crm = CRM(Path(self.tmp.name) / "db.sqlite3")
        for tenant in ("a", "b"):
            self.crm.add_property(tenant, "office", "Office")

    def test_search_is_scoped_and_ingestion_idempotent(self):
        first = self.crm.add_document(
            "a",
            "office",
            "Amenities",
            "Office includes parking and secure bike storage.",
            "https://example.org/a",
        )
        self.assertEqual(
            first,
            self.crm.add_document(
                "a",
                "office",
                "Amenities",
                "Office includes parking and secure bike storage.",
                "https://example.org/a",
            ),
        )
        self.crm.add_document(
            "b",
            "office",
            "Private",
            "Private parking agreement.",
            "https://example.org/b",
        )
        results = self.crm.search("a", "office", "parking")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["source"], "https://example.org/a")

    def test_optimistic_transition_and_tenant_isolation(self):
        lead = self.crm.create_lead("a", "office", "Visitor", "visitor@example.org")
        with self.assertRaises(NotFound):
            self.crm.get_lead("b", lead["id"])
        self.crm.transition("a", lead["id"], "qualified", 1)
        with self.assertRaises(Conflict):
            self.crm.transition("a", lead["id"], "tour", 1)
        self.assertEqual(self.crm.pipeline("a", "office")["qualified"], 1)

    def test_failed_transition_does_not_write_audit(self):
        lead = self.crm.create_lead("a", "office", "Visitor", "visitor@example.org")
        with self.assertRaises(Conflict):
            self.crm.transition("a", lead["id"], "won", 1)
        with self.crm.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM audit").fetchone()[0], 1)
