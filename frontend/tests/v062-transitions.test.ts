import {afterEach,describe,expect,it,vi} from "vitest";
import {inspectSavedRequest,safeReceiptSummary} from "../src/features/runtime/RuntimeScreens";
import type {PendingTransition} from "../src/features/runtime/RuntimeScreens";
import type {TransitionReceipt} from "../src/api/runtime";
const ID="11111111-1111-4111-8111-111111111111";
const KEY="0123456789abcdef0123456789abcdef";
const pending:PendingTransition={action:"deactivate",projectId:null,requestKey:KEY};
const receipt:TransitionReceipt={
  id:ID,request_key:KEY,action:"deactivate",project_id:null,
  status:"succeeded",result:{phase:"inactive",project_id:null},error_code:"",
  started_ns:1700000000000000000,finished_ns:1700000001000000000,
};
afterEach(()=>vi.unstubAllGlobals());
describe("saved idempotency request reconciliation",()=>{
  it("finds the exact authoritative receipt using only a GET",async()=>{
    const fetcher=vi.fn(async (_url:string,init:RequestInit)=>
      new Response(JSON.stringify({operations:[receipt]}),{status:200}));
    vi.stubGlobal("fetch",fetcher);
    expect(await inspectSavedRequest(pending)).toEqual({kind:"found",receipt});
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(fetcher.mock.calls[0][1].method).toBe("GET");
  });
  it("never falsely marks a missing receipt as a failure or sends a new request",async()=>{
    const fetcher=vi.fn(async()=>new Response(JSON.stringify({operations:[]}),{status:200}));
    vi.stubGlobal("fetch",fetcher);
    expect(await inspectSavedRequest(pending)).toEqual({kind:"missing"});
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
  it("retrieves a known saved receipt outside the recent list",async()=>{
    const fetcher=vi.fn(async (url:string)=>
      new Response(JSON.stringify(url.endsWith(ID)?receipt:{operations:[]}),{status:200}));
    vi.stubGlobal("fetch",fetcher);
    expect(await inspectSavedRequest({...pending,receiptId:ID})).toEqual({kind:"found",receipt});
    expect(fetcher).toHaveBeenCalledTimes(2);
  });
  it("rejects receipt collisions on action or target, and errors are unknown",async()=>{
    vi.stubGlobal("fetch",vi.fn(async()=>new Response(JSON.stringify({
      operations:[{...receipt,action:"activate"}]
    }),{status:200})));
    expect(await inspectSavedRequest(pending)).toEqual({kind:"missing"});
    vi.stubGlobal("fetch",vi.fn(async()=>{throw Error("offline");}));
    expect((await inspectSavedRequest(pending)).kind).toBe("unavailable");
  });
  it("copy summary includes safe status/error/timestamps but never raw key or credentials",()=>{
    const value=safeReceiptSummary({...receipt,status:"interrupted",error_code:"InterruptedTransition"});
    expect(value).toContain("interrupted");
    expect(value).toContain("InterruptedTransition");
    expect(value).toContain("Operation ID: "+ID);
    expect(value).not.toContain(KEY);
    expect(value).not.toContain("request_key");
  });
});
