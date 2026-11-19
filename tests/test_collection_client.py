"""Training orchestration uses receipts and shared files; it never owns a Simulator."""

import copy
import json
import threading
from pathlib import Path

import pytest
from PIL import Image
from recordings import recording

from nav_data.collection.admission import SceneAdmission
from nav_data.collection.client import CollectionHTTP, _task, run_remote
from nav_data.records import digest, read, write
from nav_data.tasks import short_instructions as short


@pytest.mark.parametrize("method,path,value", [
    ("GET", "/v1/health", None),
    ("POST", "/v1/collections", {"request_id": "one"}),
    ("POST", "/v1/collections/one/target", {"bbox": [0, 0, 1, 1]}),
])
def test_rpc_timeout_identifies_request_without_replay(monkeypatch, method, path, value):
    calls = []

    def timeout(req, *, timeout):
        calls.append((req.method, req.full_url, timeout))
        raise TimeoutError("timed out")

    monkeypatch.setattr("nav_data.collection.client.request.urlopen", timeout)
    with pytest.raises(TimeoutError, match="outcome uncertain; no resubmission") as error:
        CollectionHTTP("http://localhost:18813", 120).call(method, path, value)
    assert f"{method} {path}" in str(error.value)
    assert "timeout=120s" in str(error.value)
    assert calls == [(method, "http://localhost:18813" + path, 120)]


@pytest.mark.parametrize("timeout", [0, -1, True, "120", float("nan"), float("inf")])
def test_rpc_rejects_invalid_timeout(timeout):
    with pytest.raises(ValueError, match="positive and finite"):
        CollectionHTTP("http://localhost", timeout)


class FakeService:
    def __init__(self, root):
        self.root = root
        root.mkdir()
        self.rows = {}
        self.calls = []
        self.lock = threading.Lock()
        self.executions = []

    def call(self, method, path, value=None):
        with self.lock:
            self.calls.append((method, path))
            if path == "/v1/health":
                return {
                    "status": "ok",
                    "training_datasets": ["train_pilot"],
                    "capacity": 8,
                    "root": str(self.root),
                    "cameras": {"camera": {"width": 4, "height": 2}},
                    "physics": {"train_pilot": {"allow_sliding": True}},
                }
            if path == "/v1/collections":
                cid = value["request_id"]
                folder = self.root / "tasks" / cid
                folder.mkdir(parents=True)
                public = {"images": {}}
                for view in ["front", "right", "back", "left"]:
                    p = folder / (view + ".png")
                    Image.new("RGB", (4, 2)).save(p)
                    public["images"][view] = {"path": str(p), "sha256": digest(p)}
                if value["kind"] == "r2r_chunks":
                    public["instruction"] = value["instruction"]
                self.rows[cid] = {
                    "request": value,
                    "request_id": cid,
                    "status": "observation_ready",
                    "output": str(folder),
                    "public": public,
                }
                return dict(self.rows[cid])
            if path == "/v1/collections/status":
                result = []
                for cid in value["request_ids"]:
                    row = self.rows[cid]
                    if (
                        row["request"]["kind"] == "r2r_chunks"
                        and row["status"] == "observation_ready"
                    ):
                        self.execute(row)
                    result.append({k: v for k, v in row.items() if k != "request"})
                return copy.deepcopy(result)
            cid = path.split("/")[3]
            row = self.rows[cid]
            if method == "DELETE":
                row["status"] = "cancelled_before_movement"
                return dict(row)
            if path.endswith("/target"):
                row["target"] = value
                self.execute(row)
                return {k: v for k, v in row.items() if k != "request"}
            raise AssertionError(path)

    def execute(self, row):
        cid = row["request_id"]
        folder = Path(row["output"])
        assert cid not in self.executions
        self.executions.append(cid)
        recording(folder)
        goal = [0.0, 0.0, -1.15]
        ep = dict(row["request"], goals=[{"position": goal, "radius": 0.08}])
        write(
            folder / "private-geometry.json",
            {"episode": ep, "requested_goal_position": goal},
        )
        write(
            folder / "grounded-target.json",
            {"navmesh_point": goal, "world_point": [0.0, 0.01, -1.15]},
        )
        write(
            folder / "selected-goal.json",
            {
                "position": goal,
                "goal_tolerance_m": 0.12,
                "source": "planned_before_movement",
            },
        )
        geometry = {
            "passed": True,
            "termination": "expert_stop",
            "collision_steps": 0,
            "final_distance_m": 0.05,
            "source": "fake",
        }
        write(
            folder / "geometry-result.json",
            {"status": "geometry_complete", "geometry": geometry},
        )
        row.update(
            status="completed_audit",
            geometry=geometry,
            recording_complete=True,
            grounding=read(folder / "grounded-target.json"),
            region_terminal={"inside_original_bbox": True},
        )


def manifest(tmp_path, kind, count=4):
    profiles = [
        {"id": "model", "model": "fake", "endpoint": "http://unused", "slots": 2}
    ]
    cfg = {
        "seed": 1,
        "split": "train",
        "goal_tolerance_m": 0.12,
        "model_profiles": profiles,
        "ready_queue_size": 4,
        "request_timeout_s": 2,
    }
    runtime = {
        "camera": {"width": 4, "height": 2},
        "settings": {
            "simulation": {
                "scenes_root": str(tmp_path),
                "datasets": {"train_pilot": {"physics": {"allow_sliding": True}}},
            }
        },
    }
    value = {"owner": "b" * 32, "config": cfg, "runtime": runtime, "candidates": []}
    for i in range(count):
        episode = {
            "_dataset": "train_pilot",
            "scene_id": "room.glb",
            "start_position": [0, 0, 0],
            "start_rotation": [0, 0, 0, 1],
        }
        candidate = {"id": f"case-{i}", "case": {"id": f"case-{i}", "episode": episode}}
        if kind == "r2r_chunks":
            candidate.update(
                offset=[0, 0, 0],
                yaw_delta=0.0,
                reference_fraction=0.0,
                task={
                    "scene": str(tmp_path / "room.glb"),
                    "source_start_yaw": 0.0,
                    "reference_guides": [[0, 0, 0], [0, 0, -1.15]],
                    "goal_position": [0, 0, -1.15],
                    "instruction": "Reach the door.",
                },
            )
        value["candidates"].append(candidate)
    value["workflow_config"] = {
        "owner": value["owner"],
        "backend_manifest": {"config": cfg},
    }
    return value


@pytest.mark.parametrize("kind", ["short_instructions", "r2r_chunks"])
def test_prefetched_tasks_share_model_pool_and_raw_media_is_not_transferred_or_copied(
    tmp_path, kind
):
    server = FakeService(tmp_path / "server")
    m = manifest(tmp_path, kind)
    service = {
        "url": "http://fake",
        "shared_root": str(server.root),
        "dataset": "train_pilot",
        "camera": "camera",
        "max_pending": 4,
        "poll_interval_s": 0.01,
    }
    observed = []

    def model(public, profile, folder, timeout):
        assert len(server.rows) == 4, (
            "all observations can be prefetched before the first model result"
        )
        assert set(public) == (
            {"images"} if kind == "short_instructions" else {"images", "instruction"}
        )
        observed.append(folder.name)
        return {
            "instruction": "Reach the floor patch.",
            "target_view": "front",
            "bbox": [400, 600, 600, 800],
            "needs_review": False,
        }

    result = run_remote(
        m, tmp_path / "run", kind, service, transport=server, model_call=model
    )
    assert result["counts"] == {"completed_audit": 4}
    assert len(observed) == len(set(observed)) == len(server.executions) == 4
    base = (
        tmp_path / "run"
        if kind == "short_instructions"
        else tmp_path / "run/candidates"
    )
    for i in range(4):
        folder = base / f"case-{i}"
        assert (folder / "rgb").is_symlink() and (
            folder / "world_states.jsonl"
        ).is_symlink()
        if kind == "short_instructions":
            intended = read(folder / "pointnav-target.json")["episode"]["goals"][0][
                "position"
            ]
            assert intended == [0, 0, -1.15]
            assert (
                intended
                != read(folder / "pointnav-outcome.json")["last_observed_pose"][
                    "position"
                ]
            )
    assert result["concurrency"]["peak_inflight_per_profile"]["model"] <= 2
    assert not any("/step" in path or "/frames" in path for _, path in server.calls)


def test_model_failure_cancels_only_that_task_and_never_submits_a_target(tmp_path):
    server = FakeService(tmp_path / "server")
    m = manifest(tmp_path, "short_instructions", count=1)

    def fail(*args):
        raise TimeoutError("no model retry")

    service = {
        "url": "http://fake",
        "shared_root": str(server.root),
        "dataset": "train_pilot",
        "camera": "camera",
    }
    result = run_remote(
        m,
        tmp_path / "run",
        "short_instructions",
        service,
        transport=server,
        model_call=fail,
    )
    assert result["counts"] == {"failed_or_uncertain": 1}
    assert not server.executions
    assert (
        sum(
            method == "POST" and path == "/v1/collections"
            for method, path in server.calls
        )
        == 1
    )
    assert not any(path.endswith("/target") for _, path in server.calls)


def test_camera_mismatch_fails_before_admission(tmp_path):
    server = FakeService(tmp_path / "server")
    m = manifest(tmp_path, "short_instructions", count=1)
    m["runtime"]["camera"]["width"] = 480
    with pytest.raises(ValueError, match="camera"):
        run_remote(
            m,
            tmp_path / "run",
            "short_instructions",
            {
                "shared_root": str(server.root),
                "url": "http://fake",
                "dataset": "train_pilot",
                "camera": "camera",
            },
            transport=server,
        )
    assert not server.rows and not (tmp_path / "run").exists()


@pytest.mark.parametrize("kind", ["short_instructions", "r2r_chunks"])
def test_rolling_scene_admission_uses_server_window_instead_of_manifest_order(
    tmp_path, kind
):
    class RollingService(FakeService):
        def __init__(self, root):
            super().__init__(root)
            self.lock = threading.RLock()

        def schedule(self):
            result = []
            available = True
            for scene in ["second.glb", "first.glb"]:
                rows = [
                    r for r in self.rows.values() if r["request"]["scene_id"] == scene
                ]
                terminal = sum(r["status"] == "completed_audit" for r in rows)
                status = (
                    "complete" if terminal == 2 else "active" if available else "queued"
                )
                if status == "active":
                    available = False
                result.append(
                    {
                        "key": ["train_pilot", scene, "camera"],
                        "attempts": 2,
                        "claimed": len(rows),
                        "terminal": terminal,
                        "status": status,
                    }
                )
            return result

        def call(self, method, path, value=None):
            with self.lock:
                if path == "/v1/collections":
                    active = next(
                        s["key"][1] for s in self.schedule() if s["status"] == "active"
                    )
                    assert value["scene_id"] == active
                result = super().call(method, path, value)
                if path == "/v1/health":
                    result.update(
                        service_owner="owned-rolling-test",
                        scene_schedule=self.schedule(),
                    )
                return result

    server = RollingService(tmp_path / "server")
    m = manifest(tmp_path, kind)
    for i, c in enumerate(m["candidates"]):
        c["case"]["episode"]["scene_id"] = "first.glb" if i < 2 else "second.glb"
        if kind == "r2r_chunks":
            c["scene"] = str(tmp_path / c["case"]["episode"]["scene_id"])
            c["task"]["scene"] = c["scene"]
    result = run_remote(
        m,
        tmp_path / "run",
        kind,
        {
            "url": "http://fake",
            "shared_root": str(server.root),
            "dataset": "train_pilot",
            "camera": "camera",
            "per_scene_pending": 1,
            "poll_interval_s": 0.001,
        },
        transport=server,
        model_call=lambda *args: {
            "instruction": "Reach the patch.",
            "target_view": "front",
            "bbox": [400, 600, 600, 800],
            "needs_review": False,
        },
    )
    assert result["aborted"] is None
    assert result["counts"] == {"completed_audit": 4}
    assert [r["request"]["scene_id"] for r in server.rows.values()] == [
        "second.glb"
    ] * 2 + ["first.glb"] * 2
    assert len(server.executions) == 4


def test_rolling_admission_rejects_partial_budget_and_service_identity_changes():
    health = {
        "service_owner": "owned",
        "status": "ok",
        "scene_schedule": [
            {
                "key": ["train", "scene", "camera"],
                "attempts": 2,
                "claimed": 0,
                "status": "active",
            }
        ],
    }
    key = lambda candidate: ("train", "scene", "camera")
    with pytest.raises(ValueError, match="budgets"):
        SceneAdmission([{"id": 1}], key, health)
    admission = SceneAdmission([{"id": 1}, {"id": 2}], key, health)
    with pytest.raises(RuntimeError, match="changed or degraded"):
        admission.take(dict(health, service_owner="replacement"), {})
    with pytest.raises(RuntimeError, match="changed or degraded"):
        admission.take(dict(health, status="degraded"), {})


def test_success_target_uses_new_candidates_after_exclusion_and_leaves_reserve_unclaimed(
    tmp_path,
):
    class SuccessService(FakeService):
        def call(self, method, path, value=None):
            result = super().call(method, path, value)
            if path == "/v1/health":
                success = sum(
                    r["status"] == "completed_audit" for r in self.rows.values()
                )
                result.update(
                    service_owner="success-test",
                    scene_schedule=[
                        {
                            "key": ["train_pilot", "room.glb", "camera"],
                            "attempts": 4,
                            "claimed": len(self.rows),
                            "successful": success,
                            "success_goal": 2,
                            "status": "complete" if success == 2 else "active",
                        }
                    ],
                )
            return result

    server = SuccessService(tmp_path / "server")
    m = manifest(tmp_path, "short_instructions")
    service = {
        "url": "http://fake",
        "shared_root": str(server.root),
        "dataset": "train_pilot",
        "camera": "camera",
        "per_scene_pending": 1,
        "poll_interval_s": 0.001,
    }

    def model(public, profile, folder, timeout):
        return {
            "instruction": "Reach the floor.",
            "target_view": "front",
            "bbox": [400, 600, 600, 800],
            "needs_review": folder.name == "case-0",
        }

    result = run_remote(
        m,
        tmp_path / "run",
        "short_instructions",
        service,
        transport=server,
        model_call=model,
    )
    assert result["aborted"] is None and result["success_target_met"] is True
    assert result["counts"] == {
        "excluded_before_movement": 1,
        "completed_audit": 2,
        "not_needed": 1,
    }
    assert len(server.rows) == 3 and len(server.executions) == 2
    assert not (tmp_path / "run/case-3").exists()


def test_abort_only_seals_claimed_candidates_and_leaves_reserve_without_directories(tmp_path):
    class RejectService(FakeService):
        def call(self, method, path, value=None):
            if method == "POST" and path == "/v1/collections":
                raise ConnectionError("lost submission reply; retain lease, no replay")
            return super().call(method, path, value)
    server = RejectService(tmp_path / "server")
    m = manifest(tmp_path, "short_instructions", count=1000)
    summary = run_remote(m, tmp_path / "run", "short_instructions", {
        "url": "http://fake", "shared_root": str(server.root),
        "dataset": "train_pilot", "camera": "camera"}, transport=server)
    assert summary["aborted"]
    assert summary["counts"] == {"failed_or_uncertain": 1, "not_started": 999}
    assert len(list((tmp_path / "run").glob("case-*"))) == 1
    assert not (tmp_path / "run/case-999").exists()


def test_degraded_service_keeps_healthy_scene_admission_and_preserves_unclaimed_failed_scene():
    health = {"service_owner": "owned", "status": "ok", "scene_schedule": [
        {"key": ["train", name, "camera"], "attempts": 1, "claimed": 0, "status": "active"}
        for name in ["failed", "healthy"]]}
    candidates = [{"id": name} for name in ["failed", "healthy"]]
    admission = SceneAdmission(candidates, lambda c: ("train", c["id"], "camera"), health)
    health.update(status="degraded", live_workers=1)
    health["scene_schedule"][0]["status"] = "unavailable"
    assert admission.take(health, {})["id"] == "healthy"
    assert admission.remaining == 0
    assert admission.unavailable_keys == {("train", "failed", "camera")}


def test_registered_gs_development_collects_without_train_relabeling(tmp_path):
    class GSService(FakeService):
        def call(self, method, path, value=None):
            result = super().call(method, path, value)
            if path == '/v1/health':
                result['training_datasets'] = ['scans']
                result['physics'] = {'scans': {'allow_sliding': True}}
                result['geometry_profiles'] = {'short_instructions': {'split': 'train'}}
            return result

    server = GSService(tmp_path / 'server')
    m = manifest(tmp_path, 'short_instructions', count=1)
    m['config']['split'] = 'development'
    simulation = m['runtime']['settings']['simulation']
    simulation['backend'] = 'habitat_gs'
    simulation['datasets'] = {'scans': {'task': 'development',
        'scenes': {'room.glb': '/frozen/scene.json'}, 'physics': {'allow_sliding': True}}}
    m['candidates'][0]['case']['episode']['_dataset'] = 'scans'
    service = {'url': 'http://fake', 'shared_root': str(server.root),
        'dataset': 'scans', 'camera': 'camera', 'max_pending': 1, 'poll_interval_s': .01}
    m['workflow_config']['backend_manifest']['runtime'] = m['runtime']
    m['workflow_config']['collection_service'] = service
    def model(*args):
        return {'instruction': 'Reach the floor patch.', 'target_view': 'front',
                'bbox': [400, 600, 600, 800], 'needs_review': False}
    result = run_remote(m, tmp_path / 'run', 'short_instructions', service,
                        transport=server, model_call=model)
    assert result['counts'] == {'completed_audit': 1}
    target = read(tmp_path / 'run/case-0/pointnav-target.json')
    assert target['source_split'] == 'development'
    assert target['training_admission'] == 'pending_review'
    assert next(iter(server.rows.values()))['request']['dataset'] == 'scans'


@pytest.mark.parametrize('change', ['backend', 'task', 'scene', 'dataset', 'split'])
def test_gs_admission_rejects_unregistered_or_mislabeled_source(tmp_path, change):
    m = manifest(tmp_path, 'short_instructions', count=1)
    m['config']['split'] = 'development'
    simulation = m['runtime']['settings']['simulation']
    simulation['backend'] = 'habitat_gs'
    simulation['datasets']['train_pilot'].update(task='development',
                                                scenes={'room.glb': '/frozen/scene.json'})
    service = {'dataset': 'train_pilot', 'camera': 'camera'}
    if change == 'backend':
        simulation['backend'] = 'habitat'
    if change == 'task':
        simulation['datasets']['train_pilot']['task'] = 'evaluation'
    if change == 'scene':
        simulation['datasets']['train_pilot']['scenes'] = {}
    if change == 'dataset':
        service['dataset'] = 'different'
    if change == 'split':
        m['config']['split'] = 'train'
    with pytest.raises(ValueError):
        _task(m, m['candidates'][0], 'short_instructions', service)


@pytest.mark.parametrize('mode', ['tool_call', 'json_schema'])
def test_short_proposer_uses_explicit_response_protocol_and_preserves_raw_reply(tmp_path, monkeypatch, mode):
    public = {'images': {}}
    for name in ['front', 'right', 'back', 'left']:
        path = tmp_path / (name + '.png')
        Image.new('RGB', (4, 2)).save(path)
        public['images'][name] = {'path': str(path), 'sha256': digest(path)}
    proposal = {'instruction': 'Walk to the floor by the door.', 'target_view': 'front',
                'bbox': [400, 600, 600, 800], 'needs_review': False, 'reason': 'Visible floor.'}
    def caller(endpoint, payload, config):
        if mode == 'json_schema':
            assert 'tools' not in payload and 'tool_choice' not in payload
            assert payload['response_format']['json_schema']['strict'] is True
            message = {'role': 'assistant', 'content': json.dumps(proposal)}
        else:
            assert payload['tool_choice']['function']['name'] == 'propose_local_task_region'
            message = {'role': 'assistant', 'content': None, 'tool_calls': [{'function': {
                'name': 'propose_local_task_region', 'arguments': json.dumps(proposal)}}]}
        return {'choices': [{'finish_reason': 'stop', 'message': message}]}
    monkeypatch.setattr(short, 'http_call', caller)
    result = short.propose_task(public, {'model': '27b', 'endpoint': 'http://unused'},
                                tmp_path, 10, response_mode=mode)
    assert all(result[key] == value for key, value in proposal.items())
    raw = read(tmp_path / 'model-response.json')['choices'][0]['message']
    assert ('tool_calls' in raw) == (mode == 'tool_call')
