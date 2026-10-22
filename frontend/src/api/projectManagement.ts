import {apiRequest} from "./client";
import {isProjectId} from "../navigation/routes";

export interface ImportReceipt {
  id:string;slug:string;name:string;status:"queued"|"running"|"succeeded"|"failed";
  project_id:string|null;error_code:string|null;started_ns:number;finished_ns:number|null;
}
export interface ProjectCreation {project:{id:string;name:string}}
export function createProject(slug:string,name:string):Promise<ProjectCreation>{
  return apiRequest("/api/v2/projects",{method:"POST",body:{slug,name}});
}
export function submitProjectImport(url:string,slug:string,name:string,key:string){
  return apiRequest<{import:ImportReceipt}>("/api/v2/projects/import",{
    method:"POST",body:{url,slug,name},idempotencyKey:key,
  });
}
export function readProjectImport(id:string,signal?:AbortSignal){
  if(!isProjectId(id))return Promise.reject(new Error("Invalid import receipt identity"));
  return apiRequest<ImportReceipt>("/api/v2/imports/"+id,{signal});
}
export const projectSlugValid=(value:string)=>/^[a-z][a-z0-9-]{0,62}$/.test(value);
export const projectNameValid=(value:string)=>value.trim().length>0&&value===value.trim()&&value.length<=120;
export function githubSourceValid(value:string):boolean {
  try {
    const url=new URL(value);
    if(url.protocol!=="https:"||url.hostname!=="github.com"||url.username||
       url.password||url.search||url.hash||value.includes("%"))return false;
    const pieces=url.pathname.split("/").filter(Boolean);
    if(pieces.length<2||pieces.length>10)return false;
    const [owner,repo,...rest]=pieces;
    if(!/^[A-Za-z0-9-]{1,39}$/.test(owner)||owner.startsWith("-")||owner.endsWith("-")||
       !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$/.test(repo))return false;
    if(rest.length>0&&(rest.length<2||rest[0]!=="tree"||
        rest.slice(1).some(s=>!s||s==="."||s===".."||!/^[A-Za-z0-9_.-]+$/.test(s))))return false;
    return true;
  }catch{return false;}
}
