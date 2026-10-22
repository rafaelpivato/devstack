import { useState, type PropsWithChildren } from "react";
import type { RuntimeSnapshot } from "../../api/runtime";
import { Notice, Shell } from "../../components/Shell";
import { laneUrl } from "../../navigation/routes";
import { NavLink, type Navigator } from "../../navigation/useNavigation";

export const canOperate=(runtime:RuntimeSnapshot,id:string)=>
  runtime.phase==="active"&&runtime.project_id===id;

export function WorkloadShell({title,id,navigation,children}:PropsWithChildren<{
  title:string;id:string;navigation:Navigator;
}>) {
  return <Shell title={title} navigation={navigation} back>{children}</Shell>;
}
export function ActiveOnly({id,runtime}:{id:string;runtime:RuntimeSnapshot}) {
  return canOperate(runtime,id)?null:
    <Notice title="Not the active project">Service inspection and new jobs require the globally active project. Viewing this project does not activate it.</Notice>;
}
export function SectionLink({id,lane,label,navigation}:{
  id:string;lane:"jobs"|"services"|"applications";label:string;navigation:Navigator;
}) {
  return <p className="actions"><NavLink navigate={navigation.navigate} to={laneUrl(id,lane)}>{label} →</NavLink></p>;
}
export function ReadError({message}:{message:string}) {
  return <Notice title="Cannot load data" role="alert">{message}</Notice>;
}
export function TextLog({text,label}:{text:string;label:string}) {
  return <pre className="log-view" aria-label={label}>{text||"No output in this snapshot."}</pre>;
}
export function CopyLog({value}:{value:string}) {
  const [feedback,setFeedback]=useState<string|null>(null);
  const copy=async()=>{
    try{
      if(!navigator.clipboard?.writeText) throw Error("Clipboard access unavailable");
      await navigator.clipboard.writeText(value);
      setFeedback("Copied to clipboard");
    }catch{setFeedback("Clipboard unavailable; select and copy the visible output.");}
  };
  return <span className="copy-log-control">
    <button className="text-button" type="button" disabled={!value} onClick={()=>void copy()}>Copy output</button>
    {feedback&&<span className="text-muted" role="status">{feedback}</span>}
  </span>;
}
