"""Health check verdict for the hourly TrueNAS Cron Job."""

from typing import Any

STALE_INTERVALS = 3
# it87 reports manual mode as 1, or as 0 ("full speed") when the duty is 255
REGULATED_MODES = (0, 1)


def diagnose(*, module_loaded: bool, service_active: bool, state: dict[str, Any] | None,
             pwm_enable: dict[int, int | None], now: float, interval: float) -> str | None:
    """pwm_enable maps each configured pwm channel to its pwmN_enable (None: unreadable)."""
    if not module_loaded:
        return ("it87 module is not loaded, fan is on the BIOS curve "
                "(TrueNAS updated? run build.sh, then ugreen-fan load)")
    if not service_active:
        return "ugreen-fan service is not running"
    if state is None:
        return "regulator state file is missing"
    if now - state["updated"] > STALE_INTERVALS * interval:
        return "regulator state is stale, the regulator is stuck"
    if state["mode"] != "normal":
        return f"regulator is in failsafe, fans at full speed: {state['reason']}"
    for pwm, enable in sorted(pwm_enable.items()):
        if enable not in REGULATED_MODES:
            return f"pwm{pwm} is not under manual control (pwm_enable={enable})"
    for _, fan in sorted(state.get("fans", {}).items()):
        if fan["rpm"] == 0:
            return f"fan{fan['fan']} reports 0 RPM, check the fan"
    return None
