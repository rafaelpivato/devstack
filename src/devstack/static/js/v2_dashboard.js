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


const servicesPanel = document.querySelector("[data-services-project]");
if (servicesPanel && servicesPanel.dataset.servicesActive === "true") {
  const id = servicesPanel.dataset.servicesProject;
  const list = document.getElementById("services-list");
  const status = document.getElementById("services-status");
  const button = document.getElementById("refresh-services");
  let pending = false;
  async function refreshServices() {
    if (document.hidden || pending) return;
    pending = true;
    try {
      const response = await fetch(
        `/api/v2/projects/${encodeURIComponent(id)}/services`,
        {credentials: "same-origin", cache: "no-store"}
      );
      if (!response.ok) {
        status.textContent = "Service status unavailable; refresh to retry.";
        return;
      }
      const result = await response.json();
      if (!result.active) {
        status.textContent = "Project is no longer active.";
        list.replaceChildren();
        return;
      }
      list.replaceChildren();
      status.textContent = result.services.length ?
        "Current Compose service state" : "No Compose services configured.";
      for (const service of result.services) {
        const item = document.createElement("li");
        const title = document.createElement("strong");
        title.textContent = service.name + " · " + service.status;
        item.appendChild(title);
        if (!service.containers.length) {
          const note = document.createElement("small");
          note.textContent = " · no container";
          item.appendChild(note);
        }
        for (const container of service.containers) {
          const detail = document.createElement("div");
          const health = container.health ? " · health: " + container.health : "";
          detail.textContent = container.name + " · " + container.state +
            health + " · restarts: " + container.restart_count + " ";
          const link = document.createElement("a");
          link.href = `/projects/${encodeURIComponent(id)}/services/${encodeURIComponent(container.id)}/logs`;
          link.textContent = "View logs";
          detail.appendChild(link);
          item.appendChild(detail);
        }
        list.appendChild(item);
      }
    } catch (_) {
      status.textContent = "Service status unavailable; refresh to retry.";
    } finally {
      pending = false;
    }
  }
  button?.addEventListener("click", refreshServices);
  document.addEventListener("visibilitychange", refreshServices);
  window.setInterval(refreshServices, 8000);
  refreshServices();
}

const portsPanel = document.querySelector("[data-private-ports-project]");
if (portsPanel && portsPanel.dataset.privatePortsActive === "true") {
  const id = portsPanel.dataset.privatePortsProject;
  const list = document.getElementById("private-ports-list");
  const status = document.getElementById("private-ports-status");
  const button = document.getElementById("refresh-private-ports");
  let busy = false;
  async function refreshPrivatePorts() {
    if (document.hidden || busy) return;
    busy = true;
    try {
      const response = await fetch(
        `/api/v2/projects/${encodeURIComponent(id)}/private-ports`,
        {credentials: "same-origin", cache: "no-store"}
      );
      if (!response.ok) {
        status.textContent = "Private port information unavailable.";
        return;
      }
      const data = await response.json();
      list.replaceChildren();
      if (!data.active) {
        status.textContent = "This project is no longer active.";
        return;
      }
      if (!data.tailscale_available) {
        status.textContent = "Tailscale IPv4 is unavailable on the server.";
        return;
      }
      if (!data.ports.length) {
        status.textContent = "No running Compose services publish TCP ports on the Tailscale IP.";
        return;
      }
      status.textContent = "Available private TCP ports";
      for (const mapping of data.ports) {
        const row = document.createElement("li");
        const label = document.createElement("span");
        const matches = data.applications.filter(app => app.url === mapping.url
          && app.service === mapping.service);
        const appNames = matches.map(app => app.name);
        label.textContent = (appNames.length ? appNames.join(", ") + " · " : "") +
          mapping.service + ":" + mapping.container_port +
          " → " + mapping.host_port + " ";
        const link = document.createElement("a");
        link.href = mapping.url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.textContent = mapping.url;
        row.append(label, link);
        list.appendChild(row);
      }
    } catch (_) {
      status.textContent = "Private port information unavailable.";
    } finally {
      busy = false;
    }
  }
  button?.addEventListener("click", refreshPrivatePorts);
  document.addEventListener("visibilitychange", refreshPrivatePorts);
  window.setInterval(refreshPrivatePorts, 12000);
  refreshPrivatePorts();
}
