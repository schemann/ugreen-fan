"""A throwaway /sys/class/hwmon tree for tests."""

from pathlib import Path


class FakeSysfs:
    def __init__(self, root: Path):
        self.root = root
        self.next_index = 0

    def add(self, name: str, files: dict[str, object] | None = None,
            block: str | None = None, index: int | None = None,
            port: str | None = None, device: str | None = None) -> Path:
        if index is None:
            index = self.next_index
        self.next_index = max(self.next_index, index) + 1
        hwmon = self.root / f"hwmon{index}"
        hwmon.mkdir(parents=True)
        (hwmon / "name").write_text(f"{name}\n")
        for file, value in (files or {}).items():
            (hwmon / file).write_text(f"{value}\n")
        link = hwmon / "device"
        if port:
            # like /sys/devices/.../ata1/host0/target0:0:0/0:0:0:0
            target = self.root / "devices" / port / f"host{index}" / f"{index}:0:0:0"
            target.mkdir(parents=True)
            link.symlink_to(target)
        if device:
            # like /sys/devices/.../i2c-0/0-0050
            target = self.root / "devices" / f"bus{index}" / device
            target.mkdir(parents=True)
            link.symlink_to(target)
        if block:
            (link / "block" / block).mkdir(parents=True)
        return hwmon

    @staticmethod
    def read(hwmon: Path, file: str) -> str:
        return (hwmon / file).read_text().strip()
