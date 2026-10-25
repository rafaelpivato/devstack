import { useCallback, useState } from "react";
import { readMCPStatus, requestMCPAction } from "../../api/configuration";
import { useResource } from "../../hooks/useResource";
import { Notice, Shell, Skeleton, Status } from "../../components/Shell";
import { NavLink, type Navigator } from "../../navigation/useNavigation";
import { ADMIN_ROOT } from "../../navigation/routes";
import { Feedback, message } from "./Common";

export function MCPConnectionsScreen({navigation}:{navigation:Navigator}) {
  const [serial, setSerial] = useState(0);
  const [busy, setBusy] = useState(false);
  const [accepted, setAccepted] = useState(false);
  const [error, setError] = useState<string|null>(null);
  const [feedback, setFeedback] = useState<string|null>(null);
  const [copyFeedback, setCopyFeedback] = useState<string|null>(null);
  const load = useCallback((signal:AbortSignal)=>readMCPStatus(signal),[serial]);
  const state = useResource(load, 5000);
  const current = state.kind==="ready"?state.data:null;
  const enabled = current?.funnel==="enabled";
  const request = async(action:"enable"|"disable")=>{
    if(busy||(action==="enable"&&!accepted))return;
    setBusy(true);setError(null);setFeedback(null);
    try {
      const response = await requestMCPAction(action);
      setFeedback(response.message + ". Status will refresh automatically.");
      setAccepted(false);setSerial(s=>s+1);
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
      <div className="property"><span>Public Funnel</span><Status text={current.funnel}
        kind={enabled?"positive":current.funnel==="unknown"?"warning":"neutral"}/></div>
      {current.request_pending&&<p role="status">Applying your request…</p>}
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
    {!enabled&&<label className="confirm-label"><input type="checkbox"
      checked={accepted} onChange={e=>setAccepted(e.target.checked)}/>
      I understand this temporarily exposes the unauthenticated status and ping tools publicly.
    </label>}
    <div className="actions">
      <button type="button" className="primary-button"
        disabled={!current?.installed || busy || current?.request_pending || enabled || !accepted}
        onClick={()=>void request("enable")}>{busy?"Working…":"Enable MCP connection"}</button>
      <button type="button" className="text-button"
        disabled={!current?.installed || busy || current?.request_pending}
        onClick={()=>void request("disable")}>Disable public connection</button>
      <button type="button" className="text-button" onClick={()=>setSerial(x=>x+1)}>
        Refresh status</button>
    </div>
    <Feedback error={error} success={feedback}/>
    <Notice title="Connecting ChatGPT">After enabling, copy the HTTPS URL and add it
      as a custom MCP server in ChatGPT web, choosing No authentication when available.
      Test devstack_status and devstack_ping. Plugin availability and public HTTPS port
      10000 support depend on your ChatGPT account and client. You may need to approve
      Funnel in the Tailscale administration console on first use.</Notice>
    <p className="actions"><NavLink to={ADMIN_ROOT} navigate={navigation.navigate}>Administration →</NavLink></p>
  </Shell>;
}
