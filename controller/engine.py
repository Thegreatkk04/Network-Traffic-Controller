#!/usr/bin/env python3
"""Bandwidth manager engine: capture -> classify -> allocate -> enforce (every 2 s).
Used by controller/main.py (dashboard). Can also run alone:
  sudo .venv/bin/python -m controller.engine --mode v2 --down 3000 --up 1500 [--dry-run]
"""
import argparse, glob, ipaddress, os, sys, threading, time
from collections import deque
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from nfstream import NFStreamer

from ml.collect.pcap_to_csv import flow_to_row
from controller.core.policy import ActivityDebouncer
from controller.core.flow_policy import (DeviceFlows, FlowState, allocate_flows, equal_shares,
                                         MIN_FLOW_KBPS)
from controller.core.classify import Classifier
from controller.enforcer import Enforcer
from controller.core.v3 import AdaptiveV3

MODES = ("default", "equal", "v1", "v2", "v3")
TIERS = {"high": 0, "normal": 1, "low": 2}


def parse_lease(line):
    """dnsmasq lease line -> (ip, mac, hostname or "")."""
    p = line.split()
    if len(p) < 4:
        return None
    return p[2], p[1].lower(), "" if p[3] == "*" else p[3]


def rules_by_ip(rules, macs):
    """Admin rules are saved per MAC; the engine needs them per current IP.
    Returns (tiers, caps, blocked): {ip: 0|1|2}, {ip: (down|None, up|None)}, {ip}."""
    tiers, caps, blocked = {}, {}, set()
    for ip, mac in macs.items():
        r = rules.get(mac) or {}
        if r.get("blocked"):
            blocked.add(ip)
        if r.get("priority") in TIERS and r["priority"] != "normal":
            tiers[ip] = TIERS[r["priority"]]
        cd, cu = int(r.get("cap_down") or 0), int(r.get("cap_up") or 0)
        if cd or cu:
            caps[ip] = (cd or None, cu or None)
    return tiers, caps, blocked
STALE_S = 12          # forget a flow not seen for this long
RATE_WINDOW_S = 10.0  # measured throughput = bytes seen in this window


class Engine:
    def __init__(self, iface, subnet, gw, model, mode, down, up, dry_run=False):
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        self.iface, self.net, self.gw = iface, ipaddress.ip_network(subnet), gw
        self.clf, self.deb = Classifier(model), ActivityDebouncer(k=3)
        self.mode, self.down, self.up = mode, int(down), int(up)
        self.enf = None if dry_run else Enforcer(iface, self.down, self.up)
        self.v3, self.v3_info = AdaptiveV3(), {}
        self.lock = threading.Lock()      # protects flows + samples
        self.ctl = threading.Lock()       # one control step or link change at a time
        self.flows, self.samples = {}, deque()
        self.history = deque(maxlen=900)  # 30 min at 2 s
        self.names, self.macs, self.last_error = {}, {}, None   # from DHCP leases, by IP
        self.rules = {}                     # admin rules by MAC: name, cap_down, cap_up, priority, blocked
        self.link_info = {"status": "off"}  # filled by controller/autorate.py when auto is on
        self.state = {"t": 0, "mode": mode, "down": self.down, "up": self.up, "devices": []}

    # ---------- admin actions ----------
    def set_mode(self, mode):
        if mode not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}")
        self.mode = mode

    def set_link(self, down, up, rebuild=True):
        """rebuild=False changes the rate in place (used by the auto link tuner every few seconds)."""
        with self.ctl:
            self.down, self.up = int(down), int(up)
            if self.enf:
                if rebuild:
                    self.enf.set_link(self.down, self.up)
                else:
                    self.enf.retune(self.down, self.up)

    def public_state(self):
        s = dict(self.state)
        s.update(modes=list(MODES), error=self.last_error, v3=self.v3_info, autorate=self.link_info,
                 shaping=self.enf is not None, iface=self.iface,
                 known_devices=[{"ip": ip, "mac": mac, "hostname": self.names.get(ip, "")}
                                for ip, mac in sorted(self.macs.items())])
        return s

    def set_rules(self, rules):
        """Admin rules from the control file: {mac: {...}}."""
        self.rules = {str(m).lower(): r for m, r in (rules or {}).items() if isinstance(r, dict)}

    # ---------- capture + classify (runs forever in the main thread) ----------
    def capture(self):
        streamer = NFStreamer(source=self.iface, statistical_analysis=True, n_dissections=0,
                              idle_timeout=10, active_timeout=5)   # same as training
        for f in streamer:
            try:
                src, dst = ipaddress.ip_address(f.src_ip), ipaddress.ip_address(f.dst_ip)
            except ValueError:
                continue
            if src in self.net and f.src_ip != self.gw:
                client, remote, rport, cport = f.src_ip, f.dst_ip, f.dst_port, f.src_port
            elif dst in self.net and f.dst_ip != self.gw:
                client, remote, rport, cport = f.dst_ip, f.src_ip, f.src_port, f.dst_port
            else:
                continue
            row = flow_to_row(f, client)
            if row is None:
                continue
            label, conf = self.clf.predict(pd.DataFrame([row]))
            fid = f"{f.protocol}-{remote}:{rport}-{cport}"
            now = time.time()
            with self.lock:
                self.flows[fid] = {"ip": client, "label": self.deb.update(fid, label),
                                   "conf": conf, "kbps": row["bytes_per_s"] * 8 / 1000,
                                   "seen": now, "key": (client, remote, f.protocol, cport, rport)}
                self.samples.append((now, row["duration_s"], client, row["down_bytes"], row["up_bytes"]))

    def snapshot(self):
        now = time.time()
        with self.lock:
            for k in [k for k, v in self.flows.items() if now - v["seen"] > STALE_S]:
                del self.flows[k]
            items = list(self.flows.items())
        devs = {}
        for fid, v in items:
            devs.setdefault(v["ip"], []).append(FlowState(fid, v["label"], v["conf"], v["kbps"]))
        return [DeviceFlows(ip, fl) for ip, fl in sorted(devs.items())]

    def measure(self):
        """Per-device (down_kbps, up_kbps) from the flow pieces of the last window."""
        now, acc = time.time(), {}
        with self.lock:
            # pieces arrive at their END, so look at [now-15s, now-5s] where data is complete
            we = now - 5.0
            ws = we - RATE_WINDOW_S
            while self.samples and self.samples[0][0] < now - 30:
                self.samples.popleft()
            for end, dur, ip, d, u in self.samples:
                dur = max(dur, 0.001)
                overlap = min(end, we) - max(end - dur, ws)
                if overlap <= 0:
                    continue
                frac = min(1.0, overlap / dur)       # only the part inside the window
                r = acc.setdefault(ip, [0, 0])
                r[0] += d * frac
                r[1] += u * frac
        k = 8 / RATE_WINDOW_S / 1000
        return {ip: (d * k, u * k) for ip, (d, u) in acc.items()}

    def read_names(self):
        """Hostnames and MAC addresses from the hotspot's DHCP leases (NetworkManager dnsmasq)."""
        names, macs = {}, {}
        for path in glob.glob("/var/lib/NetworkManager/dnsmasq-*.leases"):
            try:
                with open(path) as fh:
                    for line in fh:
                        lease = parse_lease(line)
                        if lease:
                            ip, macs[ip], host = lease
                            if host:
                                names[ip] = host
            except OSError:
                pass
        if macs:                            # keep the last good read if the file is mid-rewrite
            self.names, self.macs = names, macs

    # ---------- one control step ----------
    def tick(self):
        self.read_names()
        with self.ctl:
            mode, down, up = self.mode, self.down, self.up
            devs = self.snapshot()
            tiers, caps, blocked = rules_by_ip(self.rules, self.macs)
            live = [d for d in devs if d.ip not in blocked]
            down_caps = {ip: c[0] for ip, c in caps.items() if c[0]}
            bulk_ceil = None
            if mode == "v3":
                stats = self.enf.stats() if self.enf else {}
                rates = self.enf.class_rates() if self.enf else {}
                alloc, bulk_ceil = (self.v3.step(live, down, stats, rates, tiers, down_caps)
                                    if live else ({}, None))
                self.v3_info = self.v3.info
            else:
                alloc = (allocate_flows(live, down, mode, tiers=tiers, caps=down_caps)
                         if (mode in ("v1", "v2") and live) else {})
                self.v3.reset()
                self.v3_info = {}
            eq = equal_shares([d.ip for d in live], down, down_caps) if mode == "equal" else {}
            with self.lock:
                keys = {fid: v["key"] for fid, v in self.flows.items()}
            if self.enf:
                self.enf.apply(mode, [d.ip for d in live], alloc, keys, bulk_ceil, caps, blocked)

        rates = self.measure()
        out, hist = [], {}
        for d in devs:
            fa = alloc.get(d.ip, {})
            flows = []
            for f in sorted(d.flows, key=lambda x: -x.demand_kbps):
                if f.demand_kbps < MIN_FLOW_KBPS:
                    continue
                a = fa.get(f.flow_id)
                flows.append({"id": f.flow_id, "activity": f.activity, "conf": round(f.confidence, 2),
                              "using": round(f.demand_kbps), "alloc": a.rate_kbps if a else None,
                              "priority": a.priority if a else None})
            if d.ip in blocked:
                total = 0
            elif mode in ("v1", "v2", "v3"):
                total = sum(a.rate_kbps for a in fa.values())
            elif mode == "equal":
                total = int(eq.get(d.ip, 0))
            else:
                total = None
            by_act = {}
            for f in flows:
                act = f["activity"] if f["conf"] >= 0.6 else "unknown"
                by_act[act] = by_act.get(act, 0) + f["using"]
            top_act = max(by_act, key=by_act.get) if by_act else None
            top = next((f for f in flows if f["activity"] == top_act), flows[0]) if flows else None
            dk, uk = rates.get(d.ip, (0.0, 0.0))
            mac = self.macs.get(d.ip, "")
            rule = self.rules.get(mac) or {}
            dev = {"ip": d.ip, "mac": mac, "name": rule.get("name") or self.names.get(d.ip, ""),
                   "hostname": self.names.get(d.ip, ""),
                   "rule": {k: rule[k] for k in ("cap_down", "cap_up", "priority", "blocked") if rule.get(k)},
                   "activity": "blocked" if d.ip in blocked else (top_act or "idle"),
                   "conf": top["conf"] if top else None,
                   "down_kbps": round(dk), "up_kbps": round(uk),
                   "using": round(sum(f.demand_kbps for f in d.flows)),
                   "alloc": total, "flows": flows}
            out.append(dev)
            hist[d.ip] = {"name": dev["name"], "activity": dev["activity"],
                          "down": dev["down_kbps"], "up": dev["up_kbps"], "alloc": total}
        now = time.time()
        self.state = {"t": now, "mode": mode, "down": down, "up": up, "devices": out}
        self.history.append({"t": now, "mode": mode, "down": down, "devices": hist})

    def control_loop(self, print_table=True):
        while True:
            time.sleep(2)
            try:
                self.tick()
                self.last_error = None
            except Exception as e:
                self.last_error = str(e)
            if print_table:
                print("\033[2J\033[H" + render(self.state, self.last_error, self.enf is None), flush=True)


def render(s, err=None, dry=False):
    lines = [f"mode={s['mode']}  down={s['down']} kbps  up={s['up']} kbps  "
             f"devices={len(s['devices'])}  {'DRY RUN (no shaping)' if dry else 'SHAPING ON'}", ""]
    if err:
        lines += [f"!! error: {err}", ""]
    for d in s["devices"]:
        g = d["alloc"] if d["alloc"] is not None else "shared"
        lines.append(f"{d['ip']} {d['name']}   down {d['down_kbps']} kbps   up {d['up_kbps']} kbps"
                     f"   guaranteed {g}")
        for f in d["flows"]:
            a = f["alloc"] if f["alloc"] is not None else "-"
            lines.append(f"   {f['activity']:9} conf {f['conf']:.2f}  using {f['using']:7}  "
                         f"alloc {a!s:>6}  {f['id']}")
        lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--iface", default="wlp0s20f3")
    ap.add_argument("--subnet", default="10.42.0.0/24")
    ap.add_argument("--gw", default="10.42.0.1")
    ap.add_argument("--model", default="ml/models/model.pkl")
    ap.add_argument("--mode", default="v2", choices=MODES)
    ap.add_argument("--down", type=int, default=8000, help="download link, kbps")
    ap.add_argument("--up", type=int, default=4000, help="upload link, kbps")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    eng = Engine(a.iface, a.subnet, a.gw, a.model, a.mode, a.down, a.up, a.dry_run)
    if eng.enf:
        eng.enf.setup()
    threading.Thread(target=eng.control_loop, daemon=True).start()
    try:
        eng.capture()
    except KeyboardInterrupt:
        pass
    finally:
        if eng.enf:
            eng.enf.teardown()
            print("\nShaping removed, network back to normal.")
