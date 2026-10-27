# First-time browser activation (recommended)

Installation itself is automatic. First-time Tailscale **authorization**
requires one browser action; no secret file, auth key, or SSH session is
needed. We call this **browser activation**, not fully unattended
authentication.

## Flow

1. Before creating a VM, sign in to your own Tailscale account. For additional
   assurance, enable **device approval** for the tailnet.
2. Add the following **non-secret** cloud-init user-data command to a fresh
   Ubuntu 24.04 server:

   ```bash
   curl -fsSL https://raw.githubusercontent.com/rafaelpivato/devstack/main/scripts/install.sh \
     | sudo env DEVSTACK_TAILSCALE=1 bash
   ```

3. Retrieve the Tailscale activation URL from startup output via the trusted
   cloud-provider console. Some providers do not surface all cloud-init logs;
   in that case use their serial console/recovery access.
4. Open the URL on your iPhone. Sign in with **your** Tailscale account.
   Check the new device in Tailscale Machines; approve it if your tailnet
   requires device approval.
5. A local `systemd` timer notices the connected device and configures
   **private HTTPS** Serve. If your tailnet requires enabling HTTPS
   certificates or first-use consent, complete that in the Tailscale admin
   console. The timer retries configuration without re-enrolling the device.
6. Install the Tailscale app on your phone, join the same tailnet, and open
   the machine's `https://<device>.<tailnet>.ts.net:8443/` address from Machines.

Devstack itself stays on `127.0.0.1:8765`. Do not expose that port publicly
or make it a Funnel target. Funnel is a separately opt-in public listener\non HTTPS 443 for explicit integration paths only. Limit tailnet access to yourself.

## First-use security

The first successful registration is pinned on disk in
`/var/lib/devstack/tailscale-identity.json`, and further automatic
re-enrollment is refused. This detects later tailnet changes but cannot
guarantee a clean VM wasn't previously tampered with. Your cloud-provider
account and first-boot output form the trusted enrollment channel.
For independent recovery, use the host console, `systemctl`, and
`journalctl` rather than relying on the dashboard.

## Advanced: credential-based provisioning

A one-off key in a root-owned, mode-0600 secret file may be used for CI or
repeatable fleet provisioning, but obtaining and delivering such a key is
**extra provisioning work**. It is not the preferred first-time onboarding.

After securely providing a key file, set
`DEVSTACK_TAILSCALE=1` and `TAILSCALE_AUTH_KEY_FILE` for the installer.
Never embed secrets directly in cloud-init user data or source control.

To perform an interactive local setup instead, install normally (no
`DEVSTACK_TAILSCALE=1`) and run:

```bash
sudo /usr/local/lib/devstack/enable-tailscale.sh
```
