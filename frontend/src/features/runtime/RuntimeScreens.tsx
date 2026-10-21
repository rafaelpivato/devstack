import { useCallback, useState } from "react";
import { ApiError } from "../../api/client";
import { readRuntime, readTransition, readTransitions, submitTransition,
  type ProjectSummary, type RuntimeSnapshot, type TransitionAction, type TransitionReceipt,
} from "../../api/runtime";
import { ItemLink, Notice, Shell, Skeleton, Status } from "../../components/Shell";
import { useResource } from "../../hooks/useResource";
import { actionUrl, globalActionUrl, OPERATIONS_URL, operationUrl, PREVIEW_ROOT, projectUrl } from "../../navigation/routes";
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
        (p.projectId!==null && typeof p.projectId!=="string")) return null;
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
export function RuntimeActionScreen({action,projectId,runtime,projects,navigation}:{
  action:TransitionAction;projectId:string|null;runtime:RuntimeSnapshot;
  projects:ProjectSummary[];navigation:Navigator;
}) {
  const [confirmed,setConfirmed]=useState(action==="activate");
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState<string|null>(null);
  const [pending,setPending]=useState<PendingTransition|null>(()=>readPending());
  const target=projectId?projects.find(p=>p.id===projectId):undefined;
  const active=runtime.project_id?projects.find(p=>p.id===runtime.project_id):undefined;
  const allowed=actionAllowed(action,projectId,runtime) &&
    (projectId===null || Boolean(target));
  const existing=pending && pending.action===action && pending.projectId===projectId
    ?pending:null;
  const pendingElsewhere=pending && !existing;
  const description=action==="switch"?
    "The active project's jobs and containers will stop before the selected project starts. Project volumes are preserved.":
    action==="deactivate"?"Stop the active project's jobs and containers. Project data is preserved.":
    action==="reconcile"?"Recovery reconciles controller-owned workloads and does not reactivate a project. Inspect its result before continuing.":
    "Start this project's development runtime.";
  const begin=async()=>{
    if (!allowed || !confirmed || busy || pendingElsewhere) return;
    setBusy(true);setError(null);
    const key=existing?.requestKey??freshKey();
    const submitted:PendingTransition={action,projectId,requestKey:key,receiptId:existing?.receiptId};
    setPending(submitted);
    savePending(submitted); // Set key BEFORE sending. Lost responses must not cause duplicate work.
    try {
      // An updated runtime must be inspected immediately before the explicit submit.
      const verified=await readRuntime();
      if (!actionAllowed(action,projectId,verified))
        throw new Error("Runtime state changed. Review the current state before retrying.");
      const response=await submitTransition(action,projectId,key);
      const result={...submitted,receiptId:response.operation.id};
      savePending(result);setPending(result);
      navigation.navigate(operationUrl(response.operation.id));
    } catch(e) {
      const definitive=e instanceof ApiError && ["invalid","not-found","conflict","unauthorized"].includes(e.kind);
      if(definitive) setError("Request rejected: "+(e as Error).message+" Inspect runtime state before retrying.");
      else setError((e instanceof Error?e.message:"Connection lost")+
        " The outcome may be unknown. Check operations before retrying with the same request key.");
    } finally {setBusy(false);}
  };
  return <Shell title={LABEL[action]} navigation={navigation} back>
    <section className="card">
      {projectId && <div className="property"><span>Target</span><strong>{target?.name??"Unknown project"}</strong></div>}
      <div className="property"><span>Runtime</span><strong>{runtime.phase}</strong></div>
      {active && <div className="property"><span>Currently active</span><strong>{active.name}</strong></div>}
    </section>
    <Notice title={action==="reconcile"?"Recovery review":"Confirm runtime change"}>{description}</Notice>
    {!allowed && <Notice title="Action unavailable" role="alert">The current runtime state does not permit this operation. Refresh or inspect operations.</Notice>}
    {allowed && <div className="confirmation">
      {action!=="activate" && <label className="confirm-label">
        <input type="checkbox" checked={confirmed} onChange={e=>setConfirmed(e.target.checked)}/>
        I understand the effect on running workloads
      </label>}
      <button type="button" className="primary-button" disabled={!confirmed||busy||Boolean(pendingElsewhere)}
        onClick={()=>void begin()}>{busy?"Submitting…":existing?"Retry with same key":LABEL[action]}</button>
    </div>}
    {error && <Notice title="Operation not confirmed" role="alert">{error}</Notice>}
    {pending && <section className="card">
      <strong>Saved request</strong>
      <div className="property"><span>Key</span><code>{pending.requestKey}</code></div>
      {pending.receiptId&&<p><NavLink to={operationUrl(pending.receiptId)} navigate={navigation.navigate}>Open receipt →</NavLink></p>}
      <p className="text-muted">Do not submit a new request key until the outcome is known.</p>
    </section>}
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
      {pending.requestKey}{match?" — A matching operation is listed below.": " — A receipt may not yet be available. Do not assume the operation failed."}
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
  if(operation && ["succeeded","failed","interrupted"].includes(operation.status)) finishPending(id);
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
    <p className="actions"><NavLink to={PREVIEW_ROOT} navigate={navigation.navigate}>Current runtime →</NavLink></p>
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
