"""
Trigger an F1 handover on the testbed and check it: the R2Lab layer on top of :class:`nador.handover.HandoverWatch`.

Temporary: triggering a handover acts on the RAN, so it belongs to IFRAN (its RAN manager); this module goes away
once IFRAN does it. It uses attributes of ``HandoverWatch`` of NADOR 1.1.0 (``ran``, ``cucp``, ``charts``,
``namespace``, ``master``, ``nodes``, ``_before``, ``_kubectl``).

``HandoverWatch`` only observes a handover; ``run_handover`` also sends the command to the CU-CP, waits, gets the
report and pings from the UE:

- OAI: ``ci trigger_f1_ho <CU UE ID>`` on the telnet server of the CU-CP (``--telnetsrv --telnetsrv.shrmod ci
  --telnetsrv.listenport <telnet_port>``; not 9090, the port of the metrics-parser sidecar in the same pod). The whole
  answer is read before closing: with OAI f80a883, closing earlier reset the connection and the telnet server aborted
  the CU-CP ("buffer overflow detected").
- OCUDU: the ``trigger_handover`` JSON command on the remote control server of the CU-CP (WebSocket), sent from the
  Kubernetes master by a standard-library client.
"""

import asyncio
import json
import shlex

from ifran.deployment import ProvisioningManagement, RANManagement
from nador.handover.ocudu import format_rnti
from nador.handover.report import HandoverReport
from nador.handover.watch import HandoverWatch

__all__ = ["run_handover", "trigger_command", "trigger_request"]

# WebSocket client run with python3 on the Kubernetes master (standard library only): sends one text message to
# ws://<host>:<port>/ and prints the first text message received (remote control server of the OCUDU CU-CP).
WS_CLIENT = """
import base64, os, socket, struct, sys
host, port, message = sys.argv[1], int(sys.argv[2]), sys.argv[3].encode()
sock = socket.create_connection((host, port), timeout=10)
key = base64.b64encode(os.urandom(16)).decode()
sock.sendall((f"GET / HTTP/1.1\\r\\nHost: {host}:{port}\\r\\nUpgrade: websocket\\r\\nConnection: Upgrade\\r\\n"
              f"Sec-WebSocket-Key: {key}\\r\\nSec-WebSocket-Version: 13\\r\\n\\r\\n").encode())
data = b""
def read(size):
    global data
    while len(data) < size:
        chunk = sock.recv(4096)
        if not chunk:
            sys.exit("connection closed by the server")
        data += chunk
    out, data = data[:size], data[size:]
    return out
while b"\\r\\n\\r\\n" not in data:
    chunk = sock.recv(4096)
    if not chunk:
        sys.exit("connection closed by the server")
    data += chunk
head, data = data.split(b"\\r\\n\\r\\n", 1)
if b" 101 " not in head.split(b"\\r\\n")[0]:
    sys.exit("handshake refused: " + head.decode(errors="replace"))
mask = os.urandom(4)
size = len(message)
header = bytes([0x81, 0x80 | size]) if size < 126 else bytes([0x81, 0x80 | 126]) + struct.pack("!H", size)
sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(message)))
while True:
    first, second = read(2)
    size = second & 0x7F
    if size == 126:
        size = struct.unpack("!H", read(2))[0]
    elif size == 127:
        size = struct.unpack("!Q", read(8))[0]
    payload = read(size)
    if first & 0x0F == 1:
        print(payload.decode())
        break
    if first & 0x0F == 8:
        sys.exit("connection closed by the server")
"""


def trigger_command(ue_id: int, telnet_port: int) -> str:
    """Shell command, run in the OAI CU-CP container, that sends ``ci trigger_f1_ho <ue_id>`` and reads the answer."""
    return (
        f"exec 3<>/dev/tcp/127.0.0.1/{telnet_port}; "
        f'printf "ci trigger_f1_ho {ue_id}\\n" >&3; '
        'while IFS= read -r -t 2 line <&3; do printf "%s\\n" "$line"; done; '
        "exec 3<&-; exec 3>&-"
    )


def trigger_request(serving_pci: int, rnti: int | str, target_pci: int, target_plmn: str, target_tac: int) -> dict:
    """
    Remote command of the OCUDU CU-CP that hands the UE of C-RNTI rnti over from serving_pci to target_pci.

    target_plmn (5 or 6 digits, 00101) and target_tac are those of the target cell.
    """
    return {
        "cmd": "trigger_handover",
        "serving_pci": serving_pci,
        "rnti": int(format_rnti(rnti), 16),
        "target_pci": target_pci,
        "plmn": target_plmn,
        "tac": target_tac,
    }


async def _trigger_oai(watch: HandoverWatch, ue: int, telnet_port: int) -> tuple[str, str]:
    """Send ci trigger_f1_ho to the OAI CU-CP telnet server: (command, answer)."""
    answer = await watch.ran.run_command(
        command=["bash", "-c", shlex.quote(trigger_command(ue, telnet_port))],
        components=[watch.cucp],
        quiet=True,
        flatten=False,
    )
    return f"ci trigger_f1_ho {ue}", str(answer[watch.cucp]).strip()  # type: ignore[index]


async def _trigger_ocudu(watch: HandoverWatch, rnti: int | str) -> tuple[str, str]:
    """Send trigger_handover to the remote control server of the OCUDU CU-CP: (request, answer)."""
    ran = watch.ran
    # advanced_config is the IFRAN model of the CU-CP (OCUDUAdvancedCUCPConfig), remote_control a dict
    advanced_config = ran.deployment_model.ran.components[watch.cucp].advanced_config
    remote_control = getattr(advanced_config, "remote_control", None) or {}
    if not remote_control.get("enabled"):
        raise ValueError(
            f"remote control of {watch.cucp} disabled: set advanced_config.remote_control "
            "{enabled: true, bind_addr: 0.0.0.0, port: 8001} in the deployment model of the CU-CP"
        )
    configs = [ran.metamodel.ran.components[name].config for name in (watch.source, watch.target)]  # type: ignore[union-attr]
    request = json.dumps(
        trigger_request(
            configs[0].physicalCellId,
            rnti,
            configs[1].physicalCellId,
            configs[1].plmnList[0].plmnId.plmnId,
            configs[1].tac,
        )
    )
    pod = next((p for p in (watch._before or {}) if p.startswith(watch.charts[watch.cucp] + "-")), None)
    if pod is None:
        raise RuntimeError(f"no pod of {watch.charts[watch.cucp]} in namespace {watch.namespace}")
    address = await watch._kubectl("get", "pod", pod, "-o", "jsonpath={.status.podIP}")
    answer = await watch.nodes.run_node_command(
        command=["python3", "-c", WS_CLIENT, address.strip(), str(remote_control.get("port", 8001)), request],
        nodes=[watch.master],
        become=True,
        quiet=True,
    )
    return request, str(answer).strip()


async def run_handover(
    ran: RANManagement,
    ue: int | str,
    *,
    source: str,
    target: str,
    cucp: str = "cucp",
    telnet_port: int = 9091,
    wait_s: float = 15,
    ue_manager: ProvisioningManagement | None = None,
    ue_name: str | None = None,
    ue_interface: str = "wwan0",
    ping_address: str = "11.1.1.1",
    quiet: bool = False,
    explain: bool = True,
) -> HandoverReport:
    """
    Hand a UE over from the DU source to the DU target, then check each step of the handover in the logs.

    ``ue`` is the CU UE ID with OAI, the C-RNTI of the UE in the source cell (``0x4601``) with OCUDU. After wait_s
    seconds, :meth:`HandoverWatch.report` reads and analyzes the logs. With ue_manager and ue_name (the COTS UE handed
    over), the UE then pings ping_address over ue_interface. The report is printed unless quiet, and returned.
    """
    watch = await HandoverWatch(ran, source=source, target=target, cucp=cucp).start()
    if watch.implementation == "oai":
        command, answer = await _trigger_oai(watch, int(ue), telnet_port)
    else:
        command, answer = await _trigger_ocudu(watch, ue)
    await asyncio.sleep(wait_s)
    report = await watch.report(ue, answer=answer, quiet=True)
    report.command = command
    if ue_manager is not None and ue_name is not None:
        try:
            out = await ue_manager.run_ue_command(
                command=["sh", "-c", f"ping -c 3 -W 2 -I {ue_interface} {ping_address} | tail -2"],
                ues=[ue_name],
                quiet=True,
                flatten=False,
            )
            report.ping = f"{ue_name}: " + " / ".join(out[ue_name].strip().splitlines())  # type: ignore[index]
        except RuntimeError:
            report.ping = f"{ue_name}: FAILED (no reply or no address on {ue_interface})"
    if not quiet:
        print(report.to_text(explain=explain))
    return report
