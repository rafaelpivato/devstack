# ADR-0003: Native SSH CLI through the existing controller IPC

**Status:** Proposed — awaiting owner review/acceptance in the first v0.5.3 documentation PR.
**Date:** 2026-10-20
**Milestone:** [#51](https://github.com/rafaelpivato/devstack/issues/51)
**Normative dependencies:** [ADR-0001](0001-multiple-projects-single-runtime.md) and [ADR-0002](0002-v052-release-gate-and-v053-validation.md)

## Context

DevStack v0.5.2 is released and was installed on a real Ubuntu host. The owner reports successfully importing two Smoke Test projects and switching A → B → A. Remaining physical-host validations in [#50](https://github.com/rafaelpivato/devstack/issues/50) are *deferred, not passed*; they do not block starting the CLI. Confirmed field defects still require fixes and regression coverage.

As shipped in v0.5.2:
- `devstack-control` is the sole Docker-privileged systemd service and owns SQLite state, project workspaces, all runtime transitions and jobs.
- `devstack-web-v2` is unprivileged and sends strict allowlisted JSON requests over `/run/devstack-control/control.sock`.
- `devstack.api.client.send` implements bounded client transport; `devstack.control.daemon.request` is a second lower-level helper. Operation allowlists/field lists reside in `Controller.handle`, with domain validation in the services.
- The installer creates `devstackctl`, `devstack` and the narrow `devstackipc` socket group. The socket is mode 0660 within a restricted runtime directory.
- Some runtime operations are synchronous and may outlast the web transport's default 30-second timeout. A timed-out caller may not know whether a transition later succeeded.

We need a native, short command for using DevStack directly from an SSH session, including narrow mobile terminals, without depending on the browser or duplicating the controller.

## Decision

### 1. One authoritative control plane; two clients

The primary operator CLI executable will be **`dev`**. It runs as the SSH user's unprivileged process on the Ubuntu host and communicates through the **same local Unix-domain controller IPC** as the web interface.

`dev` is a client, **not** a second controller, Docker client, host command runner, systemd service, direct SQLite reader, or independent privileged execution path. All mutations go through the existing controller, its shared services, the global coordinator, generation fencing, and the same authorization/admission boundaries as the web client.

No public/private TCP IPC listener, remotely exposed controller API, arbitrary host shell endpoint, or second Docker orchestration service will be added.

### 2. Shared maintained IPC interface

Promote the internal JSON-over-Unix-socket protocol into a maintained, transport-neutral client/contract package (proposed `devstack/ipc/`). The CLI and web adapter should share:
- Bounded request/response transport, timeout handling and structured rejection/unavailable errors.
- One declarative operation-and-parameter contract, including exact allowed fields, value validation at appropriate boundaries, and defined request/result shapes.
- A documented protocol/capability version check and explicit compatibility/failure behavior between a client and controller from different installed releases.
- A stable, safe-to-display error classification; no exposure of private paths, credentials, arbitrary stderr or environment variables.

Reconcile/remove the duplicate socket client helpers without bypassing existing controller service validation. The web must continue to use the same control path. No backwards-compatible v0.5.1 protocol shim is required.

Protocol design should minimize unnecessary complexity: a small typed schema/registry and standard-library networking are preferable to a framework or code generation unless implementation evidence warrants otherwise.

### 3. SSH authentication and local authorization

SSH provides host-user authentication. A dedicated Unix group (`devstackipc`) authorizes which **trusted administrator users** can connect to the controller socket. Membership is provisioned deliberately; an ordinary SSH user must be denied. A CLI call must not require `sudo` or `docker` group membership.

The daemon's Unix socket and parent directory ownership/modes remain restricted. The CLI does not read controller-owned SQLite files, Docker sockets, private project workspaces or web session secrets.

**Security boundary:** Controller IPC access is a powerful DevStack administrative capability, including approved container execution that may effectively grant host-level powers through Docker. It is **not** read-only access, per-project RBAC, or isolation between untrusted Linux users. Only explicitly trusted host operators may join `devstackipc`. Use Linux peer credentials (`SO_PEERCRED`) for attribution/auditing if useful; they do not replace Unix permission checks and do not imply an RBAC system.

Installer/updater integration must avoid overwriting an unrelated existing `dev` executable or shell command. Ensure the installed executable resolves to the current, atomically switched release, and fail clearly when installation would collide. Preserve rollback behavior.

### 4. Explicit project and runtime semantics

Controller project identity remains an immutable UUID. A user may *view or select* a project in a CLI-local context without activating it; CLI context must never change the globally active runtime.

Mutating requests always resolve to a specific, validated project UUID where applicable. The active project's identity may be displayed for convenience but must not silently substitute for an explicit target. Activating, switching, deactivating, reconciling and destructive operations require explicit intent and preserve ADR-0001 INV-01–INV-12, especially global exclusivity, fail-closed cleanup, and generation fencing.

Concurrency between web and CLI must be governed by the one coordinator; no cross-client special cases.

### 5. Long-running operations and detached jobs

Do not treat a client socket timeout as proof an operation was cancelled or failed. Runtime transitions must have a reliable outcome model: preferably a controller-owned, durable operation identifier/status with safe idempotency/retry semantics, reusing the existing coordinator, state journal and locking rather than adding another runtime engine. Final design can be refined during a focused implementation PR; it must not weaken recovery/fail-closed behavior.

Jobs remain owned by the controller and continue independently of a browser or SSH connection. `dev job logs --follow` should poll existing bounded, offset-based logs. Disconnection or Ctrl-C stops *following*, not the underlying job; `dev job cancel` is a separate explicit request. No interactive TTY, stdin streaming or browser shell is required for the initial milestone.

### 6. CLI usability and programmatic behavior

Use stdlib `argparse` and minimal dependencies. Commands and flags should be predictable and scriptable, with:
- Concise human-readable output that works on narrow mobile SSH terminals (evaluate 40/60/80 columns), no assumption of a TTY or color.
- `--json` for structured, machine-readable output without progress chatter on stdout.
- Defined nonzero exit codes for usage errors, authorization/protocol failures, conflicts, unavailable controller and failed operations; actionable but non-secret messages on stderr.
- Explicit, discoverable `--help`, project IDs and safe confirmation for consequential actions.
- A stable `dev` executable, not `devstack` as the primary command. Check runtime availability and known executable collisions during packaging/deployment.

A full-screen TUI is not required.

### 7. Scope, release and operational evidence

Implementation is sequenced by the [living implementation plan](../v0.5.3-implementation-plan.md), with core runtime/jobs commands first, repository/Git operations in a later CLI increment if necessary. Rich project-config editing, interactive shells, external GPT/API access, multi-user RBAC and general host administration are excluded. No CLI self-update shortcut bypasses the existing root-owned updater/systemd marker design; a shared secure update handoff needs separate design before adding `dev update`.

`v0.5.3` is the **provisional next release**, not a deadline for the entire CLI milestone. Release scope, packaging version, tests and notes must match the actual delivered increment. Later versions are acceptable. Do not modify an already published tag.

A docs PR must be accepted before runtime CLI implementation. Subsequent small PRs target the integration branch; a draft integration → main release PR is opened after the first documentation changes land on integration. CI continues to require full Python tests, real Docker two-project A → B → A isolation, and Ubuntu systemd/Unix privilege tests. Owner review and explicit merge approval are mandatory before publishing through the existing main-branch workflow.

## Required acceptance evidence

1. An authorized SSH user can invoke `dev` over the controller socket without `sudo`; an unauthorized SSH user cannot, even though they may log into the host.
2. Web and CLI use shared IPC and enforce equivalent controller validation and project/generation ownership. Neither frontend gets direct Docker/SQLite privileges.
3. Concurrent web/CLI project transitions remain exclusive and fail closed; stale requests cannot reactivate an old generation.
4. A disconnection during activation/switch/reconciliation yields a reconcilable, observable result rather than a misleading assumed failure/success.
5. Multiple project-scoped jobs and log followers are independent; stopping the CLI does not cancel a job, while explicit individual cancellation does not stop other jobs/services.
6. Narrow mobile output, `--json`, exit codes and error redaction have automated coverage and actual Ubuntu SSH checks.
7. New installation/updates safely expose the command and rollback without conflicting with an existing executable.
8. Outstanding manual physical-host scenarios in #50 remain labelled *not yet verified* until tested during real usage; a confirmed defect blocks an unsafe release and gets a regression test.

## Consequences

The CLI is a genuine second interface to one control plane. Centralizing protocol contracts and improving outcome reporting increases internal rigor without adding infrastructure or weakening privilege separation. Explicit trusted-group access is simple but grants substantial administrative capability, so it must be managed conservatively.

**Open for owner review:** Exact supported command spelling and grouping, the minimum protocol version negotiation mechanism, and the smallest safe design for durable runtime transition receipts. The architectural boundaries above are not optional implementation shortcuts.
