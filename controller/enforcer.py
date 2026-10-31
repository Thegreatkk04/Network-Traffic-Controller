"""Turn allocations into real shaping on the hotspot interface.

Download (internet -> phone): HTB on the hotspot interface (egress).
Upload   (phone -> internet): hotspot ingress is redirected to ifb0, HTB there.
nftables puts a mark on every forwarded packet:
    mark = last_octet(device IP) * 16 + k
    k = 1..4 -> flow priority 0..3,  k = 5 -> device default (tiny / unclassified flows)
tc class 1:<mark hex> holds that traffic. Device parent = 1:<last_octet*16 hex>.
Class 1:2 catches everything without a mark.
Admin rules: a capped device's parent class gets ceil = cap (in every mode, Default
included), and a blocked device's packets are dropped by the nftables set @blocked.
"""
import subprocess

from controller.core.flow_policy import equal_shares

K_DEFAULT = 5
PROTO = {6: "tcp", 17: "udp"}
HTB_PRIO = {1: 0, 2: 1, 3: 2, 4: 3, 5: 2}

NFT = """
table inet bwmgr {
  map devmark  { type ipv4_addr : mark ; }
  map flowmark { type ipv4_addr . ipv4_addr . inet_proto . inet_service . inet_service : mark ; }
  set blocked  { type ipv4_addr ; }
  chain bwfwd {
    type filter hook forward priority mangle; policy accept;
    oifname "LAN" ip daddr @blocked drop
    iifname "LAN" ip saddr @blocked drop
    oifname "LAN" meta mark set ip daddr map @devmark
    oifname "LAN" meta mark set ip daddr . ip saddr . meta l4proto . th dport . th sport map @flowmark
    iifname "LAN" meta mark set ip saddr map @devmark
    iifname "LAN" meta mark set ip saddr . ip daddr . meta l4proto . th sport . th dport map @flowmark
    meta mark != 0 ct mark set meta mark
  }
}
"""


def mark_of(ip, k):
    return int(ip.rsplit(".", 1)[1]) * 16 + k


def _run(cmd, stdin=None, check=True):
    r = subprocess.run(cmd, input=stdin, text=True, capture_output=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed: {r.stderr.strip()}")
    return r


class Enforcer:
    def __init__(self, lan, down_kbps, up_kbps, ifb="ifb0"):
        self.lan, self.ifb, self.down = lan, ifb, down_kbps
        self.links = {lan: down_kbps, ifb: up_kbps}
        self.rates = {}            # (dev, minor) -> (rate, prio) last applied
        self.made = set()          # (dev, minor) leaves that have qdisc + filter
        self.devmark, self.flowmark, self.blocked = {}, {}, set()

    def _tc(self, lines, check=True):
        if lines:
            _run(["tc", "-force", "-batch", "-"], "\n".join(lines) + "\n", check)

    def setup(self):
        self.teardown()
        _run(["modprobe", "ifb"], check=False)
        _run(["ip", "link", "add", self.ifb, "type", "ifb"], check=False)
        _run(["ip", "link", "set", self.ifb, "up"])
        for dev, link in self.links.items():
            self._tc([
                f"qdisc add dev {dev} root handle 1: htb default 2",
                f"class add dev {dev} parent 1: classid 1:1 htb rate {link}kbit ceil {link}kbit quantum 1514",
                f"class add dev {dev} parent 1:1 classid 1:2 htb rate {link}kbit ceil {link}kbit prio 3 quantum 1514",
                f"qdisc add dev {dev} parent 1:2 handle 2: pfifo limit 300",
            ])
        self._tc([f"qdisc add dev {self.lan} handle ffff: ingress",
                  f"filter add dev {self.lan} parent ffff: protocol ip prio 1 matchall "
                  f"action connmark action mirred egress redirect dev {self.ifb}"])
        _run(["nft", "-f", "-"], NFT.replace("LAN", self.lan))

    def teardown(self):
        for args in (["qdisc", "del", "dev", self.lan, "root"],
                     ["qdisc", "del", "dev", self.lan, "ingress"],
                     ["qdisc", "del", "dev", self.ifb, "root"]):
            _run(["tc", *args], check=False)
        _run(["nft", "delete", "table", "inet", "bwmgr"], check=False)
        self.rates.clear(); self.made.clear()
        self.devmark, self.flowmark, self.blocked = {}, {}, set()

    def apply(self, mode, ips, alloc, keys, bulk_ceil=None, caps=None, blocked=()):
        """mode: default|equal|v1|v2|v3   ips: active device IPs (blocked ones left out)
        alloc: {ip: {flow_id: Allocation}} (v1/v2/v3 only)
        keys:  flow_id -> (client_ip, remote_ip, proto, client_port, remote_port)
        caps:  {ip: (down_kbps or None, up_kbps or None)}   blocked: IPs to drop"""
        caps = caps or {}
        parts, devmark, flowmark = {}, {}, {}
        if mode == "default":                # no QoS, except that capped devices get their own class
            for ip in ips:
                if ip in caps:
                    share = {k: 0.0 for k in range(1, 6)}
                    share[K_DEFAULT] = 100.0
                    devmark[ip] = mark_of(ip, K_DEFAULT)
                    parts[ip] = share
        else:
            eq = equal_shares(ips, self.down, {ip: c[0] for ip, c in caps.items() if c[0]}) \
                if mode == "equal" else {}
            for ip in ips:
                share = {k: 0.0 for k in range(1, 6)}
                if mode == "equal":
                    share[K_DEFAULT] = eq[ip]
                else:
                    for fid, a in alloc.get(ip, {}).items():
                        key = keys.get(fid)
                        if key and key[2] in PROTO:
                            k = min(a.priority, 3) + 1
                            elem = f"{key[0]} . {key[1]} . {PROTO[key[2]]} . {key[3]} . {key[4]}"
                            flowmark[elem] = mark_of(ip, k)
                        else:
                            k = K_DEFAULT
                        share[k] += a.rate_kbps
                devmark[ip] = mark_of(ip, K_DEFAULT)
                parts[ip] = share
        self._apply_tc(mode, parts, bulk_ceil, caps)
        self._apply_nft(devmark, flowmark, set(blocked))

    def _cls(self, lines, dev, parent, minor, rate, prio, link, leaf):
        key = (dev, minor)
        old = self.rates.get(key)
        if old and old[1] == prio and old[2] == link and abs(old[0] - rate) <= 0.05 * max(old[0], 1):
            return
        lines.append(f"class replace dev {dev} parent {parent} classid 1:{minor:x} "
                     f"htb rate {rate}kbit ceil {link}kbit prio {prio} quantum 1514")
        self.rates[key] = (rate, prio, link)
        if leaf and key not in self.made:
            lines.append(f"qdisc replace dev {dev} parent 1:{minor:x} handle {minor:x}: fq_codel")
            lines.append(f"filter add dev {dev} parent 1: protocol ip prio 1 handle {minor} fw classid 1:{minor:x}")
            self.made.add(key)

    def _apply_tc(self, mode, parts, bulk_ceil=None, caps=None):
        caps = caps or {}
        for dev, link in self.links.items():
            scale = link / self.down
            side = 0 if dev == self.lan else 1           # which half of (down, up) caps this queue
            lines = []
            default_rate = link if mode == "default" else max(int(100 * scale), 8)
            self._cls(lines, dev, "1:1", 2, default_rate, 3, link, leaf=False)
            for ip, share in parts.items():
                pm = mark_of(ip, 0)
                cap = (caps.get(ip) or (None, None))[side]
                dev_ceil = min(link, int(cap)) if cap else link
                kids = {k: min(max(int(r * scale), 8), dev_ceil) for k, r in share.items()}
                self._cls(lines, dev, "1:1", pm, min(sum(kids.values()), dev_ceil), 0, dev_ceil, leaf=False)
                for k, r in kids.items():
                    ceil = min(link, max(int(bulk_ceil * scale), r)) if (bulk_ceil and k == 4) else link
                    self._cls(lines, dev, f"1:{pm:x}", mark_of(ip, k), r, HTB_PRIO[k],
                              min(ceil, dev_ceil), leaf=True)
            self._tc(lines)

    def _apply_nft(self, devmark, flowmark, blocked=frozenset()):
        cmds = [f"delete element inet bwmgr blocked {{ {ip} }}" for ip in self.blocked - blocked]
        cmds += [f"add element inet bwmgr blocked {{ {ip} }}" for ip in blocked - self.blocked]
        for name, new, old in (("devmark", devmark, self.devmark),
                               ("flowmark", flowmark, self.flowmark)):
            for k, v in old.items():
                if new.get(k) != v:
                    cmds.append(f"delete element inet bwmgr {name} {{ {k} }}")
            for k, v in new.items():
                if old.get(k) != v:
                    cmds.append(f"add element inet bwmgr {name} {{ {k} : {v} }}")
        if cmds:
            _run(["nft", "-f", "-"], "\n".join(cmds) + "\n")
        self.devmark, self.flowmark, self.blocked = devmark, flowmark, set(blocked)

    def set_link(self, down_kbps, up_kbps):
        """Rebuild the shaping tree for a new link capacity."""
        self.down = down_kbps
        self.links = {self.lan: down_kbps, self.ifb: up_kbps}
        self.setup()

    def retune(self, down_kbps, up_kbps):
        """Change the link capacity in place: queues and marks stay. Only the root class
        changes now; the next apply() re-sends every class whose ceiling used the old link."""
        self.down = down_kbps
        self.links = {self.lan: down_kbps, self.ifb: up_kbps}
        self._tc([f"class change dev {dev} parent 1: classid 1:1 htb rate {link}kbit ceil {link}kbit quantum 1514"
                  for dev, link in self.links.items()])

    def stats(self):
        """Download-side leaf queues: {class_minor: (backlog_bytes, total_drops)}."""
        import json
        r = _run(["tc", "-s", "-j", "qdisc", "show", "dev", self.lan], check=False)
        try:
            data = json.loads(r.stdout or "[]")
        except ValueError:
            return {}
        out = {}
        for q in data:
            parent = q.get("parent", "")
            if q.get("kind") == "fq_codel" and parent.startswith("1:"):
                out[int(parent[2:], 16)] = (int(q.get("backlog", 0)), int(q.get("drops", 0)))
        return out

    def class_rates(self):
        """{class_minor: guaranteed kbps} currently applied on the download side."""
        return {minor: v[0] for (dev, minor), v in self.rates.items() if dev == self.lan}
