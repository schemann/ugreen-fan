import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.fakesys import FakeSysfs
from ugreen_fan.config import Config, FanSpec, I2cDevice, Source
from ugreen_fan.hwmon import Fan
from ugreen_fan import module
from ugreen_fan.module import (SetupError, check_model, insert_module, is_loaded, module_file, probe_i2c,
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

    def test_unreadable_model(self):
        with self.assertRaisesRegex(SetupError, "cannot read the model"):
            read_model(self.dir / "missing")

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

    def test_save_bios_pwm_upgrades_legacy_file(self):
        # pwm3 was regulated by the old version (manual), pwm2 is new and still on the BIOS curve
        _, chip = self.fans({"pwm2": 60, "pwm2_enable": 2, "pwm3": 200, "pwm3_enable": 1})
        saved = self.write("bios_pwm", "51\n")
        save_bios_pwm([Fan(chip, 2, 2), Fan(chip, 3, 3)], saved)
        self.assertEqual(json.loads(saved.read_text()), {"2": 60, "3": 51})

    def test_save_bios_pwm_leaves_corrupt_file_alone(self):
        _, chip = self.fans({"pwm2": 60, "pwm2_enable": 2})
        saved = self.write("bios_pwm", "{")
        with self.assertLogs("ugreen_fan.module", "WARNING"):
            save_bios_pwm([Fan(chip, 2, 2)], saved)
        self.assertEqual(saved.read_text(), "{")

    def test_read_bios_pwm(self):
        self.assertEqual(read_bios_pwm([2, 3], self.dir / "missing"), {})
        self.assertEqual(read_bios_pwm([2, 3], self.write("json", '{"2": 51, "3": 52}')), {2: 51, 3: 52})
        self.assertEqual(read_bios_pwm([2, 3], self.write("legacy", "51\n")), {2: 51, 3: 51})

    def test_read_bios_pwm_corrupt(self):
        for text in ("{", "[51]", '{"2": "x"}', "true", '{"2": true}', '{"2": 256}', '{"2": -1}', "300",
                     '{"x": 51}', '{"2": 51.5}'):
            with self.subTest(text), self.assertRaisesRegex(SetupError, "reboot"):
                read_bios_pwm([2], self.write("bios_pwm", text))


def make_config(*pwms: int, i2c_devices: tuple[I2cDevice, ...] = ()) -> Config:
    return Config(
        supported_models=("DXP4800 Pro",), module_params="", chip="it8613",
        fans=tuple(FanSpec(pwm, pwm, ("cpu",)) for pwm in pwms),
        interval=10, hysteresis=2, min_pwm=51, truenas_alert=True,
        sources=(Source("cpu", "it8613", 1, (1.0, 110.0), ((60.0, 51), (90.0, 255))),),
        i2c_devices=i2c_devices,
    )


SPD = I2cDevice("SMBus I801 adapter", "spd5118", (0x50, 0x52))


class ProbeI2cTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.add_bus(0, "SMBus I801 adapter at efa0")  # real name carries the I/O base
        self.writes: list[tuple[str, str]] = []
        self.bind: set[str] = set()      # device names that bind a driver once registered
        self.fail: set[str] = set()      # sysfs files whose write raises OSError
        for target, kwargs in ((module, dict(attribute="_write_sysfs", side_effect=self.write)),
                               (module, dict(attribute="_run")),
                               (module.time, dict(attribute="sleep"))):
            patcher = mock.patch.object(target, **kwargs)
            mocked = patcher.start()
            self.addCleanup(patcher.stop)
            setattr(self, kwargs["attribute"].strip("_"), mocked)

    def add_bus(self, number: int, name: str) -> None:
        bus = self.root / f"i2c-{number}"
        bus.mkdir()
        (bus / "name").write_text(f"{name}\n")

    def add_device(self, name: str, bound: bool = True) -> Path:
        device = self.root / name
        device.mkdir()
        if bound:
            (device / "driver").symlink_to(self.root)
        return device

    def write(self, path: Path, text: str) -> None:
        if path.name in self.fail:
            raise OSError("Invalid argument")
        self.writes.append((f"{path.parent.name}/{path.name}", text))
        if path.name == "new_device":
            name = f"{path.parent.name.removeprefix('i2c-')}-{int(text.split()[1], 16):04x}"
            device = self.add_device(name, bound=name in self.bind)
            self.created = device

    def probe(self, *devices: I2cDevice) -> None:
        probe_i2c(devices, self.root)

    def test_modprobes_the_driver(self):
        self.probe(SPD)
        self.run.assert_called_once_with("modprobe", "spd5118")

    def test_modprobe_failure_is_logged_and_probing_goes_on(self):
        self.run.side_effect = SetupError("modprobe spd5118 failed: not found")
        with self.assertLogs("ugreen_fan.module", "WARNING") as logs:
            self.probe(SPD)
        self.assertIn("modprobe spd5118 failed", logs.output[0])
        self.assertIn(("i2c-0/new_device", "spd5118 0x50\n"), self.writes)

    def test_skips_existing_device(self):
        self.add_device("0-0050")
        self.bind.add("0-0052")
        self.probe(SPD)
        self.assertEqual([w for w in self.writes if w[0] == "i2c-0/new_device"],
                         [("i2c-0/new_device", "spd5118 0x52\n")])

    def test_skips_existing_device_without_driver(self):
        # a device the kernel registered itself is not ours to remove
        self.add_device("0-0050", bound=False)
        self.probe(I2cDevice("SMBus I801 adapter", "spd5118", (0x50,)))
        self.assertEqual(self.writes, [])

    def test_keeps_a_device_that_binds(self):
        self.bind.add("0-0052")
        with self.assertLogs("ugreen_fan.module", "INFO") as logs:
            self.probe(I2cDevice("SMBus I801 adapter", "spd5118", (0x52,)))
        self.assertEqual(self.writes, [("i2c-0/new_device", "spd5118 0x52\n")])
        self.assertTrue((self.root / "0-0052").exists())
        self.assertTrue(any("Registered spd5118 at 0x52 on i2c-0" in line for line in logs.output))

    def test_deletes_a_device_that_does_not_bind(self):
        self.probe(I2cDevice("SMBus I801 adapter", "spd5118", (0x51,)))
        self.assertEqual(self.writes, [("i2c-0/new_device", "spd5118 0x51\n"),
                                       ("i2c-0/delete_device", "0x51\n")])
        self.assertEqual(self.sleep.call_count, module.BIND_POLLS)

    def test_binding_is_polled_not_awaited_in_full(self):
        def bind_on_third_poll(seconds):
            if self.sleep.call_count == 3:
                (self.created / "driver").symlink_to(self.root)
        self.sleep.side_effect = bind_on_third_poll
        self.probe(I2cDevice("SMBus I801 adapter", "spd5118", (0x52,)))
        self.assertEqual(self.sleep.call_count, 3)
        self.assertEqual([w[0] for w in self.writes], ["i2c-0/new_device"])

    def test_device_name_is_bus_and_four_lower_case_hex_digits(self):
        self.add_bus(3, "other")
        self.add_device("3-007f")
        self.probe(I2cDevice("other", "spd5118", (0x77, 0x7f)))
        self.assertEqual([w[0] for w in self.writes], ["i2c-3/new_device", "i2c-3/delete_device"])
        self.assertEqual(self.writes[0][1], "spd5118 0x77\n")

    def test_missing_adapter_logs_and_continues(self):
        self.bind.add("0-0052")
        missing = I2cDevice("No such adapter", "spd5118", (0x50,))
        with self.assertLogs("ugreen_fan.module", "WARNING") as logs:
            self.probe(missing, I2cDevice("SMBus I801 adapter", "spd5118", (0x52,)))
        self.assertIn("i2c adapter 'No such adapter' not found", logs.output[0])
        self.assertEqual(self.writes, [("i2c-0/new_device", "spd5118 0x52\n")])

    def test_adapter_lookup_by_name_picks_the_right_bus(self):
        self.add_bus(2, "Other adapter")
        self.probe(I2cDevice("Other adapter", "spd5118", (0x50,)))
        self.assertEqual(self.writes[0][0], "i2c-2/new_device")

    def test_adapter_matches_by_name_prefix_not_substring(self):
        self.add_bus(3, "Synopsys DesignWare SMBus I801 adapter")   # contains, does not start with
        self.probe(SPD)
        self.assertTrue(all(path.startswith("i2c-0/") for path, _ in self.writes))

    def test_new_device_write_error_is_logged_and_next_address_tried(self):
        self.fail.add("new_device")
        with self.assertLogs("ugreen_fan.module", "WARNING") as logs:
            self.probe(SPD)
        self.assertEqual(len(logs.output), 2)
        self.assertIn("cannot register spd5118 at 0x50", logs.output[0])
        self.assertEqual(self.writes, [])

    def test_delete_device_write_error_is_logged_and_next_address_tried(self):
        self.fail.add("delete_device")
        with self.assertLogs("ugreen_fan.module", "WARNING") as logs:
            self.probe(I2cDevice("SMBus I801 adapter", "spd5118", (0x51, 0x53)))
        self.assertEqual([w[1] for w in self.writes if w[0].endswith("new_device")],
                         ["spd5118 0x51\n", "spd5118 0x53\n"])
        self.assertEqual(len(logs.output), 2)
        self.assertIn("cannot remove spd5118 at 0x51", logs.output[0])

    def test_no_devices_does_nothing(self):
        self.probe()
        self.run.assert_not_called()
        self.assertEqual(self.writes, [])


class LoadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmp = Path(self.tmp.name)
        self.repo = tmp / "repo"
        ko = module_file(self.repo, "6.12.105-production+truenas")
        ko.parent.mkdir(parents=True)
        ko.write_text("")
        self.sys = FakeSysfs(tmp / "hwmon")
        self.chip = self.sys.add("it8613", {"pwm2": 51, "pwm2_enable": 2, "pwm3": 52, "pwm3_enable": 2})
        self.unit, self.bios_pwm = tmp / "unit", tmp / "bios_pwm"
        uname = mock.Mock(release="6.12.105-production+truenas")
        for target, kwargs in ((module, dict(attribute="check_model", return_value="DXP4800 Pro")),
                               (module.os, dict(attribute="uname", return_value=uname)),
                               (module, dict(attribute="is_loaded", return_value=True)),
                               (module, dict(attribute="_wait_for_chip", return_value=self.chip)),
                               (module, dict(attribute="UNIT_PATH", new=self.unit)),
                               (module, dict(attribute="BIOS_PWM_FILE", new=self.bios_pwm))):
            patcher = mock.patch.object(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(module, "_run")
        self.run = patcher.start()
        self.addCleanup(patcher.stop)

    def test_saves_every_fan_and_writes_unit_for_every_pwm(self):
        module.load(make_config(2, 3), self.repo, False)
        self.assertEqual(json.loads(self.bios_pwm.read_text()), {"2": 51, "3": 52})
        self.assertIn("run --chip it8613 --pwm 2 --pwm 3\n", self.unit.read_text())
        self.assertIn(mock.call("systemctl", "restart", "ugreen-fan.service"), self.run.call_args_list)

    def test_fan_removed_from_config_is_handed_back(self):
        self.bios_pwm.write_text('{"2": 51, "3": 52}\n')
        for pwm in (2, 3):
            Fan(self.chip, pwm, pwm).set_manual(200)
        with self.assertLogs("ugreen_fan.module", "INFO") as logs:
            module.load(make_config(3), self.repo, False)
        self.assertEqual((self.sys.read(self.chip, "pwm2"), self.sys.read(self.chip, "pwm2_enable")), ("51", "2"))
        self.assertEqual(self.sys.read(self.chip, "pwm3_enable"), "1")
        self.assertEqual(json.loads(self.bios_pwm.read_text()), {"3": 52})
        self.assertTrue(any("pwm2" in line for line in logs.output))

    def test_probes_i2c_before_waiting_for_the_chip_and_starting(self):
        order = []
        self.run.side_effect = lambda *args: order.append(args[0] + " " + args[1])
        with mock.patch.object(module, "probe_i2c", side_effect=lambda devices: order.append("probe")) as probe:
            module.load(make_config(2, 3, i2c_devices=(SPD,)), self.repo, False)
        probe.assert_called_once_with((SPD,))
        self.assertLess(order.index("probe"), order.index("systemctl restart"))

    def test_regulator_starts_when_nothing_binds(self):
        root = Path(self.tmp.name) / "i2c"
        (root / "i2c-0").mkdir(parents=True)
        (root / "i2c-0" / "name").write_text("SMBus I801 adapter\n")
        with mock.patch.object(module, "I2C_ROOT", root), \
                mock.patch.object(module, "_write_sysfs"), mock.patch.object(module.time, "sleep"), \
                self.assertLogs("ugreen_fan.module", "INFO"):
            module.load(make_config(2, 3, i2c_devices=(SPD,)), self.repo, False)
        self.assertIn(mock.call("systemctl", "restart", "ugreen-fan.service"), self.run.call_args_list)

    def test_corrupt_bios_pwm_does_not_stop_the_regulator(self):
        self.bios_pwm.write_text("{")
        with self.assertLogs("ugreen_fan.module", "WARNING"):
            module.load(make_config(2, 3), self.repo, False)
        self.assertIn(mock.call("systemctl", "restart", "ugreen-fan.service"), self.run.call_args_list)
        self.assertEqual(self.bios_pwm.read_text(), "{")


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

    def test_saved_fan_no_longer_configured_is_handed_back_too(self):
        self.bios_pwm.write_text('{"2": 51, "3": 52}\n')
        restore(make_config(3))
        self.assertEqual([self.read(f) for f in ("pwm2", "pwm2_enable", "pwm3", "pwm3_enable")],
                         ["51", "2", "52", "2"])
        self.assertFalse(self.bios_pwm.exists())

    def test_file_kept_when_a_fan_cannot_be_handed_back(self):
        self.bios_pwm.write_text('{"2": 51, "3": 52, "4": 53}\n')
        (self.chip / "pwm4_enable").mkdir()  # unwritable
        with self.assertRaisesRegex(SetupError, "pwm4"):
            restore(make_config(3))
        self.assertTrue(self.bios_pwm.exists())
        self.assertNotIn(mock.call("rmmod", "it87"), self.run.call_args_list)
        self.assertEqual((self.read("pwm2_enable"), self.read("pwm3_enable")), ("2", "2"))

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
