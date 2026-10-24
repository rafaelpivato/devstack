import { useCallback, useState, type ReactNode } from "react";
import { readProject } from "../../api/projects";
import { readRuntime, type RuntimeSnapshot } from "../../api/runtime";
import {
  repositoryStatus, updateRepository, previewBranch, applyBranch, reviewSource, applySource,
  submitRepoAdd, submitRepoBulk, repoAddReceipt, repoBulkReceipt,
  type RepositoryStatus, type BranchPreview, type BranchApplyResult, type SourceReviewResult,
  type SourceApplyResult, type RepositoryAddReceipt, type RepositoryBulkReceipt,
} from "../../api/repositories";
import { ItemLink, Notice, Shell, Skeleton, Status } from "../../components/Shell";
import { useResource } from "../../hooks/useResource";
import type { Navigator } from "../../navigation/useNavigation";
import { NavLink } from "../../navigation/useNavigation";
import {
  laneUrl, repoUrl, repoAddUrl, repoBulkUrl, repoBranchUrl, repoSourceUrl, repoReceiptUrl,
} from "../../navigation/routes";
import {
  requestKey, loadSubmission, saveSubmission, clearSubmission, type SavedRepositorySubmission,
} from "../../lib/repositorySubmission";

export const repositoryRoot=(id:string)=>laneUrl(id,"repositories");
export const canWriteRepository=(runtime:RuntimeSnapshot)=>
  runtime.phase==="inactive" && runtime.project_id===null;
export const canUpdateRepository=(runtime:RuntimeSnapshot,projectId:string)=>
  canWriteRepository(runtime) ||
  (runtime.phase==="active" && runtime.project_id===projectId);
export async function verifyRepositoryUpdate(projectId:string) {
  if(!canUpdateRepository(await readRuntime(),projectId))
    throw new Error("Repository update requires this project to be active or all projects inactive.");
}
export const errorMessage=(e:unknown)=>e instanceof Error?e.message:"Request could not be completed.";
export async function verifyInactive() {
  if(!canWriteRepository(await readRuntime()))
    throw new Error("Runtime is no longer inactive. Deactivate and review again.");
}
export function WriteBoundary({runtime}:{runtime:RuntimeSnapshot}) {
  return !canWriteRepository(runtime)?
    <Notice title="Repository changes unavailable">Deactivate the active runtime before changing repositories. Inspection remains available.</Notice>:null;
}
export function ActionError({message}:{message:string|null}) {
  return message?<Notice title="Action not confirmed" role="alert">{message}</Notice>:null;
}
export function ActionButton({children,disabled,onClick}:{
  children:ReactNode;disabled?:boolean;onClick:()=>void;
}) {return <button type="button" className="primary-button" disabled={disabled} onClick={onClick}>{children}</button>;}
export function Context({id,navigation,children,title,subtitle}:{
  id:string;navigation:Navigator;children:React.ReactNode;title:string;subtitle?:string;
}) {
  return <Shell title={title} subtitle={subtitle} navigation={navigation} back>{children}</Shell>;
}
export function LinkRow({to,label,navigation}:{to:string;label:string;navigation:Navigator}) {
  return <p className="actions"><NavLink to={to} navigate={navigation.navigate}>{label} →</NavLink></p>;
}
