"""End-to-end sample: late relocation notices meeting frozen monthly releases.

Run with ``python -m looplake.relocation_demo``. Everything is synthetic.

Storyline:
  1. July is already closed out. ``box-1`` deposits were attributed to
     street-1 and the July report was published as ``july-1``.
  2. The operations team later reports that box-1 was physically moved to
     community-2 (street-2) on 2026-07-10, i.e. *before* part of the July
     volume. The notice arrives late and is re-sent by two overlapping feeds.
  3. A deposit that happened before the move but landed in quarantine (its
     material was unknown at the time) is retried after the move notice is in.
  4. The relocation is withdrawn, re-applied with a different effective date,
     and finally confirmed. None of this rewrites ``july-1``.
  5. July is re-published as ``july-2`` with an explicit supersession link.
"""
from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from .fixtures import balanced_batch, event, seed_dimensions
from .model import LakeError
from .service import LakeService


def _expect_error(code: str, action):
    try:
        action()
    except LakeError as error:
        if error.code != code:
            raise RuntimeError(f"expected {code}, got {error.code}") from error
        return error.code
    raise RuntimeError(f"expected {code}, action succeeded")


def run_demo():
    with TemporaryDirectory() as folder:
        service = LakeService(Path(folder) / "relocation.sqlite3")
        seed_dimensions(service)

        def ingest(rows):
            outcomes = [service.ingest(row) for row in rows]
            if any(row["state"] != "normalized" for row in outcomes):
                raise RuntimeError(f"event did not normalize: {outcomes}")
            return outcomes

        # --- July as originally understood: everything on street-1. ---------
        early = ingest(balanced_batch("batch-july-early", "e",
                                      happened_at="2026-07-05T09:00:00+08:00"))
        late = ingest(balanced_batch("batch-july-late", "l",
                                     happened_at="2026-07-20T09:00:00+08:00"))
        july_one = service.preview("2026-07")
        if not july_one["ready"]:
            raise RuntimeError("initial July preview is not releaseable")
        frozen = service.publish("july-1", "2026-07", july_one["fingerprint"])
        frozen_fingerprint = service.releases.get(frozen["release_id"])["fingerprint"]

        # --- A deposit before the move quarantines on an unknown material. --
        pending_rows = balanced_batch("batch-july-fiber", "f", material_id="future-fiber",
                                      payout=None, happened_at="2026-07-08T08:00:00+08:00")
        pending = [service.ingest(row) for row in pending_rows]
        if [row["state"] for row in pending] != ["quarantined", "quarantined"]:
            raise RuntimeError("expected the pre-move fiber batch to be quarantined")

        # --- The late relocation notice, delivered twice concurrently. ------
        moved = service.relocate_device(
            "box-1", "2026-07-10", "community-2",
            revision_key="notice-2026-07-box-1", reason="运营组迁址清单补录")
        repeated = service.relocate_device(
            "box-1", "2026-07-10", "community-2",
            revision_key="notice-2026-07-box-1", reason="运营组迁址清单补录")
        if moved["id"] != repeated["id"]:
            raise RuntimeError("duplicate relocation notice created two revisions")
        if sorted(moved["changed_fact_ids"]) != [late[0]["fact_id"]]:
            raise RuntimeError("only the post-move deposit should be reassigned")
        _expect_error("dimension_conflict", lambda: service.relocate_device(
            "box-1", "2026-07-10", "community-1", reason="互相矛盾的迁址通知"))

        # Effective-time attribution: the deposit from before the move stays on
        # street-1, the one after it moves to street-2. The late-arriving
        # deposit is still quarantined.
        if service.store.fact(early[0]["fact_id"])["street_id"] != "street-1":
            raise RuntimeError("pre-move deposit changed street")
        if service.store.fact(late[0]["fact_id"])["street_id"] != "street-2":
            raise RuntimeError("post-move deposit did not move to street-2")
        if service.quality.month("2026-07")["ready"]:
            raise RuntimeError("quarantined deposit must block the month")

        # --- Resolve the quarantine, then retry. ---------------------------
        service.register_dimension({"kind": "material", "external_id": "future-fiber",
                                    "name": "合成纤维", "valid_from": "2026-01-01"})
        retried = [service.retry(row["ingestion_id"]) for row in pending]
        if [row["state"] for row in retried] != ["normalized", "normalized"]:
            raise RuntimeError("retry did not normalize the quarantined fiber batch")
        # It happened before the move, so retrying it after the notice landed
        # must still attribute it to street-1.
        fiber_fact = retried[0]["fact_id"]
        if service.store.fact(fiber_fact)["street_id"] != "street-1":
            raise RuntimeError("retried late deposit ignored effective-time relocation")

        # --- Withdraw, re-apply at a corrected date, confirm. --------------
        # The first notice had the wrong date; sync should not be able to keep
        # both, so the mistaken version is retracted before re-application.
        withdrawn = service.retract_dimension("device", "box-1", version=2,
                                              revision_key="withdraw-box-1-v2",
                                              reason="迁址日期录错，先撤回")
        if service.store.fact(late[0]["fact_id"])["street_id"] != "street-1":
            raise RuntimeError("retraction did not restore the original attribution")

        service.relocate_device("box-1", "2026-07-15", "community-2",
                                revision_key="notice-2026-07-box-1-v3",
                                reason="按门禁记录修正迁址日")
        if service.store.fact(early[0]["fact_id"])["street_id"] != "street-1":
            raise RuntimeError("fact before the corrected move date moved incorrectly")
        if service.store.fact(late[0]["fact_id"])["street_id"] != "street-2":
            raise RuntimeError("fact after the corrected move date did not move")
        if service.store.fact(fiber_fact)["street_id"] != "street-1":
            raise RuntimeError("pre-move retried fact moved incorrectly")

        # --- Re-publish without touching july-1. ---------------------------
        before_republish = service.releases.get(frozen["release_id"])
        july_two = service.preview("2026-07")
        if not july_two["ready"]:
            raise RuntimeError("July is still not releaseable after relocation")
        published = service.publish("july-2", "2026-07", july_two["fingerprint"],
                                    supersedes="july-1", reason="迁址后重发布")
        after_republish = service.releases.get(frozen["release_id"])
        if after_republish["content"] != before_republish["content"]:
            raise RuntimeError("old release content was rewritten")
        if after_republish["fingerprint"] != frozen_fingerprint:
            raise RuntimeError("old release fingerprint changed")
        _expect_error("release_supersede", lambda: service.publish(
            "july-3", "2026-07", july_two["fingerprint"], supersedes="july-1",
            reason="重复指向同一前版"))
        _expect_error("release_conflict", lambda: service.publish(
            "july-1", "2026-07", july_two["fingerprint"], reason="静默改写冻结版"))

        attribution = service.query.attribution("2026-07")
        diff = service.releases.compare(frozen["release_id"], published["release_id"])
        current = service.releases.current("2026-07")
        lineage = service.lineage.release_row(published["release_id"], "street-2", "foam")
        return {
            "result": "ok",
            "frozen_release": {"id": frozen["release_id"], "fingerprint": frozen_fingerprint,
                               "current": after_republish["current"]},
            "current_release": current["release"],
            "july_rows_before": before_republish["content"]["mart"]["rows"],
            "july_rows_after": july_two["mart"]["rows"],
            "reassigned_fact_ids": attribution["reassigned_fact_ids"],
            "attribution": attribution["rows"],
            "release_diff_keys": [{"street_id": change["street_id"],
                                   "material_id": change["material_id"]}
                                  for change in diff["changes"]],
            "frozen_lineage_revisions": lineage["frozen_dimension_revisions"],
            "revision_actions": [revision["action"]
                                 for revision in service.dimensions.list_revisions("device", "box-1")],
        }


if __name__ == "__main__":
    import json
    print(json.dumps(run_demo(), ensure_ascii=False, sort_keys=True, indent=2))
