# ADR-0005 — Read-only DevStack diagnostics without privileged journal disclosure

- Status: accepted design for v0.6.2 candidate; validate on real host before release
- Context: [#81](https://github.com/rafaelpivato/devstack/issues/81), [#84](https://github.com/rafaelpivato/devstack/issues/84)
- Invariants: [ADR-0001](0001-multiple-projects-single-runtime.md), [ADR-0003](0003-native-cli-controller-ipc.md)

## Decision

The private dashboard must troubleshoot DevStack itself, not publish arbitrary host or container logs. The existing Flask web identity has no Docker group, no controller-private SQLite access and no journal group, and **shall not gain any of those capabilities**.

Three bounded sources and one constrained updater property inspection are composed for the 1/5/30-minute snapshots:

1. **Controller events.** Controller writes its own fixed-schema, allowlisted event codes (start/stop, authorized operations and controller errors), severity and UTC timestamp to a controller-owned SQLite ring (mode 0600). Operation names are mapped to static codes; names, URLs, argv, request payloads, idempotency keys, session/CSRF values and arbitrary exception messages are excluded.
2. **Runtime transitions.** A read-only, exact-field, time-filtered IPC operation queries controller-owned durable transition receipts and includes only receipt ID, action, phase/status, allowlisted error code and safe times. It never emits raw request keys or arbitrary result objects.
3. **Web events.** The unprivileged web process records only fixed event codes in its own systemd **StateDirectory**, mode 0700, SQLite 0600. It does not open controller state; the web worker can read only its own event ring. Web errors are generic to prevent leaking URLs, query arguments, credentials or project content.
4. **Updater.** The web process already reads the independent systemd updater's allowlisted `show` properties. The diagnostics view adds start/exit **timestamps only**, never `journalctl`, unit stdout, environment files, dynamic subprocess commands or privileged update state. If timestamps are unavailable, it reports status-only/partial coverage.

The controller IPC `diagnostics_snapshot(minutes)` accepts exactly one integer parameter, and any use of the HTTP GET endpoint is private to the existing Tailnet dashboard. The web aggregates controller, web and updater events, enforces a further maximum 120 events and surfaces per-source availability, chronological ordering, truncation and incomplete-coverage warnings. Nothing is sent to ChatGPT or external telemetry; browser Copy is explicit.

## Safety, availability and limitations

- Regex-only log redaction is *not* a security boundary; arbitrary host logs may contain secrets. **No raw journal reader, host log tailer, service privilege expansion or new daemon is introduced**.
- Coverage is deliberately **structured DevStack diagnostic events**, not all internal debug messages or complete journald history. Unavailable sources must never be represented as "no errors".
- A disabled/unavailable controller does not block access to already recorded web events and sampled updater status. A full controller process outage is represented as partial; web service itself being down necessarily prevents the dashboard from serving data.
- The per-process SQLite rings retain at most 600 structured events, bound to existing systemd service identities. The API clamps returned event count and lookback; no arbitrary arguments, paths or Docker inspection.
- Real Ubuntu filesystem ownership, permission, rollback/upgrade, iPhone copying and physical deuteranopia usability require validation beyond unit/CI; record gaps honestly. Historical events before installing this release were **not recorded** and cannot be reconstructed.
- This private first-party support tool does **not** authorize a future public GPT/MCP integration. External observability would require its own security design.

## Alternatives rejected

- Read `journalctl` as the web user, add it to a journal group, or expose privileged raw logs via controller IPC: too broad, susceptible to leaks.
- Use a generic redaction regex on raw logs: can miss arbitrary secrets and structured credentials.
- Invent a new centralized logging service or require a project to be active: unnecessary for the bounded internal diagnostic need, and conflicts with the controller-safe model.
