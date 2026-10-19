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
      if (node) {
        const before = node.textContent;
        node.textContent = status;
        if (before !== status && ["succeeded", "failed", "cancelled"].includes(status)) {
          window.location.reload();
        }
      }
    } catch (_) { /* Keep the last confirmed status. */ }
  }
  document.addEventListener("visibilitychange", updateJob);
  const output = document.getElementById("job-output");
  let offset = 0;
  let reading = false;
  async function refreshLogs() {
    if (document.hidden || !output || reading) return;
    reading = true;
    try {
      const id = encodeURIComponent(job.dataset.jobId);
      const project = encodeURIComponent(job.dataset.jobProject);
      const response = await fetch(
        `/api/v2/projects/${project}/jobs/${id}/logs?offset=${offset}`,
        {credentials: "same-origin", cache: "no-store"}
      );
      if (!response.ok) return;
      const data = await response.json();
      output.textContent += data.text;
      offset = data.next_offset;
    } catch (_) { /* Private logs remain available when connectivity recovers. */ }
    finally { reading = false; }
  }
  window.setInterval(() => { updateJob(); refreshLogs(); }, 2500);
  refreshLogs();
}

const imported = document.querySelector("[data-import-id]");
if (imported) {
  let outstanding = false;
  async function refreshImport() {
    if (document.hidden || outstanding) return;
    const label = document.getElementById("import-status");
    if (!label || !["queued", "running"].includes(label.textContent.trim())) return;
    outstanding = true;
    try {
      const id = encodeURIComponent(imported.dataset.importId);
      const response = await fetch("/api/v2/imports/" + id, {
        credentials: "same-origin", cache: "no-store",
        headers: {Accept: "application/json"},
      });
      if (!response.ok) return;
      const state = await response.json();
      if (state.status === "succeeded" || state.status === "failed") {
        // Link/error comes from an authoritative server-rendered page,
        // not a guessed slug or speculative client-side active project.
        window.location.reload();
      } else {
        label.textContent = state.status;
      }
    } catch (_) { /* Keep the last confirmed status. */ }
    finally { outstanding = false; }
  }
  window.setInterval(refreshImport, 2500);
  document.addEventListener("visibilitychange", refreshImport);
}
