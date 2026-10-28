# DevStack

**Latest published: v0.7.2.** Mobile-first, self-hosted development control plane for one Ubuntu server with Docker and Docker Compose.

DevStack maintains multiple **persistent projects** while allowing **only one globally active project runtime** at a time. Selecting a project in the dashboard does not activate it. Project switching drains active containers, background commands, and webhooks before the next project starts; named volumes and repository worktrees remain intact. Multiple independently cancellable, long-running ad-hoc commands may execute concurrently inside the active project.

The React project-first mobile UI is canonical at `/`; prior `/__v06` bookmarks redirect to it. Browser navigation never activates a project.

## Architecture

- `devstack-control`: the only Docker-privileged controller, with allowlisted IPC over a Unix domain socket and UUID-scoped project identity.
- `devstack-web-v2`: unprivileged mobile dashboard and read-only/project-scoped JSON API. This process cannot access Docker or the private project database directly.
- Python stdlib SQLite registry with independent project repositories, Compose/app/task definitions and durable job histories.
- Per-project webhooks are constrained to the active project and generation; optional Funnel is separate from the private dashboard.
- Tailscale provides private access. Compose services may publish explicit TCP mappings on the host's Tailscale IPv4 and DevStack displays their direct URLs. **No additional reverse-proxy daemon or automatic public application port publication.**

## HTTPS ingress

DevStack uses two HTTP-facing native systemd services behind Tailscale:
private dashboard via **Serve HTTPS 8443**, and opt-in public MCP/webhooks
via **Funnel HTTPS 443** to one unprivileged `devstack-public` application.
As of **v0.7.4**, MCP requests require a single-owner OAuth connection approved
through the private :8443 dashboard. The owner can see the connection creation
date and revoke access without any activity/last-used tracking. No external
identity provider, additional daemon or developer account database is required.

The controller is internal and Docker-privileged; public webhook payloads
go directly to the approved project container, using a narrow authorization
socket. No additional reverse proxy or infrastructure container is needed.

Existing v0.7.2 installations receive an **automatic** port migration from
the trusted release postinstall hook. After upgrading, open the dashboard
with `:8443`; no migration button or SSH is required. See
[networking and upgrade details](docs/public-ingress.md).

## Main workflows

Create or import projects from GitHub, register multiple repositories per project, inspect repository status, safely update them individually or in a single background batch, and preview/confirm per-repository or project-wide Git branch switches. If a requested branch does not exist in a repository, switching falls back to that repository's **verified remote default branch**; unsafe local changes are never discarded.

Configure each repository's optional service reactions (None / Restart / Redeploy), manually restart or redeploy individual services without switching projects, and maintain a project's Compose files, applications and saved tasks; review/selectively apply managed `.devstack/` source configuration; activate/deactivate/switch the runtime. See service/container state, health, bounded logs and private Tailscale links on mobile. Queue independent background container commands with history and cancellation.

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

The original v0.5.2 cutover **requires a clean Ubuntu host** and does not support v0.5.1 migration. `scripts/install.sh` selects the latest published stable release; existing supported deployments upgrade using the independent guarded updater. **v0.6.8 is published; v0.6.3 was intentionally skipped. The v0.6.5 compatibility bridge must precede wheel-based updates for older deployments.** Installed v0.5.2 users upgrading to newer versions must follow the v0.5.3 CLI operator enrollment and independent updater refresh notes.

For the original clean-host cutover, see [v0.5.2 release notes](docs/releases/v0.5.2.md); for the CLI setup, see [v0.5.3 notes](docs/releases/v0.5.3.md). For the published UI series, see [v0.6.0](docs/releases/v0.6.0.md), [v0.6.1](docs/releases/v0.6.1.md), and [v0.6.2](docs/releases/v0.6.2.md); [v0.6.4 notes](docs/releases/v0.6.4.md) document the root cutover. For the packaging migration, first review the mandatory [v0.6.5 compatibility bridge](docs/releases/v0.6.5.md), then [v0.6.6 verified release wheels](docs/releases/v0.6.6.md). See [v0.6.7 release notes](docs/releases/v0.6.7.md) for active-project Git synchronization. Never bypass the bridge on existing hosts. Real-device accessibility, restart/rollback and deferred acceptance are recorded separately in [issue #50](https://github.com/rafaelpivato/devstack/issues/50).

Architecture and acceptance requirements: [ADR-0001](docs/adr/0001-multiple-projects-single-runtime.md) and [v0.5.2 acceptance plan](docs/v0.5.2-acceptance-plan.md).

## Local verification

```bash
cd frontend && npm ci --ignore-scripts --no-audit && npm run build && cd ..
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m pip install 'build==1.3.0'
.venv/bin/python -m build --wheel --outdir dist
.venv/bin/python scripts/verify_release_wheel.py dist
```

Git tags and GitHub-generated source archives contain **source only** from v0.6.6. A direct pip install from these archives deliberately fails without a local frontend build. End users install CI-built release wheels, not source archives. No Node/npm is required on Ubuntu deployments.

Docker smoke integration and fresh Ubuntu systemd permission tests run in GitHub Actions. Do not grant the web service identity root or Docker group access.

## Future work

Repository synchronization (#89) shipped in v0.6.7; targeted service reactions (#90) shipped in v0.6.8. MCP starts as a v0.7 connectivity milestone; real authenticated controller operations remain future work. Updating a repository's files does **not** restart or rebuild its running services. History and earlier release documentation remain available for audit.


## Release packaging since v0.6.6

Git tracks source and lockfiles, not compiled React assets. CI builds and verifies the frontend in a Python wheel and publishes the wheel plus SHA256SUMS as checked GitHub Release assets. GitHub's autogenerated source archives are **not** ready-to-install distribution packages.

**Upgrade note:** deployed v0.6.4 installations must first install the source-compatible v0.6.5 bridge and explicitly refresh the standalone root-owned updater before upgrading to v0.6.6. See [bridge instructions](docs/releases/v0.6.5.md), [wheel release notes](docs/releases/v0.6.6.md) and [ADR-0006](docs/adr/0006-ci-release-artifacts.md). Never rerun the clean-host installer onto existing state.

## Operator experience: Web-first, SSH as last resort

**Project-wide invariant:** DevStack is managed and troubleshot through its
mobile-first Web console. Normal installation upgrades, enable/disable actions,
health checks, meaningful failure reasons, time-bounded copyable logs and
diagnostic snapshots **must work without SSH**, including for optional
services such as MCP and Tailscale Funnel. Never introduce a feature whose
normal troubleshooting instructions begin with `ssh`/`journalctl`/
`systemctl` or manual host CLI steps.

When implementing a new service, add its structured, bounded diagnostics to
Administration → DevStack diagnostics (1/5/30 minutes) and relevant contextual
errors to its own screen. Include startup, connectivity, failures, transitions
and actionable error codes, without exposing privileged raw host logs or
secrets. Keep log copy/share possible on mobile.

**SSH is reserved for failure of the Web console or its diagnostic/
recovery infrastructure itself** (i.e. troubleshooting the troubleshooting).
It remains an optional power-user CLI, not a required operational workflow.

See [AGENTS.md](AGENTS.md) for the enforceable contributor checklist.
