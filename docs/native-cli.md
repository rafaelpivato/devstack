# Native DevStack SSH command (`dev`) — first milestone

The `dev` command is a **local unprivileged client** of the same controller
Unix socket used by the web interface. The CLI has no Docker socket access,
database access or host shell bridge. I3 adds read-only project discovery and
explicit project/runtime lifecycle commands; services, jobs and Git commands
are subsequent integration PRs.

## Project and runtime commands

```shell
dev status
dev project list
dev project show <canonical-project-UUIDv4>
dev project activate <canonical-project-UUIDv4>
dev project switch <canonical-project-UUIDv4>
dev project deactivate
dev runtime reconcile
dev --json project list
dev project show <canonical-project-UUIDv4> --json
```

**Reading is inert.** `dev project list` and `dev project show UUID` never
activate or select a project globally. Display slugs/names cannot substitute
for canonical immutable UUIDv4 mutation targets. There is **at most one active
runtime** shared across SSH and the dashboard.

`activate` requires an inactive runtime; `switch` can replace the active
project or activate one while globally inactive. `deactivate` stops the
**global runtime** (it never deletes any project), and `reconcile` performs an
**explicit recovery** by cleaning up owned workloads without reactivating
any project. Mutations use the controller's existing exclusive coordinator
lock and generation fencing. Any concurrent operation from the web or SSH
goes through that same path.

**I4 limitation:** Runtime transitions are still synchronous controller
operations. The CLI allows up to 420 seconds for a transition; an SSH
disconnection, Ctrl-C or timeout does **not** imply the transition failed or
was cancelled. On a disconnected or interrupted operation, the CLI reports
an **unknown outcome** and advises checking `dev status` before any retry.
Controller-owned durable transition receipts and resumption are reserved for
the follow-up I4 reliability PR.

## Operator trust and enrollment

Only a **trusted human Linux administrator** should be granted membership in
`devstackipc`. Access to this group gives broad DevStack administrative
capabilities (including container execution once the CLI supports those
commands) and is *not* read-only or project-level RBAC. On existing 0.5.2
installations this group can also read the group-readable web configuration
secret `/etc/devstack/app.env`. Do not enroll untrusted Linux users.

After the next stable release is published and installed, the root-owned
CLI installer is available in the **versioned release**:

```shell
sudo /opt/devstack/current/venv/bin/python -m devstack.cli.install --user "$USER" --refresh-updater
```

This installs the `/usr/local/bin/dev` symlink if it is not already present,
then enrolls the explicitly named operator account. The symlink follows
`/opt/devstack/current` to the selected release, including rollbacks.

**Important v0.5.2 upgrade detail:** v0.5.2 installed its root updater outside
the release directory. That older updater continues to install new application
releases, but cannot automatically run the newly introduced CLI registration
hook. Therefore, **after upgrading a pre-existing v0.5.2 host**, run the
one-time command above. The `--refresh-updater` option atomically installs
the trusted updater shipped with the selected release and preserves the
original standalone updater as `updater.py.pre-cli-backup`. This resolves
the v0.5.2 updater migration gap for subsequent releases. New clean installs
already receive the correct standalone updater from their release; the
`--refresh-updater` option may be omitted there. No host reinstall is required.

To enroll an existing SSH user automatically during a fresh installation,
an administrator may explicitly set `DEVSTACK_CLI_OPERATOR=<linux-username>`
in root-owned installer environment. The installer never guesses the admin
from `SUDO_USER`, `ubuntu` or other system defaults.

**Never install an SSH operator account into the `docker` group.** Enrollment
in `devstackipc` does not imply Docker or SQLite filesystem access, although
its controller capabilities are powerful.

Log out and reconnect over SSH so the kernel gets the updated supplementary
groups. Then verify:

```shell
dev doctor
dev status
dev --json status
```

Run `dev --help` or `dev status --help` without contacting the server.
The web UI remains usable even if the human CLI was not enrolled.

## Output and failure conventions

- Default output is compact, readable without colors or a terminal (40-column
  mobile SSH output is tested).
- Global options are `--json` and `--socket PATH`. Both are also accepted after
  the initial `status` or `doctor` subcommand.
- JSON mode writes exactly one structured object to stdout and errors to stderr.
- Exit status: `0` success; `2` bad CLI arguments; `65` invalid/rejected request;
  `69` controller unreachable or malformed reply; `75` operation conflict;
  `77` controller socket permission denied; `78` incompatible controller/response.
- No raw controller exception message, host path, environment variable or
  credential is included in CLI errors.

## Upgrade safety

The installer and new release updater refuse to overwrite any unrelated file
or symlink named `dev` in `/usr/local/bin`. A conflict must be deliberately
resolved by the host operator; it is never replaced silently. The versioned
entry point is always accessible directly as
`/opt/devstack/current/venv/bin/dev`, even if a separate tool owns the
short command name.

The new updater performs collision checks before activation and installs the
symlink only after both controller and web health checks pass. A failed
activation rolls the release pointer back and removes any newly created
symlink that would otherwise be left dangling.

Other manual validation (real reboot/crash/rollback and job scenarios) remains
tracked in [Issue #50](https://github.com/rafaelpivato/devstack/issues/50).
