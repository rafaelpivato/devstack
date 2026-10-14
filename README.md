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

## Portable Ubuntu install and independent release updates

**Deployment is not enabled until a stable GitHub Release exists.** The installer,
user-service configuration and independent updater are being developed and
reviewed in a PR. Only install after that PR is merged and a tested release is
published. Use a fresh **Ubuntu 24.04 LTS** instance.

To bootstrap on a **trusted private server**, in cloud-init user data or an
interactive root shell:

```bash
curl -fsSL https://raw.githubusercontent.com/rafaelpivato/devstack/main/scripts/install.sh | sudo bash
```

This command executes code from GitHub with elevated privileges. Inspect the
installer first, or pin its URL to a reviewed commit SHA for a stronger
supply-chain boundary. User-data support depends on the provider's cloud-init
configuration. The same command can be run on any Ubuntu VM.

The installer provisions the non-root `devstack` service account, persistent
application environment, `devstack.service`, and a **separate**, root-owned
`devstack-update.service` plus `devstack-update.timer`. The web app binds
**only to 127.0.0.1:8765**; no public HTTP/HTTPS exposure or login is configured.
Private authenticated access (for instance via a private tunnel) must be set up
separately before accessing it from an iPhone.

Releases must be tagged `vMAJOR.MINOR.PATCH` and published as stable GitHub
Releases. Release publishing has its own CI job that runs the unit tests.
A periodic timer checks the latest stable release and installs it into
`/opt/devstack/releases/<tag>`, then atomically swaps the `current` symlink,
restarts the service and verifies `/healthz`. On failure it restores the
previous version and records that the failed tag should not be retried
automatically. The updater itself is outside the versioned application path.

Useful host-level recovery commands (do not depend on Devstack working):

```bash
sudo systemctl status devstack devstack-update.timer
sudo journalctl -u devstack -u devstack-update --no-pager -n 100
sudo systemctl start devstack-update.service
```

**Deliberate limitations:** This initial release system does not manage
databases or reversible migrations, public authentication, HTTPS, cloud
firewall policies, application preview ports or user-project process permissions.
It requires operator review of releases. A failed first install has no previous
release to roll back to and leaves the web service stopped. The independent
updater is updated by re-running a reviewed installer, not automatically with
every application release.


## Optional private iPhone access (Tailscale Serve)

Devstack continues to listen **only** on `127.0.0.1:8765`. No public cloud
ports, reverse proxy, custom domain, or app login are required for a private
single-user MVP. The private network provides the access boundary; **all users
and devices permitted by your Tailscale ACL/grants may reach this administrative
interface**, so restrict permissions to yourself. Do not use Tailscale Funnel,
which publishes the service to the public Internet.

After installing Devstack on an Ubuntu VM (and publishing the first stable
release), run this **once interactively** from the VM:

```bash
sudo /usr/local/lib/devstack/enable-tailscale.sh
```

This optional script installs Tailscale from its official signed Ubuntu APT
repository if needed, guides you through Tailscale sign-in, then configures a
private HTTPS reverse proxy to the existing local application. The script
refuses to overwrite other Serve/Funnel configurations. You can also connect
Tailscale manually and run
`sudo python3 /usr/local/lib/devstack/private_access.py`.

On the iPhone:

1. Install the official Tailscale iOS app and sign in to the **same tailnet**.
2. Enable Tailscale VPN in the iPhone app.
3. Open the printed `https://<device>.<tailnet>.ts.net/` URL in Safari.

Tailscale's Serve publishes tailnet-only HTTPS with managed TLS certificates.
The main installation enables Secure session cookies for browser access via
HTTPS. App sessions do not confer authorization independently; access is
controlled by your Tailscale network and its explicit device/user policies.

**Recovery independent of the dashboard:** You can inspect
`sudo tailscale serve status`, `sudo tailscale status`,
`sudo systemctl status devstack`, and
`sudo journalctl -u devstack --no-pager -n 100` from a rescue console.
If Tailscale is not connected, private access will be unavailable without
affecting the local Devstack process. Do not configure cloud firewalls to
open port 8765.

The Tailscale installation is optional: Devstack remains cloud/provider
agnostic and can use any separately authenticated, encrypted private proxy.

## Preferred first setup: automatic install, browser approval

**No Tailscale key is required.** Use `DEVSTACK_TAILSCALE=1` during Ubuntu
cloud-init. The installer starts Tailscale and prints an initial login URL to
the host's startup output (usually the cloud-init output log; availability in
the cloud provider's console varies by provider). It uses a **bounded**
authentication wait so provisioning can finish without waiting for you.

Open the login URL from the **trusted cloud-provider console** on your phone,
sign into **your** Tailscale account, and approve the new machine. Turn on
Tailscale **device approval** in your tailnet beforehand if you want an
additional administrator approval gate. Once registered, an independent
`systemd` timer completes **private** HTTPS Serve. You can find its final
device DNS name in Tailscale's Machines page. A separate consent to enable
HTTPS certificates for Serve may be required in the Tailscale admin console.

For cloud-init scripts, after reviewing the installer:

```bash
curl -fsSL https://raw.githubusercontent.com/rafaelpivato/devstack/main/scripts/install.sh \
  | sudo env DEVSTACK_TAILSCALE=1 bash
```

The first successful registration pins the device identity and tailnet in
`/var/lib/devstack/tailscale-identity.json`. Subsequent runs refuse a
different device/tailnet and do not automatically re-enroll a disconnected
device. Recovery must be handled from a separately trusted server console.

**Limitations:** This is trust-on-first-use, not cryptographic attestation.
Anyone with administrative access to a fresh VM or its bootstrap logs before
you claim it could interfere. You must verify that the machine joins your
intended tailnet before using it. A bootstrap browser authorization link is
not itself a Devstack dashboard or a substitute for authenticated first
contact. The credential-file flow remains an **advanced pre-provisioned-key
alternative**, not the default.
