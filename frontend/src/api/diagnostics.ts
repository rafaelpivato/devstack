import {apiRequest} from "./client";
export type DiagnosticSource = "controller"|"web"|"runtime"|"updater"|"mcp";
export interface DiagnosticEvent {
  at:number; source:DiagnosticSource; level:"info"|"warning"|"error"; code:string;
  operation_id?:string; action?:string; error_code?:string|null;
}
export interface DiagnosticSnapshot {
  retrieved_at:string; version:string; minutes:1|5|30;
  events:DiagnosticEvent[]; truncated:boolean; partial:boolean;
  sources:Record<DiagnosticSource,string>;
  web_storage:{mode:"persistent"|"volatile"|"unavailable";error_code:string|null};
  updater_status:{phase:string;unit_state:string;last_result:string;last_exit_code:string}|null;
  network_preflight?:import("./configuration").NetworkPreflight;
}
export function readDiagnostics(minutes:1|5|30,signal?:AbortSignal):Promise<DiagnosticSnapshot>{
  return apiRequest<DiagnosticSnapshot>("/api/v2/devstack/diagnostics?minutes="+minutes,{signal});
}
export function diagnosticText(snapshot:DiagnosticSnapshot):string{
  const lines=[
    "DevStack diagnostic snapshot (selected safe events; no raw host logs)",
    "Installed: "+snapshot.version,
    "Captured: "+snapshot.retrieved_at,
    "Lookback: "+snapshot.minutes+" minute(s)",
    "Sources: "+(["controller","web","runtime","updater","mcp"] as const)
      .map(source=>source+"="+snapshot.sources[source]).join("; "),
    "Web event storage: "+snapshot.web_storage.mode+
      (snapshot.web_storage.error_code?" (reason="+snapshot.web_storage.error_code+")":""),
    snapshot.partial?"WARNING: Some sources are unavailable or partial.":"All listed sources available.",
    snapshot.truncated?"WARNING: Event list truncated.":"Event list not truncated.",
  ];
  if(snapshot.network_preflight){
    const p=snapshot.network_preflight;
    lines.push("Local Funnel preflight: health="+p.health+", tailscale="+p.tailscale+
      ", identity="+p.identity+", private="+p.private_dashboard+
      ", public="+p.public_funnel+", gateway="+p.gateway+
      ", external="+p.external_reachability+
      ", reason="+(p.reason??"not_reported"));
  }
  if(snapshot.updater_status){
    const u=snapshot.updater_status;
    lines.push("Updater (sampled): phase="+u.phase+", unit="+u.unit_state+
      ", result="+u.last_result+", exit="+u.last_exit_code);
  }
  lines.push("Events (chronological):");
  for(const event of snapshot.events){
    const timestamp=new Date(event.at).toISOString();
    lines.push(timestamp+" ["+event.source+"/"+event.level+"] "+event.code+
      (event.action?" action="+event.action:"")+
      (event.operation_id?" receipt="+event.operation_id:"")+
      (event.error_code?" error="+event.error_code:""));
  }
  if(!snapshot.events.length)lines.push("(No allowlisted events in this window.)");
  return lines.join("\n");
}
