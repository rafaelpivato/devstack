/* A single transport for the current dashboard and the eventual v0.6 UI. */
const headers = {Accept: "application/json"};

export async function getJSON(url, {signal} = {}) {
  const response = await fetch(url, {
    credentials: "same-origin", cache: "no-store", headers, signal,
  });
  if (!response.ok) throw new Error(`Request failed (${response.status})`);
  return response.json();
}

export async function postForm(url, form) {
  const response = await fetch(url, {
    method: "POST", credentials: "same-origin",
    body: new FormData(form), headers,
  });
  let payload = {};
  try { payload = await response.json(); } catch (_) { /* Not JSON. */ }
  if (!response.ok) throw new Error(payload.message || `Request failed (${response.status})`);
  return payload;
}

export async function postAction(url, csrfToken) {
  const response = await fetch(url, {
    method: "POST", credentials: "same-origin",
    headers: {...headers, "X-CSRF-Token": csrfToken},
  });
  let payload = {};
  try { payload = await response.json(); } catch (_) { /* Not JSON. */ }
  if (!response.ok) throw new Error(payload.message || `Request failed (${response.status})`);
  return payload;
}
