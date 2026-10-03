"""Turning a configured source into validated temperature readings."""

from pathlib import Path

from .config import Source
from .hwmon import HWMON_ROOT, SensorError, drivetemp_ports, find_hwmon, read_temp

DRIVETEMP_MODULE = Path("/sys/module/drivetemp")


def read_source(source: Source, root: Path = HWMON_ROOT) -> dict[str, float]:
    if source.driver == "drivetemp":
        sensors = _bay_sensors(root)
    else:
        sensors = _driver_sensors(source, root)
    temps = {label: read_temp(hwmon, source.channel) for label, hwmon in sensors.items()}
    low, high = source.valid
    for label, temp in temps.items():
        if not low <= temp <= high:
            raise SensorError(f"{label}: {temp:.1f} °C is outside valid range {low:g}..{high:g}")
    return temps


def _bay_sensors(root: Path) -> dict[str, Path]:
    """Every SATA drive, labelled by its ATA port; USB disks have no ATA port and are skipped.

    Empty bays are fine (the source then adds nothing), but a missing drivetemp module
    would hide hot disks, so that is an error.
    """
    if not DRIVETEMP_MODULE.exists():
        raise SensorError("drivetemp module is not loaded, disk temperatures are unknown")
    ports = drivetemp_ports(root)
    return {f"bay{port.removeprefix('ata')}": hwmon for port, hwmon in sorted(ports.items())}


def _driver_sensors(source: Source, root: Path) -> dict[str, Path]:
    """Every hwmon of the driver, e.g. one spd5118 per DDR5 module.

    A single instance keeps the plain "{driver}/tempN" label; several are told apart by
    their device ("spd5118@0-0050/temp1"). None at all is an error unless the source is
    optional (hardware that may not be fitted).
    """
    found = find_hwmon(source.driver, root)
    if not found:
        if source.optional:
            return {}
        raise SensorError(f"no hwmon named '{source.driver}' found")
    if len(found) == 1:
        return {f"{source.driver}/temp{source.channel}": found[0]}
    return {f"{source.driver}@{_device_name(hwmon)}/temp{source.channel}": hwmon for hwmon in found}


def _device_name(hwmon: Path) -> str:
    try:
        return (hwmon / "device").resolve(strict=True).name
    except OSError:
        return hwmon.name
