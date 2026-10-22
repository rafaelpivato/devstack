import { describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { ApiError, apiRequest } from "../src/api/client";
import { actionAllowed, RuntimeActionScreen } from "../src/features/runtime/RuntimeScreens";
import type { RuntimeSnapshot } from "../src/api/runtime";
import type { Navigator } from "../src/navigation/useNavigation";
const A="11111111-1111-4111-8111-111111111111";
const B="22222222-2222-4222-8222-222222222222";
const base=(phase:RuntimeSnapshot["phase"],project_id:string|null):RuntimeSnapshot=>({phase,project_id});
describe("runtime transition guard",()=>{
  it("requires exact runtime context",()=>{
    expect(actionAllowed("activate",A,base("inactive",null))).toBe(true);
    expect(actionAllowed("activate",A,base("active",B))).toBe(false);
    expect(actionAllowed("switch",A,base("active",B))).toBe(true);
    expect(actionAllowed("switch",A,base("active",A))).toBe(false);
    expect(actionAllowed("switch",A,base("switching",B))).toBe(false);
    expect(actionAllowed("deactivate",null,base("active",A))).toBe(true);
    expect(actionAllowed("deactivate",null,base("inactive",null))).toBe(false);
    expect(actionAllowed("reconcile",null,base("error",A))).toBe(true);
    expect(actionAllowed("reconcile",null,base("switching",A))).toBe(false);
  });
  it("shows confirmation with no automatic mutation",()=>{
    const navigation={route:{kind:"runtime-action",projectId:A,action:"switch"},navigate:vi.fn(),back:vi.fn()} as unknown as Navigator;
    const html=renderToStaticMarkup(<RuntimeActionScreen action="switch" projectId={A}
      runtime={base("active",B)}
      projects={[{id:A,name:"Alpha",slug:"alpha"},{id:B,name:"Beta",slug:"beta"}]}
      navigation={navigation}/>);
    expect(html).toContain("Switch project");
    expect(html).toContain("The active project");
    expect(html).toContain("type=\"checkbox\"");
    expect(html).toContain("disabled");
    expect(html).toContain("Beta");
    expect(navigation.navigate).not.toHaveBeenCalled();
  });
  it("requires same-origin CSRF and 128-bit idempotency key",async()=>{
    const calls:RequestInit[]=[];
    const transport=vi.fn(async (_url:RequestInfo|URL,options?:RequestInit)=>{
      calls.push(options??{});
      return new Response(JSON.stringify({operation:{id:A,status:"queued"}}),{status:202});
    });
    await expect(apiRequest("/api/v2/runtime/transitions",{
      method:"POST",body:{action:"activate",project_id:A},
      csrfToken:"csrf",idempotencyKey:"bad",
    },transport)).rejects.toMatchObject({kind:"invalid"});
    expect(calls.length).toBe(0);
    const result=await apiRequest<{operation:{status:string}}>("/api/v2/runtime/transitions",{
      method:"POST",body:{action:"activate",project_id:A},
      csrfToken:"csrf",idempotencyKey:"0123456789abcdef0123456789abcdef",
    },transport);
    expect(result.operation.status).toBe("queued");
    const headers=new Headers(calls[0].headers);
    expect(headers.get("Idempotency-Key")).toBe("0123456789abcdef0123456789abcdef");
    expect(headers.get("X-CSRF-Token")).toBe("csrf");
    expect(calls[0].credentials).toBe("same-origin");
    expect(JSON.parse(calls[0].body as string)).toEqual({action:"activate",project_id:A});
  });
  it("distinguishes controller conflict from unknown network failure",async()=>{
    await expect(apiRequest("/api/v2/runtime/transitions",{
      method:"POST",body:{action:"switch",project_id:A},csrfToken:"csrf",
      idempotencyKey:"0123456789abcdef0123456789abcdef",
    },async()=>new Response(JSON.stringify({message:"Another transition"}),{status:409})))
      .rejects.toMatchObject({kind:"conflict"});
    await expect(apiRequest("/api/v2/runtime/transitions",{
      method:"POST",body:{action:"activate",project_id:A},csrfToken:"csrf",
      idempotencyKey:"0123456789abcdef0123456789abcdef",
    },async()=>{throw new Error("offline");})).rejects.toBeInstanceOf(ApiError);
  });
});
