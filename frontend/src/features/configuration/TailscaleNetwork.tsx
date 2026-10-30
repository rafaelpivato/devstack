import {useCallback, useState} from "react";
import {readTailscaleNetwork, renameTailscaleDevice} from "../../api/network";
import type {Navigator} from "../../navigation/useNavigation";
import {NavLink} from "../../navigation/useNavigation";
import {ADMIN_ROOT} from "../../navigation/routes";
import {useResource} from "../../hooks/useResource";
import {Notice, Shell, Skeleton} from "../../components/Shell";
import {Feedback, message} from "./Common";

const validName = (name:string) =>
  name.length<=63 && /^[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(name) &&
  name!=="localhost";

function CopyLink({title,url}:{title:string;url:string|null}){
  const [copied,setCopied]=useState(false);
  if(!url)return <p className="text-muted">{title}: unavailable</p>;
  return <div className="property">
    <span>{title}</span>
    <div><code style={{overflowWrap:"anywhere"}}>{url}</code>{" "}
      <button type="button" className="text-button" onClick={()=>{
        void navigator.clipboard.writeText(url).then(()=>setCopied(true)).catch(()=>setCopied(false));
      }} aria-label={"Copy "+title}>{copied?"Copied":"Copy"}</button>
    </div>
  </div>;
}

export function TailscaleNetworkScreen({navigation}:{navigation:Navigator}){
  const [serial,setSerial]=useState(0);
  const load=useCallback((signal:AbortSignal)=>readTailscaleNetwork(signal),[serial]);
  const state=useResource(load,8000);
  const [name,setName]=useState("");
  const [confirmed,setConfirmed]=useState(false);
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState<string|null>(null);
  const [success,setSuccess]=useState<string|null>(null);
  const data=state.kind==="ready"?state.data:null;
  const proposed=name.trim();
  const canSubmit=!!data?.connected && !!data.fqdn && !data.request_pending &&
    !busy && confirmed && validName(proposed) && proposed!==data.name;
  const run=async()=>{
    if(!canSubmit || !data?.fqdn)return;
    setError(null);setSuccess(null);setBusy(true);
    try{
      await renameTailscaleDevice(proposed,data.fqdn);
      setSuccess("Rename queued. Refresh this page or watch the status below. DNS and TLS may take time to become ready.");
      setSerial(x=>x+1);setConfirmed(false);
    }catch(e){setError(message(e)+" No new rename was queued.");}
    finally{setBusy(false);}
  };
  const result=data?.last_result;
  return <Shell title="Network / Tailscale" subtitle="Device name and HTTPS access" navigation={navigation} back>
    <Notice title="Only this device name changes">The Tailscale node, tailnet, Ubuntu hostname, Docker projects, access rules and Serve/Funnel visibility are not changed by this operation.</Notice>
    {state.kind==="loading"&&<Skeleton/>}
    {state.kind==="error"&&<Notice title="Network status unavailable" role="alert">{state.error.message}</Notice>}
    {data&&<section className="card">
      <div className="property"><span>Connection</span><strong>{data.connected?"Connected":"Not connected"}</strong></div>
      <div className="property"><span>Machine name</span><strong>{data.name??"Unavailable"}</strong></div>
      <div className="property"><span>Actual MagicDNS FQDN</span><code style={{overflowWrap:"anywhere"}}>{data.fqdn??"Unavailable"}</code></div>
      <CopyLink title="Private dashboard" url={data.private_url}/>
      <CopyLink title="Public MCP (only if enabled)" url={data.mcp_url}/>
      <div className="property"><span>Network configuration</span><strong>{data.network_phase}</strong></div>
      {data.request_pending&&<p role="status">Rename queued; root-owned worker pending. Do not submit another rename yet.</p>}
    </section>}
    {result&&<section className="card" aria-live="polite">
      <h2>Last rename operation</h2>
      <p><strong>{result.code.replaceAll("_"," ")}</strong> — {new Date(result.at*1000).toLocaleString()}</p>
      {result.old_dns&&<p>Before: <code>{result.old_dns}</code></p>}
      {result.new_dns&&<p>After (actual): <code>{result.new_dns}</code></p>}
      {result.old_urls&&<>
        <CopyLink title="Previous private link" url={result.old_urls.private_url}/>
        <CopyLink title="Previous MCP link" url={result.old_urls.mcp_url}/>
      </>}
      {result.new_urls&&<>
        <CopyLink title="New private link" url={result.new_urls.private_url}/>
        <CopyLink title="New MCP link" url={result.new_urls.mcp_url}/>
      </>}
      <p>External DNS / TLS reachability is not tested by this status. If pending, retry the new address later; use Administration → Diagnostics for bounded events.</p>
    </section>}
    <section className="card">
      <h2>Rename this device</h2>
      <p>Choose a unique lowercase label (1–63 characters), starting with a letter; only letters, digits and hyphens. Tailscale may assign a suffixed name if yours is taken.</p>
      <label htmlFor="tailscale-name">New machine name</label>
      <input id="tailscale-name" value={name} maxLength={63} autoComplete="off"
        spellCheck={false} onChange={e=>{setName(e.target.value);setConfirmed(false);}}
        placeholder="my-devstack" />
      {name && !validName(proposed)&&<p role="alert">Invalid name. Use a lowercase DNS label, not a URL or FQDN.</p>}
      <Notice title="Existing links may stop working">Bookmarks, ChatGPT MCP connection settings and GitHub/Twilio webhook callbacks may require manual updates after renaming. DevStack does not update external accounts or create public DNS records.</Notice>
      <label className="confirm-label">
        <input type="checkbox" checked={confirmed} onChange={e=>setConfirmed(e.target.checked)}/>
        I understand I may need to reconnect ChatGPT MCP and update external registered callback URLs.
      </label>
      <button type="button" className="primary-button" onClick={()=>void run()}
        disabled={!canSubmit}>{busy?"Requesting…":"Rename Tailscale device"}</button>
      <Feedback error={error} success={success}/>
    </section>
    <p className="actions"><button type="button" className="text-button"
      onClick={()=>setSerial(x=>x+1)}>Refresh status</button></p>
    <p className="actions"><NavLink to={ADMIN_ROOT} navigate={navigation.navigate}>Administration →</NavLink></p>
  </Shell>;
}
