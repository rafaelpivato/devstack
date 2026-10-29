#!/usr/bin/env bash
# ONLY for ephemeral Ubuntu CI runners. Requires root via sudo and a running
# Docker daemon. Exercises real systemd UIDs and the unprivileged v2 IPC path.
set -Eeuo pipefail
if [[ "${DEVSTACK_EPHEMERAL_CI:-}" != "1" || "${EUID}" != "0" ]]; then
  echo "Refusing host verification outside an explicitly isolated root CI runner" >&2
  exit 1
fi

web=devstack-ci-web
controller=devstack-ci-control
operator=devstack-ci-operator
guest=devstack-ci-guest
ipc=devstack-ci-ipc
state=/var/lib/devstack-ci
socket=/run/devstack-control-ci/control.sock
root=/opt/devstack-ci
control_unit=/etc/systemd/system/devstack-ci-control.service
web_unit=/etc/systemd/system/devstack-ci-web.service
envfile=/etc/devstack-ci/app.env
request_path=/etc/systemd/system/devstack-ci-update-request.path
request_service=/etc/systemd/system/devstack-ci-update-request.service
update_target=/etc/systemd/system/devstack-ci-update.service

cleanup() {
  systemctl stop devstack-ci-update-request.path devstack-ci-update-request.service \
    devstack-ci-update.service devstack-ci-web.service \
    devstack-ci-control.service 2>/dev/null || :
  rm -f "$control_unit" "$web_unit" "$request_path" "$request_service" \
    "$update_target" /run/devstack-ci-update-sentinel
  rm -rf /etc/systemd/system/devstack-ci-web.service.d /var/lib/devstack-ci-web
  systemctl daemon-reload
}
trap cleanup EXIT

# A real install into a root-owned location that ProtectHome allows access to.
python3 -m venv "$root/venv"
if [[ -z "${DEVSTACK_CI_WHEEL:-}" || ! -f "$DEVSTACK_CI_WHEEL" ]]; then
  echo "Expected verified CI-built wheel path" >&2
  exit 1
fi
"$root/venv/bin/python" -m pip install -q --disable-pip-version-check "$DEVSTACK_CI_WHEEL"
groupadd --system "$ipc"
useradd --system --no-create-home --gid "$ipc" \
  --shell /usr/sbin/nologin "$web"
useradd --system --no-create-home --user-group \
  --shell /usr/sbin/nologin "$controller"
usermod -aG docker "$controller"
useradd --no-create-home --shell /bin/bash "$operator"
useradd --no-create-home --shell /bin/bash "$guest"
usermod -aG "$ipc" "$operator"
if id -nG "$operator" | tr ' ' '\n' | grep -qx docker; then
  echo "Security violation: CLI operator has Docker group membership" >&2
  exit 1
fi
if id -nG "$web" | tr ' ' '\n' | grep -qx docker; then
  echo "Web service user has Docker privileges" >&2
  exit 1
fi

install -d -m 0700 -o "$controller" -g "$controller" "$state"
install -d -m 0750 -o root -g "$ipc" /etc/devstack-ci
printf 'DEVSTACK_SECRET_KEY=ci-test-only-secret-not-for-real-deployments\nDEVSTACK_SECURE_COOKIES=0\nDEVSTACK_CONTROL_SOCKET=%s\nDEVSTACK_WEB_DIAGNOSTICS_PATH=/var/lib/devstack-ci-web/diagnostics.sqlite3\n' \
  "$socket" > "$envfile"
chown root:"$ipc" "$envfile"
chmod 0640 "$envfile"

sed \
  -e "s|@DEVSTACK_CONTROL_USER@|$controller|g" \
  -e "s|@DEVSTACK_SOCKET_GROUP@|$ipc|g" \
  -e "s|/opt/devstack/current/venv|$root/venv|g" \
  -e "s|/var/lib/devstack|$state|g" \
  -e 's|/run/devstack-control|/run/devstack-control-ci|g' \
  -e 's|RuntimeDirectory=devstack-control$|RuntimeDirectory=devstack-control-ci|g' \
  -e 's|--socket-group devstack-ci-ipc$|--socket-group devstack-ci-ipc --webhook-port 18767|g' \
  deploy/devstack-control.service > "$control_unit"

sed \
  -e "s|@DEVSTACK_WEB_USER@|$web|g" \
  -e "s|@DEVSTACK_SOCKET_GROUP@|$ipc|g" \
  -e "s|devstack-control.service|devstack-ci-control.service|g" \
  -e "s|/etc/devstack/app.env|$envfile|g" \
  -e "s|/opt/devstack/current/venv|$root/venv|g" \
  -e 's|127.0.0.1:8765|127.0.0.1:18765|g' \
  -e '/^StateDirectory=/d' \
  -e '/^StateDirectoryMode=/d' \
  deploy/devstack-web-v2.service > "$web_unit"

# Reproduce an upgraded host: the installed service predates StateDirectory.
# The exact release-owned repair must add scoped persistent state without SSH
# or granting Docker, journal, or host permissions.
"$root/venv/bin/python" - <<'PY'
from pathlib import Path
from devstack.diagnostics_deploy import ensure_state_directory
unit = Path("/etc/systemd/system/devstack-ci-web.service")
dropin = Path("/etc/systemd/system/devstack-ci-web.service.d/10-diagnostics-state.conf")
assert ensure_state_directory(unit, dropin, "devstack-ci-web")
assert not ensure_state_directory(unit, dropin, "devstack-ci-web")
assert "StateDirectoryMode=0700" in dropin.read_text()
PY

# Exercise the exact path-watcher privilege handoff with a harmless
# CI-only root service rather than contacting or installing a real release.
sed 's|devstack-update-request.service|devstack-ci-update-request.service|g' \
  deploy/devstack-update-request.path > "$request_path"
sed 's|devstack-update.service|devstack-ci-update.service|g' \
  deploy/devstack-update-request.service > "$request_service"
cat > "$update_target" <<'UNIT'
[Unit]
Description=CI-only proof of manual update request handoff
[Service]
Type=oneshot
ExecStart=/usr/bin/touch /run/devstack-ci-update-sentinel
UNIT

systemctl daemon-reload
systemctl start devstack-ci-control.service
systemctl start devstack-ci-web.service
systemctl start devstack-ci-update-request.path
for attempt in $(seq 1 45); do
  if curl --silent --fail http://127.0.0.1:18765/healthz > /tmp/devstack-ci-health.json; then
    break
  fi
  sleep 1
done
cat /tmp/devstack-ci-health.json

# Exercise persistent diagnostics through actual Gunicorn + systemd sandbox,
# not merely runuser (which bypasses service mount restrictions).
test "$(stat -c '%a' /var/lib/devstack-ci-web)" = "700"
test "$(stat -c '%U' /var/lib/devstack-ci-web)" = "$web"
curl -fsS 'http://127.0.0.1:18765/api/v2/devstack/diagnostics?minutes=5' > /tmp/devstack-ci-diag-before.json
systemctl restart devstack-ci-web.service
for attempt in $(seq 1 45); do
  if curl -fsS 'http://127.0.0.1:18765/api/v2/devstack/diagnostics?minutes=5' > /tmp/devstack-ci-diag-after.json; then
    break
  fi
  sleep 1
done
"$root/venv/bin/python" - <<'PY'
import json
from pathlib import Path
before=json.loads(Path("/tmp/devstack-ci-diag-before.json").read_text())
after=json.loads(Path("/tmp/devstack-ci-diag-after.json").read_text())
assert before["sources"]["web"] == "available", before["sources"]
assert after["sources"]["web"] == "available", after["sources"]
assert after["web_storage"] == {"mode":"persistent","error_code":None}, after["web_storage"]
startup=[event for event in after["events"] if event["source"] == "web"
         and event["code"] == "web-started"]
assert len(startup) >= 2, startup
PY
echo "Persistent Web diagnostic events survive systemd Web service restart: PASS"
"$root/venv/bin/python" - <<'PY'
import json, urllib.request
with urllib.request.urlopen("http://127.0.0.1:18765/healthz", timeout=3) as r:
    assert json.load(r) == {"ok": True, "controller": "connected"}
with urllib.request.urlopen("http://127.0.0.1:18765/api/v2/runtime", timeout=3) as r:
    state = json.load(r)
    assert (state["phase"], state["project_id"]) == ("inactive", None)
PY

# Verify the real web-user -> root systemd.path handoff without running
# the updater or making an outbound release request.
test "$(stat -c '%a' /run/devstack-update-request)" = "700"
runuser -u "$web" -- test -w /run/devstack-update-request
runuser -u "$web" -- touch /run/devstack-update-request/trigger
for attempt in $(seq 1 30); do
  [[ -f /run/devstack-ci-update-sentinel ]] && break
  sleep 1
done
test -f /run/devstack-ci-update-sentinel
test ! -e /run/devstack-update-request/trigger
echo "Manual update systemd path handoff exercised successfully"

# The v0.7.0 field failure: the web user could not queue the MCP marker in
# /var/lib. Exercise its replacement in the *existing* sandboxed Web
# RuntimeDirectory with the exact service UID and protections.
test "$(stat -c '%a' /run/devstack-update-request)" = "700"
runuser -u "$web" -- env PYTHONPATH="$PWD/src" "$root/venv/bin/python" - <<'PY'
from pathlib import Path
from unittest.mock import patch
from devstack import mcp_management as mcp
runtime = Path("/run/devstack-update-request")
with patch.object(mcp, "REQUEST_DIR", runtime):
    mcp.request_action("enable")
    assert (runtime / "mcp-action").read_bytes() == b"enable"
    (runtime / "mcp-action").unlink()
PY
echo "MCP request writable from real Web user in verified RuntimeDirectory: PASS"

# Verify permissions using the actual web user, not a mock or text inspection.
if runuser -u "$web" -- test -r "$state/control.sqlite3"; then
  echo "Security violation: web can read controller SQLite state" >&2
  exit 1
fi
if runuser -u "$web" -- test -r "$state/projects"; then
  echo "Security violation: web can read controller project workspaces" >&2
  exit 1
fi
if [[ "$(stat -c %a "$state")" != "700" ]]; then
  echo "Controller state is not private" >&2
  exit 1
fi

runuser -u "$web" -- "$root/venv/bin/python" - "$socket" <<'PY'
import socket, sys
from devstack.api.client import ControllerRejected, send

path = sys.argv[1]
result = send(path, "projects")
assert result["projects"] == []
a = send(path, "project_create", slug="alpha", name="Alpha")
b = send(path, "project_create", slug="beta", name="Beta")
assert a["id"] != b["id"]
assert len(send(path, "projects")["projects"]) == 2
assert send(path, "status")["phase"] == "inactive"
try:
    send(path, "exec", argv=["docker", "ps"])
except ControllerRejected:
    pass
else:
    raise AssertionError("Arbitrary host command unexpectedly accepted")
try:
    target = socket.socket(socket.AF_UNIX)
    target.connect("/var/run/docker.sock")
except (PermissionError, FileNotFoundError):
    pass
else:
    raise AssertionError("Web account unexpectedly connected to Docker socket")
print("Web identity has scoped project IPC but no database, Docker or shell control")
PY

# Real Linux-user isolation for the new native CLI, not just mocked IPC.
operator_json="$(runuser -u "$operator" -- "$root/venv/bin/dev" \
  --socket "$socket" --json status)"
printf '%s' "$operator_json" | python3 -c \
  'import json,sys; assert json.load(sys.stdin)["phase"] == "inactive"'
runuser -u "$operator" -- "$root/venv/bin/dev" \
  --socket "$socket" doctor | grep -q 'IPC: compatible'
if runuser -u "$operator" -- test -r "$state/control.sqlite3"; then
  echo "Security violation: authorized CLI user can read control DB" >&2
  exit 1
fi
if runuser -u "$operator" -- test -r /var/run/docker.sock; then
  echo "Security violation: authorized CLI user can read Docker socket" >&2
  exit 1
fi
if runuser -u "$guest" -- "$root/venv/bin/dev" --socket "$socket" status \
    >/tmp/devstack-ci-cli-guest.out 2>/tmp/devstack-ci-cli-guest.err; then
  echo "Security violation: unauthorized CLI user reached controller" >&2
  exit 1
else
  status=$?
  if [[ "$status" != 77 ]]; then
    echo "Wrong CLI denial exit code: $status" >&2
    exit 1
  fi
fi
test ! -s /tmp/devstack-ci-cli-guest.out
grep -q 'access denied' /tmp/devstack-ci-cli-guest.err
# Unit-test stable symlink logic with a private collision path, avoiding
# arbitrary changes to /usr/local/bin on a shared GitHub runner.
ln -s "$root" "$root/current"
ci_link_dir="$(mktemp -d)"
"$root/venv/bin/python" - "$root" "$ci_link_dir" <<'PY'
import pathlib, sys
from devstack.cli.install import CLIInstallError, install_command
root = pathlib.Path(sys.argv[1])
dest = pathlib.Path(sys.argv[2]) / "dev"
assert install_command(root, dest)
assert not install_command(root, dest)
assert dest.is_symlink() and dest.resolve() == (root / "venv/bin/dev").resolve()
collision = dest.with_name("collision")
collision.write_text("leave untouched")
try:
    install_command(root, collision)
except CLIInstallError:
    pass
else:
    raise AssertionError("An unrelated executable was replaced")
assert collision.read_text() == "leave untouched"
PY
rm -rf "$ci_link_dir"
echo "Native CLI operator group access, guest denial, and wrapper safety: PASS"

# Check new durable operation history without giving SSH users filesystem
# access to the controller's SQLite sidecar or original registry.
runuser -u "$operator" -- "$root/venv/bin/dev" \
  --socket "$socket" --json runtime operations | \
  python3 -c 'import json,sys; assert json.load(sys.stdin)["operations"] == []'
if runuser -u "$operator" -- test -r "$state/runtime-transitions.sqlite3"; then
  echo "Security violation: CLI user can read transition journal" >&2
  exit 1
fi

# systemd restart must reconcile without activating stored projects.
systemctl restart devstack-ci-control.service
for attempt in $(seq 1 30); do
  if curl --silent --fail http://127.0.0.1:18765/healthz >/dev/null; then break; fi
  sleep 1
done
"$root/venv/bin/python" - <<'PY'
import json, urllib.request
base="http://127.0.0.1:18765"
with urllib.request.urlopen(base+"/api/v2/projects") as r:
    assert len(json.load(r)["projects"]) == 2
with urllib.request.urlopen(base+"/api/v2/runtime") as r:
    status=json.load(r)
    assert status["phase"] == "inactive" and status["project_id"] is None
PY
echo "Real Ubuntu systemd web/controller account separation, persistence, restart: PASS"
