import { useCallback, useState } from "react";
import { deleteWebhook, putWebhook, readWebhooks } from "../../api/configuration";
import { useResource } from "../../hooks/useResource";
import { Notice, Skeleton, Status } from "../../components/Shell";
import type { Navigator } from "../../navigation/useNavigation";
import { ConfigBack, ConfigShell, Feedback, message } from "./Common";
const validRoute=(s:string)=>/^[a-z][a-z0-9_-]{0,62}$/.test(s);
const validService=(s:string)=>/^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$/.test(s);
const validPath=(s:string)=>/^\/[A-Za-z0-9_./~-]{0,254}$/.test(s)&&!s.startsWith("//")&&!s.split("/").includes("..");

export function WebhookEditor({id,navigation}:{id:string;navigation:Navigator}) {
  const [serial,setSerial]=useState(0);
  const loader=useCallback((signal:AbortSignal)=>readWebhooks(id,signal),[id,serial]);
  const state=useResource(loader,0);
  const [route,setRoute]=useState("");
  const [service,setService]=useState("");
  const [port,setPort]=useState("8000");
  const [destination,setDestination]=useState("/");
  const [confirm,setConfirm]=useState(false);
  const [removing,setRemoving]=useState<string|null>(null);
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState<string|null>(null);
  const [success,setSuccess]=useState<string|null>(null);
  const valid=validRoute(route)&&validService(service)&&validPath(destination)&&
    Number.isInteger(Number(port))&&Number(port)>=1&&Number(port)<=65535;
  const upsert=async()=>{
    if(!confirm||!valid||busy)return;
    setBusy(true);setError(null);setSuccess(null);
    try{
      const result=await putWebhook(id,route,service,Number(port),destination);
      setSuccess("Webhook "+result.route+" saved. Active routing may change immediately.");
      setConfirm(false);setSerial(n=>n+1);
    }catch(e){setError(message(e));}finally{setBusy(false);}
  };
  const remove=async()=>{
    if(!removing||busy)return;
    setBusy(true);setError(null);setSuccess(null);
    try{
      await deleteWebhook(id,removing);
      setSuccess("Webhook "+removing+" removed.");
      setRemoving(null);setSerial(n=>n+1);
    }catch(e){setError(message(e));}finally{setBusy(false);}
  };
  return <ConfigShell title="Webhook routes" id={id} navigation={navigation}>
    <Notice title="Project-specific forwarding">These routes configure the dedicated webhook ingress—not the admin API. Changes can affect an active project's incoming requests.</Notice>
    {state.kind==="loading"&&<Skeleton/>}
    {state.kind==="error"&&<Notice title="Cannot read routes" role="alert">{state.error.message}</Notice>}
    {state.kind==="ready"&&<div className="item-group">{state.data.webhooks.map(row=>
      <div className="item-static" key={row.route}>
        <div className="item-text"><strong>{row.route}</strong><small>{row.service}:{row.port}{row.destination}</small>
          <button type="button" className="text-button"
            onClick={()=>{setRoute(row.route);setService(row.service);setPort(String(row.port));setDestination(row.destination);setConfirm(false);}}>
            Edit values</button>
          <button type="button" className="text-button"
            onClick={()=>setRemoving(row.route)}>Review removal</button>
        </div>
      </div>)}</div>}
    {removing&&<section className="card">
      <strong>Remove webhook {removing}?</strong>
      <p className="text-muted">Incoming requests for this route will stop being forwarded.</p>
      <button className="primary-button" type="button" disabled={busy} onClick={()=>void remove()}>Confirm removal</button>
      <button className="text-button" type="button" onClick={()=>setRemoving(null)}>Cancel</button>
    </section>}
    <div className="section-head">Create / replace route</div>
    <div className="form-stack">
      <label>Route<input value={route} onChange={e=>{setRoute(e.target.value);setConfirm(false);}} placeholder="github"/></label>
      <label>Compose service<input value={service} onChange={e=>{setService(e.target.value);setConfirm(false);}} placeholder="web"/></label>
      <label>Port<input value={port} inputMode="numeric" onChange={e=>{setPort(e.target.value);setConfirm(false);}}/></label>
      <label>Destination path<input value={destination} onChange={e=>{setDestination(e.target.value);setConfirm(false);}} placeholder="/hooks/github"/></label>
    </div>
    <label className="confirm-label"><input type="checkbox" disabled={!valid||busy} checked={confirm}
      onChange={e=>setConfirm(e.target.checked)}/>Apply this route to the registered project</label>
    <button type="button" className="primary-button" disabled={!confirm||!valid||busy}
      onClick={()=>void upsert()}>Save webhook route</button>
    {!valid&&<p className="text-muted">Use lowercase route names, a Compose service name, port 1–65535, and a safe absolute destination path.</p>}
    <Feedback error={error} success={success}/>
    <ConfigBack id={id} navigation={navigation}/>
  </ConfigShell>;
}
