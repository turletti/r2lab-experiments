# R2Lab experiments with NADOR and IFRAN

Notebooks that deploy a 5G network on [R2Lab](https://r2lab.inria.fr) and the SophiaNode servers with
[IFRAN](https://github.com/ifran-project/ifran), monitor it with [NADOR](https://github.com/ifran-project/nador)
(metrics, logs, CSI) and run experiments on it: iperf traffic, F1 handovers between two DUs.

NADOR is used as published (`nador==1.1.1`), without changes. What the notebooks need and neither tool provides yet
is in the `r2lab_experiments` package of this repository, and goes away once IFRAN has it.

## Notebooks

| Notebook | RAN | Radio units | Handover |
|---|---|---|---|
| `notebooks/oai-aw2s` | OAI gNB | AW2S Jaguar | – |
| `notebooks/oai-aw2s-2du` | OAI CU-CP, CU-UP, 2 DUs | AW2S Jaguar, Panther | F1, du01 ↔ du02 |
| `notebooks/oai-benetel` | OAI gNB, O-RAN 7.2 | Benetel RAN550 (benetel1) | – |
| `notebooks/oai-benetel-2du` | OAI CU-CP, CU-UP, 2 DUs, O-RAN 7.2 | benetel1, benetel2 | F1, du01 ↔ du02 |
| `notebooks/ocudu-benetel` | OCUDU gNB, O-RAN 7.2 | benetel1 | – |
| `notebooks/ocudu-benetel-2du` | OCUDU CU-CP, CU-UP, 2 DUs, O-RAN 7.2 | benetel1, benetel2 | F1, du01 → du02 |

Each notebook has its monitoring profile (`monitoring.yaml`), the Prometheus queries of its experiments
(`queries.json`) and, for the Benetel ones, the netplan of the RAN nodes (`netplan.yaml`).

The guides of `docs/guide/` explain what the notebooks measure and check, and the notebooks link to them: the CSI
([CSI guide](docs/guide/csi.md)) and the F1 handover ([procedure](docs/guide/handover-procedure.rst), how
[OAI](docs/guide/handover-oai.rst) and [OCUDU](docs/guide/handover-ocudu.rst) implement and log it, the
[handover report](docs/guide/handover.md) of NADOR and [how the notebooks trigger it](docs/guide/handover-trigger.rst)).

## Installation

Python 3.12 or later (not a conda Python on macOS), in a virtual environment at the root of this repository, which
is also the notebook kernel (select `.venv` in VS Code or Jupyter):

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e ".[dev,notebooks]"
```

The first cell of each notebook installs this repository (with `nador==1.1.1`) and then IFRAN: the `develop` branch
for the Benetel notebooks (UE configure, kubelet parameters, miscellaneous data in the rendered files, not yet in an
IFRAN release), `main` for the AW2S ones. Restart the kernel after it.

Next to the notebooks or at the root of this repository, a `.env` file (never committed) holds the R2Lab credentials:

```
R2LAB_SLICE=<your R2Lab slice>
R2LAB_EMAIL=<your R2Lab email>
R2LAB_PASSWORD=<your R2Lab password>
```

The SLICES CLI must be logged in (`slices auth login`), and the POS nodes (sopnode-f1 to f4) reserved.

## What `r2lab_experiments` provides, until IFRAN has it

- `OAI_CHARTS`: the OAI charts of IFRAN's `fhi72` branch with the NADOR monitoring additions (metrics-parser
  sidecar reading the 2026.w46 UE header, CSI file on a hostPath, CSI flush thread out of the L1 thread pool). On an
  O-RAN 7.2 node, their values set the radio `fhi72`, the SR-IOV VFs and the 1G hugepages from `miscellaneous`.
- `OCUDU_CHARTS`: the OCUDU gNB and DU charts with the O-RAN 7.2 fronthaul (SR-IOV VF, 1G hugepages, VF and CPU
  placeholders replaced at start), and their templates: CSI logger SRS, and with `ru_ofh` no `ru_sdr` section, a
  DPDK `hal` section and the SSB power (-28 dBm).
- Two adjustments around `configure_ran_monitoring` of NADOR 1.1.1, which knows no CSI image for the O-RAN 7.2 OAI
  image (`name_oai_fhi72_csi_images`, before it) and renders the OCUDU radios that log CSI with its own templates
  (`use_ocudu_fhi72_files`, after it).
- `r2lab_experiments.handover.run_handover`: sends the handover command to the CU-CP (OAI: `ci trigger_f1_ho` on its
  telnet server, port 9091, whole answer read before closing; OCUDU: `trigger_handover` on its remote control
  server), then reports it with NADOR's `HandoverWatch` and pings from the UE.

- `ansible/`: the Ansible collections of the notebooks (`requirements.yaml`) and their playbooks, run with
  `MiscellaneousManagement.run_playbook` and the `miscellaneous` entries of `r2lab_experiments.fhi72`:
  - `fhi72`: O-RAN 7.2 setup of the `du_nodes` (NIC, VFs, IRQs, CPUs, sysctl, SR-IOV device plugin, PTP), roles of
    Ziyad Mabrouk's benetel_example (963744a), with the linuxptp build always run;
  - `benetel`: Benetel RU configuration from the RAN node of each RU (tasks of IFRAN's former `ru/benetel/configure`
    role: the RUs have no Python).

## Documentation

Read the Docs builds this README, the guides and the notebooks as pages (`.readthedocs.yaml`). The guides are taken
from the documentation of the NADOR release in use by `docs/nador_guides.py` (run it again when the NADOR requirement
changes). To build the pages locally, from the root of the repository:

```bash
.venv/bin/python -m pip install -r docs/requirements.txt
PATH=.venv/bin:$PATH sh docs/convert.sh
.venv/bin/python -m sphinx -W -b html docs docs/_build/html
```

## Results

Each experiment writes its artifacts under `notebooks/<name>/results/`, which is not committed.
