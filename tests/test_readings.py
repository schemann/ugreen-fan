import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.fakesys import FakeSysfs
from ugreen_fan.config import Source
from ugreen_fan.hwmon import SensorError
from ugreen_fan.readings import read_source

CURVE = ((40.0, 100), (50.0, 200))


def disks() -> Source:
    return Source("disks", "drivetemp", 1, (1.0, 80.0), CURVE)


def ram() -> Source:
    return Source("ram", "spd5118", 1, (1.0, 100.0), CURVE)


class ReadSourceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sys = FakeSysfs(self.root)
        module = self.root / "module" / "drivetemp"
        module.mkdir(parents=True)
        patcher = mock.patch("ugreen_fan.readings.DRIVETEMP_MODULE", module)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.module = module

    def tearDown(self):
        self.tmp.cleanup()

    def test_every_bay_labelled_by_ata_port(self):
        self.sys.add("drivetemp", {"temp1_input": 47000}, block="sdb", port="ata1")
        self.sys.add("drivetemp", {"temp1_input": 48000}, block="sda", port="ata3")
        self.assertEqual(read_source(disks(), self.root), {"bay1": 47.0, "bay3": 48.0})

    def test_hot_added_disk_is_picked_up(self):
        self.sys.add("drivetemp", {"temp1_input": 47000}, block="sda", port="ata1")
        self.assertEqual(read_source(disks(), self.root), {"bay1": 47.0})
        self.sys.add("drivetemp", {"temp1_input": 50000}, block="sdc", port="ata4")
        self.assertEqual(read_source(disks(), self.root), {"bay1": 47.0, "bay4": 50.0})

    def test_usb_disks_are_ignored(self):
        self.sys.add("drivetemp", {"temp1_input": 47000}, block="sda", port="ata1")
        self.sys.add("drivetemp", {"temp1_input": 70000}, block="sdc", port="usb3")
        self.assertEqual(read_source(disks(), self.root), {"bay1": 47.0})

    def test_no_disks_in_bays_contributes_nothing(self):
        self.sys.add("drivetemp", {"temp1_input": 47000}, block="sdc", port="usb3")
        self.assertEqual(read_source(disks(), self.root), {})

    def test_drivetemp_not_loaded_is_an_error(self):
        self.module.rmdir()
        with self.assertRaisesRegex(SensorError, "drivetemp module is not loaded"):
            read_source(disks(), self.root)

    def test_out_of_range(self):
        self.sys.add("drivetemp", {"temp1_input": 0}, block="sda", port="ata1")
        with self.assertRaisesRegex(SensorError, "outside valid range"):
            read_source(disks(), self.root)

    def test_chip_channel(self):
        self.sys.add("it8613", {"temp1_input": 49000, "temp2_input": 34000})
        source = Source("cpu", "it8613", 1, (1.0, 110.0), CURVE)
        self.assertEqual(read_source(source, self.root), {"it8613/temp1": 49.0})

    def test_chip_missing(self):
        source = Source("cpu", "it8613", 1, (1.0, 110.0), CURVE)
        with self.assertRaisesRegex(SensorError, "no hwmon named 'it8613'"):
            read_source(source, self.root)

    def test_missing_optional_source_contributes_nothing(self):
        source = Source("nvme", "nvme", 1, (1.0, 100.0), CURVE, optional=True)
        self.assertEqual(read_source(source, self.root), {})

    def test_present_optional_source_is_read(self):
        self.sys.add("nvme", {"temp1_input": 41850}, device="nvme0")
        source = Source("nvme", "nvme", 1, (1.0, 100.0), CURVE, optional=True)
        self.assertEqual(read_source(source, self.root), {"nvme/temp1": 41.85})

    def test_single_instance_keeps_plain_label(self):
        self.sys.add("spd5118", {"temp1_input": 59000}, device="0-0050")
        self.assertEqual(read_source(ram(), self.root), {"spd5118/temp1": 59.0})

    def test_every_instance_labelled_by_device(self):
        self.sys.add("spd5118", {"temp1_input": 61000}, device="0-0052", index=5)
        self.sys.add("spd5118", {"temp1_input": 59000}, device="0-0050", index=4)
        self.assertEqual(read_source(ram(), self.root),
                         {"spd5118@0-0050/temp1": 59.0, "spd5118@0-0052/temp1": 61.0})

    def test_instance_without_device_link_labelled_by_hwmon(self):
        self.sys.add("spd5118", {"temp1_input": 59000}, index=4)
        self.sys.add("spd5118", {"temp1_input": 61000}, device="0-0052", index=5)
        self.assertEqual(read_source(ram(), self.root),
                         {"spd5118@hwmon4/temp1": 59.0, "spd5118@0-0052/temp1": 61.0})

    def test_every_instance_checked_against_valid(self):
        self.sys.add("spd5118", {"temp1_input": 59000}, device="0-0050")
        self.sys.add("spd5118", {"temp1_input": 0}, device="0-0052")
        with self.assertRaisesRegex(SensorError, r"spd5118@0-0052/temp1: 0.0 .* outside valid range"):
            read_source(ram(), self.root)

    def test_unreadable_instance_is_an_error(self):
        self.sys.add("spd5118", {"temp1_input": 59000}, device="0-0050")
        self.sys.add("spd5118", device="0-0052")
        with self.assertRaises(SensorError):
            read_source(ram(), self.root)
