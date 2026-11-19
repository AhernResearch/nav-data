"""Distribution extraction uses actual states and tolerates atomic compaction handoff."""

import json
import math
from pathlib import Path

import pytest
from test_compact_release import example

from nav_data.collection import distribution as dist
from nav_data.release import compact as compact
from nav_data.tasks.pointnav import frame_goal


def entry_for(case):
    client, _, raw, _ = case
    return {
        "client": str(client),
        "metadata": str(raw / "meta.json"),
        "metadata_sha256": None,
        "expected": {},
    }


def test_legacy_and_compact_statistics_are_identical(tmp_path):
    case = example(tmp_path)
    entry = entry_for(case)
    legacy = dist.extract(entry)
    compact.convert_batch([(case[0], case[1])])
    canonical = dist.extract(entry)
    assert legacy["source"]["format"] == "legacy"
    assert canonical["source"]["format"] == "compact"
    assert {k: v for k, v in legacy.items() if k != "source"} == {
        k: v for k, v in canonical.items() if k != "source"
    }
    assert canonical["duration_s"] == (canonical["frames"] - 1) * 0.25
    assert canonical["path_m"] >= canonical["endpoint_displacement_m"] - 1e-6


def test_loading_retries_published_metadata_when_sidecars_are_retired(
    tmp_path, monkeypatch
):
    case = example(tmp_path)
    entry = entry_for(case)
    original = Path.read_bytes
    switched = False

    def read_bytes(path):
        nonlocal switched
        if path == case[0] / "result.json" and not switched:
            switched = True
            compact.convert_batch([(case[0], case[1])])
            raise FileNotFoundError("sidecar retired after initial metadata check")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    assert dist.extract(entry)["source"]["format"] == "compact"
    assert switched


@pytest.mark.parametrize("angle", [0, math.pi / 2, -math.pi / 2, math.pi])
def test_target_bearing_matches_recorded_agent_coordinate_convention(tmp_path, angle):
    case = example(tmp_path)
    data, _ = dist.load_episode(entry_for(case))
    state = data["world_states"][0]
    state["world_rotation_xyzw"] = [0, math.sin(angle / 2), 0, math.cos(angle / 2)]
    target = data["records"]["client"]["pointnav-target.json"]["episode"]["goals"][0][
        "position"
    ]
    expected = frame_goal(state, target)
    actual = dist.summarize_episode(data, {})
    assert actual["goal_bearing_deg"] == pytest.approx(
        math.degrees(expected["goal_bearing_left_rad"])
    )
    assert actual["goal_forward_m"] == pytest.approx(
        expected["goal_base_forward_left_up_m"][0]
    )


def test_snapshot_has_unique_membership_and_verifies_manifest_hashes(tmp_path):
    baseline = {
        "rows": {
            "/client": [
                {
                    "id": "s00-p00001",
                    "metadata": "/raw/tasks/a/meta.json",
                    "sha256": "hash",
                }
            ]
        }
    }
    tasks = [
        {
            "entry": ["/client/s00-p00002", "/raw", "/raw/tasks/b/meta.json"],
            "expected": {},
        }
    ]
    b = json.dumps(baseline).encode()
    t = (json.dumps(tasks[0]) + "\n").encode()
    (tmp_path / "catalog-baseline.json").write_bytes(b)
    (tmp_path / "tasks.jsonl").write_bytes(t)
    (tmp_path / "plan.json").write_text(
        json.dumps(
            {
                "baseline_sha256": dist.digest_bytes(b),
                "tasks_sha256": dist.digest_bytes(t),
                "total_target": 2,
            }
        )
    )
    entries, provenance = dist.snapshot_entries(tmp_path)
    assert len(entries) == 2 and provenance["target"] == 2
    (tmp_path / "tasks.jsonl").write_bytes(t + b"\n")
    with pytest.raises(AssertionError):
        dist.snapshot_entries(tmp_path)
