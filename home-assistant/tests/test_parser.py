"""Parser tests for the Wein Home Assistant integration."""

from __future__ import annotations

import importlib.util
import struct
import unittest
from pathlib import Path


def _load_parser():
    path = (
        Path(__file__).resolve().parents[1]
        / "custom_components"
        / "wein"
        / "parser.py"
    )
    spec = importlib.util.spec_from_file_location("wein_parser", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ParserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.parser = _load_parser()

    def test_temperature_int16(self) -> None:
        raw = int(2512).to_bytes(2, "little", signed=True)
        self.assertAlmostEqual(self.parser.parse_temperature(raw), 25.12)

    def test_temperature_float32(self) -> None:
        raw = struct.pack("<f", 21.5)
        self.assertAlmostEqual(self.parser.parse_temperature(raw), 21.5, places=4)

    def test_temperature_text(self) -> None:
        self.assertEqual(self.parser.parse_temperature(b"23.45"), 23.45)

    def test_valve_boolean(self) -> None:
        self.assertTrue(self.parser.parse_boolean(b"true"))
        self.assertFalse(self.parser.parse_boolean(b"false"))
        self.assertIsNone(self.parser.parse_boolean(b"maybe"))


if __name__ == "__main__":
    unittest.main()
