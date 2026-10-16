#!/usr/bin/env bash
# Portable Ubuntu bootstrap. Run as root via cloud-init or sudo.
set -Eeuo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run installer as root (sudo)." >&2
  exit 1
fi
# shellcheck disable=SC1091
source /etc/os-release
if [[ "${ID:-}" != "ubuntu" ]]; then
  echo "Devstack currently supports Ubuntu only." >&2
  exit 1
fi
if ! python3 -c 'import sys; assert sys.version_info >= (3, 11)' 2>/dev/null; then
  echo "Python 3.11+ is required. Use Ubuntu 24.04 LTS or newer." >&2
  exit 1
fi

user="${DEVSTACK_USER:-devstack}"
repo="${DEVSTACK_REPO:-rafaelpivato/devstack}"
if [[ ! "$user" =~ ^[a-z_][a-z0-9_-]*$ || "$user" == "root" ]]; then
  echo "Invalid DEVSTACK_USER." >&2; exit 1
fi
if [[ ! "$repo" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ || "$repo" == *..* ]]; then
  echo "Invalid DEVSTACK_REPO." >&2; exit 1
fi
if ! id "$user" &>/dev/null; then
  useradd --create-home --shell /bin/bash "$user"
fi
home="$(getent passwd "$user" | cut -d: -f6)"
if [[ -z "$home" || ! -d "$home" ]]; then
  echo "User home does not exist: $home" >&2; exit 1
fi

# Resolve the latest published stable release once for a coherent initial
# installation. This does NOT pin later upgrades: the updater continues to
# follow each new published GitHub release automatically (including v0.6).
release_tag="$(DEVSTACK_REPO="$repo" python3 - <<'PY'
import json
import os
import re
import urllib.request
repo = os.environ["DEVSTACK_REPO"]
req = urllib.request.Request(
    f"https://api.github.com/repos/{repo}/releases/latest",
    headers={"Accept": "application/vnd.github+json", "User-Agent": "devstack-install"},
)
with urllib.request.urlopen(req, timeout=15) as response:
    tag = json.load(response)["tag_name"]
if not isinstance(tag, str) or not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", tag):
    raise SystemExit("No valid published stable DevStack release")
print(tag)
PY
)"
echo "Installing latest DevStack release $release_tag."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3-venv python3-pip git curl ca-certificates
# Rootful Docker is deliberately enabled for this single-user private server.
# Membership in the docker group is root-equivalent. Do not grant it to
# unrelated users. Use Ubuntu's packages to avoid dependence on cloud tooling.
if [[ "${DEVSTACK_INSTALL_DOCKER:-1}" == "1" ]]; then
  apt-get install -y -qq docker.io docker-compose-v2
  systemctl enable --now docker.service
  usermod -aG docker "$user"
fi

install -d -m 0755 /opt/devstack /usr/local/lib/devstack
install -d -m 0700 /etc/devstack
install -d -o root -g root -m 0700 /var/lib/devstack
install -d -o "$user" -g "$(id -gn "$user")" -m 0700 "$home/.config/devstack"
if [[ ! -e "$home/.config/devstack/workspace.toml" ]]; then
  install -o "$user" -g "$(id -gn "$user")" -m 0600 /dev/null "$home/.config/devstack/workspace.toml"
fi

if [[ ! -f /etc/devstack/app.env ]]; then
  secret="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
  printf 'DEVSTACK_SECRET_KEY=%s\nDEVSTACK_CONFIG=%s/.config/devstack/workspace.toml\nDEVSTACK_SECURE_COOKIES=1\n' \
    "$secret" "$home" > /etc/devstack/app.env
  chmod 0600 /etc/devstack/app.env
fi
# Preserve existing settings while hardening sessions for HTTPS access.
if ! grep -q '^DEVSTACK_SECURE_COOKIES=' /etc/devstack/app.env; then
  printf 'DEVSTACK_SECURE_COOKIES=1\n' >> /etc/devstack/app.env
fi
if ! grep -q '^DEVSTACK_RELEASE_REPO=' /etc/devstack/app.env; then
  printf 'DEVSTACK_RELEASE_REPO=%s\n' "$repo" >> /etc/devstack/app.env
fi
if [[ ! -f /etc/devstack/update.env ]]; then
  printf 'DEVSTACK_ROOT=/opt/devstack\nDEVSTACK_STATE=/var/lib/devstack\nDEVSTACK_REPO=%s\n' \
    "$repo" > /etc/devstack/update.env
  chmod 0600 /etc/devstack/update.env
fi

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT
base="https://raw.githubusercontent.com/$repo/$release_tag"
for item in scripts/updater.py scripts/private_access.py scripts/webhook_funnel.py scripts/enable-tailscale.sh scripts/bootstrap.py deploy/devstack.service deploy/devstack-webhooks.service deploy/devstack-update.service deploy/devstack-update.timer deploy/devstack-update-request.service deploy/devstack-update-request.path deploy/devstack-tailscale-finish.service deploy/devstack-tailscale-finish.timer deploy/devstack-bootstrap.service; do
  curl --fail --silent --show-error --location --retry 3 "$base/$item" \
    --output "$tmpdir/$(basename "$item")"
done
install -m 0644 "$tmpdir/updater.py" /usr/local/lib/devstack/updater.py
install -m 0644 "$tmpdir/private_access.py" /usr/local/lib/devstack/private_access.py
install -m 0755 "$tmpdir/webhook_funnel.py" /usr/local/lib/devstack/webhook_funnel.py
install -m 0755 "$tmpdir/enable-tailscale.sh" /usr/local/lib/devstack/enable-tailscale.sh
install -m 0755 "$tmpdir/bootstrap.py" /usr/local/lib/devstack/bootstrap.py
sed -e "s|@DEVSTACK_USER@|$user|g" -e "s|@DEVSTACK_HOME@|$home|g" \
  "$tmpdir/devstack.service" > /etc/systemd/system/devstack.service
sed -e "s|@DEVSTACK_USER@|$user|g" -e "s|@DEVSTACK_HOME@|$home|g" \
  "$tmpdir/devstack-webhooks.service" > /etc/systemd/system/devstack-webhooks.service
install -m 0644 "$tmpdir/devstack-update.service" /etc/systemd/system/devstack-update.service
install -m 0644 "$tmpdir/devstack-update.timer" /etc/systemd/system/devstack-update.timer
install -m 0644 "$tmpdir/devstack-update-request.service" /etc/systemd/system/devstack-update-request.service
install -m 0644 "$tmpdir/devstack-update-request.path" /etc/systemd/system/devstack-update-request.path
install -m 0644 "$tmpdir/devstack-tailscale-finish.service" /etc/systemd/system/devstack-tailscale-finish.service
install -m 0644 "$tmpdir/devstack-tailscale-finish.timer" /etc/systemd/system/devstack-tailscale-finish.timer
install -m 0644 "$tmpdir/devstack-bootstrap.service" /etc/systemd/system/devstack-bootstrap.service

# App service is enabled but starts only after a valid release is installed.
systemctl daemon-reload
systemctl enable devstack.service
# Webhook-only ingress listens on loopback; no Funnel is enabled automatically.
systemctl enable devstack-webhooks.service
# Boot the release matching the downloaded units; later timer runs follow
# new GitHub releases without a version-series restriction.
if ! /usr/bin/python3 /usr/local/lib/devstack/updater.py --tag "$release_tag"; then
  echo "Failed to activate $release_tag. Inspect devstack.service journal." >&2
  exit 1
fi
systemctl enable --now devstack-update.timer

# Refresh the service after changing Docker group membership: systemd assigns
# supplementary groups only at process startup.
# Updating a running host replaces its service unit, but an "already running"
# release does not restart the application. Restart here to activate
# RuntimeDirectory and its new watcher without waiting for another release.
if systemctl is-active --quiet devstack.service; then
  systemctl restart devstack.service
  if ! systemctl start devstack-webhooks.service; then
    echo "Webhook ingress listener not yet available; check devstack-webhooks.service." >&2
  fi
fi
systemctl enable --now devstack-update-request.path

echo "Devstack installed. Dashboard listens on 127.0.0.1:8765 only."
echo "For optional, interactive private HTTPS setup, run:"
echo "  sudo /usr/local/lib/devstack/enable-tailscale.sh"
echo "Do not publicly expose ports 8765 or 8767."
echo "Optional public webhook ingress (explicit opt-in only):"
echo "  sudo python3 /usr/local/lib/devstack/webhook_funnel.py"
echo "Logs: journalctl -u devstack -u devstack-update --no-pager -n 100"

# Optional short-lived HTTP first-boot portal. The bootstrap secret is
# supplied by the instance owner in cloud-init, and ONLY its SHA-256 digest
# is written to disk. A strong random secret is required.
if [[ -n "${DEVSTACK_BOOTSTRAP_SECRET:-}" ]]; then
  if [[ "${DEVSTACK_TAILSCALE:-0}" != "1" ]]; then
    echo "DEVSTACK_BOOTSTRAP_SECRET requires DEVSTACK_TAILSCALE=1." >&2
    exit 1
  fi
  if [[ -e /var/lib/devstack/tailscale-identity.json || -e /etc/devstack/bootstrap.env ]]; then
    echo "Refusing to recreate the one-time onboarding portal." >&2
    exit 1
  fi
  if (( ${#DEVSTACK_BOOTSTRAP_SECRET} < 24 )); then
    echo "Use a random first-time bootstrap secret of at least 24 characters." >&2
    exit 1
  fi
  secret_digest="$(printf %s "$DEVSTACK_BOOTSTRAP_SECRET" | sha256sum | cut -d' ' -f1)"
  printf 'DEVSTACK_BOOTSTRAP_HASH=%s\n' "$secret_digest" > /etc/devstack/bootstrap.env
  chmod 0600 /etc/devstack/bootstrap.env
  install -d -o "$user" -g "$(id -gn "$user")" -m 0700 /var/lib/devstack-bootstrap
  unset DEVSTACK_BOOTSTRAP_SECRET secret_digest
  echo "One-time HTTP activation portal will listen on port 80."
  echo "Open http://<instance-public-ip>/ and enter the setup secret you supplied in EC2 User Data."
fi

# Tailscale is initialized without pre-provisioned Tailscale credentials.
# A private systemd timer completes Serve only after browser authorization.
if [[ "${DEVSTACK_TAILSCALE:-0}" == "1" ]]; then
  systemctl enable --now devstack-tailscale-finish.timer
  if [[ -n "${TAILSCALE_AUTH_KEY_FILE:-}" ]]; then
    TAILSCALE_NONINTERACTIVE=1 /usr/local/lib/devstack/enable-tailscale.sh
  elif [[ -e /etc/devstack/bootstrap.env ]]; then
    TAILSCALE_BROWSER_ACTIVATION=1 \
      TAILSCALE_LOGIN_JSON_FILE=/var/lib/devstack-bootstrap/activation-url \
      /usr/local/lib/devstack/enable-tailscale.sh
    systemctl enable --now devstack-bootstrap.service
  else
    TAILSCALE_BROWSER_ACTIVATION=1 /usr/local/lib/devstack/enable-tailscale.sh
  fi
fi
