# ADR-0004: Mobile-first active-project UI and replaceable frontend

- **Status:** Accepted product/UX direction by project owner (2026-10-21); implementation decisions are detailed below and may be refined only with recorded rationale.
- **Milestone:** [v0.6.x issue #62](https://github.com/rafaelpivato/devstack/issues/62)
- **Baseline:** v0.5.3 (commit `4330f012535be9588c24245658d4e53008cf2fd6`)
- **Normative architecture:** [ADR-0001](0001-multiple-projects-single-runtime.md), [ADR-0002](0002-v052-release-gate-and-v053-validation.md), [ADR-0003](0003-native-cli-controller-ipc.md)
- **Living plan:** [v0.6 implementation plan](../v0.6-implementation-plan.md)

## Context

The v0.5.3 mobile browser UI combines project administration and development operations into a very long Jinja template, with a small stylesheet and monolithic polling script. Secondary workflows occupy separate standalone templates with inconsistent navigation. This UI has useful controller-backed operations but does not yet provide an intuitive, reusable mobile workspace.

The owner has **not used DevStack for sustained real-project development yet**. We should improve its actual navigation, hierarchy, readability and maintenance without guessing what an eventual editor/interactive terminal/developer console must do. After real usage, the owner will propose UX refinements, usage instrumentation and, separately, a developer-focused console.

## Decision: user experience and information architecture

**UX-01 — Active project is the landing context.** On opening the private web UI, fetch the authoritative global runtime. If one project is active, show that project's workspace home immediately; if none is active, show a concise choose-and-explicitly-activate flow. There is no autoactivation on navigation, refresh, boot or selection. If runtime state is transitioning, error, unknown or controller unavailable, show the accurate state, recovery/operation information as appropriate; **never** present unknown as inactive or secretly activate.

**UX-02 — Many projects still exist.** The full project directory, create/import flows and an explicit switch mechanism remain readily accessible but are not the default operator screen. Users can inspect inactive projects safely, without changing the global runtime. Make the difference between *viewed* and *active* project explicit when they differ. Two browser tabs selecting different projects are valid.

**UX-03 — Hierarchical, lane-like navigation.** Use a predictable depth stack: current project home → a feature list → feature detail → optional logs/review/operation result. Back takes the user to their parent. Deep links, reload and browser history must preserve the selected project and destination. Global Projects and DevStack administration are small, intentional escape routes; avoid dense, cross-linked, Android-like reach-anywhere navigation, unrelated global tabs and accidental transitions to another project. This is a navigation model, **not** a requirement to imitate iOS visual styling.

**UX-04 — One screen, one primary task.** Project home offers concise entry points for repositories, services, jobs, registered applications/private links and configuration. Feature lists show real summaries; details may zoom into logs or safe reviewed operations. Do not replicate the entire current page in multiple new screens. The appropriate content for individual screens can evolve during focused PRs without replacing the navigation or data architecture.

**UX-05 — Visual accessibility.** Mobile viewport and touch ergonomics first, desktop responsive and keyboard usable. Dark *neutral* surfaces, not near-black/bright-blue everywhere; modest accent color; semantic color may help but must **never** be the only indicator of state. Status uses text and/or icon/shape; ensure accessible contrast, focus, labels, tap targets, readable code/log output and reduced-motion accommodation. Avoid decorative metrics, gratuitous color and explanatory paragraphs where concise labels suffice. This is especially important for color-vision accessibility.

**UX-06 — Contextual copy actions.** Provide unobtrusive copy options on existing output and URL/detail surfaces as useful for sharing with AI agents. Do not invent GPT connectivity, collect code/log content for telemetry, or automatically copy secret/private information. iPhone screenshots remain an OS function.

## Scope comes from shipped capabilities, not mockups

The v0.5.3 sources in `src/devstack/api/http.py`, `src/devstack/ipc/protocol.py`, controller services and templates determine supported actions. The initial screen inventory is:

| Area | Existing capability | Expected UX |
| --- | --- | --- |
| Landing/runtime | `status`, `projects`, project detail; explicit activate/switch/deactivate/reconcile; durable `transition_submit/status/list` controller IPC | Active project home; explicit no-active activation and switch confirmation; reconnectable operation feedback |
| Projects | list, create, rename, GitHub import and durable import status | Directory and simple forms, with progress; selected project ≠ active project |
| Repositories | list/details, clean/behind status, guarded fast-forward, add, bulk update/status, branch preview/revision apply, source review/selective apply | Project-scoped list/detail, reviewed Git operations, clear conflict/result screens |
| Services | Compose service/container state, bounded container logs, Compose build/restart | Service list, detail, bounded logs with refresh; no invented shell or metric history |
| Applications/private access | registered app definitions, active-only Tailscale private port/app URLs | App list and explicit open/copy URL, without adding network/proxy services |
| Jobs/tasks | saved task list and run; ad-hoc supervised jobs; job history/detail, bounded offset logs, safe individual cancellation | Jobs list/detail/output and existing submission flows; jobs survive page disconnect |
| Configuration | revisioned managed Compose read/save, whole-collection application/task definitions, webhooks, name, GitHub SSH identity, stable release update handoff | Dedicated project/global screens with safe validated edits; no imaginary per-service admin |
| Runtime update | independent root-owned published-release updater, only when inactive | Global administration, clearly scoped away from project operations |

Important: v0.5.3 does **not** expose a browser interactive terminal/TTY, general filesystem/code editor, Git push/PR review, rich test report API, arbitrary host command execution, memory/CPU charts or generic secrets/environment editor. Mockups must not imply these exist. Existing job output may include test results, but do not turn that into a fictional dedicated test analytics subsystem. Registered applications and direct private URLs are already real capabilities; they are not a new gateway.

**Configuration concurrency:** `compose_save` checks an expected revision; `settings_replace` replaces *all* applications and tasks without a revision precondition. Structured editing must not mask this difference. In I6, either keep a clear whole-collection edit flow with explicit review/conflict warnings or design and test an appropriate controller-owned revision-safe contract; any backend contract addition needs separate justification and review.

## Frontend architecture

**FE-01 — Replaceable browser layer.** Introduce a TypeScript + React frontend using Vite as a build tool, served as private static assets by the existing Flask web process. **No new backend process or Node runtime on the Ubuntu server.** Before depending on the new bundle, I1 must determine and automate deterministic distribution/build packaging compatible with the existing git-tag installer, rollback, Python wheel and `main`-only release workflow. If that cannot be made safe, record and review the fallback before proceeding.

**FE-02 — Separation of responsibilities.** Keep distinct:
- application shell, root-state routing and project navigation;
- project/repositories/services/jobs/applications/configuration **feature modules**;
- typed HTTP client, error classification, CSRF/idempotency helpers and server-state polling/cache;
- feature-level data hooks and presentation/view models;
- small accessible UI primitives (list/detail, status, action controls, code/log viewer, loading/empty/error states).

Component composition and route metadata must allow reordering or restyling screens without rewriting transport or domain behavior. Avoid a giant application component, duplicate fetch clients, stateful services in presentational components, implicit global project IDs and unnecessary dependencies. Router/query state library choices should be evidence-based rather than mandatory from day one; no Redux by default.

**FE-03 — Clear state authority.** Controller via existing IPC/API remains the source of truth for projects, active runtime, jobs and logs. Browser state may store selected/visited project, navigation, filters and focus but cannot grant execution or infer a successful transition. Keep server state, navigation state and local UI state separate. Polling must avoid overlapping requests, pause sensibly for hidden tabs and handle reconnect/error/stale status without full-page reload loops.

**FE-04 — Stable project-scoped URLs.** Root entry (`/`) may resolve to the active project's home or a no-active activation flow. Deeper routes retain **immutable project UUIDs**, not a mutable global `current` identity. Example logical hierarchy (exact paths refined in I1–I2): `/projects/<uuid>`, `/projects/<uuid>/repositories/<repo-id>`, `/projects/<uuid>/services/<container-id>/logs`, `/projects/<uuid>/jobs/<job-id>`; global `/projects` and `/devstack/updates`. Stale links fail cleanly. React routing, Flask static fallback and legacy route transition must not break API URLs or security checks.

**FE-05 — No extra controller engine or authority.** Web continues through Flask JSON and the exact-field local Unix IPC already shared with native `dev`. Preserve Tailscale private access, session/CSRF, allowlists, UUID/generation fencing, read-only inactive browsing, fail-closed runtime transitions and the globally single-active runtime. Neither React nor Flask may gain Docker access, direct SQLite access, new arbitrary Git/host subprocess access or broad IPC/TCP exposure. CLI behavior remains unaffected.

**FE-06 — Durable action feedback.** The controller already owns `transition_submit/status/list`, but v0.5.3 HTTP routes still expose synchronous activate/switch/deactivate. As an explicitly scoped UI/API improvement, expose the existing durable transition submission/status to the web through strict allowlisted Flask endpoints (CSRF, idempotency key and conflict semantics preserved). Do not create a second transition executor; client loss must not imply success/failure/cancellation. Similar long-operation receipts for import/add/bulk already exist.

## Testing and releases

- Architecture/security regressions: controller-only privileges, typed IPC, isolation, A→B→A and no autoactivation, no transition on browse, no stale job/container ownership; CLI parity.
- Frontend: unit/component state tests, hierarchical/deep-link/back tests, active-vs-inactive and multi-tab tests, disconnection/unknown-state and failed transitions, output/copy, configuration conflict cases; automated mobile viewport and keyboard/a11y smoke.
- Build/release: wheel/git-tag asset presence, zero Node runtime on server, safe update/rollback, unchanged no-release-from-intermediate PR rule; full Python unit, Docker A→B→A and fresh Ubuntu systemd permission checks on integration and final release.
- Existing [real-host validation issue #50](https://github.com/rafaelpivato/devstack/issues/50) is deferred, not passed, and does not automatically block UI planning or progress. A **confirmed critical** security/isolation/data-loss defect or failing required gate blocks release.
- Intermediate PRs branch from current `integration/v060-ui` and are squash-merged there by the assistant only after required tests pass. Final integration → `main` PR is owner-reviewed/owner-merged; the assistant does not tag/publish or merge into `main`.
- Treat `v0.6.x` as a milestone family: release only coherent, tested subsets; do not silently narrow approved behavior merely to hit a version number.

## Explicitly deferred

- Secure read-only GPT observability and external integration (tentatively a later v0.7).
- Usage instrumentation: first use DevStack for weeks, gather qualitative feedback, then decide what limited, private metadata is useful to track. Measurement is not retroactive and should not collect source, secrets or raw logs.
- Focused developer console, interactive terminal/PTY, IDE/code editor, special test analytics, rich monitoring and general host management. Consider later only after concrete usage evidence.

## Consequences

A real frontend/build step creates maintenance and CI responsibility, justified by replaceability and navigable feature modules. The UI should become much easier to iterate without changing the control plane. Richer screens remain limited to real controller capabilities. The owner retains the right to revise actual screen composition during implementation while keeping the accepted hierarchy and safety invariants.
