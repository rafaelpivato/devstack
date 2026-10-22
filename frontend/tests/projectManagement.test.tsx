import {describe,it,expect,vi} from "vitest";
import {renderToStaticMarkup} from "react-dom/server";
import {apiRequest} from "../src/api/client";
import {projectNameValid,projectSlugValid,githubSourceValid} from "../src/api/projectManagement";
import {CreateProjectScreen,ImportProjectScreen} from "../src/features/projects/ProjectManagement";
import {CREATE_PROJECT_URL,IMPORT_PROJECT_URL,importReceiptUrl,routeFor,parentUrl,GLOBAL_PROJECTS_URL,PREVIEW_ROOT} from "../src/navigation/routes";
import type {RuntimeSnapshot} from "../src/api/runtime";
import type {Navigator} from "../src/navigation/useNavigation";
const A="11111111-1111-4111-8111-111111111111";
const nav={route:{kind:"projects"},navigate:vi.fn(),back:vi.fn()} as unknown as Navigator;
const runtime=(phase:RuntimeSnapshot["phase"],project_id:string|null):RuntimeSnapshot=>({phase,project_id});
describe("project management parity",()=>{
  it("has exact, hierarchical reloadable routes",()=>{
    expect(routeFor(CREATE_PROJECT_URL)).toEqual({kind:"project-create"});
    expect(routeFor(IMPORT_PROJECT_URL)).toEqual({kind:"project-import"});
    expect(routeFor(importReceiptUrl(A))).toEqual({kind:"project-import-receipt",operationId:A});
    expect(parentUrl(routeFor(importReceiptUrl(A)))).toEqual(IMPORT_PROJECT_URL);
    expect(parentUrl(routeFor(CREATE_PROJECT_URL))).toEqual(GLOBAL_PROJECTS_URL);
    expect(routeFor(PREVIEW_ROOT+"/imports/garbage")).toEqual({kind:"not-found"});
    expect(routeFor(PREVIEW_ROOT+"/projects/create/shell")).toEqual({kind:"not-found"});
  });
  it("matches controller slugs and safe supported HTTPS GitHub sources",()=>{
    expect(projectSlugValid("project-12")).toBe(true);
    expect(projectSlugValid("a".repeat(63))).toBe(true);
    expect(projectSlugValid("a".repeat(64))).toBe(false);
    expect(projectSlugValid("-abc")).toBe(false);
    expect(projectNameValid("My project")).toBe(true);
    expect(projectNameValid("  My project")).toBe(false);
    expect(projectNameValid("x".repeat(121))).toBe(false);
    expect(githubSourceValid("https://github.com/org/project/tree/main/path")).toBe(true);
    expect(githubSourceValid("https://github.com/org/project")).toBe(true);
    for(const url of ["https://evil.com/org/p","https://github.com.evil.com/org/p",
      "https://github.com/org/p?token=123","https://github.com/org/../admin",
      "https://username:password@github.com/o/p","javascript:alert(1)"])
      expect(githubSourceValid(url)).toBe(false);
  });
  it("no render may create/import or autoactivate; global runtime fences actions",()=>{
    const create=renderToStaticMarkup(<CreateProjectScreen navigation={nav}
      runtime={runtime("active",A)}/>);
    expect(create).toContain("Deactivate");
    expect(create).toContain("disabled");
    const imported=renderToStaticMarkup(<ImportProjectScreen navigation={nav}
      runtime={runtime("switching",A)}/>);
    expect(imported).toContain("disabled");
    expect(nav.navigate).not.toHaveBeenCalled();
  });
  it("import submits same-origin CSRF with explicit stable 128-bit key",async()=>{
    const requests:Array<{url:string;init:RequestInit}>=[];
    const transport=vi.fn(async(url:RequestInfo|URL,init?:RequestInit)=>{
      requests.push({url:String(url),init:init??{}});
      return new Response(JSON.stringify({import:{id:A}}),{status:202});
    });
    const key="a".repeat(32);
    for(let i=0;i<2;i++)
      await apiRequest("/api/v2/projects/import",{
        method:"POST",body:{url:"https://github.com/org/p",slug:"p",name:"Project"},
        csrfToken:"csrf",idempotencyKey:key,
      },transport);
    expect(requests).toHaveLength(2);
    expect(requests.every(x=>new Headers(x.init.headers).get("Idempotency-Key")===key)).toBe(true);
    expect(requests.every(x=>x.init.credentials==="same-origin")).toBe(true);
    expect(JSON.parse(String(requests[0].init.body))).toEqual({
      url:"https://github.com/org/p",slug:"p",name:"Project",
    });
  });
});
