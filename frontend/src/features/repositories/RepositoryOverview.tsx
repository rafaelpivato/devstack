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


import { Context, WriteBoundary, ActionButton, LinkRow, ActionError, canWriteRepository, canUpdateRepository, verifyRepositoryUpdate, errorMessage, repositoryRoot } from './RepositoryCommon';
export function RepositoryScreen({id,repositoryId,runtime,navigation}:{
  id:string;repositoryId?:string;runtime:RuntimeSnapshot;navigation:Navigator;
}) {
  const load=useCallback((signal:AbortSignal)=>readProject(id,signal),[id]);
  const detail=useResource(load,20000);
  if(repositoryId) return <RepositoryDetail key={repositoryId} id={id} repositoryId={repositoryId}
    navigation={navigation} runtime={runtime}/>;
  return <Context id={id} title="Repositories" navigation={navigation}>
    {detail.kind==="loading"&&<Skeleton/>}
    {detail.kind==="error"&&<Notice title="Cannot read repositories" role="alert">{detail.error.message}</Notice>}
    {detail.kind==="ready"&&<>
      <div className="section-head"><span>{detail.data.project.name}</span><span>{detail.data.repositories.length} repositories</span></div>
      <div className="item-group">{detail.data.repositories.map(repo=>
        <ItemLink key={repo.id} href={repoUrl(id,repo.id)} navigation={navigation}
          icon="⑂" title={repo.name} detail={repo.relative_path}/>)}</div>
      {detail.data.repositories.length===0&&<Notice title="No repositories">Add a GitHub repository while the runtime is inactive.</Notice>}
      <WriteBoundary runtime={runtime}/>
      <div className="repo-actions">
        <LinkRow to={repoAddUrl(id)} label="Add repository" navigation={navigation}/>
        {detail.data.repositories.length>0&&<>
          <LinkRow to={repoBulkUrl(id)} label="Update all (review)" navigation={navigation}/>
          <LinkRow to={repoBranchUrl(id)} label="Review branch change" navigation={navigation}/>
        </>}
        {detail.data.linked_source&&<LinkRow to={repoSourceUrl(id)} label="Review imported configuration" navigation={navigation}/>}
        {typeof detail.data.last_repository_update==="object" && detail.data.last_repository_update!==null &&
          "id" in detail.data.last_repository_update &&
          typeof detail.data.last_repository_update.id==="string" &&
          <LinkRow to={repoReceiptUrl(id,"bulk",detail.data.last_repository_update.id)}
            label="Latest batch operation" navigation={navigation}/>}
      </div>
    </>}
  </Context>;
}
function RepositoryDetail({id,repositoryId,runtime,navigation}:{
  id:string;repositoryId:string;runtime:RuntimeSnapshot;navigation:Navigator;
}) {
  const load=useCallback((signal:AbortSignal)=>repositoryStatus(id,repositoryId,signal),[id,repositoryId]);
  const resource=useResource(load,20000);
  const [result,setResult]=useState<RepositoryStatus|null>(null);
  const [error,setError]=useState<string|null>(null);
  const [busy,setBusy]=useState(false);
  const state=result??(resource.kind==="ready"?resource.data:null);
  const ready=canUpdateRepository(runtime,id) && state && !state.dirty && !!state.branch &&
    state.upstream?.startsWith("origin/") && !((state.ahead??0)>0&&(state.behind??0)>0);
  const update=async()=>{
    if(!ready||!state||busy)return;
    setBusy(true);setError(null);setResult(null);
    try{
      await verifyRepositoryUpdate(id);
      const value=await updateRepository(id,repositoryId,state.head);
      setResult(value);
    }catch(e){setError(errorMessage(e)+" Inspect repository status before another update.");}
    finally{setBusy(false);}
  };
  return <Context id={id} navigation={navigation} title={state?.name??"Repository"}>
    {resource.kind==="loading"&&<Skeleton/>}
    {resource.kind==="error"&&<Notice title="Repository unavailable" role="alert">{resource.error.message}</Notice>}
    {state&&<section className="card">
      <div className="property"><span>Branch</span><strong>{state.branch??"Detached"}</strong></div>
      <div className="property"><span>Changes</span><Status text={state.dirty?"Dirty":"Clean"} kind={state.dirty?"warning":"positive"}/></div>
      <div className="property"><span>Tracking</span><code>{state.upstream??"No upstream"}</code></div>
      <div className="property"><span>Ahead / behind</span><strong>{state.ahead??"—"} / {state.behind??"—"}</strong></div>
      <div className="property"><span>HEAD</span><code>{state.head.slice(0,12)}</code></div>
    </section>}
    {!canUpdateRepository(runtime,id)&&<Notice title="Repository update unavailable">Activate this project or deactivate the other active project to update its files.</Notice>}
    {state&&<ActionButton onClick={()=>void update()} disabled={!ready||busy}>
      {busy?"Checking Git…":"Fetch and fast-forward"}
    </ActionButton>}
    {state&&canUpdateRepository(runtime,id)&&!ready&&<p className="text-muted">Requires a clean branch tracking origin without diverged history.</p>}
    {result&&<Notice title="Repository checked">Current HEAD: {result.head.slice(0,12)}. Reload status before another change.</Notice>}
    <ActionError message={error}/>
    <LinkRow to={repoBranchUrl(id)} label="Review branch change" navigation={navigation}/>
  </Context>;
}
