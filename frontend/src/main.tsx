import { createRoot } from "react-dom/client";
import { useCallback } from "react";
import { readRuntime, readProjects } from "./api/runtime";
import { useResource } from "./hooks/useResource";
import { Shell, Notice, Skeleton } from "./components/Shell";
import { Entry, ProjectDirectory, ProjectFeature, ProjectHome } from "./features/projects/ProjectScreens";
import { useNavigation } from "./navigation/useNavigation";
import { routeProjectId } from "./navigation/routes";
import "./styles.css";

/** Staged v0.6 shell: only existing read endpoints; current / dashboard stays live. */
export function App() {
  const navigation=useNavigation();
  const loadRuntime=useCallback(readRuntime,[]);
  const loadProjects=useCallback(readProjects,[]);
  const runtime=useResource(loadRuntime,8000);
  const projects=useResource(loadProjects,20000);
  if(navigation.route.kind==="not-found") return <Shell title="Page not found" navigation={navigation} back>
    <Notice title="Unknown page" role="alert">This URL is not a DevStack v0.6 screen.</Notice>
  </Shell>;
  if(runtime.kind==="loading" || projects.kind==="loading") return <Shell title="Connecting" navigation={navigation}><Skeleton/></Shell>;
  if(runtime.kind==="error" || projects.kind==="error") {
    const error=runtime.kind==="error"?runtime.error:projects.kind==="error"?projects.error:null;
    return <Shell title="Connection unavailable" navigation={navigation}>
      <Notice title="Cannot confirm runtime state" role="alert">{error?.message??"Controller unavailable."} No project is assumed inactive.</Notice>
      <p className="text-muted">Reconnect and refresh the page when the controller is available.</p>
    </Shell>;
  }
  const current=runtime.data, list=projects.data.projects;
  const route=navigation.route;
  if(route.kind==="entry") return <Entry runtime={current} projects={list} navigation={navigation}/>;
  if(route.kind==="projects") return <ProjectDirectory projects={list} runtime={current} navigation={navigation}/>;
  const id=routeProjectId(route);
  if(!id) return null;
  if(route.kind==="project") return <ProjectHome key={id} id={id} runtime={current} navigation={navigation}
    projectName={list.find(x=>x.id===id)?.name}/>;
  if(route.kind==="lane"||route.kind==="repository") return <ProjectFeature key={id+("lane" in route?route.lane:"repo")} id={id}
    lane={route.kind==="lane"?route.lane:"repositories"}
    repoId={route.kind==="repository"?route.repositoryId:undefined}
    navigation={navigation} runtime={current}/>;
  return null;
}

const mount=document.getElementById("app");
if(mount) createRoot(mount).render(<App/>);
