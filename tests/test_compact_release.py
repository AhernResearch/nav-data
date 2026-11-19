"""Data-preserving conversion, publication ordering and bounded retirement scope."""

import gzip
import hashlib
import json
import multiprocessing
import os
import sqlite3
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Event

import pytest
from PIL import Image
from recordings import recording

from nav_data.collection.client import save_outcome
from nav_data.records import digest, read, write
from nav_data.release import compact as compact
from nav_data.release.compact import read_rows
from nav_data.release.evidence import compact_request, restore_request
from nav_data.tasks.pointnav import target_record
from nav_data.tasks.recording import validate_recording
from nav_data.tasks.region import build_request


def stop_after_worker_commit(event):
    if event["phase"] == "committed":
        raise InterruptedError("committed before retirement")


def example(tmp_path, case_id="s00-p00001"):
    client_root = tmp_path / "client"
    client = client_root / case_id
    client.mkdir(parents=True)
    owner, service = "a" * 32, "b" * 32
    attempt = hashlib.sha256(case_id.encode()).hexdigest()[:32]
    cid = hashlib.sha256((owner + ":" + client.name).encode()).hexdigest()
    shared = tmp_path / "shared"
    raw = shared / "tasks" / cid
    raw.mkdir(parents=True)
    recording(raw)
    goal = [0, 0, -1.15]
    ep = {
        "_dataset": "train_pilot",
        "scene_id": "mp3d/room/room.glb",
        "start_position": [0, 0, 0],
        "start_rotation": [0, 0, 0, 1],
    }
    item = {"id": client.name, "episode": ep}
    config = {
        "owner": owner,
        "backend_manifest": {"config": {"split": "train", "goal_tolerance_m": 0.12}},
    }
    task = {
        "instruction": "Go to the open floor.",
        "target_view": "right",
        "bbox": [300, 400, 600, 800],
    }
    grounded = {
        "navmesh_point": goal,
        "world_point": [0, 0.01, -1.15],
        "pixel_xy": [2, 1],
        "depth_m": 1.15,
    }
    geometry = {
        "passed": True,
        "termination": "expert_stop",
        "collision_steps": 0,
        "final_distance_m": 0.05,
        "steps": 11,
    }
    terminal = read(raw / "terminal.json")
    terminal.update(requested_goal_distance_m=0.05, goal_tolerance_m=0.12)
    (raw / "terminal.json").write_text(json.dumps(terminal))
    public = {"images": {}}
    for view in compact.VIEWS:
        p = raw / (view + ".png")
        Image.new("RGB", (4, 2)).save(p)
        public["images"][view] = {"path": str(p), "sha256": digest(p)}
        (raw / (view + "-depth.npz")).write_bytes(b"disposable snapshot")
    target = target_record(config, item, task, grounded, goal)
    values = {
        "request.json": dict(
            request_id=cid,
            owner=owner,
            dataset="train_pilot",
            kind="short_instructions",
            **ep,
        ),
        "lease.json": {"request_id": cid, "owner": owner, "service_owner": service},
        "view-sensors.json": {},
        "start-state.json": {"episode": ep},
        "target.json": task,
        "selected-goal.json": {"position": goal, "source": "planned_before_movement"},
        "grounded-target.json": grounded,
        "grounding-check.json": {},
        "movement-attempted.json": {"automatic_retry": False},
        "geometry-result.json": {"geometry": geometry},
        "recording-check.json": validate_recording(raw),
        "region-terminal-check.json": {"inside_original_bbox": True},
        "receipt.json": {
            "status": "completed_audit",
            "recording_complete": True,
            "request_id": cid,
            "geometry": geometry,
        },
        "private-geometry.json": {
            "episode": dict(ep, goals=[{"position": goal, "radius": 0.08}]),
            "requested_goal_position": goal,
            "path": [[0, 0, 0], goal],
        },
        "planning-claim.json": {"attempt_id": attempt},
        "execution-lease.json": {"identity": "lease"},
        "region-candidates.json": {"debug": "discard"},
        "region-selection.json": {"debug": "discard"},
        "scene-cache.json": {},
        "status.json": {},
        "planning-result.json": {},
    }
    for n, v in values.items():
        write(raw / n, v)
    client_values = {
        "lease.json": {"owner": owner, "request_id": cid},
        "proposed-task.json": task,
        "result.json": {
            "id": client.name,
            "status": "completed_audit",
            "service_request_id": cid,
            "recording_complete": True,
            "geometry": geometry,
        },
        "pointnav-target.json": target,
        "public-observation.json": public,
        "wire-request.json": compact_request(
            build_request(public, {"model": "test-model"}, raw), public
        ),
        "model-result.json": {"status": "completed", "profile": "test"},
        "model-response.json": {"model": "test-model"},
        "model-started.json": {},
        "model-finished.json": {},
        "server-receipt.json": values["receipt.json"],
    }
    for n, v in client_values.items():
        write(client / n, v)
    for n in ["world_states.jsonl", "commands.jsonl", "terminal.json"]:
        (client / n).symlink_to(raw / n)
    save_outcome(client, target, geometry, True, values["region-terminal-check.json"])
    for n in ["camera.json", "rgb", "depth"]:
        (client / n).symlink_to(raw / n)
    planner = shared / "instances" / service / "planner/attempts" / attempt
    write(planner / "input.json", {"request": {"task": {"request_id": cid}}})
    write(planner / "result.json", {})
    write(planner / "status.json", {})
    write(planner / "artifacts/debug.json", {})
    return client, shared, raw, planner


def test_conversion_preserves_actual_media_labels_and_legacy_reader(tmp_path):
    client, shared, raw, planner = example(tmp_path)
    media = {
        str(p.relative_to(raw)): p.read_bytes()
        for p in raw.rglob("*")
        if p.is_file() and (p.parent.name in ["rgb", "depth"] or p.suffix == ".png")
    }
    source_labels = (client / "pointnav-frames.jsonl").read_bytes()
    original_target = read(client / "pointnav-target.json")
    original_request = restore_request(read(client / "wire-request.json"))
    results = compact.convert_batch([(client, shared)], full=True)
    ep = compact.open_episode(client.parent, client.name)
    assert results[0]["freed_inodes"] > 30
    assert ep.model_request() == original_request
    assert "wire-request.json" not in ep.data["records"]["client"]
    assert not client.exists() and not planner.exists()
    assert {p.name for p in raw.iterdir()} == {
        "meta.json",
        "rgb",
        "depth",
        *(v + ".png" for v in compact.VIEWS),
    }
    assert ep.record("client", "pointnav-target.json") == original_target
    assert (
        ep.data["original_sha256"]["pointnav_frames"]
        == hashlib.sha256(source_labels).hexdigest()
    )
    assert (
        ep.record("client", "proposed-task.json")["instruction"]
        == "Go to the open floor."
    )
    assert ep.data["training_admission"] == "pending_review"
    for name, content in media.items():
        assert (raw / name).read_bytes() == content
    assert compact.validate_data(ep.data, full=True)["frames"] == 12
    assert validate_recording(raw)["conversion_inputs_complete"]
    assert validate_recording(client)["conversion_inputs_complete"]
    assert read_rows(client) == ep.data["world_states"]


def test_parallel_batch_publishes_all_cases_before_retiring_any(tmp_path):
    first = example(tmp_path)
    second = example(tmp_path, "s00-p00002")

    def audit(event):
        if event["phase"] == "committed":
            for client, _, raw, _ in [first, second]:
                assert (raw / "meta.json").exists() and (
                    client / "result.json"
                ).exists()

    rows = compact.convert_batch(
        [(x[0], x[1]) for x in [first, second]], workers=4, audit=audit
    )
    assert len(rows) == 2
    assert all(
        compact.open_episode(x[0].parent, x[0].name).data["frames"] == 12
        for x in [first, second]
    )


def test_spawn_process_pool_preserves_order_and_recovers_with_same_executor(tmp_path):
    cases = [example(tmp_path, f"s00-p{i:05d}") for i in range(1, 3)]
    originals = [restore_request(read(c[0] / "wire-request.json")) for c in cases]
    parent = os.getpid()

    def stop_after_commit(event):
        assert os.getpid() == parent
        if event["phase"] == "committed":
            assert all(
                (raw / "meta.json").exists() and client.exists()
                for client, _, raw, _ in cases
            )
            raise InterruptedError("resume after durable commit")

    with ProcessPoolExecutor(
        max_workers=2, mp_context=multiprocessing.get_context("spawn")
    ) as pool:
        assert pool.submit(os.getpid).result() != parent
        with pytest.raises(InterruptedError):
            compact.convert_batch(
                [(c[0], c[1]) for c in cases],
                executor=pool,
                full=True,
                audit=stop_after_commit,
            )
        assert all(c[0].exists() for c in cases)
        rows = compact.convert_batch(
            [(c[0], c[1]) for c in cases], executor=pool, full=True
        )
        assert len(rows) == 2
        fresh = example(tmp_path, "s00-p00003")
        plan = compact.prepare_episode(fresh[0], fresh[1])
        expected_freed = len(plan["remove"]) + len(list(os.walk(fresh[3])))
        fresh_rows = compact.convert_batch([(fresh[0], fresh[1])], executor=pool)
        assert fresh_rows[0]["freed_inodes"] == expected_freed
        # The caller owns the pool and can reuse it after either outcome.
        assert pool.submit(os.getpid).result() != parent
    for case, request in zip(cases, originals):
        client, _, _raw, planner = case
        ep = compact.open_episode(client.parent, client.name)
        assert ep.model_request() == request
        assert compact.validate_data(ep.data, full=True)["frames"] == 12
        assert not client.exists() and not planner.exists()


def test_spawn_preparation_failure_preserves_all_sources(tmp_path):
    cases = [example(tmp_path, f"s00-p{i:05d}") for i in range(1, 3)]
    (cases[1][2] / "rgb/00001.jpg").unlink()
    with (
        ProcessPoolExecutor(
            max_workers=2, mp_context=multiprocessing.get_context("spawn")
        ) as pool,
        pytest.raises(ValueError, match="media frame set mismatch"),
    ):
        compact.convert_batch([(c[0], c[1]) for c in cases], executor=pool)
    for client, _, raw, planner in cases:
        assert (client / "result.json").exists()
        assert (raw / "receipt.json").exists()
        assert planner.exists() and not (raw / "meta.json").exists()


def test_stream_replenishes_bounded_queue_while_first_episode_is_blocked(
    tmp_path,
    monkeypatch,
):
    cases = [example(tmp_path, f"s00-p{i:05d}") for i in range(1, 4)]
    release = Event()
    prepare = compact.prepare_episode
    retired, admitted = set(), []

    def delayed(client, shared, **kwargs):
        if client == cases[0][0]:
            assert release.wait(10), "stream waited for slow first episode"
        return prepare(client, shared, **kwargs)

    def entries():
        for client, shared, _, _ in cases:
            admitted.append(client.name)
            assert len(admitted) - len(retired) <= 2
            yield client, shared

    def audit(event):
        if event["phase"] == "committed":
            for row in event["episodes"]:
                assert compact.open_episode(cases[0][0].parent, row["id"])
                assert (cases[0][0].parent / row["id"] / "result.json").exists()
        if event["phase"] == "retired":
            retired.update(row["id"] for row in event["results"])
            if any(row["id"] == cases[2][0].name for row in event["results"]):
                assert cases[0][0].exists()
                release.set()

    monkeypatch.setattr(compact, "prepare_episode", delayed)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            rows = list(
                compact.convert_stream(
                    entries(),
                    executor=pool,
                    max_pending=2,
                    audit=audit,
                )
            )
    finally:
        release.set()
    assert len(rows) == 3 and rows[0]["id"] != cases[0][0].name
    assert all(not case[0].exists() for case in cases)


def test_stream_spawn_commit_interruption_recovers_without_media_changes(tmp_path):
    client, shared, raw, planner = example(tmp_path)
    before = {
        str(p): p.read_bytes()
        for p in raw.rglob("*")
        if p.is_file() and p.parent.name in ["rgb", "depth"]
    }

    with ProcessPoolExecutor(
        max_workers=2,
        mp_context=multiprocessing.get_context("spawn"),
    ) as pool:
        with pytest.raises(InterruptedError):
            list(
                compact.convert_stream(
                    [(client, shared)],
                    executor=pool,
                    audit=stop_after_worker_commit,
                )
            )
        assert client.exists() and (raw / "meta.json").exists()
        assert compact.open_episode(client.parent, client.name).data["frames"] == 12
        rows = list(compact.convert_stream([(client, shared)], executor=pool))
    assert len(rows) == 1 and not client.exists() and not planner.exists()
    assert all(Path(p).read_bytes() == content for p, content in before.items())


def test_single_episode_queue_concurrent_writers_and_worker_audits(tmp_path):
    workers = int(os.environ.get("NAV_COMPACT_STRESS_WORKERS", "8"))
    per_run = max(8, workers)
    cases = [
        example(tmp_path / run, f"s00-p{i:05d}")
        for run in ["first", "second"]
        for i in range(1, per_run + 1)
    ]
    expected = {
        str(c[0]): {"client/result.json": digest(c[0] / "result.json")} for c in cases
    }
    for run in ["first", "second"]:
        compact.configure_concurrent_catalog(tmp_path / run / "client")
    audit_dir = tmp_path / "audit"
    ctx = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(
        max_workers=workers,
        mp_context=ctx,
        initializer=compact.set_catalog_write_lock,
        initargs=(ctx.Lock(),),
    ) as pool:
        rows = list(
            compact.convert_stream(
                ((c[0], c[1]) for c in cases),
                executor=pool,
                max_pending=2 * per_run,
                audit_dir=audit_dir,
                expected_hashes=expected,
            )
        )
    assert len(rows) == 2 * per_run and all(not c[0].exists() for c in cases)
    logs = list(audit_dir.glob("worker-*.jsonl.gz"))
    assert len(logs) >= 2
    audited = 0
    for path in logs:
        with gzip.open(path, "rt") as f:
            events = [json.loads(line) for line in f]
        assert len(events) % 3 == 0
        assert len({e["pid"] for e in events}) == 1
        for offset in range(0, len(events), 3):
            group = events[offset : offset + 3]
            assert [e["phase"] for e in group] == ["validated", "committed", "retired"]
            assert (
                group[0]["plans"][0]["id"]
                == group[1]["episodes"][0]["id"]
                == group[2]["results"][0]["id"]
            )
            audited += 1
    assert audited == 2 * per_run
    for run in ["first", "second"]:
        compact.finish_concurrent_catalog(tmp_path / run / "client")
        with sqlite3.connect(tmp_path / run / "client/episodes.sqlite3") as db:
            assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert db.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == per_run
    for client, _, _, _ in cases:
        assert compact.open_episode(client.parent, client.name).data["frames"] == 12


def test_catalog_reader_does_not_wait_for_active_worker_write(tmp_path):
    client, shared, _, _ = example(tmp_path)
    compact.configure_concurrent_catalog(client.parent)
    compact.convert_episode((client, shared))
    with closing(sqlite3.connect(client.parent / "episodes.sqlite3")) as db:
        db.execute("BEGIN EXCLUSIVE")
        db.execute("UPDATE episodes SET frames=frames")
        with ThreadPoolExecutor(max_workers=1) as pool:
            episode = pool.submit(compact.open_episode, client.parent, client.name)
            assert episode.result(timeout=2).data["frames"] == 12
        db.rollback()
    compact.finish_concurrent_catalog(client.parent)
    assert not (client.parent / "episodes.sqlite3-wal").exists()


def test_stream_stop_drains_admitted_episodes(tmp_path):
    cases = [example(tmp_path, f"s00-p{i:05d}") for i in range(1, 4)]
    stopping = Event()

    def audit(event):
        if event["phase"] == "validated":
            stopping.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        rows = list(
            compact.convert_stream(
                ((c[0], c[1]) for c in cases),
                executor=pool,
                max_pending=2,
                should_stop=stopping.is_set,
                audit=audit,
            )
        )
    assert len(rows) == 2
    assert all(not c[0].exists() for c in cases[:2])
    assert (cases[2][0] / "result.json").exists()


@pytest.mark.parametrize("failure", ["publication", "commit", "frozen_hash"])
def test_stream_failure_preserves_uncommitted_source(tmp_path, monkeypatch, failure):
    client, shared, raw, planner = example(tmp_path)
    expected = {}

    def fail(*args, **kwargs):
        raise OSError("injected storage failure")

    if failure == "publication":
        monkeypatch.setattr(compact, "publish", fail)
    elif failure == "commit":
        monkeypatch.setattr(compact, "_commit_publications", fail)
    else:
        expected = {client.name: {"client/result.json": "wrong"}}
    with (
        ThreadPoolExecutor(max_workers=2) as pool,
        pytest.raises((OSError, ValueError)),
    ):
        list(
            compact.convert_stream(
                [(client, shared)],
                executor=pool,
                expected_hashes=expected,
            )
        )
    assert (client / "result.json").exists()
    assert (raw / "receipt.json").exists() and planner.exists()


@pytest.mark.parametrize("damage", ["goal", "label", "media", "planner", "unknown"])
def test_invalid_or_unknown_source_is_not_retired(tmp_path, damage):
    client, shared, raw, planner = example(tmp_path)
    if damage == "goal":
        x = read(raw / "selected-goal.json")
        x["position"] = [0, 0, -2]
        (raw / "selected-goal.json").write_text(json.dumps(x))
    elif damage == "label":
        lines = (client / "pointnav-frames.jsonl").read_text().splitlines()
        x = json.loads(lines[0])
        x["goal_base_forward_left_up_m"] = [99, 0, 0]
        lines[0] = json.dumps(x)
        (client / "pointnav-frames.jsonl").write_text("\n".join(lines) + "\n")
    elif damage == "media":
        (raw / "rgb/00001.jpg").unlink()
    elif damage == "planner":
        (planner / "input.json").write_text(
            json.dumps({"request": {"task": {"request_id": "d" * 64}}})
        )
    else:
        (raw / "unclassified-training-data.bin").write_bytes(b"keep")
    with pytest.raises(ValueError):
        compact.convert_batch([(client, shared)])
    assert (client / "result.json").exists() and (raw / "request.json").exists()
    assert not (raw / "meta.json").exists()


def test_publication_failure_does_not_delete_originals(tmp_path, monkeypatch):
    client, shared, raw, _ = example(tmp_path)

    def full_disk(plan):
        raise OSError(28, "no space")

    monkeypatch.setattr(compact, "publish", full_disk)
    with pytest.raises(OSError):
        compact.convert_batch([(client, shared)])
    assert (client / "result.json").exists() and (raw / "receipt.json").exists()


def test_committed_publication_can_resume_after_interruption_before_retirement(
    tmp_path,
):
    client, shared, raw, planner = example(tmp_path)

    def stop(event):
        if event["phase"] == "committed":
            raise InterruptedError("stop after durable catalog commit")

    with pytest.raises(InterruptedError):
        compact.convert_batch([(client, shared)], audit=stop)
    assert (client / "result.json").exists() and (raw / "meta.json").exists()
    assert compact.open_episode(client.parent, client.name).data["frames"] == 12
    compact.convert_batch([(client, shared)])
    assert not client.exists() and not planner.exists()


def test_uncertain_attempt_and_external_symlink_target_remain_untouched(tmp_path):
    client, shared, raw, _ = example(tmp_path)
    external = tmp_path / "subtask.keep"
    external.write_text("protected")
    (client / "camera.json").unlink()
    (client / "camera.json").symlink_to(external)
    x = read(client / "result.json")
    x["status"] = "failed_or_uncertain"
    (client / "result.json").write_text(json.dumps(x))
    assert compact.convert_batch([(client, shared)]) == []
    assert (
        external.read_text() == "protected"
        and client.exists()
        and (raw / "receipt.json").exists()
    )


def forbid_sqlite_worker():
    compact.sqlite3.connect = forbidden_sqlite_connect


def forbidden_sqlite_connect(*args, **kwargs):
    raise AssertionError("directory worker accessed SQLite")


def save_case_templates(cases):
    templates = {}
    for client, _, _, _ in cases:
        body, key, _ = compact.request_template(
            read(client / "wire-request.json"), read(client / "public-observation.json")
        )
        templates.setdefault(client.parent, {})[key] = body
    for root, bodies in templates.items():
        compact.save_request_templates(root, bodies)


def test_directory_worker_never_accesses_sqlite_and_restores_model_request(
    tmp_path, monkeypatch
):
    case = example(tmp_path)
    client, shared, raw, _ = case
    expected = restore_request(read(client / "wire-request.json"))
    save_case_templates([case])
    with closing(compact.catalog(client.parent)):
        pass
    database = client.parent / "episodes.sqlite3"
    before = database.read_bytes()
    monkeypatch.setattr(compact.sqlite3, "connect", forbidden_sqlite_connect)
    result = compact.convert_episode_directory(
        (client, shared, raw / "meta.json"), full=True
    )
    assert not client.exists()
    assert compact.Episode(raw / "meta.json").model_request() == expected
    assert compact.Episode(raw / "meta.json").model_request() == expected
    assert database.read_bytes() == before
    assert result["catalog_row"]["sha256"] == digest(raw / "meta.json")


def test_directory_publication_interruption_and_finished_task_recovery(tmp_path):
    case = example(tmp_path)
    client, shared, raw, _ = case
    save_case_templates([case])
    entry = (client, shared, raw / "meta.json")
    originals = {
        str(p): p.read_bytes()
        for p in raw.rglob("*")
        if p.is_file() and p.parent.name in ["rgb", "depth"]
    }

    def stop(event):
        if event["phase"] == "published":
            raise InterruptedError("durable metadata before retirement")

    with pytest.raises(InterruptedError):
        compact.convert_episode_directory(entry, audit=stop)
    assert client.exists() and (raw / "meta.json").exists()
    assert not (client.parent / "episodes.sqlite3").exists()
    first = compact.convert_episode_directory(entry)
    second = compact.convert_episode_directory(entry)
    assert first["catalog_row"] == second["catalog_row"]
    assert second["freed_inodes"] == 0
    assert all(Path(p).read_bytes() == body for p, body in originals.items())
    assert compact.rebuild_catalog(client.parent, [second["catalog_row"]]) == 1
    assert compact.open_episode(client.parent, client.name).data["frames"] == 12


def test_directory_missing_template_preserves_originals(tmp_path):
    client, shared, raw, planner = example(tmp_path)
    with pytest.raises(FileNotFoundError):
        compact.convert_episode_directory((client, shared, raw / "meta.json"))
    assert (client / "result.json").exists() and (raw / "receipt.json").exists()
    assert planner.exists() and not (raw / "meta.json").exists()


def test_directory_queue_runs_with_sqlite_forbidden_and_rebuilds_afterwards(tmp_path):
    workers = int(os.environ.get("NAV_COMPACT_STRESS_WORKERS", "8"))
    per_run = max(8, workers)
    cases = [
        example(tmp_path / run, f"s00-p{i:05d}")
        for run in ["one", "two"]
        for i in range(1, per_run + 1)
    ]
    save_case_templates(cases)
    databases = {}
    for run in ["one", "two"]:
        root = tmp_path / run / "client"
        with closing(compact.catalog(root)):
            pass
        databases[root] = (root / "episodes.sqlite3").read_bytes()
    with ProcessPoolExecutor(
        max_workers=workers,
        mp_context=multiprocessing.get_context("spawn"),
        initializer=forbid_sqlite_worker,
    ) as pool:
        rows = list(
            compact.convert_stream(
                ((c[0], c[1], c[2] / "meta.json") for c in cases),
                executor=pool,
                directory_only=True,
                audit_dir=tmp_path / "audit",
                max_pending=2 * per_run,
            )
        )
    assert len(rows) == 2 * per_run and all(not c[0].exists() for c in cases)
    for root, original in databases.items():
        assert (root / "episodes.sqlite3").read_bytes() == original
        group = [r["catalog_row"] for r in rows if r["client_root"] == str(root)]
        assert compact.rebuild_catalog(root, group) == per_run
        for row in group:
            assert compact.open_episode(root, row["id"]).data["frames"] == 12


def test_failed_deferred_index_does_not_replace_previous_database(tmp_path):
    case = example(tmp_path)
    client, shared, raw, _ = case
    save_case_templates([case])
    with closing(compact.catalog(client.parent)):
        pass
    database = client.parent / "episodes.sqlite3"
    original = database.read_bytes()
    result = compact.convert_episode_directory((client, shared, raw / "meta.json"))
    with pytest.raises(compact.sqlite3.IntegrityError):
        compact.rebuild_catalog(client.parent, [result["catalog_row"]] * 2)
    assert database.read_bytes() == original
    assert compact.Episode(raw / "meta.json").model_request()
