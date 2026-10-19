# DevStack

**v0.5.2.** Mobile-first, self-hosted development control plane for one Ubuntu server with Docker and Docker Compose.

DevStack maintains multiple **persistent projects** while allowing **only one globally active project runtime** at a time. Selecting a project in the dashboard does not activate it. Project switching drains active containers, background commands, and webhooks before the next project starts; named volumes and repository worktrees remain intact. Multiple independently cancellable, long-running ad-hoc commands may execute concurrently inside the active project.

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

## Installation and release status

The v0.5.2 cutover **requires a clean Ubuntu host**. No automatic v0.5.1 migration or old singleton daemon is supported. The trusted `scripts/install.sh` installs the latest **published v0.5.2-or-later release**, and refuses a pre-v0.5.2 release. Do not deploy an unverified feature or integration branch as a production release.

For the breaking changes and installation notes, see [v0.5.2 release notes](docs/releases/v0.5.2.md) and [acceptance evidence](docs/v0.5.2-acceptance-evidence.md).

Architecture and acceptance requirements: [ADR-0001](docs/adr/0001-multiple-projects-single-runtime.md) and [v0.5.2 acceptance plan](docs/v0.5.2-acceptance-plan.md).

## Local verification

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m unittest discover -s tests -v
```

Docker smoke integration and fresh Ubuntu systemd permission tests run in GitHub Actions. Do not grant the web service identity root or Docker group access.

## Deferred beyond v0.5.2

Interactive terminal/SSH CLI, richer monitoring, automated routing/proxy services, advanced Git-management workflows and external GPT observability. Individual application and saved-task visual CRUD are deferred; managed JSON editing remains available.
