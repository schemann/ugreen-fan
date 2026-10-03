"""Loading and validation of config.toml."""

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .curve import Curve

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
class Config:
    supported_models: tuple[str, ...]
    module_params: str
    chip: str
    pwm: int
    fan: int
    interval: float
    hysteresis: float
    min_pwm: int
    truenas_alert: bool
    sources: tuple[Source, ...]


def load_config(path: Path) -> Config:
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"{path} not found: copy config.example.toml to config.toml") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from e
    return parse_config(data)


def parse_config(data: dict[str, Any]) -> Config:
    try:
        config = Config(
            supported_models=tuple(str(m) for m in data["supported_models"]),
            module_params=str(data.get("module_params", "")),
            chip=str(data["chip"]),
            pwm=int(data["pwm"]),
            fan=int(data["fan"]),
            interval=float(data.get("interval", 10)),
            hysteresis=float(data.get("hysteresis", 2)),
            min_pwm=int(data.get("min_pwm", 0)),
            truenas_alert=bool(data.get("truenas_alert", True)),
            sources=tuple(_parse_source(name, raw) for name, raw in data["sources"].items()),
        )
    except KeyError as e:
        raise ConfigError(f"missing key {e}") from e
    except (TypeError, ValueError, AttributeError) as e:
        raise ConfigError(f"invalid value: {e}") from e
    _validate(config)
    return config


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
