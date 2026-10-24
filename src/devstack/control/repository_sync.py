"""Controller-owned, opt-in Git polling with durable bounded receipts.

No web/CLI Git execution, Docker actions, resets or source template imports.
All worktree writes take the global coordinator lock and match runtime owner.
"""
import re
import time

from devstack.domain.models import project_id
from devstack.projects.repositories import RepositoryGitError, RepositoryGitInvalid

POLICIES = frozenset(("manual", "check", "update"))
POLL_INTERVAL_NS = 300 * 1_000_000_000
MAX_PER_TICK = 6
MAX_EVENTS = 10
_HEAD = re.compile(r"[a-f0-9]{40,64}\Z")


class RepositorySyncInvalid(ValueError):
    pass


class RepositorySyncSupervisor:
    def __init__(self, database, registry, coordinator, repositories):
        self.database = database
        self.registry = registry
        self.coordinator = coordinator
        self.repositories = repositories

    def _verify(self, identifier, repository_id):
        project_id(identifier)
        project_id(repository_id)
        self.repositories._checkout(identifier, repository_id)

    def state(self, identifier, repository_id):
        self._verify(identifier, repository_id)
        with self.database.transaction() as con:
            row = con.execute(
                "SELECT policy,status,error_code,last_checked_ns,last_updated_ns,"
                "ahead,behind,previous_head,head "
                "FROM repository_sync WHERE project_id=? AND repository_id=?",
                (identifier, repository_id),
            ).fetchone()
            events = con.execute(
                "SELECT outcome,reason,previous_head,head,at_ns "
                "FROM repository_sync_events WHERE project_id=? AND repository_id=? "
                "ORDER BY id DESC LIMIT ?",
                (identifier, repository_id, MAX_EVENTS),
            ).fetchall()
        value = dict(row) if row else {
            "policy": "manual", "status": "idle", "error_code": None,
            "last_checked_ns": None, "last_updated_ns": None,
            "ahead": None, "behind": None, "previous_head": None, "head": None,
        }
        value["events"] = [dict(event) for event in events]
        return value

    def set_policy(self, identifier, repository_id, policy):
        if not isinstance(policy, str) or policy not in POLICIES:
            raise RepositorySyncInvalid("Unsupported synchronization policy")
        with self.coordinator.locked():
            self._verify(identifier, repository_id)
            with self.database.transaction(write=True) as con:
                con.execute(
                    "INSERT OR IGNORE INTO repository_sync "
                    "(project_id,repository_id,policy,status) VALUES (?,?,'manual','idle')",
                    (identifier, repository_id),
                )
                con.execute(
                    "UPDATE repository_sync SET policy=? "
                    "WHERE project_id=? AND repository_id=?",
                    (policy, identifier, repository_id),
                )
        return self.state(identifier, repository_id)

    @staticmethod
    def _reason(exc):
        message = str(exc).lower()
        if "diverged" in message:
            return "diverged"
        if "clean" in message or "dirty" in message:
            return "dirty_worktree"
        if "track origin" in message or "upstream" in message:
            return "no_origin_upstream"
        if "changed" in message or "stale" in message:
            return "changed_during_update"
        if "matching active" in message:
            return "runtime_conflict"
        if "timed out" in message:
            return "git_timeout"
        return "git_error"

    def _begin(self, identifier, repository_id, before):
        with self.database.transaction(write=True) as con:
            con.execute(
                "INSERT OR IGNORE INTO repository_sync "
                "(project_id,repository_id,policy,status) "
                "VALUES (?,?,'manual','idle')",
                (identifier, repository_id),
            )
            con.execute(
                "UPDATE repository_sync SET status='running',error_code=NULL,"
                "previous_head=?,head=? WHERE project_id=? AND repository_id=?",
                (before["head"], before["head"], identifier, repository_id),
            )

    def _finish(self, identifier, repository_id, before, after, outcome, reason):
        now = time.time_ns()
        previous = before["head"]
        head = after["head"] if after else previous
        checked = outcome in ("checked", "updated", "unchanged", "blocked", "failed")
        with self.database.transaction(write=True) as con:
            con.execute(
                "UPDATE repository_sync SET status=?,error_code=?,"
                "last_checked_ns=CASE WHEN ? THEN ? ELSE last_checked_ns END,"
                "last_updated_ns=CASE WHEN ? THEN ? ELSE last_updated_ns END,"
                "ahead=?,behind=?,previous_head=?,head=? "
                "WHERE project_id=? AND repository_id=?",
                (outcome, reason, int(checked), now, int(outcome == "updated"), now,
                 after["ahead"] if after else before["ahead"],
                 after["behind"] if after else before["behind"],
                 previous, head, identifier, repository_id),
            )
            con.execute(
                "INSERT INTO repository_sync_events "
                "(project_id,repository_id,outcome,reason,previous_head,head,at_ns) "
                "VALUES (?,?,?,?,?,?,?)",
                (identifier, repository_id, outcome, reason, previous, head, now),
            )
            con.execute(
                "DELETE FROM repository_sync_events WHERE project_id=? AND repository_id=? "
                "AND id NOT IN (SELECT id FROM repository_sync_events "
                "WHERE project_id=? AND repository_id=? ORDER BY id DESC LIMIT ?)",
                (identifier, repository_id, identifier, repository_id, MAX_EVENTS),
            )

    def perform(self, identifier, repository_id, *, update=False,
                expected_head=None, automatic=False):
        """Check or optionally fast-forward, serialized with runtime transitions.

        Interrupted automatic work never retries without an explicit manual check.
        Manual fast-forward continues to honor the expected-HEAD precondition.
        """
        if expected_head is not None and (
            not isinstance(expected_head, str) or not _HEAD.fullmatch(expected_head)
        ):
            raise RepositoryGitInvalid("Expected a Git commit hash")
        with self.coordinator.locked():
            self._verify(identifier, repository_id)
            if automatic:
                old = self.state(identifier, repository_id)
                if old["status"] == "interrupted":
                    return {**self.repositories.status(identifier, repository_id),
                            "sync": old}
                if old["policy"] not in (("update",) if update else ("check", "update")):
                    return {**self.repositories.status(identifier, repository_id),
                            "sync": old}
            self.repositories._sync_editable(identifier)
            before = self.repositories.status(identifier, repository_id)
            if expected_head is not None and before["head"] != expected_head:
                raise RepositoryGitError("Repository changed since inspection")
            self._begin(identifier, repository_id, before)
            after = None
            try:
                if update:
                    after = self.repositories.update_locked(
                        identifier, repository_id, before["head"],
                    )
                    outcome = "updated" if after["head"] != before["head"] else "unchanged"
                    reason = None
                else:
                    _, checkout = self.repositories._checkout(identifier, repository_id)
                    self.repositories._git(checkout, "fetch", "--no-tags", "origin")
                    after = self.repositories.status(identifier, repository_id)
                    if any(after[field] != before[field]
                           for field in ("head", "branch", "dirty", "upstream")):
                        raise RepositoryGitError("Repository changed during fetch")
                    reason = (
                        "detached_head" if not after["branch"] else
                        "no_origin_upstream" if not after["upstream"] or
                        not after["upstream"].startswith("origin/") else
                        "dirty_worktree" if after["dirty"] else
                        "diverged" if after["ahead"] and after["behind"] else None
                    )
                    outcome = "blocked" if reason else "checked"
            except Exception as exc:
                self._finish(identifier, repository_id, before, after, "failed",
                             self._reason(exc))
                raise
            self._finish(identifier, repository_id, before, after, outcome, reason)
            return {**after, "sync": self.state(identifier, repository_id)}

    def recover(self):
        """Never replay an in-flight Git mutation after controller death."""
        with self.database.transaction(write=True) as con:
            rows = con.execute(
                "SELECT project_id,repository_id,previous_head,head "
                "FROM repository_sync WHERE status='running'",
            ).fetchall()
            for row in rows:
                con.execute(
                    "UPDATE repository_sync SET status='interrupted',"
                    "error_code='controller_restart' "
                    "WHERE project_id=? AND repository_id=?",
                    (row["project_id"], row["repository_id"]),
                )
                con.execute(
                    "INSERT INTO repository_sync_events "
                    "(project_id,repository_id,outcome,reason,previous_head,head,at_ns) "
                    "VALUES (?,?,'interrupted','controller_restart',?,?,?)",
                    (row["project_id"], row["repository_id"],
                     row["previous_head"], row["head"], time.time_ns()),
                )
        return len(rows)

    def poll_once(self):
        """Bounded single-pass polling; called only by existing controller daemon."""
        now = time.time_ns()
        with self.database.transaction() as con:
            due = con.execute(
                "SELECT project_id,repository_id,policy FROM repository_sync "
                "WHERE policy IN ('check','update') AND status!='interrupted' "
                "AND (last_checked_ns IS NULL OR last_checked_ns<=?) "
                "ORDER BY COALESCE(last_checked_ns,0),repository_id LIMIT ?",
                (now - POLL_INTERVAL_NS, MAX_PER_TICK),
            ).fetchall()
        count = 0
        for row in due:
            try:
                # Foreign active projects are entirely unaffected by background sync.
                state = self.coordinator.status()
                if state["phase"] not in ("active", "inactive"):
                    continue
                if state["phase"] == "active" and state["project_id"] != row["project_id"]:
                    continue
                self.perform(row["project_id"], row["repository_id"],
                             update=row["policy"] == "update", automatic=True)
                count += 1
            except Exception:
                # perform persists an allowlisted failure receipt if admitted.
                # Never leak raw Git diagnostics or take down the poller.
                continue
        return count
