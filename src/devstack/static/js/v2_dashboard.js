// Presentation only. The controller is authoritative and page selection
// never changes the active project or starts workloads.
const phase = document.getElementById("runtime-phase");
if (phase) {
  async function refreshRuntime() {
    if (document.hidden) return;
    try {
      const response = await fetch("/api/v2/runtime", {
        credentials: "same-origin", cache: "no-store",
        headers: {Accept: "application/json"},
      });
      if (!response.ok) return;
      const current = await response.json();
      const text = phase.querySelector("strong");
      if (text && (text.textContent !== current.phase ||
          (phase.dataset.activeProject || "") !== (current.project_id || ""))) {
        // Re-render actions using the server's current state.
        window.location.reload();
      }
    } catch (_) { /* An unavailable controller is not an inactive runtime. */ }
  }
  document.addEventListener("visibilitychange", refreshRuntime);
}
const job = document.querySelector("[data-job-id]");
if (job) {
  async function updateJob() {
    if (document.hidden) return;
    const id = encodeURIComponent(job.dataset.jobId);
    const project = encodeURIComponent(job.dataset.jobProject);
    try {
      const response = await fetch(`/api/v2/projects/${project}/jobs/${id}`, {
        credentials: "same-origin", cache: "no-store",
        headers: {Accept: "application/json"},
      });
      if (!response.ok) return;
      const status = (await response.json()).status;
      const node = document.getElementById("job-status");
      if (node) node.textContent = status;
    } catch (_) { /* Keep the last confirmed status. */ }
  }
  document.addEventListener("visibilitychange", updateJob);
}
