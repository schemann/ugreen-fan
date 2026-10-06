import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from tests.fakesys import FakeSysfs
from ugreen_fan import cli
from ugreen_fan.config import ConfigError, FanSpec


def fans(*pwms: int) -> tuple[FanSpec, ...]:
    return tuple(FanSpec(pwm, pwm, ("cpu",)) for pwm in pwms)


class FailsafeCommandTest(unittest.TestCase):
    def test_failsafe_does_not_read_config(self):
        with mock.patch.object(cli, "load_config", side_effect=ConfigError("broken")) as load, \
             mock.patch.object(cli, "find_chip", return_value="/hwmon") as find, \
             mock.patch.object(cli.Fan, "set_manual") as set_manual:
            self.assertEqual(cli.main(["failsafe", "--chip", "it8613", "--pwm", "3"]), 0)
        load.assert_not_called()
        find.assert_called_once_with("it8613")
        set_manual.assert_called_once_with(255)

    def test_failsafe_sets_every_pwm(self):
        with tempfile.TemporaryDirectory() as tmp:
            sys = FakeSysfs(Path(tmp))
            chip = sys.add("it8613", {"pwm2": 51, "pwm2_enable": 2, "pwm3": 51, "pwm3_enable": 2})
            with mock.patch.object(cli, "find_chip", return_value=chip):
                self.assertEqual(cli.main(["failsafe", "--chip", "it8613", "--pwm", "2", "--pwm", "3"]), 0)
            self.assertEqual([sys.read(chip, f) for f in ("pwm2", "pwm2_enable", "pwm3", "pwm3_enable")],
                             ["255", "1", "255", "1"])

    def test_pwm_is_required(self):
        with mock.patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
            cli.main(["failsafe", "--chip", "it8613"])


class CheckCommandTest(unittest.TestCase):
    def run_check(self, problem):
        with mock.patch.object(cli, "load_config", return_value=mock.Mock(truenas_alert=True, interval=10)), \
             mock.patch.object(cli, "_probe_problem", return_value=problem), \
             mock.patch.object(cli, "raise_alert") as raise_alert, \
             mock.patch.object(cli, "clear_alert") as clear_alert:
            out = io.StringIO()
            with redirect_stdout(out):
                code = cli.main(["check"])
        return code, out.getvalue(), raise_alert, clear_alert

    def test_healthy_is_silent_and_clears(self):
        code, out, raise_alert, clear_alert = self.run_check(None)
        self.assertEqual((code, out), (0, ""))
        clear_alert.assert_called_once()
        raise_alert.assert_not_called()

    def test_problem_prints_alerts_and_fails(self):
        code, out, raise_alert, clear_alert = self.run_check("service down")
        self.assertEqual(code, 1)
        self.assertIn("service down", out)
        raise_alert.assert_called_once_with("service down")
        clear_alert.assert_not_called()

    def test_broken_config_is_reported_on_stdout(self):
        with mock.patch.object(cli, "load_config", side_effect=ConfigError("bad curve")), \
             mock.patch.object(cli, "raise_alert") as raise_alert:
            out = io.StringIO()
            with redirect_stdout(out):
                code = cli.main(["check"])
        self.assertEqual(code, 1)
        self.assertIn("bad curve", out.getvalue())
        raise_alert.assert_called_once()


class RunCommandTest(unittest.TestCase):
    def run_with(self, config, *argv: str):
        with mock.patch.object(cli, "load_config", return_value=config), \
             mock.patch.object(cli, "run", return_value=0) as run:
            return cli.main(["run", "--chip", "it8613", *argv]), run

    def test_refuses_when_config_moved_to_another_channel(self):
        code, run = self.run_with(mock.Mock(chip="it8613", fans=fans(2)), "--pwm", "3")
        self.assertEqual(code, 1)
        run.assert_not_called()

    def test_runs_when_channel_matches(self):
        code, run = self.run_with(mock.Mock(chip="it8613", fans=fans(3)), "--pwm", "3")
        self.assertEqual(code, 0)
        run.assert_called_once()

    def test_runs_when_every_channel_matches_in_any_order(self):
        code, run = self.run_with(mock.Mock(chip="it8613", fans=fans(2, 3)), "--pwm", "3", "--pwm", "2")
        self.assertEqual(code, 0)
        run.assert_called_once()

    def test_refuses_when_a_fan_was_added(self):
        code, run = self.run_with(mock.Mock(chip="it8613", fans=fans(2, 3)), "--pwm", "3")
        self.assertEqual(code, 1)
        run.assert_not_called()

    def test_refuses_when_chip_changed(self):
        code, run = self.run_with(mock.Mock(chip="it8625", fans=fans(3)), "--pwm", "3")
        self.assertEqual(code, 1)
        run.assert_not_called()


class ProbeTest(unittest.TestCase):
    def test_pwm_enable_for_every_fan(self):
        with tempfile.TemporaryDirectory() as tmp:
            chip = FakeSysfs(Path(tmp)).add("it8613", {"pwm2_enable": 1, "pwm3_enable": 2})
            config = mock.Mock(chip="it8613", fans=fans(2, 3, 4))
            with mock.patch.object(cli, "find_chip", return_value=chip):
                self.assertEqual(cli._pwm_enable(config), {2: 1, 3: 2, 4: None})

    def test_pwm_enable_without_chip(self):
        config = mock.Mock(chip="it8613", fans=fans(2, 3))
        with mock.patch.object(cli, "find_chip", side_effect=cli.SensorError("found 0")):
            self.assertEqual(cli._pwm_enable(config), {2: None, 3: None})


class InstallCommandTest(unittest.TestCase):
    def install(self, model: str) -> tuple[str, list[str]]:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.toml"
            with mock.patch.object(cli, "CONFIG", config), \
                 mock.patch.object(cli, "read_model", return_value=model), \
                 mock.patch.object(cli, "register"), \
                 mock.patch.object(cli, "load") as load, \
                 self.assertLogs("ugreen_fan", "INFO") as logs:
                self.assertEqual(cli.main(["install"]), 0)
            load.assert_called_once()
            return config.read_text(), logs.output

    def test_copies_the_preset_of_the_model(self):
        text, _ = self.install("DXP4800 Pro")
        self.assertEqual(text, (cli.PRESETS / "dxp4800-pro.toml").read_text())

    def test_dxp4800_gets_its_preset(self):
        text, _ = self.install("DXP4800")
        self.assertEqual(text, (cli.PRESETS / "dxp4800.toml").read_text())

    def test_unknown_model_falls_back_to_dxp4800_with_warning(self):
        text, logs = self.install("DXP8800 Plus")
        self.assertEqual(text, cli.FALLBACK.read_text())
        self.assertEqual(cli.FALLBACK, cli.PRESETS / "dxp4800.toml")
        self.assertTrue(any(line.startswith("WARNING") and "DXP8800 Plus" in line for line in logs))

    def test_unreadable_dmi_is_a_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(cli, "CONFIG", Path(tmp) / "config.toml"), \
                 mock.patch("ugreen_fan.module.DMI_PRODUCT", Path(tmp) / "missing"), \
                 mock.patch.object(cli, "register") as register, \
                 self.assertLogs("ugreen_fan", "ERROR") as logs:
                self.assertEqual(cli.main(["install"]), 1)
            register.assert_not_called()
            self.assertIn("cannot read the model", logs.output[0])

    def test_existing_config_is_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.toml"
            config.write_text("custom")
            with mock.patch.object(cli, "CONFIG", config), \
                 mock.patch.object(cli, "read_model") as read_model, \
                 mock.patch.object(cli, "load_config"), \
                 mock.patch.object(cli, "register"), \
                 mock.patch.object(cli, "load"):
                self.assertEqual(cli.main(["install"]), 0)
            read_model.assert_not_called()
            self.assertEqual(config.read_text(), "custom")


class UninstallCommandTest(unittest.TestCase):
    def test_unregisters_even_if_restore_fails(self):
        with mock.patch.object(cli, "load_config"), \
             mock.patch.object(cli, "unregister") as unregister, \
             mock.patch.object(cli, "restore", side_effect=cli.SetupError("bios_pwm missing")):
            self.assertEqual(cli.main(["uninstall"]), 1)
        unregister.assert_called_once()
