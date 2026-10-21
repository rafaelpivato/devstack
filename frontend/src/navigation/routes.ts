/** Navigation is presentation state, never the global active runtime. */
export const PREVIEW_ROOT = "/__v06";
export const LANES = ["repositories", "services", "jobs", "applications", "configuration"] as const;
export type Lane = typeof LANES[number];
export type AppRoute =
  | {kind: "entry"} | {kind:"projects"}
  | {kind:"project"; projectId:string}
  | {kind:"lane"; projectId:string; lane:Lane}
  | {kind:"repository"; projectId:string; repositoryId:string}
  | {kind:"not-found"};

const uuid4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
export function isProjectId(value: string): boolean { return uuid4.test(value); }
export function isLane(value: string): value is Lane {
  return (LANES as readonly string[]).includes(value);
}
export function projectUrl(id: string): string {
  if (!isProjectId(id)) throw new Error("Invalid project identifier");
  return PREVIEW_ROOT + "/projects/" + id;
}
export function laneUrl(id: string, lane: Lane): string {
  return projectUrl(id) + "/" + lane;
}
export function repoUrl(id: string, repoId: string): string {
  if (!isProjectId(repoId)) throw new Error("Invalid repository identifier");
  return laneUrl(id, "repositories") + "/" + repoId;
}
export function routeFor(pathname: string): AppRoute {
  if (pathname === PREVIEW_ROOT || pathname === PREVIEW_ROOT + "/") return {kind:"entry"};
  if (pathname === PREVIEW_ROOT + "/projects") return {kind:"projects"};
  if (!pathname.startsWith(PREVIEW_ROOT + "/projects/")) return {kind:"not-found"};
  const segments = pathname.slice((PREVIEW_ROOT + "/projects/").length).split("/");
  if (!isProjectId(segments[0])) return {kind:"not-found"};
  const projectId = segments[0];
  if (segments.length === 1) return {kind:"project", projectId};
  if (segments.length === 2 && isLane(segments[1]))
    return {kind:"lane", projectId, lane: segments[1]};
  if (segments.length === 3 && segments[1] === "repositories" && isProjectId(segments[2]))
    return {kind:"repository", projectId, repositoryId: segments[2]};
  return {kind:"not-found"};
}
/** Used only for a fresh deep link; a user-initiated Back follows browser history. */
export function parentUrl(route: AppRoute): string {
  switch (route.kind) {
    case "repository": return laneUrl(route.projectId, "repositories");
    case "lane": return projectUrl(route.projectId);
    case "project": return PREVIEW_ROOT + "/projects";
    default: return PREVIEW_ROOT;
  }
}
export function routeProjectId(route: AppRoute): string | null {
  if (route.kind === "project" || route.kind === "lane" || route.kind === "repository") return route.projectId;
  return null;
}
