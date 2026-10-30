#!/usr/bin/env python3
"""Live dry run: capture hotspot traffic, classify each flow, show allocation.

  ./run_monitor.sh                              # just watch (predictions logged)
  ./run_monitor.sh --record call --app meet     # also save rows as training data
  ./run_monitor.sh --mode v1 --link 6000
"""
import argparse, csv, ipaddress, os, sys, threading, time
from collections import Counter
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from nfstream import NFStreamer

from ml.collect.pcap_to_csv import flow_to_row
from controller.core.policy import ActivityDebouncer
from controller.core.flow_policy import DeviceFlows, FlowState, allocate_flows, MIN_FLOW_KBPS
from controller.core.classify import Classifier

FLOWS, LOCK = {}, threading.Lock()   # flow_id -> {ip, label, conf, kbps, seen}
STALE_S = 12
VOLUME = Counter()                   # predicted label -> kbps-weighted count (flows >= 50 kbps)


class CsvLog:
    """Append dict rows to a CSV; the header comes from the first row."""
    def __init__(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.path, self.f, self.w = path, None, None

    def write(self, row):
        if self.w is None:
            self.f = open(self.path, "w", newline="")
            self.w = csv.DictWriter(self.f, fieldnames=list(row))
            self.w.writeheader()
        self.w.writerow(row)
        self.f.flush()


def capture(a, clf, deb, plog, rec, session):
    net = ipaddress.ip_network(a.subnet)
    streamer = NFStreamer(source=a.iface, statistical_analysis=True, n_dissections=0,
                          idle_timeout=10, active_timeout=5)   # same as training
    for f in streamer:
        try:
            src, dst = ipaddress.ip_address(f.src_ip), ipaddress.ip_address(f.dst_ip)
        except ValueError:
            continue
        if src in net and f.src_ip != a.gw:
            client, remote, rport, cport = f.src_ip, f.dst_ip, f.dst_port, f.src_port
        elif dst in net and f.dst_ip != a.gw:
            client, remote, rport, cport = f.dst_ip, f.src_ip, f.src_port, f.dst_port
        else:
            continue
        row = flow_to_row(f, client)
        if row is None:
            continue

        label, conf = clf.predict(pd.DataFrame([row]))
        fid = f"{f.protocol}-{remote}:{rport}-{cport}"
        kbps = row["bytes_per_s"] * 8 / 1000
        accepted = deb.update(fid, label)
        with LOCK:
            FLOWS[fid] = {"ip": client, "label": accepted, "conf": conf,
                          "kbps": kbps, "seen": time.time()}
            if kbps >= 50:
                VOLUME[label] += kbps

        plog.write({"t": round(time.time(), 1), "ip": client, "flow": fid,
                    "kbps": round(kbps, 1), "pred": label, "conf": round(conf, 3),
                    "accepted": accepted, "truth": a.record or ""})
        if rec:
            rec.write({"session_id": session, "label": a.record, "app": a.app,
                       **row, "note": f"live, me={client}"})


def snapshot():
    now = time.time()
    with LOCK:
        for k in [k for k, v in FLOWS.items() if now - v["seen"] > STALE_S]:
            del FLOWS[k]
        items = list(FLOWS.items())
    devs = {}
    for fid, v in items:
        devs.setdefault(v["ip"], []).append(FlowState(fid, v["label"], v["conf"], v["kbps"]))
    return [DeviceFlows(ip, fl) for ip, fl in sorted(devs.items())]


def show(a, header):
    while True:
        time.sleep(2)
        devs = snapshot()
        out = allocate_flows(devs, a.link, a.mode) if devs else {}
        lines = [header, f"mode={a.mode}  link={a.link} kbps  devices={len(devs)}", ""]
        for d in devs:
            alloc = out.get(d.ip, {})
            big = sorted((f for f in d.flows if f.demand_kbps >= MIN_FLOW_KBPS),
                         key=lambda x: -x.demand_kbps)
            lines.append(f"{d.ip}   total alloc {sum(x.rate_kbps for x in alloc.values())} kbps"
                         f"   ({len(d.flows) - len(big)} tiny flows hidden)")
            for f in big:
                x = alloc.get(f.flow_id)
                rate = x.rate_kbps if x else "-"
                lines.append(f"   {f.activity:9} conf {f.confidence:.2f}  using {f.demand_kbps:8.0f}"
                             f"  alloc {rate!s:>6}  {f.flow_id}")
            lines.append("")
        print("\033[2J\033[H" + "\n".join(lines), flush=True)


def summary():
    total = sum(VOLUME.values())
    print("\n===== Predictions weighted by traffic (flows >= 50 kbps) =====")
    for lab, v in VOLUME.most_common():
        print(f"  {lab:10} {v / total:6.1%}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--iface", default="wlp0s20f3")
    ap.add_argument("--subnet", default="10.42.0.0/24")
    ap.add_argument("--gw", default="10.42.0.1")
    ap.add_argument("--mode", default="v2", choices=["v1", "v2"])
    ap.add_argument("--link", type=int, default=8000, help="kbps")
    ap.add_argument("--model", default="ml/models/model.pkl")
    ap.add_argument("--record", default=None,
                    choices=["call", "streaming", "download", "gaming", "browsing"],
                    help="save every flow row as training data with this label")
    ap.add_argument("--app", default="unknown", help="app name for the recording")
    a = ap.parse_args()

    stamp = time.strftime("%Y%m%d_%H%M%S")
    plog = CsvLog(f"logs/pred_{stamp}.csv")
    rec, session = None, None
    if a.record:
        session = f"live_{a.record}_{stamp}"
        rec = CsvLog(f"data/raw/{session}.csv")
    header = (f"RECORDING '{a.record}' -> data/raw/{session}.csv" if rec
              else "watching only") + f"   | predictions -> {plog.path}"

    clf = Classifier(a.model)
    threading.Thread(target=show, args=(a, header), daemon=True).start()
    try:
        capture(a, clf, ActivityDebouncer(k=3), plog, rec, session)
    except KeyboardInterrupt:
        pass
    finally:
        summary()
