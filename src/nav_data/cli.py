"""``nav-data`` command line: every production stage behind one subcommand."""

import argparse
import json
from pathlib import Path


def main(argv=None):
    parser = argparse.ArgumentParser(description="Navigation expert-data production")
    commands = parser.add_subparsers(dest="command", required=True)

    annotate = commands.add_parser("planner-chunks", help="annotate frozen planner chunks")
    annotate.add_argument("--cases", required=True)
    annotate.add_argument("--config", required=True)
    annotate.add_argument("--protocol", required=True)
    annotate.add_argument("--output", required=True)
    annotate.add_argument("--concurrency", type=int, default=1)
    annotate.add_argument("--repeats", type=int, default=1)

    pool = commands.add_parser("planner-chunk-pool", help="annotate chunks across model endpoints")
    pool.add_argument("--cases", required=True)
    pool.add_argument("--config", required=True)
    pool.add_argument("--protocol", required=True)
    pool.add_argument("--output", required=True)
    pool.add_argument("--repeats", type=int, default=1)

    extract = commands.add_parser("extract", help="extract canonical rows from a conversion root")
    extract.add_argument("--conversion-root", type=Path, required=True)
    extract.add_argument("--output", type=Path, required=True)
    extract.add_argument("--workers", type=int, default=2)
    extract.add_argument("--resume-gzip", type=Path)

    inventory = commands.add_parser("inventory", help="freeze and audit a compact episode release")
    inventory.add_argument("--conversion-root", type=Path, required=True)
    inventory.add_argument("--output", type=Path, required=True)
    inventory.add_argument("--owner", required=True)
    inventory.add_argument("--workers", type=int, default=64)

    inspect = commands.add_parser("inspect-episode", help="validate one compact episode")
    inspect.add_argument("--inspect", type=Path, required=True)
    inspect.add_argument("--full-media", action="store_true")

    args = parser.parse_args(argv)
    if args.command == "planner-chunks":
        from .planner_chunks.annotation import run

        run(args)
        return
    if args.command == "planner-chunk-pool":
        from .planner_chunks.pool import run_pool

        print(json.dumps(run_pool(args), ensure_ascii=False), flush=True)
        return
    if args.command == "extract":
        from .collection.distribution import extract_release

        print(json.dumps(extract_release(args), ensure_ascii=False), flush=True)
        return
    if args.command == "inventory":
        from .release.inventory import build_inventory

        build_inventory(args)
        return
    from .release.compact import inspect_episode

    print(json.dumps(inspect_episode(args.inspect, full_media=args.full_media)), flush=True)
