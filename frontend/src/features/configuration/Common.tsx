import type { PropsWithChildren } from "react";
import type { RuntimeSnapshot } from "../../api/runtime";
import type { Navigator } from "../../navigation/useNavigation";
import { NavLink } from "../../navigation/useNavigation";
import { laneUrl } from "../../navigation/routes";
import { Notice, Shell } from "../../components/Shell";

export const editable=(runtime:RuntimeSnapshot)=>runtime.phase==="inactive"&&runtime.project_id===null;
export const message=(e:unknown)=>e instanceof Error?e.message:"Operation could not be completed";
export async function verifyInactive(_id:string){
  const {readRuntime}=await import("../../api/runtime");
  const status=await readRuntime();
  if(!editable(status))throw Error("Runtime is not inactive. No edit submitted.");
}
export function ConfigShell({id,title,navigation,children}:PropsWithChildren<{id:string;title:string;navigation:Navigator}>) {
  return <Shell title={title} navigation={navigation} back>{children}</Shell>;
}
export function EditGuard({runtime}:{runtime:RuntimeSnapshot}){
  return editable(runtime)?null:<Notice title="View only">Deactivate the global runtime before editing managed project configuration.</Notice>;
}
export function Feedback({error,success}:{error:string|null;success:string|null}){
  return <>{error&&<Notice title="Save not confirmed" role="alert">{error}</Notice>}
    {success&&<Notice title="Saved">{success}</Notice>}</>;
}
export function ConfigBack({id,navigation}:{id:string;navigation:Navigator}){
  return <p className="actions"><NavLink to={laneUrl(id,"configuration")} navigate={navigation.navigate}>Project configuration →</NavLink></p>;
}
