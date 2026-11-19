"""``nav-data`` command line: every production stage behind one subcommand.

Stages run in order: prepare (freeze inputs, no simulation or model calls) -> collect
(submit to the sim-engine collection service) -> compact/extract -> inventory (frozen
release). ``collect-*`` exits 2 when any task is unfinished, uncertain or aborted.
"""

import argparse
import json
from pathlib import Path


def _report(value):
    print(json.dumps(value, ensure_ascii=False, default=str), flush=True)


def _require_settled(summary):
    counts = summary.get("counts", {})
    if (summary.get("aborted") or summary.get("model_threads_still_running")
            or counts.get("failed_or_uncertain") or counts.get("not_started")
            or summary.get("success_target_met") is False):
        raise SystemExit(2)


def _parser():
    parser = argparse.ArgumentParser(prog="nav-data", description="Navigation expert-data production")
    commands = parser.add_subparsers(dest="command", required=True)

    def add(name, help, *arguments):
        sub = commands.add_parser(name, help=help)
        for flags, options in arguments:
            sub.add_argument(*flags, **options)
        return sub

    path = {"type": Path, "required": True}
    required = {"required": True}

    add("prepare-short", "freeze a short-instruction run (no model or movement)",
        (["--source-config"], path), (["--output"], path))
    add("collect-short", "collect a prepared short-instruction run", (["--root"], path))
    add("prepare-r2r", "freeze R2R chunk tasks from a reviewed parent collection",
        (["--config"], path), (["--output"], path))
    add("prepare-r2r-full", "freeze R2R chunk tasks from pinned FG/continuous train sources",
        (["--config"], path), (["--episodes"], path), (["--fg-train"], path), (["--output"], path))
    add("collect-r2r", "collect a frozen R2R chunk manifest", (["--manifest-dir"], path))
    add("replenish-r2r", "freeze a deduplicated R2R replenishment from prior attempts",
        (["--base"], path), (["--prior"], {"type": Path, "action": "append", "required": True}),
        (["--output"], path))
    add("annotate-r2r-replenishment", "annotate reused geometry without movement",
        (["--manifest-dir"], path))
    add("finalize-r2r-replenishment", "write the final coverage index", (["--manifest-dir"], path))

    chunk = (["--cases"], required), (["--config"], required), (["--protocol"], required), \
        (["--output"], required), (["--repeats"], {"type": int, "default": 1})
    add("planner-chunks", "annotate frozen planner chunks", *chunk,
        (["--concurrency"], {"type": int, "default": 1}))
    add("planner-chunk-pool", "annotate planner chunks across model endpoints", *chunk)

    add("extract", "extract canonical rows from a conversion root",
        (["--conversion-root"], path), (["--output"], path),
        (["--workers"], {"type": int, "default": 2}), (["--resume-gzip"], {"type": Path}))
    add("inventory", "freeze and audit a compact episode release",
        (["--conversion-root"], path), (["--output"], path), (["--owner"], required),
        (["--workers"], {"type": int, "default": 64}))
    add("inspect-episode", "validate one compact episode",
        (["--inspect"], path), (["--full-media"], {"action": "store_true"}))
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    command = args.command
    if command == "prepare-short":
        from .tasks.short_instructions import prepare_short

        _report(prepare_short(args.source_config, args.output))
    elif command == "collect-short":
        from .tasks.short_instructions import collect_short

        summary = collect_short(args.root)
        _report(summary)
        _require_settled(summary)
    elif command == "prepare-r2r":
        from .planner_chunks.prepare import prepare
        from .records import read

        manifest = prepare(read(args.config), args.output)
        _report({"candidates": len(manifest["candidates"]), "owner": manifest["owner"]})
    elif command == "prepare-r2r-full":
        from .planner_chunks.full_prepare import prepare_full
        from .records import read

        _report(prepare_full(read(args.config), args.episodes, args.fg_train, args.output))
    elif command == "collect-r2r":
        from .planner_chunks.collect import collect_r2r

        summary = collect_r2r(args.manifest_dir)
        _report(summary)
        _require_settled(summary)
    elif command == "replenish-r2r":
        from .planner_chunks.replenish import prepare_replenishment

        _report(prepare_replenishment(args.base, args.prior, args.output)["replenishment"]["counts"])
    elif command == "annotate-r2r-replenishment":
        from .planner_chunks.prepare import verify_manifest
        from .planner_chunks.replenish import run_annotation_only

        root = args.manifest_dir.resolve()
        _report(run_annotation_only(verify_manifest(root), root / "annotation-only"))
    elif command == "finalize-r2r-replenishment":
        from .planner_chunks.prepare import verify_manifest
        from .planner_chunks.replenish import finalize_coverage

        root = args.manifest_dir.resolve()
        _report(finalize_coverage(verify_manifest(root), root))
    elif command == "planner-chunks":
        from .planner_chunks.annotation import run

        run(args)
    elif command == "planner-chunk-pool":
        from .planner_chunks.pool import run_pool

        _report(run_pool(args))
    elif command == "extract":
        from .collection.distribution import extract_release

        _report(extract_release(args))
    elif command == "inventory":
        from .release.inventory import build_inventory

        _report(build_inventory(args))
    else:
        from .release.compact import inspect_episode

        _report(inspect_episode(args.inspect, full_media=args.full_media))
