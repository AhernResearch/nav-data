"""Fair client admission for a server-owned rolling scene schedule; no simulation."""

from collections import Counter, deque


class SceneAdmission:
    def __init__(self, candidates, key, health, per_scene_pending=8):
        self.owner = health["service_owner"]
        self.key = key
        self.limit = int(per_scene_pending)
        if self.limit < 1:
            raise ValueError("positive per-scene pending limit required")
        self.groups = {}
        for candidate in candidates:
            self.groups.setdefault(key(candidate), deque()).append(candidate)
        schedule = {tuple(s["key"]): s for s in health["scene_schedule"]}
        if set(self.groups) != set(schedule) or any(
            len(self.groups[k]) != s["attempts"] or s["claimed"] != 0
            for k, s in schedule.items()
        ):
            raise ValueError(
                "client cases must match the pristine frozen scene schedule budgets"
            )
        self.unavailable_keys = set()
        self.order = deque(self.groups)
        self.remaining = len(candidates)

    def take(self, health, pending):
        usable = health["status"] == "ok" or (
            health["status"] == "degraded" and health.get("live_workers", 0) > 0)
        if health["service_owner"] != self.owner or not usable:
            raise RuntimeError(
                "rolling collection service changed or degraded; no replay"
            )
        active = {
            tuple(s["key"]) for s in health["scene_schedule"] if s["status"] == "active"
        }
        schedule = {tuple(s["key"]): s for s in health["scene_schedule"]}
        for k, s in schedule.items():
            if s["status"] == "unavailable":
                self.unavailable_keys.add(k)
            if s["status"] in {"complete", "unavailable"} and self.groups[k]:
                self.remaining -= len(self.groups[k])
                self.groups[k].clear()
        counts = Counter(self.key(row["candidate"]) for row in pending.values())
        for _ in range(len(self.order)):
            k = self.order[0]
            self.order.rotate(-1)
            s = schedule[k]
            goal = s.get("success_goal")
            goal_covered = goal is not None and s["successful"] + counts[k] >= goal
            if (
                k in active
                and self.groups[k]
                and counts[k] < self.limit
                and not goal_covered
            ):
                self.remaining -= 1
                return self.groups[k].popleft()
        return None
