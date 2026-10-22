import { apiRequest } from "./client";

export type RuntimePhase = "inactive" | "activating" | "active" | "deactivating" | "switching" | "error";
export interface RuntimeSnapshot {
  phase: RuntimePhase;
  project_id: string | null;
  message?: string;
}
export interface ProjectSummary {
  id: string;
  slug: string;
  name: string;
}
export interface ProjectListResponse { projects: ProjectSummary[] }

export function readRuntime(signal?: AbortSignal): Promise<RuntimeSnapshot> {
  return apiRequest<RuntimeSnapshot>("/api/v2/runtime", { signal });
}
export function readProjects(signal?: AbortSignal): Promise<ProjectListResponse> {
  return apiRequest<ProjectListResponse>("/api/v2/projects", { signal });
}

export type TransitionAction = "activate"|"switch"|"deactivate"|"reconcile";
export type TransitionStatus = "queued"|"running"|"succeeded"|"failed"|"interrupted";
export interface TransitionReceipt {
  id:string; request_key:string; action:TransitionAction; project_id:string|null;
  status:TransitionStatus; result:RuntimeSnapshot|null; error_code:string;
  started_ns:number; finished_ns:number|null;
}
export interface TransitionList {operations:TransitionReceipt[]}
export function readTransitions(signal?:AbortSignal):Promise<TransitionList> {
  return apiRequest("/api/v2/runtime/transitions",{signal});
}
export function readTransition(id:string,signal?:AbortSignal):Promise<TransitionReceipt> {
  return apiRequest("/api/v2/runtime/transitions/"+id,{signal});
}
export function submitTransition(action:TransitionAction,projectId:string|null,key:string) {
  return apiRequest<{operation:TransitionReceipt}>("/api/v2/runtime/transitions",{
    method:"POST",body:{action,project_id:projectId}, idempotencyKey:key,
  });
}
