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


import { Context, WriteBoundary, ActionButton, LinkRow, ActionError, canWriteRepository, verifyInactive, errorMessage, repositoryRoot } from './RepositoryCommon';
export function RepositoryAdd({id,runtime,navigation}:{id:string;runtime:RuntimeSnapshot;navigation:Navigator}) {
  const [form,setForm]=useState(()=>loadSubmission(id,"add")?.form??{url:"",name:"",branch:""});
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
  const load=useCallback(async(signal:AbortSignal):Promise<RepositoryAddReceipt|RepositoryBulkReceipt>=>
    mode==="add"?repoAddReceipt(id,operationId,signal):repoBulkReceipt(id,operationId,signal),[id,operationId,mode]);
  const resource=useResource<RepositoryAddReceipt|RepositoryBulkReceipt>(load,2000);
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
