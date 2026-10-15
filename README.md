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

## Versioned release workflow

After merging a change into `main`, GitHub Actions installs Devstack and runs
the full unit-test suite. If the version from `pyproject.toml` (e.g. `0.1.0`)
has **not** already been tagged, the workflow publishes the corresponding
stable GitHub Release (e.g. `v0.1.0`) using that tested commit.

For later releases, **bump `[project].version` in the feature PR**.
Ordinary code merges without a version bump do not publish another release.
The independent updater installs only published stable releases and checks
application health before accepting the new version.

Do not assume a successful CI run proves first-boot cloud-init or Tailscale
browser activation works: those still need a smoke test on a fresh Ubuntu VM.

## First-time setup from a phone (no cloud-init log access)

For a fresh server, Devstack can optionally serve a **secret-gated setup
page on HTTP port 80**. You provide a strong random one-time setup code
in EC2 User Data, visit the VM's public IPv4 address from your phone,
enter the code, and follow the Tailscale device authorization link. Once
private Tailscale HTTPS is configured, the temporary HTTP service is
automatically disabled.

**HTTP does not encrypt the code in transit**; this option is convenient
but less secure than the existing console-log or HTTPS-based onboarding.
Do not expose Devstack itself on the internet. Manually remove the
inbound TCP/80 AWS security-group rule after activation.

See [one-time HTTP onboarding](docs/one-time-http-onboarding.md) for the
cloud-init script, limitations, and security precautions.

## Updating the temporary HTTP activation page

Starting with v0.1.2, the onboarding systemd service loads its code from the
**active release checkout**, and the release updater restarts an active
bootstrap service after each successful update. The ordinary update timer
checks releases every ~30–35 minutes.

**Important for already-provisioned v0.1.1 VMs:** The existing service
references a separately copied file outside the versioned application.
Automatic app updates **cannot** update that HTTP portal. You must perform
a one-time reinstall of the service units using a trusted recovery channel
(e.g. AWS Systems Manager Run Command, if already configured), or replace
the disposable instance. After that, future bootstrap fixes will be delivered
through normal verified releases. Never expose the full Devstack dashboard
to the public Internet to recover access.

The post-secret success view now provides an explicit Tailscale authorization
link instead of depending on cross-site POST redirection in mobile browsers.

## GitHub SSH and multi-repository onboarding (v0.1.3)

Open **GitHub SSH settings** from the private dashboard. Generate a dedicated
Ed25519 key pair (private file remains owned by the `devstack` service
account), then copy **only its public key** into
[GitHub SSH keys](https://github.com/settings/keys). Use the **Test GitHub
SSH** button to verify the account and check if organizational SSO approval is
needed. Never reuse your personal private key or paste any private key into
the browser. The GitHub account SSH key can carry write access to all
repositories the GitHub identity can access, so keep your Tailscale device
grants restricted.

The GitHub server host keys are obtained through authenticated
`https://api.github.com/meta`, not blindly trusted via SSH. Git operations
use `StrictHostKeyChecking=yes` and the specific Devstack key.

Use **Create project**, then **Add GitHub repository** as often as required
for the same project. You can choose a different initial branch per repo.
Devstack clones into `~/workspaces/<project>/<repo>`, writes paths into
`projects.toml` atomically, and keeps repositories separate from service
definitions. **Service configuration is still managed by projects.toml;**
the dashboard's service editor is a separate future feature.

Both SSH and HTTPS GitHub repository URLs are accepted, but cloning always
uses GitHub SSH authentication and never embeds secrets in remote URLs.

## Manual Update Now (v0.1.4)

The dashboard has a **Devstack updates** panel showing the installed package
version, GitHub's latest stable release, and systemd updater state. Press
**Check & update now** to immediately request the existing independently
supervised updater, which performs the usual health check and rollback.
The normal ~30-35-minute timer continues as a fallback.

The Flask service runs as the unprivileged `devstack` user and cannot run
`sudo` or `systemctl start`. Instead, the service creates only an empty,
private marker in its systemd-owned `RuntimeDirectory`. PID 1 watches that
marker with a narrowly defined `devstack-update-request.path` unit. A
root-owned oneshot removes the marker, then invokes only
`systemctl --no-block start devstack-update.service`. There is **no
user-controlled command, argument, path or release tag**. The existing updater
serializes update attempts via `/var/lib/devstack/update.lock`.

### One-time activation on an existing installation

**Important:** The independent installer and systemd units live outside
versioned releases and are not automatically upgraded by the release updater.
After v0.1.4 is published and installed, run the following once in a
**trusted root shell** (EC2 Instance Connect is suitable), after reviewing
the current installer:

```bash
curl -fsSL https://raw.githubusercontent.com/rafaelpivato/devstack/main/scripts/install.sh | sudo bash
```

That refreshes the service units, restarts the already running dashboard to
activate its private runtime directory, and enables the marker watcher.
**Do not supply DEVSTACK_BOOTSTRAP_SECRET or DEVSTACK_TAILSCALE=1** when
refreshing an already enrolled instance. The existing Tailscale identity and
project settings remain intact. Before that activation, the dashboard will
explain why the manual update button is disabled.

The live status is advisory; a root-level update failure will show a failure
indicator, while detailed logs remain in
`sudo journalctl -u devstack-update.service --no-pager -n 100`.

## Docker-first projects (v0.2.0)

Devstack's stable dashboard continues to run as a host systemd service;
Docker is only used for project development. Rootful Docker Engine and
Compose are installed on Ubuntu by the reviewed installer. The unprivileged
`devstack` account belongs to the `docker` group, which is **root-equivalent**:
this feature is for the owner's private trusted Tailscale network, not
untrusted multi-user use. Never publish Docker's daemon socket or Devstack's
administrator dashboard.

### Existing EC2 machines: one-time installation

**Important:** A Devstack release update only updates the application.
It does not install Docker or refresh systemd/service group permissions.
After merging this PR and receiving the verified `v0.2.0` release, use the
reviewed installer *once* through a trusted Ubuntu terminal:

```bash
curl -fsSL https://raw.githubusercontent.com/rafaelpivato/devstack/main/scripts/install.sh | sudo bash
```

The installer installs Ubuntu's `docker.io` and `docker-compose-v2`
packages, enables Docker, adds the `devstack` account to the Docker group,
and restarts the dashboard so its group membership becomes active. Existing
GitHub SSH identities, Tailscale registration and project records are retained.
Set `DEVSTACK_INSTALL_DOCKER=0` if opting out of Docker installation.

### Managing a project

Create a project from the dashboard (or use **Install smoke example** for a
disposable test). Then choose **Manage Docker Compose** on its card.

The editor writes to:

```text
~/workspaces/<project>/
  .devstack/
    compose.yaml
    jobs/                 # Local action logs/status
  .secrets/               # Private local env files
  <repository-one>/
  <repository-two>/
```

The Compose file uses standard Docker Compose YAML; relative build contexts
can point to cloned repositories such as `../beacon-django` or
`../beacon-web`. New files never touch Git worktrees. The dashboard's
**Validate and save** button invokes `docker compose config --quiet`, then
atomically replaces the managed file. Invalid changes leave the old definition
intact. Multiple containers, ports, networks and named volumes are native
Docker Compose features.

Start/rebuild, build, restart, stop and down are explicit, asynchronous
actions. The dashboard polls job state and recent logs. **Down does not remove
named volumes**. Container status and logs are available from the same page.
Background workers have a 15-minute command timeout; they are not guaranteed
to survive reboot or service cgroup termination. Stale jobs eventually show
as stale. Operations may contain secrets in their log output; treat the
private dashboard as privileged.

Project Compose files may publish loopback ports explicitly, for example
`127.0.0.1:18000:8000`. A new project's default Compose example does not
publish any host port. **Avoid wildcard/public port mappings**, since Docker
can expose published ports independently of host firewall expectations.
Private Tailscale previews and public Funnel webhooks are future milestones.

### Smoke fixture

The bundled `examples/smoke/` includes a Python HTTP server, Redis and
PostgreSQL (with a named database volume). **Install smoke example** copies
it to `~/workspaces/smoke`; use its Docker page to **Start / rebuild**,
inspect containers and logs, then **Down**. HTTP is bound only to
`127.0.0.1:18080` and is not yet previewed via Tailscale Serve.
GitHub Actions' Docker smoke workflow builds and starts the same stack and
checks its `/health` endpoint.

### Deliberately deferred

Container command runner / saved tasks, direct Tailscale ports, selective Funnel webhooks,
export/import of manifests and secrets come in later milestones.

## Explicitly selected dashboard applications (v0.2.1)

A project dashboard is an **application shortlist**, not an inventory of
all Docker containers. Register only the applications you want to see,
such as **API** and **Frontend**, using each project's **Add application**
form. Choose a label and the matching Docker Compose **service** name;
optionally provide its internal container port for future preview routing.
A port is *metadata only*: registration does not change `compose.yaml`,
publish the port, create a proxy, or start containers.

The applications are stored in:
`~/workspaces/<project>/.devstack/project.toml`
outside cloned repositories, using standard TOML, for example:

```toml
[[applications]]
name = "API"
service = "backend"
port = 8000

[[applications]]
name = "Frontend"
service = "web"
port = 5173
```

Registered applications show their read-only Docker Compose container state
(running, stopped, unhealthy or unavailable). All other services (database,
Redis, workers and auxiliary tunnels) remain accessible on the **Manage
Docker Compose** page, even when absent from the main dashboard.
Applications can be edited or removed from the dashboard without stopping,
rebuilding or deleting Docker containers.

For the bundled smoke fixture, register **Web** with Compose service
`web` and port `8000`. The PostgreSQL and Redis services will not appear
on the dashboard unless you add them deliberately. The HTTP port remains
bound only to the host loopback; this release adds **no private preview**.
Private URLs, proxy routing and public webhooks remain future milestones.

Older explicitly configured `projects.toml` services have a distinct
**Legacy services** section so they are not confused with applications or
the managed Docker stack. Existing project and repository data are preserved.
This is an ordinary application release; no host installer refresh is needed
for v0.2.1.

### Keeping the smoke fixture's application list current

New imports of `examples/smoke` contain the bundled
`.devstack/project.toml`, which registers **Web** (`web:8000`) immediately.
Existing `smoke` workspaces copied in v0.2.0 do not receive files when the
Devstack application updates. After upgrading to v0.2.1, use **Refresh smoke
applications** on the smoke project card. This adds missing bundled application
entries once, preserving any previously added or customized applications.
It does not replace Compose YAML or source files, rebuild containers, publish
ports, or touch PostgreSQL volumes. It is safe to repeat: already registered
service names and labels are not duplicated.

For this milestone, refreshing a project's complete template or upgrading
its Compose definition is intentionally **not** automatic. Such migrations
need explicit review to avoid overwriting local developer changes.

## Import projects directly from GitHub (v0.2.2)

Use **Import GitHub project** on the private dashboard and paste either
`https://github.com/rafaelpivato/devstack` or
`https://github.com/rafaelpivato/devstack/tree/main/examples/smoke`.
Optionally supply a project name, e.g. `smoke-git` when the existing
`smoke` fixture is already installed.

Devstack clones the repository using its established GitHub SSH identity,
then follows a small convention:

- A URL targeting a directory selects that directory's
  `.devstack/project.toml` and optional `.devstack/compose.yaml`.
- A repository-root URL uses a root manifest if one exists; otherwise it
  discovers **exactly one** nested manifest. If several are found, use an
  explicit GitHub `/tree/<branch>/<folder>` URL to disambiguate.
- Git branch names containing slashes are supported using Git's remote refs.

The application shortlist and Compose YAML are **copied** into the new
`~/workspaces/<project>/.devstack/` directory. The cloned Git repository is
not modified. An `import.toml` stores the source subdirectory, so Docker Compose
continues to resolve relative build contexts and mounts from the source's
`.devstack` directory, even though the editable Compose YAML lives outside
the repository. Changes to the Git template are not automatically reimported
on repository updates; explicit refresh/migration is a future improvement.

A valid, working Docker Compose installation is required when the template
defines Compose services. Import validates the configuration but **does not
start or publish anything**. Review Compose carefully before running
rootful Docker; an imported Dockerfile is executable code. Ports published
by imported services remain subject to their explicit Compose definitions.

Existing projects, including the v0.2.0 smoke fixture, are not overwritten
by importing another copy; provide a distinct project name when necessary.
The original dedicated **Install smoke example** button is retained for
backward compatibility. No Ubuntu installer refresh is required for v0.2.2.

## Container command runner (v0.3.0)

Open **Commands and tasks** on a project card or **Run container commands**
from the Docker page. Select a configured Docker Compose service and use
either **Existing container (exec)** or **Temporary container (run --rm)**.
Enter a single command using shell-like argument quoting, such as
`python -m unittest discover -v`. There is **no implicit shell**:
pipes, redirects, and expansion are literal arguments unless the developer
explicitly invokes a shell, for example `sh -c 'echo hello | cat'`.

Commands execute as background jobs and expose their live output, exit code,
status, limited history, a ten-minute timeout and a cancellation request.
Only one Devstack command per project runs at once to avoid accidental
overlapping builds/migrations. Tasks and history live in private files at:

```text
~/workspaces/<project>/.devstack/
  tasks.toml
  commands/<id>.json
  commands/<id>.log
```

Saved tasks use standard TOML with argv arrays:

```toml
[[tasks]]
name = "tests"
service = "web"
mode = "exec"
command = ["python", "-m", "unittest", "discover", "-v"]
```

**Save** never executes the task. Use the separate **Run** button.
Templates imported from GitHub also import an optional
`.devstack/tasks.toml` into the project's managed folder, retaining the
usual convention that cloned repositories are not modified.
The bundled `examples/smoke` includes `tests`, `tests-fresh`, and
`python-version` tasks, plus HTTP smoke tests inside the web image.
A *new* import from the reviewed source includes these automatically;
an existing clone needs to be updated and rebuilt, while existing projects'
managed task lists are intentionally not overwritten automatically.

**Cancellation:** One-off `run --rm` commands get an explicit temporary
container name, which Devstack forcibly removes on cancellation/timeout.
For `exec`, cancellation disconnects the Docker client but may **not**
stop the command process inside the existing container. Never use
`exec` with potentially destructive long-running migrations if you need
guaranteed process termination.

**Security:** This is an owner-only privileged development tool.
Docker commands run with the configured service's privileges and mounts,
which may themselves provide root-equivalent host access. Commands,
arguments, recent output and logs may reveal credentials: avoid passing
secrets as CLI arguments, and restrict the Tailscale dashboard accordingly.
Devstack does not offer arbitrary host-shell execution, public terminal
access, or webhook-triggered commands in this release. No host installer
refresh is required for v0.3.0. Private previews and proxy/webhooks remain
v0.4.0 goals.

### Standalone Docker images (v0.3.0)

The command runner also accepts an image reference **without defining a
Compose service**. Select **Standalone Docker image**, enter an image like
`python:3.12-alpine` or `registry.example.com/tool:latest`, and run a
command; these tasks always create a fresh `docker run --rm` container.
Saved tasks can use the same feature:

```toml
[[tasks]]
name = "python-version"
image = "python:3.12-alpine"
command = ["python", "--version"]

[[tasks]]
name = "redis-ping"
image = "redis:7-alpine"
network = "project"
command = ["redis-cli", "-h", "redis", "ping"]
```

The default is `network = "none"`, with **no volumes, bind mounts,
environment variables, secrets or published ports** supplied by Devstack.
Standalone containers have `--security-opt no-new-privileges` and
`--cap-drop ALL`. Image-defined users, application behavior and Docker
daemon privileges still require trusted images and code.

Use `network = "project"` only when the project's **default Compose
network is already running**. Devstack identifies that specific network
using Docker Compose labels, not a guessed name. Attached utility
containers can reach services by Compose DNS names, for example
`redis:6379`, while still receiving no project files or secrets.

Containers are removed after completion; Devstack retains the stdout,
stderr, status and exit code in command history. Cancel/timeout attempts
to remove temporary standalone containers, just as for temporary Compose
runs. No Compose service is added, and the image is never shown on the
application dashboard. A future milestone may add opt-in read-only project
mounts and explicit secret injection, but neither is enabled here.

## Add and synchronize projects from GitHub

**Add project from GitHub** accepts a GitHub repository or directory URL and
loads a `.devstack/project.toml`, optional `.devstack/tasks.toml`, and
optional `.devstack/compose.yaml` from the selected directory. Applications,
tasks and Compose are copied into the private managed project directory;
the Git checkout remains untouched. The original source directory is tracked
in `.devstack/import.toml`.

On a linked project's **Sync project configuration** page:

1. Choose **Update source from Git** to fast-forward the *linked repository's*
   current branch. Devstack refuses dirty worktrees and non-fast-forward changes.
   This action does **not** edit local managed settings.
2. Review additions and updates from the Git template. New tasks and
   applications are preselected; existing entries—including updates upstream—
   require an explicit opt-in. Locally customized items are flagged, never
   silently overwritten. Items deleted from Git are retained locally.
3. Check the changes you want, then **Apply selected changes**. Compose
   changes require explicit selection and are validated before saving. Running
   containers are not restarted or rebuilt automatically.

Newly added GitHub projects capture a baseline of the source files in
`.devstack/.sync-base/` (private, mode 0600) to identify local customizations.
Projects added with older releases can still synchronize; because their
baseline wasn't recorded, differing existing entries are conservatively
treated as local customizations. The update preview includes a fingerprint
of source, local and baseline content; applying from an old preview fails
safely if any of those change.

This is a **configuration sync**, not a second Git clone or a general
filesystem sync. Local edits in cloned repositories, secrets, other project
repos and running Docker services are not modified by configuration sync.


## v0.4.0 — direct Tailscale development ports and selective public webhooks

### Development applications: regular ports over Tailscale

Your phone is already on the Tailscale VPN. **No additional private proxy,
Tailscale Serve mapping, or extra TLS port is needed** for project frontends
or APIs. Publish an application container port on the server's **actual
Tailscale IPv4 address**, for example:

```yaml
services:
  web:
    ports:
      - "100.101.102.103:18080:8000"
```

Replace `100.101.102.103` with the Devstack server's real address from
`tailscale ip -4`; don't copy this example literally. Make sure the app
inside Docker binds `0.0.0.0:8000`, not only its container loopback.
On a phone connected to the same Tailscale tailnet, open
`http://100.101.102.103:18080/` directly in Chrome or Safari. WebSockets
and live-reload connections use that normal TCP path. Tailnet access policies
and host firewall rules must allow the selected port.

Devstack displays **Open on Tailscale** on an explicitly registered
application only if Docker Compose already publishes its specified container
TCP port on exactly the server's Tailscale IP. It **does not rewrite Compose,
bind ports, automatically open firewalls, or expose 0.0.0.0 bindings**.
The bundled Smoke project still defaults to loopback-only `127.0.0.1:18080`;
edit its Compose port binding to your actual Tailscale IP if you want direct
phone access. Do not publish the administrative Devstack dashboard or
development ports on public IP addresses.

### Public webhooks: explicit POST-only forwarding

The private Devstack dashboard remains on Tailscale Serve HTTPS **443**.
Public webhooks, when you explicitly enable them, use **Funnel HTTPS 8443**
and a separate loopback-only listener `127.0.0.1:8767`.

Open **Public webhook routes** for a project in the dashboard. Create an
allowlisted, named route mapping to a **registered application** and an exact
destination path, for example:

```text
Public: https://<your-node>.ts.net:8443/hooks/demo/stripe
POST -> registered API service -> /webhooks/stripe
```

Only configured route names accept POST. The forwarding service never proxies
the private dashboard, general frontend pages, arbitrary HTTP methods, or
request-supplied URLs. It checks Compose container ownership and network
labels before forwarding. It preserves the raw request body and provider
signature headers and drops cookies, proxy headers and incoming host headers.
The destination service **must verify the provider's signature or shared
secret** before processing the webhook. A guessed public URL is not
authentication.

Add/remove operations **do not** publish the service. After merging, publishing
and installing the reviewed v0.4.0 release, the one-time installer refresh
provides a separate `devstack-webhooks.service` and an explicit Funnel helper:

```bash
# Only after reviewing the installer and publishing v0.4.0:
curl -fsSL https://raw.githubusercontent.com/rafaelpivato/devstack/main/scripts/install.sh | sudo bash
# Explicitly publish ONLY the webhook ingress (never the dashboard):
sudo python3 /usr/local/lib/devstack/webhook_funnel.py
# Revoke public exposure at any time:
sudo python3 /usr/local/lib/devstack/webhook_funnel.py --disable
```

The helper confirms the pinned Tailscale identity, existing exact private
Serve configuration, and local webhook-only health endpoint before enabling
Funnel 8443. Unexpected configurations are rejected rather than overwritten.
Funnel may additionally require tailnet feature enablement/permissions.
The listener itself is only on loopback; Funnel is **off by default**.

This release has no wildcard forwarding, arbitrary destination URLs,
general-purpose public proxies or additional tunneling providers.
