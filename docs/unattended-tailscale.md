# Unattended Tailscale provisioning

Devstack runs on Ubuntu and listens only on localhost. Tailscale Serve
provides optional **private, tailnet-only HTTPS** access. Do not expose
port 8765 to the public Internet or enable Tailscale Funnel.

## Prepare a one-time authentication key

1. Generate a **one-off, preferably pre-authorized** device auth key in
   the [Tailscale admin console](https://login.tailscale.com/admin/settings/keys).
   Use the narrowest permissions and expiration practical.
2. Securely deliver it to your VM *before* invoking the Devstack installer.
   The auth-key file must be a nonempty, regular, root-owned file with mode
   0600 or 0400. For example: `/run/secrets/devstack-tailscale-key`.
3. **Never put the key itself in cloud-init user-data**, shell history,
   git repositories, or application logs. Each cloud provider has its own
   secret-delivery mechanism; Devstack deliberately does not depend on one.

## Call from cloud-init / startup scripts

After the key file exists, run the normal installer with **nonsecret**
environment variables:

```bash
curl -fsSL https://raw.githubusercontent.com/rafaelpivato/devstack/main/scripts/install.sh \
  | sudo env DEVSTACK_TAILSCALE=1 \
      TAILSCALE_AUTH_KEY_FILE=/run/secrets/devstack-tailscale-key bash
```

The installer installs the Tailscale service (if needed), authenticates the
host without a browser prompt using `tailscale up --auth-key=file:<path>`,
then configures **private** HTTPS Serve. An already-connected host doesn't
need reauthentication, and existing incompatible Serve/Funnel settings
are preserved rather than overwritten.

The local auth-key file can be removed after successful registration if your
provisioning system does not handle removal itself. Never log its contents.

### Interactive fallback

Omit `DEVSTACK_TAILSCALE=1` during installation. When you can use an
interactive terminal, execute:

```bash
sudo /usr/local/lib/devstack/enable-tailscale.sh
```

Complete sign-in from your browser. The iPhone or Android device must also
be connected to the same tailnet through its Tailscale app.

## Security boundaries

- Tailnet grants/ACLs must limit access to the administrative dashboard;
  Devstack does not currently have its own login system.
- Key-file security depends on your cloud provisioning implementation.
- The VM still requires private HTTPS certificate support/MagicDNS configured
  in your tailnet.
- This approach remains provider-neutral *after the key is provisioned*.
- The install command above is for code on **main after merging this PR**;
  for stricter reproducibility, pin the installer to a reviewed commit.
