# DevStack v0.7.3 HTTPS ingress

The DevStack networking boundary has **two HTTP-facing applications**,
both native Python processes managed through systemd. No additional reverse
proxy, Docker infrastructure container, or continuously running network manager
is required; the existing Tailscale daemon terminates HTTPS.

| Boundary | Tailscale HTTPS | Loopback target |
| --- | --- | --- |
| Private dashboard/API | Serve :8443 (tailnet only) | devstack-web-v2 :8765 |
| Public MCP and webhooks | Funnel :443 (opt-in) | devstack-public :8768 |

The public ASGI app explicitly implements /mcp and
/hooks/<project-id>/<route>. Disabled integrations have no endpoint; unknown
paths, including public /, return 404. The public process has no Docker group
membership or administrative controller socket access.

## Webhooks: control versus data plane

1. Tailscale forwards /hooks to the unprivileged devstack-public app.
2. A narrow, group-restricted Unix socket asks the controller only for a
   registered active-project destination and a delivery lease.
3. devstack-public forwards the original bounded HTTP POST directly to the
   validated private Docker bridge IP/port and returns the response.
4. devstack-public releases the lease; project switching waits for in-flight
   deliveries. Application services verify their provider-specific signatures.

No webhook body goes through controller IPC or MCP. This is specialized
HTTP delivery, not an arbitrary reverse-proxy framework.

## Fresh cloud-init installation

The supported installer detects the latest stable GitHub release. On a new
Ubuntu server it configures Tailscale Serve private :8443 after initial
Tailscale enrollment, and leaves public Funnel disabled until explicitly
enabled from the existing MCP/public integration controls.

The private address is
`https://<node>.<tailnet>.ts.net:8443/`.

## v0.7.2 upgrade without manual migration

v0.7.2 used private Serve :443, optional webhook Funnel :8443 and
optional MCP Funnel :10000. The v0.7.3 verified wheel provides a
root-controlled postinstall hook compatible with the older independent
updater. Whether scheduled or started from the dashboard, installation:

1. Installs the unprivileged public service and its restricted ingress
   permissions without enabling arbitrary public routes.
2. Validates local application/controller health.
3. Automatically records previously enabled public ingress choices and
   reconciles recognized Tailscale states: releases legacy public 8443,
   prepares private Serve 8443, removes private Serve 443, and only then
   creates public Funnel 443 if MCP or webhooks had been enabled.
4. Removes old Funnel 10000, retires the obsolete Tailscale bootstrap timer
   after successful enrollment, and refreshes the independent updater.
5. Retries interrupted or unavailable network reconciliation through an
   existing-style root-owned systemd **oneshot timer**, not a new daemon.

The old browser tab may disconnect. **Simply reconnect to the new private
:8443 address**. No migration UI, SSH, manual port change or consent wizard
is required.

Legacy webhook consumers configured at external :8443 will need their
provider callback URL updated to the same hostname/path on HTTPS :443.
DevStack cannot update external provider settings. No webhook subscription
is silently registered or changed.

## Failure model

Never enable public Funnel on a port still containing the private dashboard.
Never reset Tailscale or replace a foreign route. If the networking step
fails after a healthy release activation, keep the installed release and
known-safe private access, log a bounded error, and let the retry timer
attempt the idempotent transition. Tailscale status alone does not prove
ChatGPT cloud reachability; off-tailnet TLS/MCP and signed webhook delivery
still require real-world acceptance tests.
