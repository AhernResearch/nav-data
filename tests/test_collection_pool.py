"""Real spawned processes with fake models/backends; no GPU, HTTP or movement."""

import threading

import pytest

from nav_data.collection.pool import ModelPool, validate_concurrency


def test_one_model_pool_bounds_each_endpoint_and_backpressures_the_producer():
    profiles = [
        {"id": "a", "endpoint": "http://a/v1", "model": "fake", "slots": 2},
        {"id": "b", "endpoint": "http://b/v1", "model": "fake", "slots": 1},
    ]
    release, filled = threading.Event(), threading.Event()
    lock = threading.Lock()
    seen = []

    def consume(job, profile, enqueued):
        with lock:
            seen.append((job, profile["id"]))
            if len(seen) == 3:
                filled.set()
        assert release.wait(3)

    pool = ModelPool(profiles, 1, consume).start()
    for job in range(3):
        pool.submit(job)
    assert filled.wait(3)
    pool.submit(3)  # All three slots busy; the sole pending slot is now full.
    submitted = threading.Event()
    producer = threading.Thread(target=lambda: (pool.submit(4), submitted.set()))
    producer.start()
    assert not submitted.wait(0.05)
    release.set()
    producer.join(3)
    pool.close()
    assert submitted.is_set() and sorted(job for job, _ in seen) == list(range(5))
    assert pool.stats()["peak_inflight_per_profile"] == {"a": 2, "b": 1}
    assert pool.stats()["peak_pending"] <= 1
    assert pool.stats()["threads_still_running"] == 0


def test_model_cancellation_preserves_inflight_call_and_never_starts_queued_work():
    entered, release = threading.Event(), threading.Event()
    calls = []

    def consume(job, profile, enqueued):
        calls.append(job)
        entered.set()
        release.wait(3)

    pool = ModelPool(
        [{"id": "p", "endpoint": "http://p/v1", "model": "fake", "slots": 1}], 2, consume
    ).start()
    pool.submit("started")
    assert entered.wait(3)
    pool.submit("pending")
    pool.close(cancel=True, join_timeout=0.03)
    assert calls == ["started"] and pool.stats()["threads_still_running"] == 1
    release.set()
    for thread in pool.threads:
        thread.join(3)
    assert pool.stats()["threads_still_running"] == 0
    assert calls == ["started"]


def test_failed_consumer_aborts_pending_model_work_without_retry():
    calls = []

    def fail(job, profile, enqueued):
        calls.append(job)
        raise OSError("response persistence failed")

    pool = ModelPool([{"id": "p", "endpoint": "http://p/v1", "model": "fake", "slots": 1}], 2, fail).start()
    pool.submit("first")
    pool.close()
    with pytest.raises(RuntimeError, match="persistence failed"):
        pool.check()
    assert calls == ["first"]


def concurrency_config(tmp_path):
    return {
        "simulation_workers": [{"id": "w0", "gpu": 0}, {"id": "w1", "gpu": 1}],
        "model_profiles": [{"id": "m0", "endpoint": "http://localhost:18110/v1", "model": "fake",
                            "slots": 2}],
        "ready_queue_size": 1,
        "candidate_timeout_s": 30,
        "request_timeout_s": 10,
    }


@pytest.mark.parametrize(
    "change", ["duplicate_endpoint", "credentials", "boolean_slots", "zero_queue", "bad_gpu"]
)
def test_invalid_concurrency_is_rejected(change, tmp_path):
    config = concurrency_config(tmp_path)
    if change == "duplicate_endpoint":
        config["model_profiles"].append(dict(config["model_profiles"][0], id="other"))
    elif change == "credentials":
        config["model_profiles"][0]["endpoint"] = "http://key@localhost/v1"
    elif change == "boolean_slots":
        config["model_profiles"][0]["slots"] = True
    elif change == "zero_queue":
        config["ready_queue_size"] = 0
    else:
        config["simulation_workers"][0]["gpu"] = -1
    with pytest.raises(ValueError):
        validate_concurrency(config)
