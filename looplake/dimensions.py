from __future__ import annotations

from datetime import date
import json

from .model import Dimension, LakeError, key, now
from .store import Store


class Dimensions:
    def __init__(self, store: Store):
        self.store = store

    def register_source(self, source: str, channel: str, expected_version: int | None = None):
        source = key(source, "source")
        if channel not in {"smart_box", "scale", "station", "payment"}:
            raise LakeError("invalid_channel", "source channel unsupported")
        with self.store.write() as conn:
            old = self.store.one(conn, "SELECT * FROM source_registry WHERE source=?", (source,))
            actual = old["version"] if old else 0
            if expected_version is not None and expected_version != actual:
                raise LakeError("stale_source", "source version changed", 409)
            if old and old["channel"] != channel:
                raise LakeError("source_channel", "source channel cannot change", 409)
            version = actual + 1
            if old:
                conn.execute("UPDATE source_registry SET active=1,version=? WHERE source=?", (version, source))
            else:
                conn.execute("INSERT INTO source_registry VALUES(?,?,?,?,datetime('now'))",
                             (source, channel, 1, version))
            self.store.audit(conn, "source_registered", source, {"channel": channel, "version": version})
        return {"source": source, "channel": channel, "active": True, "version": version}

    def disable_source(self, source: str, expected_version: int):
        source = key(source, "source")
        with self.store.write() as conn:
            old = self.store.one(conn, "SELECT * FROM source_registry WHERE source=?", (source,))
            if old is None:
                raise LakeError("unknown_source", "source is not registered", 404)
            if old["version"] != expected_version:
                raise LakeError("stale_source", "source version changed", 409)
            conn.execute("UPDATE source_registry SET active=0,version=version+1 WHERE source=?", (source,))
            self.store.audit(conn, "source_disabled", source, {"version": expected_version + 1})
        return {"source": source, "active": False, "version": expected_version + 1}

    def put(self, data: dict, expected_version: int | None = None):
        dimension = Dimension.parse(data)
        with self.store.write() as conn:
            old = self.store.one(conn, "SELECT MAX(version) AS v FROM dimension_versions WHERE kind=? AND external_id=?",
                                 (dimension.kind, dimension.external_id))
            actual = old["v"] or 0
            if expected_version is not None and expected_version != actual:
                raise LakeError("stale_dimension", "dimension version changed", 409)
            overlaps = self.store.all(conn, "SELECT id FROM dimension_versions WHERE kind=? AND external_id=? AND retracted_at IS NULL AND (valid_to IS NULL OR valid_to>?) AND (? IS NULL OR valid_from<?)",
                                      (dimension.kind, dimension.external_id, dimension.valid_from,
                                       dimension.valid_to, dimension.valid_to))
            if overlaps:
                raise LakeError("dimension_overlap", "effective periods overlap", 409)
            attrs = dimension.attributes
            if dimension.kind in {"community", "device"}:
                parent = "street_id" if dimension.kind == "community" else "community_id"
                if parent not in attrs:
                    raise LakeError("missing_parent", f"{parent} required")
                parent_kind = "street" if dimension.kind == "community" else "community"
                parent_row = self._resolve(conn, parent_kind, key(attrs[parent], parent), dimension.valid_from)
                if dimension.valid_to and parent_row["valid_to"] and parent_row["valid_to"] < dimension.valid_to:
                    raise LakeError("parent_range", "parent period ends before child")
                if parent_row["valid_from"] > dimension.valid_from:
                    raise LakeError("parent_range", "parent starts after child")
            version = actual + 1
            cursor = conn.execute("INSERT INTO dimension_versions(kind,external_id,name,valid_from,valid_to,attributes,version,created_at) VALUES(?,?,?,?,?,?,?,datetime('now'))",
                                  (dimension.kind, dimension.external_id, dimension.name, dimension.valid_from,
                                   dimension.valid_to, json.dumps(attrs, ensure_ascii=False, sort_keys=True), version))
            self.store.audit(conn, "dimension_added", f"{dimension.kind}:{dimension.external_id}",
                             {"id": cursor.lastrowid, "version": version, "valid_from": dimension.valid_from})
        return {"id": cursor.lastrowid, "kind": dimension.kind,
                "external_id": dimension.external_id, "version": version}

    def _resolve(self, conn, kind: str, external_id: str, day: str):
        rows = self.store.all(conn, "SELECT * FROM dimension_versions WHERE kind=? AND external_id=? AND retracted_at IS NULL AND valid_from<=? AND (valid_to IS NULL OR valid_to>?)",
                              (kind, external_id, day, day))
        if len(rows) != 1:
            raise LakeError("unknown_dimension", f"{kind}:{external_id} not valid on {day}", 409)
        return rows[0]

    def relocate(self, kind: str, external_id: str, effective_from: str,
                 parents: dict | None = None, name: str | None = None,
                 expected_version: int | None = None, revision_key: str | None = None,
                 reason: str | None = None):
        """Split a version at a business-effective date and attach a new parent.

        Late corrections are expressed as a relocation effective at the moment the
        move actually happened, never as a fresh row starting when the office got
        around to reporting it. Non-overlap is enforced by construction: the old
        version is closed the day before the split and the new version starts on it.
        """
        kind = key(kind, "kind")
        if kind not in {"community", "device"}:
            raise LakeError("invalid_kind", "only community and device relocation is supported")
        external_id = key(external_id, "external_id")
        effective_from = self._day(effective_from, "effective_from")
        revision_key = key(revision_key, "revision_key") if revision_key is not None else None
        parent_field = "street_id" if kind == "community" else "community_id"
        if not parents or list(parents) != [parent_field]:
            raise LakeError("invalid_parent", f"{kind} relocation only accepts {parent_field}")
        with self.store.write() as conn:
            replay = self._replay(conn, revision_key, kind, external_id, "relocate",
                                  effective_from, None, {"parents": parents, "name": name})
            if replay is not None:
                return replay
            rows = self.store.all(conn, "SELECT * FROM dimension_versions WHERE kind=? AND external_id=? AND retracted_at IS NULL ORDER BY valid_from,id",
                                  (kind, external_id))
            if not rows:
                raise LakeError("unknown_dimension", f"{kind}:{external_id} is not registered", 404)
            actual = self._latest_version(conn, kind, external_id)
            if expected_version is not None and expected_version != actual:
                raise LakeError("stale_dimension", "dimension version changed", 409)
            covering = [row for row in rows
                        if row["valid_from"] <= effective_from and (row["valid_to"] is None or row["valid_to"] > effective_from)]
            if not covering:
                raise LakeError("unknown_dimension", f"{kind}:{external_id} is not valid on {effective_from}", 409)
            if len(covering) > 1:
                raise LakeError("ambiguous_dimension", "effective time has overlapping versions", 409)
            old = covering[0]
            old_attrs = json.loads(old["attributes"])
            new_attrs = dict(old_attrs)
            if parents is not None:
                parent_kind = "street" if kind == "community" else "community"
                parent = key(parents[parent_field], parent_field)
                parent_row = self._resolve(conn, parent_kind, parent, effective_from)
                if old["valid_to"] and parent_row["valid_to"] and parent_row["valid_to"] < old["valid_to"]:
                    raise LakeError("parent_range", "parent period ends before child")
                new_attrs[parent_field] = parent
            else:
                parent = None
            new_name = old["name"] if name is None else self._text(name, "name")
            earlier = self._relocation_at(conn, kind, external_id, effective_from)
            if earlier is not None:
                if json.loads(earlier["after_json"]).get("attributes") == new_attrs:
                    return self._revision_from_row(earlier)
                raise LakeError("dimension_conflict",
                                "a different relocation is already effective on this date", 409)
            if new_attrs == old_attrs and new_name == old["name"]:
                raise LakeError("no_change", "relocation does not change the dimension", 409)
            if old["valid_from"] == effective_from:
                # The relocation starts exactly at the version's first day: revise in
                # place instead of creating a zero-length predecessor.
                new_version = actual + 1
                conn.execute("UPDATE dimension_versions SET retracted_at=? WHERE id=?", (now(), old["id"]))
                cursor = conn.execute("INSERT INTO dimension_versions(kind,external_id,name,valid_from,valid_to,attributes,version,created_at) VALUES(?,?,?,?,?,?,?,?)",
                                      (kind, external_id, new_name, old["valid_from"], old["valid_to"],
                                       json.dumps(new_attrs, ensure_ascii=False, sort_keys=True),
                                       new_version, now()))
                closed = None
            else:
                closed = self._close_version(conn, old, effective_from)
                new_version = actual + 1
                cursor = conn.execute("INSERT INTO dimension_versions(kind,external_id,name,valid_from,valid_to,attributes,version,created_at) VALUES(?,?,?,?,?,?,?,?)",
                                      (kind, external_id, new_name, effective_from, old["valid_to"],
                                       json.dumps(new_attrs, ensure_ascii=False, sort_keys=True),
                                       new_version, now()))
            new_row = self.store.one(conn, "SELECT * FROM dimension_versions WHERE id=?", (cursor.lastrowid,))
            repair = self._resync(conn, kind, external_id)
            changed = repair["changed"]
            revision_id = self._record(conn, kind, external_id, "relocate", effective_from, None,
                                       {"version_id": new_row["id"], "split_from_id": old["id"],
                                        "supersedes_id": old["id"] if closed else None},
                                       {"version": old, "attributes": old_attrs},
                                       {"version": new_row, "attributes": new_attrs},
                                       revision_key, reason, repair,
                                       request={"parents": parents, "name": name})
            self.store.audit(conn, "dimension_relocated", f"{kind}:{external_id}",
                             {"revision_id": revision_id, "effective_from": effective_from,
                              "parent_field": parent_field, "parent": parent,
                              "version": new_version, "changed_fact_ids": changed})
        return self.revision(revision_id)

    def retract(self, kind: str, external_id: str, version: int | None = None,
                effective_from: str | None = None, expected_version: int | None = None,
                revision_key: str | None = None, reason: str | None = None):
        """Withdraw a mistaken version and restore the period it replaced.

        Retraction never deletes history: the row keeps its attributes and gets a
        retracted_at stamp, and all facts are re-resolved against what remains.
        """
        kind = key(kind, "kind")
        external_id = key(external_id, "external_id")
        revision_key = key(revision_key, "revision_key") if revision_key is not None else None
        with self.store.write() as conn:
            replay = self._replay(conn, revision_key, kind, external_id, "retract",
                                  effective_from, version, {})
            if replay is not None:
                return replay
            rows = self.store.all(conn, "SELECT * FROM dimension_versions WHERE kind=? AND external_id=? AND retracted_at IS NULL ORDER BY version",
                                  (kind, external_id))
            if not rows:
                raise LakeError("unknown_dimension", f"{kind}:{external_id} has no active versions", 404)
            actual = self._latest_version(conn, kind, external_id)
            if expected_version is not None and expected_version != actual:
                raise LakeError("stale_dimension", "dimension version changed", 409)
            if version is None:
                target = rows[-1]
            else:
                matches = [row for row in rows if row["version"] == version]
                if not matches:
                    raise LakeError("unknown_version", "dimension version is not active", 404)
                target = matches[0]
            if len(rows) == 1 and self._find_retracted_twin(conn, rows[0]) is None:
                raise LakeError("retract_last_period", "cannot retract the only effective period", 409)
            restored = self._restore_retracted_twin(conn, target)
            if restored is None:
                restored = self._restore_predecessor(conn, target)
            conn.execute("UPDATE dimension_versions SET retracted_at=? WHERE id=?", (now(), target["id"]))
            repair = self._resync(conn, kind, external_id)
            revision_id = self._record(conn, kind, external_id, "retract", target["valid_from"], target["id"],
                                       {"version_id": target["id"], "version": target["version"],
                                        "restored_id": restored["id"]},
                                       {"version": target, "attributes": json.loads(target["attributes"])},
                                       {"restored": restored},
                                       revision_key, reason, repair, request={})
            self._retire_origin_revision(conn, kind, external_id, target["id"])
            self.store.audit(conn, "dimension_retracted", f"{kind}:{external_id}",
                             {"revision_id": revision_id, "version": target["version"],
                              "changed_fact_ids": repair["changed"]})
        return self.revision(revision_id)

    def correct(self, kind: str, external_id: str, as_of: str, *,
                name: str | None = None, parents: dict | None = None,
                expected_version: int | None = None, revision_key: str | None = None,
                reason: str | None = None):
        """Amend the version effective on as_of, keeping its original effective range."""
        kind = key(kind, "kind")
        external_id = key(external_id, "external_id")
        as_of = self._day(as_of, "as_of")
        revision_key = key(revision_key, "revision_key") if revision_key is not None else None
        parent_field = "street_id" if kind == "community" else "community_id"
        if kind not in {"community", "device"}:
            raise LakeError("invalid_kind", "only community and device correction is supported")
        with self.store.write() as conn:
            replay = self._replay(conn, revision_key, kind, external_id, "correct",
                                  as_of, None, {"parents": parents, "name": name})
            if replay is not None:
                return replay
            target = self._resolve(conn, kind, external_id, as_of)
            actual = self._latest_version(conn, kind, external_id)
            if expected_version is not None and expected_version != actual:
                raise LakeError("stale_dimension", "dimension version changed", 409)
            old_attrs = json.loads(target["attributes"])
            new_attrs = dict(old_attrs)
            parent = None
            if parents:
                if parent_field not in parents or len(parents) != 1:
                    raise LakeError("invalid_parent", f"{kind} correction only accepts {parent_field}")
                parent = key(parents[parent_field], parent_field)
                self._resolve(conn, "street" if kind == "community" else "community", parent, target["valid_from"])
                new_attrs[parent_field] = parent
            new_name = target["name"] if name is None else self._text(name, "name")
            if new_attrs == old_attrs and new_name == target["name"]:
                raise LakeError("no_change", "correction does not change the dimension", 409)
            conn.execute("UPDATE dimension_versions SET retracted_at=? WHERE id=?", (now(), target["id"]))
            new_version = actual + 1
            cursor = conn.execute("INSERT INTO dimension_versions(kind,external_id,name,valid_from,valid_to,attributes,version,created_at) VALUES(?,?,?,?,?,?,?,?)",
                                  (kind, external_id, new_name, target["valid_from"], target["valid_to"],
                                   json.dumps(new_attrs, ensure_ascii=False, sort_keys=True),
                                   new_version, now()))
            repair = self._resync(conn, kind, external_id)
            revision_id = self._record(conn, kind, external_id, "correct", target["valid_from"], target["id"],
                                       {"version_id": cursor.lastrowid, "supersedes_id": target["id"]},
                                       {"version": target, "attributes": old_attrs},
                                       {"version_id": cursor.lastrowid, "version": new_version,
                                        "attributes": new_attrs},
                                       revision_key, reason, repair,
                                       request={"parents": parents, "name": name})
            self.store.audit(conn, "dimension_corrected", f"{kind}:{external_id}",
                             {"revision_id": revision_id, "as_of": as_of,
                              "changed_fact_ids": repair["changed"]})
        return self.revision(revision_id)

    def revision(self, revision_id: int):
        with self.store.connect() as conn:
            row = self.store.one(conn, "SELECT * FROM dimension_revisions WHERE id=?", (revision_id,))
        if row is None:
            raise LakeError("unknown_revision", "dimension revision not found", 404)
        result = self._revision_from_row(row)
        result["changed_fact_ids"] = sorted(result["changed_fact_ids"])
        result["affected_fact_ids"] = sorted(result["affected_fact_ids"])
        result["blocked_fact_ids"] = [item["fact_id"] for item in result["blocked_json"]]
        return result

    # --- internals -------------------------------------------------------

    def _retire_origin_revision(self, conn, kind, external_id, version_row_id):
        """Mark the revision that produced a now-retracted version as withdrawn."""
        for row in self.store.all(conn, "SELECT * FROM dimension_revisions WHERE kind=? AND external_id=? AND action IN ('relocate','correct') AND status<>'retracted' ORDER BY id",
                                  (kind, external_id)):
            origin = json.loads(row["target_json"]).get("version_id")
            if origin == version_row_id:
                conn.execute("UPDATE dimension_revisions SET status='retracted' WHERE id=?", (row["id"],))

    def _latest_version(self, conn, kind: str, external_id: str):
        row = self.store.one(conn, "SELECT MAX(version) AS v FROM dimension_versions WHERE kind=? AND external_id=?",
                             (kind, external_id))
        return row["v"] or 0

    @staticmethod
    def _day(value, name: str):
        from .model import calendar_day
        return calendar_day(value, name)

    @staticmethod
    def _text(value, name: str):
        from .model import required
        return required(value, name, 200)

    @staticmethod
    def _shift(day: str, days: int):
        from datetime import timedelta
        return (date.fromisoformat(day) + timedelta(days=days)).isoformat()

    def _close_version(self, conn, row, split_day: str):
        prior_to = None if row["valid_to"] is None else row["valid_to"]
        if prior_to is not None and split_day >= prior_to:
            raise LakeError("invalid_effective_from",
                            "effective_from is outside the version being relocated", 409)
        conn.execute("UPDATE dimension_versions SET valid_to=? WHERE id=?",
                     (split_day, row["id"]))
        updated = self.store.one(conn, "SELECT * FROM dimension_versions WHERE id=?", (row["id"],))
        return updated

    def _find_retracted_twin(self, conn, target):
        rows = self.store.all(conn, "SELECT * FROM dimension_versions WHERE kind=? AND external_id=? AND retracted_at IS NOT NULL AND version<? AND valid_from=? AND valid_to IS ? ORDER BY version DESC LIMIT 1",
                              (target["kind"], target["external_id"], target["version"],
                               target["valid_from"], target["valid_to"]))
        if not rows:
            return None
        twin = rows[0]
        overlap = self.store.all(conn, "SELECT id FROM dimension_versions WHERE kind=? AND external_id=? AND retracted_at IS NULL AND id<>? AND (valid_to IS NULL OR valid_to>?) AND (? IS NULL OR valid_from<?)",
                                 (target["kind"], target["external_id"], target["id"],
                                  twin["valid_from"], twin["valid_to"], twin["valid_to"]))
        return None if overlap else twin

    def _relocation_at(self, conn, kind, external_id, effective_from):
        rows = self.store.all(conn, "SELECT r.* FROM dimension_revisions r WHERE r.kind=? AND r.external_id=? AND r.action='relocate' AND r.effective_from=? ORDER BY r.id DESC",
                              (kind, external_id, effective_from))
        for row in rows:
            version_row = self.store.one(conn, "SELECT retracted_at FROM dimension_versions WHERE id=?",
                                         (json.loads(row["target_json"])["version_id"],))
            if version_row is not None and version_row["retracted_at"] is None:
                return row
        return None

    @staticmethod
    def _revision_from_row(row: dict):
        result = dict(row)
        for field in ("target_json", "before_json", "after_json",
                      "affected_fact_ids", "changed_fact_ids", "blocked_json",
                      "request_json"):
            result[field] = json.loads(result[field])
        return result

    def _restore_retracted_twin(self, conn, target):
        """Reopen the version a correction replaced over the exact same range."""
        twin = self._find_retracted_twin(conn, target)
        if twin is None:
            return None
        conn.execute("UPDATE dimension_versions SET retracted_at=NULL WHERE id=?", (twin["id"],))
        return self.store.one(conn, "SELECT * FROM dimension_versions WHERE id=?", (twin["id"],))

    def _restore_predecessor(self, conn, target):
        """Reopen the version immediately preceding a retracted one, when contiguous."""
        rows = self.store.all(conn, "SELECT * FROM dimension_versions WHERE kind=? AND external_id=? AND id<>? AND valid_from<? ORDER BY valid_from DESC,id DESC LIMIT 1",
                              (target["kind"], target["external_id"], target["id"], target["valid_from"]))
        if not rows:
            raise LakeError("retract_orphaned_period",
                            "retracted version has no predecessor to restore", 409)
        previous = rows[0]
        if previous["valid_to"] != target["valid_from"]:
            raise LakeError("retract_gap",
                            "predecessor does not end exactly where the retracted version starts", 409)
        conn.execute("UPDATE dimension_versions SET valid_to=? WHERE id=?",
                     (target["valid_to"], previous["id"]))
        restored = self.store.one(conn, "SELECT * FROM dimension_versions WHERE id=?", (previous["id"],))
        # A reopened period must not swallow versions that started inside it.
        later = self.store.all(conn, "SELECT id,valid_from FROM dimension_versions WHERE kind=? AND external_id=? AND retracted_at IS NULL AND id NOT IN (?,?) AND valid_from>=? AND valid_from<?",
                               (target["kind"], target["external_id"], previous["id"], target["id"],
                                previous["valid_from"], target["valid_to"] or "9999-12-31"))
        if later:
            raise LakeError("retract_gap", "restoring the predecessor would overlap later periods", 409)
        return restored

    def _replay(self, conn, revision_key, kind, external_id, action, effective_from,
                retracts_id, target: dict):
        if revision_key is None:
            return None
        existing = self.store.one(conn, "SELECT * FROM dimension_revisions WHERE revision_key=?",
                                  (revision_key,))
        if existing is None:
            return None
        same = (existing["kind"] == kind and existing["external_id"] == external_id
                and existing["action"] == action
                and existing["effective_from"] == effective_from
                and existing["retracts_id"] == retracts_id
                and existing["request_json"] == json.dumps(target, ensure_ascii=False, sort_keys=True))
        if not same:
            raise LakeError("revision_conflict",
                            "revision_key already identified a different revision", 409)
        return self.revision(existing["id"])

    def _record(self, conn, kind, external_id, action, effective_from, retracts_id,
                target, before, after, revision_key, reason, repair, request=None):
        revision_id = conn.execute(
            "INSERT INTO dimension_revisions(kind,external_id,action,effective_from,retracts_id,target_json,before_json,after_json,affected_fact_ids,changed_fact_ids,blocked_json,request_json,status,revision_key,reason,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (kind, external_id, action, effective_from, retracts_id,
             json.dumps(target, ensure_ascii=False, sort_keys=True, default=str),
             json.dumps(before, ensure_ascii=False, sort_keys=True, default=str),
             json.dumps(after, ensure_ascii=False, sort_keys=True, default=str),
             json.dumps(repair["affected"], ensure_ascii=False),
             json.dumps(repair["changed"], ensure_ascii=False),
             json.dumps(repair["blocked"], ensure_ascii=False),
             json.dumps(target if request is None else request,
                        ensure_ascii=False, sort_keys=True, default=str),
             "applied" if not repair["blocked"] else "applied_with_conflicts",
             revision_key, reason, now())).lastrowid
        for item in repair["reassignments"]:
            conn.execute("INSERT INTO fact_reassignments(fact_id,revision_id,old_street_id,old_community_id,new_street_id,new_community_id,old_dimensions,new_dimensions,at) VALUES(?,?,?,?,?,?,?,?,?)",
                         (item["fact_id"], revision_id, item["old_street_id"], item["old_community_id"],
                          item["new_street_id"], item["new_community_id"],
                          json.dumps(item["old_dimensions"], ensure_ascii=False, sort_keys=True),
                          json.dumps(item["new_dimensions"], ensure_ascii=False, sort_keys=True), now()))
        return revision_id

    def _resync(self, conn, kind: str, external_id: str):
        """Re-resolve normalized deposits against current dimensions.

        Deposit facts pin the device -> community -> street chain, so they must be
        rebuilt after any dimension revision. Every deposit is re-checked, but the
        revision records as *affected* only the facts whose old or new chain
        involves the revised dimension. A fact whose new chain is missing or
        ambiguous keeps its last good assignment, is reported as blocked, and does
        not silently move into an unrelated street.
        """
        facts = self.store.all(conn, "SELECT * FROM facts WHERE event_type='deposit' AND state='active' ORDER BY id")
        affected, changed, blocked, reassignments = [], [], [], []
        for fact in facts:
            current = json.loads(fact["dimensions_json"])
            try:
                resolved = self._resolve_device_chain(conn, fact["device_id"], fact["day"])
            except LakeError as error:
                if error.code not in {"unknown_dimension", "ambiguous_dimension"}:
                    raise
                if self._in_scope(fact, current, None, kind, external_id):
                    affected.append(fact["id"])
                    blocked.append({"fact_id": fact["id"],
                                    "issue": {"code": error.code, "detail": error.detail}})
                continue
            new_street = resolved["street"]["external_id"]
            new_community = resolved["community"]["external_id"]
            if self._in_scope(fact, current, resolved, kind, external_id):
                affected.append(fact["id"])
            current_chain = (current.get("street", {}).get("external_id"),
                             current.get("community", {}).get("external_id"))
            if current_chain == (new_street, new_community):
                continue
            merged = dict(current)
            merged.update(resolved)
            conn.execute("UPDATE facts SET street_id=?,community_id=?,dimensions_json=? WHERE id=?",
                         (new_street, new_community,
                          json.dumps(merged, ensure_ascii=False, sort_keys=True), fact["id"]))
            changed.append(fact["id"])
            if fact["id"] not in affected:
                # Defensive: a fact must never move without appearing in the
                # revision's affected set, otherwise lineage would hide the move.
                affected.append(fact["id"])
            reassignments.append({"fact_id": fact["id"],
                                  "old_street_id": fact["street_id"], "old_community_id": fact["community_id"],
                                  "new_street_id": new_street, "new_community_id": new_community,
                                  "old_dimensions": current, "new_dimensions": merged})
        return {"affected": affected, "changed": changed, "blocked": blocked,
                "reassignments": reassignments}

    @staticmethod
    def _in_scope(fact, current: dict, resolved: dict | None, kind: str, external_id: str):
        if kind == "device":
            return fact["device_id"] == external_id
        values = {fact.get(kind + "_id")}
        for snapshot in (current, resolved):
            if snapshot and snapshot.get(kind):
                values.add(snapshot[kind].get("external_id"))
        return external_id in values

    def _resolve_device_chain(self, conn, device_id: str, day: str):
        device = self._resolve(conn, "device", device_id, day)
        community_id = key(json.loads(device["attributes"])["community_id"], "community_id")
        community = self._resolve(conn, "community", community_id, day)
        street_id = key(json.loads(community["attributes"])["street_id"], "street_id")
        street = self._resolve(conn, "street", street_id, day)
        return {"device": {"id": device["id"], "external_id": device_id,
                           "version": device["version"], "name": device["name"]},
                "community": {"id": community["id"], "external_id": community_id,
                              "version": community["version"], "name": community["name"]},
                "street": {"id": street["id"], "external_id": street_id,
                           "version": street["version"], "name": street["name"]}}

    def list_revisions(self, kind: str | None = None, external_id: str | None = None):
        return self.store.revisions(kind, external_id)

    def resolve_event(self, conn, event) -> dict:
        day = event.happened_at[:10]
        result = {}
        for kind, external_id in (("material", event.material_id), ("device", event.device_id),
                                  ("station", event.station_id), ("actor", event.actor_id)):
            if external_id is not None:
                row = self._resolve(conn, kind, external_id, day)
                result[kind] = {"id": row["id"], "external_id": external_id,
                                "version": row["version"], "name": row["name"]}
                if kind == "device":
                    attrs = json.loads(row["attributes"])
                    community_id = key(attrs["community_id"], "community_id")
                    community = self._resolve(conn, "community", community_id, day)
                    street_id = key(json.loads(community["attributes"])["street_id"], "street_id")
                    street = self._resolve(conn, "street", street_id, day)
                    result["community"] = {"id": community["id"], "external_id": community_id,
                                           "version": community["version"], "name": community["name"]}
                    result["street"] = {"id": street["id"], "external_id": street_id,
                                        "version": street["version"], "name": street["name"]}
        return result

    def list_versions(self, kind: str, external_id: str):
        with self.store.connect() as conn:
            rows = self.store.all(conn, "SELECT * FROM dimension_versions WHERE kind=? AND external_id=? ORDER BY version",
                                  (kind, external_id))
        for row in rows:
            row["attributes"] = json.loads(row["attributes"])
        return rows
