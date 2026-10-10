import copy
import tempfile
import tomllib
import unittest
from pathlib import Path

from ugreen_fan.config import ConfigError, FanSpec, I2cDevice, find_preset, load_config, parse_config

REPO = Path(__file__).resolve().parent.parent
EXAMPLE = REPO / "presets" / "dxp4800.toml"
PRO = REPO / "presets" / "dxp4800-pro.toml"


def example() -> dict:
    with EXAMPLE.open("rb") as f:
        return tomllib.load(f)


class ParseConfigTest(unittest.TestCase):
    def test_example_is_valid(self):
        config = parse_config(example())
        self.assertEqual(config.chip, "it8613")
        self.assertEqual(config.fans, (FanSpec(3, 3, ("disks", "cpu")),))
        self.assertEqual([s.name for s in config.sources], ["disks", "cpu"])
        disks = config.sources[0]
        self.assertEqual(disks.channel, 1)
        self.assertEqual(disks.curve[0], (42.0, 60))
        self.assertEqual(disks.valid, (1.0, 80.0))
        self.assertFalse(disks.optional)

    def assert_invalid(self, mutate):
        data = copy.deepcopy(example())
        mutate(data)
        with self.assertRaises(ConfigError):
            parse_config(data)

    def test_missing_key(self):
        self.assert_invalid(lambda d: d.pop("chip"))

    def test_no_sources(self):
        self.assert_invalid(lambda d: d.update(sources={}))

    def test_curve_temperatures_must_increase(self):
        self.assert_invalid(lambda d: d["sources"]["disks"].update(curve=[[50, 60], [45, 100]]))

    def test_curve_pwm_must_not_decrease(self):
        self.assert_invalid(lambda d: d["sources"]["disks"].update(curve=[[40, 100], [45, 90]]))

    def test_curve_pwm_range(self):
        self.assert_invalid(lambda d: d["sources"]["disks"].update(curve=[[40, 100], [45, 300]]))

    def test_empty_curve(self):
        self.assert_invalid(lambda d: d["sources"]["disks"].update(curve=[]))

    def test_invalid_valid_range(self):
        self.assert_invalid(lambda d: d["sources"]["disks"].update(valid=[80, 1]))

    def test_min_pwm_range(self):
        self.assert_invalid(lambda d: d.update(min_pwm=256))

    def test_interval_must_fit_watchdog(self):
        self.assert_invalid(lambda d: d.update(interval=11))

    def test_optional_source(self):
        data = copy.deepcopy(example())
        data["sources"]["cpu"]["optional"] = True
        self.assertTrue(parse_config(data).sources[1].optional)

    def test_optional_must_be_bool(self):
        self.assert_invalid(lambda d: d["sources"]["cpu"].update(optional="yes"))

    def test_wrong_type(self):
        self.assert_invalid(lambda d: d.update(pwm="three"))

    def test_legacy_fan_key_missing(self):
        self.assert_invalid(lambda d: d.pop("fan"))


def with_fans(*fans: dict) -> dict:
    data = copy.deepcopy(example())
    del data["pwm"], data["fan"]
    data["fans"] = list(fans)
    return data


class FansTest(unittest.TestCase):
    def assert_invalid(self, data, message):
        with self.assertRaisesRegex(ConfigError, message):
            parse_config(data)

    def test_fans_default_to_every_source(self):
        config = parse_config(with_fans({"pwm": 2, "fan": 2}, {"pwm": 3, "fan": 3}))
        self.assertEqual(config.fans, (FanSpec(2, 2, ("disks", "cpu")), FanSpec(3, 3, ("disks", "cpu"))))

    def test_fan_with_own_sources(self):
        config = parse_config(with_fans({"pwm": 2, "fan": 2, "sources": ["cpu"]}, {"pwm": 3, "fan": 3}))
        self.assertEqual(config.fans[0], FanSpec(2, 2, ("cpu",)))

    def test_fan_min_pwm(self):
        config = parse_config(with_fans({"pwm": 2, "fan": 2}, {"pwm": 3, "fan": 3, "min_pwm": 105}))
        self.assertEqual([fan.min_pwm for fan in config.fans], [None, 105])

    def test_fan_min_pwm_range(self):
        self.assert_invalid(with_fans({"pwm": 2, "fan": 2, "min_pwm": 256}), "min_pwm must be within 0..255")

    def test_both_forms(self):
        data = with_fans({"pwm": 2, "fan": 2})
        data["pwm"] = 3
        self.assert_invalid(data, "not both")

    def test_legacy_fan_key_alone_counts_as_legacy_form(self):
        data = with_fans({"pwm": 2, "fan": 2})
        data["fan"] = 3
        self.assert_invalid(data, "not both")

    def test_neither_form(self):
        data = with_fans()
        del data["fans"]
        self.assert_invalid(data, r"missing \[\[fans\]\]")

    def test_empty_fans(self):
        self.assert_invalid(with_fans(), "at least one")

    def test_unknown_source(self):
        self.assert_invalid(with_fans({"pwm": 2, "fan": 2, "sources": ["cpu", "gpu"]}), "unknown sources")

    def test_empty_sources(self):
        self.assert_invalid(with_fans({"pwm": 2, "fan": 2, "sources": []}), "sources is empty")

    def test_sources_must_be_a_list(self):
        self.assert_invalid(with_fans({"pwm": 2, "fan": 2, "sources": "cpu"}), "must be a list")

    def test_duplicate_pwm(self):
        self.assert_invalid(with_fans({"pwm": 2, "fan": 2}, {"pwm": 2, "fan": 3}), "more than once")

    def test_fans_as_single_table(self):
        data = with_fans()
        data["fans"] = {"pwm": 2, "fan": 2}
        self.assert_invalid(data, r"fans must be an array of tables: use \[\[fans\]\]")

    def test_fans_entry_not_a_table(self):
        self.assert_invalid(with_fans({"pwm": 2, "fan": 2}, 3), r"fans must be an array of tables: use \[\[fans\]\]")

    def test_fan_needs_tachometer(self):
        self.assert_invalid(with_fans({"pwm": 2}), "missing key 'fan'")

    def test_fan_wrong_type(self):
        self.assert_invalid(with_fans({"pwm": "two", "fan": 2}), "invalid value")


def with_i2c(*devices: dict) -> dict:
    data = copy.deepcopy(example())
    data["i2c_devices"] = list(devices)
    return data


SPD = {"adapter": "SMBus I801 adapter", "driver": "spd5118", "addresses": [0x50, 0x52]}


class I2cDevicesTest(unittest.TestCase):
    def assert_invalid(self, device: dict, message: str):
        with self.assertRaisesRegex(ConfigError, message):
            parse_config(with_i2c(device))

    def test_default_is_none(self):
        self.assertEqual(parse_config(example()).i2c_devices, ())

    def test_parsed(self):
        config = parse_config(with_i2c(SPD))
        self.assertEqual(config.i2c_devices, (I2cDevice("SMBus I801 adapter", "spd5118", (0x50, 0x52)),))

    def test_must_be_array_of_tables(self):
        data = example()
        data["i2c_devices"] = {"adapter": "x"}
        with self.assertRaisesRegex(ConfigError, r"\[\[i2c_devices\]\]"):
            parse_config(data)

    def test_adapter_and_driver_are_strings(self):
        self.assert_invalid({**SPD, "adapter": 0}, "adapter must be a non-empty string")
        self.assert_invalid({**SPD, "driver": ""}, "driver must be a non-empty string")
        self.assert_invalid({k: v for k, v in SPD.items() if k != "driver"}, "driver must be")

    def test_addresses_type(self):
        for bad in (0x52, [], ["0x52"], [0x52, True], [1.5]):
            with self.subTest(bad=bad):
                self.assert_invalid({**SPD, "addresses": bad}, "non-empty list of integers")

    def test_addresses_missing(self):
        self.assert_invalid({"adapter": "a", "driver": "d"}, "non-empty list of integers")

    def test_address_range(self):
        for bad in (0x02, 0x78, -1):
            with self.subTest(bad=bad):
                self.assert_invalid({**SPD, "addresses": [0x50, bad]}, "within 0x03..0x77")
        self.assertEqual(parse_config(with_i2c({**SPD, "addresses": [0x03, 0x77]})).i2c_devices[0].addresses,
                         (0x03, 0x77))


class PresetTest(unittest.TestCase):
    def presets(self) -> list[Path]:
        return sorted((REPO / "presets").glob("*.toml"))

    def test_every_shipped_preset_parses(self):
        self.assertIn(PRO, self.presets())
        for path in self.presets():
            with self.subTest(path.name):
                self.assertTrue(load_config(path).supported_models)

    def test_models_are_unique_across_presets(self):
        models = [m for path in self.presets() for m in load_config(path).supported_models]
        self.assertEqual(len(models), len(set(models)))

    def test_pro_preset(self):
        config = load_config(PRO)
        self.assertEqual((config.supported_models, config.chip, config.module_params),
                         (("DXP4800 Pro",), "it8613", "ignore_resource_conflict=1"))
        every = ("disks", "cpu", "ram", "nvme")
        self.assertEqual(config.fans, (FanSpec(2, 2, every, 51), FanSpec(3, 3, every, 105)))
        self.assertEqual((config.interval, config.hysteresis, config.min_pwm, config.truenas_alert),
                         (10, 2, 51, True))
        sources = {s.name: s for s in config.sources}
        example_sources = {s.name: s for s in load_config(EXAMPLE).sources}
        self.assertEqual(sources["disks"], example_sources["disks"])
        self.assertEqual(sources["cpu"], example_sources["cpu"])
        ram, nvme = sources["ram"], sources["nvme"]
        self.assertEqual((ram.driver, ram.channel, ram.valid, ram.optional), ("spd5118", 1, (1.0, 100.0), False))
        self.assertEqual(ram.curve, ((50, 51), (55, 90), (60, 140), (65, 200), (70, 255)))
        self.assertEqual((nvme.driver, nvme.channel, nvme.valid, nvme.optional), ("nvme", 1, (1.0, 100.0), True))
        self.assertEqual(nvme.curve, ((50, 51), (60, 100), (70, 180), (75, 255)))
        self.assertEqual(config.i2c_devices,
                         (I2cDevice("SMBus I801 adapter", "spd5118", (0x50, 0x51, 0x52, 0x53)),))

    def test_dxp4800_preset_has_no_i2c_devices(self):
        self.assertEqual(load_config(EXAMPLE).i2c_devices, ())

    def test_find_preset_by_model(self):
        self.assertEqual(find_preset("DXP4800 Pro", self.presets()), PRO)
        self.assertEqual(find_preset("DXP4800", self.presets()), EXAMPLE)

    def test_find_preset_unknown_model(self):
        self.assertIsNone(find_preset("DXP8800 Plus", self.presets()))


class LoadConfigTest(unittest.TestCase):
    def test_missing_file_mentions_example(self):
        with self.assertRaisesRegex(ConfigError, "presets/"):
            load_config(Path("/nonexistent/config.toml"))

    def test_broken_toml(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text("chip = [\n")
            with self.assertRaises(ConfigError):
                load_config(path)
