# ADR-0009: MCP connectivity-first gateway

- **Status:** Proposed — owner review required
- **Baseline:** v0.6.8; supersedes no accepted ADR
- **Scope:** Real remote MCP discovery, DevStack health via IPC, harmless write.

## Decision

Ship an opt-in, loopback Streamable HTTP MCP gateway on port 8768 using the
maintained Python MCP SDK with an explicit allowed public hostname. Tool names
are stable and usable later without breaking clients:

- `devstack_status`: read version of the installed gateway wheel and
  controller health via the existing `127.0.0.1:8765/healthz` Web route, which
  calls the controller's `health` operation over its existing admin Unix IPC.
  Only `connected`, `unavailable`, or `recovery_required` escapes the probe.
  Include the last transient diagnostic ping for roundtrip validation.
- `devstack_ping(value)`: harmless write only to bounded, process-local memory,
  returning `pong`. No project or privileged controller mutation.

The standalone MCP process is run under an account with no Docker, controller
IPC socket, or project state access. The web health probe is the **only**
controller interaction for this milestone, and uses loopback HTTP with
short timeout, no redirects, fixed destination and bounded response.

No new controller, socket, database, authorization schema or privileged
operations are necessary. Manual, separate Tailscale Funnel HTTPS port 10000
exposes only the gateway, not the dashboard. No authentication is required
for this deliberately narrow public test.

## Deliberate deferrals

Actual project data, logs, Git, service actions, durable writes, control socket
access, authenticated user identity, per-project permissions and public plugin
directory publication. Never extend this unauthenticated deployment to
privileged tools without a new design review and suitable authentication.
Version is the installed gateway wheel version, not a guarantee of identical
versions across independently managed processes.

## Acceptance

Automated tests use a **real MCP client, real HTTP Web health endpoint, and
real Unix controller IPC** with a fake health-only controller, including
controller disconnect and bounded ping/readback. Confirm tool discovery,
version, hostname validation and zero project or credential exposure.
Manual acceptance additionally requires cloud ChatGPT connectivity (not just
local Inspector), including mobile availability if supported. ChatGPT account
capability and port 10000 access remain empirical. No merge or release without
owner approval.
