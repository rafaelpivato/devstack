import { apiRequest } from "./client";
import { isProjectId } from "../navigation/routes";

const project = (id:string) => {
  if (!isProjectId(id)) throw Error("Invalid project ID");
  return "/api/v2/projects/" + id;
};
const jobId = (id:string) => {
  if (!isProjectId(id)) throw Error("Invalid job ID");
  return id;
};
const containerId = (id:string) => {
  if (!/^[a-f0-9]{64}$/.test(id)) throw Error("Invalid container ID");
  return id;
};
export interface ContainerRow {
  id:string;name:string;state:string;health:string|null;restart_count:number;
}
export interface ServiceRow {
  name:string;status:string;containers:ContainerRow[];
}
export interface ServicesSnapshot {project_id:string;active:boolean;services:ServiceRow[]}
export interface ServiceLogSnapshot {container_id:string;service:string;text:string;truncated:boolean;tail:number}
export interface PrivateApplication {name:string;service:string;container_port:number;host_port:number;url:string}
export interface PrivatePort {service:string;container_port:number;host_port:number;url:string}
export interface PrivatePortsSnapshot {
  project_id:string;active:boolean;tailscale_available:boolean;
  ports:PrivatePort[];applications:PrivateApplication[];
}
export interface JobRow {
  id:string;project_id:string;kind:string;status:string;action:string;
  cancel_policy:string;started_ns:number;finished_ns:number|null;exit_code:number|null;
}
export interface JobList {jobs:JobRow[]}
export interface JobLogChunk {text:string;offset:number;next_offset:number;complete:boolean}
export interface JobSubmission {job_id:string}
export type ComposeAction="build"|"restart";
export type ServiceAction="restart"|"redeploy";
export type AdhocKind="run"|"image";
export interface AdhocSpec {
  kind:AdhocKind;service:string;image:string;network:"none"|"project";command:string[];
}
export const readServices=(id:string,signal?:AbortSignal)=>
  apiRequest<ServicesSnapshot>(project(id)+"/services",{signal});
export const readServiceLogs=(id:string,container:string,tail:number,signal?:AbortSignal)=>
  apiRequest<ServiceLogSnapshot>(project(id)+"/services/"+containerId(container)+"/logs?tail="+tail,{signal});
export const readPrivatePorts=(id:string,signal?:AbortSignal)=>
  apiRequest<PrivatePortsSnapshot>(project(id)+"/private-ports",{signal});
export const readJobs=(id:string,signal?:AbortSignal)=>
  apiRequest<JobList>(project(id)+"/jobs",{signal});
export const readJob=(id:string,job:string,signal?:AbortSignal)=>
  apiRequest<JobRow>(project(id)+"/jobs/"+jobId(job),{signal});
export const readJobLogs=(id:string,job:string,offset:number,signal?:AbortSignal)=>{
  if(!Number.isInteger(offset)||offset<0||offset>2*1024*1024) throw Error("Invalid log offset");
  return apiRequest<JobLogChunk>(project(id)+"/jobs/"+jobId(job)+"/logs?offset="+offset,{signal});
};
export const runTask=(id:string,name:string)=>
  apiRequest<JobSubmission>(project(id)+"/tasks/"+encodeURIComponent(name)+"/run",{method:"POST"});
export const runCompose=(id:string,action:ComposeAction)=>
  apiRequest<JobSubmission>(project(id)+"/compose/"+action,{method:"POST"});
export const runService=(id:string,service:string,action:ServiceAction)=>
  apiRequest<JobSubmission>(project(id)+"/services/"+encodeURIComponent(service)+"/actions/"+action,
    {method:"POST"});
export const runAdhoc=(id:string,spec:AdhocSpec)=>
  apiRequest<JobSubmission>(project(id)+"/adhoc",{method:"POST",body:spec});
export const cancelJob=(id:string,job:string)=>
  apiRequest<JobRow>(project(id)+"/jobs/"+jobId(job)+"/cancel",{method:"POST"});

/** Links come from controller-verified Tailscale mapping, never definitions alone. */
export function isPrivateApplicationUrl(raw:string):boolean {
  try {
    const url=new URL(raw);
    if(url.protocol!=="http:"&&url.protocol!=="https:") return false;
    if(url.username||url.password||url.hash||url.search) return false;
    const parts=url.hostname.split(".").map(Number);
    return parts.length===4 && parts.every(n=>Number.isInteger(n)&&n>=0&&n<=255)
      &&parts[0]===100&&parts[1]>=64&&parts[1]<=127;
  } catch {return false;}
}
