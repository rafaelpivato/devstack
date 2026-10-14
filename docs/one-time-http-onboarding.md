# One-time browser setup over HTTP (opt-in)

This is an **alternative** to the existing cloud-init-log activation flow.
It is intended for a *fresh* Ubuntu 24.04 EC2 instance when you are
provisioning from a phone.

The first-run page is **not Devstack's application dashboard**. It exposes
only a code field and a button to open the Tailscale device authorization
page. It has no shell, logs, Git access, or privileged admin operations.

## EC2 settings

- Use Ubuntu 24.04 LTS (x86_64), with a public IPv4 address and outbound
  internet access.
- Temporarily allow inbound **TCP port 80**, ideally restricted to your
  current public IP if feasible. Do not open 8765, 443, or SSH for Devstack.
- Generate a high-entropy, unique setup secret in your password manager
  (at least 24 random characters; 32+ preferred).
- Put this user-data script in EC2 → Launch instance → Advanced details.
  Replace the placeholder before launching.

```bash
#!/bin/bash
set -euo pipefail
export DEVSTACK_BOOTSTRAP_SECRET='REPLACE-WITH-A-STRONG-32-CHAR-RANDOM-VALUE'
curl -fsSL https://raw.githubusercontent.com/rafaelpivato/devstack/main/scripts/install.sh \
  | env DEVSTACK_TAILSCALE=1 bash
```

The installer saves **only SHA-256 of the setup secret** in a restricted
server-side environment file. The EC2 user-data itself retains the original
secret in AWS, so protect the IAM permissions that expose user data.
The script does **not** echo the secret or the Tailscale device URL to logs.

## First use

1. Open `http://<EC2-PUBLIC-IP>/` in Safari or Chrome on your phone.
2. Enter the random setup secret (the same one you placed in User Data).
3. The server redirects you to its specific Tailscale device login URL.
   Authorize the machine under your own Tailscale account.
4. Find your new device under Tailscale **Machines**; approve it if
   device approval is enabled. The background installer/timer configures
   tailnet-private HTTPS once Tailscale reports a connected device.
5. Install/enable the native Tailscale app on your phone, then open
   `https://<machine>.<tailnet>.ts.net` in your browser.
6. After verifying setup, remove **inbound TCP 80** from the EC2 security
   group. The installer attempts to **stop and disable** the temporary HTTP
   listener and delete its local authentication artifacts after private
   HTTPS setup succeeds, but cannot modify AWS security group rules.

### Security model and limitations

**HTTP sends the bootstrap secret without transport encryption.** Anyone
able to intercept or alter the HTTP connection can capture the secret or
redirect your browser to a malicious page. A strong code limits guessing,
but **does not protect against interception or a hostile first-use host**.
For greater assurance, use HTTPS or keep the existing cloud-provider
console-log activation workflow.

The portal has a per-source-IP rate limit and strict URL validation, and
never reveals a Tailscale authorization URL until after the secret check.
Do not reuse the code. Restrict IAM access to EC2 user data, use MFA for
your Tailscale account, and enable Tailscale device approval if possible.

The **first accepted Tailscale identity** is pinned in
`/var/lib/devstack/tailscale-identity.json`. Re-enrollment to another
tailnet is refused without host-console intervention. This is
**trust-on-first-use**, not a proof that a brand-new machine wasn't
compromised beforehand.

Port 80 will not close in AWS automatically; remove its security group
rule once you have verified private access.

## Alternative: no public onboarding page

Omit `DEVSTACK_BOOTSTRAP_SECRET` and keep
`DEVSTACK_TAILSCALE=1`. Authentication URL is displayed in first-boot
cloud logs as in the original release flow, with no public port required.
