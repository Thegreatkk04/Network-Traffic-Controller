#!/usr/bin/env python3
"""Convert labeled .pcap/.pcapng captures into feature CSVs in data/raw/.

Usage:
  python ml/collect/pcap_to_csv.py pcaps/*.pcapng
  python ml/collect/pcap_to_csv.py pcaps/x.pcapng --label call --app meet
  python ml/collect/pcap_to_csv.py pcaps/x.pcapng --me 192.168.1.23
"""
import argparse
import ipaddress
import os
import shutil
import subprocess
import tempfile
from collections import Counter

import pandas as pd
from nfstream import NFStreamer

# keyword in file name -> label (checked in this order)
LABEL_KEYWORDS = [
    ("video_call", "call"), ("videocall", "call"), ("call", "call"),
    ("stream", "streaming"), ("youtube", "streaming"), ("netflix", "streaming"),
    ("gam", "gaming"),
    ("download", "download"), ("_dl", "download"),
    ("brows", "browsing"),
]

IGNORE_PORTS = {53, 67, 68, 123, 137, 138, 1900, 5353}   # DNS, DHCP, NTP, mDNS, SSDP...
MIN_PACKETS = 10                                          # tiny flows carry no pattern

STATS = [
    "packets", "bytes",
    "min_ps", "mean_ps", "stddev_ps", "max_ps",                       # packet size
    "min_piat_ms", "mean_piat_ms", "stddev_piat_ms", "max_piat_ms",   # gap between packets
]


def label_from_name(name):
    n = name.lower()
    for key, label in LABEL_KEYWORDS:
        if key in n:
            return label
    return None


def is_private_v4(ip):
    try:
        a = ipaddress.ip_address(ip)
        return a.version == 4 and a.is_private
    except ValueError:
        return False


def guess_my_ip(flows):
    """The private IPv4 address that appears in the most packets is probably us."""
    c = Counter()
    for f in flows:
        for ip in (f.src_ip, f.dst_ip):
            if is_private_v4(ip):
                c[ip] += f.bidirectional_packets
    return c.most_common(1)[0][0] if c else None


def read_flows(path):
    """Read a capture into flows; fall back to converting with editcap."""
    def run(p):
        return list(NFStreamer(
            source=p,
            statistical_analysis=True,   # packet size and gap statistics
            n_dissections=0,             # app detection OFF, so the model can't cheat
            idle_timeout=10,             # flow ends after 10 s of silence
            active_timeout=5,            # cut long flows into 5-second pieces
        ))

    try:
        flows = run(path)
        if flows:
            return flows
    except Exception as e:
        print(f"  direct read failed ({e}); trying editcap conversion")

    if shutil.which("editcap"):
        tmp = os.path.join(tempfile.mkdtemp(), "converted.pcap")
        subprocess.run(["editcap", "-F", "pcap", path, tmp], check=True)
        return run(tmp)
    print("  tip: sudo apt install wireshark-common   (gives editcap)")
    return []


def flow_to_row(f, me):
    """One flow piece -> one row of features (or None to skip it)."""
    if f.bidirectional_packets < MIN_PACKETS:
        return None
    if f.src_port in IGNORE_PORTS or f.dst_port in IGNORE_PORTS:
        return None

    # "up" = from us to the internet, "down" = towards us
    if f.src_ip == me:
        up, down = "src2dst", "dst2src"
    elif f.dst_ip == me:
        up, down = "dst2src", "src2dst"
    else:
        return None

    dur = max(f.bidirectional_duration_ms / 1000.0, 0.001)
    row = {"protocol": f.protocol, "duration_s": dur}
    for name, real in (("up", up), ("down", down)):
        for s in STATS:
            row[f"{name}_{s}"] = getattr(f, f"{real}_{s}")

    total = f.bidirectional_bytes
    row["packets_per_s"] = f.bidirectional_packets / dur
    row["bytes_per_s"] = total / dur
    row["up_byte_share"] = row["up_bytes"] / total if total else 0.0
    return row


def convert(path, label_override, app_override, me_override):
    name = os.path.splitext(os.path.basename(path))[0]
    label = label_override or label_from_name(name)
    if not label:
        print(f"SKIP {path}: can't tell the label from the name, use --label")
        return
    app = app_override or (name.split("_")[1] if "_" in name else "unknown")

    print(f"Reading {path} (label={label}) ...")
    flows = read_flows(path)
    if not flows:
        print("  SKIP: no flows read")
        return

    me = me_override or guess_my_ip(flows)
    if not me:
        print("  SKIP: couldn't detect your IP, pass --me")
        return

    rows = [r for r in (flow_to_row(f, me) for f in flows) if r]
    if not rows:
        print("  SKIP: no usable flows (very little traffic, or wrong --me?)")
        return

    df = pd.DataFrame(rows)
    df.insert(0, "session_id", name)
    df.insert(1, "label", label)
    df.insert(2, "app", app)
    df["note"] = f"pcap, me={me}"

    out = os.path.join("data", "raw", f"{name}.csv")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    df.to_csv(out, index=False)
    print(f"  OK: {len(df)} rows -> {out}  (me={me}, flows read={len(flows)})")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--label", default=None, help="force the label for all given files")
    ap.add_argument("--app", default=None, help="force the app name")
    ap.add_argument("--me", default=None, help="your IPv4 address if auto-detect is wrong")
    a = ap.parse_args()
    for f in a.files:
        convert(f, a.label, a.app, a.me)