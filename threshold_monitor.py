"""Пороги температуры: одно оповещение на пересечение, без повторов на плато."""

from __future__ import annotations

from dataclasses import dataclass, field

from app_config import ThresholdRule, ThresholdsConfig


@dataclass
class ThresholdAlert:
    """Текст оповещения целиком из config.json (поле message у порога)."""

    message: str


@dataclass
class ThresholdMonitor:
    """
    Срабатывание только в момент пересечения порога.

    Пока температура остаётся по «горячую» сторону верхнего порога (или по
    «холодную» для нижнего), повторных сообщений нет. Повтор возможен только
    после отхода на hysteresis °C ниже/выше порога и нового пересечения —
    это отсекает дребезг у границы.
    """

    thresholds: ThresholdsConfig
    _last_temperature: float | None = field(default=None, init=False)
    _high_armed: set[float] = field(default_factory=set, init=False)
    _low_armed: set[float] = field(default_factory=set, init=False)

    def update_thresholds(self, thresholds: ThresholdsConfig) -> None:
        self.thresholds = thresholds
        self._high_armed.clear()
        self._low_armed.clear()

    def check(self, temperature: float) -> list[ThresholdAlert]:
        alerts: list[ThresholdAlert] = []
        previous = self._last_temperature
        h = self.thresholds.hysteresis

        for rule in self.thresholds.high:
            if previous is not None:
                crossed_up = previous < rule.value <= temperature
                if crossed_up and rule.value not in self._high_armed:
                    self._high_armed.add(rule.value)
                    alerts.append(ThresholdAlert(rule.message))
            if temperature < rule.value - h:
                self._high_armed.discard(rule.value)

        for rule in self.thresholds.low:
            if previous is not None:
                crossed_down = previous > rule.value >= temperature
                if crossed_down and rule.value not in self._low_armed:
                    self._low_armed.add(rule.value)
                    alerts.append(ThresholdAlert(rule.message))
            if temperature > rule.value + h:
                self._low_armed.discard(rule.value)

        self._last_temperature = temperature
        return alerts
