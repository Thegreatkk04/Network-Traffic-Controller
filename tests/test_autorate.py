from controller.autorate import AutoRate, parse_default_dev, parse_ping

BASE = 40.0


def calibrated():
    ar = AutoRate(3000, 1500)
    ar.calibrate(20000, 8000, ping_ms=BASE)
    return ar


def test_speed_test_sets_85_percent_and_bounds():
    ar = calibrated()
    cap = ar.step(None, {}, 0)
    assert cap == {"down": 17000, "up": 6800}
    assert ar.lo["down"] == 5000 and ar.hi["down"] == 30000


def test_rising_ping_cuts_only_the_busy_direction():
    ar = calibrated()
    cap = ar.step(BASE + 100, {"down": 16000, "up": 200}, 100)
    assert cap["down"] == 15300                 # 17000 * 0.9
    assert cap["up"] == 6800                    # upload idle, not blamed
    assert ar.last_action.startswith("cut down")


def test_high_ping_with_idle_line_changes_nothing():
    ar = calibrated()
    assert ar.step(BASE + 100, {"down": 100, "up": 100}, 100) == {"down": 17000, "up": 6800}


def test_cuts_wait_for_the_queue_to_drain():
    ar = calibrated()
    busy = {"down": 16000, "up": 6000}
    first = ar.step(BASE + 100, busy, 100)
    second = ar.step(BASE + 100, busy, 102)     # 2 s later: too soon
    third = ar.step(BASE + 100, busy, 105)
    assert second == first and third["down"] < first["down"]


def test_saturated_line_with_low_ping_grows():
    ar = calibrated()
    cap = ar.step(BASE + 2, {"down": 16500, "up": 200}, 100)
    assert cap["down"] == 17800                 # 17000 * 1.05, rounded to 100
    assert cap["up"] == 6800


def test_cap_stays_inside_bounds():
    ar = calibrated()
    t = 0
    for _ in range(100):
        t += 5
        ar.step(BASE + 200, {"down": 1e9, "up": 1e9}, t)
    assert ar.step(None, {}, t)["down"] == 5000             # 25 % of 20000
    for _ in range(200):
        t += 11
        ar.step(BASE, {"down": 1e9, "up": 1e9}, t)
    assert ar.step(None, {}, t)["down"] == 30000            # 150 % of 20000


def test_baseline_drops_fast_and_rises_slowly():
    ar = calibrated()
    ar.step(20.0, {}, 0)
    assert ar.base == 30.0                      # halfway down at once
    ar.step(130.0, {}, 2)
    assert ar.base < 32.0                       # one slow sample barely moves it


def test_parsers():
    assert parse_ping("64 bytes from 1.1.1.1: icmp_seq=3 ttl=57 time=23.4 ms") == 23.4
    assert parse_ping("Request timeout for icmp_seq 4") is None
    route = ("default via 10.42.0.1 dev wlp0s20f3 proto static metric 600\n"
             "default via 192.168.42.129 dev enx82a8b66077d8 proto dhcp metric 100\n")
    assert parse_default_dev(route, exclude="wlp0s20f3") == "enx82a8b66077d8"
