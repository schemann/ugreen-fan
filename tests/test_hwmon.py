import tempfile
import unittest
from pathlib import Path

from tests.fakesys import FakeSysfs
from ugreen_fan.hwmon import (Fan, SensorError, drivetemp_ports, find_chip, find_hwmon, read_temp,
                              set_all_manual)


class HwmonTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sys = FakeSysfs(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_find_by_name_regardless_of_index(self):
        self.sys.add("coretemp", index=0)
        chip = self.sys.add("it8613", index=7)
        self.assertEqual(find_hwmon("it8613", self.root), [chip])
        self.assertEqual(find_chip("it8613", self.root), chip)

    def test_find_chip_missing(self):
        with self.assertRaisesRegex(SensorError, "found 0"):
            find_chip("it8613", self.root)

    def test_find_chip_ambiguous(self):
        self.sys.add("it8613")
        self.sys.add("it8613")
        with self.assertRaisesRegex(SensorError, "found 2"):
            find_chip("it8613", self.root)

    def test_drivetemp_without_ata_port_is_skipped(self):
        self.sys.add("drivetemp")
        self.assertEqual(drivetemp_ports(self.root), {})

    def test_drivetemp_ports_from_device_path(self):
        bay1 = self.sys.add("drivetemp", block="sda", port="ata1")
        bay3 = self.sys.add("drivetemp", block="sdb", port="ata3")
        self.sys.add("drivetemp", block="sdc", port="usb3")
        self.assertEqual(drivetemp_ports(self.root), {"ata1": bay1, "ata3": bay3})

    def test_read_temp_converts_millidegrees(self):
        hwmon = self.sys.add("drivetemp", {"temp1_input": 47000})
        self.assertEqual(read_temp(hwmon, 1), 47.0)

    def test_read_temp_missing_file(self):
        hwmon = self.sys.add("drivetemp")
        with self.assertRaises(SensorError):
            read_temp(hwmon, 1)

    def test_read_temp_garbage(self):
        hwmon = self.sys.add("drivetemp", {"temp1_input": "n/a"})
        with self.assertRaises(SensorError):
            read_temp(hwmon, 1)


class FanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sys = FakeSysfs(Path(self.tmp.name))
        self.hwmon = self.sys.add("it8613", {"pwm3": 51, "pwm3_enable": 2, "fan3_input": 552})
        self.fan = Fan(self.hwmon, pwm=3, fan=3)

    def tearDown(self):
        self.tmp.cleanup()

    def test_reads(self):
        self.assertEqual((self.fan.enable(), self.fan.duty(), self.fan.rpm()), (2, 51, 552))

    def test_set_manual(self):
        self.fan.set_manual(200)
        self.assertEqual(self.sys.read(self.hwmon, "pwm3_enable"), "1")
        self.assertEqual(self.sys.read(self.hwmon, "pwm3"), "200")

    def test_set_auto_restores_start_pwm_first(self):
        self.fan.set_manual(200)
        self.fan.set_auto(51)
        self.assertEqual(self.sys.read(self.hwmon, "pwm3"), "51")
        self.assertEqual(self.sys.read(self.hwmon, "pwm3_enable"), "2")

    def test_write_failure_raises_sensor_error(self):
        fan = Fan(self.hwmon / "missing", pwm=3, fan=3)
        with self.assertRaises(SensorError):
            fan.set_manual(255)

    def test_set_all_manual_writes_the_rest_before_raising(self):
        other = self.sys.add("it8613", {"pwm2": 51, "pwm2_enable": 2})
        with self.assertRaises(SensorError):
            set_all_manual([Fan(self.hwmon / "missing", 3, 3), Fan(other, 2, 2)], 255)
        self.assertEqual((self.sys.read(other, "pwm2_enable"), self.sys.read(other, "pwm2")), ("1", "255"))
