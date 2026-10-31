#!/usr/bin/env python3
"""Engine service for the Streamlit dashboard (run as root).

Talks to the dashboard through files, so the dashboard itself needs no root:
  run/state.json    engine -> dashboard, rewritten every 2 s
  run/control.json  dashboard -> engine: {"mode": "v2", "down": 3000, "up": 1500, "auto": false}
                    auto = true: the link follows the line (controller/autorate.py), down/up ignored
  logs/history_<time>.csv   one row per device per tick (for the report)

  sudo .venv/bin/python -m controller.service --down 3000 --up 1500 [--auto]
"""
import argparse, csv, json, os, signal, sys, threading, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from controller.engine import Engine, MODES
from controller.autorate import LinkTuner

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN, LOGS = os.path.join(ROOT, "run"), os.path.join(ROOT, "logs")
STATE, CONTROL = os.path.join(RUN, "state.json"), os.path.join(RUN, "control.json")
CHART_POINTS = 150                     # 5 minutes at 2 s
UID, GID = int(os.environ.get("SUDO_UID", -1)), int(os.environ.get("SUDO_GID", -1))


def give_to_user(path):
    """Files made by root are handed to the normal user, so the dashboard can use them."""
    if UID >= 0:
        os.chown(path, UID, GID)


def write_json(path, obj):
    tmp = path + ".svc.tmp"
    with open(tmp, "w") as fh:
        json.dump(obj, fh)
    os.chmod(tmp, 0o644)
    give_to_user(tmp)
    os.replace(tmp, path)              # atomic: the dashboard never reads half a file


def read_json(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def apply_control(eng, c, tuner=None):
    mode = c.get("mode")
    if mode in MODES and mode != eng.mode:
        eng.set_mode(mode)
    if tuner:
        if c.get("auto"):
            tuner.enable()
            return                     # the tuner owns the link while auto is on
        tuner.disable()
    down, up = int(c.get("down", eng.down)), int(c.get("up", eng.up))
    if (down, up) != (eng.down, eng.up):
        if not (100 <= down <= 1_000_000 and 100 <= up <= 1_000_000):
            raise ValueError("link must be between 100 and 1000000 kbps")
        eng.set_link(down, up)


def service_loop(eng, hist_path, tuner=None):
    fh = open(hist_path, "w", newline="")
    give_to_user(hist_path)
    w = csv.writer(fh)
    w.writerow(["time", "mode", "link_down_kbps", "ip", "name", "activity",
                "down_kbps", "up_kbps", "guaranteed_kbps"])
    last_ctl, last_t = read_json(CONTROL), 0
    while True:
        time.sleep(2)
        errors = []
        c = read_json(CONTROL)
        if c and c != last_ctl:
            try:
                apply_control(eng, c, tuner)
            except Exception as e:
                errors.append(f"control: {e}")
            last_ctl = c
        try:
            eng.tick()
        except Exception as e:
            errors.append(str(e))
        eng.last_error = "; ".join(errors) or None

        h = eng.history[-1] if eng.history else None
        if h and h["t"] > last_t:
            last_t = h["t"]
            for ip, d in h["devices"].items():
                w.writerow([round(h["t"], 1), h["mode"], h["down"], ip, d["name"], d["activity"],
                            d["down"], d["up"], "" if d["alloc"] is None else d["alloc"]])
            fh.flush()

        s = eng.public_state()
        s.update(history=list(eng.history)[-CHART_POINTS:], history_csv=hist_path,
                 heartbeat=time.time())
        write_json(STATE, s)
        status = f"ERROR: {eng.last_error}" if eng.last_error else "ok"
        print(f"\r[{time.strftime('%H:%M:%S')}] mode={s['mode']} link={s['down']}/{s['up']} kbps "
              f"devices={len(s['devices'])} {status}        ", end="", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--iface", default="wlp0s20f3")
    ap.add_argument("--subnet", default="10.42.0.0/24")
    ap.add_argument("--gw", default="10.42.0.1")
    ap.add_argument("--model", default="ml/models/model.pkl")
    ap.add_argument("--mode", default="v2", choices=MODES)
    ap.add_argument("--down", type=int, default=3000)
    ap.add_argument("--up", type=int, default=1500)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--auto", action="store_true",
                    help="measure the line once, then follow it with ping (--down/--up are the start value)")
    ap.add_argument("--ping-target", default="1.1.1.1", help="host the auto link tuner pings")
    a = ap.parse_args()

    for d in (RUN, LOGS):
        os.makedirs(d, exist_ok=True)
        give_to_user(d)
    eng = Engine(a.iface, a.subnet, a.gw, a.model, a.mode, a.down, a.up, a.dry_run)
    if eng.enf:
        eng.enf.setup()
    write_json(CONTROL, {"mode": a.mode, "down": a.down, "up": a.up, "auto": a.auto, "ts": time.time()})
    hist = os.path.join(LOGS, f"history_{time.strftime('%Y%m%d_%H%M%S')}.csv")
    tuner = LinkTuner(eng, a.ping_target)
    if a.auto:
        tuner.enable()
    threading.Thread(target=tuner.run, daemon=True).start()
    threading.Thread(target=service_loop, args=(eng, hist, tuner), daemon=True).start()
    cloud_cfg = read_json(os.path.join(ROOT, "secrets", "cloud.json"))
    if cloud_cfg:
        from controller.cloud_sync import CloudSync
        sync = CloudSync(cloud_cfg, ROOT, read_json, write_json, CONTROL)
        threading.Thread(target=sync.run, args=(eng,), daemon=True).start()
        print(f"Cloud sync ON -> {sync.base}")
    print(f"Engine running ({'dry run' if a.dry_run else 'shaping on'}). History -> {hist}")
    print("Start the dashboard in another terminal: ./run_dashboard.sh   (Ctrl+C here stops shaping)")
    def _stop(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        eng.capture()
    except KeyboardInterrupt:
        pass
    finally:
        tuner.disable()
        if eng.enf:
            eng.enf.teardown()
        s = read_json(STATE) or {}
        s["heartbeat"] = 0
        write_json(STATE, s)
        print("\nShaping removed, network back to normal.")
