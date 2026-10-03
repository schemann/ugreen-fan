"""Kernel module loading, BIOS hand-back and the transient systemd unit."""

import json
import logging
import os
import subprocess
import time
from pathlib import Path

from .config import Config
from .hwmon import HWMON_ROOT, Fan, SensorError, find_chip, find_hwmon

MODULE = "it87"
RUN_DIR = Path("/run/ugreen-fan")
STATE_FILE = RUN_DIR / "state.json"
BIOS_PWM_FILE = RUN_DIR / "bios_pwm"
UNIT_NAME = "ugreen-fan.service"
UNIT_PATH = Path("/run/systemd/system") / UNIT_NAME
DMI_PRODUCT = Path("/sys/class/dmi/id/product_name")
PROC_MODULES = Path("/proc/modules")
CHIP_TIMEOUT = 5.0

log = logging.getLogger(__name__)


class SetupError(Exception):
    pass


def read_model(dmi_path: Path = DMI_PRODUCT) -> str:
    return dmi_path.read_text().strip()


def check_model(supported: tuple[str, ...], force: bool, dmi_path: Path = DMI_PRODUCT) -> str:
    model = read_model(dmi_path)
    if model not in supported and not force:
        raise SetupError(f"model '{model}' is not in supported_models {list(supported)}; "
                         "adjust config.toml or pass --force")
    return model


def module_file(repo: Path, release: str) -> Path:
    return repo / "modules" / release / f"{MODULE}.ko"


def is_loaded(proc_modules: Path = PROC_MODULES) -> bool:
    return any(line.split(" ", 1)[0] == MODULE for line in proc_modules.read_text().splitlines())


def service_active() -> bool:
    result = subprocess.run(["systemctl", "is-active", "--quiet", UNIT_NAME], check=False)
    return result.returncode == 0


def unit_text(repo: Path, chip: str, pwms: list[int]) -> str:
    command = f'"{repo / "bin" / "ugreen-fan"}"'
    # run and failsafe must agree on the channels even if config.toml is edited later
    channel = " ".join([f"--chip {chip}", *(f"--pwm {pwm}" for pwm in pwms)])
    return (
        "[Unit]\n"
        "Description=UGREEN fan regulator\n"
        "After=local-fs.target\n"
        "\n"
        "[Service]\n"
        "Type=notify\n"
        f"ExecStart={command} run {channel}\n"
        f"ExecStopPost={command} failsafe {channel}\n"
        "Environment=PYTHONUNBUFFERED=1\n"
        "Restart=always\n"
        "RestartSec=5\n"
        "WatchdogSec=30\n"
    )


def read_bios_pwm(pwms: list[int], path: Path = BIOS_PWM_FILE) -> dict[int, int]:
    """pwm channel -> BIOS start PWM. A legacy file holds one number for every fan."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        if isinstance(data, int):
            return dict.fromkeys(pwms, data)
        return {int(pwm): int(duty) for pwm, duty in data.items()}
    except (ValueError, TypeError, AttributeError) as e:
        raise SetupError(f"{path} is corrupt ({e}); reboot to let the BIOS "
                         "re-initialise the fan controller") from e


def save_bios_pwm(fans: list[Fan], path: Path = BIOS_PWM_FILE) -> None:
    """Remember the start PWM of every fan still on the BIOS curve; never overwrite one."""
    saved = read_bios_pwm([fan.pwm for fan in fans], path)
    new = {fan.pwm: fan.duty() for fan in fans if fan.pwm not in saved and fan.enable() == 2}
    if not new:
        return
    saved.update(new)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({str(pwm): saved[pwm] for pwm in sorted(saved)}) + "\n")


def _output(*args: str) -> str:
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    if result.returncode:
        raise SetupError(f"{' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def _run(*args: str) -> None:
    _output(*args)


def insert_module(ko: Path, params: str) -> None:
    """insmod does not resolve dependencies (it87 needs hwmon-vid), so modprobe them first."""
    depends = _output("modinfo", "-F", "depends", str(ko)).strip()
    for dependency in filter(None, depends.split(",")):
        _run("modprobe", dependency)
    _run("insmod", str(ko), *params.split())


def _wait_for_chip(name: str, root: Path = HWMON_ROOT) -> Path:
    deadline = time.monotonic() + CHIP_TIMEOUT
    while not find_hwmon(name, root):
        if time.monotonic() > deadline:
            raise SetupError(f"hwmon '{name}' did not appear after loading {MODULE}")
        time.sleep(0.2)
    return find_chip(name, root)


def load(config: Config, repo: Path, force: bool) -> None:
    model = check_model(config.supported_models, force)
    release = os.uname().release
    ko = module_file(repo, release)
    if not ko.exists():
        raise SetupError(f"{ko} not found: module not built for kernel {release}, run build.sh")
    if not is_loaded():
        insert_module(ko, config.module_params)
        log.info("Loaded %s on %s", ko, model)
    chip = _wait_for_chip(config.chip)
    save_bios_pwm([Fan(chip, spec.pwm, spec.fan) for spec in config.fans], BIOS_PWM_FILE)
    UNIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    UNIT_PATH.write_text(unit_text(repo, config.chip, [spec.pwm for spec in config.fans]))
    _run("systemctl", "daemon-reload")
    _run("systemctl", "restart", UNIT_NAME)
    log.info("Started %s", UNIT_NAME)


def restore(config: Config) -> None:
    loaded = is_loaded()
    pwms = [spec.pwm for spec in config.fans]
    bios_pwm: dict[int, int] = {}
    if loaded:
        # check before changing anything: without a start PWM a fan cannot be handed back
        if not BIOS_PWM_FILE.exists():
            raise SetupError(f"{BIOS_PWM_FILE} is missing, cannot restore the BIOS curve; "
                             "reboot to let the BIOS re-initialise the fan controller")
        bios_pwm = read_bios_pwm(pwms, BIOS_PWM_FILE)
        missing = [f"pwm{pwm}" for pwm in pwms if pwm not in bios_pwm]
        if missing:
            raise SetupError(f"{BIOS_PWM_FILE} has no start PWM for {', '.join(missing)}, cannot "
                             "restore the BIOS curve; reboot to let the BIOS re-initialise the fan controller")
    if UNIT_PATH.exists():
        _run("systemctl", "stop", UNIT_NAME)
        UNIT_PATH.unlink()
        _run("systemctl", "daemon-reload")
    if loaded:
        try:
            chip = find_chip(config.chip)
            for spec in config.fans:
                Fan(chip, spec.pwm, spec.fan).set_auto(bios_pwm[spec.pwm])
        except SensorError as e:
            raise SetupError(f"could not hand the fans back to BIOS: {e}") from e
        _run("rmmod", MODULE)
        BIOS_PWM_FILE.unlink()
        log.info("Fans returned to BIOS control (start PWM %s), %s unloaded",
                 ", ".join(f"pwm{pwm}={bios_pwm[pwm]}" for pwm in pwms), MODULE)
