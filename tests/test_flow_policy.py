from controller.core.flow_policy import (
    DeviceFlows, FlowState, IDLE_FLOW, allocate_flows, device_totals,
)

LINK = 10000


def total(out):
    return sum(a.rate_kbps for fl in out.values() for a in fl.values())


def test_call_protected_from_download_on_same_device():
    devs = [DeviceFlows("A", [FlowState("call", "call", 0.95, 1800),
                              FlowState("dl", "download", 0.97, 8000)]),
            DeviceFlows("B", [FlowState("dl", "download", 0.97, 8000)])]
    out = allocate_flows(devs, LINK, "v2")
    assert out["A"]["call"].rate_kbps >= 1800
    assert total(out) <= LINK
    # downloads are split equally per device (B only has the floor extra)
    assert abs((out["B"]["dl"].rate_kbps - 500) - out["A"]["dl"].rate_kbps) <= 1


def test_many_connections_do_not_win():
    many = DeviceFlows("A", [FlowState(f"d{i}", "download", 0.97, 8000) for i in range(5)])
    one = DeviceFlows("B", [FlowState("d0", "download", 0.97, 8000)])
    out = allocate_flows([many, one], LINK, "v1")
    t = device_totals(out)
    assert abs(t["A"] - t["B"]) <= 5


def test_call_beats_crowd_where_plain_fair_share_would_not():
    devs = [DeviceFlows(f"d{i}", [FlowState("dl", "download", 0.97, 8000)]) for i in range(10)]
    devs.append(DeviceFlows("caller", [FlowState("call", "call", 0.95, 2500)]))
    out = allocate_flows(devs, LINK, "v1")
    assert out["caller"]["call"].rate_kbps >= 3000      # fair share would be ~909
    assert all(out[f"d{i}"]["dl"].rate_kbps > 0 for i in range(10))
    assert total(out) <= LINK


def test_tiny_flows_are_ignored():
    devs = [DeviceFlows("X", [FlowState("noise", "call", 0.9, 5)]),
            DeviceFlows("Y", [FlowState("dl", "download", 0.97, 8000)])]
    out = allocate_flows(devs, LINK, "v2")
    assert list(out["X"]) == [IDLE_FLOW]
    assert out["X"][IDLE_FLOW].rate_kbps == 500


def test_v2_reserve_keeps_call_alive_during_quiet_moment():
    devs = [DeviceFlows("A", [FlowState("call", "call", 0.95, 100)]),
            DeviceFlows("B", [FlowState("dl", "download", 0.97, 8000)])]
    v2 = allocate_flows(devs, LINK, "v2")
    assert v2["A"]["call"].rate_kbps >= 1200


def test_v2_returns_unused_bandwidth_compared_to_v1():
    devs = [DeviceFlows("A", [FlowState("call", "call", 0.95, 1000)]),
            DeviceFlows("B", [FlowState("dl", "download", 0.97, 8000)])]
    v1 = allocate_flows(devs, LINK, "v1")
    v2 = allocate_flows(devs, LINK, "v2")
    assert v2["B"]["dl"].rate_kbps > v1["B"]["dl"].rate_kbps
    assert v2["A"]["call"].rate_kbps >= 1200


def test_low_confidence_flow_gets_unknown_priority():
    devs = [DeviceFlows("A", [FlowState("f", "call", 0.4, 2000)])]
    out = allocate_flows(devs, LINK, "v1")
    assert out["A"]["f"].priority == 2


def test_nothing_negative_and_never_over_link():
    devs = [DeviceFlows(f"d{i}", [FlowState("a", "streaming", 0.9, 4000),
                                  FlowState("b", "download", 0.9, 9000)])
            for i in range(12)]
    for mode in ("v1", "v2"):
        out = allocate_flows(devs, LINK, mode)
        assert total(out) <= LINK
        assert all(a.rate_kbps >= 0 for fl in out.values() for a in fl.values())


def test_empty_and_bad_mode():
    assert allocate_flows([], LINK) == {}
    import pytest
    with pytest.raises(ValueError):
        allocate_flows([DeviceFlows("A")], LINK, "v9")