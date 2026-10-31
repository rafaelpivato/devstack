import { apiRequest } from "./client";
import { isProjectId } from "../navigation/routes";

export interface RepositorySummary { id:string; name:string; relative_path:string }
export interface ApplicationSummary {name:string;url?:string;service?:string;port?:number|null}
export interface TaskSummary { name:string; service?:string; mode?:string }
export interface ProjectDetail {
  project:{id:string; name:string; slug:string};
  repositories:RepositorySummary[];
  linked_source:boolean;
  applications:ApplicationSummary[];
  tasks:TaskSummary[];
  last_repository_update?:unknown;
}
export function readProject(id:string, signal?:AbortSignal):Promise<ProjectDetail> {
  if (!isProjectId(id)) return Promise.reject(new Error("Invalid project ID"));
  return apiRequest<ProjectDetail>("/api/v2/projects/" + id, { signal });
}
