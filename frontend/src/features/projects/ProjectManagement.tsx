import {useCallback,useState} from "react";
import {createProject,githubSourceValid,projectNameValid,projectSlugValid,
  readProjectImport,submitProjectImport} from "../../api/projectManagement";
import type {ImportReceipt} from "../../api/projectManagement";
import {readRuntime} from "../../api/runtime";
import type {RuntimeSnapshot} from "../../api/runtime";
import {useResource} from "../../hooks/useResource";
import {Notice,Shell,Skeleton,Status} from "../../components/Shell";
import {NavLink,type Navigator} from "../../navigation/useNavigation";
import {isProjectId, projectUrl, GLOBAL_PROJECTS_URL, importReceiptUrl} from "../../navigation/routes";
import {freshKey} from "../runtime/RuntimeScreens";

const KEY="devstack-v06-pending-project-import-v1";
interface SavedImport {key:string;url:string;slug:string;name:string;receiptId?:string}
function validSaved(v:unknown):v is SavedImport {
  if(!v||typeof v!=="object")return false;
  const x=v as Record<string,unknown>;
  return typeof x.key==="string"&&/^[a-f0-9]{32}$/.test(x.key)&&
    typeof x.url==="string"&&typeof x.slug==="string"&&typeof x.name==="string"&&
    (x.receiptId===undefined||(typeof x.receiptId==="string"&&isProjectId(x.receiptId)));
}
function loadSaved():SavedImport|null {
  try{const value=sessionStorage.getItem(KEY);if(!value)return null;
    const obj:unknown=JSON.parse(value);return validSaved(obj)?obj:null;
  }catch{return null;}
}
function store(v:SavedImport|null){
  try{if(v)sessionStorage.setItem(KEY,JSON.stringify(v));else sessionStorage.removeItem(KEY);}catch{}
}
const mayCreate=(runtime:RuntimeSnapshot)=>runtime.phase==="inactive"&&runtime.project_id===null;
function FormField({title,value,onChange,placeholder}:{
  title:string;value:string;onChange:(value:string)=>void;placeholder?:string;
}){
  return <label>{title}<input value={value} autoCapitalize="off" autoCorrect="off"
    spellCheck={false} placeholder={placeholder} maxLength={300}
    onChange={e=>onChange(e.target.value)}/></label>;
}
export function CreateProjectScreen({runtime,navigation}:{
  runtime:RuntimeSnapshot;navigation:Navigator;
}){
  const [slug,setSlug]=useState("");
  const [name,setName]=useState("");
  const [busy,setBusy]=useState(false);
  const [unknown,setUnknown]=useState(false);
  const [error,setError]=useState<string|null>(null);
  const allowed=mayCreate(runtime)&&projectSlugValid(slug)&&projectNameValid(name)&&!unknown;
  const submit=async()=>{
    if(!allowed||busy)return;
    setBusy(true);setError(null);
    try{if(!mayCreate(await readRuntime()))throw Error("Runtime changed. Nothing submitted.");}
    catch(e){setError(e instanceof Error?e.message:"Cannot verify runtime");setBusy(false);return;}
    try{const result=await createProject(slug,name.trim());
      navigation.navigate(projectUrl(result.project.id));
    }catch(e){setUnknown(true);setError((e instanceof Error?e.message:"Request outcome unknown")+
      " Check the project directory before trying again; creating a project is not idempotent.");}
    finally{setBusy(false);}
  };
  return <Shell title="Create project" navigation={navigation} back>
    <Notice title="Empty project">Creates a registered project without activating it. The runtime must be inactive.</Notice>
    {!mayCreate(runtime)&&<Notice title="Unavailable" role="alert">Deactivate the running project before creation.</Notice>}
    <div className="form-stack">
      <FormField title="Project slug" value={slug} onChange={setSlug} placeholder="my-project"/>
      <FormField title="Display name" value={name} onChange={setName} placeholder="My project"/>
    </div>
    <p className="text-muted">Slug: lowercase letters, digits and hyphens; must start with a letter.</p>
    <button type="button" className="primary-button" disabled={!allowed||busy}
      onClick={()=>void submit()}>{busy?"Creating…":"Create project"}</button>
    {error&&<Notice title="Create not confirmed" role="alert">{error}</Notice>}
    <p className="actions"><NavLink to={GLOBAL_PROJECTS_URL} navigate={navigation.navigate}>Inspect project directory →</NavLink></p>
  </Shell>;
}
export function ImportProjectScreen({runtime,navigation}:{
  runtime:RuntimeSnapshot;navigation:Navigator;
}){
  const [saved,setSaved]=useState<SavedImport|null>(loadSaved);
  const [url,setUrl]=useState(saved?.url??"");
  const [slug,setSlug]=useState(saved?.slug??"");
  const [name,setName]=useState(saved?.name??"");
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState<string|null>(null);
  const matched=Boolean(saved&&saved.url===url&&saved.slug===slug&&saved.name===name);
  const allowed=mayCreate(runtime)&&githubSourceValid(url)&&projectSlugValid(slug)&&
    projectNameValid(name)&&(!saved||matched);
  const submit=async()=>{
    if(!allowed||busy)return;
    setBusy(true);setError(null);
    try{if(!mayCreate(await readRuntime()))throw Error("Runtime changed. Nothing submitted.");}
    catch(e){setError(e instanceof Error?e.message:"Cannot verify runtime");setBusy(false);return;}
    const current=saved??{key:freshKey(),url,slug,name};
    store(current);setSaved(current);
    try{const r=await submitProjectImport(url,slug,name,current.key);
      const next={...current,receiptId:r.import.id};
      store(next);setSaved(next);navigation.navigate(importReceiptUrl(r.import.id));
    }catch(e){setError((e instanceof Error?e.message:"Import outcome unknown")+
      " Inspect the previous receipt or retry using the same request key; do not create a new import.");}
    finally{setBusy(false);}
  };
  return <Shell title="Import from GitHub" navigation={navigation} back>
    <Notice title="Durable import">Controller-owned GitHub import. The operation continues if this browser disconnects. It never activates the imported project.</Notice>
    {!mayCreate(runtime)&&<Notice title="Unavailable" role="alert">Deactivate the runtime before importing a project.</Notice>}
    <div className="form-stack">
      <FormField title="GitHub HTTPS URL" value={url} onChange={setUrl} placeholder="https://github.com/owner/repository"/>
      <FormField title="Project slug" value={slug} onChange={setSlug} placeholder="new-project"/>
      <FormField title="Display name" value={name} onChange={setName} placeholder="New project"/>
    </div>
    {saved&&<Notice title="Previous import request">
      {saved.receiptId?"A durable receipt is recorded.":"A request key was retained before sending."}
      {saved.receiptId&&<p><NavLink to={importReceiptUrl(saved.receiptId)}
        navigate={navigation.navigate}>Open receipt →</NavLink></p>}
      {!matched&&"The saved submission has different fields. Check its outcome before starting another."}
    </Notice>}
    <button type="button" className="primary-button" disabled={!allowed||busy}
      onClick={()=>void submit()}>{busy?"Submitting…":saved?"Retry saved import key":"Submit import"}</button>
    {error&&<Notice title="Import not confirmed" role="alert">{error}</Notice>}
    {saved&&<p className="actions"><button type="button" className="text-button"
      onClick={()=>{store(null);setSaved(null);setError(null);}}>Start a new import (after checking prior result)</button></p>}
  </Shell>;
}
export function ImportReceiptScreen({id,navigation}:{id:string;navigation:Navigator}){
  const load=useCallback((signal:AbortSignal)=>readProjectImport(id,signal),[id]);
  const state=useResource<ImportReceipt>(load,3000);
  const receipt=state.kind==="ready"?state.data:null;
  return <Shell title="Import status" navigation={navigation} back>
    {state.kind==="loading"&&<Skeleton/>}
    {state.kind==="error"&&<Notice title="Cannot inspect import" role="alert">{state.error.message} The result is unknown until the controller can be reached.</Notice>}
    {receipt&&<section className="card">
      <div className="property"><span>Project</span><strong>{receipt.name}</strong></div>
      <div className="property"><span>Slug</span><code>{receipt.slug}</code></div>
      <div className="property"><span>Status</span><Status text={receipt.status}
        kind={receipt.status==="succeeded"?"positive":receipt.status==="failed"?"warning":"neutral"}/></div>
      {receipt.error_code&&<div className="property"><span>Error</span><code>{receipt.error_code}</code></div>}
    </section>}
    {receipt?.status==="succeeded"&&receipt.project_id&&
      <p className="actions"><NavLink to={projectUrl(receipt.project_id)}
        navigate={navigation.navigate}>Inspect imported project →</NavLink></p>}
    {receipt?.status==="failed"&&<Notice title="Import failed" role="alert">The controller did not complete the import. Inspect the project directory before starting a new request.</Notice>}
    {receipt&&["queued","running"].includes(receipt.status)&&
      <Notice title="Controller working">You can safely leave this page and revisit this operation.</Notice>}
    <p className="actions"><NavLink to={GLOBAL_PROJECTS_URL} navigate={navigation.navigate}>Projects →</NavLink></p>
  </Shell>;
}
