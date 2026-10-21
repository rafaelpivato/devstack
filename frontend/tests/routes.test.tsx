import { describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { Entry, ProjectDirectory } from "../src/features/projects/ProjectScreens";
import { isProjectId, laneUrl, parentUrl, PREVIEW_ROOT, projectUrl, repoUrl, routeFor } from "../src/navigation/routes";
import type { Navigator } from "../src/navigation/useNavigation";
import type { ProjectSummary, RuntimeSnapshot } from "../src/api/runtime";

const A = "11111111-1111-4111-8111-111111111111";
const B = "22222222-2222-4222-8222-222222222222";
const projects:ProjectSummary[] = [
  {id:A, slug:"alpha", name:"Alpha"},{id:B, slug:"beta", name:"Beta"},
];
const navigator={route:{kind:"entry"},navigate:vi.fn(),back:vi.fn()} as unknown as Navigator;

describe("project-scoped hierarchical routes",()=>{
  it("keeps stable UUIDs in routes and parent hierarchy",()=>{
    expect(routeFor(PREVIEW_ROOT)).toEqual({kind:"entry"});
    expect(routeFor(PREVIEW_ROOT+"/projects")).toEqual({kind:"projects"});
    expect(routeFor(projectUrl(A))).toEqual({kind:"project",projectId:A});
    const lane=routeFor(laneUrl(A,"services"));
    expect(lane).toEqual({kind:"lane", projectId:A,lane:"services"});
    expect(parentUrl(lane)).toBe(projectUrl(A));
    const repo=routeFor(repoUrl(A,B));
    expect(repo).toEqual({kind:"repository",projectId:A,repositoryId:B});
    expect(parentUrl(repo)).toBe(laneUrl(A,"repositories"));
    expect(parentUrl(routeFor(projectUrl(A)))).toBe(PREVIEW_ROOT+"/projects");
  });
  it("rejects invalid/unplanned paths; no implicit current-project alias",()=>{
    expect(isProjectId("not-a-uuid")).toBe(false);
    expect(routeFor(PREVIEW_ROOT+"/projects/current")).toEqual({kind:"not-found"});
    expect(routeFor(PREVIEW_ROOT+"/projects/"+A+"/terminal")).toEqual({kind:"not-found"});
    expect(routeFor(PREVIEW_ROOT+"/projects/"+A+"/services/"+B)).toEqual({kind:"not-found"});
    expect(routeFor(PREVIEW_ROOT+"/projects/"+A+"/repositories/"+B+"/logs")).toEqual({kind:"not-found"});
    expect(routeFor("/api/v2/projects")).toEqual({kind:"not-found"});
    expect(()=>projectUrl("active")).toThrow("Invalid project identifier");
  });
});
describe("runtime-defined landing and non-mutating project browsing",()=>{
  it("shows explicit choose-project entry when globally inactive",()=>{
    const runtime:RuntimeSnapshot={phase:"inactive",project_id:null};
    const html=renderToStaticMarkup(<Entry runtime={runtime} projects={projects} navigation={navigator}/>);
    expect(html).toContain("No active project");
    expect(html).toContain("Choose a project");
    expect(html).toContain(PREVIEW_ROOT+"/projects");
    expect(html).not.toContain("Running");
    expect(navigator.navigate).not.toHaveBeenCalled();
  });
  it("does not reinterpret transition or error as inactive",()=>{
    for(const phase of ["activating","deactivating","switching","error"] as const){
      const html=renderToStaticMarkup(<Entry runtime={{phase,project_id:A}} projects={projects} navigation={navigator}/>);
      expect(html).toContain("Runtime: "+phase);
      expect(html).not.toContain("No active project");
    }
    const missing=renderToStaticMarkup(<Entry runtime={{phase:"active",project_id:B}} projects={[projects[0]]} navigation={navigator}/>);
    expect(missing).toContain("Active project not in registry");
  });
  it("renders inactive projects without presenting them as globally active",()=>{
    const html=renderToStaticMarkup(<ProjectDirectory projects={projects}
      runtime={{phase:"active",project_id:A}} navigation={navigator}/>);
    expect(html).toContain("Viewing does not activate");
    expect(html).toContain("Alpha");
    expect(html).toContain("Beta");
    expect(html.match(/>Active</g)?.length).toBe(1);
    expect(html).toContain(projectUrl(A));
    expect(html).toContain(projectUrl(B));
  });
});
