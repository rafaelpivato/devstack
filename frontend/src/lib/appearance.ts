/** Device-local appearance; no server setting or cross-user coupling. */
export type Appearance = "system" | "light" | "dark";
export const APPEARANCE_KEY = "devstack-appearance-v1";
export const APPEARANCE_EVENT = "devstack-appearance-changed";
export function readAppearance():Appearance {
  if(typeof window==="undefined")return "system";
  try {const v=localStorage.getItem(APPEARANCE_KEY);return v==="light"||v==="dark"?v:"system";}
  catch{return "system";}
}
export function resolvedAppearance(choice:Appearance,systemDark:boolean):"light"|"dark"{
  return choice==="system"?(systemDark?"dark":"light"):choice;
}
export function applyAppearance():void {
  const dark=window.matchMedia?.("(prefers-color-scheme: dark)").matches??false;
  document.documentElement.dataset.theme=resolvedAppearance(readAppearance(),dark);
}
export function chooseAppearance(choice:Appearance):void {
  try {
    if(choice==="system")localStorage.removeItem(APPEARANCE_KEY);
    else localStorage.setItem(APPEARANCE_KEY,choice);
  }catch{/* Browser storage may be denied; current-page choice still applies. */}
  const dark=window.matchMedia?.("(prefers-color-scheme: dark)").matches??false;
  document.documentElement.dataset.theme=resolvedAppearance(choice,dark);
  window.dispatchEvent(new Event(APPEARANCE_EVENT));
}
export function watchAppearance():()=>void {
  const media=window.matchMedia?.("(prefers-color-scheme: dark)");
  const onChange=()=>applyAppearance();
  media?.addEventListener?.("change",onChange);
  window.addEventListener("storage",onChange);
  applyAppearance();
  return ()=>{
    media?.removeEventListener?.("change",onChange);
    window.removeEventListener("storage",onChange);
  };
}
