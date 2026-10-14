#!/usr/bin/env bash
# Opt-in, one-time private HTTPS access for Ubuntu.
# Run interactively after base installation; never from cloud-init user data.
set -Eeuo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root: sudo /usr/local/lib/devstack/enable-tailscale.sh" >&2
  exit 1
fi
# shellcheck disable=SC1091
source /etc/os-release
if [[ "${ID:-}" != "ubuntu" ]]; then
  echo "Tailscale setup currently supports Ubuntu only." >&2
  exit 1
fi
if [[ ! -f /usr/local/lib/devstack/private_access.py ]]; then
  echo "Install or upgrade Devstack first." >&2
  exit 1
fi

if ! command -v tailscale >/dev/null 2>&1; then
  echo "Installing Tailscale from its official signed Ubuntu APT repository..."
  codename="${UBUNTU_CODENAME:-${VERSION_CODENAME:-}}"
  if [[ ! "$codename" =~ ^[a-z]+$ ]]; then
    echo "Unsupported Ubuntu codename." >&2
    exit 1
  fi
  apt-get update -qq
  apt-get install -y -qq ca-certificates curl
  install -d -m 0755 /usr/share/keyrings /etc/apt/sources.list.d
  tmpdir="$(mktemp -d)"
  trap 'rm -rf "$tmpdir"' EXIT
  url="https://pkgs.tailscale.com/stable/ubuntu/$codename"
  curl -fsSL --retry 3 "$url.noarmor.gpg" -o "$tmpdir/keyring.gpg"
  curl -fsSL --retry 3 "$url.tailscale-keyring.list" -o "$tmpdir/tailscale.list"
  install -m 0644 "$tmpdir/keyring.gpg" /usr/share/keyrings/tailscale-archive-keyring.gpg
  install -m 0644 "$tmpdir/tailscale.list" /etc/apt/sources.list.d/tailscale.list
  apt-get update -qq
  apt-get install -y -qq tailscale
fi

systemctl enable --now tailscaled
if ! python3 /usr/local/lib/devstack/private_access.py --check-connected; then
  echo "Sign in to Tailscale using the browser URL printed below."
  echo "Use the same tailnet on your iPhone."
  tailscale up
fi
python3 /usr/local/lib/devstack/private_access.py
echo "Install the Tailscale iOS app, sign in to the same tailnet,"
echo "and open the HTTPS URL above in Safari. Never use Tailscale Funnel."
echo "Review tailnet access rules: all permitted tailnet members could control Devstack."
