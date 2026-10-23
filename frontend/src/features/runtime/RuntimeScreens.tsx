import { useCallback, useEffect, useState } from "react";
import { ApiError } from "../../api/client";
import { readRuntime, readTransition, readTransitions, submitTransition,
  type ProjectSummary, type RuntimeSnapshot, type TransitionAction, type TransitionReceipt,
} from "../../api/runtime";
import { ItemLink, Notice, Shell, Skeleton, Status } from "../../components/Shell";
import { useResource } from "../../hooks/useResource";
import { actionUrl, globalActionUrl, isProjectId, OPERATIONS_URL, operationUrl, APP_ROOT } from "../../navigation/routes";
import type { Navigator } from "../../navigation/useNavigation";
import { NavLink } from "../../navigation/useNavigation";

export interface PendingTransition {
  action:TransitionAction; projectId:string|null; requestKey:string; receiptId?:string;
}
const STORAGE_KEY="devstack-v06-pending-runtime-transition-v1";
const HEX=/^[0-9a-f]{32}$/;
export function readPending():PendingTransition|null {
  try {
    const value=sessionStorage.getItem(STORAGE_KEY);
    if (!value) return null;
    const parsed:unknown=JSON.parse(value);
    if (!parsed || typeof parsed!=="object") return null;
    const p=parsed as Record<string,unknown>;
    if (!["activate","switch","deactivate","reconcile"].includes(String(p.action)) ||
        typeof p.requestKey!=="string" || !HEX.test(p.requestKey) ||
        (p.projectId!==null && (typeof p.projectId!=="string" || !isProjectId(p.projectId))) ||
        (p.receiptId!==undefined && (typeof p.receiptId!=="string" || !isProjectId(p.receiptId)))) return null;
    return p as unknown as PendingTransition;
  } catch { return null; }
}
export function savePending(value:PendingTransition):void {
  try { sessionStorage.setItem(STORAGE_KEY,JSON.stringify(value)); }
  catch { /* Private-mode storage can be unavailable; caller still holds the key. */ }
}
export function finishPending(id:string):void {
  try { if (readPending()?.receiptId===id) sessionStorage.removeItem(STORAGE_KEY); }
  catch { /* Nonessential navigation memory. */ }
}
export function freshKey():string {
  const bytes=new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes,b=>b.toString(16).padStart(2,"0")).join("");
}
export function actionAllowed(action:TransitionAction,target:string|null,runtime:RuntimeSnapshot):boolean {
  if(action==="reconcile") return runtime.phase==="error" && target===null;
  if(action==="activate") return runtime.phase==="inactive" && runtime.project_id===null && target!==null;
  if(action==="deactivate") return runtime.phase==="active" && runtime.project_id!==null && target===null;
  return action==="switch" && runtime.phase==="active" && runtime.project_id!==null &&
    target!==null && runtime.project_id!==target;
}
const LABEL:Record<TransitionAction,string>={
  activate:"Activate project",switch:"Switch project",deactivate:"Deactivate runtime",reconcile:"Recover runtime",
};
export type ReceiptInspection =
  | {kind:"loading"} | {kind:"unavailable";message:string}
  | {kind:"missing"} | {kind:"found";receipt:TransitionReceipt};

/** Read-only reconciliation of restored keys, including receipts beyond the recent list. */
export async function inspectSavedRequest(pending:PendingTransition,signal?:AbortSignal):Promise<ReceiptInspection> {
  try {
    const {operations}=await readTransitions(signal);
    let receipt=operations.find(op=>op.request_key===pending.requestKey);
    if(!receipt&&pending.receiptId) {
      try {receipt=await readTransition(pending.receiptId,signal);}
      catch(e){if(signal?.aborted)throw e;/* list may have rotated; remain uncertain */ }
    }
    if(!receipt||receipt.request_key!==pending.requestKey||
       receipt.action!==pending.action||receipt.project_id!==pending.projectId)
      return {kind:"missing"};
    return {kind:"found",receipt};
  } catch(e) {
    if(signal?.aborted)throw e;
    return {kind:"unavailable",message:e instanceof Error?e.message:"Controller receipts unavailable"};
  }
}
export function safeReceiptSummary(receipt:TransitionReceipt):string {
  const date=(value:number|null)=>value?new Date(value/1_000_000).toISOString():"not recorded";
  return [
    "DevStack runtime transition diagnostic (safe fields only)",
    "Action: "+receipt.action,
    "Target: "+(receipt.project_id??"global"),
    "Operation ID: "+receipt.id,
    "Status: "+receipt.status,
    "Error code: "+(receipt.error_code||"none"),
    "Started: "+date(receipt.started_ns),
    "Finished: "+date(receipt.finished_ns),
    "Result phase: "+(receipt.result?.phase??"not recorded"),
  ].join("\n");
}
async function copySafeSummary(value:string,feedback:(text:string)=>void) {
  try {
    if(!navigator.clipboard?.writeText)throw Error("clipboard unavailable");
    await navigator.clipboard.writeText(value);
    feedback("Diagnostic summary copied");
  } catch {feedback("Clipboard unavailable. Select and copy the technical details.");}
}
export function RuntimeActionScreen({action,projectId,runtime,projects,navigation}:{
  action:TransitionAction;projectId:string|null;runtime:RuntimeSnapshot;
  projects:ProjectSummary[];navigation:Navigator;
}) {
  const [confirmed,setConfirmed]=useState(action==="activate");
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState<string|null>(null);
  const [copyFeedback,setCopyFeedback]=useState<string|null>(null);
  const [pending,setPending]=useState<PendingTransition|null>(()=>readPending());
  const [inspection,setInspection]=useState<ReceiptInspection|null>(null);
  const [inspectionTick,setInspectionTick]=useState(0);
  const target=projectId?projects.find(p=>p.id===projectId):undefined;
  const active=runtime.project_id?projects.find(p=>p.id===runtime.project_id):undefined;
  const allowed=actionAllowed(action,projectId,runtime) &&
    (projectId===null || Boolean(target));
  const existing=pending && pending.action===action && pending.projectId===projectId
    ?pending:null;
  const pendingElsewhere=pending && !existing;
  useEffect(()=>{
    if(!pending){setInspection(null);return;}
    const ac=new AbortController();
    setInspection({kind:"loading"});
    void inspectSavedRequest(pending,ac.signal).then(result=>{
      if(ac.signal.aborted)return;
      setInspection(result);
      if(result.kind==="found"&&pending.receiptId!==result.receipt.id) {
        const updated={...pending,receiptId:result.receipt.id};
        savePending(updated);setPending(updated);
      }
    }).catch(()=>{
      if(!ac.signal.aborted)setInspection({kind:"unavailable",message:"Cannot inspect the saved receipt."});
    });
    return ()=>ac.abort();
  },[pending?.requestKey,pending?.receiptId,inspectionTick]);
  const matched=inspection?.kind==="found"?inspection.receipt:null;
  const finalReceipt=matched&&["succeeded","failed","interrupted"].includes(matched.status);
  const description=action==="switch"?
    "The active project's jobs and containers will stop before the selected project starts. Project volumes are preserved.":
    action==="deactivate"?"Stop the active project's jobs and containers. Project data is preserved.":
    action==="reconcile"?"Recovery reconciles controller-owned workloads and does not reactivate a project. Inspect its result before continuing.":
    "Start this project's development runtime.";
  const begin=async()=>{
    if(!allowed||!confirmed||busy||pendingElsewhere||Boolean(finalReceipt))return;
    setBusy(true);setError(null);
    let verified:RuntimeSnapshot;
    try {verified=await readRuntime();}
    catch(e){
      setError("Cannot verify current runtime. No transition was submitted. "+
        (e instanceof Error?e.message:"Try again when connected."));
      setBusy(false);return;
    }
    if(!actionAllowed(action,projectId,verified)){
      setError("Runtime state changed. No transition submitted. Inspect the saved receipt and current state.");
      setBusy(false);return;
    }
    // An old key is always replayed verbatim; no new key after an uncertain response.
    const key=existing?.requestKey??freshKey();
    const submitted:PendingTransition={action,projectId,requestKey:key,receiptId:existing?.receiptId};
    setPending(submitted);
    savePending(submitted);
    try {
      const response=await submitTransition(action,projectId,key);
      const result={...submitted,receiptId:response.operation.id};
      savePending(result);setPending(result);
      navigation.navigate(operationUrl(response.operation.id));
    } catch(e) {
      const definitive=e instanceof ApiError && ["invalid","not-found","conflict","unauthorized"].includes(e.kind);
      if(definitive)setError("Request rejected: "+e.message+" Inspect the receipt before retrying.");
      else setError((e instanceof Error?e.message:"Connection lost")+
        " Outcome unknown. Inspect the saved request; never use a new key for an uncertain request.");
      setInspectionTick(x=>x+1);
    } finally {setBusy(false);}
  };
  const status=inspection?.kind==="loading"?"Inspecting controller receipts…":
    inspection?.kind==="unavailable"?"Receipt lookup unavailable — outcome unknown.":
    inspection?.kind==="missing"?"No matching receipt found — not confirmed submitted; outcome unknown.":
    matched?"Controller receipt: "+matched.status:
    pending?"Saved request not yet inspected.":"No saved request";
  return <Shell title={LABEL[action]} navigation={navigation} back>
    <section className="card">
      {projectId&&<div className="property"><span>Target</span><strong>{target?.name??"Unknown project"}</strong></div>}
      <div className="property"><span>Runtime</span><strong>{runtime.phase}</strong></div>
      {active&&<div className="property"><span>Currently active</span><strong>{active.name}</strong></div>}
    </section>
    <Notice title={action==="reconcile"?"Recovery review":"Confirm runtime change"}>{description}</Notice>
    {!allowed&&<Notice title="Action unavailable" role="alert">Current runtime state does not permit this operation. Refresh or inspect operations.</Notice>}
    {pending&&<section className="card">
      <strong>Saved request</strong>
      <p role="status">{status}</p>
      {inspection?.kind==="unavailable"&&<p className="text-muted">{inspection.message}</p>}
      {matched&&<><div className="property"><span>Status</span>{receiptStatus(matched)}</div>
        {matched.error_code&&<p role="alert">Controller error: {matched.error_code}</p>}
        <NavLink to={operationUrl(matched.id)} navigate={navigation.navigate}>Open matching receipt →</NavLink></>}
      {!matched&&pending.receiptId&&<p><NavLink to={operationUrl(pending.receiptId)} navigate={navigation.navigate}>Open saved receipt →</NavLink></p>}
      <p className="text-muted">A missing receipt is not proof of failure. No request is automatically replayed.</p>
      <button type="button" className="text-button" onClick={()=>setInspectionTick(x=>x+1)}>Inspect previous request / refresh</button>
      <details className="technical-details"><summary>Technical details</summary>
        <div className="property"><span>Action</span><strong>{pending.action}</strong></div>
        <div className="property"><span>Runtime phase</span><strong>{runtime.phase}</strong></div>
        <div className="property"><span>Receipt ID</span><code>{matched?.id??pending.receiptId??"unknown"}</code></div>
        <div className="property"><span>Status</span><strong>{matched?.status??"unknown"}</strong></div>
        <div className="property"><span>Error code</span><code>{matched?.error_code||"none reported"}</code></div>
        {matched&&<><div className="property"><span>Started</span><span>{new Date(matched.started_ns/1_000_000).toLocaleString()}</span></div>
          <div className="property"><span>Finished</span><span>{matched.finished_ns?new Date(matched.finished_ns/1_000_000).toLocaleString():"not yet"}</span></div></>}
        <p className="text-muted">Idempotency keys are intentionally omitted from copied diagnostics.</p>
        {matched&&<button type="button" className="text-button" onClick={()=>void copySafeSummary(safeReceiptSummary(matched),setCopyFeedback)}>Copy diagnostic summary</button>}
        {copyFeedback&&<p role="status">{copyFeedback}</p>}
      </details>
      {finalReceipt&&<button type="button" className="text-button" onClick={()=>{
        if(matched)finishPending(matched.id);setPending(null);setInspection(null);
      }}>Dismiss resolved request</button>}
    </section>}
    {pendingElsewhere&&<Notice title="Another saved request exists" role="alert">
      Inspect the previous runtime request before starting another action. No new key will be generated.
    </Notice>}
    {allowed&&<div className="confirmation">
      {action!=="activate"&&<label className="confirm-label">
        <input type="checkbox" checked={confirmed} onChange={e=>setConfirmed(e.target.checked)}/>
        I understand the effect on running workloads
      </label>}
      <button type="button" className="primary-button"
        disabled={!confirmed||busy||Boolean(pendingElsewhere)||Boolean(finalReceipt)}
        onClick={()=>void begin()}>{busy?"Submitting…":existing?"Retry with same key":LABEL[action]}</button>
      {existing&&<p className="text-muted">Retry is explicit, uses identical action/target/key and rechecks the controller runtime first.</p>}
    </div>}
    {error&&<Notice title="Operation not confirmed" role="alert">{error}</Notice>}
    <p className="actions"><NavLink to={OPERATIONS_URL} navigate={navigation.navigate}>View recent operations →</NavLink></p>
  </Shell>;
}
function receiptStatus(receipt:TransitionReceipt) {
  const terminal=["succeeded","failed","interrupted"].includes(receipt.status);
  return <Status text={receipt.status} kind={receipt.status==="succeeded"?"positive":terminal?"warning":"neutral"}/>;
}
export function RuntimeOperations({navigation}: {navigation:Navigator}) {
  const loader=useCallback(readTransitions,[]);
  const state=useResource(loader,2000);
  const pending=readPending();
  const match=state.kind==="ready"&&pending?
    state.data.operations.find(o=>o.request_key===pending.requestKey):undefined;
  return <Shell title="Runtime operations" navigation={navigation} back>
    {state.kind==="loading"&&<Skeleton/>}
    {state.kind==="error"&&<Notice title="Cannot inspect operations" role="alert">{state.error.message}</Notice>}
    {pending && <Notice title="Saved request key">
      {match?"A matching controller operation is listed below.":"No matching receipt in the recent list; the outcome remains unknown."}
    </Notice>}
    {state.kind==="ready"&&<>
      {state.data.operations.length===0&&<Notice title="No recent operations">No controller receipts were returned.</Notice>}
      <div className="item-group">{state.data.operations.map(op=>
        <ItemLink key={op.id} icon="↻" title={LABEL[op.action]} detail={op.id.slice(0,8)+" · "+op.status}
          href={operationUrl(op.id)} navigation={navigation} suffix={receiptStatus(op)}/>)}</div>
    </>}
  </Shell>;
}
export function RuntimeOperationDetail({id,navigation}: {id:string;navigation:Navigator}) {
  const load=useCallback((signal:AbortSignal)=>readTransition(id,signal),[id]);
  const state=useResource(load,1600);
  const operation=state.kind==="ready"?state.data:null;
  useEffect(()=>{
    if(operation && ["succeeded","failed","interrupted"].includes(operation.status)) finishPending(id);
  },[id,operation?.status]);
  return <Shell title="Runtime operation" navigation={navigation} back>
    {state.kind==="loading"&&<Skeleton/>}
    {state.kind==="error"&&<Notice title="Cannot retrieve receipt" role="alert">{state.error.message} The runtime outcome remains unknown.</Notice>}
    {operation&&<>
      <section className="card">
        <div className="property"><span>Action</span><strong>{LABEL[operation.action]}</strong></div>
        <div className="property"><span>Status</span>{receiptStatus(operation)}</div>
        <div className="property"><span>Operation</span><code>{operation.id.slice(0,8)}</code></div>
        {operation.project_id&&<div className="property"><span>Target ID</span><code>{operation.project_id.slice(0,8)}</code></div>}
        {operation.error_code&&<div className="property"><span>Error</span><strong>{operation.error_code}</strong></div>}
      </section>
      {operation.status==="queued"||operation.status==="running"
        ? <Notice title="In progress">The controller owns this operation. You may leave this screen and return later.</Notice>
        :operation.status==="succeeded"
          ?<Notice title="Transition finished">The controller marked this operation successful. Verify the current runtime state before further action.</Notice>
          :<Notice title={operation.status==="interrupted"?"Outcome requires inspection":"Transition failed"} role="alert">
            {operation.status==="interrupted"
              ?"The controller stopped before recording a final outcome. Inspect current runtime state; do not assume it was rolled back."
              :"Review the failure and current runtime state before retrying."}
          </Notice>}
    </>}
    <p className="actions"><NavLink to={APP_ROOT} navigate={navigation.navigate}>Current runtime →</NavLink></p>
    <p className="actions"><NavLink to={OPERATIONS_URL} navigate={navigation.navigate}>Recent operations →</NavLink></p>
  </Shell>;
}
export function RuntimeControls({projectId,runtime,navigation}:{
  projectId:string;runtime:RuntimeSnapshot;navigation:Navigator;
}) {
  if(runtime.phase==="inactive"&&runtime.project_id===null)
    return <p className="actions"><NavLink to={actionUrl("activate",projectId)}
      navigate={navigation.navigate}>Activate project →</NavLink></p>;
  if(runtime.phase==="active"&&runtime.project_id!==projectId)
    return <p className="actions"><NavLink to={actionUrl("switch",projectId)}
      navigate={navigation.navigate}>Switch active project →</NavLink></p>;
  if(runtime.phase==="active"&&runtime.project_id===projectId)
    return <p className="actions"><NavLink to={globalActionUrl("deactivate")}
      navigate={navigation.navigate}>Deactivate project →</NavLink></p>;
  if(runtime.phase==="error")
    return <p className="actions"><NavLink to={globalActionUrl("reconcile")}
      navigate={navigation.navigate}>Review recovery →</NavLink></p>;
  return <p className="actions"><NavLink to={OPERATIONS_URL}
    navigate={navigation.navigate}>Inspect ongoing runtime operations →</NavLink></p>;
}
