import {useCallback,useState} from "react";
import {readDiagnostics,diagnosticText,type DiagnosticSnapshot} from "../../api/diagnostics";
import {useResource} from "../../hooks/useResource";
import {Notice,Shell,Skeleton} from "../../components/Shell";
import type {Navigator} from "../../navigation/useNavigation";

const WINDOWS=[1,5,30] as const;
export function DiagnosticsScreen({navigation}:{navigation:Navigator}){
  const [minutes,setMinutes]=useState<1|5|30>(5);
  const [refresh,setRefresh]=useState(0);
  const [copy,setCopy]=useState<string|null>(null);
  const loader=useCallback((signal:AbortSignal)=>readDiagnostics(minutes,signal),[minutes,refresh]);
  const state=useResource(loader,0);
  const data:DiagnosticSnapshot|null=state.kind==="ready"?state.data:null;
  const text=data?diagnosticText(data):"";
  const copyNow=async()=>{
    if(!data)return;
    try {
      if(!navigator.clipboard?.writeText)throw Error("unavailable");
      await navigator.clipboard.writeText(text);
      setCopy("Diagnostic snapshot copied. Share only with people you trust.");
    }catch{
      setCopy("Clipboard unavailable. Select the snapshot text below and copy it.");
    }
  };
  return <Shell title="DevStack diagnostics" subtitle="Global, read-only · No project activation" navigation={navigation} back>
    <Notice title="Safe diagnostic snapshot">
      Only allowlisted DevStack process events, runtime receipts and systemd updater metadata.
      This is not arbitrary host/container output. Nothing is uploaded or copied automatically.
    </Notice>
    <div className="diagnostic-controls">
      <label htmlFor="diagnostic-window">Lookback</label>
      <select id="diagnostic-window" value={minutes} onChange={e=>{
        setMinutes(Number(e.target.value) as 1|5|30);setCopy(null);
      }}>{WINDOWS.map(value=><option value={value} key={value}>Last {value} minute{value===1?"":"s"}</option>)}</select>
      <button type="button" className="text-button" onClick={()=>{setRefresh(x=>x+1);setCopy(null);}}>Refresh</button>
    </div>
    {state.kind==="loading"&&<Skeleton/>}
    {state.kind==="error"&&<Notice role="alert" title="Diagnostics unavailable">{state.error.message}</Notice>}
    {data&&<>
      <section className="card">
        <div className="property"><span>Installed</span><strong>{data.version}</strong></div>
        <div className="property"><span>Captured</span><strong>{new Date(data.retrieved_at).toLocaleString()} ({Intl.DateTimeFormat().resolvedOptions().timeZone})</strong></div>
        <div className="property"><span>Events</span><strong>{data.events.length}</strong></div>
        <div className="section-head">Source coverage</div>
        {(Object.keys(data.sources) as Array<keyof typeof data.sources>).map(source=>
          <div className="property" key={source}><span>{source}</span><strong>{data.sources[source]}</strong></div>)}
        {data.partial&&<Notice title="Incomplete snapshot" role="alert">Some sources were unavailable or returned status only. No events does not imply no errors.</Notice>}
        {data.truncated&&<Notice title="Truncated events" role="alert">The bounded maximum was reached. Narrow the time window for more detail.</Notice>}
        {data.updater_status&&<details className="technical-details">
          <summary>Updater status (latest sampled values)</summary>
          <div className="property"><span>Phase</span><strong>{data.updater_status.phase}</strong></div>
          <div className="property"><span>Unit</span><strong>{data.updater_status.unit_state}</strong></div>
          <div className="property"><span>Result</span><strong>{data.updater_status.last_result}</strong></div>
          <div className="property"><span>Exit</span><strong>{data.updater_status.last_exit_code||"n/a"}</strong></div>
        </details>}
      </section>
      <section className="card">
        <h2>Events · oldest to newest</h2>
        {data.events.length===0?<p>No allowlisted events in this interval. Check coverage above.</p>:
          <ol className="diagnostic-events">
            {data.events.map((event,index)=><li key={index}>
              <time dateTime={new Date(event.at).toISOString()}>{new Date(event.at).toLocaleString()}</time>
              <strong> {event.source} · {event.level}</strong>
              <div>{event.code}</div>
              {event.action&&<div>Action: {event.action}</div>}
              {event.operation_id&&<div>Receipt: <code>{event.operation_id}</code></div>}
              {event.error_code&&<div>Error: {event.error_code}</div>}
            </li>)}
          </ol>}
      </section>
      <div className="log-actions">
        <button type="button" className="text-button" onClick={()=>void copyNow()}>Copy diagnostic snapshot</button>
      </div>
      {copy&&<p role="status">{copy}</p>}
      <details className="technical-details">
        <summary>View/select full copyable snapshot</summary>
        <textarea className="diagnostic-copy-source" value={text} readOnly
          aria-label="Copyable DevStack diagnostic snapshot"/>
      </details>
    </>}
  </Shell>;
}
