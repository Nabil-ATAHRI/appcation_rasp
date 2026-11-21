import argparse
import errno
import json
import logging
import os
import secrets
import signal
import sqlite3
import threading
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

import serial

from .core import Framer, Outbox, OutboxFullError, parse_frame

log = logging.getLogger("energy_gateway")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


http = build_opener(_NoRedirect)


def _required_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"Missing environment variable: {name}")
    return value


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _upload(url: str, token: str, batch: list[dict]) -> None:
    payload = json.dumps({"events": batch}, separators=(",", ":"), allow_nan=False).encode()
    request = Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with http.open(request, timeout=15) as response:
            if response.status != 200:
                raise RuntimeError(f"Ingestion HTTP status {response.status}")
            body = response.read(65_537)
    except HTTPError as exc:
        raise RuntimeError(f"Ingestion HTTP status {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError(f"Ingestion request failed: {exc.reason}") from exc
    if len(body) > 65_536:
        raise RuntimeError("Ingestion response is too large")
    try:
        accepted = json.loads(body)["accepted"]
    except (ValueError, KeyError, TypeError) as exc:
        raise RuntimeError("Invalid ingestion acknowledgement") from exc
    expected = [event["event_id"] for event in batch]
    if accepted != expected:
        raise RuntimeError("Server acknowledgement does not match batch")


class Gateway:
    def __init__(self, serial_port: str, baudrate: int = 115200) -> None:
        self.gateway_id = _required_env("GATEWAY_ID")
        self.cloud_url = _required_env("CLOUD_URL")
        self.token = _required_env("GATEWAY_TOKEN")
        outbox_path = _required_env("OUTBOX_PATH")
        endpoint = urlsplit(self.cloud_url)
        if (
            not self.gateway_id
            or len(self.gateway_id) > 64
            or not all(char.isascii() and (char.isalnum() or char in "_-") for char in self.gateway_id)
        ):
            raise RuntimeError("Invalid gateway ID")
        if (
            endpoint.scheme != "https"
            or not endpoint.hostname
            or endpoint.username
            or endpoint.password
        ):
            raise RuntimeError("Invalid HTTPS ingestion URL")
        if len(self.token) < 32 or not self.token.isascii() or any(char.isspace() for char in self.token):
            raise RuntimeError("Invalid gateway token")
        if baudrate != 115200:
            raise RuntimeError("STM32 UART4 requires a baudrate of 115200")

        self.serial_port = serial_port
        self.baudrate = baudrate
        self._serial: serial.Serial | None = None
        self._reconnect_at = 0.0
        self._framer = Framer()
        self._session_id = secrets.token_hex(16)
        self._last_seq: int | None = None
        self._outbox = Outbox(outbox_path)
        self._stop = threading.Event()
        self._worker = threading.Thread(target=self._forward, name="cloud-forwarder", daemon=True)
        self._worker.start()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            if self._serial is None:
                self._open_serial()
                self._stop.wait(max(0.1, min(1.0, self._reconnect_at - time.monotonic())))
                continue
            try:
                data = self._serial.read(4096)
            except (serial.SerialException, OSError) as exc:
                log.error("Serial disconnected: %s; retry in 5 seconds", exc)
                self._disconnect()
                continue
            if not data:
                continue
            lines, discarded = self._framer.feed(data)
            for _ in range(discarded):
                log.error("Malformed or oversized STM32 frame discarded")
            for line in lines:
                self._accept_line(line)

    def _open_serial(self) -> None:
        if time.monotonic() < self._reconnect_at:
            return
        try:
            self._serial = serial.Serial(
                self.serial_port,
                self.baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=0.1,
                xonxoff=False,
                rtscts=False,
                exclusive=True,
            )
        except (serial.SerialException, OSError) as exc:
            self._reconnect_at = time.monotonic() + 5
            log.error("Cannot open %s; retry in 5 seconds: %s", self.serial_port, exc)
            if getattr(exc, "errno", None) in (errno.EACCES, errno.EBUSY) or "Permission denied" in str(exc):
                log.error(
                    "%s is busy or not accessible: a serial login console probably owns it. "
                    "Remove 'console=serial0,115200' from the kernel cmdline "
                    "(/boot/firmware/current/cmdline.txt on Ubuntu) and run "
                    "'sudo systemctl mask --now serial-getty@%s.service'",
                    self.serial_port,
                    os.path.basename(self.serial_port),
                )
            return
        try:
            self._serial.reset_input_buffer()
        except (serial.SerialException, OSError):
            pass
        self._framer = Framer()
        self._session_id = secrets.token_hex(16)
        self._last_seq = None
        log.info("Connected to STM32 on %s at %s baud", self.serial_port, self.baudrate)

    def _disconnect(self) -> None:
        if self._serial is not None:
            try:
                self._serial.close()
            except (serial.SerialException, OSError) as exc:
                log.warning("Error closing serial port: %s", exc)
            self._serial = None
        self._reconnect_at = time.monotonic() + 5

    def _accept_line(self, line: str) -> None:
        try:
            frame = parse_frame(line)
        except (UnicodeError, ValueError, TypeError) as exc:
            log.error("Invalid STM32 frame: %s", exc)
            return
        if "event" in frame:
            self._session_id = secrets.token_hex(16)
            self._last_seq = None
            log.info("STM32 boot: new acquisition session")
            return

        seq = frame["seq"]
        if self._last_seq is not None and seq <= self._last_seq:
            self._session_id = secrets.token_hex(16)
            log.warning("Sequence reset/wrap: new acquisition session")
        elif self._last_seq is not None and seq != self._last_seq + 1:
            log.warning("STM32 sequence gap: %s -> %s", self._last_seq, seq)
        self._last_seq = seq
        event = {
            "event_id": f"{self.gateway_id}:{self._session_id}:{seq}",
            "gateway_id": self.gateway_id,
            "session_id": self._session_id,
            "received_at": _timestamp(),
            "frame": frame,
        }
        try:
            self._outbox.push(event)
        except OutboxFullError:
            log.critical("SQLite outbox full; stopping acquisition without overwriting samples")
            self._stop.set()
            raise
        except sqlite3.Error:
            log.exception("Cannot persist telemetry; stopping acquisition")
            self._stop.set()
            raise
        log.debug("Persisted %s", event["event_id"])

    def _forward(self) -> None:
        delay = 1
        while not self._stop.is_set():
            try:
                batch = self._outbox.batch()
                if batch:
                    _upload(self.cloud_url, self.token, batch)
                    self._outbox.acknowledge([event["event_id"] for event in batch])
                delay = 1
            except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
                log.error("Cloud forwarding failed; records retained: %s", exc)
                delay = min(delay * 2, 60)
            self._stop.wait(delay)

    def close(self) -> None:
        self._stop.set()
        self._worker.join(timeout=20)
        self._disconnect()
        self._outbox.close()


def main(args: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read STM32 UART4 telemetry and forward it over HTTPS.")
    parser.add_argument("--serial-port", default=os.environ.get("SERIAL_PORT", "/dev/ttyAMA0"))
    parser.add_argument("--baudrate", type=int, default=115200)
    options = parser.parse_args(args)
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    try:
        gateway = Gateway(options.serial_port, options.baudrate)
    except (OSError, RuntimeError, sqlite3.Error) as exc:
        log.critical("Gateway initialization failed: %s", exc)
        return 1

    signal.signal(signal.SIGINT, lambda _signum, _frame: gateway.stop())
    signal.signal(signal.SIGTERM, lambda _signum, _frame: gateway.stop())
    try:
        gateway.run()
    except (OSError, RuntimeError, sqlite3.Error) as exc:
        log.critical("Gateway stopped after a fatal error: %s", exc)
        return 1
    finally:
        gateway.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
