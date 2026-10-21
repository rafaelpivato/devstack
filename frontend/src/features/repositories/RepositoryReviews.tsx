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


import { Context, WriteBoundary, ActionButton, ActionError, canWriteRepository, verifyInactive, errorMessage } from './RepositoryCommon';
export function BranchReview({id,runtime,navigation}:{id:string;runtime:RuntimeSnapshot;navigation:Navigator}) {
  const load=useCallback((signal:AbortSignal)=>readProject(id,signal),[id]);
  const project=useResource(load,30000);
  const [branch,setBranch]=useState("");
  const [scope,setScope]=useState("");
  const [preview,setPreview]=useState<BranchPreview|null>(null);
  const [result,setResult]=useState<BranchApplyResult|null>(null);
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState<string|null>(null);
  const change=()=>{setPreview(null);setResult(null);setError(null);};
  const inspect=async()=>{
    if(!canWriteRepository(runtime)||!branch.trim()||busy)return;
    setBusy(true);change();
    try{await verifyInactive();setPreview(await previewBranch(id,branch,scope||null));}
    catch(e){setError(errorMessage(e));}finally{setBusy(false);}
  };
  const apply=async()=>{
    if(!preview?.ready||busy||!canWriteRepository(runtime))return;
    setBusy(true);setError(null);
    const reviewed=preview;setPreview(null); // Prevent reapplying stale inputs on an unknown POST outcome.
    try{
      await verifyInactive();
      setResult(await applyBranch(id,reviewed.requested_branch,reviewed.repository_id,reviewed.revision));
    }catch(e){setError(errorMessage(e)+" Inspect repository states and preview again; do not blindly retry.");}
    finally{setBusy(false);}
  };
  return <Context id={id} title="Review branch" navigation={navigation}>
    <WriteBoundary runtime={runtime}/>
    <Notice title="Inactive runtime required">Preview fetches remote references. Applying changes requires the exact reviewed revision and a clean checkout.</Notice>
    <div className="form-stack">
      <label>Branch<input value={branch} maxLength={200} autoCapitalize="off" autoCorrect="off"
        onChange={e=>{setBranch(e.target.value);change();}} placeholder="feature/my-change"/></label>
      {project.kind==="ready"&&<label>Repository
        <select value={scope} onChange={e=>{setScope(e.target.value);change();}}>
          <option value="">All repositories</option>
          {project.data.repositories.map(r=><option key={r.id} value={r.id}>{r.name}</option>)}
        </select>
      </label>}
    </div>
    <ActionButton onClick={()=>void inspect()} disabled={!canWriteRepository(runtime)||busy||!branch.trim()}>
      {busy?"Working…":"Preview branch"}
    </ActionButton>
    {preview&&<>
      <div className="section-head">Reviewed changes</div>
      <div className="item-group">{preview.entries.map(row=>
        <div key={row.repository_id} className="item-static">
          <div className="item-text">
            <strong>{row.name}</strong><small>{row.current_branch??"Detached"} → {row.target_branch}{row.fallback?" (default fallback)":""}</small>
          </div><Status text={row.action} kind={row.action==="blocked"?"warning":"neutral"}/>
        </div>)}</div>
      <p className="text-muted">Branch fallback and blocked entries are shown explicitly. Revision: {preview.revision.slice(0,12)}</p>
      <ActionButton disabled={!preview.ready||busy||!canWriteRepository(runtime)} onClick={()=>void apply()}>
        Apply reviewed branch change
      </ActionButton>
    </>}
    {result&&<Notice title={result.complete?"Branch change completed":"Partial branch result"} role={result.complete?"status":"alert"}>
      {result.results.map(r=>r.name+": "+r.status+(r.fallback?" (fallback)":"")).join("; ")}
    </Notice>}
    <ActionError message={error}/>
  </Context>;
}
export function SourceReview({id,runtime,navigation}:{id:string;runtime:RuntimeSnapshot;navigation:Navigator}) {
  const load=useCallback((signal:AbortSignal)=>reviewSource(id,signal),[id]);
  const source=useResource(load,60000);
  const [selected,setSelected]=useState<string[]>([]);
  const [overwrites,setOverwrites]=useState<string[]>([]);
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState<string|null>(null);
  const [result,setResult]=useState<SourceApplyResult|null>(null);
  const changes=source.kind==="ready"?source.data.changes:[];
  const selectable=changes.filter(c=>c.selectable);
  const selectedRows=selectable.filter(c=>selected.includes(c.id));
  const required=selectedRows.filter(c=>c.locally_modified).map(c=>c.id);
  const confirmed=required.every(id=>overwrites.includes(id));
  const apply=async()=>{
    if(source.kind!=="ready"||!canWriteRepository(runtime)||busy||selected.length===0||!confirmed)return;
    setBusy(true);setError(null);setResult(null);
    const revision=source.data.revision;
    try{
      await verifyInactive();
      setSelected([]);setOverwrites([]); // Do not allow resubmission of an unknown write.
      setResult(await applySource(id,revision,selected,required));
    }catch(e){setError(errorMessage(e)+" Review source changes again before retrying.");}
    finally{setBusy(false);}
  };
  return <Context id={id} title="Source review" navigation={navigation}>
    <WriteBoundary runtime={runtime}/>
    {source.kind==="loading"&&<Skeleton/>}
    {source.kind==="error"&&<Notice title="Cannot read source" role="alert">{source.error.message}</Notice>}
    {source.kind==="ready"&&<>
      <Notice title="Imported configuration">Choose changes to apply. Locally modified settings require explicit per-item overwrite approval; stale revisions are rejected by the controller.</Notice>
      {changes.length===0&&<Notice title="No source changes">Imported configuration matches the current managed state.</Notice>}
      <div className="review-list">{changes.map(row=><section className="card" key={row.id}>
        <label className="confirm-label">
          <input type="checkbox" disabled={!row.selectable||!canWriteRepository(runtime)||busy}
            checked={selected.includes(row.id)} onChange={e=>{
              setSelected(old=>e.target.checked?[...old,row.id]:old.filter(v=>v!==row.id));
              if(!e.target.checked)setOverwrites(old=>old.filter(v=>v!==row.id));
            }}/>
          <span><strong>{row.name}</strong><small className="block-muted">{row.kind} · {row.action}
            {!row.selectable?" · unavailable":row.locally_modified?" · locally modified":""}</small></span>
        </label>
        {row.locally_modified&&selected.includes(row.id)&&<label className="confirm-label">
          <input type="checkbox" checked={overwrites.includes(row.id)} onChange={e=>
            setOverwrites(old=>e.target.checked?[...old,row.id]:old.filter(v=>v!==row.id))}/>
          Allow overwrite of my local {row.name}
        </label>}
      </section>)}</div>
      {selectable.length>0&&<ActionButton disabled={!canWriteRepository(runtime)||busy||selected.length===0||!confirmed}
        onClick={()=>void apply()}>{busy?"Applying…":"Apply selected reviewed changes"}</ActionButton>}
    </>}
    {result&&<Notice title="Applied changes">{result.applied.join(", ")}</Notice>}
    <ActionError message={error}/>
  </Context>;
}
