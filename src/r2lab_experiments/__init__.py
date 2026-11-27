"""
R2Lab experiments with NADOR and IFRAN: what the notebooks need and neither tool provides yet.

Everything here is temporary: it goes away once IFRAN supports O-RAN 7.2 radio units (Benetel RAN550) for OCUDU and
OAI, and triggers handovers itself.

- ``OAI_CHARTS``: the OAI charts of IFRAN's fhi72 branch with the NADOR monitoring additions (metrics-parser sidecar
  reading the 2026.w46 UE header, CSI file on a hostPath, CSI flush thread out of the L1 thread pool). On an O-RAN 7.2
  node, their values set the radio fhi72, the SR-IOV VFs and the 1G hugepages from ``miscellaneous``.
- ``OCUDU_CHARTS``: the OCUDU gNB and DU charts of IFRAN with the O-RAN 7.2 fronthaul (SR-IOV VF, 1G hugepages, VF
  and CPU placeholders replaced at start), and the OCUDU templates that apply both the CSI logger SRS and the O-RAN 7.2
  edits (no ru_sdr section with ru_ofh, DPDK hal section, SSB power).
- ``name_oai_fhi72_csi_images``: NADOR 1.1.1 knows no CSI image for the O-RAN 7.2 OAI image.
- ``use_ocudu_fhi72_files``: NADOR 1.1.1 renders OCUDU components that log CSI with its own templates, which do not
  apply the O-RAN 7.2 edits.
- ``r2lab_experiments.handover.run_handover``: triggers an F1 handover and reports it with NADOR's HandoverWatch.
- ``r2lab_experiments.ue.connect_ues``: connects COTS UEs and checks the PDU session of each one in the SMF log,
  connecting again a UE without session (connect accepted by the radio API before the UE registered).
- ``SSHInfo.common_args``: NADOR 1.1.1 reads this attribute of IFRAN 0.0.6, which IFRAN develop replaced by
  ``get_common_args()``; importing this package gives it back.
"""

from importlib.resources import files


def _ssh_common_args_compat() -> None:
    """Give IFRAN develop's SSHInfo the ``common_args`` of IFRAN 0.0.6, read by NADOR 1.1.1 (SSH tunnels, CSI)."""
    try:
        from ifran.models.provisioning import SSHInfo
    except ImportError:  # IFRAN is installed by the first cell of the notebooks
        return
    if not hasattr(SSHInfo, "common_args") and hasattr(SSHInfo, "get_common_args"):
        SSHInfo.common_args = property(SSHInfo.get_common_args)


_ssh_common_args_compat()

ASSETS = files(__name__) / "assets"
OAI_CHARTS = ASSETS / "ran" / "oai"
OCUDU_CHARTS = ASSETS / "ran" / "ocudu"

# O-RAN 7.2 OAI image -> its CSI logger build (R2Lab OAI CI, oai-csi-logging), same tag
OAI_FHI72_CSI_REPOSITORIES = {
    "docker.io/oaisoftwarealliance/oai-gnb-fhi72": "docker.io/r2labuser/oai-gnb-fhi72-csi",
    "docker.io/r2labuser/oai-gnb-fhi72": "docker.io/r2labuser/oai-gnb-fhi72-csi",
}


def oai_rendered_files() -> list[str]:
    """Values and configuration templates of ``OAI_CHARTS``."""
    return [str(OAI_CHARTS / "values.yaml.j2"), str(OAI_CHARTS / "config.yaml.j2")]


def ocudu_rendered_files() -> list[str]:
    """Values and configuration templates of ``OCUDU_CHARTS``."""
    return [str(OCUDU_CHARTS / "values.yaml.j2"), str(OCUDU_CHARTS / "config.yaml.j2")]


def csi_component_names(ran, config) -> list[str]:
    """OAI and OCUDU gNBs and DUs that log CSI with this profile (``csi.components``, else all of them)."""
    csi = config.metrics.csi
    if csi is None:
        return []
    components = ran.deployment_model.ran.components
    radios = [
        name
        for name, component in components.items()
        if str(component.implementation) in ("oai", "ocudu") and str(component.type) in ("gnb", "du")
    ]
    return [name for name in radios if not csi.components or name in csi.components]


def name_oai_fhi72_csi_images(ran, config) -> list[str]:
    """
    Give the OAI O-RAN 7.2 radios that log CSI their CSI logger image, before ``configure_ran_monitoring``.

    NADOR 1.1.1 refuses a CSI component whose image it does not know (``oai-gnb-fhi72``); it keeps an image whose
    repository ends with ``-csi``. Returns the names of the components changed.
    """
    changed = []
    for name in csi_component_names(ran, config):
        component = ran.deployment_model.ran.components[name]
        if component.image is None or str(component.implementation) != "oai":
            continue
        repository = OAI_FHI72_CSI_REPOSITORIES.get(component.image.repository)
        if repository is not None:
            component.image = component.image.model_copy(update={"repository": repository})
            changed.append(name)
    return changed


def use_ocudu_fhi72_files(ran) -> list[str]:
    """
    Render the OCUDU gNBs and DUs on O-RAN 7.2 charts with this repository's templates, after
    ``configure_ran_monitoring``.

    NADOR 1.1.1 sets its own OCUDU templates on the components that log CSI, and refuses others before; these templates
    apply the same CSI logger SRS and the O-RAN 7.2 edits. Only the components on ``OCUDU_CHARTS`` change. Returns
    their names.
    """
    changed = []
    for name, component in ran.deployment_model.ran.components.items():
        if str(component.implementation) != "ocudu" or str(component.type) not in ("gnb", "du"):
            continue
        if component.custom_chart_path in (str(OCUDU_CHARTS / "ocudu-gnb"), str(OCUDU_CHARTS / "ocudu-du")):
            component.custom_rendered_files = ocudu_rendered_files()
            changed.append(name)
    return changed
