"""Resume directory compaction after retirement but before derived index creation."""

import importlib.util
import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

from test_compact_release import example

from nav_data.release import compact as compact


def test_runner_resume_uses_manifest_when_completed_client_directories_are_gone(
    tmp_path,
):
    historical = [example(tmp_path / "history", f"s00-p{i:05d}") for i in [1, 2]]
    current = [example(tmp_path / "latest", f"s00-p{i:05d}") for i in [1, 2]]
    frozen = []
    for client, _, raw, _ in historical:
        frozen.append(
            {
                "id": client.name,
                "client_folder": str(client),
                "raw_folder": str(raw),
                "result_sha256": compact.sha((client / "result.json").read_bytes()),
                "receipt_sha256": compact.sha((raw / "receipt.json").read_bytes()),
                "pointnav_target_sha256": compact.sha(
                    (client / "pointnav-target.json").read_bytes()
                ),
                "pointnav_outcome_sha256": compact.sha(
                    (client / "pointnav-outcome.json").read_bytes()
                ),
            }
        )
    compact.convert_batch([(historical[0][0], historical[0][1])])
    body, key, _ = compact.request_template(
        compact.read(current[0][0] / "wire-request.json"),
        compact.read(current[0][0] / "public-observation.json"),
    )
    with closing(compact.catalog(current[0][0].parent)) as db, db:
        db.execute("INSERT INTO request_templates VALUES(?,?)", (key, json.dumps(body)))
    process = {"pid": 0, "start_ticks": "0"}
    for root in [historical[0][0].parent, current[0][0].parent]:
        (root / "client-process.json").write_text(json.dumps(process))
    latest = tmp_path / "latest"
    (latest / "reconciliation.json").write_text(json.dumps({"records": frozen}))
    previous = tmp_path / "previous"
    previous.mkdir()
    (previous / "process.json").write_text(json.dumps(process))
    (previous / "plan.json").write_text(json.dumps({"protected": {}}))
    operation = tmp_path / "operation"
    operation.mkdir()
    (operation / "identity.json").write_text(
        json.dumps({"owner": "test", "previous_operation": str(previous)})
    )
    source = Path(__file__).resolve().parent.parent / "ops/compaction_runner.py"
    script = operation / "run_fixture.py"
    shutil.copyfile(source, script)
    spec = importlib.util.spec_from_file_location("directory_runner_fixture", script)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    runner.LATEST = latest
    runner.CURRENT_SHARED = str(current[0][1])
    runner.TARGET = 4
    runner.capacity = lambda: {"free_inodes": 100000, "available_bytes": 10**12}
    convert_stream = runner.convert_stream

    def interrupt_after_results(*args, **kwargs):
        for result in convert_stream(*args, **kwargs):
            yield result
            runner.STOP_REQUESTED = True

    runner.convert_stream = interrupt_after_results
    with ThreadPoolExecutor(max_workers=2) as pool:
        runner.run(pool)
        assert (operation / "stopped.json").exists()
        assert not (operation / "result.json").exists()
        assert all(not c[0].exists() for c in historical + current)
        for root, expected in [(historical[0][0].parent, 1), (current[0][0].parent, 0)]:
            with closing(compact.sqlite3.connect(root / "episodes.sqlite3")) as db:
                assert (
                    db.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
                    == expected
                )
        runner.STOP_REQUESTED = False
        runner.convert_stream = convert_stream
        runner.run(pool)
    result = compact.read(operation / "result.json")
    assert result["passed"] and result["total_converted"] == 4
    for client, _, _, _ in historical + current:
        assert compact.open_episode(client.parent, client.name).model_request()
