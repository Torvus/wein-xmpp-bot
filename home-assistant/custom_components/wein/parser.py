"""Parsers for Wein BLE GATT payloads."""

from __future__ import annotations

import struct


def parse_temperature(data: bytes | bytearray) -> float | None:
    raw = bytes(data)
    if len(raw) == 0:
        return None
    if len(raw) == 2:
        return int.from_bytes(raw, "little", signed=True) / 100.0
    if len(raw) == 4:
        return struct.unpack("<f", raw)[0]
    try:
        text = raw.decode("utf-8").strip().replace(",", ".")
        return float(text)
    except (UnicodeDecodeError, ValueError):
        return None


def parse_boolean(data: bytes | bytearray) -> bool | None:
    try:
        text = bytes(data).decode("utf-8").strip().lower()
    except UnicodeDecodeError:
        return None
    if text == "true":
        return True
    if text == "false":
        return False
    return None


def normalize_uuid(uuid: str) -> str:
    return uuid.lower().replace("-", "")
