**********************
F1 handover with OCUDU
**********************

.. note::
   Documentation of ``nador.handover.ocudu``. From NADOR 1.1.1 (https://github.com/ifran-project/nador/tree/v1.1.1), written by docs/nador_guides.py: do not edit.

Analysis of an OCUDU F1 handover: how it is triggered, and which CU-CP and DU log lines prove each step.

The steps of the handover and their numbering are explained in the package documentation (:mod:`nador.handover`).
This module only says how OCUDU implements them and how to recognise them in the OCUDU logs.

Triggering the handover: the remote control server
==================================================

OCUDU has no telnet server. The CU-CP is driven through its **remote control server**: a WebSocket server inside the
CU-CP application, enabled in the CU-CP configuration::

    remote_control:
      enabled: true
      bind_addr: 0.0.0.0     # reachable at the IP address of the pod
      port: 8001

A client opens a WebSocket connection to ``ws://<CU-CP pod IP>:8001`` and sends JSON messages; each message names a
command in its ``cmd`` field, and the server answers each one with a JSON object (``timestamp`` when the command is
accepted, ``error`` with the reason when it is refused). The handover command::

    {"cmd": "trigger_handover", "serving_pci": 1, "rnti": 17930, "target_pci": 2, "plmn": "00101", "tac": 1}

- The UE is named by **its cell and its C-RNTI in that cell**: ``serving_pci`` and ``rnti`` (an integer: 0x460a is
  17930). The C-RNTI changes at each attachment of the UE: read it again (:func:`connected_ues`) after each new
  connection.
- The target cell is named by its PCI, with the PLMN (string of 5 or 6 digits) and TAC of the target cell.
- An unknown UE or target cell is refused at once (``"error": "CU-CP rejected trigger_handover: unknown UE or target
  cell..."``). An accepted command only means that the handover started; only the logs tell whether it succeeded.

The same server serves the metrics: with ``metrics: enable_json: true``, a client that sends ``{"cmd":
"metrics_subscribe"}`` then receives the JSON metrics of the application; NADOR's ``ocudu-metrics`` exporter
(``ran_kpis``) uses it for Prometheus. The server has no authentication: keep it on the cluster network.

Why not the console: the CU-CP console accepts ``ho <serving PCI> <C-RNTI> <target PCI> <PLMN> <TAC>``, which
reaches the same trigger. The CU-CP runs the trigger on its own executor and waits at most 5 s for it
(``dispatch_bounded``, lib/cu_cp/bounded_executor_dispatch.h). From the console thread (``workers.
get_cmd_line_executor()``), this always timed out on the R2Lab CU-CP (``Dispatch trigger_handover timed out after 5s.
The command was cancelled and does not run``); from the thread of the remote control server (``ws_server``), it runs.

Reading the OCUDU logs
======================

Three kinds of identifiers appear, and they are easy to mix up:

- **UE index of the CU-CP** (``ue=8``): the CU-CP numbers its UEs 0, 1, ... The handover creates a **second** index
  for the same UE on the target DU (``ue=15``); the DUs have their own UE indexes (``ue=0`` in a DU log).
- **gNB-DU ID** (``du=1`` in the F1AP lines of the CU-CP): the ``gnb_du_id`` of the DU configuration.
- **DU index** (``source_du=0``, ``target_du=1`` in the mobility lines of the CU-CP): the order in which the DUs
  connected to the CU-CP. It changes when a DU is redeployed: do not rely on it to name a DU.

At the ``info`` level, the CU-CP logs every F1AP message (``Tx PDU du=<gNB-DU ID> ue=<ue> ...: <message>``) and the
RRC messages of each UE (``ue=<ue> c-rnti=<rnti>: DCCH UL <message>``). The DU logs the creation of each UE by its MAC
(``[MAC] ... proc="MAC UE Creation"``) and the PRACH its scheduler detects (``[SCHED] ... Processed slot events
pci=<pci>: prach(...)``). Keep the PHY of the DUs at ``warning`` (``log: phy_level: warning``): at ``info`` it writes
a line per PUCCH, PDSCH and PUSCH, kubelet then rotates the container log within seconds, and the lines of the
handover are gone before the analysis reads them.

Each step and the line that proves it (OCUDU image 920f88c7):

=========== ========= ================================================================================================
step        log of    line that proves it
=========== ========= ================================================================================================
decision    CU-CP     ``ue=<ue>: Trigger intra-CU (inter-DU) handover from source_du=<s> to target_du=<t>``
preparation CU-CP     ``Tx PDU du=<target> ue=<target ue> ...: UEContextSetupRequest``, then ``Rx PDU du=<target>
                      ...: UEContextSetupResponse`` (``UEContextSetupFailure``: the target DU refused the UE)
            target DU ``ue=<ue> crnti=<new rnti> proc="MAC UE Creation": finished successfully``: the new C-RNTI
command     CU-CP     ``Tx PDU du=<source> ue=<ue> ...: UEContextModificationRequest`` (it carries the
                      RRCReconfiguration for the UE)
preamble    target DU ``Processed slot events pci=<target PCI>: prach(ra-rnti=<r> preamble=<p> ssb=<s>
                      tc-rnti=<new rnti>)``: with contention-free random access (``prach: cfra_enabled: true``), the
                      dedicated preamble comes with the new C-RNTI. With contention-based access, the UE gets a
                      temporary RNTI and gives its C-RNTI in Msg3, which the DU logs only at the ``debug`` level
response    target DU ``Slot decisions pci=<target PCI> ...: RAR: ra-rnti=<r> ...`` after the dedicated preamble, with
                      its RA-RNTI. A RAR and then the preamble again: the UE did not decode the RAR
completion  CU-CP     ``ue=<target ue> c-rnti=<new rnti>: DCCH UL rrcReconfigurationComplete``, or
                      ``Rx PDU du=<target> ue=<target ue> ...: ULRRCMessageTransfer``
release     CU-CP     ``Tx PDU du=<source> ue=<ue> ...: UEContextReleaseCommand``
=========== ========= ================================================================================================

When the completion is there but the target DU log has no PRACH line, the DU log does not cover the handover
(rotated, or another log level): the preamble step is then reported as not visible, not as missing.

After a failure, the CU-CP logs ``"Intra CU Handover Routine" failed ...`` (it gave up: target DU refused the UE, RRC
reconfiguration not sent), ``UEContextReleaseCommand`` to the target DU for the target UE (handover cancelled or timed
out), ``CCCH UL rrcReestablishmentRequest`` (re-establishment) or ``CCCH UL rrcSetupRequest`` (new attachment).

Validation: the lines of a successful handover and of the UE attachments were checked against R2Lab logs (OCUDU
image 920f88c7, 20, 23 and 24 November 2026, the RAR on the 24th), the remote command against the OCUDU source
(``main``, November 2026). The failure lines come from the srsRAN Project source (``main``, February 2026), from which
OCUDU derives: adapt their patterns if a failure is not recognised.
