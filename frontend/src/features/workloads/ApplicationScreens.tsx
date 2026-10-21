import { useCallback } from "react";
import { readPrivatePorts, isPrivateApplicationUrl } from "../../api/workloads";
import { readProject } from "../../api/projects";
import type { RuntimeSnapshot } from "../../api/runtime";
import { useResource } from "../../hooks/useResource";
import { Notice, Skeleton } from "../../components/Shell";
import type { Navigator } from "../../navigation/useNavigation";
import { ActiveOnly, ReadError, WorkloadShell, canOperate } from "./WorkloadShared";

export function ApplicationsScreen({id,runtime,navigation}:{
  id:string;runtime:RuntimeSnapshot;navigation:Navigator;
}) {
  const loadProject=useCallback((signal:AbortSignal)=>readProject(id,signal),[id]);
  const loadPorts=useCallback((signal:AbortSignal)=>readPrivatePorts(id,signal),[id]);
  const project=useResource(loadProject,20000);
  const ports=useResource(loadPorts,12000);
  const active=canOperate(runtime,id);
  return <WorkloadShell title="Applications" id={id} navigation={navigation}>
    <ActiveOnly id={id} runtime={runtime}/>
    {project.kind==="loading"&&<Skeleton/>}
    {project.kind==="error"&&<ReadError message={project.error.message}/>}
    {ports.kind==="error"&&<ReadError message={ports.error.message}/>}
    {ports.kind==="loading"&&<Skeleton/>}
    {ports.kind==="ready"&&active&&!ports.data.tailscale_available&&
      <Notice title="Tailscale link unavailable">No verified tailnet interface is available. No public or guessed URLs will be shown.</Notice>}
    {project.kind==="ready"&&<>
      {project.data.applications.length===0&&<Notice title="No applications configured">Applications can be registered in project configuration.</Notice>}
      <div className="item-group">{project.data.applications.map(app=>{
        const found=ports.kind==="ready"&&ports.data.active&&ports.data.tailscale_available
          ?ports.data.applications.find(p=>p.name===app.name&&p.service===app.service
             &&isPrivateApplicationUrl(p.url)):undefined;
        return <div className="item-static" key={app.name}>
          <span className="item-icon" aria-hidden="true">↗</span>
          <div className="item-text"><strong>{app.name}</strong>
            <small>{app.service}{app.port==null?"":" · container port "+app.port}</small>
            {found?<a href={found.url} target="_blank" rel="noopener noreferrer">
              Open private URL ↗</a>:<small>No verified running private URL</small>}
          </div>
        </div>;
      })}</div>
    </>}
    {ports.kind==="ready"&&ports.data.ports.length>0&&
      <p className="text-muted">Additional verified private mappings exist for this active project; only registered applications are linked here.</p>}
  </WorkloadShell>;
}
