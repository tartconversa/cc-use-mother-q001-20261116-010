from __future__ import annotations

import argparse
import json
import sys

from .model import LakeError
from .service import LakeService


def parser():
    p = argparse.ArgumentParser(prog="looplake")
    p.add_argument("--db", default="looplake.sqlite3")
    sub = p.add_subparsers(dest="command", required=True)
    source = sub.add_parser("source")
    source.add_argument("source")
    source.add_argument("channel")
    dimension = sub.add_parser("dimension")
    dimension.add_argument("file")
    relocate = sub.add_parser("relocate")
    relocate.add_argument("kind")
    relocate.add_argument("external_id")
    relocate.add_argument("effective_from")
    relocate.add_argument("--parent", required=True)
    relocate.add_argument("--reason")
    retract = sub.add_parser("retract")
    retract.add_argument("kind")
    retract.add_argument("external_id")
    retract.add_argument("--version", type=int)
    retract.add_argument("--reason")
    correct = sub.add_parser("correct")
    correct.add_argument("kind")
    correct.add_argument("external_id")
    correct.add_argument("as_of")
    correct.add_argument("--name")
    correct.add_argument("--reason")
    attribution = sub.add_parser("attribution")
    attribution.add_argument("month")
    revisions = sub.add_parser("revisions")
    revisions.add_argument("kind")
    revisions.add_argument("external_id")
    ingest = sub.add_parser("ingest")
    ingest.add_argument("file")
    batch = sub.add_parser("batch")
    batch.add_argument("file")
    batch.add_argument("checkpoint")
    batch.add_argument("--limit", type=int)
    rate = sub.add_parser("rate")
    rate.add_argument("material_id")
    rate.add_argument("valid_from")
    rate.add_argument("rate_yuan_per_kg")
    check = sub.add_parser("check")
    check.add_argument("batch_id")
    mart = sub.add_parser("mart")
    mart.add_argument("month")
    preview = sub.add_parser("preview")
    preview.add_argument("month")
    release = sub.add_parser("release")
    release.add_argument("name")
    release.add_argument("month")
    release.add_argument("fingerprint")
    release.add_argument("--supersedes")
    release.add_argument("--reason")
    inspect = sub.add_parser("inspect")
    inspect.add_argument("release_id", type=int)
    return p


def execute(service: LakeService, args):
    if args.command == "source":
        return service.register_source(args.source, args.channel)
    if args.command == "dimension":
        with open(args.file, encoding="utf-8") as file:
            return service.register_dimension(json.load(file))
    if args.command == "relocate":
        parent = "street_id" if args.kind == "community" else "community_id"
        return service.dimensions.relocate(args.kind, args.external_id, args.effective_from,
                                           parents={parent: args.parent}, reason=args.reason)
    if args.command == "retract":
        return service.dimensions.retract(args.kind, args.external_id, version=args.version,
                                          reason=args.reason)
    if args.command == "correct":
        return service.dimensions.correct(args.kind, args.external_id, args.as_of,
                                          name=args.name, reason=args.reason)
    if args.command == "attribution":
        return service.query.attribution(args.month)
    if args.command == "revisions":
        return service.dimensions.list_revisions(args.kind, args.external_id)
    if args.command == "ingest":
        with open(args.file, encoding="utf-8") as file:
            return service.ingest(json.load(file))
    if args.command == "batch":
        return service.batches.resume(args.file, args.checkpoint, args.limit)
    if args.command == "rate":
        return service.settlement.put_rate(args.material_id, "smart_box", args.valid_from,
                                           args.rate_yuan_per_kg)
    if args.command == "check":
        return service.batch_status(args.batch_id)
    if args.command == "mart":
        return service.month_report(args.month)
    if args.command == "preview":
        return service.preview(args.month)
    if args.command == "release":
        return service.publish(args.name, args.month, args.fingerprint,
                               supersedes=args.supersedes, reason=args.reason)
    if args.command == "inspect":
        return service.releases.get(args.release_id)
    raise LakeError("unknown_command", "unsupported command")


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        result = execute(LakeService(args.db), args)
    except (LakeError, OSError, ValueError, json.JSONDecodeError) as exc:
        if isinstance(exc, LakeError):
            output = {"code": exc.code, "detail": exc.detail}
        else:
            output = {"code": "input_error", "detail": str(exc)}
        print(json.dumps(output, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
