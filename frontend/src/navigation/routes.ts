/** Navigation is presentation state, never the global active runtime. */
export const PREVIEW_ROOT = "/__v06";
export const GLOBAL_PROJECTS_URL=PREVIEW_ROOT+"/projects";
export const CREATE_PROJECT_URL=GLOBAL_PROJECTS_URL+"/create";
export const IMPORT_PROJECT_URL=GLOBAL_PROJECTS_URL+"/import";
export function importReceiptUrl(id:string):string {
  if(!isProjectId(id))throw new Error("Invalid import operation");
  return PREVIEW_ROOT+"/imports/"+id;
}
export const LANES = ["repositories", "services", "jobs", "applications", "configuration"] as const;
export type Lane = typeof LANES[number];
export type AppRoute =
  | {kind: "entry"} | {kind:"projects"}
  | {kind:"project-create"} | {kind:"project-import"} | {kind:"project-import-receipt"; operationId:string}
  | {kind:"administration"} | {kind:"admin-detail";screen:"ssh"|"updates"|"diagnostics"}
  | {kind:"config-detail";projectId:string;screen:"compose"|"definitions"|"webhooks"|"name"}
  | {kind:"project"; projectId:string}
  | {kind:"lane"; projectId:string; lane:Lane}
  | {kind:"repository"; projectId:string; repositoryId:string}
  | {kind:"service-logs";projectId:string;containerId:string}
  | {kind:"job-detail";projectId:string;jobId:string}
  | {kind:"job-logs";projectId:string;jobId:string}
  | {kind:"job-run";projectId:string;mode:"task"|"compose"|"adhoc"}
  | {kind:"repo-add";projectId:string}
  | {kind:"repo-bulk";projectId:string}
  | {kind:"repo-receipt";projectId:string;mode:"add"|"bulk";operationId:string}
  | {kind:"repo-branch";projectId:string}
  | {kind:"repo-source";projectId:string}
  | {kind:"runtime-action"; action:"activate"|"switch"; projectId:string}
  | {kind:"runtime-global-action"; action:"deactivate"|"reconcile"}
  | {kind:"runtime-operations"}
  | {kind:"runtime-operation"; operationId:string}
  | {kind:"not-found"};

const uuid4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
export function isProjectId(value: string): boolean { return uuid4.test(value); }
export function isLane(value: string): value is Lane {
  return (LANES as readonly string[]).includes(value);
}
export function projectUrl(id: string): string {
  if (!isProjectId(id)) throw new Error("Invalid project identifier");
  return PREVIEW_ROOT + "/projects/" + id;
}
export function laneUrl(id: string, lane: Lane): string {
  return projectUrl(id) + "/" + lane;
}
export function repoUrl(id: string, repoId: string): string {
  if (!isProjectId(repoId)) throw new Error("Invalid repository identifier");
  return laneUrl(id, "repositories") + "/" + repoId;
}
export function repoAddUrl(id:string):string {return laneUrl(id,"repositories")+"/add";}
export function repoBulkUrl(id:string):string {return laneUrl(id,"repositories")+"/bulk";}
export function repoBranchUrl(id:string):string {return laneUrl(id,"repositories")+"/branch";}
export function repoSourceUrl(id:string):string {return laneUrl(id,"repositories")+"/source";}
export function repoReceiptUrl(id:string,mode:"add"|"bulk",operationId:string):string {
  if(!isProjectId(operationId)) throw new Error("Invalid operation identifier");
  return laneUrl(id,"repositories")+"/"+(mode==="add"?"adds":"updates")+"/"+operationId;
}
export function isContainerId(id:string):boolean{return /^[a-f0-9]{64}$/.test(id);}
export function serviceUrl(id:string,containerId:string):string {
  if(!isContainerId(containerId))throw Error("Invalid container identity");
  return laneUrl(id,"services")+"/"+containerId;
}
export function jobUrl(id:string,jobId:string):string {
  if(!isProjectId(jobId))throw Error("Invalid job identity");
  return laneUrl(id,"jobs")+"/"+jobId;
}
export function jobLogsUrl(id:string,jobId:string):string{return jobUrl(id,jobId)+"/logs";}
export function jobsRunUrl(id:string,mode:"task"|"compose"|"adhoc"):string {
  return laneUrl(id,"jobs")+"/run/"+mode;
}
export function operationUrl(id: string): string {
  if (!isProjectId(id)) throw new Error("Invalid operation identifier");
  return PREVIEW_ROOT + "/runtime/operations/" + id;
}
export function actionUrl(action: "activate"|"switch", projectId:string):string {
  return projectUrl(projectId) + "/runtime/" + action;
}
export function globalActionUrl(action:"deactivate"|"reconcile"):string {
  return PREVIEW_ROOT + "/runtime/" + action;
}
export const OPERATIONS_URL=PREVIEW_ROOT+"/runtime/operations";
export const ADMIN_ROOT=PREVIEW_ROOT+"/administration";
export function adminUrl(screen:"ssh"|"updates"|"diagnostics"):string{return ADMIN_ROOT+"/"+screen;}
export function configUrl(id:string,screen:"compose"|"definitions"|"webhooks"|"name"):string {
  return laneUrl(id,"configuration")+"/"+screen;
}
export function routeFor(pathname: string): AppRoute {
  if(pathname===CREATE_PROJECT_URL)return {kind:"project-create"};
  if(pathname===IMPORT_PROJECT_URL)return {kind:"project-import"};
  if(pathname.startsWith(PREVIEW_ROOT+"/imports/")){
    const id=pathname.slice((PREVIEW_ROOT+"/imports/").length);
    return isProjectId(id)?{kind:"project-import-receipt",operationId:id}:{kind:"not-found"};
  }
  if(pathname===ADMIN_ROOT)return {kind:"administration"};
  if(pathname===adminUrl("ssh"))return {kind:"admin-detail",screen:"ssh"};
  if(pathname===adminUrl("updates"))return {kind:"admin-detail",screen:"updates"};
  if(pathname===adminUrl("diagnostics"))return {kind:"admin-detail",screen:"diagnostics"};
  if (pathname === OPERATIONS_URL) return {kind:"runtime-operations"};
  if (pathname === globalActionUrl("deactivate")) return {kind:"runtime-global-action",action:"deactivate"};
  if (pathname === globalActionUrl("reconcile")) return {kind:"runtime-global-action",action:"reconcile"};
  if (pathname.startsWith(OPERATIONS_URL+"/")) {
    const id=pathname.slice((OPERATIONS_URL+"/").length);
    return isProjectId(id)?{kind:"runtime-operation",operationId:id}:{kind:"not-found"};
  }
  if (pathname === PREVIEW_ROOT || pathname === PREVIEW_ROOT + "/") return {kind:"entry"};
  if (pathname === PREVIEW_ROOT + "/projects") return {kind:"projects"};
  if (!pathname.startsWith(PREVIEW_ROOT + "/projects/")) return {kind:"not-found"};
  const segments = pathname.slice((PREVIEW_ROOT + "/projects/").length).split("/");
  if (!isProjectId(segments[0])) return {kind:"not-found"};
  const projectId = segments[0];
  if (segments.length === 1) return {kind:"project", projectId};
  if (segments.length === 3 && segments[1] === "runtime" &&
      (segments[2] === "activate" || segments[2] === "switch"))
    return {kind:"runtime-action",projectId,action:segments[2]};
  if (segments.length === 2 && isLane(segments[1]))
    return {kind:"lane", projectId, lane: segments[1]};
  if(segments.length===3 && segments[1]==="configuration" &&
    ["compose","definitions","webhooks","name"].includes(segments[2]))
    return {kind:"config-detail",projectId,screen:segments[2] as "compose"|"definitions"|"webhooks"|"name"};
  if(segments.length===3 && segments[1]==="services" && isContainerId(segments[2]))
    return {kind:"service-logs",projectId,containerId:segments[2]};
  if(segments.length===4 && segments[1]==="jobs" && segments[2]==="run" &&
      (segments[3]==="task"||segments[3]==="compose"||segments[3]==="adhoc"))
    return {kind:"job-run",projectId,mode:segments[3]};
  if(segments.length===4 && segments[1]==="jobs" && isProjectId(segments[2]) && segments[3]==="logs")
    return {kind:"job-logs",projectId,jobId:segments[2]};
  if(segments.length===3 && segments[1]==="jobs" && isProjectId(segments[2]))
    return {kind:"job-detail",projectId,jobId:segments[2]};
  if(segments.length===3 && segments[1]==="repositories"){
    if(segments[2]==="add") return {kind:"repo-add",projectId};
    if(segments[2]==="bulk") return {kind:"repo-bulk",projectId};
    if(segments[2]==="branch") return {kind:"repo-branch",projectId};
    if(segments[2]==="source") return {kind:"repo-source",projectId};
  }
  if(segments.length===4 && segments[1]==="repositories" && isProjectId(segments[3]) &&
      (segments[2]==="adds" || segments[2]==="updates"))
    return {kind:"repo-receipt",projectId,mode:segments[2]==="adds"?"add":"bulk",operationId:segments[3]};
  if (segments.length === 3 && segments[1] === "repositories" && isProjectId(segments[2]))
    return {kind:"repository", projectId, repositoryId: segments[2]};
  return {kind:"not-found"};
}
/** Used only for a fresh deep link; a user-initiated Back follows browser history. */
export function parentUrl(route: AppRoute): string {
  switch (route.kind) {
    case "project-create":case "project-import":return GLOBAL_PROJECTS_URL;
    case "project-import-receipt":return IMPORT_PROJECT_URL;
    case "administration":return PREVIEW_ROOT+"/projects";
    case "admin-detail":return ADMIN_ROOT;
    case "config-detail":return laneUrl(route.projectId,"configuration");
    case "runtime-operation": return OPERATIONS_URL;
    case "runtime-operations": case "runtime-global-action": return PREVIEW_ROOT;
    case "runtime-action": return projectUrl(route.projectId);
    case "service-logs": return laneUrl(route.projectId,"services");
    case "job-logs": return jobUrl(route.projectId,route.jobId);
    case "job-detail":case "job-run":return laneUrl(route.projectId,"jobs");
    case "repo-receipt": return route.mode==="add"?repoAddUrl(route.projectId):repoBulkUrl(route.projectId);
    case "repo-add":case "repo-bulk":case "repo-branch":case "repo-source":
    case "repository": return laneUrl(route.projectId, "repositories");
    case "lane": return projectUrl(route.projectId);
    case "project": return PREVIEW_ROOT + "/projects";
    default: return PREVIEW_ROOT;
  }
}
export function routeProjectId(route: AppRoute): string | null {
  if (["project","lane","repository","repo-add","repo-bulk","repo-receipt","repo-branch","repo-source","runtime-action","service-logs","job-detail","job-logs","job-run","config-detail"].includes(route.kind))
    return (route as {projectId:string}).projectId;
  return null;
}
