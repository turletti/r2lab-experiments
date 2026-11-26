******************************************
Triggering the handover from the notebooks
******************************************

.. note::
   Documentation of ``r2lab_experiments.handover``, written by docs/nador_guides.py: do not edit.

Trigger an F1 handover on the testbed and check it: the R2Lab layer on top of :class:`nador.handover.HandoverWatch`.

Temporary: triggering a handover acts on the RAN, so it belongs to IFRAN (its RAN manager); this module goes away
once IFRAN does it. It uses attributes of ``HandoverWatch`` of NADOR 1.1.1 (``ran``, ``cucp``, ``charts``,
``namespace``, ``master``, ``nodes``, ``_before``, ``_kubectl``).

``HandoverWatch`` only observes a handover; ``run_handover`` also sends the command to the CU-CP, waits, gets the
report and pings from the UE:

- OAI: ``ci trigger_f1_ho <CU UE ID>`` on the telnet server of the CU-CP (``--telnetsrv --telnetsrv.shrmod ci
  --telnetsrv.listenport <telnet_port>``; not 9090, the port of the metrics-parser sidecar in the same pod). The whole
  answer is read before closing: with OAI f80a883, closing earlier reset the connection and the telnet server aborted
  the CU-CP ("buffer overflow detected").
- OCUDU: the ``trigger_handover`` JSON command on the remote control server of the CU-CP (WebSocket), sent from the
  Kubernetes master by a standard-library client.
