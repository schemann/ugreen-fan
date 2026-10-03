import unittest

from ugreen_fan.health import diagnose

FANS = {"pwm2": {"fan": 2, "pwm": 128, "rpm": 1500}, "pwm3": {"fan": 3, "pwm": 128, "rpm": 1000}}
HEALTHY = {"mode": "normal", "reason": None, "fans": FANS, "temps": {}, "updated": 100.0}


def with_fan(name: str, **values) -> dict:
    return dict(HEALTHY, fans=dict(FANS, **{name: dict(FANS[name], **values)}))


def check(**overrides):
    args = dict(module_loaded=True, service_active=True, state=HEALTHY, pwm_enable={2: 1, 3: 1},
                now=105.0, interval=10)
    args.update(overrides)
    return diagnose(**args)


class DiagnoseTest(unittest.TestCase):
    def test_healthy(self):
        self.assertIsNone(check())

    def test_module_not_loaded(self):
        self.assertIn("build.sh", check(module_loaded=False))

    def test_service_down(self):
        self.assertIn("not running", check(service_active=False))

    def test_state_missing(self):
        self.assertIn("state", check(state=None))

    def test_state_stale_message_is_stable(self):
        self.assertEqual(check(now=200.0), check(now=900.0))

    def test_failsafe_includes_reason(self):
        state = dict(HEALTHY, mode="failsafe", reason="no drivetemp devices found")
        self.assertIn("no drivetemp devices found", check(state=state))

    def test_not_manual(self):
        self.assertEqual(check(pwm_enable={2: 1, 3: 2}), "pwm3 is not under manual control (pwm_enable=2)")

    def test_unreadable_enable_is_not_manual(self):
        self.assertEqual(check(pwm_enable={2: None, 3: 1}),
                         "pwm2 is not under manual control (pwm_enable=None)")

    def test_full_speed_reads_as_enable_zero(self):
        # it87 reports manual mode with duty 255 as pwm_enable=0 ("full speed")
        self.assertIsNone(check(pwm_enable={2: 0, 3: 1}, state=with_fan("pwm2", pwm=255)))

    def test_fan_stalled(self):
        self.assertEqual(check(state=with_fan("pwm3", rpm=0)), "fan3 reports 0 RPM, check the fan")

    def test_unknown_rpm_is_not_a_stall(self):
        self.assertIsNone(check(state=with_fan("pwm2", rpm=None)))
