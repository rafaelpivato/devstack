# DevStack HTTPS ingress (ADR-0010)

The intended topology has two listeners on the *existing Tailscale daemon*:

| Audience | HTTPS address | Loopback backend |
| --- | --- | --- |
| Private dashboard | `https://<node>.ts.net:8443/` | `127.0.0.1:8765` |
| Public MCP (opt-in) | `https://<node>.ts.net/mcp` | `127.0.0.1:8768/mcp` |
| Public webhooks (opt-in) | `https://<node>.ts.net/hooks/<name>` | `127.0.0.1:8767/hooks/<name>` |

Public Funnel HTTPS 443 is **default-deny by URL path**. There is no
public `/` handler or dashboard forwarding route. Public and private are
**different ports**; never infer network trust from Origin or a proxied
socket's peer IP. No Caddy, Nginx or extra Docker service is required.

## Migrating a live v0.7.2 installation

In the *private dashboard*, visit **Administration → ChatGPT / MCP**.
The current v0.7.2 layout uses private Serve 443, optional webhook Funnel
8443 and optional MCP Funnel 10000. Keep a copy of the future private
`https://<node>.ts.net:8443/` address before starting the cutover.

If public webhooks are already enabled on **8443**, the migration refuses
to proceed until you explicitly acknowledge and disable the legacy webhook
listener in the same private Web screen. Existing external callback URLs must be updated for
443 after the cutover; DevStack cannot silently change Twilio/GitHub URLs.
Do not migrate while that old public listener remains active.

Choose **Migrate HTTPS ports**. The root broker checks the pinned node
identity and exact legacy configuration, installs private Serve 8443
and verifies it, removes the old private 443 handler, and *only then*
installs public Funnel 443 /mcp (if old MCP was enabled) and disables
the old MCP 10000 route. The process is idempotent across recognized
intermediate configurations. It never resets the Tailscale configuration
or replaces unrecognized routes.

The original dashboard browser tab on :443 disconnects during the
cutover. Navigate to the new **private** URL with :8443. The MCP page
will show the standard-port public URL once it is enabled. It also provides
opt-in enable/disable controls for public webhook ingress on 443, through
the existing wheel-owned root broker. No outdated standalone host script is
needed after an upgrade.

## Safety and verification

MCP initially exposes unauthenticated bounded status and ephemeral ping
only—no project, host or controller execution authority. Webhook payload
validation and provider-specific signature verification remain required.
Future public routes need an explicit opt-in and route-level security review.

Tailscale `--set-path` removes the mount prefix when proxying. Our public
target URLs restore `/mcp` and `/hooks/` for their respective local
servers. Disabling one route must not disable the other.

After migration, test Tailscale private access and *independent* public
HTTPS (off-tailnet) with MCP `initialize` and `tools/list`, not merely
the status of Funnel configuration. A prior suspected Tailscale 1.104.2
Funnel regression is tracked in issue #101; a port change is not proof
that bug is fixed.

See https://tailscale.com/docs/reference/tailscale-cli/funnel
and https://tailscale.com/docs/reference/tailscale-cli/serve.
