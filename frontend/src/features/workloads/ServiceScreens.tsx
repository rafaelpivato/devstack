import { useCallback, useState } from "react";
import { readServices, readServiceLogs, runService, type ServiceAction } from "../../api/workloads";
import type { RuntimeSnapshot } from "../../api/runtime";
import { useResource } from "../../hooks/useResource";
import { isContainerId, serviceUrl, jobUrl } from "../../navigation/routes";
import { ItemLink, Notice, Skeleton, Status } from "../../components/Shell";
import type { Navigator } from "../../navigation/useNavigation";
import { ActiveOnly, CopyLog, ReadError, TextLog, WorkloadShell, canOperate } from "./WorkloadShared";

export function ServicesScreen({id,runtime,navigation}:{
  id:string;runtime:RuntimeSnapshot;navigation:Navigator;
}) {
  const load=useCallback((signal:AbortSignal)=>readServices(id,signal),[id]);
  const state=useResource(load,10000);
  const [busy,setBusy]=useState<string|null>(null);
  const [error,setError]=useState<string|null>(null);
  const [unknown,setUnknown]=useState(false);
  const submit=async(service:string,action:ServiceAction)=>{
    if(busy||unknown||!canOperate(runtime,id))return;
    if(!window.confirm(action==="redeploy"
      ?"Redeploy "+service+"? Its container will be replaced after building if applicable."
      :"Restart "+service+"? Its running process will be interrupted."))return;
    setBusy(service);setError(null);
    try{
      const result=await runService(id,service,action);
      navigation.navigate(jobUrl(id,result.job_id));
    }catch(e){
      setError(e instanceof Error?e.message:"Submission outcome unknown");
      setUnknown(true);
    }finally{setBusy(null);}
  };
  return <WorkloadShell title="Services" id={id} navigation={navigation}>
    <ActiveOnly id={id} runtime={runtime}/>
    {state.kind==="loading"&&<Skeleton/>}
    {state.kind==="error"&&<ReadError message={state.error.message}/>}
    {state.kind==="ready"&&<>
      {!state.data.active&&<Notice title="No active containers">Only the active project's Compose services can be inspected.</Notice>}
      {state.data.active&&state.data.services.length===0&&<Notice title="No Compose services">No services are registered in the active Compose stack.</Notice>}
      {state.data.services.map(service=><section className="card" key={service.name}>
        <div className="property"><span>Service</span><strong>{service.name}</strong></div>
        <div className="property"><span>State</span><Status text={service.status}
          kind={service.status==="running"?"positive":"neutral"}/></div>
        {service.containers.length===0&&<p className="text-muted">No container created</p>}
        {canOperate(runtime,id)&&<div className="repo-actions">
          <button type="button" className="text-button" disabled={busy!==null||unknown}
            onClick={()=>void submit(service.name,"restart")}>Restart</button>
          <button type="button" className="text-button" disabled={busy!==null||unknown}
            onClick={()=>void submit(service.name,"redeploy")}>Redeploy</button>
        </div>}
        <div className="item-group">{service.containers.filter(c=>isContainerId(c.id)).map(container=>
          <ItemLink key={container.id} icon="⬡" title={container.name||container.id.slice(0,12)}
            detail={container.state+(container.health?" · health "+container.health:"")}
            href={serviceUrl(id,container.id)} navigation={navigation}/>)}</div>
      </section>)}
    </>}
    {busy&&<p className="text-muted">Submitting action for {busy}…</p>}
    {error&&<Notice title="Service action not confirmed" role="alert">{error}. Check job history before retrying.</Notice>}
    <p className="text-muted">Service actions use controller-supervised jobs. Existing whole-project Compose build and restart remain in Jobs.</p>
  </WorkloadShell>;
}

export function ServiceLogsScreen({id,containerId,runtime,navigation}:{
  id:string;containerId:string;runtime:RuntimeSnapshot;navigation:Navigator;
}) {
  const [tail,setTail]=useState(150);
  const [refresh,setRefresh]=useState(0);
  const load=useCallback((signal:AbortSignal)=>readServiceLogs(id,containerId,tail,signal),
    [id,containerId,tail,refresh]);
  const state=useResource(load,0);
  return <WorkloadShell title="Container logs" id={id} navigation={navigation}>
    <ActiveOnly id={id} runtime={runtime}/>
    {canOperate(runtime,id)&&<>
      <label className="field-inline">Recent lines
        <select value={tail} onChange={e=>setTail(Number(e.target.value))}>
          <option value={50}>50</option><option value={150}>150</option><option value={200}>200</option>
        </select>
      </label>
      <button type="button" className="text-button" onClick={()=>setRefresh(x=>x+1)}>Refresh snapshot</button>
    </>}
    {state.kind==="loading"&&<Skeleton/>}
    {state.kind==="error"&&<ReadError message={state.error.message}/>}
    {state.kind==="ready"&&<>
      <p className="text-muted">Service: {state.data.service} · {state.data.tail} recent lines</p>
      {state.data.truncated&&<Notice title="Output truncated">The controller retained only the latest bounded log bytes.</Notice>}
      <TextLog text={state.data.text} label="Bounded container log snapshot"/>
      <CopyLog value={state.data.text}/>
    </>}
  </WorkloadShell>;
}
