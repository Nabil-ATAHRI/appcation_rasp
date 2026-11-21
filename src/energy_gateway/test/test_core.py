import json

import pytest

from energy_gateway.core import Framer, Outbox, parse_frame


def sht_frame(**changes):
    frame = {
        "v": 1,
        "sensor": "sht20",
        "id": 1,
        "seq": 0,
        "uptime_ms": 10,
        "status": "ok",
        "error": "",
        "data": {"temperature_c": -4.2, "humidity_pct": 60},
    }
    frame.update(changes)
    return frame


def mpr_frame(**data_changes):
    data = {
        "voltage_v": [230.0, 231.0, 230.0, 0.0],
        "line_voltage_v": [400.0, 400.0, 400.0],
        "current_a": [1.0, 1.0, 1.0, 0.0],
        "active_power_w": [200.0, 200.0, 200.0, 600.0],
        "reactive_power_var": [10.0, 10.0, 10.0, 30.0],
        "apparent_power_va": [210.0, 210.0, 210.0, 630.0],
        "power_factor": [0.95, 0.95, 0.95],
        "frequency_hz": 50.0,
    }
    data.update(data_changes)
    return {
        "v": 1,
        "sensor": "mpr47s",
        "id": 5,
        "seq": 1,
        "uptime_ms": 100,
        "status": "ok",
        "error": "",
        "data": data,
    }


def test_frame_schema_and_failure_sample():
    assert parse_frame(json.dumps(sht_frame()))["data"]["temperature_c"] == -4.2
    error = sht_frame(status="error", error="acquisition_failed", data=None)
    assert parse_frame(json.dumps(error))["data"] is None
    assert parse_frame(json.dumps(mpr_frame()))["data"]["active_power_w"][3] == 600.0
    with pytest.raises(ValueError, match="length"):
        parse_frame(json.dumps(mpr_frame(line_voltage_v=[400.0, 400.0])))
    with pytest.raises(ValueError):
        parse_frame(json.dumps(sht_frame(data={"temperature_c": 0, "humidity_pct": 101})))
    with pytest.raises(ValueError):
        parse_frame(json.dumps(sht_frame(seq=True)))
    with pytest.raises(ValueError):
        parse_frame(json.dumps(sht_frame(v=True)))
    with pytest.raises(ValueError):
        parse_frame(json.dumps(sht_frame(data={"temperature_c": 10**400, "humidity_pct": 50})))
    assert parse_frame('{"v":1,"event":"boot"}') == {"v": 1, "event": "boot"}


def test_framer_reassembles_discards_overflow_and_resynchronizes():
    framer = Framer()
    assert framer.feed(b"ab") == ([], 0)
    assert framer.feed(b"c\r\nd\n") == (["abc", "d"], 0)
    lines, overflows = framer.feed(b"x" * 2049 + b"\ngood\n")
    assert overflows == 1
    assert lines == ["good"]


def test_outbox_persists_until_acknowledged_and_enforces_limit(tmp_path):
    path = str(tmp_path / "outbox.sqlite3")
    event = {"event_id": "first"}
    box = Outbox(path, limit=1)
    box.push(event)
    with pytest.raises(RuntimeError, match="Outbox full"):
        box.push({"event_id": "second"})
    box.close()

    box = Outbox(path, limit=1)
    assert box.batch() == [event]
    box.acknowledge(["first"])
    assert box.batch() == []
    box.close()
