"""Automatic link capacity: one speed test at the start, then follow the line with ping.

Level 1: when auto is switched on, one speed test from the laptop sets the cap to
         85 % of the measured WAN speed.
Level 3: afterwards the cap follows the line, like cake-autorate:
  - the laptop pings an internet host every second. These pings leave on the WAN
    directly (the hotspot shaping does not touch them), so they feel the ISP's queue
  - baseline = idle RTT. It follows drops quickly and rises very slowly
  - RTT more than 30 ms above baseline while a direction is busy (> 50 % of its cap)
    means the ISP queue is building, so that direction is cut by 10 %
  - a direction above 75 % of its cap with RTT within 15 ms of baseline grows by 5 %
  - the cap stays between 25 % and 150 % of the speed test result
Ping can only see congestion, not capacity, which is why Level 1 gives the start value.
"""
import json, re, subprocess, threading, time
from collections import deque

START_FRAC = 0.85
HIGH_MS, LOW_MS = 30.0, 15.0           # delay above baseline: cut / ok to grow
CUT, GROW = 0.90, 1.05
BUSY_CUT, BUSY_GROW = 0.50, 0.75       # load / cap needed before a cut / a grow
CUT_GAP_S, GROW_GAP_S = 4.0, 10.0      # let queues drain after a cut before acting again
MIN_FRAC, MAX_FRAC, MIN_KBPS = 0.25, 1.50, 300
BASE_DOWN, BASE_UP = 0.5, 0.01         # baseline EWMA weight when RTT is below / above it
STEP_S = 2.0


def _round(kbps):
    return int(round(kbps / 100.0) * 100)


class AutoRate:
    """The control rule only (no I/O), so it can be unit tested."""

    def __init__(self, down_kbps, up_kbps):
        self.cap = {"down": float(down_kbps), "up": float(up_kbps)}
        self.lo = {"down": MIN_KBPS, "up": MIN_KBPS}
        self.hi = {"down": 1_000_000, "up": 1_000_000}
        self.measured, self.base = None, None
        self.last_cut, self.last_action = -1e9, "waiting"

    def calibrate(self, down_kbps, up_kbps, ping_ms=None):
        """Speed test result in kbps: start at 85 % of it and set the bounds."""
        self.measured = {"down": float(down_kbps), "up": float(up_kbps)}
        for d, m in self.measured.items():
            self.cap[d] = m * START_FRAC
            self.lo[d], self.hi[d] = max(MIN_KBPS, m * MIN_FRAC), m * MAX_FRAC
        if ping_ms:
            self.base = float(ping_ms)
        self.last_action = "calibrated"

    def step(self, rtt_ms, load, now):
        """rtt_ms: median RTT of the last step (None if every ping was lost)
        load: {"down": kbps, "up": kbps} measured on the WAN
        Returns {"down": kbps, "up": kbps}, rounded to 100 kbps."""
        if rtt_ms is not None:
            if self.base is None:
                self.base = rtt_ms
            w = BASE_DOWN if rtt_ms < self.base else BASE_UP
            self.base += w * (rtt_ms - self.base)
            delay = rtt_ms - self.base
            busy = {d: load.get(d, 0) / self.cap[d] for d in self.cap}
            if delay > HIGH_MS and now - self.last_cut >= CUT_GAP_S:
                cut = [d for d, b in busy.items() if b > BUSY_CUT]
                for d in cut:
                    self.cap[d] = max(self.lo[d], self.cap[d] * CUT)
                if cut:
                    self.last_cut = now
                    self.last_action = f"cut {'+'.join(cut)} (delay {delay:.0f} ms)"
            elif delay < LOW_MS and now - self.last_cut >= GROW_GAP_S:
                grow = [d for d, b in busy.items() if b > BUSY_GROW and self.cap[d] < self.hi[d]]
                for d in grow:
                    self.cap[d] = min(self.hi[d], self.cap[d] * GROW)
                if grow:
                    self.last_action = f"grow {'+'.join(grow)}"
        return {d: _round(v) for d, v in self.cap.items()}


def parse_ping(line):
    m = re.search(r"time[=<]([\d.]+) ?ms", line)
    return float(m.group(1)) if m else None


def parse_default_dev(route_out, exclude):
    for line in route_out.splitlines():
        m = re.search(r"\bdev (\S+)", line)
        if line.startswith("default") and m and m.group(1) != exclude:
            return m.group(1)
    return None


def speedtest():
    """One speed test from the laptop: {"down": kbps, "up": kbps, "ping": ms}."""
    r = subprocess.run(["speedtest-cli", "--json", "--secure"],
                       capture_output=True, text=True, timeout=150)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "speedtest failed")
    d = json.loads(r.stdout)
    return {"down": d["download"] / 1000, "up": d["upload"] / 1000, "ping": d["ping"]}


class LinkTuner:
    """Runs AutoRate against the real engine: pinger, WAN byte counters, speed test."""

    def __init__(self, eng, target="1.1.1.1"):
        self.eng, self.target = eng, target
        self.ar, self.enabled, self.busy = None, False, False
        self.rtts, self.ping = deque(maxlen=50), None
        self.lock = threading.Lock()
        self._publish("off")

    # ---------- switched by the control file ----------
    def enable(self):
        if self.enabled:
            return
        self.enabled = True
        self._start_ping()
        if self.ar is None:
            self.ar = AutoRate(self.eng.down, self.eng.up)
            threading.Thread(target=self._calibrate, daemon=True).start()
        else:                            # back on later: keep the speed test and resume auto's own last cap
            self._apply({d: _round(v) for d, v in self.ar.cap.items()})
            self._publish("tracking")

    def disable(self):
        if not self.enabled:
            return
        self.enabled = False
        if self.ping:
            self.ping.terminate()
            self.ping = None
        self._publish("off")

    # ---------- workers ----------
    def _calibrate(self):
        self.busy = True
        self._publish("measuring")
        try:
            res = speedtest()
            self.ar.calibrate(res["down"], res["up"], res["ping"])
            self._apply(self.ar.step(None, {}, time.time()))
            print(f"\n[auto] speed test {res['down']:.0f}/{res['up']:.0f} kbps -> "
                  f"cap {self.eng.down}/{self.eng.up} kbps", flush=True)
        except Exception as e:           # keep going from the current cap, only without bounds
            self.ar.calibrate(self.eng.down / START_FRAC, self.eng.up / START_FRAC)
            self.ar.last_action = f"speed test failed: {e}"
            print(f"\n[auto] {self.ar.last_action}", flush=True)
        self.busy = False
        self._publish("tracking")

    def _start_ping(self):
        def read(p):
            for line in p.stdout:
                ms = parse_ping(line)
                if ms is not None:
                    with self.lock:
                        self.rtts.append(ms)
        self.ping = subprocess.Popen(["ping", "-n", "-i", "1", self.target],
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        threading.Thread(target=read, args=(self.ping,), daemon=True).start()

    def _wan_bytes(self, dev):
        def rd(name):
            with open(f"/sys/class/net/{dev}/statistics/{name}") as fh:
                return int(fh.read())
        return rd("rx_bytes"), rd("tx_bytes")

    def run(self):
        prev = None
        while True:
            time.sleep(STEP_S)
            try:
                route = subprocess.run(["ip", "-4", "route", "show", "default"],
                                       capture_output=True, text=True).stdout
                wan = parse_default_dev(route, self.eng.iface)
                now = time.time()
                cur = (now, *self._wan_bytes(wan)) if wan else None
                with self.lock:
                    rtts, self.rtts = sorted(self.rtts), deque(maxlen=50)
                if not (self.enabled and self.ar and not self.busy and cur and prev):
                    prev = cur
                    continue
                dt = cur[0] - prev[0]
                load = {"down": max(cur[1] - prev[1], 0) * 8 / 1000 / dt,
                        "up": max(cur[2] - prev[2], 0) * 8 / 1000 / dt}
                prev = cur
                rtt = rtts[len(rtts) // 2] if rtts else None
                self._apply(self.ar.step(rtt, load, now))
                self._publish("tracking", rtt=rtt, load=load, wan=wan)
            except Exception as e:
                self._publish("error", error=str(e))

    def _apply(self, cap):
        if (cap["down"], cap["up"]) != (self.eng.down, self.eng.up):
            self.eng.set_link(cap["down"], cap["up"], rebuild=False)

    def _publish(self, status, rtt=None, load=None, wan=None, error=None):
        ar = self.ar
        info = {"status": status, "down": self.eng.down, "up": self.eng.up}
        if ar:
            info.update(measured={d: round(v) for d, v in ar.measured.items()} if ar.measured else None,
                        baseline_ms=round(ar.base, 1) if ar.base is not None else None,
                        action=ar.last_action)
        if rtt is not None:
            info["rtt_ms"] = round(rtt, 1)
        if load:
            info["load"] = {d: round(v) for d, v in load.items()}
        if wan:
            info["wan"] = wan
        if error:
            info["error"] = error
        self.eng.link_info = info
