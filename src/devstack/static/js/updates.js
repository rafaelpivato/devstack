import {getJSON, postForm} from "./api.js";
import {startPolling} from "./polling.js";

const installed = document.getElementById("devstack-installed");
const latest = document.getElementById("devstack-latest");
const phase = document.getElementById("devstack-update-phase");
const status = document.getElementById("devstack-update-message");
const button = document.getElementById("devstack-update-button");
const form = document.getElementById("devstack-update-form");
let submitting = false;

async function refresh() {
  try {
    const data = await getJSON("/api/devstack/update-status");
    installed.textContent = data.installed || "Unknown";
    latest.textContent = data.latest || "Unavailable";
    phase.textContent = data.phase || "Unknown";
    button.disabled = submitting || ["queued", "updating"].includes(data.phase)
      || !data.manual_available;
    const messages = {
      failed: "Last update failed; inspect updater logs.",
      updating: "Updating DevStack; dashboard may temporarily disconnect.",
      queued: "Update queued.",
    };
    status.textContent = data.setup_error || messages[data.phase]
      || (data.latest && data.latest !== data.installed
        ? "A newer stable release may be available."
        : "Ready to check for stable releases.");
    status.className = data.setup_error || data.phase === "failed"
      ? "update-status error" : "update-status";
    if (data.release_error && !data.latest && !data.setup_error) {
      status.textContent += " GitHub release check unavailable.";
    }
  } catch (_) {
    phase.textContent = "Reconnecting…";
    button.disabled = true;
    status.textContent = "DevStack is temporarily unreachable; retrying…";
  }
}
if (form) {
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (submitting) return;
    submitting = true;
    button.disabled = true;
    status.textContent = "Sending update request…";
    try {
      const data = await postForm(form.action, form);
      status.textContent = data.message || "Update request submitted.";
      status.className = data.ok ? "update-status" : "update-status error";
    } catch (error) {
      status.textContent = error.message || "Cannot submit update request.";
      status.className = "update-status error";
    } finally {
      submitting = false;
      void refresh();
    }
  });
  startPolling(refresh, 6000);
}
