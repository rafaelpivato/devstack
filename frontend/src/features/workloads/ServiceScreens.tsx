import { useCallback, useState } from "react";
import { readServices, readServiceLogs } from "../../api/workloads";
import type { RuntimeSnapshot } from "../../api/runtime";
import { useResource } from "../../hooks/useResource";
import { isContainerId, serviceUrl } from "../../navigation/routes";
import { ItemLink, Notice, Skeleton, Status } from "../../components/Shell";
import type { Navigator } from "../../navigation/useNavigation";
import { ActiveOnly, CopyLog, ReadError, TextLog, WorkloadShell, canOperate } from "./WorkloadShared";

export function ServicesScreen({id,runtime,navigation}:{
  id:string;runtime:RuntimeSnapshot;navigation:Navigator;
}) {
  const load=useCallback((signal:AbortSignal)=>readServices(id,signal),[id]);
  const state=useResource(load,10000);
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
        <div className="item-group">{service.containers.filter(c=>isContainerId(c.id)).map(container=>
          <ItemLink key={container.id} icon="⬡" title={container.name||container.id.slice(0,12)}
            detail={container.state+(container.health?" · health "+container.health:"")}
            href={serviceUrl(id,container.id)} navigation={navigation}/>)}</div>
      </section>)}
    </>}
    <p className="text-muted">Compose build and restart run as supervised jobs, managed from the Jobs section.</p>
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
