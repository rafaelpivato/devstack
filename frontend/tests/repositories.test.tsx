import { describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { apiRequest } from "../src/api/client";
import { canWriteRepository, RepositoryBulk, RepositoryAdd } from "../src/features/repositories/RepositoryScreens";
import { loadSubmission, saveSubmission, clearSubmission, requestKey } from "../src/lib/repositorySubmission";
import type { Navigator } from "../src/navigation/useNavigation";
import { repoAddUrl, repoBranchUrl, repoReceiptUrl } from "../src/navigation/routes";
import type { RuntimeSnapshot } from "../src/api/runtime";
const A="11111111-1111-4111-8111-111111111111";
const B="22222222-2222-4222-8222-222222222222";
const nav={route:{kind:"repo-add",projectId:A},navigate:vi.fn(),back:vi.fn()} as unknown as Navigator;
const state=(phase:RuntimeSnapshot["phase"],project_id:string|null):RuntimeSnapshot=>({phase,project_id});
describe("repository safety and navigation",()=>{
  it("requires exactly inactive global runtime, not merely an inactive viewed project",()=>{
    expect(canWriteRepository(state("inactive",null))).toBe(true);
    for(const value of [state("active",B),state("switching",B),state("error",B),
      state("activating",null),state("inactive",B)]){
      expect(canWriteRepository(value)).toBe(false);
    }
  });
  it("does not create mutations by rendering forms or deep links",()=>{
    const inactive=renderToStaticMarkup(<RepositoryAdd id={A}
      runtime={state("inactive",null)} navigation={nav}/>);
    expect(inactive).toContain("Repository URL");
    expect(inactive).toContain("Add repository");
    expect(inactive).toContain("disabled");
    const active=renderToStaticMarkup(<RepositoryBulk id={A}
      runtime={state("active",B)} navigation={nav}/>);
    expect(active).toContain("Activate this project or deactivate the other active project");
    const owner=renderToStaticMarkup(<RepositoryBulk id={A}
      runtime={state("active",A)} navigation={nav}/>);
    expect(owner).not.toContain("Update unavailable");
    expect(active).toContain("disabled");
    expect(repoBranchUrl(A)).toContain("/repositories/branch");
    expect(repoReceiptUrl(A,"add",B)).toContain("/repositories/adds/");
    expect(repoAddUrl(A)).toContain("/repositories/add");
    expect(nav.navigate).not.toHaveBeenCalled();
  });
  it("transports reviewed revisions and expected HEAD exactly",async()=>{
    const requests:{url:string;options:RequestInit}[]=[];
    const mock=vi.fn(async (url:RequestInfo|URL,options?:RequestInit)=>{
      requests.push({url:String(url),options:options??{}});
      return new Response(JSON.stringify({ok:true}),{status:200});
    });
    const csrfToken="csrf", idempotencyKey="0123456789abcdef0123456789abcdef";
    await apiRequest("/api/v2/projects/"+A+"/repositories/"+B+"/update",{
      method:"POST",csrfToken,body:{expected_head:"a".repeat(40)},
    },mock);
    await apiRequest("/api/v2/projects/"+A+"/git/branch-apply",{
      method:"POST",csrfToken,body:{branch:"feature/new",repository_id:B,revision:"b".repeat(64)},
    },mock);
    await apiRequest("/api/v2/projects/"+A+"/source-review/apply",{
      method:"POST",csrfToken,body:{revision:"c".repeat(64),
        selected:["application:web"],overwrites:["application:web"]},
    },mock);
    await apiRequest("/api/v2/projects/"+A+"/repositories/update-all",{
      method:"POST",csrfToken,idempotencyKey,body:{},
    },mock);
    expect(requests).toHaveLength(4);
    expect(JSON.parse(String(requests[0].options.body))).toEqual({expected_head:"a".repeat(40)});
    expect(JSON.parse(String(requests[1].options.body))).toEqual({
      branch:"feature/new",repository_id:B,revision:"b".repeat(64),
    });
    expect(JSON.parse(String(requests[2].options.body))).toEqual({
      revision:"c".repeat(64),selected:["application:web"],overwrites:["application:web"],
    });
    expect(new Headers(requests[3].options.headers).get("Idempotency-Key")).toBe(idempotencyKey);
    expect(requests.every(x=>x.options.credentials==="same-origin")).toBe(true);
  });
});
describe("durable repository submission keys",()=>{
  it("creates 128-bit keys and rejects invalid project identities",()=>{
    const key=requestKey();
    expect(key).toMatch(/^[0-9a-f]{32}$/);
    expect(loadSubmission("invalid","bulk")).toBe(null);
  });
  it("handles unavailable session storage without making navigation fail",()=>{
    // Vitest node environment intentionally has no browser storage.
    expect(loadSubmission(A,"bulk")).toBe(null);
    expect(()=>saveSubmission({projectId:A,mode:"bulk",key:"a".repeat(32)})).not.toThrow();
    expect(()=>clearSubmission(A,"bulk")).not.toThrow();
  });
});
