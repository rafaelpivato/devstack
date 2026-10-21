import { useCallback } from "react";
import { readProject, type ProjectDetail } from "../../api/projects";
import type { ProjectSummary, RuntimeSnapshot } from "../../api/runtime";
import { useResource } from "../../hooks/useResource";
import { isProjectId, laneUrl, projectUrl, type Lane } from "../../navigation/routes";
import type { Navigator } from "../../navigation/useNavigation";
import { GLOBAL_PROJECTS, OldDashboardLink } from "../../navigation/useNavigation";
import { ItemLink, Notice, Shell, Skeleton, Status } from "../../components/Shell";
import { RuntimeControls } from "../runtime/RuntimeScreens";
import { OPERATIONS_URL, globalActionUrl } from "../../navigation/routes";
import { NavLink } from "../../navigation/useNavigation";

export const SECTIONS: readonly {lane:Lane; title:string; icon:string; description:string}[] = [
  {lane:"repositories", title:"Repositories", icon:"⑂", description:"Git repositories"},
  {lane:"services", title:"Services", icon:"⬡", description:"Compose services and logs"},
  {lane:"jobs", title:"Jobs", icon:"▤", description:"Jobs, tasks and output"},
  {lane:"applications", title:"Applications", icon:"↗", description:"Private application links"},
  {lane:"configuration", title:"Configuration", icon:"⚙", description:"Project definitions"},
];

function chooseProject(projects:ProjectSummary[], id:string):ProjectSummary|undefined {
  return projects.find(project=>project.id===id);
}

export function Entry({runtime, projects, navigation}:{
  runtime:RuntimeSnapshot; projects:ProjectSummary[]; navigation:Navigator;
}) {
  if(runtime.phase==="inactive" && runtime.project_id===null)
    return <Shell title="No active project" navigation={navigation}>
      <Notice title="Choose a project">Activation is explicit. Browsing a project will not start it.</Notice>
      {projects.length===0 ? <p className="text-muted">No projects registered yet. <OldDashboardLink>Create or import a project</OldDashboardLink>.</p> :
        <ItemLink icon="▤" title="Select project" detail={projects.length+" available"} href={GLOBAL_PROJECTS} navigation={navigation}/>}
    </Shell>;
  if(runtime.phase!=="active" || !runtime.project_id || !isProjectId(runtime.project_id))
    return <Shell title="Runtime status" navigation={navigation}>
      <Notice title={"Runtime: " + runtime.phase} role={runtime.phase==="error"?"alert":"status"}>
        {runtime.phase==="error" ? "Recovery is required before activation. Review the controller-owned recovery action." :
          "DevStack is changing runtime state. No project has been assumed active."}
      </Notice>
      {runtime.phase==="error" &&
        <p className="actions"><NavLink to={globalActionUrl("reconcile")}
          navigate={navigation.navigate}>Review recovery →</NavLink></p>}
      <p className="actions"><NavLink to={OPERATIONS_URL}
        navigate={navigation.navigate}>Runtime operations →</NavLink></p>
      <ItemLink icon="▤" title="Browse projects" href={GLOBAL_PROJECTS} navigation={navigation}/>
    </Shell>;
  if (!chooseProject(projects, runtime.project_id))
    return <Shell title="Project unavailable" navigation={navigation}>
      <Notice title="Active project not in registry" role="alert">DevStack reported an active ID that is not in the project list. No fallback activation will occur.</Notice>
      <OldDashboardLink>Inspect current runtime</OldDashboardLink>
    </Shell>;
  return <ProjectHome id={runtime.project_id} runtime={runtime} navigation={navigation} projectName={chooseProject(projects,runtime.project_id)?.name}/>;
}

export function ProjectDirectory({projects,runtime,navigation}:{
  projects:ProjectSummary[];runtime:RuntimeSnapshot;navigation:Navigator;
}) {
  return <Shell title="Projects" subtitle="Viewing does not activate." navigation={navigation} back>
    <div className="section-head"><span>Registered</span><span className="count">{projects.length}</span></div>
    <div className="item-group">{projects.map(project=>
      <ItemLink key={project.id} icon="▤" title={project.name} detail={project.slug}
        href={projectUrl(project.id)} navigation={navigation}
        suffix={runtime.phase==="active" && runtime.project_id===project.id
          ? <Status text="Active" kind="positive"/> : undefined}/>)}</div>
    {projects.length===0 && <Notice title="No projects">Use the current dashboard to create or import your first project.</Notice>}
    <p className="actions"><OldDashboardLink>Create or import project (current UI) ↗</OldDashboardLink></p>
  </Shell>;
}

export function ProjectHome({id,runtime,navigation,projectName}:{
  id:string;runtime:RuntimeSnapshot;navigation:Navigator;projectName?:string;
}) {
  const loader=useCallback((signal:AbortSignal)=>readProject(id,signal),[id]);
  const detail=useResource(loader,20000);
  const active=runtime.phase==="active" && runtime.project_id===id;
  return <Shell title={detail.kind==="ready" ? detail.data.project.name : (projectName??"Project")}
    subtitle={detail.kind==="ready"?detail.data.project.slug:undefined}
    badge={<Status text={active?"Active":"Viewing only"} kind={active?"positive":"neutral"}/>}
    navigation={navigation} back={navigation.route.kind!=="entry"}>
    {detail.kind==="loading" && <Skeleton/>}
    {detail.kind==="error" && <Notice title="Project detail unavailable" role="alert">{detail.error.message}</Notice>}
    {detail.kind==="ready" && <ProjectSections detail={detail.data} navigation={navigation}/>}
    {runtime.phase==="inactive" && <Notice title="Inactive runtime">This project is not running. Activation requires an explicit confirmation.</Notice>}
    {runtime.phase==="active" && !active && <Notice title="Another project is active">This project is for inspection only. Switching requires an explicit controller operation.</Notice>}
    {runtime.phase!=="inactive" && runtime.phase!=="active" &&
      <Notice title={"Runtime: "+runtime.phase} role={runtime.phase==="error"?"alert":"status"}>No activation is available while the runtime is transitioning or requires recovery.</Notice>}
    <RuntimeControls projectId={id} runtime={runtime} navigation={navigation}/>
    <p className="actions"><NavLink to={OPERATIONS_URL} navigate={navigation.navigate}>Recent runtime operations →</NavLink></p>
  </Shell>;
}
function ProjectSections({detail,navigation}:{detail:ProjectDetail;navigation:Navigator}) {
  return <><div className="section-head">Workspace</div><div className="item-group">
    {SECTIONS.map(section=>{
      const count=section.lane==="repositories"?detail.repositories.length:
        section.lane==="applications"?detail.applications.length:
        section.lane==="configuration"?detail.tasks.length:undefined;
      const description=count===undefined?section.description:
        section.lane==="configuration"?count+" saved tasks":count+" registered";
      return <ItemLink key={section.lane} href={laneUrl(detail.project.id,section.lane)}
        navigation={navigation} icon={section.icon} title={section.title} detail={description}/>;
    })}
  </div></>;
}

export function ProjectFeature({id,lane,navigation,runtime}:{
  id:string;lane:Lane;navigation:Navigator;runtime:RuntimeSnapshot;
}) {
  const loader=useCallback((signal:AbortSignal)=>readProject(id,signal),[id]);
  const detail=useResource(loader,20000);
  const meta=SECTIONS.find(x=>x.lane===lane)!;
  return <Shell title={meta.title} navigation={navigation} back
    subtitle={detail.kind==="ready"?detail.data.project.name:undefined}
    badge={<Status text={runtime.phase==="active"&&runtime.project_id===id?"Active":"Viewing"} kind={runtime.phase==="active"&&runtime.project_id===id?"positive":"neutral"}/>}>
    {detail.kind==="loading" && <Skeleton/>}
    {detail.kind==="error" && <Notice title="Project detail unavailable" role="alert">{detail.error.message}</Notice>}
    {detail.kind==="ready" && <>
      {lane==="configuration" && <div className="card">
        <div className="property"><span>Repositories</span><strong>{detail.data.repositories.length}</strong></div>
        <div className="property"><span>Saved tasks</span><strong>{detail.data.tasks.length}</strong></div>
        <div className="property"><span>Registered applications</span><strong>{detail.data.applications.length}</strong></div>
      </div>}
    </>}
    <p className="actions"><OldDashboardLink projectId={id}>Open {meta.title.toLowerCase()} in current UI ↗</OldDashboardLink></p>
  </Shell>;
}
