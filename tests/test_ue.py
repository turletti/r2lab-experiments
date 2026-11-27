"""connect_ues: PDU session of each UE checked in the SMF log, reconnection of a UE without session."""

import asyncio
from types import SimpleNamespace

import pytest

from r2lab_experiments import ue as ue_module
from r2lab_experiments.ue import connect_ues, smf_sessions

SMF_LINE = "11/27 19:48:21.880: [smf] INFO: UE SUPI[imsi-{supi}] DNN[internet] IPv4[{ip}] IPv6[] (../src/smf/npcf-handler.c:647)"
SUPIS = {"qhat01": "001010000000006", "qhat02": "001010000000007", "qhat03": "001010000000008"}


class FakeUE:
    """UEManagement with the radio API of the R2Lab UEs: a connect is lost while the UE is not registered."""

    def __init__(self, smf: list[str], lost: dict[str, int], status: dict[str, str] | None = None):
        self.mutable_metamodel = SimpleNamespace(
            ues={
                name: SimpleNamespace(type=SimpleNamespace(value="cots"), config=SimpleNamespace(supi=supi))
                for name, supi in SUPIS.items()
            }
        )
        status = status or {}
        self.deployment_model = SimpleNamespace(
            ues={name: SimpleNamespace(connection_status=status.get(name, "configured")) for name in SUPIS}
        )
        self.smf, self.lost, self.calls = smf, dict(lost), []
        self.address = 1

    async def connect(self, ues):
        self.calls.append(("connect", list(ues)))
        for name in ues:
            self.deployment_model.ues[name].connection_status = "connected"
            if self.lost.get(name, 0) > 0:  # answered 200, no session
                self.lost[name] -= 1
                continue
            self.address += 1
            self.smf.append(SMF_LINE.format(supi=SUPIS[name], ip=f"11.1.1.{self.address}"))

    async def disconnect(self, ues, force=False):
        self.calls.append(("disconnect", list(ues)))
        for name in ues:
            self.deployment_model.ues[name].connection_status = "configured"


class FakeCore:
    def __init__(self, smf: list[str]):
        self.smf, self.windows = smf, []

    async def get_logs(self, components, containers, since_seconds, quiet):
        assert components == ["smf"] and containers == ["smf"] and quiet
        self.windows.append(since_seconds)
        return "\n".join(self.smf)


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    async def sleep(_):
        return None

    monkeypatch.setattr(ue_module.asyncio, "sleep", sleep)


def run(ue, core, **kwargs):
    return asyncio.run(connect_ues(ue, core, poll=0, **kwargs))


def test_smf_sessions():
    log = "\n".join([SMF_LINE.format(supi=SUPIS["qhat01"], ip="11.1.1.15"), "other",
                     SMF_LINE.format(supi=SUPIS["qhat02"], ip="11.1.1.3"),
                     SMF_LINE.format(supi=SUPIS["qhat01"], ip="11.1.1.2")])  # fmt: skip
    assert smf_sessions(log, SUPIS["qhat01"]) == [("internet", "11.1.1.15"), ("internet", "11.1.1.2")]
    assert smf_sessions(log, SUPIS["qhat03"]) == []
    assert smf_sessions(log, "00101000000000") == []  # a SUPI is not matched by a prefix


def test_all_sessions_at_first_connect():
    smf: list[str] = []
    ue, core = FakeUE(smf, lost={}), FakeCore(smf)
    assert run(ue, core) == {"qhat01": "11.1.1.2", "qhat02": "11.1.1.3", "qhat03": "11.1.1.4"}
    assert ue.calls == [("connect", ["qhat01", "qhat02", "qhat03"])]
    assert all(window >= 1 for window in core.windows)


def test_lost_connect_reconnected():
    # the run of 2026-11-27: qhat01 connected before it registered, then reconnected
    smf: list[str] = []
    ue, core = FakeUE(smf, lost={"qhat01": 1}), FakeCore(smf)
    assert run(ue, core, ues=["qhat01"], timeout=0.05) == {"qhat01": "11.1.1.2"}
    assert ue.calls == [("connect", ["qhat01"]), ("disconnect", ["qhat01"]), ("connect", ["qhat01"])]


def test_only_the_ue_without_session_is_reconnected():
    smf: list[str] = []
    ue, core = FakeUE(smf, lost={"qhat02": 1}), FakeCore(smf)
    assert set(run(ue, core, ues=["qhat01", "qhat02"], timeout=0.05)) == {"qhat01", "qhat02"}
    assert ue.calls == [("connect", ["qhat01", "qhat02"]), ("disconnect", ["qhat02"]), ("connect", ["qhat02"])]


def test_old_session_of_a_connected_ue_is_not_taken():
    # cell run again: qhat01 is marked connected and the SMF log already has its old session
    smf = [SMF_LINE.format(supi=SUPIS["qhat01"], ip="11.1.1.15")]
    ue, core = FakeUE(smf, lost={}, status={"qhat01": "connected"}), FakeCore(smf)
    assert run(ue, core, ues=["qhat01"]) == {"qhat01": "11.1.1.2"}
    assert ue.calls == [("disconnect", ["qhat01"]), ("connect", ["qhat01"])]


def test_no_session_after_all_attempts():
    smf: list[str] = []
    ue, core = FakeUE(smf, lost={"qhat03": 5}), FakeCore(smf)
    with pytest.raises(RuntimeError, match="no PDU session in the SMF for qhat03 after 3 connections"):
        run(ue, core, ues=["qhat03"], timeout=0.02)
    assert [call[0] for call in ue.calls].count("connect") == 3
