import json

import pytest

from injector import inject


def test_resolve_target_maps_compose_ports():
    assert inject.resolve_target("api-gateway").endswith(":8001")
    assert inject.resolve_target("data-service").endswith(":8002")
    assert inject.resolve_target("downstream-dep").endswith(":8003")


def test_resolve_unknown_target_raises():
    with pytest.raises(ValueError):
        inject.resolve_target("nope")


def test_run_enables_waits_and_reverts(tmp_path):
    calls = []
    sleeps = []
    gt = tmp_path / "gt.jsonl"
    times = iter(["T1", "T2"])

    record = inject.run(
        fault="timeout",
        target="api-gateway",
        params={"delay_ms": 3000},
        duration=30,
        clear=False,
        poster=lambda url, payload: calls.append((url, payload)),
        sleeper=lambda s: sleeps.append(s),
        gt_path=gt,
        now=lambda: next(times),
    )

    assert calls[0][1]["enabled"] is True
    assert calls[1][1]["enabled"] is False
    assert sleeps == [30]
    assert record["fault"] == "timeout"
    assert record["target"] == "api-gateway"
    assert record["started_at"] == "T1"
    assert record["ended_at"] == "T2"
    assert "incident_id" in record

    line = json.loads(gt.read_text().strip())
    assert line["target"] == "api-gateway"
    assert line["params"] == {"delay_ms": 3000}


def test_clear_only_disables_and_writes_no_ground_truth(tmp_path):
    calls = []
    gt = tmp_path / "gt.jsonl"

    record = inject.run(
        fault="timeout",
        target="api-gateway",
        params={},
        duration=None,
        clear=True,
        poster=lambda url, payload: calls.append(payload),
        sleeper=lambda s: None,
        gt_path=gt,
        now=lambda: "T",
    )

    assert calls == [{"fault": "timeout", "enabled": False, "params": {}}]
    assert record is None
    assert not gt.exists()
