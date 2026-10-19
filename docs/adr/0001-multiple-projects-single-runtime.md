# ADR-0001: Multiple persistent projects, one active runtime

- **Status:** Proposed — requires human approval before implementation
- **Decision scope:** DevStack v0.5.2 clean-break control-plane redesign
- **Supersedes:** The v0.5 single-configured-project design and its singleton API assumptions
- **Related:** [Acceptance contract](../v0.5.2-acceptance-plan.md)
- **Deployment premise:** The existing disposable v0.5.1 host will be retired. A fresh Ubuntu installation is acceptable; no old state or API compatibility is required.

## Problem and explicit product requirements

v0.5 documented "one configured project" although the intended requirement was always **multiple configured projects, at most one executing at a time**. The singleton decision leaked into persistence, runtime, import, HTTP routes, dashboard snapshots, and public webhook resolution. Passing isolated PR tests did not verify the intended user workflow.

These requirements are **normative**; no implementation PR may weaken them:

- **INV-01 — Many persistent projects.** Any number of distinct projects can be registered concurrently, regardless of whether one is active. Each retains its own Git repositories, working tree, Compose configuration, application registrations, task definitions, secrets, logs, and data volumes.
- **INV-02 — Single execution owner.** At every instant, no more than one project is admitted to execute any DevStack-managed Docker containers, Compose actions, standalone image tasks, commands, or forwarding webhooks. Zero active projects is valid.
- **INV-03 — Selection is not execution.** Browsing/configuring an inactive project does not activate it, run commands, or expose network ports. UI-selected project is independent of globally active project.
- **INV-04 — Reversible switching.** A -> B -> A works without re-cloning, re-importing, discarding volumes, or losing project settings. The controller must verify A has stopped *before* starting B.
- **INV-05 — Fail closed.** If stop/cancellation/ownership verification is uncertain, B cannot start. Error state requires explicit recovery; no implicit rollback activation.
- **INV-06 — Project and generation fencing.** Authorization is tied to immutable project ID *and* unique activation generation. Stale queued/running jobs and webhook requests cannot cross an active-project change or reactivate a project by themselves.
- **INV-07 — Recovery reconciles actual execution.** Restart, crash, process death, or host boot may not assume persisted state equals reality. The controller verifies/removes DevStack-owned runtime resources before accepting new work; no project automatically starts on host boot.
- **INV-08 — Network and permission isolation.** DevStack management remains private; application ports are opt-in and Tailscale-bound. Only the execution controller has Docker permission; project-bound requests never target another project by an implicit singleton/default.
- **INV-09 — Explicit destructive actions.** Project deletion, data/volume removal, and removal of Git checkouts are separate opt-in operations. Deactivation and switching must not delete named volumes or working data.
- **INV-10 — Clean break.** No migration from v0.5.1 singleton schema or endpoints, no backward-compatible shims, no promise of in-place upgrade. Do not reuse legacy state silently.

## Definitions

- **Project:** registered, durable configuration and its independent workspace; registration is not execution.
- **Selected project:** browser/client navigation state only, possibly different for two browser sessions.
- **Active project:** the one project recorded and admitted by the global runtime supervisor. Its identity is not derived from selected project or HTTP routing.
- **Activation generation:** unique, unguessable value created on each successful/attempted activation, invalidated on transition out. It fences old work even when activating the same project again.
- **Owned resource:** a Docker/worker artifact explicitly associated with the immutable project ID and activation generation by verifiable labels/metadata, never inferred from a guessed container name.

## Architecture decision

Use a **modular monolith plus a dedicated local control daemon**, not distributed services.

```text
Mobile browser (selected project)
   |
Tailscale Serve HTTPS -> loopback Web/API process [no Docker group]
   |
   +-- narrow authenticated-by-OS Unix socket API (typed, allowlisted operations)
          |
      Control daemon / Supervisor [sole Docker execution owner]
          +-- Registry / project operations
          +-- Runtime coordinator (global exclusive transition state)
          +-- Job scheduler / worker supervisor (generation-fenced)
          +-- Docker / Git / Tailscale adapters
          +-- SQLite metadata & runtime journal (controller-owned)
          +-- Project workspace directories / secrets / logs
   |
Public webhook ingress (opt-in, separate listener)
   +-- active-project authorization/lease through controller
   +-- strictly validated project-specific routing
```

No web route, task or webhook listener directly invokes Docker or spawns unsupervised Docker workers. The control daemon is a long-lived, systemd-supervised service; it must not depend on the Flask process lifecycle. The local socket is permission-restricted; the daemon validates every operation and project ID. Local socket access is a privileged management capability, not a generic host-command or Docker proxy.

**Privilege boundary:** Install web and controller under separate Linux accounts. Only the controller account has Docker access; web cannot read controller-owned registry/secrets or the Docker socket. The private dashboard is still an owner-operated administrative interface; Tailscale grants and CSRF do not create multi-user RBAC.

### Project registry and persistence

- Give every project an immutable UUID; use UUID for all controller, API, job, Docker Compose, webhook, and persisted foreign-key identities. A display name/slug may change without changing ownership.
- Use Python standard-library **SQLite** for project registry, repository metadata, application/task records, runtime transitions, jobs and operation history. Use a single serialized controller write path with explicit transactions and a versioned schema; no external DB dependency.
- Store Git working trees, Compose YAML, imported declarative templates, secrets and bulky logs in project-ID-scoped directories, e.g. `/var/lib/devstack/projects/<uuid>/`. Never store arbitrary secret contents or command logs in dashboard snapshots.
- `.devstack/` files inside a GitHub repository are import/template inputs. The managed configuration is distinct from the source worktree. Preserve path-base resolution for imported Compose build contexts.
- A project registry operation must not implicitly activate or deactivate execution. Destructive unregister requires an explicit separate workflow, refusing while active and verifying ownership.

### Runtime supervisor and job lifecycle

- Maintain **one** global runtime state machine, not a runtime process or state machine per project. States include `inactive`, `activating`, `active`, `deactivating`, `switching`, and `error`; state includes project identity and generation as appropriate.
- The supervisor is the sole authority for admission. Before process/container launch, atomically check controller authorization (project UUID, activation generation, state), not a previously queued authorization.
- Unified persisted job lifecycle for Compose builds/restarts, saved commands, direct commands and disposable images: `queued/running/succeeded/failed/cancelled`, with logs, timeouts, cancellation and generation. Workers must be children/managed units of the supervisor and terminated with verified cleanup.
- Deactivation stops admission, drains/terminates active jobs and in-flight webhook leases, brings down exactly owned containers (without removing named volumes), verifies cleanup, then transitions inactive. A lost Docker CLI does not alone prove an `exec` command stopped: its owning container must be stopped as part of verification.
- Switching A -> B: prevalidate B; acquire global transition authority; stop/reconcile A; verify A has zero owned running resources and no admitted work; then activate B under a fresh generation. If any step fails, stop further execution and report `error`; never silently reactivate A.
- At daemon startup/restart, reconcile owned resources across **all registered projects** and any previously recorded owner/generation. Unknown or unverifiable DevStack-labeled resources block activation. Manual/unrelated containers are outside the ownership guarantee.
- All long operations are observable, cancellable where safe and recoverable after browser disconnect; the browser's lifetime never owns a worker.

### API, UI and networking

- All project requests explicitly scope immutable ID. Examples (proposed API v2, free to refine during design review):
  - `GET/POST /api/v2/projects`
  - `GET /api/v2/projects/<id>`
  - `GET /api/v2/projects/<id>/dashboard`
  - `GET /api/v2/runtime`
  - `POST /api/v2/projects/<id>/activate` (only if globally inactive)
  - `POST /api/v2/runtime/deactivate`
  - `POST /api/v2/runtime/switch` with explicit target ID
  - `GET /api/v2/jobs/<id>`
- No global implicit-current-project HTTP adapter. A project selector changes only the viewed project; a distinct explicit action activates or switches execution.
- Dashboard reports selected project and global active runtime independently. All container, port, task and webhook status is scoped to the correct project. Inactive-project inspection/editing can work without launching Docker workloads.
- Public webhook routes must identify their project explicitly, e.g. `POST /hooks/<project-id>/<route>`. Only active project/generation can forward; unknown routes are 404, configured but inactive routes are 503. Deactivation waits for admitted forwarding to finish or abort safely; provider signatures are still verified by the destination.
- Continue private loopback dashboard + authenticated Tailscale network and opt-in Tailscale-bound development ports. Never infer permission to expose all project ports or wildcard Docker bindings.

### Installation, upgrades and deployment

- **Fresh Ubuntu only** for the first redesign release. v0.5.1 installations must be retired/reset beforehand; reject unsupported singleton storage instead of silently reinterpreting it.
- Install/manage *both* web and controller systemd units, Unix-socket permissions and data directories in a coherent deployment. Keep provider-independent Ubuntu bootstrap and the independent updater where useful, but rewrite service lifecycle and health checks for the new topology.
- A green web `/healthz` alone is insufficient: installation and release checks must verify controller readiness, registry access, runtime inactive/reconciled state and API-to-controller connectivity.
- Never auto-start project containers after installation, application update, reboot or web reload.
- No cloud provider dependency or new distributed infrastructure (PostgreSQL, Redis, Celery, Kubernetes) is required for DevStack itself. Project Docker stacks may of course use these technologies.
- The user explicitly permits a breaking `0.5.1 -> 0.5.2` change; semver compatibility is **not** a design constraint for this release.

## Proposed package ownership

```text
devstack/
  domain/         # immutable identity, models, invariants, transitions
  storage/        # SQLite repository, transactions, filesystem safety
  projects/       # project lifecycle, GitHub template import, Git configuration
  control/        # supervisor daemon, protocol, global coordinator, job scheduler
  adapters/       # Docker Compose, Git, Tailscale, host interaction
  api/            # project-scoped HTTP routes and daemon client
  web/            # replacement-ready presentation and view models
  ingress/        # project-aware webhook handling and controller admission
```

Packages are ownership boundaries, not a mandate for one class per file. Avoid cyclic imports and route handlers depending on Docker internals. Domain and application services must be testable without Flask or Docker.

## Out of scope

- More than one concurrently active project per server.
- Cross-server project scheduling or Kubernetes-style orchestration.
- Multi-user authentication/role-based access (the owner's tailnet is the administrative boundary).
- Migrations or compatibility for v0.5.1 projects, routes, release files or runtime state.
- The full visual redesign planned for v0.6.
- Managing arbitrary containers or host processes not created/owned by DevStack.

## Consequences and known tradeoffs

- More local complexity (daemon, IPC, schema, privilege separation) but a single authoritative runtime/worker lifecycle and better fault isolation from browser refreshes.
- SQLite state and filesystem must be backed up consistently if persistence becomes valuable; SQLite itself does not protect against filesystem loss.
- Docker access remains root-equivalent in the controller; controller commands, Compose files, images and templates must be treated as trusted executable input.
- A single-project smoke test alone is never sufficient to verify this system.

## Architecture enforcement

The [acceptance contract](../v0.5.2-acceptance-plan.md) defines release-blocking end-to-end scenarios. Every implementation PR must identify which `INV-xx` and `AC-xx` it implements/tests, and add a regression test before an invariant can be marked complete. A PR may not redefine a normative invariant without an explicit follow-up ADR reviewed **before** implementation.

A docs-only ADR does **not** enforce runtime behavior by itself. The CI release gate must be extended to run the multi-project integration suite, and the redesigned release must not be merged/published before that gate is green.

## Review questions to resolve before acceptance

1. Are UUID-based project identity and SQLite (with source-managed filesystem files) acceptable as the new clean-install data model?
2. Is a dedicated systemd controller, separate Linux identities and Unix-socket protocol the desired execution boundary?
3. Should switching be strictly fail-closed without automatically reactivating the previously active project?
4. Is a fresh-server-only v0.5.2 release acceptable, explicitly excluding any in-place v0.5.1 upgrade?

These are architecture choices for approval, **not** currently implemented functionality.
