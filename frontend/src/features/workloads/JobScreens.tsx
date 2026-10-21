import { useCallback, useEffect, useState } from "react";
import type { RuntimeSnapshot } from "../../api/runtime";
import { readJobs, readJob, readJobLogs, runTask, runCompose, runAdhoc, cancelJob,
  type JobRow, type AdhocKind, type AdhocSpec, type JobLogChunk, type ComposeAction } from "../../api/workloads";
import { readProject } from "../../api/projects";
import { useResource } from "../../hooks/useResource";
import { ItemLink, Notice, Skeleton, Status } from "../../components/Shell";
import { NavLink, type Navigator } from "../../navigation/useNavigation";
import { jobUrl, jobLogsUrl, jobsRunUrl } from "../../navigation/routes";
import { ActiveOnly, CopyLog, ReadError, TextLog, WorkloadShell, SectionLink, canOperate } from "./WorkloadShared";

const isRunning=(job:JobRow)=>["queued","running","cancelling"].includes(job.status);
export const cancellable=(job:JobRow,active:boolean)=>active&&
  job.cancel_policy==="independent"&&["queued","running"].includes(job.status);
export function JobsScreen({id,runtime,navigation}:{
  id:string;runtime:RuntimeSnapshot;navigation:Navigator;
}) {
  const load=useCallback((signal:AbortSignal)=>readJobs(id,signal),[id]);
  const state=useResource(load,7000);
  const active=canOperate(runtime,id);
  return <WorkloadShell title="Jobs" id={id} navigation={navigation}>
    <ActiveOnly id={id} runtime={runtime}/>
    {active&&<div className="item-group">
      <ItemLink icon="▶" title="Run saved task" detail="Controller-supervised" href={jobsRunUrl(id,"task")} navigation={navigation}/>
      <ItemLink icon="⬡" title="Compose build / restart" href={jobsRunUrl(id,"compose")} navigation={navigation}/>
      <ItemLink icon="⌘" title="Ad-hoc container command" detail="Explicit argv, no host shell" href={jobsRunUrl(id,"adhoc")} navigation={navigation}/>
    </div>}
    <div className="section-head">Recent jobs</div>
    {state.kind==="loading"&&<Skeleton/>}
    {state.kind==="error"&&<ReadError message={state.error.message}/>}
    {state.kind==="ready"&&<>
      {state.data.jobs.length===0&&<Notice title="No jobs">This project has no recorded job history.</Notice>}
      <div className="item-group">{state.data.jobs.map(job=>
        <ItemLink key={job.id} icon="▤" title={job.action} detail={job.kind+" · "+job.status}
          suffix={<Status text={job.status} kind={job.status==="succeeded"?"positive":"neutral"}/>}
          href={jobUrl(id,job.id)} navigation={navigation}/>)}</div>
    </>}
  </WorkloadShell>;
}
export function JobDetail({id,jobId,runtime,navigation}:{
  id:string;jobId:string;runtime:RuntimeSnapshot;navigation:Navigator;
}) {
  const load=useCallback((signal:AbortSignal)=>readJob(id,jobId,signal),[id,jobId]);
  const state=useResource(load,5000);
  const [error,setError]=useState<string|null>(null);
  const [confirm,setConfirm]=useState(false);
  const [busy,setBusy]=useState(false);
  const active=canOperate(runtime,id);
  const job=state.kind==="ready"?state.data:null;
  const stop=async()=>{
    if(!job||!cancellable(job,active)||!confirm||busy)return;
    setBusy(true);setError(null);
    try{
      const current=await readJob(id,jobId);
      if(!cancellable(current,active)) throw Error("This job can no longer be independently cancelled");
      await cancelJob(id,jobId);
      setConfirm(false);
    }catch(e){setError(e instanceof Error?e.message:"Job cancellation not confirmed");}
    finally{setBusy(false);}
  };
  return <WorkloadShell title="Job detail" id={id} navigation={navigation}>
    {state.kind==="loading"&&<Skeleton/>}
    {state.kind==="error"&&<ReadError message={state.error.message}/>}
    {job&&<section className="card">
      <div className="property"><span>Action</span><strong>{job.action}</strong></div>
      <div className="property"><span>Type</span><strong>{job.kind}</strong></div>
      <div className="property"><span>Status</span><Status text={job.status}
        kind={job.status==="succeeded"?"positive":"neutral"}/></div>
      <div className="property"><span>Cancel policy</span><strong>{job.cancel_policy}</strong></div>
      <div className="property"><span>Exit code</span><strong>{job.exit_code??"—"}</strong></div>
    </section>}
    {job&&<p className="actions"><NavLink navigate={navigation.navigate} to={jobLogsUrl(id,jobId)}>Open paged output →</NavLink></p>}
    {job&&cancellable(job,active)&&<>
      <label className="confirm-label"><input type="checkbox" checked={confirm}
        onChange={e=>setConfirm(e.target.checked)}/>Stop this independent container job</label>
      <button type="button" className="primary-button" disabled={!confirm||busy}
        onClick={()=>void stop()}>{busy?"Cancelling…":"Cancel job"}</button>
    </>}
    {job&&isRunning(job)&&job.cancel_policy!=="independent"&&
      <Notice title="Shared infrastructure">This job cannot be cancelled individually. Use the guarded runtime transition if it must be stopped.</Notice>}
    {error&&<Notice title="Cancellation not confirmed" role="alert">{error}</Notice>}
    <SectionLink id={id} lane="jobs" label="Job history" navigation={navigation}/>
  </WorkloadShell>;
}
export function JobLogs({id,jobId,navigation}:{
  id:string;jobId:string;navigation:Navigator;
}) {
  const [offset,setOffset]=useState(0);
  const [pages,setPages]=useState<string[]>([]);
  const [complete,setComplete]=useState(false);
  const [loading,setLoading]=useState(false);
  const [error,setError]=useState<string|null>(null);
  const [started,setStarted]=useState(false);
  const fetchPage=useCallback(async()=>{
    if(loading||complete||offset>=2*1024*1024)return;
    setLoading(true);setError(null);
    try{
      const result:JobLogChunk=await readJobLogs(id,jobId,offset);
      if(result.offset!==offset||result.next_offset<offset||result.next_offset>2*1024*1024)
        throw Error("Unexpected controller log offset");
      setPages(p=>result.next_offset===offset?p:[...p,result.text]);
      setOffset(result.next_offset);
      // 'complete' means the job stopped, not that all historical log pages
      // have been consumed. Read through to an empty terminal page.
      setComplete(result.complete && result.next_offset===offset);
    }catch(e){setError(e instanceof Error?e.message:"Cannot retrieve log page");}
    finally{setLoading(false);setStarted(true);}
  },[id,jobId,offset,loading,complete]);
  useEffect(()=>{if(!started)void fetchPage();},[started,fetchPage]);
  const text=pages.join("");
  return <WorkloadShell title="Job output" id={id} navigation={navigation}>
    <p className="text-muted">Controller-paged output (max 2 MiB). No interactive terminal.</p>
    {error&&<ReadError message={error}/>}
    <TextLog label="Paged job output" text={text}/>
    <CopyLog value={text}/>
    {!complete&&<button className="text-button" type="button" disabled={loading||offset>=2*1024*1024}
      onClick={()=>void fetchPage()}>{loading?"Loading…":"Load new output"}</button>}
    {complete&&<Notice title="Output complete">The controller marked the job output complete.</Notice>}
    <p className="text-muted">Read offset: {offset} bytes</p>
  </WorkloadShell>;
}
export function RunJob({id,mode,runtime,navigation}:{
  id:string;mode:"task"|"compose"|"adhoc";runtime:RuntimeSnapshot;navigation:Navigator;
}) {
  const load=useCallback((signal:AbortSignal)=>readProject(id,signal),[id]);
  const project=useResource(load,20000);
  const [task,setTask]=useState("");
  const [action,setAction]=useState<ComposeAction>("build");
  const [kind,setKind]=useState<AdhocKind>("run");
  const [service,setService]=useState("");
  const [image,setImage]=useState("");
  const [network,setNetwork]=useState<"none"|"project">("none");
  const [argv,setArgv]=useState("");
  const [confirmed,setConfirmed]=useState(false);
  const [busy,setBusy]=useState(false);
  const [unknown,setUnknown]=useState(false);
  const [error,setError]=useState<string|null>(null);
  const active=canOperate(runtime,id);
  const available=project.kind==="ready"?project.data.tasks:[];
  const valid=mode==="compose"||mode==="task"&&available.some(t=>t.name===task)||
    mode==="adhoc" && !!argv.trim() && (kind==="run"?!!service.trim():!!image.trim());
  const submit=async()=>{
    if(!active||!confirmed||!valid||busy||unknown)return;
    setBusy(true);setError(null);
    // Verify the controller state before attempting a POST. An unavailable
    // preflight is not an unknown submission and may be retried normally.
    try{
      if(!await readJobSafeRuntime(id))throw Error("The runtime changed. No job submitted.");
    }catch(e){
      setError((e instanceof Error?e.message:"Runtime unavailable")+". No job was submitted.");
      setBusy(false);
      return;
    }
    try{
      let result;
      if(mode==="task")result=await runTask(id,task);
      else if(mode==="compose")result=await runCompose(id,action);
      else {
        const spec:AdhocSpec={
          kind,service:kind==="run"?service:"",image:kind==="image"?image:"",
          network:kind==="run"?"none":network,
          command:argv.split("\n").map(x=>x.trim()).filter(Boolean),
        };
        result=await runAdhoc(id,spec);
      }
      navigation.navigate(jobUrl(id,result.job_id));
    }catch(e){
      setError(e instanceof Error?e.message:"Job submission outcome unknown");
      setUnknown(true); // No job-submit idempotency contract: never automatically retry.
    }finally{setBusy(false);}
  };
  return <WorkloadShell title={mode==="task"?"Run saved task":mode==="compose"?"Compose operation":"Ad-hoc container"} id={id} navigation={navigation}>
    <ActiveOnly id={id} runtime={runtime}/>
    <Notice title="Controller-owned job">This runs on the active project. It is supervised and recorded, not an interactive shell. Review recent jobs after an uncertain response.</Notice>
    {mode==="task"&&project.kind==="ready"&&<>
      {available.length===0&&<Notice title="No saved tasks">Configure tasks under project settings.</Notice>}
      <label className="field-inline">Saved task
        <select value={task} onChange={e=>{setTask(e.target.value);setConfirmed(false);}}>
          <option value="">Choose a task</option>
          {available.map(t=><option key={t.name} value={t.name}>{t.name}</option>)}
        </select></label>
    </>}
    {mode==="compose"&&<label className="field-inline">Compose action
      <select value={action} onChange={e=>{setAction(e.target.value as ComposeAction);setConfirmed(false);}}>
        <option value="build">Build</option><option value="restart">Restart</option>
      </select></label>}
    {mode==="adhoc"&&<div className="form-stack">
      <label>Container mode<select value={kind} onChange={e=>{setKind(e.target.value as AdhocKind);setConfirmed(false);}}>
        <option value="run">Existing Compose service</option><option value="image">Standalone image</option>
      </select></label>
      {kind==="run"?<label>Service<input value={service} onChange={e=>setService(e.target.value)}
        placeholder="web" autoCapitalize="off"/></label>:<>
        <label>Image<input value={image} onChange={e=>setImage(e.target.value)}
          placeholder="python:3.12-alpine" autoCapitalize="off"/></label>
        <label>Network<select value={network} onChange={e=>setNetwork(e.target.value as "none"|"project")}>
          <option value="none">None</option><option value="project">Project network</option></select></label>
      </>}
      <label>Command argv — one argument per line<textarea rows={5} value={argv}
        onChange={e=>setArgv(e.target.value)} placeholder={"python\n-c\nprint('hello')"} /></label>
    </div>}
    <label className="confirm-label"><input type="checkbox" checked={confirmed}
      onChange={e=>setConfirmed(e.target.checked)}/>Run this job on the active project</label>
    <button className="primary-button" type="button" onClick={()=>void submit()}
      disabled={!active||!valid||!confirmed||busy||unknown}>{busy?"Submitting…":"Submit supervised job"}</button>
    {error&&<Notice title="Job submission not confirmed" role="alert">{error}. Inspect job history before another attempt.</Notice>}
    <SectionLink id={id} lane="jobs" label="Inspect job history" navigation={navigation}/>
  </WorkloadShell>;
}
async function readJobSafeRuntime(id:string):Promise<boolean>{
  const {readRuntime}=await import("../../api/runtime");
  const state=await readRuntime();
  return canOperate(state,id);
}
