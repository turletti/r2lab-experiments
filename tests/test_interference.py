"""Offline tests of r2lab_experiments.interference: no fit node, a local script stands in for uhd_siggen."""

import asyncio
import gzip
import json
import math
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from r2lab_experiments import interference
from r2lab_experiments.interference import (
    FrequencyPlan,
    NoiseGenerator,
    annotate_grafana,
    csi_rb_power,
    fit_command,
    iperf_intervals,
    prometheus_section_means,
    section_windows,
)

BENETEL = FrequencyPlan(point_a_arfcn=626772, n_rb=273, ssb_arfcn=630048)
AW2S = FrequencyPlan(point_a_arfcn=626748, n_rb=133, ssb_arfcn=627360)


def test_frequency_plan_benetel():
    assert BENETEL.low_hz == pytest.approx(3401.58e6)
    assert BENETEL.center_hz == pytest.approx(3450.72e6)
    assert BENETEL.ssb_hz == pytest.approx(3450.72e6)
    assert BENETEL.rbs(3475e6, 15e6) == (183, 224)
    assert "away from the SSB" in BENETEL.describe(3475e6, 15e6)
    assert "covers the SSB" in BENETEL.describe(3450.72e6, 15e6)
    assert BENETEL.rbs(3530e6, 15e6) is None
    assert "outside the carrier" in BENETEL.describe(3530e6, 15e6)


def test_frequency_plan_aw2s_5g_ansible_default():
    assert AW2S.low_hz == pytest.approx(3401.22e6)
    assert AW2S.high_hz == pytest.approx(3449.10e6)
    assert AW2S.rbs(3411.22e6, 15e6) == (6, 48)
    assert "covers the SSB" in AW2S.describe(3411.22e6, 15e6)


def test_fit_command_goes_through_the_gateway():
    args = fit_command("myslice", 2, "uhd_find_devices")
    assert args[-2:] == ["root@fit02", "uhd_find_devices"]
    assert any("myslice@faraday.inria.fr" in a for a in args)
    tunnel = fit_command("myslice", 8, None, forwards=["127.0.0.1:5901:127.0.0.1:5901"])
    assert tunnel[-2:] == ["-N", "root@fit08"]
    assert "-L" in tunnel


def test_noise_command():
    noise = NoiseGenerator("s", 2, freq=3475e6, rate=15e6, gain=89, waveform="sine", waveform_freq=1e6)
    command = noise.command()
    assert command.startswith("PYTHONUNBUFFERED=1 timeout -s INT 1800 uhd_siggen --sine")
    assert "-f 3475000000 -s 15000000 -g 89 -m 0.15 -x 1000000" in command
    with pytest.raises(ValueError):
        NoiseGenerator("s", 2, freq=1, rate=1, gain=1, waveform="square")


FAKE_SIGGEN = """
import sys, time
print("[UHD-SIGGEN] UHD Signal Generator")
time.sleep(0.3)
line = input("[UHD-SIGGEN] Press Enter to quit:\\n")
print("stopped")
"""


def test_noise_start_stop_with_a_fake_siggen(monkeypatch, tmp_path):
    script = tmp_path / "siggen.py"
    script.write_text(FAKE_SIGGEN)
    monkeypatch.setattr(interference, "fit_command", lambda *a, **k: [sys.executable, str(script)])

    async def scenario():
        noise = NoiseGenerator("s", 2, freq=3475e6, rate=15e6, gain=89)
        start = await noise.start(timeout=10)
        assert noise.emitting
        await asyncio.sleep(0.2)
        stop = await noise.stop(timeout=10)
        assert not noise.emitting
        return start, stop, noise.output

    start, stop, output = asyncio.run(scenario())
    assert stop >= start
    assert "stopped" in output


def test_noise_start_fails_when_siggen_exits(monkeypatch, tmp_path):
    script = tmp_path / "siggen.py"
    script.write_text("print('No devices found')\n")
    monkeypatch.setattr(interference, "fit_command", lambda *a, **k: [sys.executable, str(script)])
    noise = NoiseGenerator("s", 2, freq=3475e6, rate=15e6, gain=89)
    with pytest.raises(RuntimeError, match="No devices found"):
        asyncio.run(noise.start(timeout=10))


def test_psd_script_finds_a_tone(tmp_path):
    np = pytest.importorskip("numpy")  # numpy runs on the fit node, not a dependency here

    rate, freq, nfft = 1e6, 3.4e9, 256
    n = np.arange(200_000)
    x = (np.exp(2j * np.pi * 100e3 * n / rate) + 0.01 * np.random.default_rng(0).standard_normal(len(n))).astype(
        np.complex64
    )
    path = tmp_path / "capture.cfile"
    x.tofile(path)
    out = subprocess.run(
        [sys.executable, "-c", interference.PSD_SCRIPT, str(path), str(rate), str(freq), str(nfft)],
        capture_output=True,
        text=True,
        check=True,
    )
    result = json.loads(out.stdout)
    peak = result["freq_hz"][int(np.argmax(result["psd_db"]))]
    assert abs(peak - (freq + 100e3)) < 2 * rate / nfft


def test_csi_rb_power(tmp_path):
    path = tmp_path / "csi_per_rb.csv.gz"
    with gzip.open(path, "wt") as fp:
        fp.write(
            '# {"format": "csi_v3.1"}\nframe,slot,rnti,ant_rx,port_tx,rb,real,imag\n# TIMESTAMP: 2026-11-27T10:00:05Z\n'
        )
        fp.write("1,8,4660,0,0,0,1,0\n1,8,4660,1,0,0,0,1\n1,8,4660,0,0,1,10,0\n")
    rbs, db, rows = csi_rb_power(path)
    assert rbs == [0, 1] and rows == 3
    assert db[0] == pytest.approx(0.0) and db[1] == pytest.approx(20.0)


def write_run(tmp_path):
    (tmp_path / "timeline_summary.json").write_text(
        json.dumps(
            {
                "windows": [
                    {"level": "campaign", "name": "run", "start_epoch": 0, "end_epoch": 100},
                    {"level": "section", "name": "ul_baseline", "start_epoch": 10, "end_epoch": 20},
                    {"level": "section", "name": "ul_interference", "start_epoch": 30, "end_epoch": 40},
                ]
            }
        )
    )
    rows = ["query_name,query,timestamp,value,metric_json"]
    for t, v in [(12, 10), (15, 20), (32, 1), (35, 3), (50, 99)]:
        metric = json.dumps({"__name__": "oai_gnb_mac_ul_mcs", "rnti": "4660"}).replace('"', '""')
        rows.append(f'ul_mcs,q,{t},{v},"{metric}"')
    (tmp_path / "prometheus_timeseries.csv").write_text("\n".join(rows) + "\n")


def test_sections_and_prometheus_means(tmp_path):
    write_run(tmp_path)
    assert section_windows(tmp_path) == {"ul_baseline": (10.0, 20.0), "ul_interference": (30.0, 40.0)}
    means = prometheus_section_means(tmp_path)
    assert means == {"ul_mcs:oai_gnb_mac_ul_mcs{rnti=4660}": {"ul_baseline": 15.0, "ul_interference": 2.0}}
    assert prometheus_section_means(tmp_path, metric_filter="bler") == {}


def test_iperf_intervals():
    report = {
        "start": {"timestamp": {"timesecs": 1000}},
        "intervals": [{"sum": {"end": 1.0, "bits_per_second": 5e6}}, {"sum": {"end": 2.0, "bits_per_second": 7e6}}],
    }
    assert iperf_intervals(report) == ([1001.0, 1002.0], [5.0, 7.0])


def test_annotate_grafana():
    posted = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            body = json.dumps([{"uid": "a1", "title": "OAI"}, {"uid": "b2", "title": "Logs"}]).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            posted.append(
                (self.headers["Authorization"], json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            )
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"id": 1}')

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        titles = annotate_grafana(f"http://127.0.0.1:{server.server_port}", 10.5, 20.25, "noise")
    finally:
        server.shutdown()
    assert titles == ["OAI", "Logs"]
    assert posted[0][0].startswith("Basic ")
    assert posted[0][1] == {
        "dashboardUID": "a1", "time": 10500, "timeEnd": 20250, "tags": ["interference"], "text": "noise"
    }  # fmt: skip


def test_noise_on_a_server_runs_through_its_ssh(monkeypatch, tmp_path):
    noise = NoiseGenerator(
        "s",
        0,
        freq=3477.5e6,
        rate=40e6,
        gain=60,
        usrp_args=interference.N3XX_USRPS["n320"],
        ssh=["ssh", "root@sopnode-f1"],
        environment=interference.UHD_ENVIRONMENT,
    )
    remote = noise.remote()
    assert remote[:2] == ["ssh", "root@sopnode-f1"]
    assert remote[2].startswith("LD_LIBRARY_PATH=/usr/local/lib LD_PRELOAD=/usr/local/lib/libuhd.so.4.7.0")
    assert "--args product=n320,serial=31B3A77" in remote[2] and remote[2].endswith(" -v")


def test_power_n3xx_and_wait_usrp(monkeypatch):
    calls = []

    async def fake_run(args, timeout):
        calls.append(args[-1])
        if "uhd_find_devices" in args[-1] and len(calls) < 3:
            raise RuntimeError("No UHD Devices Found")
        return "ok"

    monkeypatch.setattr(interference, "_run", fake_run)
    asyncio.run(interference.power_n3xx("s", "n320", on=True))
    assert calls == ["rhubarbe pdu on n320"]
    with pytest.raises(ValueError):
        asyncio.run(interference.power_n3xx("s", "x310", on=True))
    assert asyncio.run(interference.wait_usrp(["ssh", "h"], "addr=192.168.235.105", period=0)) == "ok"
    assert calls[-1].endswith("uhd_find_devices --args addr=192.168.235.105")


def test_prepare_fit_node_downloads_missing_uhd_images(monkeypatch):
    commands = []

    async def run(args, timeout):
        commands.append(args[-1])
        return "found" if args[-1] == "uhd_find_devices" else ""

    async def sleep(_):
        return None

    monkeypatch.setattr(interference, "_run", run)
    monkeypatch.setattr(interference.asyncio, "sleep", sleep)
    assert asyncio.run(interference.prepare_fit_node("slice", 2)) == "found"
    assert commands == [
        "rhubarbe usrpoff 2",
        "rhubarbe load -t 600 -i gnuradio 2",
        "rhubarbe wait -t 60 2",
        "rhubarbe usrpon 2",
        interference.FIT_UHD_IMAGES,
        "uhd_find_devices",
    ]


def test_node_inventory_lists_a_node_without_role():
    from types import SimpleNamespace

    from ifran.models.provisioning import SSHInfo

    node = SimpleNamespace(name="sopnode-f1", ssh_info=SSHInfo(hostname="172.28.2.76", user="root"))
    manager = SimpleNamespace(
        deployment_model=SimpleNamespace(nodes={"sopnode-f1": node}, params=SimpleNamespace(ssh=SSHInfo()))
    )
    inventory = interference.node_inventory(manager, "sopnode-f1")
    assert inventory.startswith("[noise_nodes]\nsopnode-f1 ansible_host=172.28.2.76 ")
    assert "ansible_user=root" in inventory


CSI_HEADER = (
    '# { "granularity": "rb", "format_version": "3.1", "columns": [ "frame", "slot", "rnti", "ant_rx", "port_tx", '
    '"rb", "real", "imag" ] }\nframe,slot,rnti,ant_rx,port_tx,rb,real,imag\n# TIMESTAMP: 2026-11-28 20:01:18\n'
)


def test_csi_rb_stats_per_ue_without_empty_rbs(tmp_path):
    rows = [
        "164,4,0xc6f9,0,0,0,100,0",  # iperf UE, RB 0: 40 dB
        "164,4,0xc6f9,1,0,0,100,0",
        "164,4,0xc6f9,0,0,1,1000,0",  # RB 1: 60 and 40 dB
        "164,4,0xc6f9,1,0,1,100,0",
        "164,4,0xc6f9,0,0,2,0,0",  # RB 2 without SRS: left out, not -200 dB
        "164,4,0x224b,0,0,0,10,0",  # another UE
    ]
    path = tmp_path / "csi_per_rb.csv.gz"
    with gzip.open(path, "wt") as fp:
        fp.write(CSI_HEADER + "\n".join(rows) + "\n")
    rbs, mean_db, std_db, used = interference.csi_rb_stats(path, "c6f9")
    assert rbs == [0, 1] and used == 4
    assert mean_db[0] == pytest.approx(40) and std_db[0] == pytest.approx(0)
    assert mean_db[1] == pytest.approx(10 * math.log10((1e6 + 1e4) / 2)) and std_db[1] == pytest.approx(10)
    assert interference.csi_rb_stats(path, "0x224b")[3] == 1
    assert interference.csi_rb_stats(path)[3] == 5  # all UEs


def test_traffic_rnti():
    means = {
        "ran_ul_goodput_mbps:oai_gnb_mac_ul_goodput_mbps{rnti=224b}": {"uplink_baseline": 0.21},
        "ran_ul_goodput_mbps:oai_gnb_mac_ul_goodput_mbps{rnti=c6f9}": {"uplink_baseline": 129.69},
        "ran_dl_goodput_mbps:oai_gnb_mac_dl_goodput_mbps{rnti=c6f9}": {"downlink_baseline": 203.65},
        "ran_dl_goodput_mbps:oai_gnb_mac_dl_goodput_mbps{rnti=5ba7}": {"downlink_baseline": 0.0},
        "ran_cqi:oai_gnb_mac_cqi{rnti=9ca3}": {"uplink_baseline": 500.0},
    }
    assert interference.traffic_rnti(means, "uplink") == "c6f9"
    assert interference.traffic_rnti(means, "downlink") == "c6f9"
    assert (
        interference.traffic_rnti({"slice_throughput_bps:slice_throughput": {"uplink_baseline": 1.0}}, "uplink") is None
    )
