import copy
import tempfile
import tomllib
import unittest
from pathlib import Path

from ugreen_fan.config import ConfigError, load_config, parse_config

EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.toml"


def example() -> dict:
    with EXAMPLE.open("rb") as f:
        return tomllib.load(f)


class ParseConfigTest(unittest.TestCase):
    def test_example_is_valid(self):
        config = parse_config(example())
        self.assertEqual(config.chip, "it8613")
        self.assertEqual(config.pwm, 3)
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


class LoadConfigTest(unittest.TestCase):
    def test_missing_file_mentions_example(self):
        with self.assertRaisesRegex(ConfigError, "config.example.toml"):
            load_config(Path("/nonexistent/config.toml"))

    def test_broken_toml(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text("chip = [\n")
            with self.assertRaises(ConfigError):
                load_config(path)
