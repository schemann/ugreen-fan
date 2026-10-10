"""Loading and validation of config.toml."""

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .curve import Curve

I2C_MIN, I2C_MAX = 0x03, 0x77  # usable 7-bit i2c addresses (the rest is reserved)
MAX_INTERVAL = 10  # the systemd watchdog is 30 s; three missed steps kill the service


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Source:
    name: str
    driver: str
    channel: int
    valid: tuple[float, float]
    curve: Curve
    optional: bool = False  # no hwmon of this driver is fine (hardware that may not be fitted)


@dataclass(frozen=True)
class FanSpec:
    pwm: int                    # pwmN that drives the fan
    fan: int                    # fanN_input with its tachometer
    sources: tuple[str, ...]    # names of the sources that drive it
    min_pwm: int | None = None  # per-fan floor, overrides the global min_pwm


@dataclass(frozen=True)
class I2cDevice:
    adapter: str                # adapter name as in /sys/bus/i2c/devices/i2c-N/name
    driver: str                 # kernel driver to modprobe and bind, e.g. "spd5118"
    addresses: tuple[int, ...]  # 7-bit addresses to probe


@dataclass(frozen=True)
class Config:
    supported_models: tuple[str, ...]
    module_params: str
    chip: str
    fans: tuple[FanSpec, ...]
    interval: float
    hysteresis: float
    min_pwm: int
    truenas_alert: bool
    sources: tuple[Source, ...]
    i2c_devices: tuple[I2cDevice, ...] = ()


def load_config(path: Path) -> Config:
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"{path} not found: run 'ugreen-fan install' or copy a preset from presets/") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from e
    return parse_config(data)


def find_preset(model: str, presets: list[Path]) -> Path | None:
    """The first preset whose supported_models lists `model`."""
    for path in presets:
        if model in load_config(path).supported_models:
            return path
    return None


def parse_config(data: dict[str, Any]) -> Config:
    try:
        sources = tuple(_parse_source(name, raw) for name, raw in data["sources"].items())
        config = Config(
            supported_models=tuple(str(m) for m in data["supported_models"]),
            module_params=str(data.get("module_params", "")),
            chip=str(data["chip"]),
            fans=_parse_fans(data, tuple(s.name for s in sources)),
            interval=float(data.get("interval", 10)),
            hysteresis=float(data.get("hysteresis", 2)),
            min_pwm=int(data.get("min_pwm", 0)),
            truenas_alert=bool(data.get("truenas_alert", True)),
            sources=sources,
            i2c_devices=_parse_i2c_devices(data),
        )
    except KeyError as e:
        raise ConfigError(f"missing key {e}") from e
    except (TypeError, ValueError, AttributeError) as e:
        raise ConfigError(f"invalid value: {e}") from e
    _validate(config)
    return config


def _parse_fans(data: dict[str, Any], all_sources: tuple[str, ...]) -> tuple[FanSpec, ...]:
    """[[fans]] tables, or the legacy top-level pwm + fan: one fan driven by every source."""
    legacy = "pwm" in data or "fan" in data
    if legacy and "fans" in data:
        raise ConfigError("use either [[fans]] or the top-level pwm and fan keys, not both")
    if legacy:
        return (FanSpec(int(data["pwm"]), int(data["fan"]), all_sources),)
    if "fans" not in data:
        raise ConfigError("missing [[fans]] (or the top-level pwm and fan keys)")
    if not isinstance(data["fans"], list) or not all(isinstance(raw, dict) for raw in data["fans"]):
        raise ConfigError("fans must be an array of tables: use [[fans]]")
    fans = []
    for raw in data["fans"]:
        names = raw.get("sources", list(all_sources))
        if not isinstance(names, list):
            raise ConfigError(f"fans pwm{raw.get('pwm')}: sources must be a list of source names")
        min_pwm = raw.get("min_pwm")
        fans.append(FanSpec(int(raw["pwm"]), int(raw["fan"]), tuple(str(n) for n in names),
                            None if min_pwm is None else int(min_pwm)))
    return tuple(fans)


def _parse_i2c_devices(data: dict[str, Any]) -> tuple[I2cDevice, ...]:
    raw_devices = data.get("i2c_devices", [])
    if not isinstance(raw_devices, list) or not all(isinstance(raw, dict) for raw in raw_devices):
        raise ConfigError("i2c_devices must be an array of tables: use [[i2c_devices]]")
    devices = []
    for raw in raw_devices:
        for key in ("adapter", "driver"):
            if not isinstance(raw.get(key), str) or not raw[key]:
                raise ConfigError(f"i2c_devices: {key} must be a non-empty string")
        prefix = f"i2c_devices {raw['driver']}"
        addresses = raw.get("addresses")
        # bool is an int subclass: true/false is not an address
        if (not isinstance(addresses, list) or not addresses
                or not all(isinstance(a, int) and not isinstance(a, bool) for a in addresses)):
            raise ConfigError(f"{prefix}: addresses must be a non-empty list of integers")
        if not all(I2C_MIN <= a <= I2C_MAX for a in addresses):
            raise ConfigError(f"{prefix}: addresses must be within 0x{I2C_MIN:02x}..0x{I2C_MAX:02x}")
        devices.append(I2cDevice(raw["adapter"], raw["driver"], tuple(addresses)))
    return tuple(devices)


def _parse_source(name: str, raw: dict[str, Any]) -> Source:
    low, high = raw["valid"]
    optional = raw.get("optional", False)
    if not isinstance(optional, bool):
        raise ConfigError(f"sources.{name}: optional must be true or false")
    return Source(
        name=name,
        driver=str(raw["driver"]),
        channel=int(raw.get("channel", 1)),
        valid=(float(low), float(high)),
        curve=tuple((float(t), int(p)) for t, p in raw["curve"]),
        optional=optional,
    )


def _validate(config: Config) -> None:
    if not config.sources:
        raise ConfigError("at least one [sources.*] section is required")
    if not 0 <= config.min_pwm <= 255:
        raise ConfigError("min_pwm must be within 0..255")
    if not 0 < config.interval <= MAX_INTERVAL:
        raise ConfigError(f"interval must be within (0, {MAX_INTERVAL}] seconds")
    if config.hysteresis < 0:
        raise ConfigError("hysteresis must not be negative")
    for source in config.sources:
        _validate_source(source)
    _validate_fans(config)


def _validate_fans(config: Config) -> None:
    if not config.fans:
        raise ConfigError("at least one [[fans]] table is required")
    names = {source.name for source in config.sources}
    pwms = [fan.pwm for fan in config.fans]
    for fan in config.fans:
        prefix = f"fans pwm{fan.pwm}"
        if not fan.sources:
            raise ConfigError(f"{prefix}: sources is empty")
        unknown = sorted(set(fan.sources) - names)
        if unknown:
            raise ConfigError(f"{prefix}: unknown sources {unknown}")
        if pwms.count(fan.pwm) > 1:
            raise ConfigError(f"{prefix}: the pwm channel is listed more than once")
        if fan.min_pwm is not None and not 0 <= fan.min_pwm <= 255:
            raise ConfigError(f"{prefix}: min_pwm must be within 0..255")


def _validate_source(source: Source) -> None:
    prefix = f"sources.{source.name}"
    if not source.curve:
        raise ConfigError(f"{prefix}: curve is empty")
    temps = [t for t, _ in source.curve]
    pwms = [p for _, p in source.curve]
    if any(b <= a for a, b in zip(temps, temps[1:])):
        raise ConfigError(f"{prefix}: curve temperatures must strictly increase")
    if any(b < a for a, b in zip(pwms, pwms[1:])) or not all(0 <= p <= 255 for p in pwms):
        raise ConfigError(f"{prefix}: curve PWM must be non-decreasing and within 0..255")
    if source.valid[0] >= source.valid[1]:
        raise ConfigError(f"{prefix}: valid must be [low, high] with low < high")
