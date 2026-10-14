# Devstack

Mobile-first, cloud-agnostic development project controller for a single Ubuntu server.
One repository, multiple projects, one active development workflow.

**Status:** early scaffold — **not ready for public deployment or production use**.

## Goals

- Use a phone browser rather than a desktop IDE for routine project operations.
- Organize repositories by client/project; one checked-out branch per repository.
- Start/stop project services, inspect recent logs, safely fast-forward repositories,
  and switch branches.
- Support Docker Compose and systemd user services without requiring every project to use Docker.
- Stay independent of AWS, DigitalOcean, or any other hosting provider.
- Eventually provide one-line installation, tests-before-release, independent self-updates,
  health checks and automatic rollback.

## Developer quick start (Ubuntu or other Python 3.11+ machine)

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
mkdir -p ~/.config/devstack
cp examples/projects.toml ~/.config/devstack/projects.toml
# Edit the example paths and services for your machine.
devstack
```

Open `http://127.0.0.1:8765` on the machine.

The application **only listens on loopback by default**. Do not bind it to a
public interface or expose it via an unauthenticated reverse proxy.
For remote access, use an authenticated private tunnel such as Tailscale Serve.
The app itself does **not** yet implement login or authorization.

Run tests with `python -m unittest discover -s tests -v`.

## Configuration

See [examples/projects.toml](examples/projects.toml).
Project services may use `kind = "compose"` with `cwd` and optional
`compose_file`, or `kind = "systemd"` with a **user-level** systemd `unit`.

Specify a different config with `DEVSTACK_CONFIG=/path/to/projects.toml`.

## Safety and limitations

- The dashboard executes **only configured operations**, without shell interpolation.
- POST actions require a session CSRF token. **CSRF is not authentication**.
- Git update rejects dirty worktrees and only fast-forwards the checked-out branch
  from `origin/<branch>`. Local unpushed commits are not overwritten.
- Git branch switches reject dirty working trees; new branches should be created on GitHub first.
- Logs may include secrets. Never expose the dashboard or logs publicly.
- This scaffold does not install itself, auto-update, roll back, manage TLS, or implement
  the complete deployment/recovery design. Those belong in dedicated follow-up changes.
- Do not configure production systemd units or production Compose projects as development services.

## Roadmap

1. Portable installer, dedicated Linux user and persistent systemd service.
2. Private HTTPS access and secure first-run authentication.
3. Independently supervised, release-based self-update with health check and rollback.
4. Project previews and browser-side error reporting.
5. Optional carefully scoped ChatGPT integration.
