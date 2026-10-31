from controller.enforcer import K_DEFAULT, reserve_default


def share(**kw):
    s = {k: 0.0 for k in range(1, 6)}
    s.update({int(k[1:]): v for k, v in kw.items()})
    return s


def test_reserve_comes_from_download_first():
    s = reserve_default(share(k1=500, k4=3000), 300)
    assert s[K_DEFAULT] == 300 and s[4] == 2700 and s[1] == 500


def test_realtime_is_never_touched():
    s = reserve_default(share(k1=500, k4=100), 300)
    assert s[1] == 500                       # call / game keeps everything
    assert s[4] == 8 and s[K_DEFAULT] == 92  # takes what bulk can give, keeps the 8 kbps minimum


def test_device_total_is_unchanged():
    before = share(k1=200, k3=1200, k4=900, k5=50)
    total = sum(before.values())
    after = reserve_default(dict(before), 300)
    assert sum(after.values()) == total and after[K_DEFAULT] == 300


def test_existing_default_share_is_kept():
    s = reserve_default(share(k4=1000, k5=400), 300)
    assert s[K_DEFAULT] == 400 and s[4] == 1000
