#!/usr/bin/env bash
# Optional private HTTPS: manual login, browser activation, or pre-provisioned credential.
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
if ! python3 /usr/local/lib/devstack/private_access.py --check-connected >/dev/null 2>&1; then
  # Automatic re-enrollment after first activation is forbidden.
  if [[ -e /var/lib/devstack/tailscale-identity.json ]]; then
    echo "Previously enrolled Tailscale identity is offline or expired; refusing automatic re-enrollment." >&2
    exit 1
  fi
  if [[ "${TAILSCALE_NONINTERACTIVE:-0}" == "1" ]]; then
    key_file="${TAILSCALE_AUTH_KEY_FILE:-}"
    if [[ -z "$key_file" || ! -f "$key_file" || -L "$key_file" || ! -s "$key_file" ]]; then
      echo "Missing or invalid Tailscale auth key file." >&2
      exit 1
    fi
    if [[ "$(stat -c %u -- "$key_file")" != "0" || -n "$(find "$key_file" -maxdepth 0 -perm /077 -print)" ]]; then
      echo "Tailscale auth key file must be root-owned and inaccessible to other users." >&2
      exit 1
    fi
    tailscale up "--auth-key=file:$key_file"
  elif [[ "${TAILSCALE_BROWSER_ACTIVATION:-0}" == "1" ]]; then
    # Print the login URL to cloud-init output. Do not block indefinitely:
    # a systemd timer finishes private Serve configuration after approval.
    echo "DEVSTACK ACTIVATION: open the Tailscale login URL below."
    echo "Authorize only from your own Tailscale account; confirm the device afterward."
    if [[ -n "${TAILSCALE_LOGIN_JSON_FILE:-}" ]]; then
      # Capture machine-readable browser URL without writing it to boot logs.
      # The setup page discloses it only after a valid one-time secret.
      umask 077
      if tailscale up --json --timeout=20s > "$TAILSCALE_LOGIN_JSON_FILE"; then
        echo "Tailscale authorized during bootstrap."
      else
        echo "Tailscale authorization pending; use the temporary setup page."
      fi
      chown devstack:devstack "$TAILSCALE_LOGIN_JSON_FILE"
      chmod 0600 "$TAILSCALE_LOGIN_JSON_FILE"
    elif tailscale up --timeout=20s; then
      echo "Tailscale activated during bootstrap."
    else
      echo "DEVSTACK ACTIVATION: pending browser approval."
      echo "Retrieve the URL from your cloud provider's startup/instance logs."
    fi
    if ! python3 /usr/local/lib/devstack/private_access.py --check-connected >/dev/null 2>&1; then
      echo "Devstack remains private until Tailscale authorization completes."
      exit 0
    fi
  else
    echo "Sign in to Tailscale using the browser URL printed below."
    tailscale up
  fi
fi
python3 /usr/local/lib/devstack/private_access.py
echo "Install the Tailscale iOS app, sign in to the same tailnet,"
echo "and open the HTTPS URL above in Safari. Never use Tailscale Funnel."
echo "Review tailnet access rules: all permitted tailnet members could control Devstack."
