from __future__ import annotations

import json

from .mart import Mart
from .model import LakeError, digest, key, now, required
from .reconcile import Reconciliation
from .store import Store


FROZEN_FIELDS = ("month", "ready", "mart", "reconciliation", "inventory",
                 "quarantined_ingestion_ids", "dimension_conflicts", "dimension_revisions")


class Release:
    def __init__(self, store: Store, mart: Mart, reconciliation: Reconciliation, inventory=None):
        self.store, self.mart, self.reconciliation = store, mart, reconciliation
        self.inventory = inventory

    def preview(self, month: str):
        report = self.mart.month(month)
        checked = self.reconciliation.month(month)
        inventory = self.inventory.month(month) if self.inventory else None
        with self.store.connect() as conn:
            quarantined = self.store.all(conn, "SELECT id FROM ingestions WHERE state='quarantined' AND substr(json_extract(raw_json,'$.happened_at'),1,7)=?",
                                         (month,))
        current = self.store.current_release(month)
        revisions = self._month_revisions(month, report)
        conflicts = [{"revision_id": revision["revision_id"], "fact_ids": revision["blocked_fact_ids"],
                      "issues": revision["blocked"]}
                     for revision in revisions if revision["blocked_fact_ids"]]
        body = {"month": month,
                "ready": report["ready"] and checked["ready"]
                and (inventory is None or inventory["ready"]) and not quarantined
                and not conflicts,
                "mart": report, "reconciliation": checked, "inventory": inventory,
                "quarantined_ingestion_ids": [row["id"] for row in quarantined],
                "dimension_conflicts": conflicts,
                "dimension_revisions": revisions}
        # The fingerprint covers exactly the frozen body, so it can be re-derived
        # from a stored release; informational fields such as current_release are
        # excluded because they change after supersession without data changing.
        return {**body, "current_release": current, "fingerprint": self.fingerprint_of(body)}

    @staticmethod
    def fingerprint_of(content: dict):
        return digest({name: content[name] for name in FROZEN_FIELDS if name in content})

    def _month_revisions(self, month: str, report: dict):
        fact_ids = {fact_id for row in report["rows"] for fact_id in row["fact_ids"]}
        facts = self.store.facts("WHERE substr(day,1,7)=?", (month,))
        fact_ids.update(fact["id"] for fact in facts)
        revisions = []
        for row in self.store.revisions():
            changed = set(row["changed_fact_ids"])
            affected = set(row["affected_fact_ids"])
            blocked = ([item for item in row["blocked_json"] if item["fact_id"] in fact_ids]
                       if row["status"] != "retracted" else [])
            touched = sorted((affected | changed) & fact_ids)
            if not touched and not blocked:
                continue
            revisions.append({"revision_id": row["id"], "kind": row["kind"],
                              "external_id": row["external_id"], "action": row["action"],
                              "effective_from": row["effective_from"], "status": row["status"],
                              "reason": row["reason"], "fact_ids": touched,
                              "reassigned_fact_ids": sorted(changed & fact_ids),
                              "blocked_fact_ids": [item["fact_id"] for item in blocked],
                              "blocked": blocked})
        return revisions

    def publish(self, name: str, month: str, expected_fingerprint: str,
                supersedes: str | int | None = None, reason: str | None = None):
        name = key(name, "name")
        if not isinstance(expected_fingerprint, str) or len(expected_fingerprint) != 64:
            raise LakeError("invalid_fingerprint", "expected_fingerprint must be SHA-256")
        if reason is not None:
            reason = required(reason, "reason", 500)
        with self.store.write() as conn:
            superseded_id = None
            if supersedes is not None:
                previous = self.store.release(supersedes)
                if previous["project_month"] != month:
                    raise LakeError("release_supersede", "superseded release is for another month", 409)
                if previous["superseded_by"]:
                    raise LakeError("release_supersede", "release is already superseded", 409)
                if previous["fingerprint"] == self._fingerprint(month):
                    raise LakeError("release_supersede", "month content is unchanged", 409)
                superseded_id = previous["id"]
            preview = self.preview(month)
            if not preview["ready"]:
                raise LakeError("unresolved_month", "month has unresolved material batches", 409)
            if expected_fingerprint != preview["fingerprint"]:
                raise LakeError("stale_preview", "month changed since preview", 409)
            previous = self.store.one(conn, "SELECT * FROM releases WHERE name=?", (name,))
            if previous:
                same = previous["fingerprint"] == preview["fingerprint"]
                if not same or previous["project_month"] != month:
                    raise LakeError("release_conflict", "release name already has different content", 409)
                return {"release_id": previous["id"], "name": name,
                        "fingerprint": previous["fingerprint"], "created": False}
            if superseded_id is not None:
                clash = self.store.one(conn, "SELECT id,name FROM releases WHERE supersedes_id=?",
                                       (superseded_id,))
                if clash:
                    raise LakeError("release_supersede", "release already has a successor", 409)
            cursor = conn.execute("INSERT INTO releases(name,project_month,fingerprint,content,created_at,supersedes_id,reason) VALUES(?,?,?,?,?,?,?)",
                                  (name, month, preview["fingerprint"],
                                   json.dumps(preview, ensure_ascii=False, sort_keys=True), now(),
                                   superseded_id, reason))
            self.store.audit(conn, "release_published", name, {"id": cursor.lastrowid,
                                                                  "fingerprint": preview["fingerprint"],
                                                                  "supersedes": superseded_id,
                                                                  "reason": reason})
            if superseded_id is not None:
                self.store.audit(conn, "release_superseded", name,
                                 {"release_id": superseded_id, "by": cursor.lastrowid})
        return {"release_id": cursor.lastrowid, "name": name,
                "fingerprint": preview["fingerprint"], "created": True,
                "supersedes_id": superseded_id}

    def _fingerprint(self, month: str):
        return self.preview(month)["fingerprint"]

    def get(self, identifier: int | str):
        return self.store.release(identifier)

    def compare(self, left: int | str, right: int | str):
        first, second = self.get(left), self.get(right)
        before = {(row["street_id"], row["material_id"]): row
                  for row in first["content"]["mart"]["rows"]}
        after = {(row["street_id"], row["material_id"]): row
                 for row in second["content"]["mart"]["rows"]}
        changes = []
        for key_tuple in sorted(set(before) | set(after)):
            a, b = before.get(key_tuple), after.get(key_tuple)
            if a != b:
                changes.append({"street_id": key_tuple[0], "material_id": key_tuple[1],
                                "before": a, "after": b})
        return {"left_id": first["id"], "right_id": second["id"],
                "left_fingerprint": first["fingerprint"],
                "right_fingerprint": second["fingerprint"],
                "supersedes": second["supersedes_id"] == first["id"],
                "same_content": first["fingerprint"] == second["fingerprint"],
                "changes": changes}

    def chain(self, identifier: int | str):
        return self.store.release_chain(self.get(identifier)["id"])

    def current(self, month: str):
        current = self.store.current_release(month)
        return {"month": month, "release": current}
