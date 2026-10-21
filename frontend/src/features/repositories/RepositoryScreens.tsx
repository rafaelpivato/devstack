import { useCallback, useEffect, useState } from "react";
import { ApiError } from "../../api/client";
import { readProject } from "../../api/projects";
import { readRuntime, type RuntimeSnapshot } from "../../api/runtime";
import {
  repositoryStatus, updateRepository, previewBranch, applyBranch, reviewSource, applySource,
  submitRepoAdd, submitRepoBulk, repoAddReceipt, repoBulkReceipt,
  type RepositoryStatus, type BranchPreview, type BranchApplyResult, type SourceReviewResult,
  type SourceApplyResult,
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

const repositoryRoot=(id:string)=>laneUrl(id,"repositories");
export const canWriteRepository=(runtime:RuntimeSnapshot)=>
  runtime.phase==="inactive" && runtime.project_id===null;
const errorMessage=(e:unknown)=>e instanceof Error?e.message:"Request could not be completed.";
async function verifyInactive() {
  if(!canWriteRepository(await readRuntime()))
    throw new Error("Runtime is no longer inactive. Deactivate and review again.");
}
function WriteBoundary({runtime}:{runtime:RuntimeSnapshot}) {
  return !canWriteRepository(runtime)?
    <Notice title="Repository changes unavailable">Deactivate the active runtime before changing repositories. Inspection remains available.</Notice>:null;
}
function ActionError({message}:{message:string|null}) {
  return message?<Notice title="Action not confirmed" role="alert">{message}</Notice>:null;
}
function ActionButton({children,disabled,onClick}:{
  children:React.ReactNode;disabled?:boolean;onClick:()=>void;
}) {return <button type="button" className="primary-button" disabled={disabled} onClick={onClick}>{children}</button>;}
function Context({id,navigation,children,title,subtitle}:{
  id:string;navigation:Navigator;children:React.ReactNode;title:string;subtitle?:string;
}) {
  return <Shell title={title} subtitle={subtitle} navigation={navigation} back>{children}</Shell>;
}
function LinkRow({to,label,navigation}:{to:string;label:string;navigation:Navigator}) {
  return <p className="actions"><NavLink to={to} navigate={navigation.navigate}>{label} →</NavLink></p>;
}
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
        {detail.data.last_repository_update && typeof detail.data.last_repository_update==="object" &&
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
  const ready=canWriteRepository(runtime) && state && !state.dirty && !!state.branch &&
    state.upstream?.startsWith("origin/") && !((state.ahead??0)>0&&(state.behind??0)>0);
  const update=async()=>{
    if(!ready||!state||busy)return;
    setBusy(true);setError(null);setResult(null);
    try{
      await verifyInactive();
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
    <WriteBoundary runtime={runtime}/>
    {state&&<ActionButton onClick={()=>void update()} disabled={!ready||busy}>
      {busy?"Checking Git…":"Fetch and fast-forward"}
    </ActionButton>}
    {state&&canWriteRepository(runtime)&&!ready&&<p className="text-muted">Requires a clean branch tracking origin without diverged history.</p>}
    {result&&<Notice title="Repository checked">Current HEAD: {result.head.slice(0,12)}. Reload status before another change.</Notice>}
    <ActionError message={error}/>
    <LinkRow to={repoBranchUrl(id)} label="Review branch change" navigation={navigation}/>
  </Context>;
}
export function RepositoryAdd({id,runtime,navigation}:{id:string;runtime:RuntimeSnapshot;navigation:Navigator}) {
  const [form,setForm]=useState({url:"",name:"",branch:""});
  const [pending,setPending]=useState<SavedRepositorySubmission|null>(()=>loadSubmission(id,"add"));
  const [error,setError]=useState<string|null>(null);
  const [busy,setBusy]=useState(false);
  const allowed=canWriteRepository(runtime);
  const locked=Boolean(pending && (pending.form?.url!==form.url || pending.form.name!==form.name ||
    pending.form.branch!==form.branch));
  const submit=async()=>{
    if(!allowed||busy||!form.url.trim()||!form.name.trim()||locked)return;
    setBusy(true);setError(null);
    try{
      await verifyInactive();
      const saved=pending??{mode:"add" as const,projectId:id,key:requestKey(),form};
      saveSubmission(saved);setPending(saved);
      const value=await submitRepoAdd(id,form,saved.key);
      const next={...saved,operationId:value.import.id};
      saveSubmission(next);setPending(next);
      navigation.navigate(repoReceiptUrl(id,"add",value.import.id));
    }catch(e){setError(errorMessage(e)+" Check operation status before retrying with the same key.");}
    finally{setBusy(false);}
  };
  return <Context id={id} title="Add repository" navigation={navigation}>
    <WriteBoundary runtime={runtime}/>
    <Notice title="GitHub repository">Adding a repository requires the global runtime to be inactive. No project switch occurs.</Notice>
    <div className="form-stack">
      <label>Repository URL<input value={form.url} onChange={e=>setForm({...form,url:e.target.value})}
        placeholder="https://github.com/owner/repo" maxLength={2048} autoCapitalize="off" autoCorrect="off"/></label>
      <label>Name<input value={form.name} onChange={e=>setForm({...form,name:e.target.value})}
        maxLength={100} autoCapitalize="off" autoCorrect="off"/></label>
      <label>Branch (optional)<input value={form.branch} onChange={e=>setForm({...form,branch:e.target.value})}
        maxLength={200} autoCapitalize="off" autoCorrect="off"/></label>
    </div>
    {pending&&<Notice title="Saved request">
      {pending.operationId?"A previous add operation has a receipt.":"A request key was retained in this tab."}
      {pending.operationId&&<LinkRow to={repoReceiptUrl(id,"add",pending.operationId)}
        label="View receipt" navigation={navigation}/>}
      {locked&&"The saved request used different fields. Inspect its result before starting another."}
    </Notice>}
    <ActionButton onClick={()=>void submit()} disabled={!allowed||busy||locked||!form.url.trim()||!form.name.trim()}>
      {busy?"Submitting…":pending?"Retry with saved key":"Add repository"}
    </ActionButton>
    <ActionError message={error}/>
    {pending&&<p className="actions"><button className="text-button" type="button"
      onClick={()=>{clearSubmission(id,"add");setPending(null);setError(null);}}>
      Start a new request (only after checking previous result)</button></p>}
  </Context>;
}
export function RepositoryBulk({id,runtime,navigation}:{id:string;runtime:RuntimeSnapshot;navigation:Navigator}) {
  const [confirmed,setConfirmed]=useState(false);
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState<string|null>(null);
  const [pending,setPending]=useState<SavedRepositorySubmission|null>(()=>loadSubmission(id,"bulk"));
  const run=async()=>{
    if(!confirmed||!canWriteRepository(runtime)||busy)return;
    setBusy(true);setError(null);
    try{
      await verifyInactive();
      const saved=pending??{mode:"bulk" as const,projectId:id,key:requestKey()};
      saveSubmission(saved);setPending(saved);
      const receipt=await submitRepoBulk(id,saved.key);
      const next={...saved,operationId:receipt.id};
      saveSubmission(next);setPending(next);
      navigation.navigate(repoReceiptUrl(id,"bulk",receipt.id));
    }catch(e){setError(errorMessage(e)+" Inspect the latest batch before retrying with the same key.");}
    finally{setBusy(false);}
  };
  return <Context id={id} title="Update repositories" navigation={navigation}>
    <WriteBoundary runtime={runtime}/>
    <Notice title="Review batch update">Fetch each registered repository and fast-forward eligible clean tracking branches. Dirty, diverged or unsupported repositories are reported per item. This does not switch branches.</Notice>
    {pending?.operationId&&<LinkRow to={repoReceiptUrl(id,"bulk",pending.operationId)}
      label="View saved batch receipt" navigation={navigation}/>}
    <label className="confirm-label"><input type="checkbox" checked={confirmed}
      onChange={e=>setConfirmed(e.target.checked)}/>Run an inactive-project Git update</label>
    <ActionButton onClick={()=>void run()} disabled={!confirmed||busy||!canWriteRepository(runtime)}>
      {busy?"Submitting…":pending?"Retry with saved key":"Update all repositories"}
    </ActionButton>
    <ActionError message={error}/>
    {pending&&<p className="actions"><button className="text-button" type="button"
      onClick={()=>{clearSubmission(id,"bulk");setPending(null);setError(null);}}>
      Start a new batch (only after checking prior outcome)</button></p>}
  </Context>;
}
export function RepositoryReceipt({id,operationId,mode,navigation}:{
  id:string;operationId:string;mode:"add"|"bulk";navigation:Navigator;
}) {
  const load=useCallback((signal:AbortSignal)=>mode==="add"?
    repoAddReceipt(id,operationId,signal):repoBulkReceipt(id,operationId,signal),[id,operationId,mode]);
  const resource=useResource(load,2000);
  const value=resource.kind==="ready"?resource.data:null;
  return <Context id={id} title={mode==="add"?"Repository add":"Repository batch"} navigation={navigation}>
    {resource.kind==="loading"&&<Skeleton/>}
    {resource.kind==="error"&&<Notice title="Receipt unavailable" role="alert">
      {resource.error.message}. The outcome is unknown; inspect before submitting again.</Notice>}
    {value&&<>
      <section className="card">
        <div className="property"><span>Operation</span><code>{value.id.slice(0,8)}</code></div>
        <div className="property"><span>State</span><Status text={value.status}
          kind={value.status==="succeeded"||value.status==="completed"?"positive":
            ["failed","interrupted"].includes(value.status)?"warning":"neutral"}/></div>
        {mode==="add" && "name" in value&&<div className="property"><span>Repository</span><strong>{value.name}</strong></div>}
      </section>
      {mode==="add"&&"error_code" in value&&value.error_code&&
        <Notice title="Operation error" role="alert">{value.error_code}</Notice>}
      {mode==="bulk"&&"items" in value&&<div className="item-group">{value.items.map((item,i)=>
        <div key={item.repository_id+"-"+i} className="item-static">
          <div className="item-text"><strong>{item.name}</strong><small>{item.reason??"No reason reported"}</small></div>
          <Status text={item.status} kind={["failed","blocked","skipped"].includes(item.status)?"warning":"neutral"}/>
        </div>)}</div>}
      {["queued","running"].includes(value.status)&&
        <Notice title="In progress">The controller owns this operation. You can return later.</Notice>}
      {["failed","interrupted"].includes(value.status)&&
        <Notice title="Inspect before retry" role="alert">Do not assume a rollback or duplicate the operation with a new key.</Notice>}
    </>}
    <LinkRow to={repositoryRoot(id)} label="Repositories" navigation={navigation}/>
  </Context>;
}
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
    setSelected([]);setOverwrites([]); // Do not allow resubmission of an unknown write.
    try{
      await verifyInactive();
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
