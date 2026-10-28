"""Demo: classifier + debounce + policy v1/v2, using CSV rows as pretend live traffic.
Run from the project root:  python demo_replay.py
"""
import glob

import numpy as np
import pandas as pd

from controller.core.classify import Classifier
from controller.core.policy import (
    ActivityDebouncer, DeviceState, policy_v1, policy_v2,
)

LINK = 10000   # kbps
STEPS = 8

clf = Classifier()
deb = ActivityDebouncer(k=3)

# one pretend device per recorded class
sessions = {}
for f in sorted(glob.glob("data/raw/*.csv")):
    df = pd.read_csv(f)
    sessions[df["label"].iloc[0]] = df
devices = {f"10.0.0.{i + 2}": label for i, label in enumerate(sessions)}

rng = np.random.RandomState(1)
for step in range(1, STEPS + 1):
    states, info = [], {}
    for ip, truth in devices.items():
        row = sessions[truth].sample(1, random_state=int(rng.randint(1_000_000)))
        label, conf = clf.predict(row)
        act = deb.update(ip, label)                   # needs 3 identical readings to change
        demand = float(row["bytes_per_s"].iloc[0]) * 8 / 1000
        states.append(DeviceState(ip, act, conf, demand))
        info[ip] = (truth, label, act, conf, demand)

    v1, v2 = policy_v1(states, LINK), policy_v2(states, LINK)
    print(f"\n--- step {step} ---")
    print(f"{'device':10} {'truth':10} {'model':10} {'accepted':10} {'conf':>5} "
          f"{'demand':>8} {'v1':>6} {'v2':>6}")
    for ip, (truth, label, act, conf, demand) in info.items():
        print(f"{ip:10} {truth:10} {label:10} {act:10} {conf:5.2f} "
              f"{demand:8.0f} {v1[ip].rate_kbps:6d} {v2[ip].rate_kbps:6d}")