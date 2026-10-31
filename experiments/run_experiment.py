#!/usr/bin/env python3
"""Mode comparison experiment.

Keep the SAME activities running on the phones for the whole run
(e.g. A: video call, B: YouTube, C: big download), with the engine running (./start_all.sh):

  sudo .venv/bin/python -m experiments.run_experiment --duration 90

Automatic link speed is switched OFF for the run and the link is fixed (--down/--up,
default 3000/1500), so every mode gets the same capacity. The old setting is restored after.
For each mode it switches the policy, waits for it to settle, then every 2 s records:
  - each device's measured throughput (from the engine)
  - each gateway queue's sent bytes, backlog and drops (tc)
    queue delay = backlog / send rate   (Little's law, per queue)
Results: logs/exp_<time>/ samples.csv, queues.csv, summary.csv and PNG charts.
"""
import argparse, csv, json, os, subprocess, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from controller.service import CONTROL, LOGS, STATE, give_to_user, read_json, write_json

K_NAME = {1: "realtime", 2: "browsing", 3: "streaming", 4: "download", 5: "other"}


def tc_queues(dev):
    """{class_minor: (bytes_sent, backlog_bytes, drops)} for every leaf queue under 1:."""
    r = subprocess.run(["tc", "-s", "-j", "qdisc", "show", "dev", dev], capture_output=True, text=True)
    try:
        data = json.loads(r.stdout or "[]")
    except ValueError:
        return {}
    out = {}
    for q in data:
        parent = q.get("parent", "")
        if not parent.startswith("1:"):
            continue
        st = q.get("stats", q)
        out[int(parent[2:], 16)] = (int(st.get("bytes", 0)), int(st.get("backlog", 0)),
                                    int(st.get("drops", 0)))
    return out


def category(minor):
    return "shared" if minor == 2 else K_NAME.get(minor % 16, "other")


def run_mode(mode, a, sw, qw):
    ctl = read_json(CONTROL) or {}
    ctl.update(mode=mode, auto=False, down=a.down, up=a.up, ts=time.time())
    write_json(CONTROL, ctl)
    print(f"\n== {mode}: settling {a.warmup}s", flush=True)
    time.sleep(a.warmup)
    prev, prev_t = tc_queues(a.iface), time.time()
    end = time.time() + a.duration
    while time.time() < end:
        time.sleep(2)
        now = time.time()
        s = read_json(STATE) or {}
        if now - s.get("heartbeat", 0) > 10:
            print("\n  !! engine not responding, is ./start_all.sh running?", flush=True)
            continue
        if s.get("mode") != mode:
            continue
        for d in s.get("devices", []):
            sw.writerow([round(now, 1), mode, d["ip"], d.get("name", ""), d["activity"],
                         d["down_kbps"], d["up_kbps"], "" if d["alloc"] is None else d["alloc"]])
        cur, dt = tc_queues(a.iface), now - prev_t
        for minor, (b, backlog, drops) in cur.items():
            pb, _, pdrop = prev.get(minor, (b, 0, drops))
            rate_bps = max(b - pb, 0) * 8 / dt
            delay = backlog * 8 / rate_bps * 1000 if rate_bps > 8000 else None
            qw.writerow([round(now, 1), mode, minor, category(minor), round(rate_bps / 1000, 1),
                         backlog, max(drops - pdrop, 0), "" if delay is None else round(delay, 1)])
        prev, prev_t = cur, now
        print(f"\r  {mode}: {max(0, int(end - time.time())):3d}s left, "
              f"devices={len(s.get('devices', []))}   ", end="", flush=True)


def jain(xs):
    xs = [x for x in xs if x > 0]
    return (sum(xs) ** 2) / (len(xs) * sum(x * x for x in xs)) if xs else float("nan")


def summarize(out_dir, link_kbps, modes):
    import pandas as pd
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    smp = pd.read_csv(os.path.join(out_dir, "samples.csv"))
    qs = pd.read_csv(os.path.join(out_dir, "queues.csv"))
    smp["device"] = smp.apply(lambda r: f"{r['name']} ({r['ip']})" if isinstance(r["name"], str) and r["name"] else r["ip"], axis=1)

    rows = []
    for mode in modes:
        m = smp[smp["mode"] == mode]
        per_dev = m.groupby("device")["down_kbps"].mean()
        q = qs[(qs["mode"] == mode) & qs["delay_ms"].notna() & (qs["rate_kbps"] > 0)]

        def wdelay(cat):
            c = q[q["category"] == ("shared" if mode == "default" else cat)]
            return round((c["delay_ms"] * c["rate_kbps"]).sum() / c["rate_kbps"].sum(), 1) if len(c) else None

        rows.append({
            "mode": mode,
            "devices": len(per_dev),
            "total_down_kbps": round(per_dev.sum()),
            "utilization_pct": round(per_dev.sum() / link_kbps * 100, 1),
            "jain_fairness": round(jain(per_dev.tolist()), 3),
            "realtime_delay_ms": wdelay("realtime"),
            "streaming_delay_ms": wdelay("streaming"),
            "download_delay_ms": wdelay("download"),
            "drops": int(qs[qs["mode"] == mode]["drops"].sum()),
        })
    summ = pd.DataFrame(rows)
    summ.to_csv(os.path.join(out_dir, "summary.csv"), index=False)

    plt.rcParams.update({"font.size": 10, "axes.spines.top": False, "axes.spines.right": False,
                         "axes.grid": True, "grid.color": "#e6e8eb", "axes.axisbelow": True})
    palette = ["#0b57d0", "#146c2e", "#b3261e", "#7627bb", "#e37400", "#00838f"]

    # 1. throughput per device per mode
    tp = smp.groupby(["mode", "device"])["down_kbps"].mean().unstack().reindex(modes)
    ax = tp.plot(kind="bar", figsize=(9, 4.5), color=palette[:len(tp.columns)], width=0.8, rot=0)
    ax.axhline(link_kbps, ls="--", color="#9aa0a6", lw=1, label="Link capacity")
    ax.set_ylabel("Mean download (kbps)"); ax.set_xlabel("")
    ax.set_title("Download throughput per device")
    ax.legend(frameon=False, fontsize=9)
    plt.tight_layout(); plt.savefig(os.path.join(out_dir, "throughput_per_device.png"), dpi=150); plt.close()

    # 2. queue delay by traffic type
    d = summ.set_index("mode")[["realtime_delay_ms", "streaming_delay_ms", "download_delay_ms"]].astype(float)
    d.columns = ["Call / gaming", "Streaming", "Download"]
    ax = d.plot(kind="bar", figsize=(9, 4.5), color=["#146c2e", "#0b57d0", "#5f6368"], width=0.8, rot=0)
    ax.set_ylabel("Queue delay at gateway (ms)"); ax.set_xlabel("")
    ax.set_title("Queueing delay by traffic type (lower is better)")
    ax.legend(frameon=False)
    plt.tight_layout(); plt.savefig(os.path.join(out_dir, "queue_delay.png"), dpi=150); plt.close()

    # 3. fairness and utilization
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(10, 4))
    a1.bar(summ["mode"], summ["jain_fairness"], color="#0b57d0")
    a1.set_ylim(0, 1.05); a1.set_title("Jain's fairness index (1 = perfectly equal)")
    a2.bar(summ["mode"], summ["utilization_pct"], color="#146c2e")
    a2.set_ylim(0, 110); a2.set_title("Link utilization (%)")
    plt.tight_layout(); plt.savefig(os.path.join(out_dir, "fairness_utilization.png"), dpi=150); plt.close()

    return summ


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--iface", default="wlp0s20f3")
    ap.add_argument("--modes", nargs="+", default=["default", "equal", "v1", "v2", "v3"])
    ap.add_argument("--duration", type=int, default=90, help="seconds measured per mode")
    ap.add_argument("--warmup", type=int, default=12, help="seconds to settle after a switch")
    ap.add_argument("--name", default="", help="scenario label, e.g. call_stream_download")
    ap.add_argument("--down", type=int, default=3000, help="fixed download link for the run, kbps")
    ap.add_argument("--up", type=int, default=1500, help="fixed upload link for the run, kbps")
    a = ap.parse_args()

    s = read_json(STATE) or {}
    if time.time() - s.get("heartbeat", 0) > 10:
        raise SystemExit("Engine is not running. Start ./start_all.sh first.")
    ctl0 = read_json(CONTROL) or {}
    link = a.down
    if ctl0.get("auto"):
        print("Automatic link speed is on: switching it off for the run.")

    out_dir = os.path.join(LOGS, f"exp_{time.strftime('%Y%m%d_%H%M%S')}{'_' + a.name if a.name else ''}")
    os.makedirs(out_dir, exist_ok=True)
    give_to_user(out_dir)
    total = len(a.modes) * (a.duration + a.warmup)
    print(f"Link {link} kbps. Modes: {', '.join(a.modes)}. About {total // 60} min {total % 60} s.")
    print("Keep the same activities running on all phones until it finishes.")

    with open(os.path.join(out_dir, "samples.csv"), "w", newline="") as f1, \
         open(os.path.join(out_dir, "queues.csv"), "w", newline="") as f2:
        sw, qw = csv.writer(f1), csv.writer(f2)
        sw.writerow(["time", "mode", "ip", "name", "activity", "down_kbps", "up_kbps", "alloc_kbps"])
        qw.writerow(["time", "mode", "class_minor", "category", "rate_kbps", "backlog_bytes", "drops", "delay_ms"])
        try:
            for mode in a.modes:
                run_mode(mode, a, sw, qw)
                f1.flush(); f2.flush()
        finally:
            ctl0["ts"] = time.time()
            write_json(CONTROL, ctl0)            # restore the mode that was active before
            print(f"\n\nRestored mode: {ctl0.get('mode')}")

    summ = summarize(out_dir, link, a.modes)
    for fn in os.listdir(out_dir):
        give_to_user(os.path.join(out_dir, fn))
    print("\n" + summ.to_string(index=False))
    print(f"\nResults in {out_dir}")
