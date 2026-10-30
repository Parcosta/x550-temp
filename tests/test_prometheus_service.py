"""Run the example's actual shell commands against temporary files and a fake tool."""
import configparser
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PrometheusServiceTests(unittest.TestCase):
    def test_old_sample_removed_on_success_failure_or_crash(self):
        config = configparser.ConfigParser(interpolation=None)
        config.read(ROOT / "examples/x550-temp-prometheus.service")
        service = config["Service"]
        for status in (0, 1, 42, 127, -15):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                directory = Path(directory)
                target = directory / "x550.prom"
                temporary = directory / "x550.prom.tmp"
                target.write_text("x550_temperature_celsius 99\n")
                temporary.write_text("previous partial output\n")
                tool = directory / "fake-x550-temp"
                if status != 127:
                    action = "kill -TERM $$" if status == -15 else "exit " + str(status)
                    tool.write_text("#!/bin/sh\nprintf 'x550_temperature_celsius 43.82\\n'\n" + action + "\n")
                    tool.chmod(0o755)
                def command(name):
                    return shlex.split(service[name].replace(
                        "/var/lib/node_exporter/textfile_collector", str(directory)).replace(
                        "/usr/local/bin/x550-temp", str(tool)))
                subprocess.run(command("ExecStartPre"), check=True)
                self.assertFalse(target.exists())
                self.assertFalse(temporary.exists())
                completed = subprocess.run(command("ExecStart"), capture_output=True, text=True)
                subprocess.run(command("ExecStopPost"), check=True)
                if status == 0:
                    self.assertEqual(completed.returncode, 0)
                    self.assertEqual(target.read_text(), "x550_temperature_celsius 43.82\n")
                else:
                    self.assertNotEqual(completed.returncode, 0)
                    self.assertFalse(target.exists(), "failed collection retained old temperature")
                self.assertFalse(temporary.exists())


if __name__ == "__main__":
    unittest.main()
