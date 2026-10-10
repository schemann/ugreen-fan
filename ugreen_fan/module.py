"""Kernel module loading, BIOS hand-back and the transient systemd unit."""

import json
import logging
import os
import subprocess
import time
from pathlib import Path

from .config import Config, I2cDevice
from .hwmon import HWMON_ROOT, Fan, SensorError, find_chip, find_hwmon

MODULE = "it87"
RUN_DIR = Path("/run/ugreen-fan")
STATE_FILE = RUN_DIR / "state.json"
BIOS_PWM_FILE = RUN_DIR / "bios_pwm"
UNIT_NAME = "ugreen-fan.service"
UNIT_PATH = Path("/run/systemd/system") / UNIT_NAME
DMI_PRODUCT = Path("/sys/class/dmi/id/product_name")
PROC_MODULES = Path("/proc/modules")
I2C_ROOT = Path("/sys/bus/i2c/devices")
CHIP_TIMEOUT = 5.0
BIND_POLLS = 10     # x BIND_INTERVAL: wait up to 1 s for a probed device to bind
BIND_INTERVAL = 0.1

log = logging.getLogger(__name__)


class SetupError(Exception):
    pass


def read_model(dmi_path: Path | None = None) -> str:
    path = dmi_path or DMI_PRODUCT
    try:
        return path.read_text().strip()
    except OSError as e:
        raise SetupError(f"cannot read the model from {path}: {e}") from e


def check_model(supported: tuple[str, ...], force: bool, dmi_path: Path | None = None) -> str:
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


def _duty(value: object) -> int:
    # bool is an int subclass: JSON true/false is not a duty
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 255:
        raise ValueError(f"{value!r} is not a PWM duty")
    return value


def _parse_bios_pwm(path: Path) -> int | dict[int, int] | None:
    """None: no file; int: legacy file (one start PWM for every fan); else pwm channel -> start PWM."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            return _duty(data)
        return {int(pwm): _duty(duty) for pwm, duty in data.items()}
    except (OSError, ValueError) as e:
        raise SetupError(f"{path} is corrupt ({e}); reboot to let the BIOS "
                         "re-initialise the fan controller") from e


def read_bios_pwm(pwms: list[int], path: Path = BIOS_PWM_FILE) -> dict[int, int]:
    """pwm channel -> BIOS start PWM. A legacy file holds one number for every fan."""
    data = _parse_bios_pwm(path)
    if isinstance(data, int):
        return dict.fromkeys(pwms, data)
    return data or {}


def _write_bios_pwm(saved: dict[int, int], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({str(pwm): saved[pwm] for pwm in sorted(saved)}) + "\n")


def save_bios_pwm(fans: list[Fan], path: Path = BIOS_PWM_FILE) -> None:
    """Remember the start PWM of every fan still on the BIOS curve; never overwrite one.

    A corrupt file is left alone (restore then refuses with the reboot advice), so the
    regulator still starts. A legacy single-number file is rewritten as JSON: fans still
    on the BIOS curve get their real start PWM, the others keep the legacy value.
    """
    try:
        data = _parse_bios_pwm(path)
    except SetupError as e:
        log.warning("%s", e)
        return
    if isinstance(data, int):
        _write_bios_pwm({fan.pwm: fan.duty() if fan.enable() == 2 else data for fan in fans}, path)
        return
    saved = data or {}
    new = {fan.pwm: fan.duty() for fan in fans if fan.pwm not in saved and fan.enable() == 2}
    if new:
        _write_bios_pwm(saved | new, path)


def release_unconfigured(chip: Path, config: Config, path: Path = BIOS_PWM_FILE) -> None:
    """Hand fans that were saved but are no longer configured back to the BIOS curve.

    Otherwise a [[fans]] table removed from config.toml leaves its fan in manual mode
    at the last duty, with nothing regulating it.
    """
    try:
        data = _parse_bios_pwm(path)
    except SetupError as e:
        log.warning("%s", e)
        return
    if not isinstance(data, dict):
        return
    configured = {spec.pwm for spec in config.fans}
    released = {}
    for pwm in sorted(set(data) - configured):
        try:
            Fan(chip, pwm, 0).set_auto(data[pwm])
        except SensorError as e:
            log.error("Could not hand pwm%d back to BIOS: %s", pwm, e)
            continue
        released[pwm] = data.pop(pwm)
    if released:
        _write_bios_pwm(data, path)
        log.info("pwm%s no longer configured, returned to BIOS control (start PWM %s)",
                 ", pwm".join(map(str, released)), ", ".join(map(str, released.values())))


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


def _write_sysfs(path: Path, text: str) -> None:
    path.write_text(text)


def _find_bus(adapter: str, root: Path) -> int | None:
    buses = []
    for entry in root.glob("i2c-*"):
        try:
            # the kernel appends the I/O base ("SMBus I801 adapter at efa0"), which varies
            if (entry / "name").read_text().strip().startswith(adapter):
                buses.append(int(entry.name.removeprefix("i2c-")))
        except (OSError, ValueError):
            continue
    return min(buses, default=None)


def _wait_for_driver(device: Path) -> bool:
    for _ in range(BIND_POLLS):
        if (device / "driver").is_symlink():
            return True
        time.sleep(BIND_INTERVAL)
    return (device / "driver").is_symlink()


def probe_i2c(devices: tuple[I2cDevice, ...], root: Path = I2C_ROOT) -> None:
    """Register i2c sensors the kernel does not find itself (SPD hubs only at 0x50/0x51).

    A device that does not bind is removed again. Nothing here may fail `load`: a missing
    sensor is already a failsafe for its source and an alert from `check`.
    """
    for spec in devices:
        try:
            _run("modprobe", spec.driver)
        except SetupError as e:
            log.warning("%s", e)
        bus = _find_bus(spec.adapter, root)
        if bus is None:
            log.warning("i2c adapter '%s' not found, skipping %s", spec.adapter, spec.driver)
            continue
        for address in spec.addresses:
            device = root / f"{bus}-{address:04x}"
            if device.exists():
                continue
            try:
                _write_sysfs(root / f"i2c-{bus}" / "new_device", f"{spec.driver} 0x{address:02x}\n")
            except OSError as e:
                log.warning("cannot register %s at 0x%02x on i2c-%d: %s", spec.driver, address, bus, e)
                continue
            if _wait_for_driver(device):
                log.info("Registered %s at 0x%02x on i2c-%d", spec.driver, address, bus)
                continue
            try:
                _write_sysfs(root / f"i2c-{bus}" / "delete_device", f"0x{address:02x}\n")
            except OSError as e:
                log.warning("cannot remove %s at 0x%02x on i2c-%d: %s", spec.driver, address, bus, e)
                continue
            log.info("No %s at 0x%02x on i2c-%d, removed", spec.driver, address, bus)


def load(config: Config, repo: Path, force: bool) -> None:
    model = check_model(config.supported_models, force)
    release = os.uname().release
    ko = module_file(repo, release)
    if not ko.exists():
        raise SetupError(f"{ko} not found: module not built for kernel {release}, run build.sh")
    if not is_loaded():
        insert_module(ko, config.module_params)
        log.info("Loaded %s on %s", ko, model)
    probe_i2c(config.i2c_devices)
    chip = _wait_for_chip(config.chip)
    save_bios_pwm([Fan(chip, spec.pwm, spec.fan) for spec in config.fans], BIOS_PWM_FILE)
    UNIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    UNIT_PATH.write_text(unit_text(repo, config.chip, [spec.pwm for spec in config.fans]))
    _run("systemctl", "daemon-reload")
    _run("systemctl", "restart", UNIT_NAME)
    log.info("Started %s", UNIT_NAME)
    # after the restart: the old unit's ExecStopPost may still name the removed channel
    release_unconfigured(chip, config, BIOS_PWM_FILE)


def restore(config: Config) -> None:
    loaded = is_loaded()
    configured = [spec.pwm for spec in config.fans]
    pwms = configured
    bios_pwm: dict[int, int] = {}
    if loaded:
        # check before changing anything: without a start PWM a fan cannot be handed back
        if not BIOS_PWM_FILE.exists():
            raise SetupError(f"{BIOS_PWM_FILE} is missing, cannot restore the BIOS curve; "
                             "reboot to let the BIOS re-initialise the fan controller")
        bios_pwm = read_bios_pwm(configured, BIOS_PWM_FILE)
        # saved fans that are no longer configured go back as well
        pwms = sorted(set(configured) | set(bios_pwm))
        missing = [f"pwm{pwm}" for pwm in configured if pwm not in bios_pwm]
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
        except SensorError as e:
            raise SetupError(f"could not hand the fans back to BIOS: {e}") from e
        failed = []
        for pwm in pwms:
            try:
                Fan(chip, pwm, 0).set_auto(bios_pwm[pwm])
            except SensorError as e:
                failed.append(f"pwm{pwm}: {e}")
        if failed:
            # keep the file and the module: the start PWM is the only way back without a reboot
            raise SetupError(f"could not hand the fans back to BIOS: {'; '.join(failed)}")
        _run("rmmod", MODULE)
        BIOS_PWM_FILE.unlink()
        log.info("Fans returned to BIOS control (start PWM %s), %s unloaded",
                 ", ".join(f"pwm{pwm}={bios_pwm[pwm]}" for pwm in pwms), MODULE)
