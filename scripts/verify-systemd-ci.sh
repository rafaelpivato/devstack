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
ipc=devstack-ci-ipc
state=/var/lib/devstack-ci
socket=/run/devstack-control-ci/control.sock
root=/opt/devstack-ci
control_unit=/etc/systemd/system/devstack-ci-control.service
web_unit=/etc/systemd/system/devstack-ci-web.service
envfile=/etc/devstack-ci/app.env

cleanup() {
  systemctl stop devstack-ci-web.service devstack-ci-control.service 2>/dev/null || :
  rm -f "$control_unit" "$web_unit"
  systemctl daemon-reload
}
trap cleanup EXIT

# A real install into a root-owned location that ProtectHome allows access to.
python3 -m venv "$root/venv"
"$root/venv/bin/python" -m pip install -q --disable-pip-version-check .
groupadd --system "$ipc"
useradd --system --no-create-home --gid "$ipc" \
  --shell /usr/sbin/nologin "$web"
useradd --system --no-create-home --user-group \
  --shell /usr/sbin/nologin "$controller"
usermod -aG docker "$controller"
if id -nG "$web" | tr ' ' '\n' | grep -qx docker; then
  echo "Web service user has Docker privileges" >&2
  exit 1
fi

install -d -m 0700 -o "$controller" -g "$controller" "$state"
install -d -m 0750 -o root -g "$ipc" /etc/devstack-ci
printf 'DEVSTACK_SECRET_KEY=ci-test-only-secret-not-for-real-deployments\nDEVSTACK_SECURE_COOKIES=0\nDEVSTACK_CONTROL_SOCKET=%s\n' \
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
  deploy/devstack-web-v2.service > "$web_unit"

systemctl daemon-reload
systemctl start devstack-ci-control.service
systemctl start devstack-ci-web.service
for attempt in $(seq 1 45); do
  if curl --silent --fail http://127.0.0.1:18765/healthz > /tmp/devstack-ci-health.json; then
    break
  fi
  sleep 1
done
cat /tmp/devstack-ci-health.json
"$root/venv/bin/python" - <<'PY'
import json, urllib.request
with urllib.request.urlopen("http://127.0.0.1:18765/healthz", timeout=3) as r:
    assert json.load(r) == {"ok": True, "controller": "connected"}
with urllib.request.urlopen("http://127.0.0.1:18765/api/v2/runtime", timeout=3) as r:
    state = json.load(r)
    assert (state["phase"], state["project_id"]) == ("inactive", None)
PY

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
