/**
 * Audits the complete locked dependency graph. npm's registry can sometimes
 * reject the Bulk Advisory endpoint and fall back to the retiring Quick Audit
 * endpoint with HTTP 400 "Invalid package tree". A transient registry response
 * must not be confused with a clean report OR with a vulnerable dependency tree.
 *
 * Success requires a valid online npm report, exit 0, and zero advisories.
 * Registry/transport failures receive bounded retries; vulnerabilities fail
 * immediately. Never use --offline or --omit=dev here.
 */
import {spawnSync} from "node:child_process";
import {setTimeout as delay} from "node:timers/promises";
import {pathToFileURL} from "node:url";

export function classifyAudit(stdout, status, error) {
  let report;
  try { report=JSON.parse(stdout); }
  catch { return {kind:"transport-error",reason:error||"Audit did not return JSON"}; }
  const totals=report?.metadata?.vulnerabilities;
  if(totals && typeof totals.total==="number" && totals.total>0) {
    return {kind:"vulnerable",reason:`${totals.total} known advisories found`};
  }
  if(report?.vulnerabilities && Object.keys(report.vulnerabilities).length>0) {
    return {kind:"vulnerable",reason:"Known vulnerable packages found"};
  }
  if(totals && totals.total===0 && status===0) return {kind:"clean"};
  const description=report?.error?.message || report?.error?.summary ||
    (status===null?"npm audit timed out":"npm audit exited "+status);
  return {kind:"transport-error",reason:String(description)};
}

async function run() {
  const maxAttempts=3;
  for(let i=1;i<=maxAttempts;i++) {
    const result=spawnSync(process.platform==="win32"?"npm.cmd":"npm",
      ["audit","--json","--audit-level=low","--fetch-retries=0","--fetch-timeout=60000"],
      {encoding:"utf8",timeout:105000,maxBuffer:10*1024*1024});
    const checked=classifyAudit(result.stdout??"",result.status,result.error?.message);
    if(checked.kind==="clean") {
      console.log(`Full online npm audit passed, zero vulnerabilities (attempt ${i}).`);
      return;
    }
    if(checked.kind==="vulnerable") {
      console.error(`Security gate failed: ${checked.reason}.`);
      console.error((result.stdout??"").slice(0,6000));
      process.exitCode=1;
      return;
    }
    console.error(`npm advisory service attempt ${i}/${maxAttempts} failed: ${checked.reason}`);
    if(result.stderr)console.error(result.stderr.slice(0,1000));
    if(i<maxAttempts)await delay(i*3000);
    else {
      console.error("Security gate FAILED CLOSED: no complete audit report available.");
      process.exitCode=1;
    }
  }
}
if(import.meta.url===pathToFileURL(process.argv[1]||"").href) await run();
