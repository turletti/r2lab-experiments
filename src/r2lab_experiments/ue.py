"""
COTS UE connection checked against the core: each UE must get a PDU session, or it is connected again.

The R2Lab radio API accepts a connect sent before the UE registered with the core (for instance while the cell is
coming up): it answers 200 and sets an address on ``wwan0``, but the modem never requests the PDU session (MBIM
connection state ``deactivated``, no session in the SMF). IFRAN then marks the UE connected, so a second connect is
skipped. ``connect_ues`` waits for the session of each UE in the Open5GS SMF log, and disconnects and connects again
a UE that has none.
"""

import asyncio
import math
import re
import time
from typing import Any

# Open5GS SMF, once the session policy is set: UE SUPI[imsi-001010000000006] DNN[internet] IPv4[11.1.1.2] IPv6[]
SMF_SESSION = r"UE SUPI\[imsi-{supi}\] DNN\[([^\]]*)\] IPv4\[([0-9.]*)\]"


def smf_sessions(smf_log: str, supi: str) -> list[tuple[str, str]]:
    """(DNN, IPv4 address) of each PDU session that the Open5GS SMF created for this SUPI, in log order."""
    return re.findall(SMF_SESSION.format(supi=re.escape(supi)), smf_log)


async def _new_sessions(core: Any, supis: dict[str, str], since: float) -> dict[str, tuple[str, str]]:
    """Last PDU session of each UE created since the monotonic time since, from the SMF log."""
    window = math.ceil(time.monotonic() - since) + 1
    log = await core.get_logs(components=["smf"], containers=["smf"], since_seconds=window, quiet=True)
    found = {}
    for name, supi in supis.items():
        if sessions := smf_sessions(str(log), supi):
            found[name] = sessions[-1]
    return found


async def connect_ues(
    ue: Any,
    core: Any,
    ues: list[str] | None = None,
    attempts: int = 3,
    timeout: float = 90,
    poll: float = 10,
) -> dict[str, str]:
    """
    Connect COTS UEs and wait until the SMF has created a PDU session for each one.

    A UE without session after timeout seconds is disconnected and connected again, up to attempts connections.
    ue is the UEManagement of the notebook, core its CoreManagement (Open5GS). Returns the IPv4 address of each UE.
    """
    metamodel_ues = ue.mutable_metamodel.ues
    names = ues if ues is not None else [name for name, meta in metamodel_ues.items() if meta.type.value == "cots"]
    supis = {name: metamodel_ues[name].config.supi for name in names}
    addresses: dict[str, str] = {}
    pending = list(names)
    # a UE already marked connected (cell run again) is disconnected first: IFRAN would skip its connect, and its
    # session, if any, would be an old one
    if stale := [name for name in pending if str(ue.deployment_model.ues[name].connection_status) == "connected"]:
        await ue.disconnect(ues=stale)
    for attempt in range(1, attempts + 1):
        if attempt > 1:
            print(f"no PDU session for {', '.join(pending)}: disconnect and connect again ({attempt}/{attempts})")
            await ue.disconnect(ues=pending, force=True)
        start = time.monotonic()
        await ue.connect(ues=pending)
        while pending and time.monotonic() - start < timeout:
            await asyncio.sleep(poll)
            for name, (dnn, address) in (await _new_sessions(core, {n: supis[n] for n in pending}, start)).items():
                print(f"{name} (imsi-{supis[name]}): PDU session on DNN {dnn}, address {address}")
                addresses[name] = address
                pending.remove(name)
        if not pending:
            return addresses
    raise RuntimeError(
        f"no PDU session in the SMF for {', '.join(pending)} after {attempts} connections: check that the cell is on "
        "the air (DU log) and that the UE registered (AMF log, 'Registration complete')"
    )
