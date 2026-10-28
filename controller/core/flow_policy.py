"""Per-flow bandwidth policy (v1 fixed rules / v2 demand-based).

Why per flow? One device can run a call AND a download at the same time.
Each flow is classified separately, so the call can be protected from the
download, even when both are on the same device.

Rules:
  1. Priority comes first: call/gaming flows are served before browsing,
     streaming, and downloads, across ALL devices.
  2. Inside one priority class, bandwidth is split fairly PER DEVICE
     (a device opening many connections gains nothing), then per flow.
  3. Every device keeps a small floor, so nobody is cut to zero.
  4. Tiny flows (ACKs, background chatter) are ignored.
  5. v2 only: wants follow measured demand, but call/gaming flows keep a
     reserve so a short quiet moment cannot squeeze them to the floor.
"""
from dataclasses import dataclass, field

from controller.core.policy import (
    BULK_NEED, HEADROOM, PROFILES, Allocation, _profile, max_min_share,
)

MIN_FLOW_KBPS = 20                       # flows below this are ignored
RESERVE_KBPS = {"call": 1200, "gaming": 300}   # v2 minimum while the flow exists
IDLE_FLOW = "_idle"                      # used for devices with no active flow


@dataclass
class FlowState:
    flow_id: str
    activity: str = "unknown"
    confidence: float = 1.0
    demand_kbps: float = 0.0


@dataclass
class DeviceFlows:
    ip: str
    flows: list = field(default_factory=list)      # list of FlowState


def _want(f, link_kbps, mode):
    prof = _profile(f)
    if mode == "v1":
        return prof.need_kbps
    if prof.need_kbps >= BULK_NEED:                 # busy bulk flow wants it all
        return link_kbps
    want = min(prof.need_kbps, f.demand_kbps * HEADROOM)
    if prof is PROFILES.get(f.activity):            # real label, not 'unknown'
        want = max(want, min(RESERVE_KBPS.get(f.activity, 0), prof.need_kbps))
    return want


def allocate_flows(devices, link_kbps, mode="v2", floor_kbps=500,
                   min_flow_kbps=MIN_FLOW_KBPS):
    """Return {ip: {flow_id: Allocation}}.  Devices with no active flow get
    one entry named '_idle'. Ignored tiny flows share the device's default class."""
    if mode not in ("v1", "v2"):
        raise ValueError("mode must be 'v1' or 'v2'")
    if not devices:
        return {}

    n = len(devices)
    floor = min(floor_kbps, link_kbps // n)
    remaining = float(link_kbps - floor * n)

    active = {d.ip: sorted((f for f in d.flows if f.demand_kbps >= min_flow_kbps),
                           key=lambda f: (_profile(f).priority, f.flow_id))
              for d in devices}
    rate = {ip: {f.flow_id: 0.0 for f in fl} or {IDLE_FLOW: 0.0}
            for ip, fl in active.items()}
    prio_of = {ip: {f.flow_id: _profile(f).priority for f in fl} or {IDLE_FLOW: 3}
               for ip, fl in active.items()}

    # the floor is given to each device's most important flow
    floor_flow = {ip: next(iter(rate[ip])) for ip in rate}
    for ip in rate:
        rate[ip][floor_flow[ip]] += floor

    def residual_want(ip, f):
        w = _want(f, link_kbps, mode)
        return max(w - (floor if f.flow_id == floor_flow[ip] else 0), 0)

    groups = {}
    for ip, fl in active.items():
        for f in fl:
            groups.setdefault(_profile(f).priority, {}).setdefault(ip, []).append(f)

    for prio in sorted(groups):
        by_dev = groups[prio]
        dev_want = {ip: sum(residual_want(ip, f) for f in fl) for ip, fl in by_dev.items()}
        got_dev = max_min_share(dev_want, remaining)          # fair PER DEVICE
        for ip, fl in by_dev.items():
            got = max_min_share({f.flow_id: residual_want(ip, f) for f in fl}, got_dev[ip])
            for fid, g in got.items():
                rate[ip][fid] += g
                remaining -= g
        if remaining <= 1e-9:
            break

    if remaining > 1e-9:                    # nobody wanted the rest: share equally
        for ip in rate:
            last = list(rate[ip])[-1]
            rate[ip][last] += remaining / n

    return {ip: {fid: Allocation(int(r), link_kbps, prio_of[ip][fid])
                 for fid, r in flows.items()}
            for ip, flows in rate.items()}


def device_totals(result):
    """{ip: total guaranteed kbps}, handy for the per-device level."""
    return {ip: sum(a.rate_kbps for a in flows.values()) for ip, flows in result.items()}


if __name__ == "__main__":
    LINK = 10000
    devs = [
        DeviceFlows("A", [FlowState("a-call", "call", 0.95, 1800),
                          FlowState("a-dl", "download", 0.97, 8000)]),
        DeviceFlows("B", [FlowState("b-dl", "download", 0.97, 8000)]),
    ]
    for mode in ("v1", "v2"):
        out = allocate_flows(devs, LINK, mode)
        print(f"\n{mode} on {LINK} kbps")
        for ip, flows in out.items():
            for fid, a in flows.items():
                print(f"  device {ip}  flow {fid:7} rate={a.rate_kbps:5} kbps  priority={a.priority}")
        print("  device totals:", device_totals(out))