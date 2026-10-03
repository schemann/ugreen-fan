"""Control loop: temperatures → PWM, with failsafe, state file and systemd watchdog."""

import json
import logging
import os
import signal
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .config import Config
from .curve import next_pwm
from .hwmon import HWMON_ROOT, Fan, SensorError, find_chip, set_all_manual
from .readings import read_source

FAILSAFE_PWM = 255
STALL_INTERVALS = 2  # a step taking longer than this many intervals is hung

log = logging.getLogger(__name__)


@dataclass
class State:
    mode: str
    reason: str | None
    fans: dict[str, dict[str, Any]]  # "pwmN" -> {"fan": tachometer, "pwm": duty, "rpm": RPM or None}
    temps: dict[str, float]
    updated: float


def fans_for(config: Config, chip: Path) -> list[Fan]:
    return [Fan(chip, spec.pwm, spec.fan) for spec in config.fans]


class Regulator:
    def __init__(self, config: Config, fans: list[Fan], root: Path = HWMON_ROOT,
                 clock: Callable[[], float] = time.time):
        self.config = config
        self.fans = fans
        self.root = root
        self.clock = clock
        self.levels = {source.name: FAILSAFE_PWM for source in config.sources}

    def step(self) -> State:
        try:
            temps = self._compute()
        except SensorError as e:
            self.levels = dict.fromkeys(self.levels, FAILSAFE_PWM)
            set_all_manual(self.fans, FAILSAFE_PWM)
            duties = [FAILSAFE_PWM] * len(self.fans)
            return State("failsafe", str(e), self._report(duties), {}, self.clock())
        duties = [max(self.config.min_pwm, *(self.levels[name] for name in spec.sources))
                  for spec in self.config.fans]
        for fan, duty in zip(self.fans, duties):
            fan.set_manual(duty)
        return State("normal", None, self._report(duties), temps, self.clock())

    def _compute(self) -> dict[str, float]:
        temps: dict[str, float] = {}
        levels = dict(self.levels)
        for source in self.config.sources:
            readings = read_source(source, self.root)
            if not readings:
                levels[source.name] = 0
                continue
            levels[source.name] = next_pwm(source.curve, max(readings.values()),
                                           levels[source.name], self.config.hysteresis)
            temps.update(readings)
        self.levels = levels
        return temps

    def _report(self, duties: list[int]) -> dict[str, dict[str, Any]]:
        return {f"pwm{fan.pwm}": {"fan": fan.fan, "pwm": duty, "rpm": _rpm(fan)}
                for fan, duty in zip(self.fans, duties)}


def _rpm(fan: Fan) -> int | None:
    try:
        return fan.rpm()
    except SensorError:
        return None


class StallGuard:
    """Forces full speed if a control step hangs, e.g. in the kernel on a failing disk.

    A process stuck in uninterruptible I/O cannot be killed by the systemd watchdog,
    so ExecStopPost would never run; this guard writes to the fan chip from another thread.
    """

    def __init__(self, fans: list[Fan], limit: float, clock: Callable[[], float] = time.monotonic):
        self.fans = fans
        self.limit = limit
        self.clock = clock
        self.last = clock()
        self.tripped = False

    def beat(self) -> None:
        self.last = self.clock()
        self.tripped = False

    def poll(self) -> bool:
        if self.tripped or self.clock() - self.last <= self.limit:
            return False
        set_all_manual(self.fans, FAILSAFE_PWM)
        self.tripped = True
        return True


def _watch_stalls(guard: StallGuard, stop: threading.Event) -> None:
    while not stop.wait(1):
        try:
            if guard.poll():
                log.error("Control step hung for over %gs, fans forced to %d", guard.limit, FAILSAFE_PWM)
        except SensorError:
            log.exception("Stall guard could not set failsafe PWM")


def write_state(state: State, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(state)))
    os.replace(tmp, path)


def read_state(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def sd_notify(message: str) -> None:
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return
    if address.startswith("@"):
        address = "\0" + address[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        sock.sendto(message.encode(), address)


def run(config: Config, state_path: Path, root: Path = HWMON_ROOT) -> int:
    fans = fans_for(config, find_chip(config.chip, root))
    regulator = Regulator(config, fans, root)
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())

    guard = StallGuard(fans, STALL_INTERVALS * config.interval)
    threading.Thread(target=_watch_stalls, args=(guard, stop), daemon=True).start()

    log.info("Regulating %s %s every %gs", config.chip,
             ", ".join(f"pwm{spec.pwm}" for spec in config.fans), config.interval)
    previous_mode = None
    try:
        while not stop.is_set():
            state = regulator.step()
            guard.beat()
            if state.mode != previous_mode:
                if state.mode == "failsafe":
                    log.warning("Failsafe, fans at %d: %s", FAILSAFE_PWM, state.reason)
                else:
                    log.info("Normal mode, pwm %s, temps %s",
                             {name: fan["pwm"] for name, fan in state.fans.items()}, state.temps)
                previous_mode = state.mode
            write_state(state, state_path)
            sd_notify("READY=1\nWATCHDOG=1")
            stop.wait(config.interval)
    finally:
        try:
            set_all_manual(fans, FAILSAFE_PWM)
        except SensorError:
            log.exception("Could not set failsafe PWM on exit")
    log.info("Stopped, fans left at %d", FAILSAFE_PWM)
    return 0
