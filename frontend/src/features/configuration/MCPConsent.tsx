import { useEffect, useState } from "react";
import { readMCPApproval, decideMCPApproval, type MCPAuthorizationRequest } from "../../api/configuration";
import { watchAppearance } from "../../lib/appearance";
import { message } from "./Common";

/** One-purpose private tailnet consent; no shell, navigation, runtime fetch or project polling. */
export function MCPConsentScreen() {
  useEffect(watchAppearance, []);
  const identifier = new URLSearchParams(window.location.search).get("oauth_request");
  const [request, setRequest] = useState<MCPAuthorizationRequest | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!identifier || !/^[A-Za-z0-9_-]{32,128}$/.test(identifier)) {
      setError("Invalid or missing connection request. Start the connection from ChatGPT again.");
      return;
    }
    let alive = true;
    readMCPApproval(identifier).then(result => {
      if (alive) { setRequest(result.request); setError(null); }
    }).catch(() => {
      if (alive) setError("This request expired or is no longer available. Start again from ChatGPT.");
    });
    return () => { alive = false; };
  }, [identifier]);

  const decide = async (decision: "approve" | "deny") => {
    if (!identifier || !request || busy) return;
    setBusy(true); setError(null);
    try {
      const response = await decideMCPApproval(identifier, decision);
      const callback = new URL(response.redirect);
      if (callback.origin !== "https://chatgpt.com" ||
          callback.pathname !== "/connector_platform_oauth_redirect" ||
          callback.hash || !callback.searchParams.has("state") ||
          !callback.searchParams.has("iss") ||
          !(callback.searchParams.has("code") || callback.searchParams.get("error") === "access_denied"))
        throw new Error("Invalid authorization callback");
      // Fixed, server-generated callback only. No arbitrary redirect from model input.
      window.location.assign(response.redirect);
    } catch (e) {
      setError(message(e)); setBusy(false);
    }
  };

  return <main className="mcp-consent" id="content">
    <div className="mcp-consent__brand" aria-label="DevStack workstation">
      <span className="mcp-consent__brand-mark" aria-hidden="true">▤</span> DevStack
    </div>
    <section className="mcp-consent__card" aria-labelledby="mcp-consent-title">
      <p className="mcp-consent__eyebrow">Private workstation · Tailscale</p>
      <div className="mcp-consent__icon" aria-hidden="true">◎</div>
      <h1 id="mcp-consent-title">Authorize ChatGPT?</h1>
      <p className="mcp-consent__description">
        ChatGPT is requesting permission to connect to this DevStack workstation.
      </p>
      {request && <div className="mcp-consent__disclosure">
        <p><strong>Client:</strong> {request.client}</p>
        <p>Approving grants access to <strong>all available DevStack MCP tools</strong>,
          including project information and logs. Logs can contain sensitive information.</p>
        <p className="text-muted">Requested {new Date(request.created_at).toLocaleString()}</p>
      </div>}
      {!request && !error && <p role="status" className="mcp-consent__description">
        Loading connection request…
      </p>}
      {request && <div className="mcp-consent__actions">
        <button type="button" className="mcp-consent__approve" disabled={busy}
          onClick={() => void decide("approve")}>Authorize connection</button>
        <button type="button" className="mcp-consent__deny" disabled={busy}
          onClick={() => void decide("deny")}>Deny</button>
      </div>}
      {error && <p className="mcp-consent__error" role="alert">{error}</p>}
    </section>
    <p className="mcp-consent__footnote">Approval is available only from the private Tailscale dashboard.</p>
  </main>;
}
