import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.fakesys import FakeSysfs
from ugreen_fan.config import Config, FanSpec, Source
from ugreen_fan import daemon
from ugreen_fan.daemon import Regulator, StallGuard, State, read_state, run, write_state
from ugreen_fan.hwmon import Fan, SensorError

DISK_CURVE = ((40.0, 100), (50.0, 200))
CPU_CURVE = ((60.0, 51), (90.0, 255))


def make_config(min_pwm: int = 51, fans: tuple[FanSpec, ...] = (FanSpec(3, 3, ("disks", "cpu")),)) -> Config:
    return Config(
        supported_models=("DXP4800",), module_params="", chip="it8613", fans=fans,
        interval=10, hysteresis=2, min_pwm=min_pwm, truenas_alert=True,
        sources=(
            Source("disks", "drivetemp", 1, (1.0, 80.0), DISK_CURVE),
            Source("cpu", "it8613", 1, (1.0, 110.0), CPU_CURVE),
        ),
    )


def duty(state: State, pwm: int = 3) -> int:
    return state.fans[f"pwm{pwm}"]["pwm"]


class RegulatorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sys = FakeSysfs(self.root)
        self.chip = self.sys.add("it8613", {"pwm3": 51, "pwm3_enable": 2, "fan3_input": 900,
                                            "temp1_input": 50000})
        self.sda = self.sys.add("drivetemp", {"temp1_input": 45000}, block="sda", port="ata1")
        self.fan = Fan(self.chip, 3, 3)
        self.regulator = Regulator(make_config(), [self.fan], self.root, clock=lambda: 1000.0)
        module = self.root / "module" / "drivetemp"
        module.mkdir(parents=True)
        patcher = mock.patch("ugreen_fan.readings.DRIVETEMP_MODULE", module)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def set_disk(self, celsius: float):
        (self.sda / "temp1_input").write_text(f"{int(celsius * 1000)}\n")

    def pwm(self) -> str:
        return self.sys.read(self.chip, "pwm3")

    def test_first_step_starts_from_failsafe_and_uses_shifted_curve(self):
        state = self.regulator.step()
        self.assertEqual(state.mode, "normal")
        self.assertEqual(state.fans, {"pwm3": {"fan": 3, "pwm": 170, "rpm": 900}})  # disks: curve(45 + 2)
        self.assertEqual(self.pwm(), "170")
        self.assertEqual(self.sys.read(self.chip, "pwm3_enable"), "1")
        self.assertEqual(state.temps, {"bay1": 45.0, "it8613/temp1": 50.0})
        self.assertEqual(state.updated, 1000.0)

    def test_hotter_source_wins(self):
        (self.chip / "temp1_input").write_text("90000\n")
        self.assertEqual(duty(self.regulator.step()), 255)

    def test_min_pwm_floor(self):
        self.set_disk(30)
        regulator = Regulator(make_config(min_pwm=120), [self.fan], self.root)
        regulator.step()
        self.assertEqual(duty(regulator.step()), 120)

    def test_hysteresis_holds_then_falls(self):
        self.regulator.step()                       # 170
        self.set_disk(45)
        self.assertEqual(duty(self.regulator.step()), 170)
        self.set_disk(42)
        self.assertEqual(duty(self.regulator.step()), 140)

    def test_unreadable_disk_goes_failsafe(self):
        self.regulator.step()
        (self.sda / "temp1_input").unlink()
        state = self.regulator.step()
        self.assertEqual((state.mode, duty(state), self.pwm()), ("failsafe", 255, "255"))
        self.assertIn("temp1_input", state.reason)

    def test_invalid_reading_goes_failsafe(self):
        self.set_disk(0)
        self.assertEqual(self.regulator.step().mode, "failsafe")
        self.assertEqual(self.pwm(), "255")

    def test_recovers_after_failsafe(self):
        self.set_disk(0)
        self.regulator.step()
        self.set_disk(45)
        state = self.regulator.step()
        self.assertEqual((state.mode, duty(state)), ("normal", 170))

    def test_no_disks_follows_cpu_curve(self):
        self.regulator.step()
        for entry in (self.sda / "device" / "block" / "sda", self.sda / "device" / "block"):
            entry.rmdir()
        (self.sda / "device").unlink()
        (self.chip / "temp1_input").write_text("75000\n")
        state = self.regulator.step()
        self.assertEqual((state.mode, state.temps), ("normal", {"it8613/temp1": 75.0}))
        self.assertEqual(duty(state), 153)  # cpu rises: curve(75); disks add nothing

    def test_missing_tachometer_is_not_fatal(self):
        (self.chip / "fan3_input").unlink()
        state = self.regulator.step()
        self.assertEqual((state.mode, state.fans["pwm3"]["rpm"]), ("normal", None))



class MultiFanRegulatorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sys = FakeSysfs(self.root)
        self.chip = self.sys.add("it8613", {"pwm2": 51, "pwm2_enable": 2, "fan2_input": 840,
                                            "pwm3": 51, "pwm3_enable": 2, "fan3_input": 845,
                                            "temp1_input": 75000})
        self.sda = self.sys.add("drivetemp", {"temp1_input": 45000}, block="sda", port="ata1")
        module = self.root / "module" / "drivetemp"
        module.mkdir(parents=True)
        patcher = mock.patch("ugreen_fan.readings.DRIVETEMP_MODULE", module)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.fans = [Fan(self.chip, 2, 2), Fan(self.chip, 3, 3)]

    def regulator(self, *fans: FanSpec) -> Regulator:
        return Regulator(make_config(fans=fans), self.fans, self.root, clock=lambda: 5.0)

    def test_each_fan_follows_its_own_sources(self):
        state = self.regulator(FanSpec(2, 2, ("cpu",)), FanSpec(3, 3, ("disks",))).step()
        self.assertEqual(state.fans, {"pwm2": {"fan": 2, "pwm": 167, "rpm": 840},   # cpu: curve(75 + 2)
                                      "pwm3": {"fan": 3, "pwm": 170, "rpm": 845}})  # disks: curve(45 + 2)
        self.assertEqual((self.sys.read(self.chip, "pwm2"), self.sys.read(self.chip, "pwm3")), ("167", "170"))
        self.assertEqual((self.sys.read(self.chip, "pwm2_enable"), self.sys.read(self.chip, "pwm3_enable")),
                         ("1", "1"))

    def test_fan_with_every_source_takes_the_hottest(self):
        state = self.regulator(FanSpec(2, 2, ("disks", "cpu")), FanSpec(3, 3, ("disks",))).step()
        self.assertEqual((duty(state, 2), duty(state, 3)), (170, 170))

    def test_min_pwm_applies_per_fan(self):
        self.set_cpu(30)
        regulator = self.regulator(FanSpec(2, 2, ("cpu",)), FanSpec(3, 3, ("disks",)))
        regulator.step()
        state = regulator.step()
        self.assertEqual((duty(state, 2), duty(state, 3)), (51, 170))

    def test_sensor_error_sends_every_fan_to_failsafe(self):
        regulator = self.regulator(FanSpec(2, 2, ("cpu",)), FanSpec(3, 3, ("disks",)))
        regulator.step()
        (self.sda / "temp1_input").write_text("0\n")
        state = regulator.step()
        self.assertEqual(state.mode, "failsafe")
        self.assertEqual({name: fan["pwm"] for name, fan in state.fans.items()}, {"pwm2": 255, "pwm3": 255})
        self.assertEqual((self.sys.read(self.chip, "pwm2"), self.sys.read(self.chip, "pwm3")), ("255", "255"))

    def test_failsafe_reaches_the_other_fans_when_one_write_fails(self):
        broken = Fan(self.root / "missing", 2, 2)
        regulator = Regulator(make_config(fans=(FanSpec(2, 2, ("cpu",)), FanSpec(3, 3, ("cpu",)))),
                              [broken, self.fans[1]], self.root)
        (self.chip / "temp1_input").unlink()
        with self.assertRaises(SensorError):
            regulator.step()
        self.assertEqual(self.sys.read(self.chip, "pwm3"), "255")

    def set_cpu(self, celsius: float):
        (self.chip / "temp1_input").write_text(f"{int(celsius * 1000)}\n")


class StateFileTest(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sub" / "state.json"
            fans = {"pwm2": {"fan": 2, "pwm": 128, "rpm": 1000}, "pwm3": {"fan": 3, "pwm": 90, "rpm": None}}
            write_state(State("normal", None, fans, {"sda": 45.0}, 1.5), path)
            self.assertEqual(read_state(path), {"mode": "normal", "reason": None, "fans": fans,
                                                "temps": {"sda": 45.0}, "updated": 1.5})

    def test_missing_file(self):
        self.assertIsNone(read_state(Path("/nonexistent/state.json")))

    def test_corrupt_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("{")
            self.assertIsNone(read_state(path))


class StallGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sys = FakeSysfs(Path(self.tmp.name))
        self.chip = self.sys.add("it8613", {"pwm3": 100, "pwm3_enable": 1, "pwm2": 100, "pwm2_enable": 1})
        self.now = 0.0
        self.guard = StallGuard([Fan(self.chip, 2, 2), Fan(self.chip, 3, 3)], limit=20, clock=lambda: self.now)

    def tearDown(self):
        self.tmp.cleanup()

    def test_quiet_while_steps_complete(self):
        self.now = 19
        self.assertFalse(self.guard.poll())
        self.assertEqual(self.sys.read(self.chip, "pwm3"), "100")

    def test_trips_once_when_step_hangs(self):
        self.now = 21
        self.assertTrue(self.guard.poll())
        self.assertEqual(self.sys.read(self.chip, "pwm3"), "255")
        self.assertEqual(self.sys.read(self.chip, "pwm2"), "255")
        (self.chip / "pwm3").write_text("100\n")
        self.now = 40
        self.assertFalse(self.guard.poll())
        self.assertEqual(self.sys.read(self.chip, "pwm3"), "100")

    def test_beat_rearms(self):
        self.now = 21
        self.guard.poll()
        self.guard.beat()
        self.now = 30
        self.assertFalse(self.guard.poll())
        self.now = 42
        self.assertTrue(self.guard.poll())


class RunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sys = FakeSysfs(self.root)
        self.chip = self.sys.add("it8613", {"pwm2": 51, "pwm2_enable": 2, "pwm3": 51, "pwm3_enable": 2,
                                            "temp1_input": 50000})
        module = self.root / "module" / "drivetemp"
        module.mkdir(parents=True)
        patcher = mock.patch("ugreen_fan.readings.DRIVETEMP_MODULE", module)
        patcher.start()
        self.addCleanup(patcher.stop)
        # no signal handlers, stall thread or systemd socket in the test process
        for target in ("signal.signal", "threading.Thread", "sd_notify"):
            patcher = mock.patch(f"ugreen_fan.daemon.{target}")
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_every_fan_left_at_failsafe_when_the_loop_dies(self):
        config = make_config(fans=(FanSpec(2, 2, ("cpu",)), FanSpec(3, 3, ("cpu",))))
        with mock.patch.object(daemon.Regulator, "step", side_effect=RuntimeError("boom")), \
             self.assertRaises(RuntimeError):
            run(config, self.root / "state.json", self.root)
        self.assertEqual([self.sys.read(self.chip, f) for f in ("pwm2", "pwm2_enable", "pwm3", "pwm3_enable")],
                         ["255", "1", "255", "1"])

    def test_regulates_every_configured_fan(self):
        config = make_config(fans=(FanSpec(2, 2, ("cpu",)), FanSpec(3, 3, ("cpu",))))
        state_path = self.root / "state.json"
        with mock.patch.object(daemon, "write_state", side_effect=RuntimeError("stop")) as write, \
             self.assertRaises(RuntimeError):
            run(config, state_path, self.root)
        state = write.call_args.args[0]
        self.assertEqual({name: fan["pwm"] for name, fan in state.fans.items()}, {"pwm2": 51, "pwm3": 51})
