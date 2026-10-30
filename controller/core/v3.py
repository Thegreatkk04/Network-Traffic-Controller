"""Adaptive V3: V2 plus a feedback loop that watches the gateway's own queues.

Every control step (2 s):
 1. Demand is smoothed with an EWMA so short spikes and pauses do not flip allocations.
 2. For every device, the queue of its real-time class (call/gaming, k=1) and its
    streaming class (k=3) is checked. Queue delay = backlog bits / class rate.
    Delay above target, or new drops, means that class is congested: its demand
    gets a boost (x1.25 per step, up to x2.5). When calm, the boost decays to 1.
 3. If ANY real-time class is congested, downloads back off: the ceiling they may
    borrow up to is cut by 20 %. When calm it grows again by 5 % of the link per
    step (AIMD, the same idea TCP uses).
 4. The boosted demands go through the normal V2 allocator.
"""
from controller.core.flow_policy import DeviceFlows, FlowState, allocate_flows
from controller.core.policy import _profile

ALPHA = 0.4                              # EWMA weight of the newest reading
DELAY_TARGET_MS = {1: 15.0, 3: 60.0}     # k=1 call/gaming, k=3 streaming
BOOST_UP, BOOST_MAX, BOOST_DECAY = 1.25, 2.5, 0.95
MD, AI, MIN_BULK = 0.8, 0.05, 0.3        # multiplicative decrease, additive increase, floor


class AdaptiveV3:
    def __init__(self):
        self.reset()

    def reset(self):
        self.ewma, self.boost, self.prev_drops = {}, {}, {}
        self.bulk_frac, self.info = 1.0, {}

    def step(self, devices, link_kbps, stats, rates):
        """devices: list of DeviceFlows (raw demand)
        stats: {class_minor: (backlog_bytes, total_drops)} from the download-side queues
        rates: {class_minor: guaranteed_kbps currently applied}
        Returns (alloc like allocate_flows, bulk_ceil_kbps)."""
        # 1. smooth demand
        seen, smooth = set(), []
        for d in devices:
            fl = []
            for f in d.flows:
                prev = self.ewma.get(f.flow_id, f.demand_kbps)
                v = ALPHA * f.demand_kbps + (1 - ALPHA) * prev
                self.ewma[f.flow_id] = v
                seen.add(f.flow_id)
                fl.append((f, v))
            smooth.append((d, fl))
        for fid in [k for k in self.ewma if k not in seen]:
            del self.ewma[fid]

        # 2. congestion check per device and class
        ips = {d.ip for d in devices}
        congested_rt, delays = False, {}
        for d in devices:
            octet = int(d.ip.rsplit(".", 1)[1])
            for k, target in DELAY_TARGET_MS.items():
                minor = octet * 16 + k
                backlog, drops = stats.get(minor, (0, 0))
                rate = rates.get(minor, 0)
                delay = backlog * 8 / rate if rate > 0 else 0.0
                new_drops = drops - self.prev_drops.get(minor, drops)
                self.prev_drops[minor] = drops
                hot = rate > 0 and (delay > target or new_drops > 0)
                b = self.boost.get((d.ip, k), 1.0)
                b = min(BOOST_MAX, b * BOOST_UP) if hot else max(1.0, b * BOOST_DECAY)
                self.boost[(d.ip, k)] = b
                delays[(d.ip, k)] = round(delay, 1)
                if hot and k == 1:
                    congested_rt = True
        for key in [k for k in self.boost if k[0] not in ips]:
            del self.boost[key]

        # 3. AIMD on how much downloads may borrow
        if congested_rt:
            self.bulk_frac = max(MIN_BULK, self.bulk_frac * MD)
        else:
            self.bulk_frac = min(1.0, self.bulk_frac + AI)

        # 4. boosted demand -> V2 allocator
        boosted = []
        for d, fl in smooth:
            out = []
            for f, v in fl:
                k = min(_profile(f).priority, 3) + 1
                b = self.boost.get((d.ip, k), 1.0) if k in DELAY_TARGET_MS else 1.0
                out.append(FlowState(f.flow_id, f.activity, f.confidence, v * b))
            boosted.append(DeviceFlows(d.ip, out))
        alloc = allocate_flows(boosted, link_kbps, "v2")
        bulk_ceil = int(link_kbps * self.bulk_frac)

        self.info = {
            "bulk_ceil": bulk_ceil, "bulk_frac": round(self.bulk_frac, 2), "congested": congested_rt,
            "boost": {f"{ip}/{'realtime' if k == 1 else 'streaming'}": round(b, 2)
                      for (ip, k), b in self.boost.items() if b > 1.001},
            "delay_ms": {f"{ip}/{'realtime' if k == 1 else 'streaming'}": v
                         for (ip, k), v in delays.items() if v > 0},
        }
        return alloc, bulk_ceil
