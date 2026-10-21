import { describe, expect, it, vi } from "vitest";
import { apiRequest, ApiError } from "../src/api/client";

describe("typed Flask API transport", () => {
  it("uses same-origin cookies and never forwards tokens elsewhere", async () => {
    const transport = vi.fn(async () => new Response(JSON.stringify({ phase: "inactive" }), {
      status: 200, headers: { "Content-Type": "application/json" },
    }));
    const result = await apiRequest<{phase: string}>("/api/v2/runtime", {}, transport);
    expect(result.phase).toBe("inactive");
    expect(transport).toHaveBeenCalledWith("/api/v2/runtime", expect.objectContaining({
      credentials: "same-origin", cache: "no-store", method: "GET",
    }));
    await expect(apiRequest("//evil.example/api/v2/runtime", {}, transport)).rejects.toBeInstanceOf(ApiError);
    await expect(apiRequest("/api/v2/../secret", {}, transport)).rejects.toBeInstanceOf(ApiError);
    expect(transport).toHaveBeenCalledTimes(1);
  });

  it("requires CSRF and preserves exact JSON mutation fields", async () => {
    const transport = vi.fn(async () => new Response(JSON.stringify({ ok: true }), {status: 200}));
    await expect(apiRequest("/api/v2/projects", {
      method: "POST", body: {slug: "demo", name:"Demo"},
    }, transport)).rejects.toMatchObject({kind: "unauthorized"});
    await apiRequest("/api/v2/projects", {
      method: "POST", csrfToken: "expected", body: {slug:"demo", name:"Demo"},
    }, transport);
    const options = transport.mock.calls[0][1] as RequestInit;
    expect(new Headers(options.headers).get("X-CSRF-Token")).toBe("expected");
    expect(JSON.parse(options.body as string)).toEqual({slug:"demo", name:"Demo"});
  });

  it("classifies controller conflict and unavailable distinctly", async () => {
    const conflict = vi.fn(async () => new Response(JSON.stringify({message: "Operation not permitted"}), {status: 409}));
    await expect(apiRequest("/api/v2/runtime", {}, conflict)).rejects.toMatchObject({kind:"conflict",status:409});
    const unavailable = vi.fn(async () => {throw Error("private IP detail")});
    await expect(apiRequest("/api/v2/runtime", {}, unavailable)).rejects.toMatchObject({
      kind:"unavailable", message:"Cannot reach DevStack. Check the connection.",
    });
  });
});
