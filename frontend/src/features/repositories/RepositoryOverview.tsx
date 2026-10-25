import { useCallback, useState, type ReactNode } from "react";
import { readProject } from "../../api/projects";
import { readRuntime, type RuntimeSnapshot } from "../../api/runtime";
import {
  repositoryStatus, updateRepository, checkRepository, saveRepositorySyncPolicy,
  readRepositoryReactions, setRepositoryReaction, type ServiceReaction,
  type RepositoryReactions,
  type RepositorySyncPolicy, previewBranch, applyBranch, reviewSource, applySource,
  submitRepoAdd, submitRepoBulk, repoAddReceipt, repoBulkReceipt,
  type RepositoryStatus, type BranchPreview, type BranchApplyResult, type SourceReviewResult,
  type SourceApplyResult, type RepositoryAddReceipt, type RepositoryBulkReceipt,
} from "../../api/repositories";
import { ItemLink, Notice, Shell, Skeleton, Status } from "../../components/Shell";
import { useResource } from "../../hooks/useResource";
import type { Navigator } from "../../navigation/useNavigation";
import { NavLink } from "../../navigation/useNavigation";
import {
  laneUrl, repoUrl, repoAddUrl, repoBulkUrl, repoBranchUrl, repoSourceUrl, repoReceiptUrl, jobUrl,
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
  const reactionLoad=useCallback((signal:AbortSignal)=>readRepositoryReactions(id,repositoryId,signal),[id,repositoryId]);
  const reactionResource=useResource(reactionLoad,20000);
  const [reactionResult,setReactionResult]=useState<RepositoryReactions|null>(null);
  const [reactionBusy,setReactionBusy]=useState<string|null>(null);
  const [reactionError,setReactionError]=useState<string|null>(null);
  const reactions=reactionResult??(reactionResource.kind==="ready"?reactionResource.data:null);
  const saveReaction=async(service:string,action:ServiceReaction)=>{
    if(reactionBusy!==null)return;
    setReactionBusy(service);setReactionError(null);
    try{setReactionResult(await setRepositoryReaction(id,repositoryId,service,action));}
    catch(e){setReactionError(errorMessage(e));}
    finally{setReactionBusy(null);}
  };
  const [result,setResult]=useState<RepositoryStatus|null>(null);
  const [error,setError]=useState<string|null>(null);
  const [busy,setBusy]=useState(false);
  const [selection,setSelection]=useState<RepositorySyncPolicy|null>(null);
  const state=result??(resource.kind==="ready"?resource.data:null);
  const selectedPolicy=selection??state?.sync?.policy??"manual";
  const check=async()=>{
    if(!canUpdateRepository(runtime,id)||busy)return;
    setBusy(true);setError(null);
    try{
      await verifyRepositoryUpdate(id);
      setResult(await checkRepository(id,repositoryId));
    }catch(e){setError(errorMessage(e)+" Inspect the repository before retrying.");}
    finally{setBusy(false);}
  };
  const savePolicy=async()=>{
    if(!state||busy||selection===null)return;
    setBusy(true);setError(null);
    try{
      const sync=await saveRepositorySyncPolicy(id,repositoryId,selectedPolicy);
      setResult({...state,sync});setSelection(null);
    }catch(e){setError(errorMessage(e));}
    finally{setBusy(false);}
  };
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
    {state&&<section className="card">
      <h3>Repository synchronization</h3>
      <p className="text-muted">Fetch remote changes without switching projects. Only explicitly configured service reactions may restart or redeploy Compose; managed configuration is not imported.</p>
      <label>Automatic synchronization
        <select value={selectedPolicy} onChange={e=>setSelection(e.target.value as RepositorySyncPolicy)} disabled={busy}>
          <option value="manual">Manual only (default)</option>
          <option value="check">Check automatically every 5 minutes</option>
          <option value="update">Fast-forward automatically every 5 minutes when safe</option>
        </select>
      </label>
      <div className="repo-actions">
        <ActionButton onClick={()=>void savePolicy()} disabled={busy||selection===null||selectedPolicy===state.sync?.policy}>Save policy</ActionButton>
        <ActionButton onClick={()=>void check()} disabled={busy||!canUpdateRepository(runtime,id)}>{busy?"Working…":"Check remote now"}</ActionButton>
      </div>
      <div className="property"><span>Last result</span><strong>{state.sync?.status??"idle"}{state.sync?.error_code?" ("+state.sync.error_code+")":""}</strong></div>
      <div className="property"><span>Last check</span><span>{state.sync?.last_checked_ns?new Date(state.sync.last_checked_ns/1e6).toLocaleString():"Never"}</span></div>
      <div className="property"><span>Last fast-forward</span><span>{state.sync?.last_updated_ns?new Date(state.sync.last_updated_ns/1e6).toLocaleString():"Never"}</span></div>
      {(state.sync?.events??[]).slice(0,3).map((event,i)=><div className="property" key={event.at_ns+"-"+i}>
        <span>{event.outcome}{event.reason?" / "+event.reason:""}</span><code>{event.head?.slice(0,12)??"—"}</code>
      </div>)}
      {state.sync?.status==="interrupted"&&<Notice title="Manual review required">Automatic updates remain paused for this repository until an explicit check or update.</Notice>}
    </section>}
    {state&&<section className="card">
      <h3>After source update</h3>
      <p className="text-muted">Bind this repository to actual Compose services. None is the default. Only a changed Git HEAD triggers an automatic reaction while this project is active.</p>
      {reactionResource.kind==="loading"&&!reactions&&<Skeleton/>}
      {reactionResource.kind==="error"&&!reactions&&
        <Notice title="Service reactions unavailable" role="alert">{reactionResource.error.message}</Notice>}
      {reactions&&reactions.services.length===0&&<p className="text-muted">No Compose services configured.</p>}
      {reactions?.services.map(item=><label key={item.name} className="field-inline">
        {item.name}
        <select aria-label={"Reaction for "+item.name} value={item.action}
          disabled={reactionBusy!==null}
          onChange={e=>void saveReaction(item.name,e.target.value as ServiceReaction)}>
          <option value="none">None</option>
          <option value="restart">Restart</option>
          <option value="redeploy">Redeploy</option>
        </select>
      </label>)}
      {reactionBusy&&<p className="text-muted">Saving {reactionBusy}…</p>}
      {reactionError&&<Notice title="Reaction was not saved" role="alert">{reactionError}</Notice>}
      {(reactions?.events??[]).slice(0,5).map((event,i)=>
        <div className="property" key={event.at_ns+"-"+i}>
          <span>{event.service} · {event.action} · {event.job_status??event.status}</span>
          {event.job_id?<NavLink navigate={navigation.navigate} to={jobUrl(id,event.job_id)}>Job details</NavLink>:
            <small>{event.reason??event.head.slice(0,12)}</small>}
        </div>)}
    </section>}
    {state&&<ActionButton onClick={()=>void update()} disabled={!ready||busy}>
      {busy?"Checking Git…":"Fetch and fast-forward"}
    </ActionButton>}
    {state&&canUpdateRepository(runtime,id)&&!ready&&<p className="text-muted">Requires a clean branch tracking origin without diverged history.</p>}
    {result&&<Notice title="Repository checked">Current HEAD: {result.head.slice(0,12)}. Reload status before another change.</Notice>}
    <ActionError message={error}/>
    <LinkRow to={repoBranchUrl(id)} label="Review branch change" navigation={navigation}/>
  </Context>;
}
