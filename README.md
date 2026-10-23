# DevStack

**Latest published: v0.6.2. In progress: v0.6.4 default React UI cutover and legacy UI cleanup (owner-review only).** Mobile-first, self-hosted development control plane for one Ubuntu server with Docker and Docker Compose.

DevStack maintains multiple **persistent projects** while allowing **only one globally active project runtime** at a time. Selecting a project in the dashboard does not activate it. Project switching drains active containers, background commands, and webhooks before the next project starts; named volumes and repository worktrees remain intact. Multiple independently cancellable, long-running ad-hoc commands may execute concurrently inside the active project.

The released v0.6.2 React project-first mobile UI is staged at `/__v06` while the historic server-rendered dashboard remains at `/`. **v0.6.4 (unreleased)** will make React canonical at `/`, remove legacy-only UI resources, and redirect old preview bookmarks. Browser navigation never activates a project.

## Architecture

- `devstack-control`: the only Docker-privileged controller, with allowlisted IPC over a Unix domain socket and UUID-scoped project identity.
- `devstack-web-v2`: unprivileged mobile dashboard and read-only/project-scoped JSON API. This process cannot access Docker or the private project database directly.
- Python stdlib SQLite registry with independent project repositories, Compose/app/task definitions and durable job histories.
- Per-project webhooks are constrained to the active project and generation; optional Funnel is separate from the private dashboard.
- Tailscale provides private access. Compose services may publish explicit TCP mappings on the host's Tailscale IPv4 and DevStack displays their direct URLs. **No reverse proxy or automatic port publication.**

## Main workflows

Create or import projects from GitHub, register multiple repositories per project, inspect repository status, safely update them individually or in a single background batch, and preview/confirm per-repository or project-wide Git branch switches. If a requested branch does not exist in a repository, switching falls back to that repository's **verified remote default branch**; unsafe local changes are never discarded.

Configure a project's Compose files, applications and saved tasks; review/selectively apply managed `.devstack/` source configuration; activate/deactivate/switch the runtime. See service/container state, health, bounded logs and private Tailscale links on mobile. Queue independent background container commands with history and cancellation.

Check for published stable DevStack releases or request **Update now** through a restricted systemd marker handoff. The independent root-owned updater validates an inactive healthy controller, installs verified release tags and rolls back a failed deployment. The periodic timer is unchanged.

## Native SSH CLI (`dev`)

v0.5.3 adds the unprivileged `dev` operator command over the same
controller-owned Unix IPC as the mobile dashboard. It supports global
runtime operations, projects, service state/logs, saved tasks and Compose
maintenance, detached background jobs and cancellation, plus safe GitHub
import and repository workflows. CLI users must be explicitly enrolled in
the trusted `devstackipc` group—**not** the Docker group. See
[CLI guide](docs/native-cli.md) for exact commands, recovery and access.

## Installation and release status

The original v0.5.2 cutover **requires a clean Ubuntu host** and does not support v0.5.1 migration. `scripts/install.sh` selects the latest published stable release; existing supported deployments upgrade using the independent guarded updater. **v0.6.2 is published; v0.6.4 is an unmerged candidate, and v0.6.3 is intentionally skipped.** Installed v0.5.2 users upgrading to newer versions must follow the v0.5.3 CLI operator enrollment and independent updater refresh notes.

For the original clean-host cutover, see [v0.5.2 release notes](docs/releases/v0.5.2.md); for the CLI setup, see [v0.5.3 notes](docs/releases/v0.5.3.md). For the published UI series, see [v0.6.0](docs/releases/v0.6.0.md), [v0.6.1](docs/releases/v0.6.1.md), and [v0.6.2](docs/releases/v0.6.2.md); [v0.6.4 notes](docs/releases/v0.6.4.md) describe the proposed root cutover. Real-device accessibility, restart/rollback and deferred acceptance are recorded separately in [issue #50](https://github.com/rafaelpivato/devstack/issues/50).

Architecture and acceptance requirements: [ADR-0001](docs/adr/0001-multiple-projects-single-runtime.md) and [v0.5.2 acceptance plan](docs/v0.5.2-acceptance-plan.md).

## Local verification

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m unittest discover -s tests -v
```

Docker smoke integration and fresh Ubuntu systemd permission tests run in GitHub Actions. Do not grant the web service identity root or Docker group access.

## Future work

Automatic repository synchronization and policy-driven targeted container restart/rebuild are deferred beyond v0.7; GPT/MCP and secure external observability require separate architectural decisions. Neither is part of v0.6.4. History and earlier release documentation remain available for audit.
