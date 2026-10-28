import { useEffect, useState } from "react";
import { apiRequest } from "../../api/client";
import { Notice, Shell, Skeleton } from "../../components/Shell";
import type { Navigator } from "../../navigation/useNavigation";

type Pending = {id:string;client:string;scope:string;created_at:string};
type Decision = {redirect_url:string};

/** Private Tailscale-only approval. A URL alone cannot approve: POST needs CSRF. */
export function MCPApprovalScreen({navigation}:{navigation:Navigator}) {
  const identifier = new URLSearchParams(window.location.search).get("request_id") ?? "";
  const [pending,setPending] = useState<Pending|null>(null);
  const [error,setError] = useState<string|null>(null);
  const [busy,setBusy] = useState(false);
  useEffect(()=>{
    const controller=new AbortController();
    if(!/^[A-Za-z0-9_-]{20,128}$/.test(identifier)){
      setError("Invalid authorization request."); return ()=>controller.abort();
    }
    apiRequest<Pending>("/api/v2/devstack/mcp/oauth/requests/"+identifier,
      {signal:controller.signal}).then(setPending).catch(e=>{
      if(!controller.signal.aborted)setError(e instanceof Error?e.message:"Cannot read authorization request.");
    });
    return ()=>controller.abort();
  },[identifier]);
  async function decide(action:"approve"|"deny"){
    if(!pending || busy)return;
    setBusy(true);setError(null);
    try{
      const result=await apiRequest<Decision>(
        "/api/v2/devstack/mcp/oauth/requests/"+identifier+"/"+action,
        {method:"POST",body:{}});
      // The backend constructs the exact pre-registered ChatGPT callback.
      const target=new URL(result.redirect_url);
      if(target.origin!=="https://chatgpt.com" ||
         target.pathname!=="/connector_platform_oauth_redirect")throw Error("Invalid callback");
      window.location.assign(result.redirect_url);
    }catch(e){setError(e instanceof Error?e.message:"Unable to complete authorization.");setBusy(false);}
  }
  return <Shell title="Authorize ChatGPT" subtitle="Private workstation approval" navigation={navigation} back>
    <Notice title="Workstation owner approval">This request must be approved from
      your private Tailscale dashboard. Approve only if you initiated the ChatGPT connection.</Notice>
    {!pending&&!error&&<Skeleton/>}
    {error&&<Notice title="Authorization unavailable" role="alert">{error}</Notice>}
    {pending&&<section className="card">
      <h2>{pending.client} wants to connect</h2>
      <p>Grant read-only MCP access to this DevStack workstation. This release
        supports status and diagnostic ping only; project operations are not enabled.</p>
      <div className="property"><span>Requested access</span><strong>{pending.scope}</strong></div>
      <div className="actions">
        <button type="button" className="primary-button" disabled={busy}
          onClick={()=>void decide("approve")}>{busy?"Working…":"Approve connection"}</button>
        <button type="button" className="text-button" disabled={busy}
          onClick={()=>void decide("deny")}>Deny</button>
      </div>
    </section>}
  </Shell>;
}
