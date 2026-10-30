"""Optional cloud relay: mirrors the engine to Firebase Realtime Database.

Enabled automatically when secrets/cloud.json exists:
  {"key": "secrets/firebase-key.json", "db_url": "https://...firebasedatabase.app", "gateway": "laptop1"}
Every 5 s it uploads the live state (as one JSON string, so IPs with dots are safe)
and syncs the control (mode / link) both ways: the newest change ("ts") wins.
"""
import json, os, time

import requests
from google.auth.transport.requests import Request
from google.oauth2 import service_account

SCOPES = ["https://www.googleapis.com/auth/firebase.database",
          "https://www.googleapis.com/auth/userinfo.email"]
INTERVAL_S = 5
CHART_POINTS = 60


class CloudSync:
    def __init__(self, cfg, root, read_json, write_json, control_path):
        key = cfg["key"] if os.path.isabs(cfg["key"]) else os.path.join(root, cfg["key"])
        self.creds = service_account.Credentials.from_service_account_file(key, scopes=SCOPES)
        self.base = f"{cfg['db_url'].rstrip('/')}/gateways/{cfg.get('gateway', 'laptop1')}"
        self.read_json, self.write_json, self.control_path = read_json, write_json, control_path
        self.last_error = None

    def _token(self):
        if not self.creds.valid:
            self.creds.refresh(Request())
        return self.creds.token

    def _req(self, method, path="", **kw):
        url = f"{self.base}{'/' + path if path else ''}.json"
        r = requests.request(method, url, params={"access_token": self._token()}, timeout=10, **kw)
        r.raise_for_status()
        return r.json()

    def push_state(self, eng):
        s = eng.public_state()
        s.update(history=list(eng.history)[-CHART_POINTS:], heartbeat=time.time(), cloud=True)
        self._req("PATCH", json={"state_json": json.dumps(s), "heartbeat": time.time()})

    def sync_control(self):
        local = self.read_json(self.control_path) or {}
        cloud = self._req("GET", "control") or {}
        if cloud.get("ts", 0) > local.get("ts", 0):
            self.write_json(self.control_path, cloud)      # changed from the cloud dashboard
        elif local and local != cloud:
            self._req("PUT", "control", json=local)         # show the local setting in the cloud

    def run(self, eng):
        while True:
            try:
                self.sync_control()
                self.push_state(eng)
                if self.last_error:
                    print("\n[cloud] back online", flush=True)
                self.last_error = None
            except Exception as e:
                if str(e) != self.last_error:
                    print(f"\n[cloud] sync error: {e}", flush=True)
                self.last_error = str(e)
            time.sleep(INTERVAL_S)
