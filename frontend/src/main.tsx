import { createRoot } from "react-dom/client";
import { useCallback, useEffect } from "react";
import { watchAppearance } from "./lib/appearance";
import { readRuntime, readProjects } from "./api/runtime";
import { useResource } from "./hooks/useResource";
import { Shell, Notice, Skeleton } from "./components/Shell";
import { Entry, ProjectDirectory, ProjectFeature, ProjectHome } from "./features/projects/ProjectScreens";
import { useNavigation } from "./navigation/useNavigation";
import { routeProjectId } from "./navigation/routes";
import { RuntimeActionScreen, RuntimeOperations, RuntimeOperationDetail } from "./features/runtime/RuntimeScreens";
import { RepositoryScreen, RepositoryAdd, RepositoryBulk, RepositoryReceipt, BranchReview, SourceReview } from "./features/repositories/RepositoryScreens";
import { ServicesScreen, ServiceLogsScreen } from "./features/workloads/ServiceScreens";
import { ApplicationsScreen } from "./features/workloads/ApplicationScreens";
import { JobsScreen, JobDetail, JobLogs, RunJob } from "./features/workloads/JobScreens";
import { ConfigurationHome, ComposeEditor, DefinitionsEditor, ProjectNameEditor } from "./features/configuration/ProjectConfiguration";
import { WebhookEditor } from "./features/configuration/WebhookEditor";
import { AdministrationHome, GitHubSSHScreen, UpdatesScreen } from "./features/configuration/Administration";
import {DiagnosticsScreen} from "./features/configuration/Diagnostics";
import {CreateProjectScreen,ImportProjectScreen,ImportReceiptScreen} from "./features/projects/ProjectManagement";
import "./styles.css";

/** Staged v0.6 shell: only existing read endpoints; current / dashboard stays live. */
export function App() {
  useEffect(watchAppearance,[]);
  const navigation=useNavigation();
  const loadRuntime=useCallback(readRuntime,[]);
  const loadProjects=useCallback(readProjects,[]);
  const runtime=useResource(loadRuntime,8000);
  const projects=useResource(loadProjects,20000);
  // Diagnostics must remain reachable with no active project or failed controller.
  if(navigation.route.kind==="administration")return <AdministrationHome navigation={navigation}/>;
  if(navigation.route.kind==="admin-detail"&&navigation.route.screen==="diagnostics")
    return <DiagnosticsScreen navigation={navigation}/>;
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
  if(route.kind==="runtime-operations") return <RuntimeOperations navigation={navigation}/>;
  if(route.kind==="runtime-operation") return <RuntimeOperationDetail id={route.operationId} navigation={navigation}/>;
  if(route.kind==="runtime-global-action") return <RuntimeActionScreen key={route.action}
    action={route.action} projectId={null} runtime={current} projects={list} navigation={navigation}/>;
  if(route.kind==="runtime-action") return <RuntimeActionScreen key={route.projectId+route.action}
    action={route.action} projectId={route.projectId} runtime={current} projects={list} navigation={navigation}/>;
  if(route.kind==="admin-detail")
    return route.screen==="ssh"?<GitHubSSHScreen navigation={navigation}/>:
      <UpdatesScreen runtime={current} navigation={navigation}/>;
  if(route.kind==="entry") return <Entry runtime={current} projects={list} navigation={navigation}/>;
  if(route.kind==="project-create")return <CreateProjectScreen runtime={current} navigation={navigation}/>;
  if(route.kind==="project-import")return <ImportProjectScreen runtime={current} navigation={navigation}/>;
  if(route.kind==="project-import-receipt")return <ImportReceiptScreen id={route.operationId} navigation={navigation}/>;
  if(route.kind==="projects") return <ProjectDirectory projects={list} runtime={current} navigation={navigation}/>;
  const id=routeProjectId(route);
  if(!id) return null;
  if(route.kind==="project") return <ProjectHome key={id} id={id} runtime={current} navigation={navigation}
    projectName={list.find(x=>x.id===id)?.name}/>;
  if(route.kind==="lane"&&route.lane==="configuration")
    return <ConfigurationHome id={id} runtime={current} navigation={navigation}/>;
  if(route.kind==="config-detail"){
    if(route.screen==="compose")return <ComposeEditor key={id} id={id} runtime={current} navigation={navigation}/>;
    if(route.screen==="definitions")return <DefinitionsEditor key={id} id={id} runtime={current} navigation={navigation}/>;
    if(route.screen==="webhooks")return <WebhookEditor key={id} id={id} navigation={navigation}/>;
    return <ProjectNameEditor key={id} id={id} runtime={current} navigation={navigation}/>;
  }
  if(route.kind==="lane" && route.lane==="repositories")
    return <RepositoryScreen key={id} id={id} runtime={current} navigation={navigation}/>;
  if(route.kind==="repository")
    return <RepositoryScreen key={id+route.repositoryId} id={id} repositoryId={route.repositoryId}
      runtime={current} navigation={navigation}/>;
  if(route.kind==="repo-add") return <RepositoryAdd id={id} runtime={current} navigation={navigation}/>;
  if(route.kind==="repo-bulk") return <RepositoryBulk id={id} runtime={current} navigation={navigation}/>;
  if(route.kind==="repo-receipt") return <RepositoryReceipt id={id} mode={route.mode}
      operationId={route.operationId} navigation={navigation}/>;
  if(route.kind==="repo-branch") return <BranchReview id={id} runtime={current} navigation={navigation}/>;
  if(route.kind==="repo-source") return <SourceReview id={id} runtime={current} navigation={navigation}/>;
  if(route.kind==="lane"&&route.lane==="services")
    return <ServicesScreen id={id} runtime={current} navigation={navigation}/>;
  if(route.kind==="service-logs") return <ServiceLogsScreen key={id+route.containerId} id={id}
    containerId={route.containerId} runtime={current} navigation={navigation}/>;
  if(route.kind==="lane"&&route.lane==="applications")
    return <ApplicationsScreen id={id} runtime={current} navigation={navigation}/>;
  if(route.kind==="lane"&&route.lane==="jobs")
    return <JobsScreen id={id} runtime={current} navigation={navigation}/>;
  if(route.kind==="job-detail") return <JobDetail key={id+route.jobId}
    id={id} jobId={route.jobId} runtime={current} navigation={navigation}/>;
  if(route.kind==="job-logs") return <JobLogs key={id+route.jobId} id={id}
    jobId={route.jobId} navigation={navigation}/>;
  if(route.kind==="job-run") return <RunJob key={id+route.mode}
    id={id} mode={route.mode} runtime={current} navigation={navigation}/>;
  if(route.kind==="lane") return <ProjectFeature key={id+route.lane} id={id}
    lane={route.lane} navigation={navigation} runtime={current}/>;
  return null;
}

const mount=document.getElementById("app");
if(mount) createRoot(mount).render(<App/>);
