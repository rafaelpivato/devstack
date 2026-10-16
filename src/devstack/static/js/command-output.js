import {getJSON} from "./api.js";
import {startPolling} from "./polling.js";

const status = document.getElementById("status");
const output = document.getElementById("output");
const cancel = document.getElementById("cancel-form");
const url = document.body.dataset.jobUrl;
if (status && output && cancel && url) {
  startPolling(async () => {
    try {
      const data = await getJSON(url);
      status.textContent = data.job.state +
        (data.job.exit_code == null ? "" : " · Exit " + data.job.exit_code);
      if (output.textContent !== data.output)
        output.textContent = data.output || "(no output yet)";
      cancel.hidden = !["queued", "running"].includes(data.job.state);
    } catch (_) {
      status.textContent = "Connection interrupted; retrying…";
    }
  }, 1500);
}
