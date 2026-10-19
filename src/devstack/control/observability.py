"""Read-only, UUID- and generation-scoped Compose observability.

All Docker inspection is performed by the privileged controller. Browser
selection is not activation; an inactive project never initiates Docker reads.
"""
from devstack.domain.models import project_id
from .coordinator import ControlError


class ContainerObservability:
    def __init__(self, coordinator, backend):
        self.coordinator = coordinator
        self.backend = backend

    def _active(self, identifier: str, *, strict: bool):
        project_id(identifier)
        self.coordinator.registry.get(identifier)
        state = self.coordinator.status()
        if (state["phase"] == "active" and state["project_id"] == identifier
                and state["generation"]):
            return state["generation"]
        if strict:
            raise ControlError("Container logs require the active project")
        return None

    def services(self, identifier: str) -> dict:
        with self.coordinator.locked():
            generation = self._active(identifier, strict=False)
            if generation is None:
                return {"project_id": identifier, "active": False, "services": []}
            return {"project_id": identifier, "active": True,
                    **self.backend.service_snapshot(identifier, generation)}

    def logs(self, identifier: str, container_id: str, tail: int) -> dict:
        with self.coordinator.locked():
            generation = self._active(identifier, strict=True)
            return self.backend.service_logs(identifier, generation, container_id, tail)
