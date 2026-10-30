"""Bandwidth Manager admin console (Streamlit).

The engine runs separately as root (controller/service.py). They share files in run/:
  run/state.json   live state written by the engine
  run/control.json policy + link chosen here
"""
import hashlib, hmac, html, json, os, secrets, time
from datetime import datetime

import altair as alt
import requests
import pandas as pd
import streamlit as st

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(ROOT, "run", "state.json")
CONTROL = os.path.join(ROOT, "run", "control.json")
ADMIN = os.path.join(ROOT, "data", "admin.json")


def _cloud_cfg():
    try:
        return dict(st.secrets["firebase"])
    except Exception:
        return None


CLOUD = _cloud_cfg()            # set only on Streamlit Cloud (secrets)
STALE_S = 20 if CLOUD else 10

MODE_INFO = {
    "default": ("Default", "No QoS. Every device shares one queue, first come first served."),
    "equal":   ("Equal share", "Every device gets the same guaranteed share of the link."),
    "v1":      ("Adaptive V1", "Fixed need per activity, served in priority order."),
    "v2":      ("Adaptive V2", "Follows measured demand and keeps calls and games protected."),
    "v3":      ("Adaptive V3", "V2 plus live congestion feedback."),
}
PALETTE = ["#0b57d0", "#146c2e", "#b3261e", "#7627bb", "#e37400", "#00838f", "#c2185b", "#5f6368"]

st.set_page_config(page_title="Bandwidth Manager", page_icon="📶", layout="wide",
                   initial_sidebar_state="collapsed")
st.markdown("""
<style>
#MainMenu, footer {visibility: hidden;}
[data-testid="stHeader"] {background: transparent;}
.block-container {padding-top: 1.6rem; max-width: 1240px;}
[data-testid="stVerticalBlockBorderWrapper"] {background: #ffffff; border-radius: 12px;}
[data-testid="stMetric"] {background: #ffffff; border: 1px solid #e0e3e7; border-radius: 12px; padding: 14px 18px;}
[data-testid="stMetricLabel"] p {color: #5f6368; font-size: 12px; font-weight: 500;}
[data-testid="stMetricValue"] {font-weight: 400;}
.bw-top {display: flex; align-items: center; gap: 12px;}
.bw-logo {width: 34px; height: 34px; border-radius: 8px; background: #0b57d0; color: #fff;
          display: grid; place-items: center; font-weight: 600; font-size: 13px;}
.bw-title {font-size: 20px; font-weight: 500; margin: 0; line-height: 1.2; color: #1f1f1f;}
.bw-sub {color: #5f6368; font-size: 12px;}
.bw-card-title {font-size: 16px; font-weight: 500; margin: 0; color: #1f1f1f;}
.pill {display: inline-block; padding: 3px 10px; border-radius: 999px; font-size: 12px;
       font-weight: 500; margin-right: 6px;}
.pill.ok {background: #e6f4ea; color: #146c2e;} .pill.warn {background: #fef7e0; color: #7a4f01;}
.pill.info {background: #e9f0fd; color: #0b57d0;}
</style>
""", unsafe_allow_html=True)


# ---------- helpers ----------
def read_json(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def write_json(path, obj):
    tmp = f"{path}.ui{os.getpid()}.tmp"
    with open(tmp, "w") as fh:
        json.dump(obj, fh)
    os.replace(tmp, path)


def write_control(**changes):
    c = load_control() or {}
    c.update(changes, ts=time.time())
    if CLOUD:
        tok = _fb_token()
        if tok:
            requests.put(_fb_url("control"), params={"auth": tok}, json=c, timeout=10)
    else:
        write_json(CONTROL, c)


def hash_pw(pw, salt):
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 200_000).hex()


def fmt(kbps):
    if kbps is None:
        return "-"
    return f"{kbps / 1000:.1f} Mbps" if kbps >= 1000 else f"{round(kbps)} kbps"


def mode_name(m):
    return MODE_INFO.get(m, (m, ""))[0]


def brand(sub):
    st.markdown(f'<div class="bw-top"><div class="bw-logo">BW</div><div>'
                f'<p class="bw-title">Bandwidth Manager</p><div class="bw-sub">{html.escape(sub)}</div>'
                f'</div></div>', unsafe_allow_html=True)


def card_title(title, sub=None):
    st.markdown(f'<p class="bw-card-title">{html.escape(title)}</p>', unsafe_allow_html=True)
    if sub:
        st.caption(sub)


# ---------- data source: local files, or Firebase when deployed ----------
def _fb_token():
    fb = st.session_state.get("fb")
    if not fb:
        return None
    if time.time() > fb["exp"] - 120:
        r = requests.post(f"https://securetoken.googleapis.com/v1/token?key={CLOUD['api_key']}",
                          data={"grant_type": "refresh_token", "refresh_token": fb["refresh"]}, timeout=10)
        if r.status_code != 200:
            st.session_state.pop("fb", None)
            return None
        j = r.json()
        fb.update(id=j["id_token"], refresh=j["refresh_token"], exp=time.time() + int(j["expires_in"]))
    return fb["id"]


def _fb_url(path):
    return f"{CLOUD['database_url'].rstrip('/')}/gateways/{CLOUD.get('gateway_id', 'laptop1')}/{path}.json"


def _fb_get(path):
    tok = _fb_token()
    if not tok:
        return None
    try:
        r = requests.get(_fb_url(path), params={"auth": tok}, timeout=10)
        return r.json() if r.ok else None
    except (requests.RequestException, ValueError):
        return None


def load_state():
    if not CLOUD:
        return read_json(STATE)
    raw = _fb_get("state_json")
    try:
        return json.loads(raw) if raw else None
    except (TypeError, ValueError):
        return None


def load_control():
    return _fb_get("control") if CLOUD else read_json(CONTROL)


def cloud_login_view():
    _, mid, _ = st.columns([1, 1.1, 1])
    with mid:
        st.write("")
        brand("Cloud console")
        st.write("")
        with st.container(border=True):
            card_title("Sign in", "Use the Firebase account that owns this gateway.")
            with st.form("cloud_login", border=False):
                email = st.text_input("Email")
                pw = st.text_input("Password", type="password")
                ok = st.form_submit_button("Sign in", type="primary")
            if ok:
                try:
                    r = requests.post(
                        "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword"
                        f"?key={CLOUD['api_key']}",
                        json={"email": email, "password": pw, "returnSecureToken": True}, timeout=10)
                except requests.RequestException as e:
                    st.error(f"Could not reach Firebase: {e}")
                    return
                if r.ok:
                    j = r.json()
                    st.session_state.fb = {"id": j["idToken"], "refresh": j["refreshToken"],
                                           "exp": time.time() + int(j["expiresIn"])}
                    st.session_state.user = email
                    st.rerun()
                time.sleep(1)
                st.error("Wrong email or password.")


# ---------- login ----------
def login_view():
    if CLOUD:
        return cloud_login_view()
    admin = read_json(ADMIN)
    _, mid, _ = st.columns([1, 1.1, 1])
    with mid:
        st.write("")
        brand("Admin console")
        st.write("")
        with st.container(border=True):
            if admin is None:
                card_title("Create admin account", "First run. This account protects the console.")
                with st.form("setup", border=False):
                    user = st.text_input("Username", value="admin")
                    pw = st.text_input("Password", type="password")
                    pw2 = st.text_input("Repeat password", type="password")
                    if st.form_submit_button("Create account", type="primary"):
                        if len(pw) < 6:
                            st.error("Use at least 6 characters.")
                        elif pw != pw2:
                            st.error("Passwords do not match.")
                        else:
                            salt = secrets.token_hex(16)
                            os.makedirs(os.path.dirname(ADMIN), exist_ok=True)
                            write_json(ADMIN, {"user": user.strip() or "admin", "salt": salt,
                                               "hash": hash_pw(pw, salt)})
                            os.chmod(ADMIN, 0o600)
                            st.session_state.user = user.strip() or "admin"
                            st.rerun()
            else:
                card_title("Sign in", "Use the admin account for this gateway.")
                with st.form("login", border=False):
                    user = st.text_input("Username")
                    pw = st.text_input("Password", type="password")
                    if st.form_submit_button("Sign in", type="primary"):
                        ok = (hmac.compare_digest(user, admin["user"]) and
                              hmac.compare_digest(hash_pw(pw, admin["salt"]), admin["hash"]))
                        if ok:
                            st.session_state.user = user
                            st.rerun()
                        time.sleep(1)
                        st.error("Wrong username or password.")


# ---------- live part (refreshes every 2 s) ----------
def draw_chart(s):
    rows, link = [], []
    for h in s.get("history", []):
        t = datetime.fromtimestamp(h["t"])
        link.append({"time": t, "kbps": h.get("down")})
        for ip, d in h["devices"].items():
            name = f"{d['name']} ({ip})" if d.get("name") else ip
            rows.append({"time": t, "device": name, "kbps": d["down"]})
    if not rows:
        st.info("Waiting for traffic. Start something on a phone connected to the hotspot.")
        return
    x = alt.X("time:T", title=None,
              axis=alt.Axis(format="%H:%M:%S", grid=False, labelColor="#5f6368", tickCount=6))
    y = alt.Y("kbps:Q", title="kbps",
              axis=alt.Axis(gridColor="#eef0f3", labelColor="#5f6368", titleColor="#5f6368"))
    lines = alt.Chart(pd.DataFrame(rows)).mark_line(strokeWidth=2, interpolate="monotone").encode(
        x=x, y=y,
        color=alt.Color("device:N", scale=alt.Scale(range=PALETTE),
                        legend=alt.Legend(orient="bottom", title=None)),
        tooltip=[alt.Tooltip("device:N", title="Device"),
                 alt.Tooltip("time:T", format="%H:%M:%S", title="Time"),
                 alt.Tooltip("kbps:Q", format=",.0f", title="kbps")])
    cap = alt.Chart(pd.DataFrame(link)).mark_line(
        strokeDash=[6, 4], color="#9aa0a6", strokeWidth=1.5).encode(x=x, y=y)
    st.altair_chart((cap + lines).properties(height=280).configure_view(strokeWidth=0),
                    width="stretch")


@st.fragment(run_every=5 if CLOUD else 2)
def live():
    s = load_state() or {}
    if time.time() - s.get("heartbeat", 0) > STALE_S:
        st.warning("The engine is not running. Start it in another terminal: "
                   "`./run_service.sh --down 3000 --up 1500`")
        return

    shaping = s.get("shaping")
    updated = datetime.fromtimestamp(s["t"]).strftime("%H:%M:%S") if s.get("t") else "-"
    st.markdown(f'<span class="pill {"ok" if shaping else "warn"}">'
                f'{"Shaping on" if shaping else "Dry run, no shaping"}</span>'
                f'<span class="pill info">Active policy: {html.escape(mode_name(s.get("mode", "")))}</span>'
                f'<span class="bw-sub">Updated {updated}</span>', unsafe_allow_html=True)
    if s.get("error"):
        st.error(f"Engine error: {s['error']}")

    v3 = s.get("v3") or {}
    if s.get("mode") == "v3" and v3:
        msg = (f"V3 feedback: downloads may borrow up to {fmt(v3.get('bulk_ceil'))} "
               f"({int(v3.get('bulk_frac', 1) * 100)}% of link). ")
        msg += ("Real-time queue congested, downloads are backing off."
                if v3.get("congested") else "Real-time queues are calm.")
        if v3.get("boost"):
            msg += " Boosted: " + ", ".join(f"{k} x{b}" for k, b in v3["boost"].items()) + "."
        if v3.get("delay_ms"):
            msg += " Queue delay: " + ", ".join(f"{k} {v} ms" for k, v in v3["delay_ms"].items()) + "."
        st.info(msg)

    devs = s.get("devices", [])
    tot_d = sum(d["down_kbps"] for d in devs)
    tot_u = sum(d["up_kbps"] for d in devs)
    util = min(100.0, tot_d / s["down"] * 100) if s.get("down") else 0.0
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Connected devices", len(devs),
              help=f"{sum(len(d['flows']) for d in devs)} active flows")
    m2.metric("Download in use", fmt(tot_d), help=f"Link capacity {fmt(s.get('down'))}")
    m3.metric("Upload in use", fmt(tot_u), help=f"Link capacity {fmt(s.get('up'))}")
    m4.metric("Download link utilization", f"{util:.0f}%")

    with st.container(border=True):
        head, btn = st.columns([5, 1], vertical_alignment="center")
        with head:
            card_title("Download per device", "Measured throughput over the last 5 minutes")
        path = s.get("history_csv")
        if path and os.path.exists(path):
            with open(path, "rb") as fh:
                btn.download_button("Export CSV", fh.read(), file_name=os.path.basename(path),
                                    mime="text/csv", key="export")
        draw_chart(s)

    with st.container(border=True):
        card_title("Connected devices",
                   "Expand a device below the table to see its flows and how each one is classified.")
        if not devs:
            st.info("No active devices yet. A device appears a few seconds after it starts using the hotspot.")
            return
        table = pd.DataFrame([{
            "Device": d["name"] or "Unknown device", "IP": d["ip"],
            "Activity": d["activity"].capitalize(),
            "Confidence": round((d["conf"] or 0) * 100),
            "Download": d["down_kbps"], "Upload": d["up_kbps"],
            "Guaranteed": d["alloc"], "Flows": len(d["flows"]),
        } for d in devs])
        st.dataframe(table, hide_index=True, width="stretch", column_config={
            "Confidence": st.column_config.ProgressColumn("Confidence", min_value=0, max_value=100, format="%d%%"),
            "Download": st.column_config.NumberColumn(format="%d kbps"),
            "Upload": st.column_config.NumberColumn(format="%d kbps"),
            "Guaranteed": st.column_config.NumberColumn(
                format="%d kbps", help="Rate the policy guarantees. Empty in Default mode (shared queue)."),
        })
        for d in devs:
            with st.expander(f"{d['name'] or 'Unknown device'}  ·  {d['ip']}"):
                if not d["flows"]:
                    st.caption("Only small background flows right now.")
                    continue
                st.dataframe(pd.DataFrame([{
                    "Activity": f["activity"].capitalize(),
                    "Confidence": round(f["conf"] * 100),
                    "Using": f["using"], "Guaranteed": f["alloc"],
                    "Priority": f["priority"], "Flow": f["id"],
                } for f in d["flows"]]), hide_index=True, width="stretch", column_config={
                    "Confidence": st.column_config.ProgressColumn("Confidence", min_value=0, max_value=100, format="%d%%"),
                    "Using": st.column_config.NumberColumn(format="%d kbps"),
                    "Guaranteed": st.column_config.NumberColumn(format="%d kbps"),
                    "Priority": st.column_config.NumberColumn(help="0 = most important"),
                })


def run_speedtest():
    import subprocess
    r = subprocess.run(["speedtest-cli", "--json", "--secure"],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "speedtest failed")
    d = json.loads(r.stdout)
    return {"down": d["download"] / 1000, "up": d["upload"] / 1000, "ping": d["ping"]}


def speed_tool():
    res = st.session_state.get("speed")
    c1, c2 = st.columns(2)
    if c1.button("Measure speed", help="Runs a speed test from this laptop, takes about 30 seconds. "
                                       "Do it while the phones are idle."):
        with st.spinner("Measuring internet speed..."):
            try:
                st.session_state.speed = res = run_speedtest()
            except Exception as e:
                st.error(f"Speed test failed: {e}")
    if res:
        sd = max(100, int(res["down"] * 0.8 // 100 * 100))
        su = max(100, int(res["up"] * 0.8 // 100 * 100))
        st.caption(f"Measured {fmt(res['down'])} down, {fmt(res['up'])} up, ping {res['ping']:.0f} ms. "
                   f"Suggested (80%): {sd} / {su} kbps.")
        if c2.button("Use suggested", type="primary"):
            write_control(down=sd, up=su)
            st.toast("Link capacity updated")
            st.rerun()


# ---------- main page ----------
def main_view():
    s = load_state() or {}
    ctl = load_control() or {}

    left, right = st.columns([6, 1], vertical_alignment="center")
    with left:
        brand(f"Hotspot gateway on {s.get('iface', 'wlp0s20f3')}  ·  signed in as {st.session_state.user}")
    with right:
        if st.button("Sign out"):
            st.session_state.pop("user", None)
            st.session_state.pop("fb", None)
            st.rerun()
    st.write("")

    if not ctl:
        st.warning("No engine settings found yet. Start the engine first: `./run_service.sh --down 3000 --up 1500`")

    pol, lnk = st.columns([2, 1])
    with pol:
        with st.container(border=True):
            card_title("Distribution policy", "Choose how the link is shared. A change takes effect within 2 seconds.")
            modes = s.get("modes") or ["default", "equal", "v1", "v2"]
            cur = ctl.get("mode", s.get("mode", "v2"))
            sel = st.segmented_control("Policy", modes, default=cur if cur in modes else None,
                                       format_func=mode_name, key="mode_sel",
                                       label_visibility="collapsed")
            if sel and sel != cur:
                write_control(mode=sel)
                st.toast(f"Policy set to {mode_name(sel)}")
                cur = sel
            st.caption(MODE_INFO.get(cur, ("", ""))[1])
    with lnk:
        with st.container(border=True):
            card_title("Link capacity", "Set about 80% of the real speed, or a fixed low value for experiments.")
            if not CLOUD:
                speed_tool()
            with st.form("link", border=False):
                a, b = st.columns(2)
                down = a.number_input("Download (kbps)", 100, 1_000_000,
                                      int(ctl.get("down", s.get("down", 3000))), step=100)
                up = b.number_input("Upload (kbps)", 100, 1_000_000,
                                    int(ctl.get("up", s.get("up", 1500))), step=100)
                if st.form_submit_button("Apply"):
                    write_control(down=int(down), up=int(up))
                    st.toast("Link capacity updated")
    live()


if "user" not in st.session_state or (CLOUD and not st.session_state.get("fb")):
    login_view()
else:
    main_view()
