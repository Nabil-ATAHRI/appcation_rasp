import json
import math
import sqlite3
import threading
from pathlib import Path
from typing import Any

MAX_FRAME = 2048
UINT32_MAX = (1 << 32) - 1
SHT_FIELDS = {"temperature_c", "humidity_pct"}
MPR_FIELDS = {
    "voltage_v": 4,
    "line_voltage_v": 3,
    "current_a": 4,
    "active_power_w": 4,
    "reactive_power_var": 4,
    "apparent_power_va": 4,
    "power_factor": 3,
}


class OutboxFullError(RuntimeError):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _reject_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON number: {value}")


def _number(value: Any, minimum: float, maximum: float) -> None:
    _require(
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and (isinstance(value, int) or math.isfinite(value))
        and minimum <= value <= maximum,
        "Measurement outside limits",
    )


def _keys(value: Any, expected: set[str]) -> None:
    _require(isinstance(value, dict) and set(value) == expected, "Unexpected or missing fields")


def parse_frame(line: str) -> dict[str, Any]:
    _require(len(line.encode("utf-8")) <= MAX_FRAME, "Frame too large")
    frame = json.loads(line, parse_constant=_reject_constant)
    _require(
        isinstance(frame, dict)
        and isinstance(frame.get("v"), int)
        and not isinstance(frame.get("v"), bool)
        and frame["v"] == 1,
        "Unsupported protocol version",
    )
    if "event" in frame:
        _keys(frame, {"v", "event"})
        _require(frame["event"] == "boot", "Unknown board event")
        return frame

    _keys(frame, {"v", "sensor", "id", "seq", "uptime_ms", "status", "error", "data"})
    _require(
        isinstance(frame["sensor"], str) and frame["sensor"] in {"sht20", "mpr47s"},
        "Unknown sensor",
    )
    for name in ("id", "seq", "uptime_ms"):
        value = frame[name]
        _require(
            isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= UINT32_MAX,
            f"Invalid {name}",
        )
    _require(1 <= frame["id"] <= 247, "Invalid sensor ID")

    if frame["status"] == "error":
        _require(
            frame["data"] is None and frame["error"] == "acquisition_failed",
            "Invalid error sample",
        )
        return frame
    _require(frame["status"] == "ok" and frame["error"] == "", "Invalid sample status")

    data = frame["data"]
    if frame["sensor"] == "sht20":
        _keys(data, SHT_FIELDS)
        _number(data["temperature_c"], -40, 125)
        _number(data["humidity_pct"], 0, 100)
        return frame

    _keys(data, {*MPR_FIELDS, "frequency_hz"})
    for name, length in MPR_FIELDS.items():
        values = data[name]
        _require(isinstance(values, list) and len(values) == length, f"Invalid {name} length")
        for value in values:
            _number(value, -1e30, 1e30)
    _number(data["frequency_hz"], 0, 1000)
    return frame


class Framer:
    def __init__(self) -> None:
        self._line = bytearray()
        self._discard = False

    def feed(self, data: bytes) -> tuple[list[str], int]:
        lines: list[str] = []
        overflows = 0
        for byte in data:
            if byte == 10:
                if self._line and not self._discard:
                    if self._line.endswith(b"\r"):
                        self._line.pop()
                    try:
                        lines.append(self._line.decode("utf-8", errors="strict"))
                    except UnicodeDecodeError:
                        overflows += 1
                self._line.clear()
                self._discard = False
            elif not self._discard:
                if len(self._line) == MAX_FRAME:
                    self._line.clear()
                    self._discard = True
                    overflows += 1
                else:
                    self._line.append(byte)
        return lines, overflows


class Outbox:
    def __init__(self, path: str, limit: int = 100_000) -> None:
        if limit <= 0:
            raise ValueError("Outbox limit must be positive")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._limit = limit
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, timeout=5, isolation_level=None, check_same_thread=False)
        try:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS outbox "
                "(event_id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
            )
        except sqlite3.Error:
            self._db.close()
            raise

    def push(self, event: dict[str, Any]) -> None:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                count = self._db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
                if count >= self._limit:
                    raise OutboxFullError("Outbox full: restore cloud connectivity")
                self._db.execute(
                    "INSERT INTO outbox(event_id, payload) VALUES(?, ?)",
                    (event["event_id"], json.dumps(event, separators=(",", ":"), allow_nan=False)),
                )
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    def batch(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT payload FROM outbox ORDER BY rowid LIMIT ?", (limit,)
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def acknowledge(self, ids: list[str]) -> None:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.executemany("DELETE FROM outbox WHERE event_id = ?", ((id_,) for id_ in ids))
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    def close(self) -> None:
        with self._lock:
            self._db.close()
