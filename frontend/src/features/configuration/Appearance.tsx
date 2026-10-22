import {useEffect,useState} from "react";
import {APPEARANCE_EVENT,chooseAppearance,readAppearance,type Appearance} from "../../lib/appearance";

/** The setting lives in global administration, not the cramped mobile header. */
export function AppearanceControls() {
  const [choice,setChoice]=useState<Appearance>(readAppearance);
  useEffect(()=>{
    const sync=()=>setChoice(readAppearance());
    window.addEventListener("storage",sync);
    window.addEventListener(APPEARANCE_EVENT,sync);
    return ()=>{
      window.removeEventListener("storage",sync);
      window.removeEventListener(APPEARANCE_EVENT,sync);
    };
  },[]);
  const select=(value:Appearance)=>{setChoice(value);chooseAppearance(value);};
  return <fieldset className="appearance-options">
    <legend>Appearance</legend>
    <p className="text-muted">This browser only. System follows your device's light/dark setting.</p>
    {(["system","light","dark"] as const).map(value=>
      <label key={value} className={"appearance-choice"+(choice===value?" appearance-choice--selected":"")}>
        <input type="radio" name="devstack-appearance" value={value} checked={choice===value}
          onChange={()=>select(value)}/>
        <span>{value==="system"?"System":value==="light"?"Light":"Dark"}</span>
        {choice===value&&<strong className="appearance-selected" aria-hidden="true">✓ Selected</strong>}
      </label>)}
  </fieldset>;
}
