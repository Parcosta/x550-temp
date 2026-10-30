"""Hardware-free regression tests: no NIC discovery, sockets, or ioctls reach the host."""
import contextlib
import errno
import importlib.machinery
import importlib.util
import io
import json
from pathlib import Path
import struct
import subprocess
import sys
import unittest
from unittest.mock import Mock, mock_open, patch

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "x550-temp"
loader = importlib.machinery.SourceFileLoader("x550_temp", str(TOOL))
spec = importlib.util.spec_from_loader(loader.name, loader)
x550 = importlib.util.module_from_spec(spec)
loader.exec_module(x550)


def result(celsius=43.82, pci="0000:05:00"):
    return {"pci": pci, "interfaces": ["eth0", "eth1"], "celsius": celsius,
            "read_via": "eth0" if celsius is not None else None,
            "error": None if celsius is not None else "sensor not ready"}


class ReadTests(unittest.TestCase):
    def test_swapped_and_straight_encoding_through_ioctl(self):
        for swapped in (True, False):
            with self.subTest(swapped=swapped):
                requests = []
                def ioctl(fd, operation, request):
                    self.assertEqual(operation, x550.SIOCGMIIREG)
                    iface, phy, reg, _, _ = struct.unpack("16sHHHH8x", request)
                    self.assertEqual(iface.rstrip(b"\0"), b"eth0")
                    requests.append((phy, reg))
                    prtad, devad = (phy >> 5) & 31, phy & 31
                    address, mmd = (devad, prtad) if swapped else (prtad, devad)
                    value = 0
                    if address == 0:
                        value = {(1, 2): 0x0154, (30, 0xC821): 1,
                                 (30, 0xC820): 11218}.get((mmd, reg), 0)
                    return struct.pack("16sHHHH8x", iface, phy, reg, 0, value)
                with patch.object(x550.fcntl, "ioctl", side_effect=ioctl):
                    self.assertEqual(x550.read_chip(Mock(), ["eth0"]), (43.82, "eth0"))
                expected = [(0x8020, 2)]
                if not swapped:
                    expected.append((0x8001, 2))
                thermal_phy = 0x83C0 if swapped else 0x801E
                expected += [(thermal_phy, 0xC821), (thermal_phy, 0xC820)]
                self.assertEqual(requests, expected)

    def test_wrong_phy_never_reads_thermal_registers(self):
        with patch.object(x550, "_mdio_read", return_value=0xFFFF) as read:
            self.assertEqual(x550.read_chip(Mock(), ["eth0"]),
                             (None, "unexpected PHY identifier"))
        self.assertTrue(all(call.args[-1] == 2 for call in read.call_args_list))

    def test_not_ready_never_reads_temperature(self):
        def read(sock, iface, prtad, devad, reg):
            return 0x0154 if reg == 2 else 0
        with patch.object(x550, "_mdio_read", side_effect=read) as mdio:
            self.assertEqual(x550.read_chip(Mock(), ["eth0"]), (None, "sensor not ready"))
        self.assertNotIn(x550.REG_TEMP, [call.args[-1] for call in mdio.call_args_list])

    def test_ioctl_failure_and_permission_failure(self):
        for error, reason in [(OSError(errno.EIO, "Input/output error"),
                               "ioctl failed: Input/output error"),
                              (OSError(errno.EBUSY, "Device or resource busy"),
                               "ioctl failed: Device or resource busy"),
                              (PermissionError(errno.EPERM, "Operation not permitted"),
                               "permission denied (requires root or CAP_NET_ADMIN)")]:
            with self.subTest(error=error), patch.object(x550.fcntl, "ioctl", side_effect=error):
                self.assertEqual(x550.read_chip(Mock(), ["eth0"]), (None, reason))

    def test_signed_temperature_and_zero_with_ready_bit(self):
        for raw, expected in [(0x2D00, 45.0), (0x2D80, 45.5), (0xD800, -40.0),
                              (0x0000, 0.0), (0xFF00, -1.0), (0xF580, -10.5),
                              (0x8000, -128.0), (0x7FFF, 128.0)]:
            with self.subTest(raw=raw), patch.object(x550, "_mdio_read", side_effect=[0x0154, 1, raw]):
                self.assertEqual(x550.read_chip(Mock(), ["eth0"]), (expected, "eth0"))

    def test_readiness_failure_preserved_when_other_encoding_mismatches(self):
        with patch.object(x550, "_mdio_read", side_effect=[0x0154, 0, 0xFFFF]):
            self.assertEqual(x550.read_chip(Mock(), ["eth0"]), (None, "sensor not ready"))

    def test_discovery_groups_ports_and_filters_driver_and_device(self):
        def readlink(path):
            if path.endswith("/driver"):
                return "/drivers/other" if "eth2/" in path else "/drivers/ixgbe"
            return "/devices/0000:05:00." + ("1" if "eth1/" in path else "0")
        def opened(path):
            value = "0x8086" if path.endswith("/vendor") else ("0xFFFF" if "eth3/" in path else "0x1563")
            return mock_open(read_data=value)()
        with patch.object(x550.os, "listdir", return_value=["eth3", "eth2", "eth1", "eth0"]), \
             patch.object(x550.os, "readlink", side_effect=readlink), \
             patch("builtins.open", side_effect=opened):
            self.assertEqual(x550.ixgbe_interfaces(), {"0000:05:00": ["eth0", "eth1"]})

    def test_pci_allowlist_excludes_related_phys_and_virtual_functions(self):
        # All fake devices advertise the same PHY high word. Only the documented
        # Intel X550 PCI IDs may reach read_chip/ioctl at all.
        for vendor, device, supported in [("0x8086", "0x1563", True),
                                          ("0x8086", "0x15d1", True),
                                          ("0x8086", "0x1528", False),  # X540-T2
                                          ("0x8086", "0x1560", False),  # X540-T1
                                          ("0x8086", "0x15ad", False),  # X550EM-X
                                          ("0x8086", "0x15c8", False),  # X550EM-A
                                          ("0x8086", "0x10fb", False),  # 82599
                                          ("0x8086", "0x1565", False),  # X550 VF
                                          ("0x1234", "0x1563", False)]:
            with self.subTest(vendor=vendor, device=device):
                def opened(path):
                    return mock_open(read_data=vendor if path.endswith("/vendor") else device)()
                def readlink(path):
                    return "/drivers/ixgbe" if path.endswith("/driver") else "/devices/0000:05:00.0"
                with patch.object(x550.os, "listdir", return_value=["eth0"]), \
                     patch.object(x550.os, "readlink", side_effect=readlink), \
                     patch("builtins.open", side_effect=opened), \
                     patch.object(x550.socket, "socket"), \
                     patch.object(x550, "_mdio_read", side_effect=[0x0154, 1, 0x2D00]) as mdio:
                    readings = x550.read_all()
                if supported:
                    self.assertEqual(readings[0]["celsius"], 45.0)
                    self.assertEqual(mdio.call_count, 3)
                else:
                    self.assertEqual(readings, [])
                    mdio.assert_not_called()

    def test_read_all_closes_socket_and_reports_failed_chip(self):
        sock = Mock()
        sock.__enter__ = Mock(return_value=sock)
        sock.__exit__ = Mock()
        with patch.object(x550, "ixgbe_interfaces", return_value={"0000:05:00": ["eth0"]}), \
             patch.object(x550.socket, "socket", return_value=sock), \
             patch.object(x550, "read_chip", return_value=(None, "sensor not ready")):
            self.assertEqual(x550.read_all()[0]["error"], "sensor not ready")
        sock.__exit__.assert_called_once()


class OutputTests(unittest.TestCase):
    def run_main(self, args, results):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(x550, "read_all", return_value=results), \
             contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = x550.main(args)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_plain_and_json(self):
        good, bad = result(), result(None, "0000:06:00")
        code, out, err = self.run_main([], [good, bad])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(out, "0000:05:00 eth0,eth1 43.82\n"
                             "0000:06:00 eth0,eth1 unreadable (sensor not ready)\n")
        code, out, err = self.run_main(["--json"], [good, bad])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), [good, bad])

    def test_prometheus_omits_failed_sensor(self):
        code, out, err = self.run_main(["--prometheus"], [result(), result(None, "0000:06:00")])
        self.assertEqual(code, 0)
        self.assertIn('# TYPE x550_temperature_celsius gauge\n', out)
        self.assertIn('x550_temperature_celsius{pci="0000:05:00",interfaces="eth0,eth1"} 43.82\n', out)
        self.assertNotIn("0000:06:00", out)
        code, out, err = self.run_main(["--prometheus"], [result(None)])
        self.assertEqual(code, 1)
        self.assertFalse(any(line.startswith("x550_temperature_celsius{") for line in out.splitlines()))

    def test_statsd_sends_only_successes(self):
        sock = Mock()
        with patch.object(x550.socket, "socket", return_value=sock):
            x550.send_statsd([result(), result(None)], "127.0.0.1:8125")
        sock.sendto.assert_called_once_with(b"x550.0000_05_00.temperature:43.82|g", ("127.0.0.1", 8125))

    def test_all_sensor_failures_exit_one(self):
        code, out, err = self.run_main([], [result(None)])
        self.assertEqual(code, 1)
        self.assertIn("unreadable (sensor not ready)", out)

    def test_missing_sysfs_is_expected_no_card_without_socket(self):
        with patch.object(x550.os, "listdir", side_effect=FileNotFoundError), \
             patch.object(x550.socket, "socket") as sock:
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                self.assertEqual(x550.main([]), 1)
            self.assertEqual(stdout.getvalue(), "")
            self.assertEqual(stderr.getvalue(), "x550-temp: no Intel X550 ports bound to ixgbe found\n")
            sock.assert_not_called()

    def test_entrypoint_no_card_exact_diagnostic_not_crash(self):
        # Execute the real __main__ path in a subprocess while preventing discovery
        # and socket creation. A traceback also exits 1, so verify both streams.
        script = '''import os, runpy, socket, sys
from unittest.mock import patch
sys.argv = [sys.argv[1]]
with patch.object(os, "listdir", return_value=[]), patch.object(socket, "socket", side_effect=AssertionError("unexpected socket")):
    runpy.run_path(sys.argv[0], run_name="__main__")
'''
        completed = subprocess.run([sys.executable, "-c", script, str(TOOL)],
                                   capture_output=True, text=True)
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(completed.stderr, "x550-temp: no Intel X550 ports bound to ixgbe found\n")


if __name__ == "__main__":
    unittest.main()
