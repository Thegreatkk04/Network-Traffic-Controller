from controller.core.flow_policy import DeviceFlows, FlowState
from controller.core.v3 import AdaptiveV3

LINK = 8000
CALL_MINOR = 88 * 16 + 1          # real-time class of 10.42.0.88


def devs():
    return [DeviceFlows("10.42.0.88", [FlowState("call", "call", 0.95, 1500),
                                       FlowState("dl", "download", 0.97, 8000)]),
            DeviceFlows("10.42.0.45", [FlowState("dl2", "download", 0.97, 8000)])]


def test_calm_network_keeps_full_download_ceiling():
    v3 = AdaptiveV3()
    _, ceil = v3.step(devs(), LINK, {}, {})
    assert ceil == LINK and not v3.info["congested"]


def test_congested_call_gets_boost_and_downloads_back_off():
    v3 = AdaptiveV3()
    calm, _ = v3.step(devs(), LINK, {CALL_MINOR: (0, 0)}, {CALL_MINOR: 1950})
    hot, ceil = v3.step(devs(), LINK, {CALL_MINOR: (10_000, 3)}, {CALL_MINOR: 1950})  # ~41 ms queue
    assert v3.info["congested"]
    assert ceil < LINK
    assert hot["10.42.0.88"]["call"].rate_kbps > calm["10.42.0.88"]["call"].rate_kbps


def test_ceiling_recovers_when_calm_again():
    v3 = AdaptiveV3()
    v3.step(devs(), LINK, {CALL_MINOR: (10_000, 0)}, {CALL_MINOR: 1950})
    low = v3.info["bulk_ceil"]
    for _ in range(3):
        v3.step(devs(), LINK, {CALL_MINOR: (0, 0)}, {CALL_MINOR: 1950})
    assert v3.info["bulk_ceil"] > low


def test_never_over_link():
    v3 = AdaptiveV3()
    for _ in range(5):
        out, _ = v3.step(devs(), LINK, {CALL_MINOR: (50_000, 9)}, {CALL_MINOR: 1950})
    assert sum(a.rate_kbps for fl in out.values() for a in fl.values()) <= LINK
