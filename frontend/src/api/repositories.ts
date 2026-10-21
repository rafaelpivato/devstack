import { apiRequest } from "./client";
import { isProjectId } from "../navigation/routes";
export interface RepositoryStatus {
  id:string;project_id:string;name:string;branch:string|null;head:string;upstream:string|null;
  dirty:boolean;ahead:number|null;behind:number|null;
}
export interface BranchEntry {
  repository_id:string;name:string;current_branch:string|null;head:string;dirty:boolean;
  default_branch:string;target_branch:string;fallback:boolean;target_head:string;remote_head:string;
  local_exists:boolean;action:"blocked"|"already"|"switch";
}
export interface BranchPreview {project_id:string;requested_branch:string;repository_id:string|null;
  entries:BranchEntry[];ready:boolean;revision:string}
export interface BranchApplyResult {project_id:string;requested_branch:string;complete:boolean;
  results:{repository_id:string;name:string;target_branch:string;fallback:boolean;status:string;head?:string}[]}
export interface SourceChange {id:string;kind:string;name:string;action:string;locally_modified:boolean;selectable:boolean}
export interface SourceReviewResult {project_id:string;source_repository_id:string;revision:string;changes:SourceChange[]}
export interface SourceApplyResult {project_id:string;applied:string[];review:SourceReviewResult}
export interface RepositoryAddReceipt {id:string;project_id:string;repo_id:string|null;name:string;
  status:string;error_code:string|null;started_ns:number;finished_ns:number|null}
export interface RepositoryBulkItem {repository_id:string;name:string;status:string;reason:string|null}
export interface RepositoryBulkReceipt {id:string;project_id:string;status:string;items:RepositoryBulkItem[];
  started_ns:number;finished_ns:number|null}
const base=(id:string)=>{
  if(!isProjectId(id)) throw new Error("Invalid project ID");
  return "/api/v2/projects/"+id;
};
const uuid=(id:string)=>{if(!isProjectId(id)) throw new Error("Invalid entity ID");return id;};
export const repositoryStatus=(projectId:string,repositoryId:string,signal?:AbortSignal)=>
  apiRequest<RepositoryStatus>(base(projectId)+"/repositories/"+uuid(repositoryId),{signal});
export const updateRepository=(projectId:string,repositoryId:string,expected_head:string)=>
  apiRequest<RepositoryStatus>(base(projectId)+"/repositories/"+uuid(repositoryId)+"/update",
    {method:"POST",body:{expected_head}});
export const previewBranch=(id:string,branch:string,repositoryId:string|null,signal?:AbortSignal)=>
  apiRequest<BranchPreview>(base(id)+"/git/branch-preview?"+
    new URLSearchParams({...{branch},...(repositoryId?{repository_id:uuid(repositoryId)}:{})}).toString(),{signal});
export const applyBranch=(id:string,branch:string,repositoryId:string|null,revision:string)=>
  apiRequest<BranchApplyResult>(base(id)+"/git/branch-apply",
    {method:"POST",body:{branch,repository_id:repositoryId,revision}});
export const reviewSource=(id:string,signal?:AbortSignal)=>
  apiRequest<SourceReviewResult>(base(id)+"/source-review",{signal});
export const applySource=(id:string,revision:string,selected:string[],overwrites:string[])=>
  apiRequest<SourceApplyResult>(base(id)+"/source-review/apply",
    {method:"POST",body:{revision,selected,overwrites}});
export const submitRepoAdd=(id:string,input:{url:string;name:string;branch:string},key:string)=>
  apiRequest<{import:RepositoryAddReceipt}>(base(id)+"/repositories",
    {method:"POST",body:input,idempotencyKey:key});
export const repoAddReceipt=(id:string,operationId:string,signal?:AbortSignal)=>
  apiRequest<RepositoryAddReceipt>(base(id)+"/repository-adds/"+uuid(operationId),{signal});
export const submitRepoBulk=(id:string,key:string)=>
  apiRequest<RepositoryBulkReceipt>(base(id)+"/repositories/update-all",
    {method:"POST",body:{},idempotencyKey:key});
export const repoBulkReceipt=(id:string,operationId:string,signal?:AbortSignal)=>
  apiRequest<RepositoryBulkReceipt>(base(id)+"/repository-updates/"+uuid(operationId),{signal});
