import { apiRequest } from "./client";

export interface RenameResult {
  code:string; at:number; old_dns:string|null; new_dns:string|null;
  old_urls?:{private_url:string;mcp_url:string};
  new_urls?:{private_url:string;mcp_url:string};
}
export interface TailscaleNetwork {
  connected:boolean;
  name:string|null;
  fqdn:string|null;
  private_url:string|null;
  mcp_url:string|null;
  network_phase:string;
  request_pending:boolean;
  last_result:RenameResult|null;
}
export const readTailscaleNetwork=(signal?:AbortSignal)=>
  apiRequest<TailscaleNetwork>("/api/v2/devstack/network/tailscale",{signal});
export const renameTailscaleDevice=(name:string,expected_dns:string)=>
  apiRequest<{message:string;request_pending:boolean}>(
    "/api/v2/devstack/network/tailscale/rename",
    {method:"POST",body:{name,expected_dns}});
