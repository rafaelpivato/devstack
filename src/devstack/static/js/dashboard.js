import {getJSON, postAction} from "./api.js";
import {startPolling} from "./polling.js";

const byId = (id) => document.getElementById(id);

function replaceRows(container, rows, render, fallback) {
  if (!container) return;
  const fragment = document.createDocumentFragment();
  if (rows.length) {
    for (const row of rows) fragment.appendChild(render(row));
  } else {
    const note = document.createElement("p");
    note.className = "status";
    note.textContent = fallback;
    fragment.appendChild(note);
  }
  container.replaceChildren(fragment);
}
function el(tag, content, className) {
  const node = document.createElement(tag);
  node.textContent = content;
  if (className) node.className = className;
  return node;
}
function link(text, url) {
  const node = document.createElement("a");
  node.href = url;
  node.textContent = text;
  return node;
}
function line(title, subtitle, url) {
  const wrapper = el("div", "", "dashboard-item");
  wrapper.appendChild(el("strong", title));
  if (subtitle) wrapper.appendChild(el("small", subtitle));
  if (url) wrapper.appendChild(link("Open ↗", url));
  return wrapper;
}
function render(payload) {
  const phase = payload.runtime?.phase || "error";
  const status = byId("runtime-phase");
  if (status) {
    status.textContent = phase;
    status.dataset.phase = phase;
  }
  const message = byId("runtime-message");
  if (message) message.textContent = payload.runtime?.message || "";
  for (const form of document.querySelectorAll("[data-runtime-action]")) {
    const action = form.dataset.runtimeAction;
    form.hidden = !(action === "activate" ? phase === "inactive"
      : phase === "active" || phase === "error");
  }
  for (const control of document.querySelectorAll("[data-requires-active]")) {
    control.disabled = phase !== "active";
  }

  const project = payload.project;
  if (!project) return;
  const counters = byId("project-summary");
  if (counters) counters.textContent =
    `${payload.repositories.length} repos · ${payload.applications.length} apps · ${payload.tasks.length} tasks`;

  replaceRows(byId("dashboard-containers"), payload.containers,
    (container) => line(container.name || container.service,
      `${container.service} · ${container.state}${container.health ? " · " + container.health : ""}`, null),
    phase === "active" ? "No containers running." : "Containers stopped.");
  replaceRows(byId("dashboard-services"), payload.services,
    (service) => line(service.name, service.state, null),
    phase === "active" ? "No Compose services available." : "Services inactive.");
  replaceRows(byId("dashboard-applications"), payload.applications,
    (app) => line(app.name, `${app.service} · ${app.state}`, null),
    "No applications configured.");
  replaceRows(byId("dashboard-ports"), payload.ports,
    (port) => line(`${port.service} · :${port.host_port}`,
      `Container :${port.container_port} · private Tailscale TCP`, port.url),
    phase === "active" ? "No private ports published on the Tailscale IP." : "No ports while inactive.");
  replaceRows(byId("dashboard-tasks"), payload.tasks,
    (task) => line(task.name, task.image || task.service, null),
    "No tasks saved.");
  replaceRows(byId("dashboard-webhooks"), payload.webhooks,
    (hook) => line(hook.name, `POST /hooks/${hook.name} · ${hook.enabled ? "active" : "inactive"}`, null),
    "No webhook routes configured.");
  replaceRows(byId("dashboard-jobs"), payload.recent_jobs.slice(0, 5),
    (job) => line(job.service || job.mode || "Task", job.state || "unknown", null),
    "No recent tasks.");
  const compose = byId("dashboard-compose-job");
  if (compose) compose.textContent = payload.compose_job
    ? `${payload.compose_job.action} · ${payload.compose_job.state}` : "No Compose jobs";
  const error = byId("dashboard-errors");
  if (error) {
    const sections = Object.keys(payload.errors || {});
    error.textContent = sections.length ? `Unavailable sections: ${sections.join(", ")}` : "";
  }
}

async function refresh() {
  try {
    const payload = await getJSON("/api/v1/dashboard");
    render(payload);
    const connection = byId("dashboard-connection");
    if (connection) connection.textContent = "Connected";
  } catch (error) {
    const connection = byId("dashboard-connection");
    if (connection) connection.textContent = "Connection interrupted; retrying…";
  }
}
const csrf = document.querySelector('meta[name="devstack-csrf"]')?.content || "";
for (const form of document.querySelectorAll("[data-runtime-action]")) {
  form.addEventListener("submit", async (event) => {
    if (!csrf) return; // HTML form fallback preserves CSRF.
    event.preventDefault();
    const action = form.dataset.runtimeAction;
    const button = form.querySelector('button[type="submit"]');
    if (button) button.disabled = true;
    try {
      await postAction(`/api/v1/runtime/${action}`, csrf);
      await refresh();
    } catch (error) {
      const errorNode = byId("dashboard-errors");
      if (errorNode) errorNode.textContent = error.message || "Runtime action failed";
    } finally {
      if (button) button.disabled = false;
    }
  });
}
startPolling(refresh, 4000);
