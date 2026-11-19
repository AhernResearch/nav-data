"""Training-side collection orchestration. All simulation happens in sim-engine.

Only task submission, model requests, receipt reads and training-label derivation
live here. No Simulator import, GPU worker lifecycle, physics or movement retry.
"""

import hashlib
import json
import math
import threading
import time
import traceback
from collections import Counter
from pathlib import Path
from urllib import request
from urllib.parse import urlsplit

from nav_data.collection.pool import ModelPool
from nav_data.records import read, write

TERMINAL = {
    "completed_audit",
    "excluded_before_movement",
    "failed_or_uncertain",
    "cancelled_before_movement",
    "planning_budget_exhausted",
}


class CollectionHTTP:
    def __init__(self, url, timeout=30):
        parsed = urlsplit(url)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "collection URL must not contain credentials or query parameters"
            )
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("collection HTTP timeout must be positive and finite")
        self.url, self.timeout = url.rstrip("/"), timeout

    def call(self, method, path, value=None):
        payload = None if value is None else json.dumps(value, allow_nan=False).encode()
        req = request.Request(
            self.url + path,
            data=payload,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        # A lost response never triggers a second submission or movement command.
        began = time.perf_counter()
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                return json.load(response)
        except TimeoutError as exc:
            raise TimeoutError(
                f"collection RPC {method} {path} timed out after "
                f"{time.perf_counter() - began:.3f}s (timeout={self.timeout}s); "
                "outcome uncertain; no resubmission"
            ) from exc


def _subset(expected, actual):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            k in actual and _subset(v, actual[k]) for k, v in expected.items()
        )
    return expected == actual


def measure_reference(config, episode):
    """Read-only server NavMesh measurement for train-source chunk preparation."""
    service = config.get("collection_service")
    if not service:
        raise ValueError("reference geometry requires sim-engine collection_service")
    body = {
        "owner": hashlib.sha256(
            json.dumps(config, sort_keys=True).encode()
        ).hexdigest()[:32],
        "kind": "reference_geometry",
        "dataset": service["dataset"],
        "camera": service["camera"],
        "scene_id": episode["scene_id"],
        "seed": config["seed"],
        "start_position": episode["reference_path"][0],
        "start_rotation": episode["start_rotation"],
        "reference_guides": episode["reference_path"],
    }
    http = CollectionHTTP(service["url"])
    identity = http.call("GET", "/v1/health")["service_owner"]
    body["request_id"] = hashlib.sha256(
        (json.dumps(body, sort_keys=True) + identity).encode()
    ).hexdigest()
    state = http.call("POST", "/v1/collections", body)
    deadline = time.monotonic() + float(service.get("reference_timeout_s", 900))
    while state["status"] not in TERMINAL | {"reference_complete"}:
        if time.monotonic() > deadline:
            raise TimeoutError("reference query did not finish; no resubmission")
        time.sleep(0.05)
        state = http.call("GET", "/v1/collections/" + body["request_id"])
    if state["status"] != "reference_complete":
        raise ValueError("reference geometry failed: " + str(state.get("error")))
    return state["reference_legs"]


def _task(manifest, candidate, kind, service):
    owner = manifest["owner"]
    cid = hashlib.sha256((owner + ":" + candidate["id"]).encode()).hexdigest()
    if kind == "short_instructions":
        item = candidate["case"]
        episode = item["episode"]
        from nav_data.tasks.pointnav import require_train

        require_train({"backend_manifest": manifest, "collection_service": service}, item)
        extra = {}
    else:
        task = candidate["task"]
        if "reference_guides" not in task:
            raise ValueError(
                "server collection requires the pinned sparse R2R reference representation"
            )
        yaw = task["source_start_yaw"]
        import math

        scene_root = Path(manifest["runtime"]["settings"]["simulation"]["scenes_root"])
        episode = {
            "scene_id": str(Path(task["scene"]).relative_to(scene_root)),
            "start_position": task["reference_guides"][0],
            "start_rotation": [0.0, math.sin(yaw / 2), 0.0, math.cos(yaw / 2)],
        }
        extra = {
            key: candidate[key] for key in ("offset", "yaw_delta", "reference_fraction")
        }
        extra.update(
            reference_guides=task["reference_guides"],
            goal_position=task["goal_position"],
            instruction=task["instruction"],
        )
    return dict(
        request_id=cid,
        owner=owner,
        kind=kind,
        dataset=service["dataset"],
        camera=service["camera"],
        scene_id=episode["scene_id"],
        start_position=episode["start_position"],
        start_rotation=episode["start_rotation"],
        seed=manifest["config"]["seed"],
        **extra,
    )


def _output(response, shared_root):
    output = Path(response["output"]).resolve()
    if (
        shared_root not in output.parents
        or output.parent.name != "tasks"
        or output.name != response["request_id"]
    ):
        raise ValueError("service artifact escaped configured shared root")
    return output


def _link_raw(folder, source):
    names = [
        "camera.json",
        "view-sensors.json",
        "start-state.json",
        "grounded-target.json",
        "grounding-check.json",
        "private-geometry.json",
        "geometry-result.json",
        "preflight.json",
        "reference-check.json",
        "selected-goal.json",
        "planning-claim.json",
        "planning-result.json",
        "execution-lease.json",
        "terminal.json",
        "recording-check.json",
        "region-terminal-check.json",
        "world_states.jsonl",
        "commands.jsonl",
        "rgb",
        "depth",
    ]
    for name in names:
        if (source / name).exists():
            (folder / name).symlink_to(
                source / name, target_is_directory=(source / name).is_dir()
            )


def _labels(config, candidate, folder, receipt):
    from nav_data.tasks.pointnav import save_outcome, target_record

    task = read(folder / "proposed-task.json")
    grounded = read(folder / "grounded-target.json")
    selected = read(folder / "selected-goal.json")
    goal = read(folder / "private-geometry.json")["requested_goal_position"]
    if goal != selected["position"] or selected["source"] != "planned_before_movement":
        raise ValueError("intended goal does not match the pre-movement server record")
    target = target_record(config, candidate["case"], task, grounded, goal)
    write(folder / "pointnav-target.json", target)
    save_outcome(
        folder,
        target,
        receipt["geometry"],
        receipt["recording_complete"],
        receipt["region_terminal"],
    )


def run_remote(
    manifest,
    output,
    kind,
    service,
    *,
    prepared_output=False,
    transport=None,
    model_call=None,
):
    """Bounded cross-task pipeline: submit, infer from ready views, freeze target, join receipts."""
    output = Path(output)
    shared = Path(service["shared_root"]).resolve()
    http = transport or CollectionHTTP(service["url"], service.get("http_timeout_s", 30))
    health = http.call("GET", "/v1/health")
    if (
        health["status"] != "ok"
        or service["dataset"] not in health["training_datasets"]
    ):
        raise ValueError("collection service is not ready for this training dataset")
    if Path(health["root"]).resolve() != shared:
        raise ValueError(
            "collection service output root differs from configured shared storage"
        )
    expected_camera = manifest["runtime"]["camera"]
    if not _subset(expected_camera, health["cameras"].get(service["camera"])):
        raise ValueError(
            "collection service camera differs from frozen training configuration"
        )
    datasets = manifest["runtime"]["settings"]["simulation"]["datasets"]
    source_dataset = service["dataset"] if service["dataset"] in datasets else "train_pilot"
    physics = datasets[source_dataset]["physics"]
    if not _subset(physics, health["physics"][service["dataset"]]):
        raise ValueError(
            "collection service physics differs from frozen training configuration"
        )
    for key, value in health.get("geometry_profiles", {}).get(kind, {}).items():
        if (
            key not in {"seed", "split"}
            and key in manifest["config"]
            and manifest["config"][key] != value
        ):
            raise ValueError(
                "collection geometry profile differs from frozen configuration: " + key
            )
    if manifest["config"].get("split") != "train":
        if kind != "short_instructions" or manifest["config"].get("split") != "development":
            raise ValueError("collection requires train inputs or explicit GS development inputs")
        from nav_data.tasks.pointnav import require_train

        for candidate in manifest["candidates"]:
            require_train({"backend_manifest": manifest, "collection_service": service},
                          candidate["case"])
    candidates = manifest["candidates"]
    ids = [c["id"] for c in candidates]
    import re

    if (
        not ids
        or len(set(ids)) != len(ids)
        or any(not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", cid) for cid in ids)
    ):
        raise ValueError("nonempty unique safe candidate identities required")
    admission = None
    if health.get("scene_schedule"):
        from nav_data.collection.admission import SceneAdmission

        def scene_key(candidate):
            value = _task(manifest, candidate, kind, service)
            return value["dataset"], value["scene_id"], value["camera"]

        admission = SceneAdmission(
            candidates, scene_key, health, service.get("per_scene_pending", 8)
        )
    root = output if kind == "short_instructions" else output / "candidates"
    if prepared_output:
        if any((root / cid).exists() for cid in ids):
            raise FileExistsError("prepared collection already contains candidate data")
    else:
        output.mkdir(parents=True, exist_ok=False)
    write(
        output / "started.json",
        {
            "owner": manifest["owner"],
            "service": health,
            "automatic_retry": False,
            "execution_backend": "sim_engine_collection_http",
        },
    )
    started = time.perf_counter()
    pending, next_index, results = {}, 0, []
    submitted_ids = set()
    result_counts = Counter()
    last_progress = 0.0
    stop = threading.Event()
    capacity = min(int(service.get("max_pending", 64)), health["capacity"])
    if capacity < 1:
        raise ValueError("positive collection prefetch capacity required")
    abort = None

    def consume(job, profile, enqueued):
        candidate, state = job
        folder = root / candidate["id"]
        source = _output(state, shared)
        write(
            folder / "model-started.json",
            {
                "owner": manifest["owner"],
                "profile": profile["id"],
                "automatic_retry": False,
                "started_unix": time.time(),
            },
        )
        began = time.perf_counter()
        try:
            public = state["public"]
            if set(public) != (
                {"images"}
                if kind == "short_instructions"
                else {"instruction", "images"}
            ):
                raise ValueError("unexpected service observation fields")
            write(folder / "public-observation.json", public)
            if model_call is None:
                if kind == "short_instructions":
                    from nav_data.tasks.short_instructions import propose_task

                    value = propose_task(
                        public,
                        profile,
                        folder,
                        manifest["config"]["request_timeout_s"],
                        observation_folder=source,
                        response_mode=manifest["workflow_config"].get("response_mode", "tool_call"),
                        **(
                            {"compact_evidence": True}
                            if manifest["workflow_config"].get(
                                "compact_request_evidence"
                            )
                            else {}
                        ),
                    )
                else:
                    from nav_data.tasks.instruction import annotate

                    value = annotate(
                        public,
                        profile,
                        folder,
                        manifest["config"]["request_timeout_s"],
                        observation_folder=source,
                    )
            else:
                value = model_call(
                    public, profile, folder, manifest["config"]["request_timeout_s"]
                )
            if kind == "short_instructions":
                write(folder / "proposed-task.json", value)
                if stop.is_set() or value["needs_review"]:
                    http.call("DELETE", "/v1/collections/" + state["request_id"])
                else:
                    target = {
                        "target_view": value["target_view"],
                        "bbox": value["bbox"],
                    }
                    write(folder / "target-submission-started.json", target)
                    http.call(
                        "POST",
                        "/v1/collections/" + state["request_id"] + "/target",
                        target,
                    )
            result = {"status": "completed", "annotation": value}
        except Exception as exc:  # noqa: BLE001 - freeze uncertain model evidence without replay
            result = {
                "status": "failed_or_uncertain",
                "error": repr(exc),
                "automatic_retry": False,
            }
            try:
                http.call("DELETE", "/v1/collections/" + state["request_id"])
            except Exception as cancel_error:  # noqa: BLE001 - lost cancellation is uncertain
                write(
                    folder / "model-cancel-failed.json", {"error": repr(cancel_error)}
                )
        result.update(
            wall_s=time.perf_counter() - began,
            queue_wait_s=began - enqueued,
            profile=profile["id"],
        )
        write(folder / "model-result.json", result)
        write(folder / "model-finished.json", {"status": result["status"]})

    pool = ModelPool(
        manifest["config"]["model_profiles"],
        manifest["config"]["ready_queue_size"],
        consume,
    ).start()
    try:
        while pending or (
            admission.remaining if admission else next_index < len(candidates)
        ):
            admission_health = http.call("GET", "/v1/health") if admission else None
            while len(pending) < capacity and (
                admission.remaining if admission else next_index < len(candidates)
            ):
                candidate = (
                    admission.take(admission_health, pending)
                    if admission
                    else candidates[next_index]
                )
                if candidate is None:
                    if not pending and admission.remaining:
                        raise RuntimeError(
                            "rolling scene schedule stalled; no active work or replay"
                        )
                    break
                folder = root / candidate["id"]
                folder.mkdir(parents=True)
                value = _task(manifest, candidate, kind, service)
                write(
                    folder / "lease.json",
                    {
                        "owner": manifest["owner"],
                        "request_id": value["request_id"],
                        "automatic_retry": False,
                    },
                )
                write(folder / "submission-started.json", value)
                pending[value["request_id"]] = {
                    "candidate": candidate,
                    "submitted_model": False,
                }
                next_index += 1
                submitted_ids.add(candidate["id"])
                http.call("POST", "/v1/collections", value)
            if not pending:
                break
            responses = http.call(
                "POST", "/v1/collections/status", {"request_ids": list(pending)}
            )
            for state in responses:
                row = pending[state["request_id"]]
                candidate = row["candidate"]
                folder = root / candidate["id"]
                if "public" in state and not row["submitted_model"]:
                    pool.submit(
                        (candidate, state),
                        check=lambda: pool.check(
                            manifest["config"]["request_timeout_s"] + 5
                        ),
                    )
                    row["submitted_model"] = True
                if state["status"] not in TERMINAL:
                    continue
                if (
                    row["submitted_model"]
                    and not (folder / "model-finished.json").exists()
                ):
                    continue
                model = (
                    read(folder / "model-result.json")
                    if row["submitted_model"]
                    else None
                )
                source = _output(state, shared)
                write(folder / "server-receipt.json", state)
                _link_raw(folder, source)
                status = state["status"]
                if status == "completed_audit" and (
                    not model or model["status"] != "completed"
                ):
                    status = "failed_or_uncertain"
                if status == "cancelled_before_movement":
                    status = (
                        "excluded_before_movement"
                        if model and model["status"] == "completed"
                        else "failed_or_uncertain"
                    )
                if kind == "short_instructions":
                    result = {
                        "id": candidate["id"],
                        "status": status,
                        "geometry": state.get("geometry"),
                        "grounding": state.get("grounding"),
                        "region_terminal": state.get("region_terminal"),
                        "recording_complete": state.get("recording_complete", False),
                        "semantic_admission": "pending_review",
                    }
                    if status == "completed_audit":
                        _labels(manifest["workflow_config"], candidate, folder, state)
                else:
                    result = {
                        "candidate_id": candidate["id"],
                        "status": status,
                        "geometry": read(folder / "geometry-result.json")
                        if (folder / "geometry-result.json").exists()
                        else None,
                        "model": model,
                        "stop_label_policy": "expert_terminal_receipt_only",
                    }
                result.update(
                    planning_outcome=state.get("planning_outcome"),
                    automatic_retry=False,
                    admission="audit_only",
                    service_request_id=state["request_id"],
                )
                write(folder / "result.json", result)
                if status == "completed_audit" and service.get("compact_completed_episodes", False):
                    if kind != "short_instructions":
                        raise ValueError("compact episode output requires free-instruction collection")
                    from nav_data.release.compact import convert_batch

                    convert_batch([(folder, shared)], workers=1)
                results.append(result)
                result_counts[status] += 1
                del pending[state["request_id"]]
            if time.perf_counter() - last_progress >= 5:
                from nav_data.records import write_text_atomic

                write_text_atomic(
                    output / "progress.json",
                    json.dumps(
                        {
                            "owner": manifest["owner"],
                            "wall_s": time.perf_counter() - started,
                            "submitted": next_index,
                            "counts": dict(result_counts),
                            "pending": len(pending),
                            "automatic_retry": False,
                        }
                    )
                    + "\n",
                )
                last_progress = time.perf_counter()
            pool.check(manifest["config"]["request_timeout_s"] + 5)
            if pending:
                time.sleep(float(service.get("poll_interval_s", 0.1)))
    except BaseException as exc:  # noqa: BLE001 - interrupts must retain leases and cancel owned work
        abort = repr(exc)
        stop.set()
        pool.cancel.set()
        write(output / "interrupted.json", {
            "error": abort, "traceback": traceback.format_exc(), "automatic_retry": False,
        })
        for cid in pending:
            try:
                http.call("DELETE", "/v1/collections/" + cid)
            except Exception as cancel_error:  # noqa: BLE001 - preserve ambiguous cancellation
                folder = root / pending[cid]["candidate"]["id"]
                write(
                    folder / "abort-cancel-failed.json", {"error": repr(cancel_error)}
                )
    finally:
        pool.close(
            cancel=bool(abort),
            progress_timeout=manifest["config"]["request_timeout_s"] + 5,
        )
    finished_ids = {r.get("id", r.get("candidate_id")) for r in results}
    for candidate in candidates:
        folder = root / candidate["id"]
        if candidate["id"] not in finished_ids:
            not_needed = bool(
                admission and not abort and candidate["id"] not in submitted_ids
                and admission.key(candidate) not in admission.unavailable_keys
            )
            result = {
                "id" if kind == "short_instructions" else "candidate_id": candidate[
                    "id"
                ],
                "status": "not_needed"
                if not_needed
                else "failed_or_uncertain"
                if candidate["id"] in submitted_ids
                else "not_started",
                "automatic_retry": False,
                "admission": "audit_only",
            }
            if candidate["id"] in submitted_ids:
                write(folder / "result.json", result)
            results.append(result)

    summary = {
        "owner": manifest["owner"],
        "wall_s": time.perf_counter() - started,
        "counts": dict(Counter(r["status"] for r in results)),
        "aborted": abort,
        "model_threads_still_running": pool.stats()["threads_still_running"],
        "concurrency": pool.stats(),
        "service": health,
        "collection_adapter": kind,
        "worker_exit_codes": {},
        "training_exports": 0,
        "vla_executions": 0,
        "admission": "audit_only",
    }
    goals = [s.get("success_goal") for s in health.get("scene_schedule", [])]
    success_target = sum(g for g in goals if g is not None)
    summary["success_target"] = success_target or None
    summary["success_target_met"] = (
        result_counts["completed_audit"] == success_target if success_target else None
    )
    if kind == "short_instructions":
        summary["cases"] = results
    write(output / "summary.json", summary)
    return summary
