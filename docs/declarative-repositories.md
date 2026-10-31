# Declarative GitHub repository import

An umbrella repository can declare additional GitHub source repositories
without Git submodules, monorepos, or one-by-one onboarding.

The umbrella repository needs `.devstack/project.toml` (applications).
`.devstack/compose.yaml` and `.devstack/tasks.toml` remain optional.
Add a `.devstack/repositories.toml` file:

```toml
[[repositories]]
name = "beacon-django"
url = "git@github.com:YOUR_ORG/beacon-django.git"
branch = "main"

[[repositories]]
name = "beacon-react"
url = "git@github.com:YOUR_ORG/beacon-react.git"

[[repositories]]
name = "conduit-elixir"
url = "git@github.com:YOUR_ORG/conduit-elixir.git"
```

These URLs are examples, not verified repository names. Use real GitHub
locations and branches. The importing umbrella repository is implicit:
do not repeat it. Omit `branch` to use the remote default.

In the private DevStack Web dashboard, choose **Import from GitHub**,
enter the umbrella repository URL, and monitor the durable import status.
DevStack stages the source checkout and all declared checkouts in one
unpublished project workspace. Only after every clone succeeds are the
repositories registered together with the source and the workspace published.
A failed clone produces a failed import without exposing a partial project.
Private sources use the existing controller-managed GitHub SSH identity.
Importing does not activate Docker or applications.

Validation: at most 24 additional repositories, distinct names and GitHub
source URLs, validated HTTPS/SSH GitHub repository locations (not tree URLs),
and optional conservative branch names. No tokens, arbitrary shell commands
or host checkout paths are accepted. Existing projects without this optional
file preserve their original import behavior.

**Current limitation:** this is declarative **initial** import only. Editing
`repositories.toml` later does not clone, overwrite or delete checkouts.
Add new repositories through the existing Web **Add repository** workflow until
a separate reviewed, durable reconciliation workflow exists. Removing a
manifest entry must never implicitly remove a repository, volume or data.

The existing repository status, fast-forward synchronization, branch preview,
and repository-to-service reactions work on all successfully imported repos.
PostgreSQL, Redis and other backing services belong in Compose, not the
repository manifest.
