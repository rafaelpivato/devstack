import { useCallback, useState } from "react";
import { readProject } from "../../api/projects";
import { readCompose, saveCompose, readReviewedSettings, saveReviewedSettings,
  validateCollection, renameProject } from "../../api/configuration";
import type { ReviewedSettings } from "../../api/configuration";
import type { RuntimeSnapshot } from "../../api/runtime";
import { useResource } from "../../hooks/useResource";
import { ItemLink, Notice, Shell, Skeleton } from "../../components/Shell";
import type { Navigator } from "../../navigation/useNavigation";
import { configUrl } from "../../navigation/routes";
import { ConfigBack, ConfigShell, EditGuard, Feedback, editable, message, verifyInactive } from "./Common";

export function ConfigurationHome({id,runtime,navigation}:{
  id:string;runtime:RuntimeSnapshot;navigation:Navigator;
}) {
  const load=useCallback((signal:AbortSignal)=>readProject(id,signal),[id]);
  const state=useResource(load,20000);
  return <Shell title="Configuration" navigation={navigation} back>
    <EditGuard runtime={runtime}/>
    {state.kind==="loading"&&<Skeleton/>}
    {state.kind==="error"&&<Notice title="Project unavailable" role="alert">{state.error.message}</Notice>}
    {state.kind==="ready"&&<>
      <p className="text-muted">{state.data.project.name}</p>
      <div className="item-group">
        <ItemLink href={configUrl(id,"compose")} navigation={navigation} icon="▤"
          title="Compose YAML" detail="Revision-checked full-file editor"/>
        <ItemLink href={configUrl(id,"definitions")} navigation={navigation} icon="⚙"
          title="Applications & tasks" detail="Review and replace complete definitions"/>
        <ItemLink href={configUrl(id,"webhooks")} navigation={navigation} icon="↗"
          title="Webhook routes" detail="Project-scoped forwarding routes"/>
        <ItemLink href={configUrl(id,"name")} navigation={navigation} icon="✎"
          title="Project name" detail="Rename this registered project"/>
      </div>
    </>}
  </Shell>;
}
export function ComposeEditor({id,runtime,navigation}:{
  id:string;runtime:RuntimeSnapshot;navigation:Navigator;
}) {
  const loader=useCallback((signal:AbortSignal)=>readCompose(id,signal),[id]);
  const state=useResource(loader,0);
  const [content,setContent]=useState<string|null>(null);
  const [confirmed,setConfirmed]=useState(false);
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState<string|null>(null);
  const [success,setSuccess]=useState<string|null>(null);
  const current=state.kind==="ready"?state.data:null;
  const text=content??current?.content??"";
  const save=async()=>{
    if(!current||!editable(runtime)||!confirmed||busy||!text.trim())return;
    setBusy(true);setError(null);setSuccess(null);
    try {
      await verifyInactive(id);
      const value=await saveCompose(id,text,current.revision);
      setContent(value.content);
      setConfirmed(false);
      setSuccess("Compose configuration validated and saved at revision "+value.revision.slice(0,12)+". Refresh before another edit.");
    }catch(e){setError(message(e)+" Reload the latest Compose revision before retrying.");}
    finally{setBusy(false);}
  };
  return <ConfigShell title="Compose YAML" id={id} navigation={navigation}>
    <EditGuard runtime={runtime}/>
    <Notice title="Revision checked">Saving validates the entire Compose configuration and refuses a stale revision. Only edit while inactive.</Notice>
    {state.kind==="loading"&&<Skeleton/>}
    {state.kind==="error"&&<Notice title="Cannot read Compose" role="alert">{state.error.message}</Notice>}
    {current&&<>
      <p className="text-muted">Loaded revision: {current.revision.slice(0,12)}</p>
      <label className="form-stack">Managed compose.yaml
        <textarea rows={14} className="editor-textarea" value={text}
          disabled={!editable(runtime)||busy||Boolean(success)}
          spellCheck={false} onChange={e=>{setContent(e.target.value);setConfirmed(false);}}/>
      </label>
      {!success&&<label className="confirm-label"><input type="checkbox" checked={confirmed}
        disabled={!editable(runtime)} onChange={e=>setConfirmed(e.target.checked)}/>
        Replace the reviewed Compose file</label>}
      {!success&&<button type="button" className="primary-button"
        disabled={!editable(runtime)||!confirmed||busy||!text.trim()} onClick={()=>void save()}>
        {busy?"Validating…":"Validate and save Compose"}</button>}
    </>}
    <Feedback error={error} success={success}/>
    <ConfigBack id={id} navigation={navigation}/>
  </ConfigShell>;
}
export function DefinitionsEditor({id,runtime,navigation}:{
  id:string;runtime:RuntimeSnapshot;navigation:Navigator;
}) {
  const loader=useCallback((signal:AbortSignal)=>readReviewedSettings(id,signal),[id]);
  const state=useResource(loader,0);
  const [raw,setRaw]=useState<string|null>(null);
  const [confirmed,setConfirmed]=useState(false);
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState<string|null>(null);
  const [success,setSuccess]=useState<string|null>(null);
  const current=state.kind==="ready"?state.data:null;
  const initial=current?JSON.stringify({
    applications:current.applications.map(a=>({
      name:a.name,service:a.service,...(a.port==null?{}:{port:a.port}),
    })),
    tasks:current.tasks,
  },null,2):"";
  const text=raw??initial;
  const parsed=(()=>{
    try{const obj:unknown=JSON.parse(text);return validateCollection(obj)?obj:null;}catch{return null;}
  })();
  const save=async()=>{
    if(!current||!parsed||!confirmed||!editable(runtime)||busy)return;
    setBusy(true);setError(null);setSuccess(null);
    try{
      await verifyInactive(id);
      const value:ReviewedSettings={...parsed,revision:current.revision};
      await saveReviewedSettings(id,value);
      setConfirmed(false);
      setSuccess("Complete applications/tasks collection replaced with a revision check. Reload before editing again.");
    }catch(e){setError(message(e)+" No automatic overwrite: reload and review the latest definitions.");}
    finally{setBusy(false);}
  };
  return <ConfigShell title="Applications & tasks" id={id} navigation={navigation}>
    <EditGuard runtime={runtime}/>
    <Notice title="Whole-collection replacement">This editor submits both lists together, not individual rows. The controller compares the reviewed revision under its transition lock, rejecting concurrent edits.</Notice>
    {state.kind==="loading"&&<Skeleton/>}
    {state.kind==="error"&&<Notice title="Definitions unavailable" role="alert">{state.error.message}</Notice>}
    {current&&<>
      <p className="text-muted">Reviewed revision: {current.revision.slice(0,12)} · {current.applications.length} applications · {current.tasks.length} tasks</p>
      <label className="form-stack">Complete definitions JSON
        <textarea rows={16} className="editor-textarea" spellCheck={false}
          disabled={!editable(runtime)||busy||Boolean(success)}
          value={text} onChange={e=>{setRaw(e.target.value);setConfirmed(false);}}/>
      </label>
      {!parsed&&<Notice title="Invalid definitions" role="alert">Expected an object containing applications and tasks arrays with valid entry fields. No save is permitted.</Notice>}
      {!success&&<label className="confirm-label"><input type="checkbox" checked={confirmed}
        disabled={!editable(runtime)||!parsed} onChange={e=>setConfirmed(e.target.checked)}/>
        I approve replacing the full reviewed collection</label>}
      {!success&&<button type="button" className="primary-button"
        disabled={!editable(runtime)||!parsed||!confirmed||busy} onClick={()=>void save()}>
        {busy?"Saving…":"Save reviewed definitions"}</button>}
    </>}
    <Feedback error={error} success={success}/>
    <ConfigBack id={id} navigation={navigation}/>
  </ConfigShell>;
}
export function ProjectNameEditor({id,runtime,navigation}:{
  id:string;runtime:RuntimeSnapshot;navigation:Navigator;
}) {
  const loader=useCallback((signal:AbortSignal)=>readProject(id,signal),[id]);
  const state=useResource(loader,0);
  const [name,setName]=useState<string|null>(null);
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState<string|null>(null);
  const [success,setSuccess]=useState<string|null>(null);
  const project=state.kind==="ready"?state.data.project:null;
  const value=name??project?.name??"";
  const save=async()=>{
    if(!project||!editable(runtime)||!value.trim()||busy)return;
    setBusy(true);setError(null);setSuccess(null);
    try{
      await verifyInactive(id);
      const result=await renameProject(id,value.trim());
      setSuccess("Project renamed to "+result.project.name+".");
    }catch(e){setError(message(e));}
    finally{setBusy(false);}
  };
  return <ConfigShell title="Project name" id={id} navigation={navigation}>
    <EditGuard runtime={runtime}/>
    {state.kind==="loading"&&<Skeleton/>}
    {state.kind==="error"&&<Notice title="Project unavailable" role="alert">{state.error.message}</Notice>}
    {project&&<><p className="text-muted">Slug: {project.slug}</p>
      <div className="form-stack"><label>Project display name
        <input value={value} maxLength={100} disabled={!editable(runtime)||busy}
          onChange={e=>setName(e.target.value)}/></label></div>
      <button type="button" className="primary-button" disabled={!editable(runtime)||busy||!value.trim()}
        onClick={()=>void save()}>Save name</button></>}
    <Feedback error={error} success={success}/>
    <ConfigBack id={id} navigation={navigation}/>
  </ConfigShell>;
}
