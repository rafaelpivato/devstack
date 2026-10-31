import { useCallback, useState } from "react";
import { resolveApplicationUrl } from "../../lib/applicationUrls";
import { readProject } from "../../api/projects";
import type { RuntimeSnapshot } from "../../api/runtime";
import { useResource } from "../../hooks/useResource";
import { Notice, Skeleton } from "../../components/Shell";
import type { Navigator } from "../../navigation/useNavigation";
import { ActiveOnly, ReadError, WorkloadShell } from "./WorkloadShared";

/** Applications are owner-declared bookmarks, not discovered Docker bindings. */
export function ApplicationsScreen({id,runtime,navigation}:{
  id:string;runtime:RuntimeSnapshot;navigation:Navigator;
}) {
  const loadProject=useCallback((signal:AbortSignal)=>readProject(id,signal),[id]);
  const project=useResource(loadProject,20000);
  const [copied,setCopied]=useState<string|null>(null);
  return <WorkloadShell title="Applications" id={id} navigation={navigation}>
    <ActiveOnly id={id} runtime={runtime}/>
    {project.kind==="loading"&&<Skeleton/>}
    {project.kind==="error"&&<ReadError message={project.error.message}/>}
    {project.kind==="ready"&&<>
      {project.data.applications.length===0&&
        <Notice title="No applications configured">Add named URLs in the project's Applications & tasks configuration or umbrella manifest.</Notice>}
      <div className="item-group">{project.data.applications.map(app=>{
        const url=app.url?resolveApplicationUrl(app.url,window.location.hostname):null;
        return <div className="item-static" key={app.name}>
          <span className="item-icon" aria-hidden="true">↗</span>
          <div className="item-text">
            <strong>{app.name}</strong>
            {url?<><a href={url} target="_blank" rel="noopener noreferrer">
              Open application ↗</a>
              <small className="text-muted">{url}</small>
              <button type="button" onClick={()=>void navigator.clipboard.writeText(url).then(
                ()=>setCopied(app.name),()=>setCopied(null)
              )}>{copied===app.name?"Copied":"Copy URL"}</button></>
              :<small>No explicit URL configured. Update this application in project settings.</small>}
          </div>
        </div>;
      })}</div>
    </>}
  </WorkloadShell>;
}
