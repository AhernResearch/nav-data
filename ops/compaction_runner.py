"""One queue, 128 independent directory workers, derived catalogs built at the end."""

import datetime
import hashlib
import json
import multiprocessing
import os
import shutil
import signal
import sqlite3
import time
import traceback
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from contextlib import closing
from pathlib import Path

from nav_data.release.compact import (
    convert_stream,
    finish_concurrent_catalog,
    fsync_directory,
    identity,
    rebuild_catalog,
    save_request_templates,
)

HERE = Path(__file__).parent
# An operation directory carries identity.json and, once started, plan.json/reconciliation.json.
IDENTITY = json.loads((HERE / "identity.json").read_text())
OWNER = IDENTITY["owner"]
LATEST = Path(
    IDENTITY.get(
        "latest",
        "/data/nav_storage/nav-train/collections/"
        "free-instructions-100k-v5-io-20261115-6ca6ed773bdb",
    )
)
WORKERS = int(IDENTITY.get("workers", 128))
TARGET = int(IDENTITY.get("target", 92167))
CURRENT_SHARED = IDENTITY.get(
    "current_shared", "/data/nav_storage/sim-engine/collections/free-v5-io-6ca6ed773bdb"
)
STOP_REQUESTED = False


def read(path):
    return json.loads(Path(path).read_text())


def save(name, value):
    target = HERE / name
    temp = target.with_suffix(".tmp")
    with temp.open("w") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    temp.replace(target)
    fsync_directory(HERE)


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def capacity():
    s = os.statvfs("/data/nav_storage")
    return {"free_inodes": s.f_favail, "available_bytes": s.f_bavail * s.f_frsize}


def process_alive(record):
    stat = Path("/proc", str(record["pid"]), "stat")
    return (
        stat.exists()
        and stat.read_text().rsplit(")", 1)[1].split()[19] == record["start_ticks"]
    )


def forbidden_sqlite(*args, **kwargs):
    raise RuntimeError("directory workers must not access SQLite")


def worker_init():
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    sqlite3.connect = forbidden_sqlite


def request_stop(signum, frame):
    global STOP_REQUESTED
    STOP_REQUESTED = True


def inspect_case(folder):
    try:
        r = read(folder / "result.json")
    except (OSError, ValueError):
        return folder.name, "missing_or_partial_result", None
    return folder.name, r.get("status", "unknown"), r.get("service_request_id")


def frozen_hashes(row):
    return {
        "client/result.json": row["result_sha256"],
        "raw/receipt.json": row["receipt_sha256"],
        "client/pointnav-target.json": row["pointnav_target_sha256"],
        "client/pointnav-outcome.json": row["pointnav_outcome_sha256"],
    }


def prepare(pool):
    previous = Path(IDENTITY["previous_operation"])
    assert not process_alive(read(previous / "process.json")), (
        "previous conversion still alive"
    )
    old_plan = read(previous / "plan.json")
    if (HERE / "plan.json").exists():
        plan = read(HERE / "plan.json")
    elif old_plan.get("directory_only"):
        for name in ["catalog-baseline.json", "tasks.jsonl"]:
            shutil.copyfile(previous / name, HERE / name)
        plan = {**old_plan, "owner": OWNER}
        save("plan.json", plan)
    else:
        recon = read(LATEST / "reconciliation.json")
        groups, shared = defaultdict(list), {}
        for row in recon["records"]:
            root = str(Path(row["client_folder"]).parent)
            groups[root].append(row)
            shared[root] = str(Path(row["raw_folder"]).parent.parent)
        current = str(LATEST / "client")
        groups[current] = []
        shared[current] = CURRENT_SHARED
        protected = old_plan["protected"]
        assert all(sha(p) == h for p, h in protected.items()), (
            "protected source changed"
        )
        baseline, db_identities, done = {}, {}, {}
        for root in groups:
            assert not process_alive(read(Path(root) / "client-process.json")), (
                "collection client active"
            )
            finish_concurrent_catalog(root)
            save_request_templates(root)
            database = Path(root) / "episodes.sqlite3"
            with closing(
                sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
            ) as db:
                assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                db.row_factory = sqlite3.Row
                baseline[root] = [dict(r) for r in db.execute("SELECT * FROM episodes")]
            db_identities[root] = identity(database)
            done[root] = {r["id"] for r in baseline[root]}
            protected[str(Path(root) / "request-templates.json")] = sha(
                Path(root) / "request-templates.json"
            )
        save(
            "catalog-baseline.json",
            {"rows": baseline, "database_identities": db_identities},
        )
        total_baseline = sum(map(len, baseline.values()))
        tasks, skipped = [], Counter()
        for root, rows in groups.items():
            for row in rows:
                if row["id"] not in done[root] or Path(row["client_folder"]).exists():
                    tasks.append(
                        {
                            "entry": [
                                row["client_folder"],
                                shared[root],
                                str(Path(row["raw_folder"]) / "meta.json"),
                            ],
                            "expected": frozen_hashes(row),
                            "recovery": row["id"] in done[root],
                        }
                    )
        folders = sorted(
            Path(e.path)
            for e in os.scandir(current)
            if e.is_dir(follow_symlinks=False)
            and e.name.startswith("s")
            and "-p" in e.name
        )
        for cid, status, request_id in pool.map(inspect_case, folders, chunksize=32):
            if cid in done[current]:
                original = next(r for r in baseline[current] if r["id"] == cid)
                tasks.append(
                    {
                        "entry": [
                            str(Path(current) / cid),
                            shared[current],
                            original["metadata"],
                        ],
                        "expected": {},
                        "recovery": True,
                    }
                )
            elif status == "completed_audit":
                assert isinstance(request_id, str) and len(request_id) == 64
                tasks.append(
                    {
                        "entry": [
                            str(Path(current) / cid),
                            shared[current],
                            str(
                                Path(shared[current])
                                / "tasks"
                                / request_id
                                / "meta.json"
                            ),
                        ],
                        "expected": {},
                        "recovery": False,
                    }
                )
            else:
                skipped[status] += 1
        target = total_baseline + sum(not t["recovery"] for t in tasks)
        assert target == TARGET, (total_baseline, len(tasks), target)
        with (HERE / "tasks.jsonl").open("x") as f:
            for task in tasks:
                f.write(json.dumps(task, separators=(",", ":")) + "\n")
            f.flush()
            os.fsync(f.fileno())
        plan = {
            "owner": OWNER,
            "directory_only": True,
            "workers": WORKERS,
            "max_pending": 256,
            "worker_sqlite": False,
            "indexing": "once after conversion",
            "roots": shared,
            "protected": protected,
            "already_indexed": total_baseline,
            "total_target": target,
            "task_count": len(tasks),
            "skipped": dict(skipped),
            "tasks_sha256": sha(HERE / "tasks.jsonl"),
            "baseline_sha256": sha(HERE / "catalog-baseline.json"),
        }
        save("plan.json", plan)
    assert sha(HERE / "tasks.jsonl") == plan["tasks_sha256"]
    assert sha(HERE / "catalog-baseline.json") == plan["baseline_sha256"]
    assert all(sha(p) == h for p, h in plan["protected"].items())
    return plan


def run(pool):
    began = time.monotonic()
    plan = prepare(pool)
    baseline = read(HERE / "catalog-baseline.json")
    with (HERE / "tasks.jsonl").open() as f:
        tasks = [json.loads(line) for line in f]
    hashes = {t["entry"][0]: t["expected"] for t in tasks if t["expected"]}
    recovering = {t["entry"][0] for t in tasks if t["recovery"]}
    rows = {
        root: {r["id"]: r for r in group} for root, group in baseline["rows"].items()
    }
    counts = Counter()
    started = last_report = time.monotonic()
    last_count = 0
    with (HERE / "catalog-results.jsonl").open("w") as results:

        def progress(force=False):
            nonlocal last_report, last_count
            now = time.monotonic()
            if not force and now - last_report < 5:
                return
            results.flush()
            os.fsync(results.fileno())
            value = {
                "owner": OWNER,
                "workers": WORKERS,
                "backend": "process",
                "task_unit": "complete_directory",
                "worker_sqlite": False,
                "indexing": "deferred",
                "status": "running",
                "already_indexed": plan["already_indexed"],
                "total_target": plan["total_target"],
                "total_converted": plan["already_indexed"] + counts["new_episodes"],
                "counts": dict(counts),
                "recent_episodes_per_s": (counts["new_episodes"] - last_count)
                / max(now - last_report, 1e-9),
                "processing_elapsed_s": now - started,
                "elapsed_s": now - began,
                **capacity(),
            }
            save("progress.json", value)
            print(json.dumps(value), flush=True)
            last_report, last_count = now, counts["new_episodes"]

        progress(True)

        def entries():
            for task in tasks:
                if capacity()["free_inodes"] < 4096:
                    raise RuntimeError("inode headroom below 4096")
                yield tuple(map(Path, task["entry"]))

        for result in convert_stream(
            entries(),
            executor=pool,
            max_pending=256,
            directory_only=True,
            audit_dir=HERE / "worker-audit",
            expected_hashes=hashes,
            should_stop=lambda: STOP_REQUESTED,
        ):
            row, root = result["catalog_row"], result["client_root"]
            rows[root][row["id"]] = row
            if str(Path(root) / row["id"]) in recovering:
                counts["recovered_retirements"] += 1
            else:
                counts["new_episodes"] += 1
            counts["frames"] += result["frames"]
            counts["freed_inodes"] += result["freed_inodes"]
            counts["retired_allocated_bytes"] += result["retired_allocated_bytes"]
            results.write(json.dumps(result, separators=(",", ":")) + "\n")
            progress()
        progress(True)
    if STOP_REQUESTED:
        save(
            "stopped.json",
            {
                "owner": OWNER,
                "reason": "requested_stop_after_draining_queue",
                "total_converted": plan["already_indexed"] + counts["new_episodes"],
                "indexing": "deferred; resume using immutable tasks.jsonl",
            },
        )
        return
    assert sum(map(len, rows.values())) == plan["total_target"]
    save(
        "indexing-started.json",
        {
            "owner": OWNER,
            "worker_conversion_finished": True,
            "total": plan["total_target"],
        },
    )
    catalogs = {}
    for root, group in rows.items():
        assert (
            identity(Path(root) / "episodes.sqlite3")
            == baseline["database_identities"][root]
        ), "old catalog changed while workers ran"
        catalogs[root] = rebuild_catalog(root, group.values())
    assert sum(catalogs.values()) == plan["total_target"]
    assert all(sha(p) == h for p, h in plan["protected"].items())
    save(
        "result.json",
        {
            "owner": OWNER,
            "passed": True,
            "status": "completed",
            "worker_sqlite": False,
            "total_converted": plan["total_target"],
            "catalogs": catalogs,
            "counts": dict(counts),
            "elapsed_s": time.monotonic() - began,
            "subtask_touched": False,
            "uncertain_attempts_deleted": False,
            "utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        },
    )
    print("completed", flush=True)


def main():
    for name in [
        "OPENBLAS_NUM_THREADS",
        "OMP_NUM_THREADS",
        "MKL_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
    ]:
        os.environ[name] = "1"
    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    with ProcessPoolExecutor(
        max_workers=WORKERS,
        mp_context=multiprocessing.get_context("spawn"),
        initializer=worker_init,
    ) as pool:
        run(pool)


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:
        save(
            "error.json",
            {
                "owner": OWNER,
                "error": repr(exc),
                "traceback": traceback.format_exc(),
                "automatic_retry": False,
            },
        )
        raise
