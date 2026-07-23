import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from property_crm.repository import CRM, Conflict, NotFound
from property_crm.tours import Tours


class TourTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.crm = CRM(Path(self.temp.name) / "crm.db")
        self.tours = Tours(self.crm)
        for tenant in ("a", "b"):
            self.crm.add_property(tenant, "p", "Home")
        self.lead = self.crm.create_lead("a", "p", "Person", "person@example.test")[
            "id"
        ]

    def book(self, key="one", start="2026-07-01T10:00:00Z", end="2026-07-01T10:30:00Z"):
        return self.tours.book("a", self.lead, "agent", start, end, key)

    def test_retry_conflict_and_adjacent_booking(self):
        first = self.book()
        self.assertEqual(self.book()["id"], first["id"])
        with self.assertRaises(Conflict):
            self.book("other")
        with self.assertRaises(Conflict):
            self.book("one", "2026-07-01T11:00:00Z", "2026-07-01T11:30:00Z")
        self.book("adjacent", "2026-07-01T10:30:00Z", "2026-07-01T11:00:00Z")

    def test_cancellation_is_versioned_and_releases_capacity(self):
        tour = self.book()
        with self.assertRaises(Conflict):
            self.tours.cancel("a", tour["id"], 99)
        self.tours.cancel("a", tour["id"], 1)
        self.tours.cancel("a", tour["id"], 1)
        self.book("replacement")

    def test_tenant_and_calendar_boundaries(self):
        tour = self.book()
        with self.assertRaises(NotFound):
            self.tours.cancel("b", tour["id"], 1)
        self.assertEqual(
            self.tours.calendar("b", "2026-07-01T00:00:00Z", "2026-07-02T00:00:00Z"), []
        )
        self.assertEqual(
            len(
                self.tours.calendar("a", "2026-07-01T10:30:00Z", "2026-07-02T00:00:00Z")
            ),
            0,
        )
        with self.assertRaises(ValueError):
            self.book("invalid", "2026-07-01T10:00:00", "2026-07-01T10:30:00")

    def test_concurrent_overlaps_have_one_winner(self):
        def attempt(key):
            try:
                self.book(key)
                return True
            except Conflict:
                return False

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sum(pool.map(attempt, ["a", "b"])), 1)
