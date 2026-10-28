"""Bandwidth policies. Pure Python: no network access, easy to test.

Input : list of DeviceState (what each device is doing right now)
Output: dict  ip -> Allocation (guaranteed rate, max rate, priority)
"""
import time
from dataclasses import dataclass

BULK_NEED = 10**9        # "wants everything"
MIN_CONFIDENCE = 0.60    # below this we don't trust the classifier
IDLE_KBPS = 50           # below this a device counts as idle
HEADROOM = 1.3           # v2: let a device grow 30% per decision


@dataclass(frozen=True)
class Profile:
    need_kbps: int       # rate this activity is happy with
    priority: int        # 0 = most important


# Starting guesses. We tune these during the experiments.
PROFILES = {
    "call":      Profile(3000, 0),
    "gaming":    Profile(1000, 0),
    "browsing":  Profile(4000, 1),
    "streaming": Profile(5000, 2),
    "unknown":   Profile(1500, 2),      # used when the classifier is unsure
    "download":  Profile(BULK_NEED, 3),
}


@dataclass
class DeviceState:
    ip: str
    activity: str = "unknown"
    confidence: float = 1.0
    demand_kbps: float = 0.0      # measured throughput in the last window


@dataclass(frozen=True)
class Allocation:
    rate_kbps: int     # guaranteed share
    ceil_kbps: int     # maximum it may borrow
    priority: int


def _profile(d):
    """Pick the profile. An unsure classifier means gentle 'unknown' treatment."""
    if d.confidence < MIN_CONFIDENCE or d.activity not in PROFILES:
        return PROFILES["unknown"]
    return PROFILES[d.activity]


def max_min_share(wants, capacity):
    """Fair split: small wants are fully met, the rest is shared equally.
    Example: wants {a:2, b:10, c:10}, capacity 12 -> a=2, b=5, c=5."""
    alloc = {k: 0.0 for k in wants}
    remaining = dict(wants)
    cap = float(capacity)
    while remaining and cap > 1e-9:
        share = cap / len(remaining)
        satisfied = [k for k, w in remaining.items() if w <= share]
        if not satisfied:
            for k in remaining:
                alloc[k] += share
            return alloc
        for k in satisfied:
            alloc[k] += remaining[k]
            cap -= remaining[k]
            del remaining[k]
    return alloc


def _allocate(devices, link_kbps, floor_kbps, wants):
    """Shared engine: floor, then priority groups, then leftover."""
    n = len(devices)
    floor = min(floor_kbps, link_kbps // n)
    alloc = {d.ip: float(floor) for d in devices}
    remaining = float(link_kbps - floor * n)

    groups = {}
    for d in devices:
        groups.setdefault(_profile(d).priority, []).append(d)

    for prio in sorted(groups):
        residual = {d.ip: max(wants[d.ip] - floor, 0) for d in groups[prio]}
        got = max_min_share(residual, remaining)
        for ip, g in got.items():
            alloc[ip] += g
            remaining -= g
        if remaining <= 0:
            break

    if remaining > 0:                      # nobody wanted the rest
        for ip in alloc:
            alloc[ip] += remaining / n

    return {
        d.ip: Allocation(int(alloc[d.ip]), link_kbps, _profile(d).priority)
        for d in devices
    }


def policy_v1(devices, link_kbps, floor_kbps=500):
    """Fixed rules: every device wants what its activity type normally needs."""
    if not devices:
        return {}
    wants = {d.ip: _profile(d).need_kbps for d in devices}
    return _allocate(devices, link_kbps, floor_kbps, wants)


def policy_v2(devices, link_kbps, floor_kbps=500):
    """Demand-based: devices want what they actually use (plus headroom)."""
    if not devices:
        return {}
    wants = {}
    for d in devices:
        need = _profile(d).need_kbps
        if d.demand_kbps <= IDLE_KBPS:
            wants[d.ip] = floor_kbps                       # idle: floor only
        elif need >= BULK_NEED:
            wants[d.ip] = link_kbps                        # busy bulk: wants all
        else:
            wants[d.ip] = min(need, d.demand_kbps * HEADROOM)
    return _allocate(devices, link_kbps, floor_kbps, wants)


class Smoother:
    """Stops the connection feeling jumpy: ignore small changes and
    don't change the same device again too soon."""

    def __init__(self, threshold=0.12, hold_seconds=6.0):
        self.threshold = threshold
        self.hold = hold_seconds
        self.applied = {}          # ip -> (Allocation, time applied)

    def apply(self, new, now=None):
        now = time.monotonic() if now is None else now
        result = {}
        for ip, alloc in new.items():
            old = self.applied.get(ip)
            if old is not None:
                old_alloc, t = old
                change = abs(alloc.rate_kbps - old_alloc.rate_kbps) / max(old_alloc.rate_kbps, 1)
                if change < self.threshold or now - t < self.hold:
                    result[ip] = old_alloc
                    continue
            self.applied[ip] = (alloc, now)
            result[ip] = alloc
        for ip in list(self.applied):          # forget devices that left
            if ip not in new:
                del self.applied[ip]
        return result


class ActivityDebouncer:
    """A device's activity only changes after k identical readings in a row."""

    def __init__(self, k=3):
        self.k = k
        self.current = {}      # ip -> accepted activity
        self.pending = {}      # ip -> (candidate, count)

    def update(self, ip, reading):
        if ip not in self.current:
            self.current[ip] = reading
            return reading
        if reading == self.current[ip]:
            self.pending.pop(ip, None)
            return reading
        cand, count = self.pending.get(ip, (reading, 0))
        count = count + 1 if cand == reading else 1
        if count >= self.k:
            self.current[ip] = reading
            self.pending.pop(ip, None)
        else:
            self.pending[ip] = (reading, count)
        return self.current[ip]


if __name__ == "__main__":
    devs = [
        DeviceState("10.0.0.2", "call", 0.92, 1800),
        DeviceState("10.0.0.3", "download", 0.97, 6000),
        DeviceState("10.0.0.4", "download", 0.95, 6000),
        DeviceState("10.0.0.5", "streaming", 0.88, 3500),
    ]
    for name, fn in (("v1", policy_v1), ("v2", policy_v2)):
        print(f"\n{name} on a 10000 kbps link")
        for ip, a in fn(devs, 10000).items():
            act = next(d.activity for d in devs if d.ip == ip)
            print(f"  {ip}  {act:10} rate={a.rate_kbps:5} kbps  priority={a.priority}")