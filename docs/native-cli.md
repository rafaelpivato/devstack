# Native DevStack SSH command (`dev`) — v0.5.3

The `dev` command is a **local unprivileged client** of the same controller
Unix socket used by the web interface. The CLI has no Docker socket access,
database access or host shell bridge. v0.5.3 adds project/runtime lifecycle, service inspection, Compose and saved
tasks, background jobs, and safe GitHub/repository workflows.

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

**I4: durable transition receipts.** By default, the CLI submits
the controller-owned transition and polls its durable receipt until it
finishes. No Docker operation is owned by an SSH or browser connection.
Use the following optional commands:

```shell
dev project activate <uuid> --detach
dev project switch <uuid> --request-key <hex32>
dev project deactivate --detach
dev runtime reconcile --detach
dev runtime operations
dev runtime operation <operation-uuid>
dev --json runtime operations
```

Each mutating CLI command prints an idempotency request key to **stderr before
submission**, even in JSON mode. Retain this key when connection reliability
is uncertain. If SSH disconnects after submission but before the reply,
reissue the **identical operation and arguments** with
`--request-key <hex32>`. The controller returns the *original receipt*,
including completed outcomes, without starting Docker a second time.
The `--detach` option returns the receipt immediately; without it,
`dev` polls by operation ID until it reaches a terminal state.
`dev runtime operations` displays up to 20 recent receipts.
`dev runtime operation <id>` gives the exact current status. Status values
are `queued`, `running`, `succeeded`, `failed` and `interrupted`.

The sidecar receipt journal is private to the privileged controller and
survives daemon restarts. The previous release's registry schema is unchanged,
so the published v0.5.2 rollback path can still open the old database.
After a daemon crash, pending receipts are explicitly marked
`interrupted`: **they are never automatically replayed**. The existing
coordinator performs fail-closed startup reconciliation and removes old
runtime ownership; an interrupted receipt is intentionally not interpreted as
success or failure. Inspect `dev status` and reconcile where appropriate.
No retry may bypass the coordinator's generation fencing or global lock.

Legacy synchronous web operations use the same coordinator; a queued CLI
transition snapshots the exact runtime state, and must fail rather than
execute later if a web action changes that state first. The user-facing
interface and the CLI never gain direct Docker or registry access.


## I5: services, Compose, and saved tasks

All I5 operations require an **explicit project UUID** supplied with
`--project UUID`. The CLI never silently targets whichever project happens
to be active. Service reads for an inactive project return an empty service
list; logs require that exact project to be active, and the controller
verifies container ownership and activation generation.

```shell
dev service list --project <uuid>
dev service logs <64-character-container-id> --project <uuid> --tail 150
dev --json service list --project <uuid>

dev compose show --project <uuid>
dev compose build --project <uuid>
dev compose restart --project <uuid>

dev task list --project <uuid>
dev task run <saved-task-name> --project <uuid>
```

`compose show` reads the revisioned managed YAML through the controller. This
is read-only; no CLI edit or arbitrary Compose command is exposed.
`compose build` and `compose restart` are the only allowed maintenance
actions. They run as **controller-owned jobs**, not foreground CLI processes.
Likewise, `task run` executes only a preconfigured saved task; it cannot
supply arbitrary host commands or raw Docker flags.

The CLI prints the controller-assigned **job UUID** on acceptance. The work
continues if the SSH session exits. Inspect jobs, follow logs and cancel
independent jobs using the I6 commands documented below or the web dashboard.

`service logs` is a **bounded snapshot** of the last 1–200 log lines
(default 150), with bounded bytes and time enforced by the controller.
Raw log lines and Compose YAML are printed unchanged, even if they exceed
the terminal width. Structured service/task/job summaries wrap cleanly on
40-column terminals. Use `--json` to preserve metadata such as log
truncation flags, Compose revision, and job UUID. There is no interactive
container shell, arbitrary command runner, or unrestricted Compose API.

## I6: background jobs, log follow and targeted cancellation

Jobs run under the privileged controller and are **not tied to the life of
the SSH client**. Every job command requires `--project UUID`, including
history and log reads. The target project is never inferred from the active
project, and all job/container ownership checks remain in the controller.

```shell
dev job list --project <project-uuid>
dev job show <job-uuid> --project <project-uuid>
dev job logs <job-uuid> --project <project-uuid>
dev job logs <job-uuid> --project <project-uuid> --offset 16384
dev job logs <job-uuid> --project <project-uuid> --follow
dev job cancel <job-uuid> --project <project-uuid>

# Disposable Compose service command (not an interactive shell)
dev job run web --project <project-uuid> -- python -m unittest

# Standalone image command with an explicit network policy
dev job image python:3.12-alpine --project <project-uuid> \
  --network none -- python -V
dev job image redis:7-alpine --project <project-uuid> \
  --network project -- redis-cli ping
```

Use a literal `--` to separate **ad-hoc container argv** from CLI options.
All DevStack options, including `--project` and `--network`, go **before**
that separator; everything after it is passed as a bounded array to the
existing controller-side task-spec validator (maximum 64 argv strings and
4 KiB total). There is no host shell, interactive stdin/TTY, arbitrary
Docker CLI flag support or raw host command runner.

`job list` shows up to 40 recent controller jobs for one project.
`job show` reports a job's status, cancellation policy, and exit code if
finished. `job logs` reads one controller-bounded 16 KiB page, with a byte
offset from 0 through 2 MiB. The response includes `next_offset` and
`complete` in JSON for programmatic pagination. `--follow` polls successive
pages, emits only new text and stops after the controller reports completion.
Its JSON mode uses **newline-delimited JSON (NDJSON)**, one object per emitted
chunk/final completion notice. The plain log text is not reflowed or altered.

Ctrl-C or SSH disconnection **stops following only**; it does not terminate
or cancel the job. `job cancel` is a separate, explicit command and is only
permitted for the controller's `independent` cancellation policy. A saved
task that executes inside a shared service and Compose build/restart are not
independently cancellable. Independent cancellation targets the named job
container, verifies termination, and keeps other jobs and services running.
The existing global coordinator/activation generation fences stale work.
No log file, database or Docker socket is read directly by the CLI.

## I7: GitHub project imports and safe repository updates

The CLI uses **only the existing controller IPC** for GitHub imports, Git
status, clones and branch changes. It never runs local Git, reads credentials,
uses a project checkout directly, or changes the globally active runtime.

```shell
# Public HTTPS GitHub or authenticated SSH source URL:
dev project import https://github.com/owner/repo --slug demo --name "Demo"
dev project import-status <operation-uuid>

# Registered repo discovery and observed HEAD status:
dev repo list --project <project-uuid>
dev repo status <repo-uuid> --project <project-uuid>
dev repo update <repo-uuid> --project <project-uuid> --expected-head <40-char-sha>

# Durable background clone/bulk update with explicit request keys:
dev repo add git@github.com:owner/extra.git \
  --name extra --project <project-uuid> --branch main
dev repo add-status <operation-uuid> --project <project-uuid>
dev repo update-all --project <project-uuid>
dev repo update-status <operation-uuid> --project <project-uuid>

# Branches require a separate immutable review token:
dev repo branch-preview feature/login --project <project-uuid>
dev repo branch-apply feature/login --project <project-uuid> \
  --revision <64-char-preview-revision>
```

`dev repo branch-preview` defaults to examining **all repositories** of the
explicit project. Add `--repository <repo-uuid>` to preview or apply one.
Review the returned `ready` flag, per-repository `action`, `target_branch`,
and `fallback` **before** applying. Never guess the 64-character revision:
the controller rechecks that exact preview revision and refuses stale/dirty
changes. Review and apply require the runtime to be fully inactive.

`dev repo update` requires an **observed HEAD commit hash** from
`dev repo status`. The controller fetches and fast-forwards only when the
checkout is clean, on an upstream-tracking branch, and has not changed since
inspection. Bulk updates similarly require a deactivated runtime.

Project import, repository add and bulk updates return **durable controller
operation receipts**. By default they submit and return quickly. Use
`--wait` on the original mutation to poll the operation until terminal, or
the corresponding `...-status` command to recover a prior receipt. Mutation
commands print a 32-character idempotency request key to stderr **before**
submission. Preserve it across SSH disconnects and resubmit **identical
arguments** with `--request-key <hex32>` to retrieve the existing operation
without repeating side effects. Ctrl-C stops waiting only. Terminal states
may include `succeeded`, `failed`, `interrupted`, `partial`, `blocked` or
`recovery`; non-success terminal receipts yield nonzero status with
`--wait`. Follow up on partial or interrupted actions through the controller
or web UI; never assume implicit rollback.

The entire workflow requires trusted `devstackipc` membership and has the
same controller validation/locking as the web UI. JSON mode outputs the
original typed controller response (including revision, error_code and
per-repository results) without inventing extra fields. Human-readable text
wraps for 40-column terminals. SSH credentials for private repositories
must be configured separately through the existing secure GitHub SSH setup.

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
- JSON mode writes one structured object to stdout and errors to stderr,
  except `job logs --follow --json`, which intentionally uses NDJSON chunks.
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
