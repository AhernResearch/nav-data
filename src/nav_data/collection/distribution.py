"""Read-only distributions of a fixed successful-collection snapshot."""

from __future__ import annotations

import datetime
import gzip
import hashlib
import json
import math
import re
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from itertools import pairwise
from pathlib import Path


def digest_bytes(data):
    return hashlib.sha256(data).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_bytes())


def snapshot_entries(conversion_root):
    root = Path(conversion_root)
    plan = read_json(root / "plan.json")
    baseline_bytes = (root / "catalog-baseline.json").read_bytes()
    tasks_bytes = (root / "tasks.jsonl").read_bytes()
    assert digest_bytes(baseline_bytes) == plan["baseline_sha256"]
    assert digest_bytes(tasks_bytes) == plan["tasks_sha256"]
    entries = {}
    for client_root, rows in json.loads(baseline_bytes)["rows"].items():
        for row in rows:
            client = str(Path(client_root) / row["id"])
            entries[client] = {
                "client": client,
                "metadata": row["metadata"],
                "metadata_sha256": row["sha256"],
                "expected": {},
            }
    for line in tasks_bytes.splitlines():
        task = json.loads(line)
        client, _shared, metadata = task["entry"]
        if client in entries:
            assert task.get("recovery"), "duplicate task without recovery identity"
            assert entries[client]["metadata"] == metadata
            entries[client]["expected"] = task.get("expected", {})
        else:
            entries[client] = {
                "client": client,
                "metadata": metadata,
                "metadata_sha256": None,
                "expected": task.get("expected", {}),
            }
    assert len(entries) == plan["total_target"], (len(entries), plan["total_target"])
    request_ids = [Path(e["metadata"]).parent.name for e in entries.values()]
    assert len(set(request_ids)) == len(entries), "duplicate service request identity"
    provenance = {
        "conversion_root": str(root),
        "target": len(entries),
        "baseline_sha256": digest_bytes(baseline_bytes),
        "tasks_sha256": digest_bytes(tasks_bytes),
        "snapshot_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    return list(entries.values()), provenance


def load_episode(entry):
    """Prefer atomic meta.json; retry it if retirement removes legacy sidecars."""
    metadata = Path(entry["metadata"])
    client, raw = Path(entry["client"]), metadata.parent
    for _ in range(3):
        if metadata.is_file():
            content = metadata.read_bytes()
            if (
                entry.get("metadata_sha256")
                and digest_bytes(content) != entry["metadata_sha256"]
            ):
                raise ValueError("baseline metadata digest changed")
            data = json.loads(content)
            if data["client_folder"] != str(client) or data["raw_folder"] != str(raw):
                raise ValueError("canonical metadata/source scope mismatch")
            for key, expected in entry.get("expected", {}).items():
                if data["original_sha256"].get(key) != expected:
                    raise ValueError(
                        "canonical source differs from frozen reconciliation"
                    )
            return data, {"format": "compact", "sha256": digest_bytes(content)}
        try:
            client_names = [
                "result.json",
                "proposed-task.json",
                "pointnav-target.json",
                "pointnav-outcome.json",
            ]
            raw_names = ["receipt.json"]
            payloads = {f"client/{n}": (client / n).read_bytes() for n in client_names}
            payloads.update({f"raw/{n}": (raw / n).read_bytes() for n in raw_names})
            payloads["raw/world_states.jsonl"] = (
                raw / "world_states.jsonl"
            ).read_bytes()
            for key, expected in entry.get("expected", {}).items():
                if digest_bytes(payloads[key]) != expected:
                    raise ValueError("legacy source differs from frozen reconciliation")
            records = {
                "client": {
                    n: json.loads(payloads["client/" + n]) for n in client_names
                },
                "raw": {n: json.loads(payloads["raw/" + n]) for n in raw_names},
            }
            data = {
                "id": client.name,
                "request_id": raw.name,
                "client_folder": str(client),
                "raw_folder": str(raw),
                "records": records,
                "world_states": [
                    json.loads(line)
                    for line in payloads["raw/world_states.jsonl"].splitlines()
                    if line
                ],
            }
            return data, {
                "format": "legacy",
                "sha256": {k: digest_bytes(v) for k, v in payloads.items()},
            }
        except FileNotFoundError:
            if metadata.exists():
                continue
            time.sleep(0.02)
    raise FileNotFoundError(f"cannot read either representation: {metadata}")


def wrap_angle(x):
    return math.atan2(math.sin(x), math.cos(x))


def distance(a, b, axes=(0, 1, 2)):
    return math.sqrt(sum((a[k] - b[k]) ** 2 for k in axes))


def word_tokens(text):
    return re.findall(r"[a-z]+(?:'[a-z]+)?|\d+(?:\.\d+)?", text.lower())


def summarize_episode(data, source):
    client, raw = data["records"]["client"], data["records"]["raw"]
    target, outcome = client["pointnav-target.json"], client["pointnav-outcome.json"]
    result, receipt = client["result.json"], raw["receipt.json"]
    if any(x["status"] != "completed_audit" for x in [result, receipt, outcome]):
        raise ValueError("snapshot contains an unconfirmed success")
    if not result["recording_complete"] or not receipt["recording_complete"]:
        raise ValueError("snapshot contains incomplete recording")
    states = data["world_states"]
    positions = [s["world_position"] for s in states]
    if not states or [s["frame"] for s in states] != list(range(len(states))):
        raise ValueError("actual frame sequence is incomplete")
    if data.get("frames", len(states)) != len(states):
        raise ValueError("canonical frame count differs")
    goal = target["episode"]["goals"][0]["position"]
    start = target["episode"]["start_position"]
    if (
        distance(positions[0], start) > 0.001
        or distance(goal, outcome["intended_goal_position"]) > 1e-6
    ):
        raise ValueError("frozen start/goal differs from actual recording")
    times = [s["control_time_s"] for s in states]
    if any(b < a for a, b in pairwise(times)):
        raise ValueError("nonmonotonic recorded time")
    dt = raw.get("camera.json", {}).get("control_dt_s", 0.25)
    if any(abs(b - a - dt) > 1e-5 for a, b in pairwise(times)):
        raise ValueError("unexpected actual sampling interval")
    yaws = [s["base_pose_se2"][2] for s in states]
    yaw_steps = [wrap_angle(b - a) for a, b in pairwise(yaws)]
    path = sum(distance(a, b) for a, b in pairwise(positions))
    planar_path = sum(distance(a, b, (0, 2)) for a, b in pairwise(positions))
    displacement = distance(positions[0], positions[-1])
    q = states[0]["world_rotation_xyzw"]
    dx, dy, dz = [goal[i] - positions[0][i] for i in range(3)]
    vx, vy, vz, w = q
    cx, cy, cz = vy * dz - vz * dy, vz * dx - vx * dz, vx * dy - vy * dx
    c2x, c2y, c2z = vy * cz - vz * cy, vz * cx - vx * cz, vx * cy - vy * cx
    lx, _ly, lz = (
        dx - 2 * w * cx + 2 * c2x,
        dy - 2 * w * cy + 2 * c2y,
        dz - 2 * w * cz + 2 * c2z,
    )
    bearing = math.degrees(math.atan2(-lx, -lz))
    instruction = client["proposed-task.json"]["instruction"]
    bbox = target["target_source"]["bbox"]
    return {
        "uid": data["request_id"],
        "case_id": data["id"],
        "run": Path(data["client_folder"]).parent.parent.name,
        "scene": target["episode"]["scene_id"],
        "instruction": instruction,
        "words": len(word_tokens(instruction)),
        "characters": len(instruction),
        "start": positions[0],
        "goal": goal,
        "endpoint": positions[-1],
        "start_yaw_deg": math.degrees(wrap_angle(yaws[0])),
        "goal_bearing_deg": bearing,
        "goal_forward_m": -lz,
        "goal_left_m": -lx,
        "goal_planar_m": math.hypot(lx, lz),
        "goal_euclidean_m": distance(start, goal),
        "goal_height_delta_m": goal[1] - start[1],
        "path_m": path,
        "path_planar_m": planar_path,
        "endpoint_displacement_m": displacement,
        "straightness": displacement / path if path > 1e-6 else None,
        "detour_ratio": path / displacement if displacement > 0.05 else None,
        "frames": len(states),
        "duration_s": times[-1] - times[0],
        "turn_abs_deg": math.degrees(sum(map(abs, yaw_steps))),
        "turn_net_deg": math.degrees(sum(yaw_steps)),
        "rotation_only_steps": sum(
            distance(a, b) < 0.005 and abs(dy) > 0.005
            for a, b, dy in zip(positions, positions[1:], yaw_steps)
        ),
        "stationary_steps": sum(
            distance(a, b) < 0.005 and abs(dy) <= 0.005
            for a, b, dy in zip(positions, positions[1:], yaw_steps)
        ),
        "collision_frames": sum(bool(s.get("collided")) for s in states),
        "collision_steps": outcome["collision_steps"],
        "final_error_m": outcome["final_geodesic_distance_m"],
        "target_view": target["target_source"]["target_view"],
        "bbox": bbox,
        "bbox_area_fraction": (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]) / 1e6,
        "bbox_center": [(bbox[0] + bbox[2]) / 2000, (bbox[1] + bbox[3]) / 2000],
        "trajectory": positions,
        "yaw_deg": [round(math.degrees(wrap_angle(y)), 4) for y in yaws],
        "semantic_admission": result.get("semantic_admission", "pending_review"),
        "metadata_path": str(Path(data["raw_folder"]) / "meta.json"),
        "source": source,
    }


def extract(entry):
    data, source = load_episode(entry)
    return summarize_episode(data, source)


def recovered_prefix(path):
    """Recover complete ordered rows even if an interrupted gzip lacks its footer."""
    with gzip.open(path, "rt") as f:
        while True:
            try:
                line = f.readline()
                if not line:
                    break
                yield json.loads(line)
            except (EOFError, json.JSONDecodeError):
                break


def extract_release(args):
    out = args.output
    out.mkdir(parents=True, exist_ok=False)
    entries, provenance = snapshot_entries(args.conversion_root)
    (out / "snapshot.json").write_text(json.dumps(provenance, indent=2) + "\n")
    with gzip.open(out / "source-manifest.jsonl.gz", "wt") as f:
        for e in entries:
            f.write(json.dumps(e, separators=(",", ":")) + "\n")
    started = time.monotonic()
    formats = {}
    seen = set()
    try:
        with (
            ThreadPoolExecutor(max_workers=args.workers) as pool,
            gzip.open(out / "episodes.jsonl.gz.tmp", "wt") as f,
        ):

            def emit(row, i):
                if row["uid"] != Path(entries[i - 1]["metadata"]).parent.name:
                    raise ValueError(
                        "resumed extraction order differs from fixed snapshot"
                    )
                if row["uid"] in seen:
                    raise ValueError("duplicate extracted request identity")
                seen.add(row["uid"])
                kind = row["source"]["format"]
                formats[kind] = formats.get(kind, 0) + 1
                f.write(json.dumps(row, separators=(",", ":"), allow_nan=False) + "\n")
                if i % 1000 == 0:
                    progress = {
                        "completed": i,
                        "total": len(entries),
                        "elapsed_s": time.monotonic() - started,
                        "formats": formats,
                    }
                    (out / "progress.json").write_text(json.dumps(progress) + "\n")
                    print(json.dumps(progress), flush=True)

            recovered = 0
            if args.resume_gzip:
                for recovered, row in enumerate(recovered_prefix(args.resume_gzip), 1):
                    emit(row, recovered)
            for i, row in enumerate(
                pool.map(extract, entries[recovered:]), recovered + 1
            ):
                emit(row, i)
        (out / "episodes.jsonl.gz.tmp").rename(out / "episodes.jsonl.gz")
        result = {
            **provenance,
            "completed": len(seen),
            "formats": formats,
            "recovered_rows": recovered,
            "elapsed_s": time.monotonic() - started,
            "episodes_sha256": digest_bytes((out / "episodes.jsonl.gz").read_bytes()),
            "passed": True,
        }
        (out / "extraction-result.json").write_text(json.dumps(result, indent=2) + "\n")
        return result
    except BaseException as exc:
        (out / "error.json").write_text(
            json.dumps(
                {"error": repr(exc), "traceback": traceback.format_exc()}, indent=2
            )
            + "\n"
        )
        raise
