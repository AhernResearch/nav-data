import json
import threading
import time
from pathlib import Path

from nav_data.planner_chunks.pool import run_queue

ROOT = Path(__file__).resolve().parents[1] / 'annotations/planner_chunks'


def test_shared_queue_caps_and_keeps_failed_attempts(tmp_path):
    cases = json.loads((ROOT / 'fixtures/cases.json').read_text())
    profiles = [{'id': 'fast', 'endpoint': 'fast', 'slots': 3, 'config': {}},
                {'id': 'slow', 'endpoint': 'slow', 'slots': 1, 'config': {}}]
    active = {'fast': 0, 'slow': 0}
    peaks = dict(active)
    calls = []
    lock = threading.Lock()

    def worker(case, config, protocol, media, folder, endpoint):
        folder.mkdir()
        with lock:
            active[endpoint] += 1
            peaks[endpoint] = max(peaks[endpoint], active[endpoint])
            calls.append(folder.name)
        time.sleep(.003 if endpoint == 'fast' else .03)
        with lock:
            active[endpoint] -= 1
        return {'case_id': case['id'], 'status': 'failed_or_uncertain' if case['id'] == '1'
                else 'completed_audit', 'wall_s': .01}

    result = run_queue(cases, profiles, {}, ROOT / 'fixtures/media', tmp_path, repeats=8, worker=worker)
    assert len(calls) == len(set(calls)) == 32
    assert peaks['fast'] <= 3 and peaks['slow'] <= 1
    assert result['failed_or_uncertain'] == 8  # no failed job is automatically retried
    assert result['per_server']['fast']['jobs'] > result['per_server']['slow']['jobs']
    assert result['total_inflight_limit'] == 4
    assert result['training_exports'] == result['movement_steps'] == 0
