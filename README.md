# ugreen-fan

Fan control for the **UGREEN DXP4800** and **DXP4800 Pro** running **TrueNAS SCALE**,
driven by **hard-drive temperature** (with the CPU, and on the Pro the RAM and the
boot NVMe, as safety nets).

Out of the box the fans are run by the BIOS curve inside the Super-I/O chip, and that
curve follows only the CPU temperature. The disks, the thing you actually care
about in a NAS, are ignored. ugreen-fan takes the fans over from the OS and
regulates it by the hottest drive, with a hard failsafe to full speed.

> [!WARNING]
> This loads a self-built, out-of-tree kernel module. It taints the kernel and is
> not supported by iXsystems. Tested on a DXP4800 with TrueNAS SCALE 25.10.3; the
> DXP4800 Pro preset is based on measurements on TrueNAS SCALE 25.10.7.
> Use at your own risk.

## How it works

- The fan controller is an **ITE IT8613E** Super-I/O. Mainline `it87` does not know
  it, so the [frankcrawford/it87](https://github.com/frankcrawford/it87) fork is built
  for the running TrueNAS kernel. It is loaded with `ignore_resource_conflict=1`
  because ACPI reserves the chip's I/O range (the ACPI tables never access it).
- The DXP4800 has one fan, `pwm3` / `fan3`. The DXP4800 Pro has two: `pwm2` / `fan2`
  and `pwm3` / `fan3`. Every configured fan is regulated, and every failsafe path
  acts on all of them.
- At boot a TrueNAS **Init script (POSTINIT)** runs `ugreen-fan load`: it loads the
  module and starts the regulator as a transient systemd unit (the root filesystem
  is read-only, so everything lives on your pool and in `/run`).
- An hourly TrueNAS **Cron Job** runs `ugreen-fan check` and alerts you when
  something is wrong.

## Requirements

- UGREEN DXP4800 or DXP4800 Pro with TrueNAS SCALE
- Docker for the build: either on the NAS itself (an apps pool configured), or any
  x86-64 Docker host reachable over SSH. The NAS ships the `docker` CLI even without
  Apps, and `build.sh` uses no bind mounts (headers go in and the module comes out
  through the docker stream), so `DOCKER_HOST=ssh://user@buildhost` works. The build
  host needs internet access. The container is pinned to `linux/amd64` (TrueNAS SCALE
  is amd64 only, and the module's vermagic does not record the architecture), so an
  arm64 build host needs amd64 emulation; `build.sh` refuses to run on a non-x86_64 NAS.
- SSH access with sudo

## Install

```sh
sudo git clone https://github.com/tarasverq/ugreen-fan /mnt/<pool>/apps/ugreen-fan
cd /mnt/<pool>/apps/ugreen-fan
sudo ./build.sh              # builds modules/<kernel>/it87.ko in a Debian container
# or, without Apps on the NAS (root's SSH key must be authorised on the build host):
# sudo DOCKER_HOST=ssh://user@buildhost ./build.sh
sudo bin/ugreen-fan install  # creates config.toml from the model's preset, registers Init script + Cron Job, starts
sensors it8613-isa-0a30
bin/ugreen-fan status
```

## Configuration

Without an existing `config.toml`, `install` copies the preset whose
`supported_models` contains the DMI product name
(`/sys/class/dmi/id/product_name`):

| Preset | Model |
|---|---|
| `presets/dxp4800.toml` | DXP4800 |
| `presets/dxp4800-pro.toml` | DXP4800 Pro |

If no preset matches, it copies `presets/dxp4800.toml` and logs a warning; check
`chip`, `[[fans]]` and the sources before relying on it. Edit `config.toml` and run
`sudo bin/ugreen-fan load` to apply.

| Key | Meaning |
|---|---|
| `supported_models` | DMI product names allowed to load the module |
| `module_params` | parameters for `insmod` |
| `chip` | hwmon name of the fan controller |
| `[[fans]]` | one table per fan: `pwm` (PWM channel), `fan` (tachometer), optional `sources` (source names driving it, default all) and optional `min_pwm` (overrides the global floor for this fan) |
| `pwm`, `fan` | legacy form: one fan driven by every source, instead of `[[fans]]` |
| `interval` | seconds between control steps (max 10, the watchdog is 30 s) |
| `hysteresis` | °C the temperature must drop before the fan slows down |
| `min_pwm` | lowest PWM ever written, keeps the fans spinning; a fan's own `min_pwm` overrides it |
| `truenas_alert` | raise a bell alert on problems (see [Alerts](#alerts)) |
| `[sources.*]` | temperature inputs, each with its own `curve` and `valid` range |

Each source maps its hottest reading through a piecewise-linear `curve` of
`[temperature, pwm]` points. With hysteresis the level for a source is

```
max(curve(T), min(current, curve(T + hysteresis)))
```

so it rises immediately and falls only once the temperature has dropped by
`hysteresis` degrees. Each fan gets the highest level over its sources, never below
`min_pwm`.

```toml
[[fans]]
pwm = 2
fan = 2                       # follows every source

[[fans]]
pwm = 3
fan = 3
sources = ["disks", "cpu"]    # only these
```

Use either `[[fans]]` or the top-level `pwm` + `fan` keys (older configs keep
working unchanged), not both. An unknown source name, an empty `sources` list or
the same `pwm` channel twice is a config error.

The DXP4800 preset has two sources: **disks** (every SATA drive, found by its ATA port and
labelled `bayN` from `ataN`, so USB disks are skipped; 42 °C → 60 … 55 °C → 255;
with all bays empty it adds nothing and the CPU curve drives the fan) and **cpu** (`temp1` of the chip, PECI,
60 °C → 51 … 90 °C → 255). The CPU source exists because this is the only fan in the
case: driving it by the disks alone would leave the CPU uncooled under load.

The DXP4800 Pro preset drives both fans by the same disks and cpu sources plus two
more:

- **ram** (`spd5118`, the DDR5 sensor, one hwmon per module; 50 °C → 51 … 70 °C →
  255). The SO-DIMMs sit stacked: under Memtest the RAM went from 59 to 80 °C in 10
  minutes and the box froze, twice. That is why the RAM drives the fans directly.
- **nvme** (`nvme`, `temp1` = "Composite" of the boot NVMe; 50 °C → 51 … 75 °C →
  255), `optional = true` so a box without an NVMe drive runs normally. The measured
  drive (TWSC TSC3AN128) reports `temp1_crit` 94.85 °C; other drives differ, so check
  yours and adjust the curve.

A source has these keys:

| Key | Meaning |
|---|---|
| `driver` | hwmon name to read; `drivetemp` means every SATA drive in the bays |
| `channel` | `tempN_input` to read (default 1) |
| `valid` | `[low, high]`; a reading outside it sends the fans to failsafe |
| `curve` | `[temperature, pwm]` points |
| `optional` | `true`: no hwmon of this driver is fine, the source then adds nothing (default `false`, which treats a missing sensor as a failure) |

Every hwmon with the driver's name is read, sorted, and each reading is checked
against `valid`. One instance is labelled `{driver}/temp{channel}` (`it8613/temp1`);
several, such as one `spd5118` per DDR5 module, are labelled by their device:
`spd5118@0-0050/temp1`, `spd5118@0-0051/temp1`.

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
| module not loaded at boot | the fans stay on the BIOS curve; `check` alerts you |

`run` and `ExecStopPost` get the chip and every PWM channel on their command line,
so the failsafe works even if `config.toml` is broken, and the regulator refuses to
start if `config.toml` was changed to another chip or set of channels without
re-running `load`. On the Pro the unit runs

```
ExecStart="/mnt/<pool>/apps/ugreen-fan/bin/ugreen-fan" run --chip it8613 --pwm 2 --pwm 3
ExecStopPost="/mnt/<pool>/apps/ugreen-fan/bin/ugreen-fan" failsafe --chip it8613 --pwm 2 --pwm 3
```

`load` saves the BIOS start PWM of every fan still on the BIOS curve to
`/run/ugreen-fan/bios_pwm` (JSON, e.g. `{"2": 51, "3": 51}`; an entry is never
overwritten). A fan whose `[[fans]]` table was removed is handed back to the BIOS
curve with its saved value by the next `load` and dropped from the file. `restore`
hands every configured or saved fan back, and refuses before changing anything if a
configured fan has no saved value (reboot to let the BIOS re-initialise the fan
controller); if a fan cannot be written, the file and the module stay so you can
retry. A file from an older version holding a single number is rewritten as JSON on
the next `load`: fans still on the BIOS curve get their real start PWM, the others
keep the old number. A corrupt file is left untouched (the regulator still starts,
`restore` refuses with the reboot advice).

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

1. Update and reboot. The fans run on the BIOS curve; the Cron Job alerts you within
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
| `restore` | stop the regulator and hand the fans back to the BIOS curve |
| `check` | health check used by the Cron Job |
| `status` | print the regulator state (temperatures, PWM and RPM per fan, mode) |
| `run --chip --pwm [--pwm ...]` | the regulator itself (started by systemd) |
| `failsafe --chip --pwm [--pwm ...]` | set the fans to full speed (used by systemd) |

`status` prints `/run/ugreen-fan/state.json`:

```json
{
  "mode": "normal",
  "reason": null,
  "fans": {
    "pwm2": {"fan": 2, "pwm": 90, "rpm": 1350},
    "pwm3": {"fan": 3, "pwm": 90, "rpm": 1010}
  },
  "temps": {"bay1": 41.0, "it8613/temp1": 52.0, "spd5118/temp1": 54.5, "nvme/temp1": 45.85},
  "updated": 1791000000.0
}
```

`rpm` is `null` when the tachometer cannot be read. `check` reports a fan at 0 RPM
or a PWM channel that is not under manual control by name (`fan3`, `pwm2`).

## Uninstall

```sh
sudo bin/ugreen-fan uninstall
sudo rm -rf /mnt/<pool>/apps/ugreen-fan
```

The BIOS curve is restored and the module unloaded. Nothing is ever written to the
BIOS or firmware; a reboot also returns everything to stock.

## Other UGREEN models

Not supported - only the DXP4800 and DXP4800 Pro have presets. Other models
probably work the same way, and you can adapt this yourself:

1. Identify the Super-I/O chip: `sensors-detect`, or read the ID registers at
   ports `0x2E`/`0x4E`. Check that the frankcrawford/it87 fork supports it.
2. Build and load the module (`sudo ./build.sh`, then `sudo modprobe hwmon-vid` and
   `sudo insmod modules/$(uname -r)/it87.ko ignore_resource_conflict=1`; insmod does not
   load the `hwmon-vid` dependency itself).
3. Find the fan channels: every `fanN_input` with a non-zero RPM.
4. Test `pwmN` by hand. **Note the original `pwmN` value first**: on ITE chips the
   manual duty register doubles as the start PWM of the BIOS curve, so to give control
   back you must write the original value and then `pwmN_enable=2`.
5. Put your model name (`cat /sys/class/dmi/id/product_name`), `chip` and one
   `[[fans]]` table per fan into `config.toml` and run `sudo bin/ugreen-fan install`.

Pull requests with verified presets (`presets/<model>.toml`) are welcome.

## Development

```sh
python3 -m unittest discover -s tests -t .
bash -n build.sh && shellcheck build.sh bin/ugreen-fan
```

GitHub Actions (`.github/workflows/tests.yml`) runs the same on every push and pull
request, with Python 3.11 (what TrueNAS 25.10 ships), 3.12 and 3.13.

Python 3.11 standard library only — TrueNAS has no pip.

## Credits

- [frankcrawford/it87](https://github.com/frankcrawford/it87) — the driver
- [schemann](https://github.com/schemann) — DXP4800 Pro preset and multi-fan support
- [rw-martin/UGREEN-DXP4800-Fan-Curve](https://github.com/rw-martin/UGREEN-DXP4800-Fan-Curve) — BIOS fan curve settings
- [TrueNAS forum: UGREEN NAS DXP4800 fan control scripts](https://forums.truenas.com/t/ugreen-nas-dxp4800-fan-control-scripts/67587)
