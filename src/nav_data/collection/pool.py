"""Bounded model-endpoint pooling shared by remote collection and planner chunks.

Physical collection runs inside the sim-engine collection service; this module only
bounds concurrent model calls and carries the worker-to-coordinator handshake.
"""
import queue
import re
import threading
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

from nav_data.records import read


def validate_profiles(profiles, *, strict=True):
    if not profiles or len({p['id'] for p in profiles}) != len(profiles):
        raise ValueError('nonempty unique model profiles required')
    if len({p['endpoint'] for p in profiles}) != len(profiles):
        raise ValueError('duplicate endpoints would hide the true concurrency cap')
    for profile in profiles:
        if type(profile['slots']) is not int or profile['slots'] <= 0:
            raise ValueError('positive integer endpoint slots required')
        if strict:
            if set(profile) != {'id', 'endpoint', 'slots', 'model'} or not profile['model']:
                raise ValueError('unsupported model profile fields or empty model')
            url = urlsplit(profile['endpoint'])
            if (url.scheme not in ('http', 'https') or not url.hostname or url.username
                    or url.password or url.query or url.fragment):
                raise ValueError('endpoint must not contain credentials or query parameters')


def validate_concurrency(config):
    workers = config['simulation_workers']
    if not workers or len({w['id'] for w in workers}) != len(workers):
        raise ValueError('nonempty unique simulation workers required')
    for worker in workers:
        if (set(worker) != {'id', 'gpu'} or not re.fullmatch(r'[a-zA-Z0-9_-]+', worker['id'])
                or type(worker['gpu']) is not int or worker['gpu'] < 0):
            raise ValueError('invalid worker identity or GPU')
    validate_profiles(config['model_profiles'])
    for key in ('ready_queue_size', 'candidate_timeout_s', 'request_timeout_s'):
        if type(config[key]) is not int or config[key] <= 0:
            raise ValueError(f'{key} must be a positive integer')
    reserve = config.get('minimum_free_disk_bytes', 0)
    if type(reserve) is not int or reserve < 0:
        raise ValueError('minimum_free_disk_bytes must be a nonnegative integer')


class ModelPool:
    """All endpoint slots consume one bounded queue; fast slots take more work.

    A handler owns its request/response persistence and receives no retry. Threads
    are daemonized so an uncertain network call cannot prevent process shutdown.
    A pool belongs to one coordinator; separate runs need separate resource budgets.
    """
    def __init__(self, profiles, capacity, handler, *, strict=True):
        validate_profiles(profiles, strict=strict)
        if type(capacity) is not int or capacity < 1:
            raise ValueError('positive bounded model queue capacity required')
        self.profiles, self.handler = profiles, handler
        self.queue = queue.Queue(maxsize=capacity)
        self.cancel = threading.Event()
        self.threads = []
        self.lock = threading.Lock()
        self.errors, self.inflight = [], {}
        self.active = Counter()
        self.peak = Counter()
        self.completed = Counter()
        self.submitted = self.peak_pending = 0

    def start(self):
        if self.threads:
            raise RuntimeError('model pool already started')
        for profile in self.profiles:
            for slot in range(profile['slots']):
                thread = threading.Thread(target=self._consume, args=(profile,), daemon=True,
                                          name=f"model-{profile['id']}-{slot}")
                thread.start()
                self.threads.append(thread)
        return self

    def _consume(self, profile):
        while True:
            item = self.queue.get()
            try:
                if item is None:
                    return
                if self.cancel.is_set():
                    continue
                job, enqueued = item
                ident = threading.get_ident()
                with self.lock:
                    self.inflight[ident] = time.monotonic()
                    self.active[profile['id']] += 1
                    self.peak[profile['id']] = max(self.peak[profile['id']], self.active[profile['id']])
                try:
                    self.handler(job, profile, enqueued)
                except BaseException as error:
                    with self.lock:
                        self.errors.append(repr(error))
                    self.cancel.set()
                finally:
                    with self.lock:
                        self.active[profile['id']] -= 1
                        self.inflight.pop(ident)
                        self.completed[profile['id']] += 1
            finally:
                self.queue.task_done()

    def check(self, stalled_after=None):
        with self.lock:
            if self.errors:
                raise RuntimeError('model consumer failed: ' + self.errors[0])
            if stalled_after is not None and any(time.monotonic()-t > stalled_after for t in self.inflight.values()):
                raise TimeoutError('model call did not settle; preserve its started evidence')
        if self.cancel.is_set():
            raise RuntimeError('model pool cancelled')

    def submit(self, job, *, check=None):
        blocked_at = time.monotonic()
        while True:
            self.check()
            if check is not None:
                check()
            try:
                self.queue.put((job, time.perf_counter()), timeout=.05)
                with self.lock:
                    self.submitted += 1
                    self.peak_pending = max(self.peak_pending, self.queue.qsize())
                return time.monotonic()-blocked_at
            except queue.Full:
                pass

    def close(self, *, cancel=False, progress_timeout=30., join_timeout=2.):
        if cancel:
            self.cancel.set()
        pending = self.queue.unfinished_tasks
        deadline = time.monotonic()+progress_timeout
        while self.queue.unfinished_tasks and any(t.is_alive() for t in self.threads) and not self.cancel.is_set():
            if pending != self.queue.unfinished_tasks:
                pending = self.queue.unfinished_tasks
                deadline = time.monotonic()+progress_timeout
            if time.monotonic() >= deadline:
                self.cancel.set()
                break
            time.sleep(.01)
        if self.cancel.is_set():
            while True:
                try:
                    self.queue.get_nowait()
                    self.queue.task_done()
                except queue.Empty:
                    break
        deadline = time.monotonic()+join_timeout
        for _ in range(sum(t.is_alive() for t in self.threads)):
            while time.monotonic() < deadline:
                try:
                    self.queue.put(None, timeout=.05)
                    break
                except queue.Full:
                    pass
        for thread in self.threads:
            thread.join(timeout=max(0, deadline-time.monotonic()))

    def stats(self):
        with self.lock:
            return {'total_inflight_limit': sum(p['slots'] for p in self.profiles),
                    'queue_capacity': self.queue.maxsize, 'peak_pending': self.peak_pending,
                    'submitted': self.submitted, 'peak_inflight_per_profile': dict(self.peak),
                    'finished_per_profile': dict(self.completed), 'errors': list(self.errors),
                    'threads_still_running': sum(t.is_alive() for t in self.threads)}


class ModelClient:
    """Worker-to-coordinator request: only candidate identity crosses the queue."""
    def __init__(self, worker_id, events, replies, stop, heartbeat_interval=5.):
        self.worker_id, self.events, self.replies, self.stop = worker_id, events, replies, stop
        self.heartbeat_interval = heartbeat_interval

    def submit(self, candidate_id):
        self.events.put(('observation_ready', self.worker_id, time.monotonic(), candidate_id))

    def wait(self, candidate_id, folder):
        heartbeat_at = time.monotonic()
        while not self.stop.is_set():
            try:
                received = self.replies.get(timeout=.2)
                if received != candidate_id:
                    raise ValueError('model reply belongs to a different candidate')
                return read(Path(folder)/'model-result.json')
            except queue.Empty:
                # Waiting behind bounded model work is not a hung simulator.
                if time.monotonic()-heartbeat_at >= self.heartbeat_interval:
                    heartbeat_at = time.monotonic()
                    self.events.put(('model_wait', self.worker_id, heartbeat_at, candidate_id))
        raise InterruptedError('collection stopped while awaiting model; no movement attempted')
