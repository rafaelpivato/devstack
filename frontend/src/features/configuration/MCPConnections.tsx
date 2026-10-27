import { useCallback, useState } from "react";
import { readMCPStatus, requestMCPAction } from "../../api/configuration";
import { useResource } from "../../hooks/useResource";
import { Notice, Shell, Skeleton, Status } from "../../components/Shell";
import { NavLink, type Navigator } from "../../navigation/useNavigation";
import { ADMIN_ROOT, adminUrl } from "../../navigation/routes";
import { message } from "./Common";

const FUNNEL_FIXES: Record<string,string> = {
  funnel_setup_timeout: "Funnel setup did not finish within 45 seconds. This may be a first-time tailnet approval or HTTPS certificate delay. Review the Tailscale admin console and retry here; no SSH is needed.",
  funnel_permission_required: "Approve Funnel access in the Tailscale admin console and retry here. Check the tailnet Funnel policy and node permissions.",
  funnel_https_required: "Enable MagicDNS and HTTPS certificates for your tailnet in the Tailscale admin console, then retry.",
  funnel_certificate_failed: "Tailscale could not issue its HTTPS certificate. Check the tailnet HTTPS settings and certificate rate limits before retrying.",
  funnel_port_in_use: "Tailscale reports that the MCP port is already occupied. DevStack will not overwrite another service or an unknown route.",
  network_migration_incomplete: "Automatic Tailscale reconciliation has not finished. DevStack retries independently.",
  funnel_command_failed: "Tailscale rejected the Funnel operation for an unclassified reason. Copy the MCP diagnostic snapshot to report it; DevStack does not disclose raw host logs.",
};

export function MCPConnectionsScreen({navigation}:{navigation:Navigator}) {
  const [serial, setSerial] = useState(0);
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
    <Notice title="Opt-in public test">This publishes only the DevStack MCP status and ping tools.
      Funnel is reachable from the public internet and has no authentication yet. Never enter secrets
      as ping values. Your private DevStack dashboard stays on the tailnet.</Notice>
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
      I understand this temporarily exposes the unauthenticated status and ping tools publicly.
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
          {["funnel_setup_timeout","funnel_permission_required","funnel_https_required","funnel_certificate_failed"].includes(current.last_result)
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
    <Notice title="Connecting ChatGPT">After enabling, copy the HTTPS URL and add it
      as a custom MCP server in ChatGPT web, choosing No authentication when available.
      Test devstack_status and devstack_ping. Plugin availability and remote HTTPS support depend on your ChatGPT account and client. You may need to approve
      Funnel in the Tailscale administration console on first use.</Notice>
    <p className="actions"><NavLink to={ADMIN_ROOT} navigate={navigation.navigate}>Administration →</NavLink></p>
  </Shell>;
}
