# x550-temp

Read the die temperature of **Intel X550** 10GBASE-T network controllers on Linux,
including systems where `sensors` shows no NIC temperature.

One Python file, no third-party dependencies. Outputs plain text, JSON, Prometheus metrics,
or statsd gauges.

## Quick start

Requires Linux, Python 3.8 or later, and an X550 bound to the `ixgbe` driver.
The network link does not need to be up.

Download and install system-wide:

```sh
curl -fsSLo x550-temp https://raw.githubusercontent.com/Parcosta/x550-temp/main/x550-temp
sudo install -d /usr/local/bin
sudo install -m 0755 x550-temp /usr/local/bin/x550-temp
sudo x550-temp
```

Example output — one line per chip, with temperature in °C:

```text
0000:05:00 enp5s0f0,enp5s0f1 43.82
```


## Usage

```sh
sudo x550-temp --json                       # JSON
sudo x550-temp --prometheus                 # Prometheus textfile metrics
sudo x550-temp --statsd 127.0.0.1:8125        # Netdata or another statsd listener
sudo x550-temp --watch 5                    # Repeat every five seconds until Ctrl-C
```

Exit status is 0 when at least one chip was read, and 1 otherwise. Unreadable chips include
a reason; they are never replaced with a fabricated 0 °C reading. JSON includes failures
with `celsius: null`; Prometheus and statsd omit failed sensors.

## Why this is needed

The X550 has an on-die thermal sensor, but Linux doesn't expose it:

- The in-tree `ixgbe` driver registers hwmon sensors only for chips that have a
  `get_thermal_sensor_data` operation, and for the X550 that operation is `NULL`. Intel's
  out-of-tree driver (6.5.x) does the same.
- FreeBSD's `ix` driver reads the same register, but only for the X552/X557 (Xeon-D). It
  refuses on an X550.

So advice like "install lm-sensors, look under `hwmon`" doesn't work for this chip.

## How it works

Intel's [X550 datasheet, revision 2.7](https://cdrdv2-public.intel.com/333369/333369_X550_Datasheet_Rev2.7.pdf)
(333369, sections 10.6.29–10.6.30) documents the sensor registers:

| Register | Meaning |
|---|---|
| `1E.C820` *Global Thermal Status 1* | signed 16-bit two's-complement junction temperature, 1/256 °C per LSB, ±3 °C |
| `1E.C821` *Global Thermal Status 2* | bit 0 = reading valid |

The explicit register table in section 10.6.30 defines ready bit 0 as 1 = valid. When
ready, zero and sub-zero readings are decoded as temperatures; failures remain unreadable.

Notes from the datasheet:

- These registers are accessible only through the MDIO interface of PHY 0.
- Host software may read them but must not change their programming. This tool never writes.
- Limits: Tj max 105 °C. The PHY shuts itself down at its 107 °C threshold, which drops the link.

`x550-temp` reads these registers with the standard `SIOCGMIIREG` ioctl, using a clause-45
address. `ixgbe` handles that ioctl in its own MDIO bus code, which takes the driver's
software/firmware semaphore, so a read never races the card's firmware. Both ports of a
dual-port card share one die and one sensor, so the tool reports one value per chip.

### The kernel quirk this works around

Since Linux 6.3 (commit `308c8ffd5a7d`, "ixgbe: Separate C22 and C45 transactions"),
`ixgbe_mii_bus_read_c45()` declares its arguments as `(bus, devad, addr, regnum)`. The MDIO core
calls it as `(bus, addr, devad, regnum)`, so the PHY address and the MMD arrive swapped. A plain
clause-45 read of `1E.C820` therefore returns `0`: it actually reads PHY 30, MMD 0.

`x550-temp` tries the swapped encoding first, then the straight one. It accepts a read only when
the PHY's PMA/PMD identifier high word (register `1.2`) is `0x0154`. This checks the address
encoding; other Intel PHYs share that word, so discovery separately requires Intel vendor
`8086` and one of the two documented X550 PCI device IDs below. This handles affected kernels,
kernels older than 6.3, and kernels where the bug has been fixed.

Upstream status: **SUBMITTED**. The [v1 argument-order fix](https://lore.kernel.org/netdev/20261029-ixgbe-c45-fix-v1-1-7eb38075a3d1@parcosta.com/)
was submitted to netdev on October 30, 2026. It has not been accepted or merged.

## Monitoring

[`examples/`](examples/) has:

- **Prometheus (node_exporter textfile collector):** a systemd service and timer that write
  `x550.prom` every minute. Requires systemd, node_exporter with its textfile collector enabled,
  and an existing `/var/lib/node_exporter/textfile_collector` directory writable by the service
  and readable by node_exporter. The examples run as root and expect the system-wide install above.
- **Netdata:** a statsd chart definition and health alarms (warn 85 °C, crit 95 °C, stale after
  10 minutes). Requires Netdata with its statsd listener enabled at the chosen address and a
  scheduled invocation of `x550-temp --statsd HOST:PORT`; the configuration files do not schedule reads.

For Prometheus, install the service and timer in `/etc/systemd/system/`, reload systemd,
and enable `x550-temp-prometheus.timer`. The service removes the previous sample before
starting a new read, then atomically renames the temporary file only on exit 0. All-sensor
failure, no cards, a crash, a missing executable, or a timeout therefore leaves the temperature
absent. A partial success publishes only the readable chips. There can be a brief gap during
collection. Failure to remove the old file (for example, broken directory permissions) prevents
collection and needs an operator fix.

Alert on missing chips as well as high temperature. For a single monitored host, an
`absent_over_time(x550_temperature_celsius[5m])` alert detects total loss; use your inventory's
expected `pci` labels to detect individual missing chips. Also alert when
`time() - node_textfile_mtime_seconds{file="/var/lib/node_exporter/textfile_collector/x550.prom"}`
exceeds 180 seconds (adjust the file label to your node_exporter output), and on a missing
mtime series. This catches a stopped timer that never starts another collection.

To remove monitoring, disable and stop `x550-temp-prometheus.timer` and its service, remove
those two unit files, reload systemd, and remove `x550.prom` and `x550.prom.tmp` from the
collector directory. Remove the Netdata chart/alarm files if installed. Remove the executable
from `$HOME/.local/bin/x550-temp` or `/usr/local/bin/x550-temp`, as applicable. System-wide
removal requires the same administrator privileges as installation.

Suggested alert thresholds: warn above 85 °C and critical above 95 °C, using a 5-minute average.
That leaves room under the 105 °C Tj max and the 107 °C self-shutdown, and allows for the
sensor's ±3 °C accuracy.

## Tested on

| Card | Firmware | OS / kernel | Result |
|---|---|---|---|
| Intel X550-T2 (Dell OEM, 8086:1563) | 19.5.12 | Proxmox VE 9.2, 7.0.14-pve | ✅ swapped encoding |

Reports for other cards and kernels are welcome. Please open an issue with `x550-temp --json`
output, `uname -r` and `ethtool -i <iface>`.

Documented support is limited to X550 dual-port `8086:1563` and single-port `8086:15d1`
(X550 NVM section 6). Only the X550-T2 listed above has been tested on physical hardware;
X550-T1 and X550-AT2 coverage is based on documentation, not additional hardware tests.
X540 (`8086:1528`/`8086:1560`), X550EM-X/A, X557, and 82599 are not supported by this reader.

The X540 uses a related PHY, but its `C820`/`C821` temperature-register semantics have not
been verified. Intel's [X540 FAQ, section 2.35](https://cdrdv2-public.intel.com/612549/ethernet-x540-faq.pdf)
describes monitoring its thermal diode with an external sensor; that does not establish an
X550-compatible digital interface or rule out other embedded sensing. Shared PHY ancestry
or the broader ixgbe argument-order bug is not evidence of compatible temperature registers.

## Optional user-local installation

After downloading with the `curl` command above:

```sh
mkdir -p "$HOME/.local/bin"
install -m 0755 x550-temp "$HOME/.local/bin/x550-temp"
sudo "$HOME/.local/bin/x550-temp"
```

Use this full path in place of `x550-temp` in the usage examples. The systemd service uses
`/usr/local/bin/x550-temp` and expects the default system-wide installation.

## Pinning an installation

For repeatable installs, replace `main` in the download URL with a specific commit from the
[commit history](https://github.com/Parcosta/x550-temp/commits/main). Installation is otherwise
the same.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

All 16 hardware-free tests pass in CI on Python 3.8 and 3.13. They mock discovery and ioctl
responses; the hardware coverage table above lists the physical-card validation.

## License

MIT. See [LICENSE](LICENSE).
