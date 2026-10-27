# ADR-0010: two HTTP-facing DevStack applications

Status: Proposed, reviewed under #102 / PR #103. Baseline v0.7.2.

## Decision

DevStack infrastructure runs on Ubuntu under systemd. The internal controller
has Docker administration authority; managed user projects run in Docker.

Private ingress consists of Tailscale Serve :8443 -> loopback Gunicorn
`devstack-web-v2:8765`. Public ingress consists of Tailscale Funnel :443
-> loopback ASGI `devstack-public:8768`. The latter mounts the MCP SDK's
HTTP transport alongside ordinary, strictly bounded webhook POST endpoints.
No general-purpose proxy or per-integration daemon/container is installed.
All public handlers are opt-in and default deny.

Public webhook traffic is parsed only by the unprivileged public application.
It asks the controller through a distinct, permission-restricted Unix
socket for project/generation admission and an internal service destination.
It sends the HTTP body **directly** to the Docker bridge target and releases
its in-flight lease. Neither MCP nor controller IPC transports webhook bodies.
The controller remains the single Docker orchestrator.

## Ownership and lifecycle

A root-owned, nonresident Python network reconciler owns the desired-state
Tailscale Serve/Funnel topology. It is called by release postinstall and a
systemd retry timer, not by an interactive migration wizard. An allowlisted
state machine safely moves legacy Serve private 443 and public Funnel
8443/10000 to private 8443 and public 443. Network errors never silently
expose private 443 nor force healthy app code to roll back into an
incompatible networking layout.

The release-owned installer hook works even when v0.7.2's older independent
updater performs the upgrade. After verification, it also refreshes the
standalone updater for subsequent upgrades.

## Rejected alternatives

Caddy/Nginx, custom Python reverse proxy, standalone gateway container,
per-integration public daemons, generic user-pluggable proxy framework,
MCP as a webhook transport, webhook HTTP parsing with Docker controller
privileges, and operator-driven one-time migration controls.

## Acceptance

Unit, frontend, wheel reproducibility, Linux systemd, fresh installer,
legacy-updater, guarded route cutover, webhook lease and direct-delivery tests.
External HTTPS/Funnel reachability and real provider callbacks require a
host/network acceptance test.
