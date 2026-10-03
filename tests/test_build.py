import subprocess
import unittest
from pathlib import Path

BUILD = Path(__file__).resolve().parent.parent / "build.sh"


class BuildScriptTest(unittest.TestCase):
    """build.sh needs a TrueNAS kernel and Docker; these checks guard its shape."""

    def setUp(self):
        self.text = BUILD.read_text()

    def test_syntax(self):
        subprocess.run(["bash", "-n", str(BUILD)], check=True)

    def test_no_bind_mounts_so_a_remote_docker_host_works(self):
        self.assertNotIn(" -v ", self.text)
        self.assertNotIn("--volume", self.text)
        self.assertIn("tar -C / -czf - usr/src | docker run --rm -i", self.text)

    def test_module_is_the_only_stdout_of_the_container(self):
        self.assertIn("exec 3>&1 1>&2", self.text)
        self.assertIn("cat /src/it87.ko >&3", self.text)

    def test_vermagic_rechecked_on_the_host_before_install(self):
        host = self.text.split("' >\"$tmp\"", 1)[1]
        self.assertLess(host.index('modinfo -F vermagic "$tmp"'), host.index('mv "$tmp" "$out/it87.ko"'))

    def test_temp_file_removed_on_failure(self):
        self.assertIn("set -euo pipefail", self.text)
        self.assertIn("trap 'rm -f \"$tmp\"' EXIT", self.text)
