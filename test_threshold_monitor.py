"""Проверка: нет спама при плато и дребезге у порога."""

from app_config import ThresholdRule, ThresholdsConfig
from threshold_monitor import ThresholdMonitor


def _monitor(high=None, low=None, hysteresis=0.5):
    return ThresholdMonitor(
        ThresholdsConfig(
            high=high or [],
            low=low or [],
            hysteresis=hysteresis,
        )
    )


def test_no_alert_without_previous_reading():
    m = _monitor(high=[ThresholdRule(27, "hot")])
    assert m.check(28.0) == []


def test_single_alert_on_cross_up():
    m = _monitor(high=[ThresholdRule(27, "hot")])
    assert m.check(26.0) == []
    alerts = m.check(28.0)
    assert len(alerts) == 1
    assert alerts[0].message == "hot"


def test_no_spam_while_above_plateau():
    m = _monitor(high=[ThresholdRule(27, "hot")])
    m.check(26.0)
    m.check(28.0)
    for _ in range(20):
        assert m.check(29.5) == []
        assert m.check(30.0) == []


def test_no_spam_on_duplicate_readings():
    m = _monitor(high=[ThresholdRule(27, "hot")])
    m.check(26.0)
    m.check(28.0)
    for _ in range(10):
        assert m.check(28.0) == []


def test_no_spam_on_boundary_jitter_with_hysteresis():
    """Колебания 27.1 / 26.9 у порога 27 — без повторов при hysteresis 0.5."""
    m = _monitor(high=[ThresholdRule(27, "hot")], hysteresis=0.5)
    m.check(26.0)
    assert len(m.check(27.2)) == 1
    for _ in range(10):
        m.check(27.1)
        assert m.check(26.9) == []
        assert m.check(27.2) == []


def test_realert_after_clearing_hysteresis():
    m = _monitor(high=[ThresholdRule(27, "hot")], hysteresis=0.5)
    m.check(26.0)
    m.check(28.0)
    m.check(26.4)  # ниже 27 - 0.5 → сброс
    assert len(m.check(27.5)) == 1


def test_low_plateau_no_spam():
    m = _monitor(low=[ThresholdRule(18, "cold")])
    m.check(20.0)
    m.check(17.0)
    for _ in range(10):
        assert m.check(15.0) == []


def test_two_high_thresholds_one_crossing_each():
    m = _monitor(
        high=[
            ThresholdRule(27, "warm"),
            ThresholdRule(32, "hot"),
        ]
    )
    m.check(26.0)
    assert len(m.check(28.0)) == 1
    assert m.check(30.0) == []
    assert len(m.check(33.0)) == 1


def main() -> None:
    test_no_alert_without_previous_reading()
    test_single_alert_on_cross_up()
    test_no_spam_while_above_plateau()
    test_no_spam_on_duplicate_readings()
    test_no_spam_on_boundary_jitter_with_hysteresis()
    test_realert_after_clearing_hysteresis()
    test_low_plateau_no_spam()
    test_two_high_thresholds_one_crossing_each()
    print("Все проверки anti-spam пройдены.")


if __name__ == "__main__":
    main()
