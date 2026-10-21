import { describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { apiRequest } from "../src/api/client";
import { validateCollection } from "../src/api/configuration";
import { editable } from "../src/features/configuration/Common";
import { ConfigurationHome } from "../src/features/configuration/ProjectConfiguration";
import { AdministrationHome } from "../src/features/configuration/Administration";
import { ADMIN_ROOT, adminUrl, configUrl, laneUrl, parentUrl, routeFor } from "../src/navigation/routes";
import type { RuntimeSnapshot } from "../src/api/runtime";
import type { Navigator } from "../src/navigation/useNavigation";
const A="11111111-1111-4111-8111-111111111111";
const nav={route:{kind:"entry"},navigate:vi.fn(),back:vi.fn()} as unknown as Navigator;
const state=(phase:RuntimeSnapshot["phase"],id:string|null):RuntimeSnapshot=>({phase,project_id:id});
describe("configuration and admin lane",()=>{
  it("uses hierarchical parent and strict deep-link names",()=>{
    expect(routeFor(configUrl(A,"compose"))).toEqual({kind:"config-detail",projectId:A,screen:"compose"});
    expect(parentUrl(routeFor(configUrl(A,"webhooks")))).toBe(laneUrl(A,"configuration"));
    expect(routeFor(ADMIN_ROOT)).toEqual({kind:"administration"});
    expect(routeFor(adminUrl("ssh"))).toEqual({kind:"admin-detail",screen:"ssh"});
    expect(parentUrl(routeFor(adminUrl("updates")))).toBe(ADMIN_ROOT);
    expect(routeFor(configUrl(A,"compose")+"/shell")).toEqual({kind:"not-found"});
    expect(routeFor(ADMIN_ROOT+"/terminal")).toEqual({kind:"not-found"});
  });
  it("never edits managed configuration if the global runtime is not inactive",()=>{
    expect(editable(state("inactive",null))).toBe(true);
    expect(editable(state("active",A))).toBe(false);
    expect(editable(state("switching",A))).toBe(false);
    expect(editable(state("error",A))).toBe(false);
    const html=renderToStaticMarkup(<ConfigurationHome id={A} runtime={state("active",A)} navigation={nav}/>);
    expect(html).toContain("View only");
    expect(nav.navigate).not.toHaveBeenCalled();
    expect(renderToStaticMarkup(<AdministrationHome navigation={nav}/>)).toContain("secondary");
  });
  it("requires full arrays and exact properties before collection writes",()=>{
    expect(validateCollection({applications:[],tasks:[]})).toBe(true);
    expect(validateCollection({applications:[],tasks:[],extra:true})).toBe(false);
    expect(validateCollection({applications:[{name:"web",service:"api",port:8000}],tasks:[]})).toBe(true);
    expect(validateCollection({applications:[{name:"web",service:"api",port:null}],tasks:[]})).toBe(false);
    expect(validateCollection({applications:[],tasks:[{name:"test",service:"web",mode:"exec",image:"",network:"none",command:["pytest"]}]})).toBe(true);
    expect(validateCollection({applications:[],tasks:[{name:"test",command:"pytest"}]})).toBe(false);
  });
  it("transports revision and all collections as one same-origin CSRF-protected PUT",async()=>{
    const calls:RequestInit[]=[];
    const transport=vi.fn(async (_url:RequestInfo|URL,options?:RequestInit)=>{
      calls.push(options??{});
      return new Response(JSON.stringify({applications:[],tasks:[]}),{status:200});
    });
    const payload={applications:[],tasks:[],revision:"a".repeat(64)};
    await apiRequest("/api/v2/projects/"+A+"/settings-review",{
      method:"PUT",csrfToken:"csrf",body:payload,
    },transport);
    expect(calls).toHaveLength(1);
    expect(calls[0].credentials).toBe("same-origin");
    expect(new Headers(calls[0].headers).get("X-CSRF-Token")).toBe("csrf");
    expect(JSON.parse(String(calls[0].body))).toEqual(payload);
  });
});
