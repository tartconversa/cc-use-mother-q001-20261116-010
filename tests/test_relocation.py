import tempfile
import threading
import unittest
from pathlib import Path

from looplake.fixtures import balanced_batch, event, seed_dimensions
from looplake.model import LakeError
from looplake.relocation_demo import run_demo
from looplake.service import LakeService


class RelocationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "lake.sqlite3"
        self.service = LakeService(self.db)
        seed_dimensions(self.service)

    def ingest(self, rows):
        outcomes = [self.service.ingest(row) for row in rows]
        self.assertEqual([row["state"] for row in outcomes], ["normalized"] * len(rows))
        return outcomes

    def version_periods(self, kind="device", external_id="box-1"):
        rows = self.service.dimensions.list_versions(kind, external_id)
        active = [row for row in rows if row["retracted_at"] is None]
        return [(row["valid_from"], row["valid_to"]) for row in active]

    def test_late_notice_uses_actual_move_time(self):
        before = self.ingest(balanced_batch("batch-before", "a",
                                           happened_at="2026-07-05T09:00:00+08:00"))
        after = self.ingest(balanced_batch("batch-after", "b",
                                          happened_at="2026-07-20T09:00:00+08:00"))
        revision = self.service.relocate_device("box-1", "2026-07-10", "community-2")
        self.assertEqual(revision["status"], "applied")
        self.assertEqual(revision["changed_fact_ids"], [after[0]["fact_id"]])
        self.assertEqual(self.service.store.fact(before[0]["fact_id"])["street_id"], "street-1")
        self.assertEqual(self.service.store.fact(after[0]["fact_id"])["street_id"], "street-2")
        self.assertEqual(self.service.store.fact(after[0]["fact_id"])["community_id"], "community-2")
        self.assertEqual(self.version_periods(),
                         [("2026-01-01", "2026-07-10"), ("2026-07-10", None)])
        # Payout follows the deposit it settles, not the device at payout time.
        rows = self.service.mart.month("2026-07")["rows"]
        self.assertEqual([(row["street_id"], row["payout_yuan"]) for row in rows],
                         [("street-1", "0.8"), ("street-2", "0.8")])

    def test_out_of_order_notice_splits_the_right_period(self):
        self.ingest(balanced_batch("batch-1", "a", happened_at="2026-07-20T09:00:00+08:00"))
        self.service.relocate_device("box-1", "2026-07-15", "community-2")
        self.service.relocate_device("box-1", "2026-07-05", "community-2")
        self.assertEqual(sorted(self.version_periods()),
                         [("2026-01-01", "2026-07-05"), ("2026-07-05", "2026-07-15"),
                          ("2026-07-15", None)])
        periods = sorted(self.version_periods())
        for left, right in zip(periods, periods[1:]):
            self.assertEqual(left[1], right[0])

    def test_overlap_never_appears_under_concurrency(self):
        errors = []

        def notify(index):
            try:
                LakeService(self.db).dimensions.relocate(
                    "device", "box-1", "2026-07-0%d" % (index + 1),
                    parents={"community_id": "community-2"},
                    revision_key="notice-%d" % index)
            except LakeError as error:
                errors.append((index, error.code))

        threads = [threading.Thread(target=notify, args=(index,)) for index in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertFalse([error for error in errors if error[1] != "no_change"])
        periods = sorted(self.version_periods())
        for left, right in zip(periods, periods[1:]):
            self.assertEqual(left[1], right[0])

    def test_duplicate_notice_is_idempotent_and_conflict_is_explicit(self):
        first = self.service.relocate_device("box-1", "2026-07-10", "community-2",
                                             revision_key="notice-1")
        again = self.service.relocate_device("box-1", "2026-07-10", "community-2",
                                             revision_key="notice-1")
        self.assertEqual(first["id"], again["id"])
        duplicate = self.service.relocate_device("box-1", "2026-07-10", "community-2")
        self.assertEqual(duplicate["id"], first["id"])
        with self.assertRaises(LakeError) as failure:
            self.service.relocate_device("box-1", "2026-07-10", "community-1",
                                         revision_key="notice-1")
        self.assertEqual(failure.exception.code, "revision_conflict")
        with self.assertRaises(LakeError) as failure:
            self.service.relocate_device("box-1", "2026-07-10", "community-1")
        self.assertEqual(failure.exception.code, "dimension_conflict")

    def test_retraction_restores_previous_attribution(self):
        outcomes = self.ingest(balanced_batch("batch-1", "a",
                                              happened_at="2026-07-20T09:00:00+08:00"))
        first = self.service.relocate_device("box-1", "2026-07-10", "community-2")
        self.assertEqual(self.service.store.fact(outcomes[0]["fact_id"])["street_id"], "street-2")
        retracted = self.service.retract_dimension("device", "box-1", version=2,
                                                   reason="迁址清单撤回")
        self.assertEqual(self.service.store.fact(outcomes[0]["fact_id"])["street_id"], "street-1")
        self.assertEqual(retracted["changed_fact_ids"], first["changed_fact_ids"])
        versions = self.service.dimensions.list_versions("device", "box-1")
        self.assertEqual([row["version"] for row in versions], [1, 2])
        self.assertIsNotNone(versions[1]["retracted_at"])
        self.assertEqual(self.version_periods(), [("2026-01-01", None)])
        # A later correction must still get a fresh, non-reused version.
        corrected = self.service.correct_dimension("device", "box-1", "2026-07-11",
                                                   name="合成箱一（改）")
        self.assertEqual(corrected["after_json"]["version"], 3)

    def test_correction_and_its_retraction_restore_the_replaced_row(self):
        self.ingest(balanced_batch())
        corrected = self.service.correct_dimension("community", "community-1", "2026-07-15",
                                                   name="合成一社区（更名）")
        self.assertEqual(self.service.store.dimension("community", "community-1", "2026-07-15")["name"],
                         "合成一社区（更名）")
        self.service.retract_dimension("community", "community-1",
                                       version=corrected["after_json"]["version"])
        self.assertEqual(self.service.store.dimension("community", "community-1", "2026-07-15")["name"],
                         "合成一社区")

    def test_retrying_quarantine_respects_event_time(self):
        pending = [self.service.ingest(row) for row in
                   balanced_batch("batch-fiber", "f", material_id="future-fiber", payout=None,
                                  happened_at="2026-07-08T08:00:00+08:00")]
        self.assertEqual([row["state"] for row in pending], ["quarantined", "quarantined"])
        self.service.relocate_device("box-1", "2026-07-10", "community-2")
        self.service.register_dimension({"kind": "material", "external_id": "future-fiber",
                                         "name": "合成纤维", "valid_from": "2026-01-01"})
        retried = [self.service.retry(row["ingestion_id"]) for row in pending]
        self.assertEqual([row["state"] for row in retried], ["normalized", "normalized"])
        self.assertEqual(self.service.store.fact(retried[0]["fact_id"])["street_id"], "street-1")

    def test_blocked_chain_keeps_last_good_street_and_blocks_release(self):
        self.service.register_dimension({"kind": "street", "external_id": "street-3",
                                         "name": "合成三街道", "valid_from": "2026-01-01",
                                         "valid_to": "2026-07-15"})
        self.service.register_dimension({"kind": "community", "external_id": "community-3",
                                         "name": "合成三社区", "valid_from": "2026-01-01",
                                         "attributes": {"street_id": "street-3"}})
        outcomes = self.ingest(balanced_batch("batch-1", "a",
                                              happened_at="2026-07-20T09:00:00+08:00"))
        revision = self.service.relocate_device("box-1", "2026-07-05", "community-3")
        self.assertEqual(revision["status"], "applied_with_conflicts")
        self.assertEqual(revision["blocked_fact_ids"], [outcomes[0]["fact_id"]])
        fact = self.service.store.fact(outcomes[0]["fact_id"])
        self.assertEqual(fact["street_id"], "street-1")
        preview = self.service.preview("2026-07")
        self.assertFalse(preview["ready"])
        self.assertEqual(preview["dimension_conflicts"][0]["fact_ids"], [outcomes[0]["fact_id"]])
        with self.assertRaises(LakeError) as failure:
            self.service.publish("july-1", "2026-07", preview["fingerprint"])
        self.assertEqual(failure.exception.code, "unresolved_month")

    def test_relocation_requires_known_dates_and_parent(self):
        with self.assertRaises(LakeError) as failure:
            self.service.relocate_device("box-1", "2025-12-01", "community-2")
        self.assertEqual(failure.exception.code, "unknown_dimension")
        with self.assertRaises(LakeError) as failure:
            self.service.relocate_device("box-1", "2026-07-10", "missing")
        self.assertEqual(failure.exception.code, "unknown_dimension")
        with self.assertRaises(LakeError) as failure:
            self.service.relocate_device("box-1", "2026-07-10", "community-1")
        self.assertEqual(failure.exception.code, "no_change")
        with self.assertRaises(LakeError) as failure:
            self.service.relocate_device("box-1", "2026-07-10", "community-2",
                                         expected_version=99)
        self.assertEqual(failure.exception.code, "stale_dimension")

    def test_lineage_shows_original_and_current_street(self):
        outcomes = self.ingest(balanced_batch("batch-1", "a",
                                              happened_at="2026-07-20T09:00:00+08:00"))
        self.service.relocate_device("box-1", "2026-07-10", "community-2", reason="迁址")
        trace = self.service.lineage.fact(outcomes[0]["fact_id"])
        self.assertEqual(trace["fact"]["street_id"], "street-2")
        self.assertEqual(trace["reassignments"][0]["old_street_id"], "street-1")
        self.assertEqual(trace["reassignments"][0]["new_street_id"], "street-2")
        attribution = self.service.query.attribution("2026-07")
        self.assertEqual(attribution["reassigned_fact_ids"], [outcomes[0]["fact_id"]])
        self.assertEqual(attribution["rows"][0]["original_street_id"], "street-1")


class RepublishTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = LakeService(Path(self.temp.name) / "lake.sqlite3")
        seed_dimensions(self.service)
        for row in balanced_batch("batch-1", "a", happened_at="2026-07-20T09:00:00+08:00"):
            self.service.ingest(row)
        preview = self.service.preview("2026-07")
        self.old = self.service.publish("july-1", "2026-07", preview["fingerprint"])

    def test_republish_supersedes_without_rewriting_frozen_version(self):
        before = self.service.releases.get(self.old["release_id"])
        self.service.relocate_device("box-1", "2026-07-10", "community-2")
        preview = self.service.preview("2026-07")
        self.assertFalse(preview["ready"] is False)
        new = self.service.publish("july-2", "2026-07", preview["fingerprint"],
                                   supersedes="july-1", reason="迁址重发布")
        after = self.service.releases.get(self.old["release_id"])
        self.assertEqual(before["content"], after["content"])
        self.assertEqual(before["fingerprint"], after["fingerprint"])
        self.assertEqual(after["superseded_by"], {"id": new["release_id"], "name": "july-2"})
        self.assertFalse(after["current"])
        self.assertEqual(self.service.releases.current("2026-07")["release"]["name"], "july-2")
        self.assertEqual([row["name"] for row in
                          self.service.releases.chain(new["release_id"])], ["july-2", "july-1"])
        diff = self.service.releases.compare(self.old["release_id"], new["release_id"])
        self.assertTrue(diff["supersedes"])
        self.assertEqual({(row["street_id"], row["material_id"]) for row in diff["changes"]},
                         {("street-1", "foam"), ("street-2", "foam")})

    def test_frozen_name_and_single_successor_are_enforced(self):
        self.service.relocate_device("box-1", "2026-07-10", "community-2")
        preview = self.service.preview("2026-07")
        with self.assertRaises(LakeError) as failure:
            self.service.publish("july-1", "2026-07", preview["fingerprint"])
        self.assertEqual(failure.exception.code, "release_conflict")
        self.service.publish("july-2", "2026-07", preview["fingerprint"], supersedes="july-1")
        with self.assertRaises(LakeError) as failure:
            self.service.publish("july-3", "2026-07", preview["fingerprint"], supersedes="july-1")
        self.assertEqual(failure.exception.code, "release_supersede")

    def test_frozen_lineage_keeps_revision_context(self):
        self.service.relocate_device("box-1", "2026-07-10", "community-2", reason="迁址")
        preview = self.service.preview("2026-07")
        new = self.service.publish("july-2", "2026-07", preview["fingerprint"],
                                   supersedes="july-1")
        lineage = self.service.lineage.release_row(new["release_id"], "street-2", "foam")
        self.assertEqual(lineage["row"]["street_id"], "street-2")
        self.assertEqual(lineage["frozen_dimension_revisions"][0]["action"], "relocate")
        self.assertEqual(lineage["frozen_dimension_revisions"][0]["reason"], "迁址")
        frozen = self.service.lineage.release_row(self.old["release_id"], "street-1", "foam")
        self.assertEqual(frozen["frozen_dimension_revisions"], [])
        self.assertFalse(frozen["current"])
        self.assertEqual(frozen["superseded_by"]["name"], "july-2")

    def test_supersede_requires_same_month(self):
        with self.assertRaises(LakeError) as failure:
            self.service.publish("june-1", "2026-06", "0" * 64, supersedes="july-1")
        self.assertEqual(failure.exception.code, "release_supersede")

    def test_retraction_unblocks_month_after_blocked_chain(self):
        self.service.register_dimension({"kind": "street", "external_id": "street-3",
                                         "name": "合成三街道", "valid_from": "2026-01-01",
                                         "valid_to": "2026-07-15"})
        self.service.register_dimension({"kind": "community", "external_id": "community-3",
                                         "name": "合成三社区", "valid_from": "2026-01-01",
                                         "attributes": {"street_id": "street-3"}})
        blocked = self.service.relocate_device("box-1", "2026-07-05", "community-3")
        self.assertEqual(blocked["status"], "applied_with_conflicts")
        self.assertFalse(self.service.preview("2026-07")["ready"])
        with self.assertRaises(LakeError) as failure:
            self.service.publish("july-2", "2026-07",
                                 self.service.preview("2026-07")["fingerprint"])
        self.assertEqual(failure.exception.code, "unresolved_month")
        self.service.retract_dimension("device", "box-1", version=2)
        unblocked = self.service.preview("2026-07")
        self.assertTrue(unblocked["ready"])
        self.assertEqual([item["status"] for item in unblocked["dimension_revisions"]],
                         ["retracted", "applied"])

    def test_revision_status_is_frozen_per_release(self):
        self.service.relocate_device("box-1", "2026-07-10", "community-2")
        first = self.service.publish("july-2", "2026-07",
                                     self.service.preview("2026-07")["fingerprint"],
                                     supersedes="july-1")
        self.service.retract_dimension("device", "box-1", version=2)
        second_preview = self.service.preview("2026-07")
        second = self.service.publish("july-3", "2026-07", second_preview["fingerprint"],
                                      supersedes="july-2")
        frozen_first = self.service.releases.get(first["release_id"])["content"]
        frozen_second = self.service.releases.get(second["release_id"])["content"]
        self.assertEqual([item["status"] for item in frozen_first["dimension_revisions"]],
                         ["applied"])
        self.assertEqual([item["status"] for item in frozen_second["dimension_revisions"]],
                         ["retracted", "applied"])


class RelocationDemoTests(unittest.TestCase):
    def test_demo_reaches_a_consistent_end_state(self):
        result = run_demo()
        self.assertEqual(result["result"], "ok")
        self.assertFalse(result["frozen_release"]["current"])
        self.assertEqual(result["current_release"]["name"], "july-2")
        self.assertEqual(result["reassigned_fact_ids"], [4])
        self.assertEqual(result["revision_actions"], ["relocate", "retract", "relocate"])


if __name__ == "__main__":
    unittest.main()
