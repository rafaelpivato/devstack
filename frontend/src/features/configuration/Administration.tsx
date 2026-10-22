import { useCallback, useState } from "react";
import {
  readGitHubIdentity, createGitHubIdentity, testGitHubIdentity, readReleaseStatus,
  startReleaseUpdate,
} from "../../api/configuration";
import type { RuntimeSnapshot } from "../../api/runtime";
import { readRuntime } from "../../api/runtime";
import { useResource } from "../../hooks/useResource";
import { ItemLink, Notice, Shell, Skeleton } from "../../components/Shell";
import { NavLink, GLOBAL_PROJECTS, type Navigator } from "../../navigation/useNavigation";
import { ADMIN_ROOT, adminUrl } from "../../navigation/routes";
import { Feedback, message, editable } from "./Common";
import { AppearanceControls } from "./Appearance";

export function AdministrationHome({navigation}:{navigation:Navigator}) {
  return <Shell title="DevStack administration" subtitle="Global — not a project" navigation={navigation} back>
    <Notice title="Secondary controls">These settings affect DevStack itself and are intentionally outside the active project's workspace.</Notice>
    <AppearanceControls/>
    <div className="item-group">
      <ItemLink icon="⑂" title="GitHub SSH" detail="Public key and connectivity"
        href={adminUrl("ssh")} navigation={navigation}/>
      <ItemLink icon="↻" title="Published updates" detail="Installed release and guarded updater"
        href={adminUrl("updates")} navigation={navigation}/>
    </div>
    <p className="actions"><NavLink to={GLOBAL_PROJECTS} navigate={navigation.navigate}>Projects →</NavLink></p>
  </Shell>;
}
export function GitHubSSHScreen({navigation}:{navigation:Navigator}) {
  const [serial,setSerial]=useState(0);
  const loader=useCallback((signal:AbortSignal)=>readGitHubIdentity(signal),[serial]);
  const state=useResource(loader,0);
  const [confirmation,setConfirmation]=useState(false);
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState<string|null>(null);
  const [success,setSuccess]=useState<string|null>(null);
  const [copy,setCopy]=useState<string|null>(null);
  const [user,setUser]=useState<string|null>(null);
  const run=async(kind:"generate"|"test")=>{
    if(busy || (kind==="generate"&&!confirmation))return;
    setBusy(true);setError(null);setSuccess(null);
    try{
      if(kind==="generate"){
        await createGitHubIdentity();
        setConfirmation(false);setSerial(x=>x+1);
        setSuccess("SSH identity generated. Register its public key in GitHub before testing.");
      }else{
        const result=await testGitHubIdentity();
        setUser(result.authenticated_user);
        setSuccess("GitHub SSH authentication verified.");
      }
    }catch(e){setError(message(e));}
    finally{setBusy(false);}
  };
  const publicKey=state.kind==="ready"?state.data.public_key:null;
  const copyKey=async()=>{
    try {
      if(!publicKey||!navigator.clipboard?.writeText)throw Error();
      await navigator.clipboard.writeText(publicKey);
      setCopy("Public key copied");
    }catch{setCopy("Clipboard unavailable. Select and copy the key text.");}
  };
  return <Shell title="GitHub SSH" navigation={navigation} back>
    <Notice title="Public identity only">DevStack never exposes the private SSH key in the browser. Generating an identity is an explicit global operation.</Notice>
    {state.kind==="loading"&&<Skeleton/>}
    {state.kind==="error"&&<Notice title="Identity unavailable" role="alert">{state.error.message}</Notice>}
    {publicKey?<section className="card">
      <strong>Public key</strong>
      <pre className="log-view" aria-label="GitHub public SSH key">{publicKey}</pre>
      <button type="button" className="text-button" onClick={()=>void copyKey()}>Copy public key</button>
      {copy&&<p className="text-muted" role="status">{copy}</p>}
    </section>:state.kind==="ready"&&<Notice title="No identity">Generate a key to connect DevStack to GitHub.</Notice>}
    <button type="button" className="primary-button" disabled={busy} onClick={()=>void run("test")}>
      {busy?"Checking…":"Test GitHub SSH"}</button>
    {user&&<p className="text-muted">Authenticated user: {user}</p>}
    <div className="section-head">Key generation</div>
    <label className="confirm-label"><input type="checkbox" checked={confirmation}
      onChange={e=>setConfirmation(e.target.checked)}/>
      {publicKey?"I acknowledge this could affect the registered GitHub identity":"Create a new DevStack SSH identity"}</label>
    <button type="button" className="primary-button" disabled={!confirmation||busy}
      onClick={()=>void run("generate")}>Generate SSH identity</button>
    <Feedback error={error} success={success}/>
    <p className="actions"><NavLink to={ADMIN_ROOT} navigate={navigation.navigate}>Administration →</NavLink></p>
  </Shell>;
}
export function UpdatesScreen({runtime,navigation}:{runtime:RuntimeSnapshot;navigation:Navigator}) {
  const [check,setCheck]=useState(false);
  const [serial,setSerial]=useState(0);
  const load=useCallback((signal:AbortSignal)=>readReleaseStatus(check,signal),[check,serial]);
  const state=useResource(load,0);
  const [confirmation,setConfirmation]=useState(false);
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState<string|null>(null);
  const [success,setSuccess]=useState<string|null>(null);
  const data=state.kind==="ready"?state.data:null;
  const canUpdate=editable(runtime)&&Boolean(data?.update_available)&&!data?.check_error;
  const start=async()=>{
    if(!confirmation||!canUpdate||busy)return;
    setBusy(true);setError(null);setSuccess(null);
    try{
      const verified=await readRuntime();
      if(!editable(verified))throw Error("Runtime changed; no update requested.");
    }catch(e){
      setError(message(e));setBusy(false);return;
    }
    try {
      const result=await startReleaseUpdate();
      setSuccess(result.message+" DevStack may restart during the update.");
      setSerial(x=>x+1);setConfirmation(false);
    }catch(e){setError(message(e)+" Check the updater status before trying again.");}
    finally{setBusy(false);}
  };
  return <Shell title="Published updates" navigation={navigation} back>
    <Notice title="Safe release updater">Checking published versions is always read-only, even with an active project. Installing a release requires an inactive runtime; the host independently rechecks this.</Notice>
    {state.kind==="loading"&&<Skeleton/>}
    {state.kind==="error"&&<Notice title="Update status unavailable" role="alert">{state.error.message}</Notice>}
    {data&&<section className="card">
      <div className="property"><span>Installed</span><strong>{data.installed}</strong></div>
      <div className="property"><span>Published</span><strong>{data.latest??"Not checked"}</strong></div>
      <div className="property"><span>Update available</span><strong>{data.update_available?"Yes":"Not confirmed"}</strong></div>
      {data.check_error&&<p role="alert">Release check failed: {data.check_error}</p>}
      {data.update&&<details className="technical-details" open><summary>System updater status</summary>
        <div className="property"><span>Phase</span><strong>{data.update.phase}</strong></div>
        <div className="property"><span>Unit</span><strong>{data.update.unit_state}</strong></div>
        <div className="property"><span>Last result</span><strong>{data.update.last_result}</strong></div>
        <div className="property"><span>Exit code</span><code>{data.update.last_exit_code||"not available"}</code></div>
      </details>}
    </section>}
    <button className="text-button" type="button" onClick={()=>{setCheck(true);setSerial(x=>x+1);}}>
      Check published releases (read only)</button>
    {!editable(runtime)&&<Notice title="Runtime must be inactive">Deactivate the active project before requesting a DevStack release update.</Notice>}
    {data?.update_available&&<label className="confirm-label">
      <input type="checkbox" checked={confirmation} onChange={e=>setConfirmation(e.target.checked)}/>
      Install this published release; DevStack itself may restart
    </label>}
    <button type="button" className="primary-button" disabled={!confirmation||!canUpdate||busy}
      onClick={()=>void start()}>{busy?"Requesting…":"Request published update"}</button>
    <Feedback error={error} success={success}/>
    <p className="actions"><NavLink to={ADMIN_ROOT} navigate={navigation.navigate}>Administration →</NavLink></p>
  </Shell>;
}
