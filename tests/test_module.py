import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.fakesys import FakeSysfs
from ugreen_fan.config import Config, FanSpec, Source
from ugreen_fan.hwmon import Fan
from ugreen_fan import module
from ugreen_fan.module import (SetupError, check_model, insert_module, is_loaded, module_file,
                               read_bios_pwm, read_model, restore, save_bios_pwm, unit_text)


class ModuleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name: str, text: str) -> Path:
        path = self.dir / name
        path.write_text(text)
        return path

    def test_supported_model(self):
        dmi = self.write("product_name", "DXP4800\n")
        self.assertEqual(check_model(("DXP4800",), False, dmi), "DXP4800")

    def test_read_model(self):
        self.assertEqual(read_model(self.write("product_name", "DXP4800 Pro\n")), "DXP4800 Pro")

    def test_unsupported_model(self):
        dmi = self.write("product_name", "DXP8800 Plus\n")
        with self.assertRaisesRegex(SetupError, "--force"):
            check_model(("DXP4800",), False, dmi)

    def test_unsupported_model_forced(self):
        dmi = self.write("product_name", "DXP8800 Plus\n")
        self.assertEqual(check_model(("DXP4800",), True, dmi), "DXP8800 Plus")

    def test_module_file_per_kernel(self):
        self.assertEqual(module_file(Path("/repo"), "6.12.33-production+truenas"),
                         Path("/repo/modules/6.12.33-production+truenas/it87.ko"))

    def test_is_loaded(self):
        modules = self.write("modules", "drivetemp 16384 0 - Live 0x0\nit87 65536 0 - Live 0x0\n")
        self.assertTrue(is_loaded(modules))

    def test_is_not_loaded_prefix_match(self):
        modules = self.write("modules", "it87_wdt 16384 0 - Live 0x0\n")
        self.assertFalse(is_loaded(modules))

    def test_unit_failsafe_does_not_need_config(self):
        text = unit_text(Path("/mnt/tank/apps/ugreen-fan"), "it8613", [3])
        self.assertIn('ExecStart="/mnt/tank/apps/ugreen-fan/bin/ugreen-fan" run --chip it8613 --pwm 3\n', text)
        self.assertIn('ExecStopPost="/mnt/tank/apps/ugreen-fan/bin/ugreen-fan" failsafe --chip it8613 --pwm 3\n', text)
        for line in ("Type=notify", "Restart=always", "WatchdogSec=30"):
            self.assertIn(line, text)

    def test_unit_lists_every_fan(self):
        text = unit_text(Path("/repo"), "it8613", [2, 3])
        self.assertIn('ExecStart="/repo/bin/ugreen-fan" run --chip it8613 --pwm 2 --pwm 3\n', text)
        self.assertIn('ExecStopPost="/repo/bin/ugreen-fan" failsafe --chip it8613 --pwm 2 --pwm 3\n', text)

    def fans(self, files: dict) -> tuple[FakeSysfs, Path]:
        sys = FakeSysfs(self.dir / "hwmon")
        return sys, sys.add("it8613", files)

    def test_save_bios_pwm_only_in_auto_mode_and_once(self):
        _, chip = self.fans({"pwm2": 51, "pwm2_enable": 2, "pwm3": 51, "pwm3_enable": 2})
        fans = [Fan(chip, 2, 2), Fan(chip, 3, 3)]
        saved = self.dir / "run" / "bios_pwm"
        save_bios_pwm(fans, saved)
        self.assertEqual(json.loads(saved.read_text()), {"2": 51, "3": 51})
        for fan in fans:
            fan.set_manual(200)
        save_bios_pwm(fans, saved)
        self.assertEqual(json.loads(saved.read_text()), {"2": 51, "3": 51})

    def test_save_bios_pwm_skips_manual_mode(self):
        _, chip = self.fans({"pwm3": 200, "pwm3_enable": 1})
        saved = self.dir / "bios_pwm"
        save_bios_pwm([Fan(chip, 3, 3)], saved)
        self.assertFalse(saved.exists())

    def test_save_bios_pwm_adds_missing_fan_without_overwriting(self):
        _, chip = self.fans({"pwm2": 60, "pwm2_enable": 2, "pwm3": 70, "pwm3_enable": 2})
        saved = self.write("bios_pwm", '{"3": 51}\n')
        save_bios_pwm([Fan(chip, 2, 2), Fan(chip, 3, 3)], saved)
        self.assertEqual(json.loads(saved.read_text()), {"2": 60, "3": 51})

    def test_save_bios_pwm_keeps_legacy_file(self):
        _, chip = self.fans({"pwm2": 60, "pwm2_enable": 2, "pwm3": 70, "pwm3_enable": 2})
        saved = self.write("bios_pwm", "51\n")
        save_bios_pwm([Fan(chip, 2, 2), Fan(chip, 3, 3)], saved)
        self.assertEqual(saved.read_text(), "51\n")

    def test_read_bios_pwm(self):
        self.assertEqual(read_bios_pwm([2, 3], self.dir / "missing"), {})
        self.assertEqual(read_bios_pwm([2, 3], self.write("json", '{"2": 51, "3": 52}')), {2: 51, 3: 52})
        self.assertEqual(read_bios_pwm([2, 3], self.write("legacy", "51\n")), {2: 51, 3: 51})

    def test_read_bios_pwm_corrupt(self):
        for text in ("{", "[51]", '{"2": "x"}'):
            with self.subTest(text), self.assertRaisesRegex(SetupError, "reboot"):
                read_bios_pwm([2], self.write("bios_pwm", text))


def make_config(*pwms: int) -> Config:
    return Config(
        supported_models=("DXP4800 Pro",), module_params="", chip="it8613",
        fans=tuple(FanSpec(pwm, pwm, ("cpu",)) for pwm in pwms),
        interval=10, hysteresis=2, min_pwm=51, truenas_alert=True,
        sources=(Source("cpu", "it8613", 1, (1.0, 110.0), ((60.0, 51), (90.0, 255))),),
    )


class LoadTest(unittest.TestCase):
    def test_saves_every_fan_and_writes_unit_for_every_pwm(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            ko = module_file(repo, "6.12.105-production+truenas")
            ko.parent.mkdir(parents=True)
            ko.write_text("")
            chip = FakeSysfs(Path(tmp) / "hwmon").add(
                "it8613", {"pwm2": 51, "pwm2_enable": 2, "pwm3": 52, "pwm3_enable": 2})
            unit, bios_pwm = Path(tmp) / "unit", Path(tmp) / "bios_pwm"
            uname = mock.Mock(release="6.12.105-production+truenas")
            with mock.patch.object(module, "check_model", return_value="DXP4800 Pro"), \
                 mock.patch.object(module.os, "uname", return_value=uname), \
                 mock.patch.object(module, "is_loaded", return_value=True), \
                 mock.patch.object(module, "_wait_for_chip", return_value=chip), \
                 mock.patch.object(module, "UNIT_PATH", unit), \
                 mock.patch.object(module, "BIOS_PWM_FILE", bios_pwm), \
                 mock.patch.object(module, "_run") as run:
                module.load(make_config(2, 3), repo, False)
            self.assertEqual(json.loads(bios_pwm.read_text()), {"2": 51, "3": 52})
            self.assertIn("run --chip it8613 --pwm 2 --pwm 3\n", unit.read_text())
            self.assertIn(mock.call("systemctl", "restart", "ugreen-fan.service"), run.call_args_list)


class RestoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.sys = FakeSysfs(self.dir / "hwmon")
        self.chip = self.sys.add("it8613", {"pwm2": 255, "pwm2_enable": 1, "pwm3": 255, "pwm3_enable": 1})
        self.bios_pwm = self.dir / "bios_pwm"
        self.unit = self.dir / "ugreen-fan.service"
        self.unit.write_text("[Unit]\n")
        for name, value in (("BIOS_PWM_FILE", self.bios_pwm), ("UNIT_PATH", self.unit)):
            patcher = mock.patch.object(module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name, value in (("is_loaded", True), ("find_chip", self.chip)):
            patcher = mock.patch.object(module, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(module, "_run")
        self.run = patcher.start()
        self.addCleanup(patcher.stop)

    def read(self, file: str) -> str:
        return self.sys.read(self.chip, file)

    def test_hands_every_fan_back(self):
        self.bios_pwm.write_text('{"2": 51, "3": 52}\n')
        restore(make_config(2, 3))
        self.assertEqual([self.read(f) for f in ("pwm2", "pwm2_enable", "pwm3", "pwm3_enable")],
                         ["51", "2", "52", "2"])
        self.assertIn(mock.call("rmmod", "it87"), self.run.call_args_list)
        self.assertFalse(self.bios_pwm.exists())
        self.assertFalse(self.unit.exists())

    def test_legacy_file_applies_to_every_fan(self):
        self.bios_pwm.write_text("51\n")
        restore(make_config(2, 3))
        self.assertEqual([self.read(f) for f in ("pwm2", "pwm2_enable", "pwm3", "pwm3_enable")],
                         ["51", "2", "51", "2"])

    def test_fan_without_saved_value_changes_nothing(self):
        self.bios_pwm.write_text('{"3": 51}\n')
        with self.assertRaisesRegex(SetupError, "no start PWM for pwm2.*reboot to let the BIOS re-initialise"):
            restore(make_config(2, 3))
        self.run.assert_not_called()
        self.assertTrue(self.unit.exists())
        self.assertEqual((self.read("pwm2_enable"), self.read("pwm3_enable")), ("1", "1"))

    def test_missing_file_changes_nothing(self):
        with self.assertRaisesRegex(SetupError, "is missing.*reboot to let the BIOS re-initialise"):
            restore(make_config(2, 3))
        self.run.assert_not_called()
        self.assertTrue(self.unit.exists())

    def test_module_not_loaded_only_removes_the_unit(self):
        with mock.patch.object(module, "is_loaded", return_value=False):
            restore(make_config(2, 3))
        self.assertEqual(self.run.call_args_list, [mock.call("systemctl", "stop", "ugreen-fan.service"),
                                                   mock.call("systemctl", "daemon-reload")])
        self.assertFalse(self.unit.exists())


class InsertModuleTest(unittest.TestCase):
    def test_loads_dependencies_before_insmod(self):
        # insmod does not resolve dependencies: it87 needs hwmon-vid (found after a reboot)
        with mock.patch.object(module, "_output", return_value="hwmon-vid\n") as output, \
             mock.patch.object(module, "_run") as run:
            insert_module(Path("/repo/it87.ko"), "ignore_resource_conflict=1")
        output.assert_called_once_with("modinfo", "-F", "depends", "/repo/it87.ko")
        self.assertEqual(run.call_args_list, [
            mock.call("modprobe", "hwmon-vid"),
            mock.call("insmod", "/repo/it87.ko", "ignore_resource_conflict=1"),
        ])

    def test_no_dependencies(self):
        with mock.patch.object(module, "_output", return_value="\n"), \
             mock.patch.object(module, "_run") as run:
            insert_module(Path("/repo/it87.ko"), "")
        self.assertEqual(run.call_args_list, [mock.call("insmod", "/repo/it87.ko")])
