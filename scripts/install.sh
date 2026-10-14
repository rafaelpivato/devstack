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

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3-venv python3-pip git curl ca-certificates

install -d -m 0755 /opt/devstack /usr/local/lib/devstack
install -d -m 0700 /etc/devstack
install -d -o root -g root -m 0700 /var/lib/devstack
install -d -o "$user" -g "$(id -gn "$user")" -m 0700 "$home/.config/devstack"
if [[ ! -e "$home/.config/devstack/projects.toml" ]]; then
  install -o "$user" -g "$(id -gn "$user")" -m 0600 /dev/null "$home/.config/devstack/projects.toml"
fi

if [[ ! -f /etc/devstack/app.env ]]; then
  secret="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
  printf 'DEVSTACK_SECRET_KEY=%s\nDEVSTACK_CONFIG=%s/.config/devstack/projects.toml\nDEVSTACK_SECURE_COOKIES=1\n' \
    "$secret" "$home" > /etc/devstack/app.env
  chmod 0600 /etc/devstack/app.env
fi
# Preserve existing settings while hardening sessions for HTTPS access.
if ! grep -q '^DEVSTACK_SECURE_COOKIES=' /etc/devstack/app.env; then
  printf 'DEVSTACK_SECURE_COOKIES=1\\n' >> /etc/devstack/app.env
fi
if [[ ! -f /etc/devstack/update.env ]]; then
  printf 'DEVSTACK_ROOT=/opt/devstack\nDEVSTACK_STATE=/var/lib/devstack\nDEVSTACK_REPO=%s\n' \
    "$repo" > /etc/devstack/update.env
  chmod 0600 /etc/devstack/update.env
fi

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT
base="https://raw.githubusercontent.com/$repo/main"
for item in scripts/updater.py scripts/private_access.py scripts/enable-tailscale.sh deploy/devstack.service deploy/devstack-update.service deploy/devstack-update.timer; do
  curl --fail --silent --show-error --location --retry 3 "$base/$item" \
    --output "$tmpdir/$(basename "$item")"
done
install -m 0644 "$tmpdir/updater.py" /usr/local/lib/devstack/updater.py
install -m 0644 "$tmpdir/private_access.py" /usr/local/lib/devstack/private_access.py
install -m 0755 "$tmpdir/enable-tailscale.sh" /usr/local/lib/devstack/enable-tailscale.sh
sed -e "s|@DEVSTACK_USER@|$user|g" -e "s|@DEVSTACK_HOME@|$home|g" \
  "$tmpdir/devstack.service" > /etc/systemd/system/devstack.service
install -m 0644 "$tmpdir/devstack-update.service" /etc/systemd/system/devstack-update.service
install -m 0644 "$tmpdir/devstack-update.timer" /etc/systemd/system/devstack-update.timer

# App service is enabled but starts only after a valid release is installed.
systemctl daemon-reload
systemctl enable devstack.service
systemctl enable --now devstack-update.timer
if ! systemctl start devstack-update.service; then
  echo "No working release installed yet (or update failed). See updater journal." >&2
  echo "The timer will retry when a valid stable GitHub release exists." >&2
fi

echo "Devstack installed. Dashboard listens on 127.0.0.1:8765 only."
echo "For optional, interactive private HTTPS setup, run:"
echo "  sudo /usr/local/lib/devstack/enable-tailscale.sh"
echo "Do not publicly expose port 8765."
echo "Logs: journalctl -u devstack -u devstack-update --no-pager -n 100"
