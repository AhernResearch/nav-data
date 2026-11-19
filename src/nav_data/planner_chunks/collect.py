"""Run a frozen R2R chunk manifest against the sim-engine collection service."""

from pathlib import Path

from nav_data.collection.client import run_remote
from nav_data.planner_chunks.prepare import verify_manifest


def collect_r2r(manifest_dir):
    """One new attempt per manifest directory; output goes to ``<manifest_dir>/attempt``."""
    manifest_dir = Path(manifest_dir).resolve()
    manifest = verify_manifest(manifest_dir)
    service = manifest['config'].get('collection_service')
    if not service:
        raise ValueError('R2R collection requires an explicit sim-engine collection_service')
    tasks = {task['id']: task for task in manifest['tasks']}
    manifest = dict(manifest, candidates=[dict(c, task=tasks[c['task_id']])
                                          for c in manifest['candidates']])
    return run_remote(manifest, manifest_dir / 'attempt', 'r2r_chunks', service)
