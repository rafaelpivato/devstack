import {afterEach,describe,expect,it,vi} from "vitest";
import {readFileSync} from "node:fs";
import {renderToStaticMarkup} from "react-dom/server";
import {Status} from "../src/components/Shell";
import {applyAppearance,chooseAppearance,readAppearance,resolvedAppearance,watchAppearance} from "../src/lib/appearance";

afterEach(()=>vi.unstubAllGlobals());

function setupWindow(initial:string|null,systemDark=false){
  const values=new Map<string,string>();
  if(initial!==null)values.set("devstack-appearance-v1",initial);
  let listener:(()=>void)|undefined;
  const media={
    matches:systemDark,
    addEventListener:vi.fn((_name:string,cb:()=>void)=>{listener=cb;}),
    removeEventListener:vi.fn()
  };
  const localStorage={
    getItem:vi.fn((key:string)=>values.get(key)??null),
    setItem:vi.fn((key:string,value:string)=>{values.set(key,value);}),
    removeItem:vi.fn((key:string)=>{values.delete(key);})
  };
  const windowObject={
    localStorage,matchMedia:vi.fn(()=>media),
    dispatchEvent:vi.fn(),addEventListener:vi.fn(),removeEventListener:vi.fn()
  };
  const documentObject={documentElement:{dataset:{} as Record<string,string>}};
  vi.stubGlobal("window",windowObject);
  vi.stubGlobal("document",documentObject);
  vi.stubGlobal("localStorage",localStorage);
  return {windowObject,media,fireMedia:()=>listener?.(),localStorage,documentObject};
}

describe("device-local System/Light/Dark preference",()=>{
  it("defaults to System, rejecting stale/invalid stored preferences",()=>{
    const {documentObject}=setupWindow("previous-setting",true);
    expect(readAppearance()).toBe("system");
    applyAppearance();
    expect(documentObject.documentElement.dataset.theme).toBe("dark");
    expect(resolvedAppearance("system",false)).toBe("light");
    expect(resolvedAppearance("light",true)).toBe("light");
  });
  it("persists an override and removes it when System is selected",()=>{
    const {localStorage,documentObject}=setupWindow(null,true);
    chooseAppearance("light");
    expect(localStorage.setItem).toHaveBeenCalledWith("devstack-appearance-v1","light");
    expect(documentObject.documentElement.dataset.theme).toBe("light");
    chooseAppearance("system");
    expect(localStorage.removeItem).toHaveBeenCalledWith("devstack-appearance-v1");
    expect(documentObject.documentElement.dataset.theme).toBe("dark");
  });
  it("follows OS changes only in System and unsubscribes on teardown",()=>{
    const {media,fireMedia,documentObject}=setupWindow(null,false);
    const unwatch=watchAppearance();
    expect(documentObject.documentElement.dataset.theme).toBe("light");
    media.matches=true;
    fireMedia();
    expect(documentObject.documentElement.dataset.theme).toBe("dark");
    chooseAppearance("light");
    fireMedia();
    expect(documentObject.documentElement.dataset.theme).toBe("light");
    unwatch();
    expect(media.removeEventListener).toHaveBeenCalled();
  });
  it("fails safely when browser storage is unavailable",()=>{
    const {localStorage,documentObject}=setupWindow(null,false);
    localStorage.getItem.mockImplementation(()=>{throw Error("blocked");});
    localStorage.setItem.mockImplementation(()=>{throw Error("blocked");});
    expect(readAppearance()).toBe("system");
    expect(()=>chooseAppearance("dark")).not.toThrow();
    expect(documentObject.documentElement.dataset.theme).toBe("dark");
  });
});

// WCAG luminance plus Machado 100% deuteranopia transform in linear RGB.
// Automated checks supplement, and do not replace, real-device owner acceptance.
const css=readFileSync(new URL("../src/styles.css",import.meta.url),"utf8");
const rgb=(hex:string):number[]=>[1,3,5].map(offset=>{
  const c=parseInt(hex.slice(offset,offset+2),16)/255;
  return c<=.04045?c/12.92:((c+.055)/1.055)**2.4;
});
const matrix=[
  [.367322,.860646,-.227968],
  [.280085,.672501,.047413],
  [-.011820,.042940,.968881],
];
const simulated=(values:number[])=>matrix.map(row=>
  Math.max(0,Math.min(1,row.reduce((sum,v,i)=>sum+v*values[i],0))));
const luminance=(values:number[])=>values[0]*.2126+values[1]*.7152+values[2]*.0722;
const contrast=(a:string,b:string,simulate=false)=>{
  const aa=simulate?simulated(rgb(a)):rgb(a),bb=simulate?simulated(rgb(b)):rgb(b);
  const [low,high]=[luminance(aa),luminance(bb)].sort((x,y)=>x-y);
  return (high+.05)/(low+.05);
};
function token(theme:"light"|"dark",name:string){
  const prefix=theme==="light"?':root, :root[data-theme="light"] {':':root[data-theme="dark"] {';
  const section=css.split(prefix)[1]?.split("}")[0]??"";
  const value=new RegExp("--"+name+"\\s*:\\s*(#[0-9a-fA-F]{6})").exec(section)?.[1];
  if(!value)throw new Error("Missing semantic token: "+theme+"/"+name);
  return value;
}
describe("WCAG 2.2 AA plus deuteranopia simulation",()=>{
  for(const theme of ["light","dark"] as const){
    for(const simulate of [false,true]){
      const label=theme+(simulate?" simulated-deuteranopia":" standard");
      it(label+": legible regular text, muted/help, links, log text and button labels",()=>{
        for(const [fg,bg] of [
          ["text","panel"],["text","canvas"],["muted","panel"],
          ["accent","panel"],["text","code-bg"],["control-text","accent"]
        ]){
          expect(contrast(token(theme,fg),token(theme,bg),simulate)).toBeGreaterThanOrEqual(4.5);
        }
      });
      it(label+": 3:1 visible outlines and nontext boundaries",()=>{
        for(const [fg,bg] of [["line","panel"],["focus","panel"],["focus","canvas"]]){
          expect(contrast(token(theme,fg),token(theme,bg),simulate)).toBeGreaterThanOrEqual(3);
        }
      });
    }
  }
  it("never uses colored status indicators without textual/shape cues",()=>{
    const html=renderToStaticMarkup(Status({text:"Completed",kind:"positive"}));
    expect(html).toContain("Completed");
    expect(html).toContain("✓");
    const error=renderToStaticMarkup(Status({text:"Failed",kind:"warning"}));
    expect(error).toContain("Failed");
    expect(error).toContain("!");
  });
  it("covers separate light/dark code-editor, focus, input and selection surfaces",()=>{
    expect(css).toContain("background:var(--code-bg)");
    expect(css).toContain("outline:3px solid var(--focus)");
    expect(css).toContain("appearance-choice--selected");
    expect(css).toContain("--selection:");
  });
});
