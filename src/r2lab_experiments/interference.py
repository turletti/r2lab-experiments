"""
Controlled radio interference in R2Lab, as in the 5g_ansible interference scenario: a USRP emits noise in the band of
the cell, and an R2Lab fit node can show the spectrum. The USRP is either an N300 or an N320 of R2Lab, driven by a
SophiaNode server (wide noise: tens of MHz), or the USRP of a fit node (B210 or B205mini, narrower noise).

Temporary: IFRAN does not drive these USRPs (its R2Lab provisioner only powers RUs and UEs), so they are driven here
over SSH, with the commands of 5g_ansible (``roles/scenarios/interference``, ``roles/r2lab/rru``):

- ``rhubarbe`` on the R2Lab gateway (``faraday.inria.fr``) powers the N300/N320 on and off (``pdu``), loads the
  ``gnuradio`` image on a fit node and powers its USRP on and off;
- ``uhd_siggen`` (GNU Radio) emits the interference, on the server for an N300/N320 (UHD 4.7 and GNU Radio built by
  the ``uhd_gnuradio`` role of this repository, as 5g_ansible does) or on the fit node: Gaussian or uniform noise, a
  sine tone, a constant carrier, two tones or a sweep, centered on ``freq`` and ``rate`` wide;
- ``uhd_rx_cfile`` on a second fit node captures samples for a spectrum snapshot (power spectral density computed on
  that node with numpy), or ``uhd_fft`` shows the live spectrum in a VNC session (5g_ansible ``watch`` role).

The analysis helpers read the artifacts of a NADOR ``ExperimentRun`` (timeline, Prometheus export, CSI cut per
section) and post the interference windows as Grafana annotations.
"""

import asyncio
import base64
import csv
import gzip
import json
import math
import shlex
import time
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "FIT_USRPS",
    "N3XX_USRPS",
    "UHD_ENVIRONMENT",
    "WAVEFORMS",
    "FrequencyPlan",
    "LiveSpectrum",
    "NoiseGenerator",
    "annotate_grafana",
    "csi_rb_power",
    "fit_command",
    "gateway_command",
    "grafana_dashboards",
    "iperf_intervals",
    "node_ssh_command",
    "power_n3xx",
    "power_off_fit_node",
    "prepare_fit_node",
    "prometheus_section_means",
    "section_windows",
    "spectrum_snapshot",
    "wait_usrp",
]

GATEWAY = "faraday.inria.fr"
# Fit nodes with a USRP used by the 5g_ansible interference scenario (deploy.sh get_fit_info)
FIT_USRPS = {"b210": 2, "b205mini": 8}
# UHD device arguments of the R2Lab N300 and N320, as in 5g_ansible (roles/scenarios/interference/run): the N300 data
# and management address can swap at boot (SFP0/SFP1); 192.168.235.103 is the one that answered with UHD 4.7
N3XX_USRPS = {
    "n300": "product=n300,serial=31D98C7,addr=192.168.235.103,mgmt_addr=192.168.235.103",
    "n320": "product=n320,serial=31B3A77,addr=192.168.235.105,mgmt_addr=192.168.235.105,master_clock_rate=245.76e6",
}
# UHD 4.7 and GNU Radio built in /usr/local by the uhd_gnuradio role: GNU Radio must use this libuhd, not a system one
# (5g_ansible), and its Python modules, which the Debian layout puts in /usr/local/lib/python3/dist-packages
UHD_ENVIRONMENT = (
    "LD_LIBRARY_PATH=/usr/local/lib LD_PRELOAD=/usr/local/lib/libuhd.so.4.7.0 PATH=/usr/local/bin:$PATH "
    "PYTHONPATH=$(ls -d /usr/local/lib/python3*/dist-packages 2>/dev/null | paste -sd:)"
)
# uhd_siggen waveform options (GNU Radio gr-uhd/apps/uhd_siggen_base.py)
WAVEFORMS = ("gaussian", "uniform", "sine", "const", "2tone", "sweep")
# Line printed by uhd_siggen once its flow graph runs, i.e. once the USRP emits
SIGGEN_READY = "Press Enter to quit"
SSH_OPTIONS = (
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=20",
    # the fit nodes are reimaged by rhubarbe load: their host keys change at every load
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "LogLevel=ERROR",
)  # fmt: skip


def fit_name(fit_number: int) -> str:
    """Host name of a fit node on the R2Lab gateway (fit02 for 2)."""
    return f"fit{int(fit_number):02d}"


def gateway_command(slice_name: str, command: str) -> list[str]:
    """SSH command running ``command`` on the R2Lab gateway as the slice user (rhubarbe needs the slice lease)."""
    return ["ssh", *SSH_OPTIONS, f"{slice_name}@{GATEWAY}", command]


# B2xx firmware and FPGA images of the UHD of the gnuradio image (/usr/local), downloaded when missing
FIT_UHD_IMAGES = (
    "test -e /usr/local/share/uhd/images/usrp_b200_fw.hex || /usr/local/lib/uhd/utils/uhd_images_downloader.py -t b2xx"
)


def fit_command(slice_name: str, fit_number: int, command: str | None, *, forwards: Iterable[str] = ()) -> list[str]:
    """
    SSH command running ``command`` as root on a fit node, through the gateway. Without command, the connection only
    carries the ``forwards`` (``-L`` specs).
    """
    proxy = f"ssh {' '.join(SSH_OPTIONS)} -W %h:%p {slice_name}@{GATEWAY}"
    args = ["ssh", *SSH_OPTIONS, "-o", f"ProxyCommand={proxy}"]
    for forward in forwards:
        args += ["-L", forward]
    if command is None:
        args.append("-N")
    return [*args, f"root@{fit_name(fit_number)}", *([] if command is None else [command])]


async def _run(args: list[str], timeout: float) -> str:
    """Run a local command, return its output (stdout and stderr), raise on failure or timeout."""
    process = await asyncio.create_subprocess_exec(
        *args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise RuntimeError(f"timeout after {timeout} s: {shlex.join(args)}") from None
    text = output.decode(errors="replace")
    if process.returncode:
        raise RuntimeError(f"exit code {process.returncode}: {shlex.join(args)}\n{text}")
    return text


async def prepare_fit_node(slice_name: str, fit_number: int, *, image: str = "gnuradio", quiet: bool = True) -> str:
    """
    Load the GNU Radio image on a fit node and power its USRP on (5g_ansible scenarios/interference/setup).

    The USRP is powered off before the load, the image load takes several minutes (huge image), and the USRP gets
    20 seconds to start once powered on. The image has UHD in /usr/local but not always the B2xx firmware and FPGA
    images, without which ``uhd_find_devices`` finds no USRP: they are downloaded when missing (as the 5g_ansible
    interference recovery does). Returns the USRP seen by ``uhd_find_devices`` on the node.
    """
    steps = [
        (f"rhubarbe usrpoff {fit_number}", 120),
        (f"rhubarbe load -t 600 -i {shlex.quote(image)} {fit_number}", 900),
        (f"rhubarbe wait -t 60 {fit_number}", 120),
        (f"rhubarbe usrpon {fit_number}", 120),
    ]
    for command, timeout in steps:
        output = await _run(gateway_command(slice_name, command), timeout)
        if not quiet:
            print(f"{fit_name(fit_number)}: {command}\n{output}")
    output = await _run(fit_command(slice_name, fit_number, FIT_UHD_IMAGES), 600)
    if not quiet:
        print(f"{fit_name(fit_number)}: {FIT_UHD_IMAGES}\n{output}")
    await asyncio.sleep(20)  # USRP start, as in 5g_ansible
    return await _run(fit_command(slice_name, fit_number, "uhd_find_devices"), 120)


def node_ssh_command(manager, node: str) -> list[str]:
    """SSH command prefix (without the remote command) to a node of an IFRAN deployment model, with its jump hosts."""
    from ifran.models.provisioning import SSHInfo
    from ifran.utils.utils import deep_merge

    model = manager.deployment_model
    ssh = SSHInfo.model_validate(
        deep_merge(
            model.params.ssh.model_dump(mode="json", exclude_unset=True),
            model.nodes[node].ssh_info.model_dump(mode="json", exclude_unset=True),
        )
    )
    args = ["ssh", "-o", "BatchMode=yes", *shlex.split(ssh.get_common_args() or "")]
    if ssh.private_key_file:
        args += ["-i", str(Path(ssh.private_key_file).expanduser())]
    return [*args, "-p", str(ssh.port), f"{ssh.user}@{ssh.hostname or node}"]


def node_inventory(manager, node: str, group: str = "noise_nodes") -> str:
    """
    Ansible INI inventory of one node of an IFRAN deployment model, for MiscellaneousManagement.run_playbook.

    IFRAN puts in its inventory only the nodes with a role (one group per role): a node without role, such as the
    noise server, is not in it, and a playbook on it matches no host and succeeds without doing anything.
    """
    from ifran.utils.ansible import get_ansible_inventory_line

    model = manager.deployment_model
    line = get_ansible_inventory_line(resource_name=node, resource_data=model.nodes[node], ssh_params=model.params.ssh)
    return f"[{group}]\n{line}\n"


async def power_n3xx(slice_name: str, usrp: str, *, on: bool) -> str:
    """Power an R2Lab N300/N320 on or off (its PDU outlet, through the R2Lab gateway)."""
    if usrp not in N3XX_USRPS:
        raise ValueError(f"usrp must be one of {list(N3XX_USRPS)}")
    return await _run(gateway_command(slice_name, f"rhubarbe pdu {'on' if on else 'off'} {usrp}"), 120)


async def wait_usrp(ssh: list[str], usrp_args: str, *, timeout: float = 600, period: float = 15) -> str:
    """Wait until ``uhd_find_devices`` (UHD 4.7) finds the USRP from the host of ``ssh``; return its output."""
    command = f"{UHD_ENVIRONMENT} uhd_find_devices --args {shlex.quote(usrp_args)}"
    deadline = time.monotonic() + timeout
    while True:
        try:
            return await _run([*ssh, command], 120)
        except RuntimeError:
            if time.monotonic() > deadline:
                raise
            await asyncio.sleep(period)


async def power_off_fit_node(slice_name: str, fit_number: int) -> str:
    """Power the USRP and the fit node off."""
    output = await _run(gateway_command(slice_name, f"rhubarbe usrpoff {fit_number}"), 120)
    return output + await _run(gateway_command(slice_name, f"rhubarbe off {fit_number}"), 120)


@dataclass
class NoiseGenerator:
    """
    Interference emitted by a USRP with ``uhd_siggen``: the USRP of fit node ``fit_number`` by default, or, with
    ``ssh`` (an SSH command prefix such as ``node_ssh_command``), the USRP of ``usrp_args`` driven by that host.

    ``start()`` returns once the USRP emits (``uhd_siggen`` prints its prompt once its flow graph runs) and ``stop()``
    answers the prompt, which stops the flow graph: the emission window is known to the second. ``safety_s`` bounds
    the emission on the node itself (``timeout``), should this notebook stop before ``stop()``.
    """

    slice_name: str
    fit_number: int
    freq: float  # center frequency, Hz
    rate: float  # sample rate, Hz: the width of the noise
    gain: float  # TX gain, dB (UHD keeps it within the range of the USRP)
    waveform: str = "gaussian"
    amplitude: float = 0.15  # baseband amplitude, 0 to 1 (uhd_siggen default)
    waveform_freq: float = 0.0  # tone offset from freq (sine, 2tone, sweep), Hz
    waveform2_freq: float = 0.0  # second tone (2tone) or sweep rate (sweep), Hz
    usrp_args: str = ""
    ssh: list[str] = field(default_factory=list)  # host running uhd_siggen (default: the fit node)
    environment: str = ""  # variables set before uhd_siggen (UHD_ENVIRONMENT on a server)
    verbose: bool = True  # uhd_siggen -v: prints the sample rate, gain and frequency actually set
    safety_s: int = 1800
    output: list[str] = field(default_factory=list)
    _process: asyncio.subprocess.Process | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.waveform not in WAVEFORMS:
            raise ValueError(f"waveform must be one of {WAVEFORMS}")

    def command(self) -> str:
        """Command run on the fit node."""
        siggen = [
            "uhd_siggen", f"--{self.waveform}",
            "-f", f"{self.freq:.0f}", "-s", f"{self.rate:.0f}", "-g", f"{self.gain:g}", "-m", f"{self.amplitude:g}",
        ]  # fmt: skip
        if self.waveform_freq:
            siggen += ["-x", f"{self.waveform_freq:.0f}"]
        if self.waveform2_freq:
            siggen += ["-y", f"{self.waveform2_freq:.0f}"]
        if self.usrp_args:
            siggen += ["--args", self.usrp_args]
        if self.verbose:
            siggen.append("-v")
        # unbuffered output: the ready line must come at once; SIGINT at the end of the safety delay
        environment = f"{self.environment} " if self.environment else ""
        return f"{environment}PYTHONUNBUFFERED=1 timeout -s INT {int(self.safety_s)} {shlex.join(siggen)}"

    def remote(self) -> list[str]:
        """Local command that runs ``command()`` on the host of the USRP."""
        if self.ssh:
            return [*self.ssh, self.command()]
        return fit_command(self.slice_name, self.fit_number, self.command())

    @property
    def emitting(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def start(self, timeout: float = 90) -> float:
        """Start the emission; return its start time (epoch) once the USRP emits."""
        if self.emitting:
            raise RuntimeError("the noise generator already emits")
        self.output = []
        self._process = await asyncio.create_subprocess_exec(
            *self.remote(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            await asyncio.wait_for(self._wait_ready(), timeout)
        except BaseException:
            await self._kill()
            raise
        return time.time()

    async def _wait_ready(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        buffer = b""
        while True:
            chunk = await self._process.stdout.read(1024)
            if not chunk:
                await self._process.wait()
                raise RuntimeError("uhd_siggen stopped before emitting:\n" + buffer.decode(errors="replace"))
            buffer += chunk
            if SIGGEN_READY.encode() in buffer:
                self.output = buffer.decode(errors="replace").splitlines()
                return

    async def stop(self, timeout: float = 30) -> float:
        """Stop the emission (Enter at the uhd_siggen prompt); return the stop time (epoch)."""
        if self._process is None:
            return time.time()
        process, self._process = self._process, None
        stopped = time.time()
        if process.returncode is None and process.stdin is not None:
            try:
                process.stdin.write(b"\n")
                await process.stdin.drain()
                process.stdin.close()
            except (BrokenPipeError, ConnectionResetError):
                pass
        try:
            rest, _ = await asyncio.wait_for(process.communicate(), timeout)
            self.output += rest.decode(errors="replace").splitlines()
        except TimeoutError:
            process.kill()
            await process.wait()
        return stopped

    async def _kill(self) -> None:
        if self._process is not None and self._process.returncode is None:
            self._process.kill()
            await self._process.wait()
        self._process = None


# Run on the fit node: power spectral density (Welch average, Hann window) of a complex64 capture, as JSON
PSD_SCRIPT = """
import json, sys
import numpy as np
path, rate, freq, nfft = sys.argv[1], float(sys.argv[2]), float(sys.argv[3]), int(sys.argv[4])
x = np.fromfile(path, dtype=np.complex64)
x = x[len(x) // 10:]  # skip the start of the capture (gain and DC offset settling)
n = len(x) // nfft
segments = x[: n * nfft].reshape(n, nfft) * np.hanning(nfft)
psd = np.fft.fftshift((np.abs(np.fft.fft(segments, axis=1)) ** 2).mean(axis=0)) / (nfft * rate)
f = freq + np.fft.fftshift(np.fft.fftfreq(nfft, 1 / rate))
print(json.dumps({"freq_hz": f.tolist(), "psd_db": (10 * np.log10(psd + 1e-20)).tolist(), "segments": n}))
"""


async def spectrum_snapshot(
    slice_name: str,
    fit_number: int,
    *,
    freq: float,
    rate: float,
    gain: float,
    seconds: float = 0.5,
    nfft: int = 1024,
    usrp_args: str = "",
) -> dict[str, Any]:
    """
    Spectrum around ``freq`` seen by the USRP of a fit node: ``uhd_rx_cfile`` captures ``seconds`` of samples, and
    numpy computes the power spectral density on the node (relative dB: the USRP is not calibrated).
    """
    capture = "/tmp/r2lab-spectrum.cfile"
    rx = ["uhd_rx_cfile", "-f", f"{freq:.0f}", "-r", f"{rate:.0f}", "-g", f"{gain:g}", "-N", f"{int(rate * seconds)}"]
    if usrp_args:
        rx += ["--args", usrp_args]
    command = (
        f"{shlex.join([*rx, capture])} > /tmp/r2lab-spectrum.log 2>&1 || {{ cat /tmp/r2lab-spectrum.log; exit 1; }}; "
        f"python3 -c {shlex.quote(PSD_SCRIPT)} {capture} {rate:.0f} {freq:.0f} {int(nfft)}; rm -f {capture}"
    )
    output = await _run(fit_command(slice_name, fit_number, command), 180)
    result = json.loads(output.strip().splitlines()[-1])
    result.update({"time": time.time(), "fit": fit_name(fit_number), "gain_db": gain})
    return result


@dataclass
class LiveSpectrum:
    """
    Live spectrum (``uhd_fft``) of a fit node in a VNC session, as the 5g_ansible ``watch`` role does, with an SSH
    tunnel from this machine: open ``vnc://localhost:<local_port>`` with the password ``password``.
    """

    slice_name: str
    fit_number: int
    freq: float
    rate: float
    gain: float = 30
    local_port: int = 5901
    password: str = "1234567890"
    tunnel: asyncio.subprocess.Process | None = field(default=None, repr=False)

    def setup_commands(self) -> list[str]:
        fft = shlex.join(["uhd_fft", "-f", f"{self.freq:.0f}", "-s", f"{self.rate:.0f}", "-g", f"{self.gain:g}"])
        xstartup = (
            "#!/bin/bash\nunset SESSION_MANAGER\nunset DBUS_SESSION_BUS_ADDRESS\n"
            "export XDG_RUNTIME_DIR=/tmp/runtime-root\nmkdir -p $XDG_RUNTIME_DIR\nchmod 700 $XDG_RUNTIME_DIR\n"
            "exec dbus-launch --exit-with-session startxfce4\n"
        )
        return [
            "DEBIAN_FRONTEND=noninteractive apt-get update -q",
            (
                "DEBIAN_FRONTEND=noninteractive apt-get install -y -q tigervnc-standalone-server "
                "tigervnc-xorg-extension tigervnc-viewer xfce4 xfce4-goodies xterm dbus-x11"
            ),
            (
                f"mkdir -p /root/.vnc && echo {shlex.quote(self.password)} | vncpasswd -f > /root/.vnc/passwd "
                "&& chmod 600 /root/.vnc/passwd"
            ),
            f"printf %s {shlex.quote(xstartup)} > /root/.vnc/xstartup && chmod +x /root/.vnc/xstartup",
            "tigervncserver -kill :1 >/dev/null 2>&1; tigervncserver :1 -geometry 1920x1080 -depth 24 -localhost yes",
            f"nohup env DISPLAY=:1 xterm -e {shlex.quote(fft + '; bash')} >/dev/null 2>&1 &",
        ]

    async def start(self) -> str:
        """Install and start VNC and uhd_fft on the fit node (minutes: apt), then open the tunnel."""
        for command in self.setup_commands():
            await _run(fit_command(self.slice_name, self.fit_number, command), 1200)
        forward = f"127.0.0.1:{self.local_port}:127.0.0.1:5901"
        self.tunnel = await asyncio.create_subprocess_exec(
            *fit_command(self.slice_name, self.fit_number, None, forwards=[forward]), stdin=asyncio.subprocess.DEVNULL
        )
        return f"vnc://localhost:{self.local_port}"

    async def stop(self) -> None:
        """Close the tunnel and stop the VNC session."""
        if self.tunnel is not None and self.tunnel.returncode is None:
            self.tunnel.terminate()
            await self.tunnel.wait()
        self.tunnel = None
        await _run(fit_command(self.slice_name, self.fit_number, "tigervncserver -kill :1 || true"), 60)


@dataclass(frozen=True)
class FrequencyPlan:
    """Carrier of the cell and the resource block (RB) grid, to place the interference and read the CSI per RB."""

    point_a_arfcn: int
    n_rb: int
    scs_khz: int = 30
    ssb_arfcn: int | None = None

    @staticmethod
    def arfcn_to_hz(arfcn: int) -> float:
        """NR-ARFCN to frequency (3GPP TS 38.104, 3 to 24.25 GHz: 15 kHz raster from 3000 MHz)."""
        if not 600000 <= arfcn < 2016667:
            raise ValueError("only NR-ARFCN between 600000 and 2016666 (3 to 24.25 GHz) are supported")
        return 3000e6 + 15e3 * (arfcn - 600000)

    @property
    def low_hz(self) -> float:
        """Lower edge of RB 0 (point A, no offset to carrier)."""
        return self.arfcn_to_hz(self.point_a_arfcn)

    @property
    def rb_hz(self) -> float:
        return 12 * self.scs_khz * 1e3

    @property
    def high_hz(self) -> float:
        return self.low_hz + self.n_rb * self.rb_hz

    @property
    def center_hz(self) -> float:
        return (self.low_hz + self.high_hz) / 2

    @property
    def ssb_hz(self) -> float | None:
        """SSB center (20 RB wide)."""
        return None if self.ssb_arfcn is None else self.arfcn_to_hz(self.ssb_arfcn)

    def rbs(self, center_hz: float, width_hz: float) -> tuple[int, int] | None:
        """First and last RB of the carrier overlapped by a band, or None outside the carrier."""
        low, high = center_hz - width_hz / 2, center_hz + width_hz / 2
        first = max(0, int((low - self.low_hz) // self.rb_hz))
        last = min(self.n_rb - 1, int((high - self.low_hz) // self.rb_hz))
        return None if high <= self.low_hz or low >= self.high_hz or first > last else (first, last)

    def describe(self, center_hz: float, width_hz: float) -> str:
        """Where a band falls with respect to the carrier and its SSB."""
        text = (
            f"carrier {self.low_hz / 1e6:.2f}-{self.high_hz / 1e6:.2f} MHz ({self.n_rb} RB of {self.rb_hz / 1e3:.0f} kHz), "
            f"interference {(center_hz - width_hz / 2) / 1e6:.2f}-{(center_hz + width_hz / 2) / 1e6:.2f} MHz: "
        )
        rbs = self.rbs(center_hz, width_hz)
        if rbs is None:
            return text + "outside the carrier"
        text += f"RB {rbs[0]} to {rbs[1]}"
        if self.ssb_hz is not None:
            ssb_low, ssb_high = self.ssb_hz - 10 * self.rb_hz, self.ssb_hz + 10 * self.rb_hz
            hit = center_hz - width_hz / 2 < ssb_high and center_hz + width_hz / 2 > ssb_low
            text += ", covers the SSB" if hit else ", away from the SSB"
        return text


# --- Analysis of the artifacts of an ExperimentRun ---


def section_windows(run_directory: str | Path) -> dict[str, tuple[float, float]]:
    """Start and end (epoch) of each section of a run, from its timeline."""
    summary = json.loads((Path(run_directory) / "timeline_summary.json").read_text())
    return {
        w["name"]: (float(w["start_epoch"]), float(w["end_epoch"]))
        for w in summary.get("windows", [])
        if w.get("level") == "section"
    }


def prometheus_section_means(
    run_directory: str | Path, query_names: Iterable[str] | None = None, *, metric_filter: str = ""
) -> dict[str, dict[str, float]]:
    """
    Mean of each exported Prometheus series per section: {series: {section: mean}}. A series is named after its
    query, its metric name and its ``rnti``/``ue_name``/``imsi`` labels when present. ``metric_filter`` keeps the
    series whose name contains one of its comma-separated words.
    """
    windows = section_windows(run_directory)
    names = set(query_names) if query_names is not None else None
    words = [w.strip().lower() for w in metric_filter.split(",") if w.strip()]
    sums: dict[str, dict[str, list[float]]] = {}
    with (Path(run_directory) / "prometheus_timeseries.csv").open(newline="") as fp:
        for row in csv.DictReader(fp):
            if names is not None and row["query_name"] not in names:
                continue
            metric = json.loads(row.get("metric_json") or "{}")
            label = ",".join(f"{k}={metric[k]}" for k in ("rnti", "ue_name", "imsi") if metric.get(k))
            series = f"{row['query_name']}:{metric.get('__name__', '')}" + (f"{{{label}}}" if label else "")
            if words and not any(w in series.lower() for w in words):
                continue
            try:
                t, value = float(row["timestamp"]), float(row["value"])
            except ValueError:
                continue
            for section, (start, end) in windows.items():
                if start <= t <= end:
                    sums.setdefault(series, {}).setdefault(section, []).append(value)
    return {s: {sec: sum(v) / len(v) for sec, v in per.items() if v} for s, per in sorted(sums.items())}


def iperf_intervals(report: dict[str, Any]) -> tuple[list[float], list[float]]:
    """Per-second throughput of an iperf3 report: (epoch of each interval end on the UE clock, Mbit/s)."""
    start = float(report.get("start", {}).get("timestamp", {}).get("timesecs", 0))
    times, mbps = [], []
    for interval in report.get("intervals", []):
        s = interval.get("sum", {})
        if "end" in s and "bits_per_second" in s:
            times.append(start + float(s["end"]))
            mbps.append(float(s["bits_per_second"]) / 1e6)
    return times, mbps


def csi_rb_power(path: str | Path) -> tuple[list[int], list[float], int]:
    """
    Mean power per RB of a CSI v3.1 file (``csi_per_rb.csv`` or its ``.gz`` cut): mean of ``real² + imag²`` over all
    rows of the RB (SRS occasions, UEs, receive antennas, ports), in dB. Returns (RBs, dB, rows read).
    """
    opener = gzip.open if str(path).endswith(".gz") else open
    totals: dict[int, list[float]] = {}
    rows = 0
    rb = real = imag = None
    with opener(path, "rt") as fp:
        for line in fp:
            if line.startswith("#"):
                continue
            fields = line.rstrip("\n").split(",")
            if rb is None:  # column header
                columns = [c.strip() for c in fields]
                rb, real, imag = columns.index("rb"), columns.index("real"), columns.index("imag")
                continue
            try:
                index, power = int(fields[rb]), float(fields[real]) ** 2 + float(fields[imag]) ** 2
            except (ValueError, IndexError):
                continue
            total = totals.setdefault(index, [0.0, 0])
            total[0] += power
            total[1] += 1
            rows += 1
    rbs = sorted(totals)
    return rbs, [10 * math.log10(totals[i][0] / totals[i][1] + 1e-20) for i in rbs], rows


def _grafana(url: str, path: str, body: dict | None = None, auth=("admin", "admin")) -> Any:
    """Call the Grafana HTTP API (basic authentication)."""
    token = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
    headers = {"Authorization": f"Basic {token}", "Content-Type": "application/json"}
    request = urllib.request.Request(
        url.rstrip("/") + path, data=None if body is None else json.dumps(body).encode(), headers=headers
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read() or b"null")


def grafana_dashboards(url: str, auth=("admin", "admin")) -> dict[str, str]:
    """Dashboards of a Grafana: {title: uid}."""
    return {d["title"]: d["uid"] for d in _grafana(url, "/api/search?type=dash-db", auth=auth)}


def annotate_grafana(
    url: str, start: float, end: float, text: str, *, tags: Iterable[str] = ("interference",), auth=("admin", "admin")
) -> list[str]:
    """Add a region annotation (start to end, epoch) to every Grafana dashboard; return the dashboard titles."""
    annotated = []
    for title, uid in grafana_dashboards(url, auth).items():
        _grafana(
            url,
            "/api/annotations",
            {
                "dashboardUID": uid,
                "time": int(start * 1000),
                "timeEnd": int(end * 1000),
                "tags": list(tags),
                "text": text,
            },
            auth,
        )
        annotated.append(title)
    return annotated
