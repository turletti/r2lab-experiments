"""
O-RAN 7.2 node and Benetel RU parameters, kept out of IFRAN in the deployment model ``miscellaneous`` field.

Same models and derivations as Ziyad Mabrouk's benetel_example (main.ipynb, commit 963744a). The result of
``fhi72_miscellaneous`` is read by the Ansible playbooks of this repository (``ansible/playbooks/fhi72.yaml`` and
``benetel.yaml``) and by the OAI and OCUDU charts of this package:

- ``fhi72_params``: per RAN node, the fronthaul NIC, VFs, CPU and PTP settings (``Fhi72NodeParams``)
- ``derived_fhi72_params``: per RAN node, the SR-IOV resources requested by the radio pod and their PCI env variables
- ``ru_config``: per RU, the ``/etc/ru_config.cfg`` values
- ``du_resources``: the 1G hugepages of the radio pods
"""

from typing import Literal, Self

from ifran.utils.radio import get_rbs, nr_arfcn_to_frequency_hz
from metamodel_3gpp.models.base import StrictBaseModel
from metamodel_3gpp.models.common import DuplexMode
from pydantic import Field, model_validator


class Fhi72VF(StrictBaseModel):
    """One SR-IOV virtual function used by an O-RAN 7.2 fronthaul plane."""

    resource_name: str = Field(pattern=r"^[A-Za-z0-9_]+$")
    plane: Literal["U", "C"]
    mac: str = Field(pattern=r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")
    vlan: int = Field(ge=1, le=4094)


class Fhi72PTPParams(StrictBaseModel):
    """PTP settings for the node's linuxptp services."""

    interface: str
    domain_number: int = Field(default=24, ge=0, le=255)
    network_transport: Literal["L2", "UDPv4", "UDPv6"] = "L2"
    log_announce_interval: int = 1
    log_sync_interval: int = -4
    linuxptp_version: str = "v4.4"


class Fhi72NodeParams(StrictBaseModel):
    """O-RAN 7.2 node settings consumed by the Ansible roles."""

    fronthaul_interface: str
    mtu: int = Field(default=9216, ge=1500, le=9728)
    vfs: list[Fhi72VF] = Field(min_length=2)
    resource_prefix: str = "openshift.io"
    hugepages_1g: int = Field(default=0, ge=0)
    ring_size: int | None = Field(default=8160, ge=64)
    reserved_cpus: str = "0-5"
    topology_manager_policy: Literal["none", "best-effort", "restricted", "single-numa-node"] = "best-effort"
    irq_pattern: str | None = None
    ptp: Fhi72PTPParams | None = None
    device_plugin_image: str = "ghcr.io/k8snetworkplumbingwg/sriov-network-device-plugin:v3.9.0"

    @model_validator(mode="after")
    def validate_planes(self) -> Self:
        planes = sorted(vf.plane for vf in self.vfs)
        if planes != ["C", "U"]:
            raise ValueError("Configure exactly one C-plane VF and one U-plane VF.")
        if len({vf.vlan for vf in self.vfs}) != 1:
            raise ValueError("The C-plane and U-plane VFs must use the same VLAN.")
        return self

    def vf(self, plane: Literal["U", "C"]) -> Fhi72VF:
        return next(vf for vf in self.vfs if vf.plane == plane)


class BenetelRUParams(StrictBaseModel):
    """Benetel RAN550 settings combined with the metamodel radio configuration."""

    tx_power_dbm: float = 24.0
    downlink_scaling: Literal[0, 6, 12, 18] = 0
    compression: Literal["static_uncompressed", "dynamic_uncompressed", "static_compressed", "dynamic_compressed"] = (
        "static_compressed"
    )
    prach_format: Literal["short", "long"] = "short"
    extra: dict[str, str] = Field(default_factory=dict)


def derived_fhi72_params(node_params: Fhi72NodeParams) -> dict:
    """SR-IOV resources of the radio pod (U-plane VF, then C-plane VF) and their PCI device env variables."""
    resources = [f"{node_params.resource_prefix}/{node_params.vf(plane).resource_name}" for plane in ("U", "C")]
    return {
        "sriov_resources": resources,
        "pci_device_env_vars": [
            "PCIDEVICE_" + resource.upper().replace(".", "_").replace("/", "_") for resource in resources
        ],
    }


def benetel_tdd_config(radio_config) -> dict[str, str]:
    """Benetel TDD pattern of a metamodel radio configuration."""
    tdd = radio_config.tddConfiguration
    if radio_config.duplexMode is not DuplexMode.TDD or tdd is None:
        raise ValueError("Benetel RU configuration supports TDD only.")

    slots = round(float(tdd.periodicityMilliseconds) * int(radio_config.subcarrierSpacingKhz) / 15)
    has_special_slot = tdd.nrofDownlinkSymbols > 0 or tdd.nrofUplinkSymbols > 0
    pattern = "D" * tdd.nrofDownlinkSlots + ("S" if has_special_slot else "") + "U" * tdd.nrofUplinkSlots
    if len(pattern) != slots:
        raise ValueError(f"TDD pattern {pattern!r} does not fill its {slots}-slot period.")
    if 2 * slots > 10:
        raise ValueError(f"Benetel supports at most five slots per TDD period; got {slots}.")

    guard_symbols = 14 - tdd.nrofDownlinkSymbols - tdd.nrofUplinkSymbols
    if guard_symbols < 0:
        raise ValueError("The TDD special slot cannot contain more than 14 downlink and uplink symbols.")

    return {
        "tdd_pattern_1": pattern,
        "tdd_pattern_2": pattern,
        "special_slots_symbols": "D" * tdd.nrofDownlinkSymbols + "G" * guard_symbols + "U" * tdd.nrofUplinkSymbols,
    }


def benetel_ru_config(metamodel, ru_name: str, component_name: str, node_params: Fhi72NodeParams,
                      params: BenetelRUParams) -> dict[str, str]:
    """/etc/ru_config.cfg values of a Benetel RU served by a metamodel gNB or DU on an O-RAN 7.2 node."""
    ru_meta = metamodel.rus[ru_name]
    radio_config = metamodel.ran.components[component_name].config.radioConfiguration

    mimo_modes = {(4, 4): "1_2_3_4_4x4", (4, 2): "1_2_3_4_4x2"}
    mimo_mode = mimo_modes.get((ru_meta.numberOfTxPorts, ru_meta.numberOfRxPorts))
    if mimo_mode is None:
        raise ValueError(
            f"Benetel RU {ru_name!r} does not support {ru_meta.numberOfTxPorts}T{ru_meta.numberOfRxPorts}R."
        )

    bandwidth_rb = get_rbs(radio_config.subcarrierSpacingKhz, radio_config.channelBandwidthMhz)
    center_frequency_hz = nr_arfcn_to_frequency_hz(radio_config.pointAFrequencyArfcn) + (
        bandwidth_rb * 12 * int(radio_config.subcarrierSpacingKhz) * 1000 // 2
    )
    vlan = f"{node_params.vf('U').vlan:x}"

    config = {
        "mimo_mode": mimo_mode,
        "downlink_scaling": str(params.downlink_scaling),
        "prach_format": params.prach_format,
        "compression": params.compression,
        "c_plane_du_mac": node_params.vf("C").mac.lower(),
        "u_plane_du_mac": node_params.vf("U").mac.lower(),
        "u_plane_du_vlan_uplink": vlan,
        "u_plane_du_vlan_downlink": vlan,
        "c_plane_du_vlan": vlan,
        "bandwidth_hz": str(radio_config.channelBandwidthMhz * 1_000_000),
        "centre_frequency_hz": str(center_frequency_hz),
        "tx_power_dbm": f"{params.tx_power_dbm:.6f}",
        **benetel_tdd_config(radio_config),
    }
    return config | params.extra


def fhi72_miscellaneous(metamodel, nodes: dict[str, Fhi72NodeParams],
                        rus: dict[str, tuple[str, str, BenetelRUParams]], hugepages_1gi: str) -> dict:
    """
    ``miscellaneous`` entries of the O-RAN 7.2 nodes and Benetel RUs.

    nodes: RAN node name -> its parameters. rus: RU name -> (RAN node of its radio, gNB or DU component name, RU
    parameters). hugepages_1gi: 1G hugepages of each radio pod (e.g. "2Gi").
    """
    return {
        "fhi72_params": {name: params.model_dump(mode="json") for name, params in nodes.items()},
        "derived_fhi72_params": {name: derived_fhi72_params(params) for name, params in nodes.items()},
        "ru_config": {
            ru_name: benetel_ru_config(metamodel, ru_name, component_name, nodes[node_name], params)
            for ru_name, (node_name, component_name, params) in rus.items()
        },
        "du_resources": {
            "requests": {"nf": {"hugepages_1gi": hugepages_1gi}},
            "limits": {"nf": {"hugepages_1gi": hugepages_1gi}},
        },
    }
