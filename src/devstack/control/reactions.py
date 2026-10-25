"""Issue #90: explicit repository-to-service policies and fenced Compose reactions.

Git synchronization only records an intent when HEAD advances. The existing
controller job supervisor executes allowlisted targeted Compose operations.
No GET or branch preview can run Docker mutations.
"""
import time

from devstack.domain.models import project_id
from devstack.control.jobs import JobError, JobInvalid


class ReactionInvalid(ValueError):
    pass


class ReactionSupervisor:
    def __init__(self, database, registry, coordinator, backend, jobs):
        self.database = database
        self.registry = registry
        self.coordinator = coordinator
        self.backend = backend
        self.jobs = jobs

    def _repository(self, identifier, repository_id):
        project_id(identifier)
        project_id(repository_id)
        if not any(repo.id == repository_id for repo in self.registry.repositories(identifier)):
            raise LookupError("Repository not registered for this project")

    def get(self, identifier, repository_id):
        self._repository(identifier, repository_id)
        services = self.backend.configured_services(identifier)
        with self.database.transaction() as con:
            rows = con.execute(
                "SELECT service,action FROM service_reaction_policies "
                "WHERE project_id=? AND repository_id=?",
                (identifier, repository_id),
            ).fetchall()
            events = con.execute(
                "SELECT e.service,e.action,e.head,e.status,e.reason,e.at_ns,"
                "e.job_id,j.status AS job_status FROM service_reaction_events e "
                "LEFT JOIN jobs j ON j.id=e.job_id "
                "WHERE e.project_id=? AND e.repository_id=? "
                "ORDER BY e.id DESC LIMIT 15",
                (identifier, repository_id),
            ).fetchall()
        policy = {row["service"]: row["action"] for row in rows}
        return {"services": [
            {"name": name, "action": policy.get(name, "none")} for name in services
        ], "events": [dict(row) for row in events]}

    def set_policy(self, identifier, repository_id, service, action):
        if (not isinstance(service, str) or not isinstance(action, str)
                or action not in ("none", "restart", "redeploy")):
            raise ReactionInvalid("Invalid reaction policy")
        with self.coordinator.locked():
            self._repository(identifier, repository_id)
            if service not in self.backend.configured_services(identifier):
                raise ReactionInvalid("Unknown Compose service")
            with self.database.transaction(write=True) as con:
                con.execute(
                    "DELETE FROM service_reaction_policies "
                    "WHERE project_id=? AND repository_id=? AND service=?",
                    (identifier, repository_id, service),
                )
                if action != "none":
                    con.execute(
                        "INSERT INTO service_reaction_policies "
                        "(project_id,repository_id,service,action) VALUES (?,?,?,?)",
                        (identifier, repository_id, service, action),
                    )
        return self.get(identifier, repository_id)

    def maintenance_busy_locked(self, identifier):
        """Guard Git source mutations while a service build/redeploy is pending."""
        with self.database.transaction() as con:
            return bool(
                con.execute(
                    "SELECT 1 FROM service_reaction_events e "
                    "LEFT JOIN jobs j ON j.id=e.job_id "
                    "WHERE e.project_id=? AND (e.status='pending' OR "
                    "(e.status='submitted' AND j.status IN "
                    "('queued','running','cancelling'))) LIMIT 1",
                    (identifier,),
                ).fetchone()
                or con.execute(
                    "SELECT 1 FROM jobs WHERE project_id=? AND kind='compose' "
                    "AND status IN ('queued','running','cancelling') LIMIT 1",
                    (identifier,),
                ).fetchone()
            )

    def capture(self, con, identifier, repository_id, head):
        """Called in the same SQLite transaction as a successful sync receipt.

        Never promote a read-only check, unchanged HEAD or inactive runtime to
        a Docker operation. The UNIQUE key prevents duplicate reactions.
        """
        state = self.coordinator.status()
        active = state["phase"] == "active" and state["project_id"] == identifier
        policies = con.execute(
            "SELECT service,action FROM service_reaction_policies "
            "WHERE project_id=? AND repository_id=? ORDER BY service",
            (identifier, repository_id),
        ).fetchall()
        for item in policies:
            con.execute(
                "INSERT OR IGNORE INTO service_reaction_events "
                "(project_id,repository_id,service,action,head,generation,status,reason,at_ns) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (identifier, repository_id, item["service"], item["action"],
                 head, state["generation"] if active else None,
                 "pending" if active else "skipped",
                 None if active else "inactive_project", time.time_ns()),
            )
        # Keep bounded historical receipts without deleting work that might run.
        con.execute(
            "DELETE FROM service_reaction_events WHERE project_id=? AND repository_id=? "
            "AND status NOT IN ('pending','submitted') AND id NOT IN "
            "(SELECT id FROM service_reaction_events WHERE project_id=? "
            "AND repository_id=? ORDER BY id DESC LIMIT 40)",
            (identifier, repository_id, identifier, repository_id),
        )

    def dispatch_pending(self):
        """Schedule at most one group; group same-service intents across repositories.

        A queued Compose job and every linked event are committed atomically.
        Failed or ambiguous admissions never auto-repeat indefinitely.
        """
        with self.coordinator.locked():
            with self.database.transaction() as con:
                oldest = con.execute(
                    "SELECT id,project_id,generation,service FROM service_reaction_events "
                    "WHERE status='pending' ORDER BY id LIMIT 1",
                ).fetchone()
                if oldest is None:
                    return False
                grouped = con.execute(
                    "SELECT id,action FROM service_reaction_events "
                    "WHERE status='pending' AND project_id=? "
                    "AND generation=? AND service=? ORDER BY id",
                    (oldest["project_id"], oldest["generation"], oldest["service"]),
                ).fetchall()
            ids = tuple(row["id"] for row in grouped)
            state = self.coordinator.status()
            if (state["phase"] != "active"
                    or state["project_id"] != oldest["project_id"]
                    or state["generation"] != oldest["generation"]):
                self._finish(ids, "skipped", "stale_activation")
                return False
            if self.maintenance_busy_locked(oldest["project_id"]):
                # Ignore our own pending intents; check only already-admitted jobs.
                with self.database.transaction() as con:
                    busy = con.execute(
                        "SELECT 1 FROM jobs WHERE status IN "
                        "('queued','running','cancelling') LIMIT 1",
                    ).fetchone()
                if busy:
                    return False
            action = ("redeploy" if any(row["action"] == "redeploy" for row in grouped)
                      else "restart")
            try:
                self.jobs.submit_service(
                    oldest["project_id"], oldest["service"], action,
                    reaction_ids=ids, already_locked=True,
                )
            except (JobError, JobInvalid) as exc:
                # Concurrency is retryable; invalid/stale configuration is not.
                if "admission limit" in str(exc).lower() or "maintenance" in str(exc).lower():
                    return False
                self._finish(ids, "interrupted", "admission_failed")
                return False
            except Exception:
                self._finish(ids, "interrupted", "controller_error")
                return False
            return True

    def _finish(self, ids, status, reason):
        if not ids:
            return
        with self.database.transaction(write=True) as con:
            con.execute(
                "UPDATE service_reaction_events SET status=?,reason=? "
                "WHERE status='pending' AND id IN (" +
                ",".join("?" for _ in ids) + ")",
                (status, reason, *ids),
            )

    def recover(self):
        """No unsubmitted work is automatically replayed after controller death."""
        with self.database.transaction(write=True) as con:
            con.execute(
                "UPDATE service_reaction_events SET status='interrupted',"
                "reason='controller_restart' WHERE status='pending'"
            )
