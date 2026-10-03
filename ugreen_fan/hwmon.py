"""sysfs hwmon access: lookup by name, temperature reads, PWM control."""

import re
from dataclasses import dataclass
from pathlib import Path

HWMON_ROOT = Path("/sys/class/hwmon")
ATA_PORT = re.compile(r"ata\d+")


class SensorError(Exception):
    pass


def find_hwmon(name: str, root: Path = HWMON_ROOT) -> list[Path]:
    found = []
    for entry in sorted(root.glob("hwmon*")):
        try:
            if (entry / "name").read_text().strip() == name:
                found.append(entry)
        except OSError:
            continue
    return found


def find_chip(name: str, root: Path = HWMON_ROOT) -> Path:
    found = find_hwmon(name, root)
    if len(found) != 1:
        raise SensorError(f"expected one hwmon named '{name}', found {len(found)}")
    return found[0]


def drivetemp_ports(root: Path = HWMON_ROOT) -> dict[str, Path]:
    """ATA port ("ata1") → drivetemp hwmon. Ports are fixed by wiring, unlike sdX names."""
    ports = {}
    for hwmon in find_hwmon("drivetemp", root):
        try:
            parts = (hwmon / "device").resolve(strict=True).parts
        except OSError:
            continue
        port = next((p for p in parts if ATA_PORT.fullmatch(p)), None)
        if port:
            ports[port] = hwmon
    return ports


def read_int(path: Path) -> int:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError) as e:
        raise SensorError(f"cannot read {path}: {e}") from e


def read_temp(hwmon: Path, channel: int) -> float:
    return read_int(hwmon / f"temp{channel}_input") / 1000


def _write(path: Path, value: int) -> None:
    try:
        path.write_text(f"{value}\n")
    except OSError as e:
        raise SensorError(f"cannot write {path}: {e}") from e


@dataclass(frozen=True)
class Fan:
    hwmon: Path
    pwm: int
    fan: int

    @property
    def _duty_path(self) -> Path:
        return self.hwmon / f"pwm{self.pwm}"

    @property
    def _enable_path(self) -> Path:
        return self.hwmon / f"pwm{self.pwm}_enable"

    def enable(self) -> int:
        return read_int(self._enable_path)

    def duty(self) -> int:
        return read_int(self._duty_path)

    def rpm(self) -> int:
        return read_int(self.hwmon / f"fan{self.fan}_input")

    def set_manual(self, value: int) -> None:
        _write(self._enable_path, 1)
        _write(self._duty_path, value)

    def set_auto(self, start_pwm: int) -> None:
        # On ITE chips the manual duty register doubles as the auto-curve start PWM.
        self.set_manual(start_pwm)
        _write(self._enable_path, 2)


def set_all_manual(fans: list[Fan], value: int) -> None:
    """Write every fan even if one fails, then report the first failure."""
    error = None
    for fan in fans:
        try:
            fan.set_manual(value)
        except SensorError as e:
            error = error or e
    if error:
        raise error
