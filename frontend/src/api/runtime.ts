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
