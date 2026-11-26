********************
F1 handover with OAI
********************

.. note::
   Documentation of ``nador.handover.oai``. From NADOR 1.1.1 (https://github.com/ifran-project/nador/tree/v1.1.1), written by docs/nador_guides.py: do not edit.

Analysis of an OAI F1 handover: how it is triggered, and which CU-CP and DU log lines prove each step.

The steps of the handover and their numbering are explained in the package documentation (:mod:`nador.handover`).
This module only says how OAI implements them and how to recognise them in the OAI logs.

Triggering the handover
=======================

The OAI CU-CP has a telnet server (options ``--telnetsrv --telnetsrv.shrmod ci``). The command is::

    ci trigger_f1_ho <CU UE ID>

- The UE is named by its **CU UE ID**: the number the CU-CP gives each UE when it attaches (1, 2, ...), shown in
  the CU-CP log (``Create UE context: CU UE ID 2``) and by :func:`connected_ues`.
- The target is **not** given: the CU-CP picks the next cell among the DUs connected to it (with two DUs, the other
  one).
- The CU-CP answers ``RRC F1 handover triggered for UE <id>`` when it starts the handover, or a refusal (unknown UE,
  handover already running, single cell...): see ``REFUSALS``. An accepted command only means that the handover
  started; only the logs tell whether it succeeded.

Reading the OAI logs
====================

- The CU-CP log has no timestamps; the DU log lines start with a time in seconds (``2098.765438``).
- RNTIs are printed in hexadecimal without ``0x`` (``RNTI 7e3e``), except in a few MAC lines (``rnti 0x9003``).
- During a handover the UE has two RNTIs: its C-RNTI in the source cell, and the new one allocated by the target DU
  (``RNTI 7e3e`` then ``RNTI 9003``). The CU UE ID does not change.

Each step and the line that proves it (OAI 2026.w42 to w46, default log level of the R2Lab notebooks):

=========== ========= ================================================================================================
step        log of    line that proves it
=========== ========= ================================================================================================
decision    CU-CP     ``Handover triggered for UE <id>/RNTI <rnti> towards cell <cell>/assoc_id <a>/PCI <pci>``
preparation target DU ``Added new CFRA process for UE RNTI <new rnti> ... preamble(s): <preamble>``: the target DU
                      created the UE, with its new C-RNTI and a dedicated preamble
command     CU-CP     ``HO acknowledged: Send reconfiguration for UE <id>/RNTI <rnti>``, then
                      ``UE <id>: update CU F1AP Context DU UE ID <a> => <b> and RNTI <rnti> => <new rnti>``
preamble    target DU ``TC-RNTI <new rnti>: initiating RA procedure for RAPID <preamble>``: the dedicated preamble
response    target DU ``(rnti 0x<new rnti>) CFRA procedure succeeded!``
completion  CU-CP     ``(cellID <target cell>, UE ID <id> RNTI <rnti>) Received RRCReconfigurationComplete``
release     CU-CP     ``UE <id> Handover: trigger release on cell PCI <pci>`` (after ``handover for UE <id>/RNTI
                      <rnti> complete!``)
=========== ========= ================================================================================================

Two lines need care:

- ``CFRA procedure succeeded!`` is written when the target DU **sends** the random access response, not when the UE
  receives it. It does not prove that the UE decoded the response, nor that its uplink works.
- What the target DU logs next tells what went wrong when the completion is missing:

  - ``ignoring RAPID <preamble>``: the UE sent its dedicated preamble again, so it did not decode the response
    (problem on the downlink of the target cell, at the UE);
  - ``Received a MAC CE for C-RNTI with <new rnti>``: the UE **is** on the target cell. It had no uplink grant it
    could use, so it made a contention-based random access and gave its new C-RNTI in Msg3. The target DU does not
    decode its other uplink transmissions (the uplink statistics of the UE, ``UE <rnti>: ulsch_rounds ...``, show
    the errors and DTX).

After a failure, the CU-CP logs ``Reestablishment RNTI <r> req C-RNTI <c> physCellId <pci>`` (the UE asks for an RRC
re-establishment; the C-RNTI tells in which cell it lost the link), ``handover for UE <id>/RNTI <rnti> failed,
rollback`` (the CU-CP keeps the UE on the source cell) or ``Create UE context: CU UE ID <id>`` (new attachment).

Known OAI issues
================

- **Target DU and UE with different configurations** (OAI up to 2026.w47 at least). The target DU creates the UE
  with a CellGroupConfig built without the UE capabilities (``SRS configured with 1 ports`` just before ``Added new
  CFRA process``), but the handover command gives the UE a copy updated with its capabilities (``SRS configured with
  2 ports`` just after). The target DU keeps scheduling the UE with the first one until the handover completes; when
  they differ (SRS ports, uplink rank, hence the size of the DCI), the UE cannot send its RRCReconfigurationComplete:
  the second case above. This happens with ``pusch_AntennaPorts`` > 1, periodic SRS and UEs announcing 2 SRS ports
  (Quectel). Fixed by commit f274937 (duranta-project, Robert Schmidt), which applies the configuration at once on
  the target DU, and applied in the R2Lab OAI images. The analysis reports this case when the SRS ports differ.
- **Target DU abort** (``Could not encode CellGroup``) when the UE re-establishes on it after a failed handover: seen
  with OAI 2026.w46 on the R2Lab Benetel setup; earlier versions also abort when the UE re-establishes with SRB1 only.
