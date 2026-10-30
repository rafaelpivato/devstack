import { useCallback, useEffect, useState } from "react";
import { readMCPStatus, requestMCPAction, readMCPConnections,
  revokeMCPConnection, type MCPConnection } from "../../api/configuration";
import { useResource } from "../../hooks/useResource";
import { Notice, Shell, Skeleton, Status } from "../../components/Shell";
import { NavLink, type Navigator } from "../../navigation/useNavigation";
import { ADMIN_ROOT, adminUrl } from "../../navigation/routes";
import { message } from "./Common";

const FUNNEL_FIXES: Record<string,string> = {
  funnel_setup_timeout: "Funnel setup did not finish within 45 seconds; approval or certificate status is not verified. Check preflight and retry here.",
  funnel_timeout_unknown: "Funnel timed out; this alone does not prove that an approval is required. Check the read-only readiness report and retry here.",
  funnel_approval_required: "Tailscale requested Funnel approval. Open Tailscale administration in your browser, complete approval, then retry Enable MCP here.",
  funnel_policy_denied: "Tailscale denied Funnel access by policy. Review node and tailnet Funnel permissions in Tailscale administration, then retry here.",
  funnel_certificate_pending: "The HTTPS certificate is still pending. Check Tailscale HTTPS settings and try enabling again later.",
  funnel_permission_required: "Approve Funnel access in the Tailscale admin console and retry here. Check the tailnet Funnel policy and node permissions.",
  funnel_https_required: "Enable MagicDNS and HTTPS certificates for your tailnet in the Tailscale admin console, then retry.",
  funnel_certificate_failed: "Tailscale could not issue its HTTPS certificate. Check the tailnet HTTPS settings and certificate rate limits before retrying.",
  funnel_port_in_use: "Tailscale reports that the MCP port is already occupied. DevStack will not overwrite another service or an unknown route.",
  network_migration_incomplete: "Automatic Tailscale reconciliation has not finished. DevStack retries independently.",
  funnel_command_failed: "Tailscale rejected the Funnel operation for an unclassified reason. Copy the MCP diagnostic snapshot to report it; DevStack does not disclose raw host logs.",
};

export function MCPConnectionsScreen({navigation}:{navigation:Navigator}) {
  const [serial, setSerial] = useState(0);
  const [connections, setConnections] = useState<MCPConnection[]>([]);
  const [connectionError, setConnectionError] = useState<string|null>(null);
  const [authBusy, setAuthBusy] = useState(false);
  useEffect(()=>{
    let alive=true;
    readMCPConnections().then(result=>{
      if(alive){setConnections(result.connections);setConnectionError(null);}
    }).catch(e=>{if(alive)setConnectionError(message(e));});
    return ()=>{alive=false;};
  },[serial]);
  const revoke=async(id:string)=>{
    if(authBusy||!window.confirm("Revoke this ChatGPT connection immediately?"))return;
    setAuthBusy(true);setConnectionError(null);
    try{await revokeMCPConnection(id);setSerial(s=>s+1);}
    catch(e){setConnectionError(message(e));}
    finally{setAuthBusy(false);}
  };
  const [busy, setBusy] = useState(false);
  const [accepted, setAccepted] = useState(false);
  const [acceptedWebhookDisable, setAcceptedWebhookDisable] = useState(false);
  const [acceptedWebhookEnable, setAcceptedWebhookEnable] = useState(false);
  const [error, setError] = useState<string|null>(null);
  const [feedback, setFeedback] = useState<string|null>(null);
  const [copyFeedback, setCopyFeedback] = useState<string|null>(null);
  const load = useCallback((signal:AbortSignal)=>readMCPStatus(signal),[serial]);
  const state = useResource(load, 5000);
  const current = state.kind==="ready"?state.data:null;
  const enabled = current?.funnel==="enabled";
  const preflight = current?.network_preflight;
  const request = async(action:"enable"|"disable"|"disable_webhooks"|"enable_webhooks")=>{
    if(busy||(action==="enable"&&!accepted)||(action==="disable_webhooks"&&!acceptedWebhookDisable)||(action==="enable_webhooks"&&!acceptedWebhookEnable))return;
    setBusy(true);setError(null);setFeedback(null);
    try {
      const response = await requestMCPAction(action);
      setFeedback(response.message + ". Status will refresh automatically.");
      setAccepted(false);setAcceptedWebhookDisable(false);setAcceptedWebhookEnable(false);setSerial(s=>s+1);
    } catch (e) { setError(message(e)); }
    finally {setBusy(false);}
  };
  const copy = async()=>{
    if(!current?.url)return;
    try{
      await navigator.clipboard.writeText(current.url);
      setCopyFeedback("MCP URL copied.");
    }catch{setCopyFeedback("Clipboard unavailable. Select the URL to copy.");}
  };
  return <Shell title="ChatGPT / MCP" subtitle="Global — connectivity" navigation={navigation} back>
    <Notice title="Owner-approved MCP">A single authorization grants the connected
      client access to all available DevStack MCP tools. Connection approval uses a
      dedicated private page. Do not enter secrets as ping values.</Notice>
    <section className="card">
      <h2>Authorized connections</h2>
      <p className="text-muted">Only creation date is stored; DevStack does not track
        when a client last used MCP.</p>
      {connections.length===0&&!connectionError&&<p>No connections approved yet.</p>}
      {connections.map(item=><div className="property" key={item.id}>
        <span><strong>{item.name}</strong><span className="text-muted" style={{display:"block"}}>
          Created {new Date(item.created_at).toLocaleString()}</span></span>
        <button type="button" className="text-button" disabled={authBusy}
          onClick={()=>void revoke(item.id)}>Revoke</button>
      </div>)}
      {connectionError&&<Notice title="Cannot load connections" role="alert">{connectionError}</Notice>}
    </section>
    {state.kind==="loading"&&<Skeleton/>}
    {state.kind==="error"&&<Notice title="Cannot check MCP status" role="alert">{state.error.message}</Notice>}
    {current&&<section className="card">
      <div className="property"><span>MCP gateway</span><strong>{current.gateway}</strong></div>
      <div className="property"><span>Request queue</span>
        <strong>{current.request_state==="ready"?"ready":current.request_state.replaceAll("_"," ")}</strong>
      </div>
      <div className="property"><span>Public Funnel</span><Status text={current.funnel}
        kind={enabled?"positive":current.funnel==="unknown"?"warning":"neutral"}/></div>
      {current.private_url&&<div className="property"><span>Private dashboard</span><strong style={{overflowWrap:"anywhere"}}>{current.private_url}</strong></div>}
      {current.request_pending&&<p role="status">Applying your request…</p>}
      {current.request_state!=="ready"&&<Notice title="Request queue unavailable" role="alert">
        DevStack cannot currently queue MCP actions ({current.request_state.replaceAll("_"," ")}).
        Check DevStack diagnostics for the matching failure event.
      </Notice>}
      {current.last_result&&<p className="text-muted">Last action: {current.last_result.replaceAll("_"," ")}</p>}
      {current.url&&<div>
        <div className="section-head">MCP connection URL</div>
        <pre className="log-view" style={{overflowWrap:"anywhere",whiteSpace:"pre-wrap"}}>{current.url}</pre>
        <button type="button" className="text-button" onClick={()=>void copy()}>Copy connection URL</button>
        {copyFeedback&&<p role="status">{copyFeedback}</p>}
      </div>}
    </section>}
    {current&&<section className="card">
      <h2>Tailscale / Funnel readiness</h2>
      <p className="text-muted">Read-only checks refresh about once a minute. They verify
        local setup, not whether an external ChatGPT client can reach the server.</p>
      {preflight&&<>
        {preflight.reason&&preflight.reason!=="none"&&<p className="text-muted">
          Readiness reason: <code>{preflight.reason.replaceAll("_"," ")}</code>.
          This reports only local configuration and never verifies external access.</p>}
        <div className="property"><span>Local readiness</span>
          <Status text={preflight.health} kind={preflight.health==="ready"?"positive":
            preflight.health==="disabled"?"neutral":"warning"}/></div>
        <div className="property"><span>Tailscale</span><strong>{preflight.tailscale}</strong></div>
        <div className="property"><span>Device identity</span><strong>{preflight.identity.replaceAll("_"," ")}</strong></div>
        <div className="property"><span>Private dashboard route</span>
          <strong>{preflight.private_dashboard.replaceAll("_"," ")}</strong></div>
        <div className="property"><span>Public MCP Funnel route</span>
          <strong>{preflight.public_funnel}</strong></div>
        <div className="property"><span>Local gateway HTTP health</span>
          <strong>{preflight.gateway.replaceAll("_"," ")}</strong></div>
        {preflight.reason==="routes_unrecognized"&&<Notice title="Tailscale routes need review">
          DevStack cannot classify the configured Serve/Funnel routes using the
          expected topology. It will not reset them; check diagnostics or report a bug.
        </Notice>}
        {preflight.reason==="serve_status_unavailable"&&<Notice title="Tailscale route status unavailable">
          DevStack could not read Tailscale's read-only route status. This does not
          by itself mean your public MCP connection has failed.
        </Notice>}
        {preflight.health==="unknown"&&<Notice title="Preflight unavailable">
          The diagnostics collector has not produced a recent verified snapshot.
          Refresh after setup; no settings have been changed.</Notice>}
        {preflight.tailscale==="disconnected"&&<Notice title="Tailscale not connected">
          Complete first-time Tailscale enrollment before enabling public MCP.</Notice>}
        {preflight.identity==="changed"&&<Notice title="Device identity changed" role="alert">
          DevStack refuses to publish through a different node or tailnet.
          Review the device in Tailscale administration; no automatic reset is performed.</Notice>}
        {preflight.private_dashboard==="migration_required"&&<Notice title="Network reconciliation pending">
          The existing reconciler must finish moving the private dashboard to HTTPS 8443.
          It will retry without a manual reset.</Notice>}
        {preflight.public_funnel==="pending"&&<Notice title="Public Funnel not yet configured">
          DevStack has requested public MCP, but its final route is not confirmed.
          Check the last action below. You can retry Enable MCP after resolving the issue.</Notice>}
        {preflight.gateway==="unavailable"&&<Notice title="Local public gateway unavailable">
          Funnel may be configured, but the local HTTP health check failed.
          Review the MCP diagnostics; do not expose the private dashboard to diagnose this.</Notice>}
      </>}
      <p className="text-muted">An externally reachable HTTPS connection is
        confirmed only by a separate off-server test or a successful ChatGPT tool call.</p>
    </section>}
    {current&&!current.installed&&<Notice title="Host setup missing" role="alert">
      The installed release did not provision the MCP service. Use the supported release updater
      to install the latest version before enabling this connection.</Notice>}
    {current?.funnel==="unknown"&&<Notice title="Cannot verify Tailscale">
      Check that Tailscale is connected and private Serve is configured. DevStack will refuse
      to overwrite any unrecognized Serve or Funnel routes.</Notice>}
    {current?.network_phase!=="new"&&current?.network_phase!=="unknown"&&
      <Notice title="Automatic network reconciliation">
        The new private dashboard uses HTTPS :8443. DevStack will reconcile
        Tailscale routes during the release update and retry automatically.
        No manual migration action is needed.
      </Notice>}
    {!enabled&&<label className="confirm-label"><input type="checkbox"
      checked={accepted} onChange={e=>setAccepted(e.target.checked)}/>
      I understand this enables a publicly reachable OAuth-protected MCP endpoint.
    </label>}
    <div className="actions">
      <button type="button" className="primary-button"
        disabled={!current?.installed || busy || current?.request_pending || enabled || !accepted || current?.network_phase!=="new"}
        onClick={()=>void request("enable")}>{busy?"Working…":"Enable MCP connection"}</button>
      <button type="button" className="text-button"
        disabled={!current?.installed || busy || current?.request_pending}
        onClick={()=>void request("disable")}>Disable public connection</button>
      <button type="button" className="text-button" onClick={()=>setSerial(x=>x+1)}>
        Refresh status</button>
    </div>
    {error&&<Notice title="MCP connection request failed" role="alert">{error}
      <p>Check the request queue status and copy the Web diagnostics snapshot.
        No SSH is required for routine troubleshooting.</p>
    </Notice>}
    {feedback&&<Notice title="MCP request submitted">{feedback}</Notice>}
    <section className="card">
      <h2>MCP troubleshooting</h2>
      <p>Gateway requests, controller health checks and Funnel lifecycle errors are
        included in DevStack diagnostics. Choose a 1, 5 or 30 minute window and copy
        the snapshot directly from the browser.</p>
      <p className="actions"><NavLink to={adminUrl("diagnostics")}
        navigate={navigation.navigate}>View and copy MCP diagnostics →</NavLink></p>
      {current?.last_result && !["enabled","disabled"].includes(current.last_result) &&
        <Notice title="Last MCP action failed" role="alert">
          <p>Error code: <code>{current.last_result}</code></p>
          <p>{FUNNEL_FIXES[current.last_result] ??
            "Open diagnostics for the matching event and share the copied snapshot. No SSH is required."}</p>
          {["funnel_setup_timeout","funnel_timeout_unknown","funnel_permission_required",
             "funnel_approval_required","funnel_policy_denied","funnel_https_required",
             "funnel_certificate_pending","funnel_certificate_failed"].includes(current.last_result)
            && <p><a href="https://login.tailscale.com/admin" target="_blank" rel="noreferrer">
              Open Tailscale administration</a></p>}
        </Notice>}
    </section>
    {current?.network_phase==="new"&&<section className="card">
      <h2>Public webhook ingress</h2>
      <p>The unified public service handles <code>/hooks/*</code> over HTTPS :443.
        Only registered project routes are forwarded directly to private Docker services.
        Provider signatures must still be verified by the receiving application.</p>
      <div className="property"><span>Webhooks</span><strong>{current.webhooks_enabled?"enabled":"disabled"}</strong></div>
      {!current.webhooks_enabled&&<label className="confirm-label">
        <input type="checkbox" checked={acceptedWebhookEnable}
          onChange={e=>setAcceptedWebhookEnable(e.target.checked)}/>
        I understand this will allow public webhook requests on HTTPS 443.
      </label>}
      {current.webhooks_enabled&&<label className="confirm-label">
        <input type="checkbox" checked={acceptedWebhookDisable}
          onChange={e=>setAcceptedWebhookDisable(e.target.checked)}/>
        I understand disabling public ingress interrupts existing webhook callbacks.
      </label>}
      <div className="actions"><button type="button" className="text-button"
        disabled={busy||current.request_pending||
          (current.webhooks_enabled?!acceptedWebhookDisable:!acceptedWebhookEnable)}
        onClick={()=>void request(current.webhooks_enabled?"disable_webhooks":"enable_webhooks")}>
        {current.webhooks_enabled?"Disable public webhooks":"Enable public webhooks"}
      </button></div>
    </section>}
    <Notice title="Connecting ChatGPT">After enabling, add the HTTPS MCP URL in ChatGPT
      with OAuth authentication. Approve the connection from this private dashboard while
      connected to Tailscale, then test devstack_status and devstack_ping.
      Existing unauthenticated connections must be reconnected after this upgrade.</Notice>
    <p className="actions"><NavLink to={ADMIN_ROOT} navigate={navigation.navigate}>Administration →</NavLink></p>
  </Shell>;
}
