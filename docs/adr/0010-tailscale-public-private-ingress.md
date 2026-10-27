# ADR-0010: public/private Tailscale listener isolation

- **Status:** Proposed, pending issue #102 and PR #103 review.
- **Baseline:** v0.7.2; supersedes ADR-0009 ingress ports only.

## Decision

The tailnet-only dashboard lives under Serve **HTTPS 8443**.
All public integration endpoints share Funnel **HTTPS 443** by explicit
path mounts, currently `/mcp` and `/hooks/`. Unknown public paths
have no handler. Existing Python listeners remain on localhost. Tailscale
already provides the required TLS and reverse proxy, so no additional
daemon, Docker container, Caddy, Nginx or custom Python reverse proxy.

Public/private exposure is set at the external port boundary; never use
`Origin`, `Host` or an upstream-proxied peer socket address as a substitute
for that boundary.

## Migration and security

Legacy private :443 must be removed **before** public Funnel :443 is
ever enabled; first stage and validate private Serve :8443. A pre-existing
public webhook :8443 prevents cutover until disabled (and callback
consumers must be reconfigured). Validate pinned Tailscale identity and
whole known Serve/Funnel configuration, refuse unknown routes, and recover
only along allowlisted transitional states. Web UI and bounded diagnostics
control normal changes; no routine SSH.

MCP remains restricted to status and diagnostic ping without independent
authentication. Authenticated privileged tools are deliberately deferred.
Webhook handlers still verify source signatures; no wildcard public route
and no public management UI.

## Rejected

Source-IP/Origin-based routing, publicly accessible dashboard, generic
reconfig/reset of Tailscale, extra reverse proxy services, and dependence
on nonstandard public HTTPS ports.

## Acceptance

CI + real-host private and independent off-tailnet Funnel TLS/HTTP tests.
Owner approves before final squash merge/release.
