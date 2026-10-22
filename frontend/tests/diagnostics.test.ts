import {describe,expect,it} from "vitest";
import {diagnosticText,type DiagnosticSnapshot} from "../src/api/diagnostics";
const base:DiagnosticSnapshot={
  retrieved_at:"2026-10-22T20:00:00Z",version:"v0.6.2",minutes:5,
  events:[{at:1792699200000,source:"controller",level:"info",code:"controller-started"}],
  truncated:false,partial:false,
  sources:{controller:"available",web:"available",runtime:"available",updater:"available"},
  updater_status:{phase:"idle",unit_state:"inactive",last_result:"success",last_exit_code:"0"}
};
describe("copyable DevStack global diagnostics",()=>{
  it("includes bounded window/source/UTC context and sampled updater state",()=>{
    const text=diagnosticText(base);
    expect(text).toContain("Installed: v0.6.2");
    expect(text).toContain("Lookback: 5");
    expect(text).toContain("controller=available");
    expect(text).toContain("[controller/info] controller-started");
    expect(text).toContain("phase=idle");
  });
  it("clearly warns on unavailable components, truncation and empty events",()=>{
    const text=diagnosticText({...base,events:[],partial:true,truncated:true,
      sources:{...base.sources,controller:"unavailable"}});
    expect(text).toContain("WARNING: Some sources are unavailable");
    expect(text).toContain("WARNING: Event list truncated");
    expect(text).toContain("No allowlisted events");
  });
  it("contains no user-initiated upload, credential or arbitrary host output contract",()=>{
    const text=diagnosticText(base);
    expect(text).not.toContain("request_key");
    expect(text).not.toContain("Bearer ");
    expect(text).toContain("no raw host logs");
  });
});
