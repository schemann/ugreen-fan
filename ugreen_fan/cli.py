"""Command-line entry point."""

import argparse
import json
import logging
import shutil
import sys
import time
from pathlib import Path

from .config import Config, ConfigError, find_preset, load_config
from .daemon import FAILSAFE_PWM, read_state, run
from .health import diagnose
from .hwmon import Fan, SensorError, find_chip, set_all_manual
from .module import STATE_FILE, SetupError, is_loaded, load, read_model, restore, service_active
from .truenas import TrueNASError, clear_alert, raise_alert, register, unregister

REPO = Path(__file__).resolve().parent.parent
CONFIG = REPO / "config.toml"
EXAMPLE = REPO / "config.example.toml"  # the DXP4800 preset, also the fallback
PRESETS = REPO / "presets"

log = logging.getLogger("ugreen_fan")


def cmd_load(args: argparse.Namespace) -> int:
    load(load_config(CONFIG), REPO, args.force)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    config = load_config(CONFIG)
    configured = sorted(spec.pwm for spec in config.fans)
    if (config.chip, configured) != (args.chip, sorted(args.pwm)):
        raise ConfigError(f"config.toml now drives {config.chip} {_channels(configured)}, but the "
                          f"service was set up for {args.chip} {_channels(sorted(args.pwm))}; "
                          "run 'ugreen-fan load'")
    return run(config, STATE_FILE)


def _channels(pwms: list[int]) -> str:
    return ", ".join(f"pwm{pwm}" for pwm in pwms)


def cmd_failsafe(args: argparse.Namespace) -> int:
    chip = find_chip(args.chip)
    set_all_manual([Fan(chip, pwm, 0) for pwm in args.pwm], FAILSAFE_PWM)
    return 0


def _pwm_enable(config: Config) -> dict[int, int | None]:
    pwms = [spec.pwm for spec in config.fans]
    try:
        chip = find_chip(config.chip)
    except SensorError:
        return dict.fromkeys(pwms, None)
    enable: dict[int, int | None] = {}
    for pwm in pwms:
        try:
            enable[pwm] = Fan(chip, pwm, 0).enable()
        except SensorError:
            enable[pwm] = None
    return enable


def _probe_problem(config: Config) -> str | None:
    return diagnose(module_loaded=is_loaded(), service_active=service_active(),
                    state=read_state(STATE_FILE), pwm_enable=_pwm_enable(config),
                    now=time.time(), interval=config.interval)


def cmd_check(args: argparse.Namespace) -> int:
    alert = True
    try:
        config = load_config(CONFIG)
        alert = config.truenas_alert
        problem = _probe_problem(config)
    except ConfigError as e:
        problem = f"config error: {e}"
    try:
        if problem is None:
            if alert:
                clear_alert()
            return 0
        print(problem)
        if alert:
            raise_alert(problem)
    except TrueNASError as e:
        print(f"could not update TrueNAS alert: {e}")
    return 1


def cmd_restore(args: argparse.Namespace) -> int:
    restore(load_config(CONFIG))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    print(json.dumps(read_state(STATE_FILE), indent=2))
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    if not CONFIG.exists():
        model = read_model()
        preset = find_preset(model, [EXAMPLE, *sorted(PRESETS.glob("*.toml"))])
        if preset is None:
            log.warning("No preset supports '%s'; check chip, fans and sources in %s", model, CONFIG)
            preset = EXAMPLE
        shutil.copyfile(preset, CONFIG)
        log.info("Created %s from %s", CONFIG, preset.name)
    config = load_config(CONFIG)
    register(REPO)
    log.info("Registered POSTINIT script and hourly Cron Job in TrueNAS")
    load(config, REPO, args.force)
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    unregister()
    log.info("Removed POSTINIT script and Cron Job from TrueNAS")
    restore(load_config(CONFIG))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ugreen-fan", description="UGREEN NAS fan regulator")
    commands = parser.add_subparsers(required=True, metavar="command")

    def add(name: str, handler, help_text: str) -> argparse.ArgumentParser:
        sub = commands.add_parser(name, help=help_text)
        sub.set_defaults(handler=handler)
        return sub

    add("load", cmd_load, "load the module and start the regulator (POSTINIT)") \
        .add_argument("--force", action="store_true", help="skip the model check")
    for name, handler, help_text in (
            ("run", cmd_run, "run the regulator in the foreground (systemd)"),
            ("failsafe", cmd_failsafe, "set the fans to full speed")):
        sub = add(name, handler, help_text)
        sub.add_argument("--chip", required=True)
        sub.add_argument("--pwm", type=int, action="append", required=True,
                         help="pwm channel, repeat for every fan")
    add("check", cmd_check, "health check for the TrueNAS Cron Job")
    add("restore", cmd_restore, "stop regulating and hand the fans back to BIOS")
    add("status", cmd_status, "print the regulator state")
    add("install", cmd_install, "register in TrueNAS and start") \
        .add_argument("--force", action="store_true", help="skip the model check")
    add("uninstall", cmd_uninstall, "restore BIOS control and unregister from TrueNAS")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s",
                        stream=sys.stderr)
    try:
        return args.handler(args)
    except (ConfigError, SetupError, SensorError, TrueNASError) as e:
        log.error("%s", e)
        return 1
