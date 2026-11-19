"""Compact completed free-instruction episodes; publish verified metadata before retirement.

Media bytes stay in place. One SQLite catalog per client run maps legacy case IDs to
canonical raw/meta.json files. Incomplete/uncertain attempts are never converted.
"""

from __future__ import annotations

import base64
import copy
import gzip
import hashlib
import json
import os
import re
import sqlite3
import stat
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import closing, nullcontext
from functools import lru_cache, partial
from pathlib import Path

_CATALOG_WRITE_LOCK = None


def set_catalog_write_lock(lock):
    """Install the pool-shared mutex in each worker initializer."""
    global _CATALOG_WRITE_LOCK
    _CATALOG_WRITE_LOCK = lock


SCHEMA = "nav_train.compact_episode.v1"
VIEWS = ("front", "right", "back", "left")
CLIENT_KEEP = (
    "lease.json",
    "proposed-task.json",
    "result.json",
    "pointnav-target.json",
    "pointnav-outcome.json",
    "public-observation.json",
    "wire-request.json",
)
RAW_KEEP = (
    "request.json",
    "lease.json",
    "camera.json",
    "view-sensors.json",
    "start-state.json",
    "target.json",
    "selected-goal.json",
    "grounded-target.json",
    "grounding-check.json",
    "movement-attempted.json",
    "terminal.json",
    "geometry-result.json",
    "recording-check.json",
    "region-terminal-check.json",
    "receipt.json",
)
RAW_OPTIONAL = ("planning-claim.json", "execution-lease.json")
RAW_RETIRE = set(
    RAW_KEEP
    + RAW_OPTIONAL
    + (
        "commands.jsonl",
        "world_states.jsonl",
        "execution-budget.json",
        "planning-result.json",
        "preflight.json",
        "private-geometry.json",
        "public-observation.json",
        "observation-ready.json",
        "region-candidates.json",
        "region-selection.json",
        "scene-cache.json",
        "status.json",
        "reference-check.json",
        "visible-floor-mask.json",
        "visible-floor-mask.npz",
        *(view + "-depth.npz" for view in VIEWS),
    )
)
CLIENT_RETIRE = (
    set(
        CLIENT_KEEP
        + (
            "model-finished.json",
            "model-started.json",
            "model-response.json",
            "model-result.json",
            "submission-started.json",
            "target-submission-started.json",
            "server-receipt.json",
            "pointnav-frames.jsonl",
        )
    )
    | RAW_RETIRE
    | {"rgb", "depth"}
)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def read(path):
    return json.loads(Path(path).read_bytes())


def under(path, root):
    path, root = Path(path).absolute(), Path(root).absolute()
    return path == root or root in path.parents


def identity(path):
    s = Path(path).lstat()
    return [s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, stat.S_IFMT(s.st_mode)]


def fsync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Episode:
    """Reader for the approved compact format; no retired sidecars are required."""

    def __init__(self, metadata):
        self.path = Path(metadata)
        self.data = read(self.path)
        if self.data.get("schema") != SCHEMA:
            raise ValueError("unsupported compact episode schema")
        self.raw = Path(self.data["raw_folder"])
        if self.path.absolute() != (self.raw / "meta.json").absolute():
            raise ValueError("compact metadata media location mismatch")

    def record(self, namespace, name):
        return self.data["records"][namespace][name]

    def original_sha256(self, namespace, name):
        return self.data["original_sha256"][namespace + "/" + name]

    def media_path(self, modality, frame):
        if modality not in ("rgb", "depth") or not 0 <= frame < self.data["frames"]:
            raise ValueError("invalid media request")
        return (
            self.raw
            / modality
            / (f"{frame:05d}" + (".jpg" if modality == "rgb" else ".npz"))
        )

    def model_request(self):
        from nav_data.release.evidence import request_digest, restore_request

        legacy = self.data["records"]["client"].get("wire-request.json")
        if legacy is not None:
            return restore_request(legacy)
        client_root = Path(self.data["client_folder"]).parent
        key = self.data["model"]["request_template_sha256"]
        if (client_root / "request-templates.json").exists():
            request = copy.deepcopy(_load_request_templates(str(client_root))[key])
        else:
            database = client_root / "episodes.sqlite3"
            with closing(
                sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
            ) as db:
                row = db.execute(
                    "SELECT body FROM request_templates WHERE sha256=?", (key,)
                ).fetchone()
            if row is None:
                raise ValueError("missing shared model request template")
            request = json.loads(row[0])
        if request_digest(request) != key:
            raise ValueError("shared model request template digest mismatch")
        images = self.record("client", "public-observation.json")["images"]
        for message in request["messages"]:
            if isinstance(message["content"], list):
                for part in message["content"]:
                    if part.get("type") == "image_url":
                        view = part["image_url"]["url"].removeprefix("view:")
                        content = (self.raw / (view + ".png")).read_bytes()
                        if sha(content) != images[view]["sha256"]:
                            raise ValueError("retained model image digest mismatch")
                        part["image_url"]["url"] = (
                            "data:image/png;base64,"
                            + base64.b64encode(content).decode()
                        )
        if request_digest(request) != self.data["model"]["canonical_request_sha256"]:
            raise ValueError("restored model request differs from original input")
        return request


def request_template(wire, public):
    from nav_data.release.evidence import compact_request, request_digest

    if wire.get("encoding") != "request_with_retained_png_references":
        wire = compact_request(wire, public)
    template = copy.deepcopy(wire["request"])
    views = {
        "image-sha256:" + item["sha256"]: view
        for view, item in public["images"].items()
    }
    for message in template["messages"]:
        if isinstance(message["content"], list):
            for part in message["content"]:
                if part.get("type") == "image_url":
                    part["image_url"]["url"] = "view:" + views[part["image_url"]["url"]]
    return template, request_digest(template), wire["canonical_request_sha256"]


def open_episode(client_root, case_id):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", case_id):
        raise ValueError("invalid case identity")
    database = Path(client_root) / "episodes.sqlite3"
    with closing(
        sqlite3.connect(database.absolute().as_uri() + "?mode=ro", uri=True, timeout=60)
    ) as db:
        row = db.execute(
            "SELECT metadata,sha256 FROM episodes WHERE id=?", (case_id,)
        ).fetchone()
    if row is None:
        raise KeyError(case_id)
    path = Path(row[0])
    if sha(path.read_bytes()) != row[1]:
        raise ValueError("compact metadata differs from committed catalog")
    return Episode(path)


@lru_cache(maxsize=32)
def _load_request_templates(client_root):
    from nav_data.release.evidence import request_digest

    value = read(Path(client_root) / "request-templates.json")
    if value.get("schema") != "nav_train.request_templates.v1":
        raise ValueError("unsupported request template manifest")
    templates = value["templates"]
    if any(request_digest(body) != key for key, body in templates.items()):
        raise ValueError("shared request template digest mismatch")
    return templates


def save_request_templates(client_root, templates=None):
    """Publish immutable run-level templates before admitting independent tasks."""
    client_root = Path(client_root).absolute()
    if templates is None:
        with closing(
            sqlite3.connect(
                (client_root / "episodes.sqlite3").as_uri() + "?mode=ro",
                uri=True,
            )
        ) as db:
            templates = {
                key: json.loads(body)
                for key, body in db.execute("SELECT sha256,body FROM request_templates")
            }
    from nav_data.release.evidence import request_digest

    if not templates or any(
        request_digest(body) != key for key, body in templates.items()
    ):
        raise ValueError("missing or invalid shared request templates")
    value = {"schema": "nav_train.request_templates.v1", "templates": templates}
    target = client_root / "request-templates.json"
    if target.exists():
        if read(target) != value:
            raise ValueError("request template manifest conflict")
        return
    temp = client_root / "request-templates.json.tmp"
    with temp.open("x") as f:
        json.dump(value, f, ensure_ascii=False, allow_nan=False)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    if read(temp) != value:
        raise ValueError("request template manifest round-trip mismatch")
    temp.rename(target)
    fsync_directory(client_root)


def validate_data(data, *, media=True, full=False):
    """Verify intended goal, actual recording/labels and retained input image identities."""
    import numpy as np

    raw, client = data["records"]["raw"], data["records"]["client"]
    result, receipt = client["result.json"], raw["receipt.json"]
    target, outcome = client["pointnav-target.json"], client["pointnav-outcome.json"]
    states, commands, labels = (
        data["world_states"],
        data["commands"],
        data["pointnav_frames"],
    )
    terminal, check = raw["terminal.json"], raw["recording-check.json"]
    if result["status"] != "completed_audit" or receipt["status"] != "completed_audit":
        raise ValueError("only client-and-server-confirmed success may be compacted")
    if (
        target["source_split"] != "train"
        or target["owner"] != client["lease.json"]["owner"]
    ):
        raise ValueError("training source/owner mismatch")
    if (
        not result["recording_complete"]
        or not receipt["recording_complete"]
        or not check["conversion_inputs_complete"]
        or check["control_dt_s"] != 0.25
    ):
        raise ValueError("incomplete actual recording")
    goal = target["episode"]["goals"][0]["position"]
    if (
        goal != raw["selected-goal.json"]["position"]
        or goal != outcome["intended_goal_position"]
    ):
        raise ValueError("intended goal changed")
    if raw["selected-goal.json"]["source"] != "planned_before_movement":
        raise ValueError("goal was not frozen before movement")
    if (
        outcome["status"] != "completed_audit"
        or outcome["collision_steps"] != 0
        or not outcome["inside_original_bbox"]
        or not terminal["receipt_stop"]
        or terminal["termination"] != "expert_stop"
        or outcome["final_geodesic_distance_m"]
        > target["success_criteria"]["goal_tolerance_m"]
    ):
        raise ValueError("unsuccessful PointNav outcome")
    from nav_data.tasks.pointnav import outcome_record

    if raw["geometry-result.json"]["geometry"] != result["geometry"]:
        raise ValueError("actual geometry result mismatch")
    recomputed = outcome_record(
        target,
        states,
        result["geometry"],
        terminal,
        True,
        raw["region-terminal-check.json"],
    )
    if recomputed != outcome:
        raise ValueError("stored PointNav outcome mismatch")
    episode = target["episode"]
    if (
        not np.allclose(
            states[0]["world_position"], episode["start_position"], atol=1e-5, rtol=0
        )
        or abs(np.dot(states[0]["world_rotation_xyzw"], episode["start_rotation"]))
        < 1 - 1e-5
        or raw["request.json"]["start_position"] != episode["start_position"]
    ):
        raise ValueError("actual start differs from frozen source")
    n = len(states)
    if (
        n != len(labels)
        or n != check["frames"]
        or n != data["frames"]
        or terminal["frame"] != n - 1
    ):
        raise ValueError("frame/label/terminal count mismatch")
    if len(commands) != 2 * (n - 1):
        raise ValueError("actual action journal count mismatch")
    if raw["camera.json"].get("schema_version") != 2:
        raise ValueError("calibrated sensor recording required")
    for i, (state, label) in enumerate(zip(states, labels)):
        if (
            state["frame"] != i
            or label["frame"] != i
            or state["collided"]
            or abs(state["control_time_s"] - i * 0.25) > 1e-8
            or state["control_time_s"] != label["control_time_s"]
        ):
            raise ValueError("invalid actual frame alignment")
        if i:
            a, b = commands[2 * (i - 1) : 2 * i]
            if (
                a["phase"] != "started"
                or b["phase"] != "returned"
                or a["step"] != i - 1
                or b["step"] != i - 1
                or b["collided"]
                or a["command"] != state["incoming_command"]
            ):
                raise ValueError("actual command/state mismatch")
    # Vectorized current-agent PointNav transform; preserve all saved values unchanged.
    p = np.asarray([s["world_position"] for s in states], dtype=float)
    q = np.asarray([s["world_rotation_xyzw"] for s in states], dtype=float)
    if (
        p.shape != (n, 3)
        or q.shape != (n, 4)
        or not np.isfinite(p).all()
        or not np.isfinite(q).all()
    ):
        raise ValueError("invalid actual pose array")
    if not np.allclose(np.linalg.norm(q, axis=1), 1, atol=1e-5, rtol=0):
        raise ValueError("invalid actual quaternion")
    q /= np.linalg.norm(q, axis=1)[:, None]
    delta = np.asarray(goal) - p
    local = (
        delta
        - 2 * q[:, 3:] * np.cross(q[:, :3], delta)
        + 2 * np.cross(q[:, :3], np.cross(q[:, :3], delta))
    )
    expected = np.stack([-local[:, 2], -local[:, 0], local[:, 1]], axis=1)
    if not np.allclose(
        expected, [x["goal_base_forward_left_up_m"] for x in labels], atol=1e-7, rtol=0
    ):
        raise ValueError("PointNav frame target mismatch")
    if not np.allclose(
        np.hypot(expected[:, 0], expected[:, 1]),
        [x["goal_distance_planar_m"] for x in labels],
        atol=1e-7,
        rtol=0,
    ):
        raise ValueError("PointNav frame distance mismatch")
    if not np.allclose(
        np.arctan2(expected[:, 1], expected[:, 0]),
        [x["goal_bearing_left_rad"] for x in labels],
        atol=1e-7,
        rtol=0,
    ):
        raise ValueError("PointNav frame bearing mismatch")
    if media:
        folder = Path(data["raw_folder"])
        for modality, suffix in [("rgb", ".jpg"), ("depth", ".npz")]:
            if (folder / modality).is_symlink():
                raise ValueError("symlink media directory")
            if set(os.listdir(folder / modality)) != {
                f"{i:05d}{suffix}" for i in range(n)
            }:
                raise ValueError("media frame set mismatch")
        public = client["public-observation.json"]["images"]
        for view in VIEWS:
            path = folder / (view + ".png")
            if (
                Path(public[view]["path"]).resolve() != path.resolve()
                or not path.is_file()
                or full
                and sha(path.read_bytes()) != public[view]["sha256"]
            ):
                raise ValueError("retained model input image mismatch")
        if full:
            from PIL import Image

            for i, state in enumerate(states):
                image, depth = (
                    folder / "rgb" / f"{i:05d}.jpg",
                    folder / "depth" / f"{i:05d}.npz",
                )
                if (
                    sha(image.read_bytes()) != state["rgb_sha256"]
                    or sha(depth.read_bytes()) != state["depth_sha256"]
                ):
                    raise ValueError("recorded media hash mismatch")
                with Image.open(image) as im:
                    if list(im.size) != raw["camera.json"]["resolution_wh"]:
                        raise ValueError("RGB dimensions mismatch")
                with np.load(depth, allow_pickle=False) as z:
                    values = z["depth_m"]
                    if (
                        values.dtype != np.float32
                        or not np.isfinite(values).all()
                        or not (values > 0).any()
                    ):
                        raise ValueError("invalid metric depth")
    return {"passed": True, "frames": n, "training_admission": "pending_review"}


def _files_to_remove(root, allowed, *, links=False):
    found = []
    for e in os.scandir(root):
        p = Path(e.path)
        if p.name in allowed and (
            e.is_file(follow_symlinks=False) or links and e.is_symlink()
        ):
            s = p.lstat()
            if stat.S_ISREG(s.st_mode) and s.st_nlink != 1:
                raise ValueError("refuse retirement of multiply linked metadata")
            found.append((str(p), identity(p)))
    return found


def prepare_episode(
    client_folder,
    shared_root,
    *,
    full=False,
    use_catalog=True,
    metadata=None,
):
    client_folder, shared_root = (
        Path(client_folder).absolute(),
        Path(shared_root).absolute(),
    )
    if client_folder.resolve() != client_folder or not re.fullmatch(
        r"s\d+-p\d+", client_folder.name
    ):
        raise ValueError("unsafe client case directory")
    database = client_folder.parent / "episodes.sqlite3"
    if metadata is not None and Path(metadata).exists():
        prior = Episode(metadata)
        if Path(prior.data["client_folder"]) != client_folder or not under(
            prior.raw, shared_root / "tasks"
        ):
            raise ValueError("canonical metadata/source scope mismatch")
        validate_data(prior.data, full=full)
        return _retirement_plan(prior.data)
    if use_catalog and database.exists():
        try:
            prior = open_episode(client_folder.parent, client_folder.name)
        except KeyError:
            prior = None
        if prior is not None:
            if Path(prior.data["client_folder"]) != client_folder or not under(
                prior.raw, shared_root / "tasks"
            ):
                raise ValueError("catalog/source scope mismatch")
            validate_data(prior.data, full=full)
            return _retirement_plan(prior.data)
    result = read(client_folder / "result.json")
    if result.get("status") != "completed_audit":
        return None
    request_id = result["service_request_id"]
    if not re.fullmatch(r"[0-9a-f]{64}", request_id):
        raise ValueError("unsafe request identity")
    folder = shared_root / "tasks" / request_id
    if folder.is_symlink() or folder.resolve() != folder:
        raise ValueError("unsafe raw directory")
    if (folder / "meta.json").exists():
        prior = Episode(folder / "meta.json")
        if (
            Path(prior.data["client_folder"]) != client_folder
            or prior.data["request_id"] != request_id
        ):
            raise ValueError("canonical metadata/source scope mismatch")
        validate_data(prior.data, full=full)
        return _retirement_plan(prior.data)
    records = {"raw": {}, "client": {}}
    hashes = {}

    def load(namespace, name, base):
        content = (base / name).read_bytes()
        hashes[namespace + "/" + name] = sha(content)
        records[namespace][name] = json.loads(content)

    for name in CLIENT_KEEP:
        load("client", name, client_folder)
    for name in RAW_KEEP:
        load("raw", name, folder)
    for name in RAW_OPTIONAL:
        if (folder / name).exists():
            load("raw", name, folder)
    streams = {}
    for key, base, name in [
        ("world_states", folder, "world_states.jsonl"),
        ("commands", folder, "commands.jsonl"),
        ("pointnav_frames", client_folder, "pointnav-frames.jsonl"),
    ]:
        content = (base / name).read_bytes()
        hashes[key] = sha(content)
        streams[key] = [json.loads(line) for line in content.splitlines()]
    model = read(client_folder / "model-result.json")
    if model["status"] != "completed":
        raise ValueError("model result incomplete")
    response = read(client_folder / "model-response.json")
    lease = records["client"]["lease.json"]
    raw_lease = records["raw"]["lease.json"]
    if lease["request_id"] != request_id or raw_lease["request_id"] != request_id:
        raise ValueError("claim identity mismatch")
    expected = hashlib.sha256(
        (lease["owner"] + ":" + client_folder.name).encode()
    ).hexdigest()
    if expected != request_id:
        raise ValueError("client request identity mismatch")
    # Keep the frozen start/goal summary, discard the predicted path and preview traces.
    prepared = read(folder / "private-geometry.json")
    records["raw"]["private-geometry.json"] = {
        k: prepared[k]
        for k in [
            "episode",
            "requested_goal_position",
            "goal_adjustment_m",
            "endpoint_review_required",
        ]
        if k in prepared
    }
    data = {
        "schema": SCHEMA,
        "id": client_folder.name,
        "request_id": request_id,
        "client_folder": str(client_folder),
        "raw_folder": str(folder),
        "frames": len(streams["world_states"]),
        "records": records,
        **streams,
        "original_sha256": hashes,
        "model": {"version": response.get("model"), "profile": model.get("profile")},
        "training_admission": "pending_review",
        "automatic_retry": False,
    }
    validate_data(data, full=full)
    wire = records["client"].pop("wire-request.json")
    template, template_sha, wire_sha = request_template(
        wire, records["client"]["public-observation.json"]
    )
    data["model"].update(
        request_template_sha256=template_sha, canonical_request_sha256=wire_sha
    )
    plan = _retirement_plan(data)
    plan["request_template"] = template
    return plan


def _retirement_plan(data):
    folder = Path(data["raw_folder"])
    client_folder = Path(data["client_folder"])
    shared_root = folder.parent.parent
    records = data["records"]
    request_id = data["request_id"]
    raw_lease = records["raw"]["lease.json"]
    remove = _files_to_remove(folder, RAW_RETIRE)
    if client_folder.exists():
        remove += _files_to_remove(client_folder, CLIENT_RETIRE, links=True)
    planning = None
    claim = records["raw"].get("planning-claim.json")
    if claim:
        service_owner, attempt = raw_lease["service_owner"], claim["attempt_id"]
        if not re.fullmatch(r"[0-9a-f]{32}", service_owner) or not re.fullmatch(
            r"[0-9a-f]{32}", attempt
        ):
            raise ValueError("unsafe planning identity")
        planning = (
            shared_root / "instances" / service_owner / "planner/attempts" / attempt
        )
        if planning.exists():
            if planning.resolve() != planning:
                raise ValueError("unsafe planner directory")
            status = (
                read(planning / "status.json")
                if (planning / "status.json").exists()
                else {}
            )
            if status.get("task_id"):
                if (
                    status["task_id"] != request_id
                    or status.get("attempt_id") != attempt
                ):
                    raise ValueError("planning directory belongs to a different task")
                if status.get("input_digest") != claim.get("input_digest"):
                    raise ValueError("planning input digest differs from frozen claim")
            elif (planning / "input.json").exists():
                evidence = read(planning / "input.json")
                if evidence["request"]["task"]["request_id"] != request_id:
                    raise ValueError("planning directory belongs to a different task")
            for directory, dirs, files in os.walk(planning, followlinks=False):
                if any((Path(directory) / d).is_symlink() for d in dirs):
                    raise ValueError("symlink in planning evidence")
                for name in files:
                    p = Path(directory) / name
                    if p.is_symlink() or p.stat().st_nlink != 1:
                        raise ValueError("linked planning evidence")
                    remove.append((str(p), identity(p)))
    keep_names = {v + ".png" for v in VIEWS} | {"rgb", "depth", "meta.json"}
    if set(os.listdir(folder)) - keep_names - RAW_RETIRE:
        raise ValueError(
            "unknown raw artifacts; expand retention explicitly before conversion"
        )
    if client_folder.exists() and set(os.listdir(client_folder)) - CLIENT_RETIRE:
        raise ValueError("unknown client artifacts; refuse deleting case directory")
    plan = {
        "data": data,
        "remove": remove,
        "planning": None if planning is None else str(planning),
    }
    if (
        data["model"].get("request_template_sha256")
        and (client_folder / "wire-request.json").exists()
    ):
        template, key, original = request_template(
            read(client_folder / "wire-request.json"),
            records["client"]["public-observation.json"],
        )
        if (
            key != data["model"]["request_template_sha256"]
            or original != data["model"]["canonical_request_sha256"]
        ):
            raise ValueError("shared request template/source mismatch")
        plan["request_template"] = template
    return plan


def publish(plan):
    folder = Path(plan["data"]["raw_folder"])
    target = folder / "meta.json"
    temp = folder / "meta.json.tmp"
    content = (
        json.dumps(
            plan["data"], ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode()
        + b"\n"
    )
    if target.exists():
        if read(target) != plan["data"]:
            raise ValueError("canonical metadata conflict")
        content = target.read_bytes()
        plan["new_metadata"] = False
    else:
        with temp.open("xb") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        if json.loads(temp.read_bytes()) != plan["data"]:
            raise ValueError("metadata round-trip mismatch")
        temp.rename(target)
        fsync_directory(folder)
        plan["new_metadata"] = True
    validate_data(Episode(target).data, media=False)
    return {
        "id": plan["data"]["id"],
        "request_id": plan["data"]["request_id"],
        "metadata": str(target),
        "sha256": sha(content),
        "frames": plan["data"]["frames"],
    }


def catalog(client_root, *, database=None):
    db = sqlite3.connect(database or Path(client_root) / "episodes.sqlite3", timeout=60)
    db.execute("PRAGMA synchronous=FULL")
    db.execute(
        "CREATE TABLE IF NOT EXISTS episodes(id TEXT PRIMARY KEY,request_id TEXT UNIQUE,metadata TEXT UNIQUE,sha256 TEXT,frames INTEGER)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS request_templates(sha256 TEXT PRIMARY KEY, body TEXT)"
    )
    db.commit()
    return db


def configure_concurrent_catalog(client_root):
    """Enable readers during worker commits, before admitting queue tasks."""
    with closing(catalog(client_root)) as db:
        mode = db.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if mode != "wal":
            raise RuntimeError("cannot enable concurrent catalog readers")


def finish_concurrent_catalog(client_root):
    """Fold the WAL into the catalog once all queue tasks have finished."""
    with closing(catalog(client_root)) as db:
        busy, _, _ = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if busy or db.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
            raise RuntimeError("catalog checkpoint still has active users")


def retire(plan):
    freed = allocated = 0
    for name, expected in plan["remove"]:
        p = Path(name)
        if identity(p) != expected:
            raise ValueError("artifact changed after validation: " + name)
        s = p.lstat()
        p.unlink()
        freed += 1
        if s.st_nlink == 1:
            allocated += s.st_blocks * 512
    if plan["planning"]:
        root = Path(plan["planning"])
        if root.exists():
            for directory, dirs, files in os.walk(root, topdown=False):
                if files:
                    raise ValueError("unretired planning artifacts")
                Path(directory).rmdir()
                freed += 1
    client = Path(plan["data"]["client_folder"])
    if client.exists():
        client.rmdir()
        freed += 1
    fsync_directory(Path(plan["data"]["raw_folder"]))
    return {
        "freed_inodes": freed - int(plan.get("new_metadata", False)),
        "retired_allocated_bytes": allocated,
        "id": plan["data"]["id"],
        "frames": plan["data"]["frames"],
    }


def _prepare_entry(entry, *, full):
    """Picklable preparation entry point for thread or process executors."""
    return prepare_episode(*entry, full=full)


def _publish_entry(plan):
    row = publish(plan)
    # Process workers mutate their own copy; return publication state explicitly.
    return row, plan["new_metadata"]


def _commit_publications(client_root, plans, rows):
    """Durably catalog published metadata before allowing its retirement."""
    with _CATALOG_WRITE_LOCK if _CATALOG_WRITE_LOCK is not None else nullcontext():
        with closing(catalog(client_root)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            for plan in plans:
                if "request_template" in plan:
                    db.execute(
                        "INSERT OR IGNORE INTO request_templates VALUES(?,?)",
                        (
                            plan["data"]["model"]["request_template_sha256"],
                            json.dumps(
                                plan["request_template"],
                                ensure_ascii=False,
                                allow_nan=False,
                            ),
                        ),
                    )
            for row in rows:
                old = db.execute(
                    "SELECT metadata,sha256 FROM episodes WHERE id=?", (row["id"],)
                ).fetchone()
                if old is not None and old != (row["metadata"], row["sha256"]):
                    raise ValueError("catalog identity conflict")
            db.executemany(
                "INSERT OR IGNORE INTO episodes VALUES(:id,:request_id,:metadata,:sha256,:frames)",
                rows,
            )
        fsync_directory(client_root)


def convert_episode(entry, *, full=False, expected_hashes=None, audit=None):
    """Complete one episode in the calling worker, including its catalog commit."""
    plan = prepare_episode(*entry, full=full)
    if plan is None:
        return None
    for name, digest in (expected_hashes or {}).items():
        if plan["data"]["original_sha256"].get(name) != digest:
            raise ValueError("source differs from frozen reconciliation: " + name)
    if audit:
        audit(
            {
                "phase": "validated",
                "plans": [
                    {
                        "id": plan["data"]["id"],
                        "remove": plan["remove"],
                        "planning": plan["planning"],
                    }
                ],
            }
        )
    row = publish(plan)
    _commit_publications(Path(entry[0]).parent, [plan], [row])
    if audit:
        audit({"phase": "committed", "episodes": [row]})
    result = retire(plan)
    if audit:
        audit({"phase": "retired", "results": [result]})
    return result


def convert_episode_directory(entry, *, full=False, expected_hashes=None, audit=None):
    """Complete one directory without reading or writing a shared database."""
    client, shared = entry[:2]
    metadata = entry[2] if len(entry) == 3 else None
    plan = prepare_episode(
        client,
        shared,
        full=full,
        use_catalog=False,
        metadata=metadata,
    )
    if plan is None:
        return None
    if (
        metadata is not None
        and (Path(plan["data"]["raw_folder"]) / "meta.json").absolute()
        != Path(metadata).absolute()
    ):
        raise ValueError("task manifest/canonical location mismatch")
    for name, digest in (expected_hashes or {}).items():
        if plan["data"]["original_sha256"].get(name) != digest:
            raise ValueError("source differs from frozen reconciliation: " + name)
    root = str(Path(client).absolute().parent)
    key = plan["data"]["model"]["request_template_sha256"]
    template = _load_request_templates(root).get(key)
    if template is None or (
        "request_template" in plan and plan["request_template"] != template
    ):
        raise ValueError("episode differs from durable run request template")
    if audit:
        audit(
            {
                "phase": "validated",
                "plans": [
                    {
                        "id": plan["data"]["id"],
                        "remove": plan["remove"],
                        "planning": plan["planning"],
                    }
                ],
            }
        )
    row = publish(plan)
    if audit:
        audit({"phase": "published", "episodes": [row]})
    result = retire(plan)
    result.update(client_root=root, catalog_row=row)
    if audit:
        audit({"phase": "retired", "results": [result]})
    return result


def rebuild_catalog(client_root, rows):
    """Build and verify a derived index, then atomically replace the old index."""
    root = Path(client_root).absolute()
    database = root / "episodes.sqlite3"
    before = identity(database) if database.exists() else None
    if any(
        (root / ("episodes.sqlite3" + suffix)).exists()
        for suffix in ["-wal", "-shm", "-journal"]
    ):
        raise ValueError(
            "catalog must be quiescent and checkpointed before replacement"
        )
    temp = root / f"episodes.sqlite3.rebuild-{os.getpid()}"
    if temp.exists():
        raise ValueError("unfinished catalog rebuild exists")
    count = 0
    with closing(catalog(root, database=temp)) as db:
        with db:
            db.executemany(
                "INSERT INTO request_templates VALUES(?,?)",
                [
                    (key, json.dumps(body, ensure_ascii=False, allow_nan=False))
                    for key, body in _load_request_templates(str(root)).items()
                ],
            )
            for row in rows:
                path = Path(row["metadata"])
                if (
                    not path.is_absolute()
                    or path.name != "meta.json"
                    or path.parent.name != row["request_id"]
                    or path.parent.parent.name != "tasks"
                ):
                    raise ValueError("invalid catalog metadata path")
                db.execute(
                    "INSERT INTO episodes VALUES(:id,:request_id,:metadata,:sha256,:frames)",
                    row,
                )
                count += 1
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("rebuilt catalog integrity failed")
        if db.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] != count:
            raise ValueError("rebuilt catalog count differs")
    with temp.open("rb") as f:
        os.fsync(f.fileno())
    if (identity(database) if database.exists() else None) != before:
        raise ValueError("catalog changed during rebuild")
    temp.replace(database)
    fsync_directory(root)
    return count


def _convert_queue_entry(
    entry, *, full, expected_hashes, audit, audit_dir, directory_only
):
    convert = convert_episode_directory if directory_only else convert_episode
    if audit_dir is None:
        return convert(
            entry,
            full=full,
            expected_hashes=expected_hashes,
            audit=audit,
        )
    if audit is not None:
        raise ValueError("choose an audit callback or a worker audit directory")
    # Each process handles one episode at a time and owns its append-only log.
    with gzip.open(Path(audit_dir) / f"worker-{os.getpid()}.jsonl.gz", "at") as log:

        def record(event):
            log.write(
                json.dumps({"pid": os.getpid(), **event}, separators=(",", ":")) + "\n"
            )
            log.flush()
            os.fsync(log.fileno())

        return convert(
            entry,
            full=full,
            expected_hashes=expected_hashes,
            audit=record,
        )


def convert_stream(
    entries,
    *,
    executor,
    max_pending=256,
    full=False,
    audit=None,
    audit_dir=None,
    expected_hashes=None,
    should_stop=None,
    directory_only=False,
):
    """One queue of complete episode tasks, consumed by the executor's workers.

    The caller only feeds the queue and collects results. Each worker performs
    validation, publication, a SQLite transaction and retirement in order.
    SQLite serializes its own writers; no stage returns to the coordinator.
    """
    if max_pending < 1:
        raise ValueError("max_pending must be positive")
    if audit_dir is not None:
        Path(audit_dir).mkdir(parents=True, exist_ok=True)
    source = iter(entries)
    pending, seen = set(), set()
    exhausted = False
    try:
        while pending or not exhausted:
            while not exhausted and len(pending) < max_pending:
                if should_stop and should_stop():
                    exhausted = True
                    break
                try:
                    entry = next(source)
                except StopIteration:
                    exhausted = True
                    break
                folder = Path(entry[0]).absolute()
                if folder in seen:
                    raise ValueError("duplicate case in conversion queue")
                seen.add(folder)
                hashes = expected_hashes or {}
                expected = hashes.get(str(folder), hashes.get(folder.name, {}))
                pending.add(
                    executor.submit(
                        _convert_queue_entry,
                        entry,
                        full=full,
                        expected_hashes=expected,
                        audit=audit,
                        audit_dir=audit_dir,
                        directory_only=directory_only,
                    )
                )
            if not pending:
                break
            completed, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in completed:
                pending.remove(future)
                result = future.result()
                if result is not None:
                    yield result
    finally:
        for future in pending:
            future.cancel()


def convert_batch(
    entries, *, workers=4, full=False, audit=None, expected_hashes=None, executor=None
):
    """All canonical files and the catalog commit before any original is removed."""
    if not entries:
        return []
    client_root = Path(entries[0][0]).parent
    if len({str(Path(folder).absolute()) for folder, _ in entries}) != len(entries):
        raise ValueError("duplicate case in conversion batch")
    if any(Path(folder).parent != client_root for folder, _ in entries):
        raise ValueError("one client root per batch required")
    with (
        nullcontext(executor)
        if executor is not None
        else ThreadPoolExecutor(max_workers=workers)
    ) as pool:
        plans = list(pool.map(partial(_prepare_entry, full=full), entries))
    plans = [p for p in plans if p is not None]
    for plan in plans:
        expected = (expected_hashes or {}).get(plan["data"]["id"], {})
        for name, digest in expected.items():
            if plan["data"]["original_sha256"].get(name) != digest:
                raise ValueError("source differs from frozen reconciliation: " + name)
    if audit:
        audit(
            {
                "phase": "validated",
                "plans": [
                    {
                        "id": p["data"]["id"],
                        "remove": p["remove"],
                        "planning": p["planning"],
                    }
                    for p in plans
                ],
            }
        )
    with (
        nullcontext(executor)
        if executor is not None
        else ThreadPoolExecutor(max_workers=workers)
    ) as pool:
        publications = list(pool.map(_publish_entry, plans))
    rows = []
    for plan, (row, new_metadata) in zip(plans, publications):
        plan["new_metadata"] = new_metadata
        rows.append(row)
    _commit_publications(client_root, plans, rows)
    if audit:
        audit({"phase": "committed", "episodes": rows})
    with (
        nullcontext(executor)
        if executor is not None
        else ThreadPoolExecutor(max_workers=workers)
    ) as pool:
        results = list(pool.map(retire, plans))
    if audit:
        audit({"phase": "retired", "results": results})
    return results


def inspect_episode(path, *, full_media=False):
    """Validate one compact episode; media bytes are checked only when asked."""
    return validate_data(Episode(path).data, full=full_media)


