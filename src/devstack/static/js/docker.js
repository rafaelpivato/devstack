import {getJSON} from "./api.js";
import {startPolling} from "./polling.js";

const status = document.getElementById("job-state");
const output = document.getElementById("job-output");
const url = document.body.dataset.dockerStatusUrl;
if (status && output && url) {
  let previous = null;
  startPolling(async () => {
    try {
      const data = await getJSON(url);
      const summary = data.job
        ? data.job.action + " · " + data.job.state +
          (data.job.exit_code == null ? "" : " · exit " + data.job.exit_code)
        : "none";
      status.textContent = "Last operation: " + summary;
      if (data.log !== previous) {
        previous = data.log;
        output.textContent = data.log || "(no output)";
      }
    } catch (_) {
      status.textContent = "Connection interrupted; retrying…";
    }
  }, 2500);
}
