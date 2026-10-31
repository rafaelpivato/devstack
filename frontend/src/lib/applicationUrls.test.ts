import { describe, expect, it } from "vitest";
import { resolveApplicationUrl } from "./applicationUrls";

describe("explicit application bookmarks",()=>{
  it("preserves owner-supplied URLs and paths",()=>{
    expect(resolveApplicationUrl("https://site.example:8080/b/admin/","devstack.tailnet.ts.net"))
      .toBe("https://site.example:8080/b/admin/");
  });
  it("uses the dashboard hostname without consulting container mappings",()=>{
    expect(resolveApplicationUrl("http://{host}:5173/a/login","devstack.tailnet.ts.net"))
      .toBe("http://devstack.tailnet.ts.net:5173/a/login");
    expect(resolveApplicationUrl("http://{host}:5173/b/admin/","devstack.tailnet.ts.net"))
      .toBe("http://devstack.tailnet.ts.net:5173/b/admin/");
  });
  it("rejects scripts, credentials, mixed placeholders and invalid URL syntax",()=>{
    for(const bad of ["javascript:alert(1)","http://bob:password@example.com",
      "//example.com/a","http://{host}.evil/a","http://{host}/x?token=secret",
      "https://example.com/x#fragment","http://{host}:broken/a","http://{x}:5173/"]){
      expect(resolveApplicationUrl(bad,"devstack.tailnet.ts.net")).toBeNull();
    }
  });
});
