#!/usr/bin/env bash
# DevStack v0.5.2 clean Ubuntu installation only. No legacy migration.
set -Eeuo pipefail
if [[ "$EUID" -ne 0 ]]; then echo "Run as root (sudo)." >&2; exit 1; fi
# shellcheck disable=SC1091
source /etc/os-release
if [[ "$ID" != "ubuntu" ]]; then echo "Ubuntu is required." >&2; exit 1; fi
if ! python3 -c 'import sys; assert sys.version_info >= (3, 11)' 2>/dev/null; then
  echo "Python 3.11+ required (Ubuntu 24.04 recommended)." >&2; exit 1
fi
: "${DEVSTACK_REPO:=rafaelpivato/devstack}"
repo="$DEVSTACK_REPO"
if [[ ! "$repo" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ || "$repo" == *..* ]]; then
  echo "Invalid GitHub repository." >&2; exit 1
fi

# A v0.5.1 installation is not an upgrade target. Decommission it and
# install onto an empty Ubuntu host instead of silently reusing old data.
if [[ -e /etc/systemd/system/devstack.service ||
      -e /var/lib/devstack/control.sqlite3 ||
      -e /var/lib/devstack/runtime.json ||
      -e /var/lib/devstack/projects.json ||
      -e /var/lib/devstack/config.toml ||
      -e /opt/devstack/current ]]; then
  echo "An existing DevStack installation/state was detected." >&2
  echo "v0.5.2 requires a clean host. No data was changed." >&2
  exit 1
fi

# Only published stable releases may be installed by this bootstrap.
# The v0.5.2 feature branch must not be installed as a release.
release_tag="$(DEVSTACK_REPO="$repo" python3 - <<'PY'
import json, os, re, urllib.request
repo = os.environ["DEVSTACK_REPO"]
req = urllib.request.Request(
    f"https://api.github.com/repos/{repo}/releases/latest",
    headers={"Accept":"application/vnd.github+json","User-Agent":"devstack-install-v2"},
)
with urllib.request.urlopen(req, timeout=20) as response:
    version = json.load(response)["tag_name"]
if not isinstance(version, str) or not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", version):
    raise SystemExit("No stable DevStack release available")
numbers = tuple(map(int, version[1:].split(".")))
if numbers < (0, 5, 2):
    raise SystemExit("Latest published release is older than v0.5.2; clean-install cutover not released yet")
print(version)
PY
)"
echo "Installing fresh DevStack $release_tag"

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3-venv python3-pip git curl ca-certificates
if [[ "${DEVSTACK_INSTALL_DOCKER:-1}" == "1" ]]; then
  apt-get install -y -qq docker.io docker-compose-v2
  systemctl enable --now docker.service
fi
command -v docker >/dev/null || { echo "Docker missing." >&2; exit 1; }
docker compose version >/dev/null || { echo "Docker Compose v2 missing." >&2; exit 1; }
getent group docker >/dev/null || { echo "Docker group missing." >&2; exit 1; }

# Separate unprivileged HTTP identity and sole privileged execution identity.
# The socket's narrow group is NOT a Docker group.
web_user=devstack
control_user=devstackctl
socket_group=devstackipc
if ! getent group "$socket_group" >/dev/null; then
  groupadd --system "$socket_group"
fi
if ! id "$web_user" &>/dev/null; then
  useradd --system --user-group --create-home --home-dir /var/lib/devstack-web \
    --shell /usr/sbin/nologin "$web_user"
fi
if ! id "$control_user" &>/dev/null; then
  useradd --system --user-group --create-home --home-dir /var/lib/devstack \
    --shell /usr/sbin/nologin "$control_user"
fi
if id -nG "$web_user" | tr ' ' '\n' | grep -qx docker; then
  echo "Security violation: web service user is a member of docker." >&2; exit 1
fi
usermod -aG "$socket_group" "$web_user"
usermod -aG docker "$control_user"
install -d -m 0755 /opt/devstack /usr/local/lib/devstack
install -d -o "$control_user" -g "$control_user" -m 0700 /var/lib/devstack
install -d -o "$control_user" -g "$control_user" -m 0700 /var/lib/devstack/projects
install -d -o "$web_user" -g "$web_user" -m 0700 /var/lib/devstack-bootstrap
install -d -o root -g "$socket_group" -m 0750 /etc/devstack

secret="$(python3 -c 'import secrets;print(secrets.token_hex(32))')"
printf 'DEVSTACK_SECRET_KEY=%s\nDEVSTACK_SECURE_COOKIES=1\nDEVSTACK_CONTROL_SOCKET=/run/devstack-control/control.sock\n' "$secret" > /etc/devstack/app.env
chown root:"$socket_group" /etc/devstack/app.env
chmod 0640 /etc/devstack/app.env
printf 'DEVSTACK_ROOT=/opt/devstack\nDEVSTACK_STATE=/var/lib/devstack\nDEVSTACK_REPO=%s\n' "$repo" > /etc/devstack/update.env
chmod 0600 /etc/devstack/update.env
unset secret

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT
base="https://raw.githubusercontent.com/$repo/$release_tag"
for path in \
  scripts/updater.py scripts/private_access.py scripts/webhook_funnel.py \
  scripts/enable-tailscale.sh scripts/bootstrap.py \
  deploy/devstack-control.service deploy/devstack-web-v2.service \
  deploy/devstack-update.service deploy/devstack-update.timer \
  deploy/devstack-tailscale-finish.service deploy/devstack-tailscale-finish.timer \
  deploy/devstack-bootstrap.service
do
  file="$(basename "$path")"
  curl --fail --silent --show-error --location --retry 3 "$base/$path" -o "$tmpdir/$file"
done
install -m 0644 "$tmpdir/updater.py" /usr/local/lib/devstack/updater.py
install -m 0644 "$tmpdir/private_access.py" /usr/local/lib/devstack/private_access.py
install -m 0755 "$tmpdir/webhook_funnel.py" /usr/local/lib/devstack/webhook_funnel.py
install -m 0755 "$tmpdir/enable-tailscale.sh" /usr/local/lib/devstack/enable-tailscale.sh
install -m 0755 "$tmpdir/bootstrap.py" /usr/local/lib/devstack/bootstrap.py
sed -e "s|@DEVSTACK_CONTROL_USER@|$control_user|g" \
    -e "s|@DEVSTACK_SOCKET_GROUP@|$socket_group|g" \
    "$tmpdir/devstack-control.service" > /etc/systemd/system/devstack-control.service
sed -e "s|@DEVSTACK_WEB_USER@|$web_user|g" \
    -e "s|@DEVSTACK_SOCKET_GROUP@|$socket_group|g" \
    "$tmpdir/devstack-web-v2.service" > /etc/systemd/system/devstack-web-v2.service
sed -e "s|@DEVSTACK_WEB_USER@|$web_user|g" \
    "$tmpdir/devstack-bootstrap.service" > /etc/systemd/system/devstack-bootstrap.service
for file in devstack-update.service devstack-update.timer \
  devstack-tailscale-finish.service devstack-tailscale-finish.timer
do
  install -m 0644 "$tmpdir/$file" "/etc/systemd/system/$file"
done
systemctl daemon-reload
systemctl enable devstack-control.service devstack-web-v2.service
if ! python3 /usr/local/lib/devstack/updater.py --tag "$release_tag"; then
  echo "Release health checks failed; inspect devstack-control and devstack-web-v2 journals." >&2
  exit 1
fi
systemctl enable --now devstack-update.timer
echo "DevStack $release_tag ready; dashboard is private at http://127.0.0.1:8765."
echo "For private HTTPS, run: sudo /usr/local/lib/devstack/enable-tailscale.sh"
echo "Do not expose ports 8765 or 8767 publicly."
echo "Optional public webhooks (project UUID paths):"
echo "  sudo python3 /usr/local/lib/devstack/webhook_funnel.py"
echo "Logs: journalctl -u devstack-control -u devstack-web-v2 --no-pager -n 100"

# First-use browser activation remains opt-in and separate from the dashboard.
if [[ -n "${DEVSTACK_BOOTSTRAP_SECRET:-}" ]]; then
  if [[ "${DEVSTACK_TAILSCALE:-0}" != "1" ]]; then
    echo "DEVSTACK_BOOTSTRAP_SECRET requires DEVSTACK_TAILSCALE=1." >&2; exit 1
  fi
  if [[ -e /var/lib/devstack/tailscale-identity.json || -e /etc/devstack/bootstrap.env ]]; then
    echo "Refusing to reopen an existing onboarding portal." >&2; exit 1
  fi
  if (( ${#DEVSTACK_BOOTSTRAP_SECRET} < 24 )); then
    echo "Use a random bootstrap secret of at least 24 characters." >&2; exit 1
  fi
  digest="$(printf '%s' "$DEVSTACK_BOOTSTRAP_SECRET" | sha256sum | cut -d' ' -f1)"
  printf 'DEVSTACK_BOOTSTRAP_HASH=%s\n' "$digest" > /etc/devstack/bootstrap.env
  chown root:"$web_user" /etc/devstack/bootstrap.env
  chmod 0640 /etc/devstack/bootstrap.env
  unset DEVSTACK_BOOTSTRAP_SECRET digest
  echo "Temporary first-boot Tailscale login portal: http://<instance-public-ip>/"
fi

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
