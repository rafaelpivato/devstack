import { apiRequest } from "./client";
import { isProjectId } from "../navigation/routes";

const project=(id:string)=>{if(!isProjectId(id))throw Error("Invalid project ID");return "/api/v2/projects/"+id;};
export interface ComposeDocument {content:string;revision:string}
export interface ApplicationDefinition {name:string;service:string;port?:number|null}
export interface TaskDefinition {name:string;service:string;mode:string;image:string;network:string;command:string[]}
export interface SettingsCollection {applications:ApplicationDefinition[];tasks:TaskDefinition[]}
export interface ReviewedSettings extends SettingsCollection {revision:string}
export interface WebhookRoute {route:string;service:string;port:number;destination:string}
export interface WebhookList {webhooks:WebhookRoute[]}
export interface GitHubIdentity {public_key:string|null}
export interface GitHubTestResult {authenticated_user:string}
export interface ReleaseStatus {
  installed:string;latest:string|null;update_available:boolean;
  update:{phase:string;unit_state:string;last_result:string;last_exit_code:string}|null;
  check_error:string|null;
}
export const readCompose=(id:string,signal?:AbortSignal)=>
  apiRequest<ComposeDocument>(project(id)+"/compose",{signal});
export const saveCompose=(id:string,content:string,revision:string)=>
  apiRequest<ComposeDocument>(project(id)+"/compose",{method:"PUT",body:{content,revision}});
export const readReviewedSettings=(id:string,signal?:AbortSignal)=>
  apiRequest<ReviewedSettings>(project(id)+"/settings-review",{signal});
export const saveReviewedSettings=(id:string,data:ReviewedSettings)=>
  apiRequest<SettingsCollection>(project(id)+"/settings-review",{method:"PUT",body:data});
export const readWebhooks=(id:string,signal?:AbortSignal)=>
  apiRequest<WebhookList>(project(id)+"/webhooks",{signal});
export const putWebhook=(id:string,route:string,service:string,port:number,destination:string)=>
  apiRequest<WebhookRoute>(project(id)+"/webhooks/"+encodeURIComponent(route),
    {method:"PUT",body:{service,port,destination}});
export const deleteWebhook=(id:string,route:string)=>
  apiRequest<{removed:boolean}>(project(id)+"/webhooks/"+encodeURIComponent(route),{method:"DELETE"});
export const renameProject=(id:string,name:string)=>
  apiRequest<{project:{id:string;name:string;slug:string}}>(project(id),
    {method:"PATCH",body:{name}});
export const readGitHubIdentity=(signal?:AbortSignal)=>
  apiRequest<GitHubIdentity>("/api/v2/github/ssh",{signal});
export const createGitHubIdentity=()=>
  apiRequest<GitHubIdentity>("/api/v2/github/ssh/generate",{method:"POST"});
export const testGitHubIdentity=()=>
  apiRequest<GitHubTestResult>("/api/v2/github/ssh/test",{method:"POST"});
export const readReleaseStatus=(check:boolean,signal?:AbortSignal)=>
  apiRequest<ReleaseStatus>("/api/v2/devstack/update-status"+(check?"?check=1":""),{signal});
export const startReleaseUpdate=()=>
  apiRequest<{message:string}>("/api/v2/devstack/update-now",{method:"POST",body:{}});
/** Strict contract before sending a collection-wide replacement. */
export function validateCollection(value:unknown):value is SettingsCollection {
  if(!value || typeof value!=="object" || Array.isArray(value))return false;
  const row=value as Record<string,unknown>;
  if(Object.keys(row).sort().join(",")!=="applications,tasks" ||
     !Array.isArray(row.applications) || !Array.isArray(row.tasks) ||
     row.applications.length>30 || row.tasks.length>40) return false;
  const apps=row.applications.every((a:unknown)=>{
    if(!a||typeof a!=="object"||Array.isArray(a))return false;
    const obj=a as Record<string,unknown>;
    return Object.keys(obj).every(k=>["name","service","port"].includes(k)) &&
      typeof obj.name==="string"&&typeof obj.service==="string" &&
      (obj.port===undefined||(Number.isInteger(obj.port)&&Number(obj.port)>0&&Number(obj.port)<=65535));
  });
  const tasks=row.tasks.every((t:unknown)=>{
    if(!t||typeof t!=="object"||Array.isArray(t))return false;
    const obj=t as Record<string,unknown>;
    return Object.keys(obj).every(k=>["name","service","mode","image","network","command"].includes(k)) &&
      ["name","service","mode","image","network"].every(k=>typeof obj[k]==="string") &&
      Array.isArray(obj.command) && obj.command.every((c:unknown)=>typeof c==="string");
  });
  return apps&&tasks;
}

export interface MCPStatus {
  gateway:"running"|"stopped";funnel:"enabled"|"disabled"|"unknown";
  url:string|null; request_pending:boolean; last_result:string|null; installed:boolean;
}
export const readMCPStatus=(signal?:AbortSignal)=>
  apiRequest<MCPStatus>("/api/v2/devstack/mcp",{signal});
export const requestMCPAction=(action:"enable"|"disable")=>
  apiRequest<{message:string;request_pending:true}>("/api/v2/devstack/mcp/"+action,{method:"POST",body:{}});
