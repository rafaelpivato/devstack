import {describe,expect,it,vi} from "vitest";
import {renderToStaticMarkup} from "react-dom/server";
import {apiRequest} from "../src/api/client";
import {isPrivateApplicationUrl} from "../src/api/workloads";
import {canOperate} from "../src/features/workloads/WorkloadShared";
import {cancellable,JobLogs} from "../src/features/workloads/JobScreens";
import {ApplicationsScreen} from "../src/features/workloads/ApplicationScreens";
import {jobUrl,jobLogsUrl,jobsRunUrl,isContainerId,serviceUrl,routeFor,parentUrl,laneUrl} from "../src/navigation/routes";
import type {Navigator} from "../src/navigation/useNavigation";
import type {RuntimeSnapshot} from "../src/api/runtime";
import type {JobRow} from "../src/api/workloads";
const A="11111111-1111-4111-8111-111111111111",B="22222222-2222-4222-8222-222222222222",CID="a".repeat(64);
const nav={route:{kind:"entry"},navigate:vi.fn(),back:vi.fn()} as unknown as Navigator;
const runtime=(phase:RuntimeSnapshot["phase"],project_id:string|null):RuntimeSnapshot=>({phase,project_id});
describe("workload navigation",()=>{
 it("preserves one-way lane/detail/output hierarchy and rejects all unsupported paths",()=>{
  expect(isContainerId(CID)).toBe(true);
  expect(isContainerId("ab")).toBe(false);
  expect(routeFor(serviceUrl(A,CID))).toEqual({kind:"service-logs",projectId:A,containerId:CID});
  expect(parentUrl(routeFor(serviceUrl(A,CID)))).toBe(laneUrl(A,"services"));
  expect(routeFor(jobUrl(A,B))).toEqual({kind:"job-detail",projectId:A,jobId:B});
  expect(routeFor(jobLogsUrl(A,B))).toEqual({kind:"job-logs",projectId:A,jobId:B});
  expect(parentUrl(routeFor(jobLogsUrl(A,B)))).toBe(jobUrl(A,B));
  expect(routeFor(jobsRunUrl(A,"adhoc"))).toEqual({kind:"job-run",projectId:A,mode:"adhoc"});
  expect(routeFor(laneUrl(A,"services")+"/unknown")).toEqual({kind:"not-found"});
  expect(routeFor(laneUrl(A,"jobs")+"/run/shell")).toEqual({kind:"not-found"});
  expect(routeFor(jobUrl(A,B)+"/terminal")).toEqual({kind:"not-found"});
 });
 it("does not treat viewing a non-active project as runtime admission",()=>{
  expect(canOperate(runtime("active",A),A)).toBe(true);
  expect(canOperate(runtime("active",B),A)).toBe(false);
  expect(canOperate(runtime("inactive",null),A)).toBe(false);
  expect(canOperate(runtime("switching",A),A)).toBe(false);
  const html=renderToStaticMarkup(<ApplicationsScreen id={A}
    runtime={runtime("active",B)} navigation={nav}/>);
  expect(html).toContain("Not the active project");
  expect(nav.navigate).not.toHaveBeenCalled();
 });
});
describe("mobile job output controls",()=>{
 it("keeps Copy and Load in a spaced, wrapping action group",()=>{
   const html=renderToStaticMarkup(<JobLogs id={A} jobId={B} navigation={nav}/>);
   expect(html).toContain('class="log-actions"');
   expect(html).toContain('class="copy-log-control"');
   expect(html).toContain("Copy output");
   expect(html).toContain("Load new output");
 });
});
describe("private application URLs",()=>{
 it("requires explicit controller-returned tailnet-only HTTP(S) target",()=>{
  expect(isPrivateApplicationUrl("http://100.100.12.4:8000/")).toBe(true);
  expect(isPrivateApplicationUrl("https://100.64.0.1:443/")).toBe(true);
  for(const url of ["https://example.com","http://127.0.0.1:8000","http://192.168.1.1",
    "http://100.128.0.1","javascript:alert(1)","http://100.100.1.1@evil.com/",
    "http://100.99.1.3?token=secret"])
    expect(isPrivateApplicationUrl(url)).toBe(false);
 });
});
describe("job supervision",()=>{
 const job:JobRow={id:B,project_id:A,kind:"adhoc",status:"running",action:"test",
   cancel_policy:"independent",started_ns:1,finished_ns:null,exit_code:null};
 it("permits targeted cancel only on independent active jobs",()=>{
   expect(cancellable(job,true)).toBe(true);
   expect(cancellable(job,false)).toBe(false);
   expect(cancellable({...job,cancel_policy:"shared"},true)).toBe(false);
   expect(cancellable({...job,status:"succeeded"},true)).toBe(false);
 });
 it("reuses same-origin CSRF protected exact argv and targeted cancellation",async()=>{
   const calls:Array<{url:string;init:RequestInit}>=[];
   const transport=vi.fn(async(url:RequestInfo|URL,init?:RequestInit)=>{
     calls.push({url:String(url),init:init??{}});
     return new Response(JSON.stringify({job_id:B}),{status:202});
   });
   await apiRequest("/api/v2/projects/"+A+"/adhoc",{
     method:"POST",csrfToken:"csrf",
     body:{kind:"run",service:"web",image:"",network:"none",command:["python","-V"]},
   },transport);
   await apiRequest("/api/v2/projects/"+A+"/jobs/"+B+"/cancel",{method:"POST",csrfToken:"csrf"},transport);
   expect(calls).toHaveLength(2);
   expect(calls[0].init.credentials).toBe("same-origin");
   expect(JSON.parse(calls[0].init.body as string)).toEqual({
     kind:"run",service:"web",image:"",network:"none",command:["python","-V"],
   });
   expect(calls[1].url.endsWith("/cancel")).toBe(true);
 });
});
