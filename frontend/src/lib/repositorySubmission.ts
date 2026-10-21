/**
 * Session-only idempotency memory: keep a request key before submitting.
 * Never automatically retry an unknown POST. A user can explicitly retry
 * with the original key; a persisted receipt is the preferred recovery path.
 */
import { isProjectId } from "../navigation/routes";
export type RepositorySubmitMode="add"|"bulk";
export interface SavedRepositorySubmission {
  projectId:string;mode:RepositorySubmitMode;key:string;operationId?:string;
  form?:{url:string;name:string;branch:string};
}
const PREFIX="devstack-v06-repo-submission-v1-";
const validKey=(k:string)=>/^[0-9a-f]{32}$/.test(k);
export function requestKey():string {
  const bytes=new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes,b=>b.toString(16).padStart(2,"0")).join("");
}
export function loadSubmission(projectId:string,mode:RepositorySubmitMode):SavedRepositorySubmission|null {
  if(!isProjectId(projectId)) return null;
  try {
    const raw=sessionStorage.getItem(PREFIX+projectId+"-"+mode);
    if(!raw) return null;
    const data:unknown=JSON.parse(raw);
    if(!data||typeof data!=="object") return null;
    const v=data as Record<string,unknown>;
    if(v.projectId!==projectId||v.mode!==mode||typeof v.key!=="string"||!validKey(v.key)
      ||(v.operationId!==undefined&&(typeof v.operationId!=="string"||!isProjectId(v.operationId))))
      return null;
    if(mode==="add"&&(!v.form||typeof v.form!=="object"||
      !["url","name","branch"].every(k=>typeof (v.form as Record<string,unknown>)[k]==="string")))
      return null;
    return v as unknown as SavedRepositorySubmission;
  }catch{return null;}
}
export function saveSubmission(value:SavedRepositorySubmission):void {
  try{sessionStorage.setItem(PREFIX+value.projectId+"-"+value.mode,JSON.stringify(value));}catch{}
}
export function clearSubmission(projectId:string,mode:RepositorySubmitMode):void {
  try{sessionStorage.removeItem(PREFIX+projectId+"-"+mode);}catch{}
}
