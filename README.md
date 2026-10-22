# DevStack

**Latest published: v0.6.1. v0.6.2 stabilization candidate is in [PR #85](https://github.com/rafaelpivato/devstack/pull/85) (not released).** Mobile-first, self-hosted development control plane for one Ubuntu server with Docker and Docker Compose.

DevStack maintains multiple **persistent projects** while allowing **only one globally active project runtime** at a time. Selecting a project in the dashboard does not activate it. Project switching drains active containers, background commands, and webhooks before the next project starts; named volumes and repository worktrees remain intact. Multiple independently cancellable, long-running ad-hoc commands may execute concurrently inside the active project.

The redesigned v0.6 project-first mobile interface remains available as an **opt-in preview** at `/__v06` on an existing private DevStack host. Until real-device acceptance and the release cutover decision, the existing dashboard remains at `/`; no browser visit activates a project.

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

The original v0.5.2 cutover **requires a clean Ubuntu host** and does not support v0.5.1 migration. The trusted `scripts/install.sh` installs only the **latest published v0.5.2-or-later release**. v0.6.0 is a published release. v0.6.1 is a proposed patch until owner review, merge into `main`, verification and publication by release CI. Existing v0.5.2 installations may upgrade through their established updater to the latest published stable version; the v0.5.3 transition still requires explicit CLI enrollment and standalone-updater refresh (see [release notes](docs/releases/v0.5.3.md)).

For the original clean-host cutover, see [v0.5.2 release notes](docs/releases/v0.5.2.md). For the CLI patch, see [v0.5.3 release notes](docs/releases/v0.5.3.md). For the released UI, see [v0.6.0 notes](docs/releases/v0.6.0.md); for this patch, see [v0.6.1 notes](docs/releases/v0.6.1.md) and the [acceptance checklist](docs/v0.6-ux-parity-audit.md). Several physical-host checks (especially in-place update/rollback and reboot) remain deferred to live deployment; passing CI is not equivalent to an actual-host acceptance pass.

Architecture and acceptance requirements: [ADR-0001](docs/adr/0001-multiple-projects-single-runtime.md) and [v0.5.2 acceptance plan](docs/v0.5.2-acceptance-plan.md).

## Local verification

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m unittest discover -s tests -v
```

Docker smoke integration and fresh Ubuntu systemd permission tests run in GitHub Actions. Do not grant the web service identity root or Docker group access.

## Deferred beyond v0.5.3

Interactive container/browser terminal and TTY, unrestricted Git operations or host-shell access, automated routing/proxy services and external GPT observability are **not** part of v0.5.3. Individual application and saved-task visual CRUD remain deferred. Any live-host regressions discovered during first-project use will be addressed in later targeted patches.
