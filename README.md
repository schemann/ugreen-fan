# ugreen-fan

Fan control for the **UGREEN DXP4800** running **TrueNAS SCALE**, driven by
**hard-drive temperature** (with the CPU as a safety net).

Out of the box the fan is run by the BIOS curve inside the Super-I/O chip, and that
curve follows only the CPU temperature. The disks — the thing you actually care
about in a NAS — are ignored. ugreen-fan takes the fan over from the OS and
regulates it by the hottest drive, with a hard failsafe to full speed.

> [!WARNING]
> This loads a self-built, out-of-tree kernel module. It taints the kernel and is
> not supported by iXsystems. Tested only on a DXP4800 with TrueNAS SCALE 25.10.3.
> Use at your own risk.

## How it works

- The fan controller is an **ITE IT8613E** Super-I/O. Mainline `it87` does not know
  it, so the [frankcrawford/it87](https://github.com/frankcrawford/it87) fork is built
  for the running TrueNAS kernel. It is loaded with `ignore_resource_conflict=1`
  because ACPI reserves the chip's I/O range (the ACPI tables never access it).
- The fan is `pwm3` / `fan3`.
- At boot a TrueNAS **Init script (POSTINIT)** runs `ugreen-fan load`: it loads the
  module and starts the regulator as a transient systemd unit (the root filesystem
  is read-only, so everything lives on your pool and in `/run`).
- An hourly TrueNAS **Cron Job** runs `ugreen-fan check` and alerts you when
  something is wrong.

## Requirements

- UGREEN DXP4800 with TrueNAS SCALE
- Docker for the build: either on the NAS itself (an apps pool configured), or any
  x86-64 Docker host reachable over SSH. The NAS ships the `docker` CLI even without
  Apps, and `build.sh` uses no bind mounts (headers go in and the module comes out
  through the docker stream), so `DOCKER_HOST=ssh://user@buildhost` works. The build
  host needs internet access.
- SSH access with sudo

## Install

```sh
sudo git clone https://github.com/tarasverq/ugreen-fan /mnt/<pool>/apps/ugreen-fan
cd /mnt/<pool>/apps/ugreen-fan
sudo ./build.sh              # builds modules/<kernel>/it87.ko in a Debian container
# or, without Apps on the NAS (root's SSH key must be authorised on the build host):
# sudo DOCKER_HOST=ssh://user@buildhost ./build.sh
sudo bin/ugreen-fan install  # creates config.toml, registers Init script + Cron Job, starts
sensors it8613-isa-0a30
bin/ugreen-fan status
```

## Configuration

`install` copies `config.example.toml` to `config.toml`. Edit it and run
`sudo bin/ugreen-fan load` to apply.

| Key | Meaning |
|---|---|
| `supported_models` | DMI product names allowed to load the module |
| `module_params` | parameters for `insmod` |
| `chip`, `pwm`, `fan` | hwmon name, PWM channel and tachometer of the fan |
| `interval` | seconds between control steps (max 10, the watchdog is 30 s) |
| `hysteresis` | °C the temperature must drop before the fan slows down |
| `min_pwm` | lowest PWM ever written, keeps the fan spinning |
| `truenas_alert` | raise a bell alert on problems (see [Alerts](#alerts)) |
| `[sources.*]` | temperature inputs, each with its own `curve` and `valid` range |

Each source maps its hottest reading through a piecewise-linear `curve` of
`[temperature, pwm]` points. With hysteresis the level for a source is

```
max(curve(T), min(current, curve(T + hysteresis)))
```

so it rises immediately and falls only once the temperature has dropped by
`hysteresis` degrees. The fan gets the highest level over all sources, never below
`min_pwm`.

The preset has two sources: **disks** (every SATA drive — found by its ATA port and
labelled `bayN` from `ataN`, so USB disks are skipped; 42 °C → 60 … 55 °C → 255;
with all bays empty it adds nothing and the CPU curve drives the fan) and **cpu** (`temp1` of the chip, PECI,
60 °C → 51 … 90 °C → 255). The CPU source exists because this is the only fan in the
case: driving it by the disks alone would leave the CPU uncooled under load.

## Failsafe

Every failure ends at **PWM 255, manual mode**:

| Situation | Who sets 255 |
|---|---|
| temperature unreadable, out of `valid`, `drivetemp` module not loaded | the regulator (keeps running and recovers when readings return) |
| uncaught exception | the regulator, then systemd `ExecStopPost` |
| process killed (`kill -9`) or crashed | `ExecStopPost`, then `Restart=always` |
| process hangs | systemd watchdog kills it → `ExecStopPost` |
| a step is stuck in the kernel (e.g. a failing disk), unkillable | a guard thread inside the regulator, after 2 × `interval` |
| **whole OS hangs** | **not covered** — the chip keeps the last value, it has no PWM watchdog |
| module not loaded at boot | fan stays on the BIOS curve; `check` alerts you |

`run` and `ExecStopPost` get the chip and channel on their command line, so the
failsafe works even if `config.toml` is broken, and the regulator refuses to start if
`config.toml` was changed to another channel without re-running `load`.

## Alerts

The Cron Job runs `ugreen-fan check` hourly. It is silent when everything is fine.
On a problem it prints the reason and exits 1, so TrueNAS marks the job as failed
and e-mails the output to root's address (if e-mail is configured).

TrueNAS SCALE has no way to raise a custom alert from a script: alert classes live
in the middleware code on the read-only root filesystem. As a workaround,
ugreen-fan **borrows the built-in one-shot class `ApplicationsStartFailed`**, whose
text takes a free-form argument. The alert then reaches the bell and every alert
service you configured (e-mail, Telegram, Slack, …). Side effects:

- it shows up under **Applications** with level **CRITICAL**;
- a successful Docker start clears every alert of that class, including ours — the
  next hourly check raises it again;
- when the problem is gone, `check` removes the alert, unless a genuine Docker alert
  of the same class is also present (both are then cleared by Docker).

Set `truenas_alert = false` to disable the bell alert and keep only the Cron Job.

## After every TrueNAS update

A new TrueNAS release usually ships a new kernel, and the module has to be rebuilt:

1. Update and reboot. The fan runs on the BIOS curve; the Cron Job alerts you within
   an hour.
2. `cd /mnt/<pool>/apps/ugreen-fan && sudo ./build.sh && sudo bin/ugreen-fan load`
   (with a remote build host: `sudo DOCKER_HOST=ssh://user@buildhost ./build.sh`).
   The module is built against the NAS's own `/usr/src` headers and its vermagic is
   checked in the container and again on the NAS before it lands in `modules/`.

Modules are kept per kernel in `modules/<kernel>/`, so booting an older boot
environment keeps working.

## Commands

| Command | What it does |
|---|---|
| `install [--force]` | create `config.toml`, register Init script and Cron Job, `load` |
| `uninstall` | `restore`, then remove the Init script and Cron Job |
| `load [--force]` | load the module, start the regulator (`--force` skips the model check) |
| `restore` | stop the regulator and hand the fan back to the BIOS curve |
| `check` | health check used by the Cron Job |
| `status` | print the regulator state (temperatures, PWM, RPM, mode) |
| `run --chip --pwm` | the regulator itself (started by systemd) |
| `failsafe --chip --pwm` | set the fan to full speed (used by systemd) |

## Uninstall

```sh
sudo bin/ugreen-fan uninstall
sudo rm -rf /mnt/<pool>/apps/ugreen-fan
```

The BIOS curve is restored and the module unloaded. Nothing is ever written to the
BIOS or firmware; a reboot also returns everything to stock.

## Other UGREEN models

Not supported — only the DXP4800 has been tested. Other models probably work the
same way, and you can adapt this yourself:

1. Identify the Super-I/O chip: `sensors-detect`, or read the ID registers at
   ports `0x2E`/`0x4E`. Check that the frankcrawford/it87 fork supports it.
2. Build and load the module (`sudo ./build.sh`, then
   `sudo insmod modules/$(uname -r)/it87.ko ignore_resource_conflict=1`).
3. Find the fan channel: the `fanN_input` with a non-zero RPM.
4. Test `pwmN` by hand. **Note the original `pwmN` value first**: on ITE chips the
   manual duty register doubles as the start PWM of the BIOS curve, so to give control
   back you must write the original value and then `pwmN_enable=2`.
5. Put your model name (`cat /sys/class/dmi/id/product_name`), `chip`, `pwm` and
   `fan` into `config.toml` and run `sudo bin/ugreen-fan install`.

Pull requests with verified presets are welcome.

## Development

```sh
python3 -m unittest discover -s tests -t .
```

Python 3.11 standard library only — TrueNAS has no pip.

## Credits

- [frankcrawford/it87](https://github.com/frankcrawford/it87) — the driver
- [rw-martin/UGREEN-DXP4800-Fan-Curve](https://github.com/rw-martin/UGREEN-DXP4800-Fan-Curve) — BIOS fan curve settings
- [TrueNAS forum: UGREEN NAS DXP4800 fan control scripts](https://forums.truenas.com/t/ugreen-nas-dxp4800-fan-control-scripts/67587)
