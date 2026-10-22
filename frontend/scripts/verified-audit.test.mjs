import {test} from "node:test";
import {strict as assert} from "node:assert";
import {classifyAudit} from "./verified-audit.mjs";

test("only a complete audit with zero issues passes",()=>{
  assert.equal(classifyAudit(JSON.stringify({metadata:{vulnerabilities:{total:0}},vulnerabilities:{}}),0).kind,"clean");
  assert.equal(classifyAudit(JSON.stringify({metadata:{vulnerabilities:{total:0}}}),1).kind,"transport-error");
  assert.equal(classifyAudit(JSON.stringify({metadata:{vulnerabilities:{total:2}},vulnerabilities:{foo:{severity:"high"}}}),1).kind,"vulnerable");
  assert.equal(classifyAudit(JSON.stringify({vulnerabilities:{bar:{severity:"moderate"}}}),1).kind,"vulnerable");
  assert.equal(classifyAudit(JSON.stringify({error:{code:"EAUDIT",message:"Invalid package tree"}}),1).kind,"transport-error");
  assert.equal(classifyAudit("not JSON",1).kind,"transport-error");
  assert.equal(classifyAudit("",null,"timed out").kind,"transport-error");
  assert.equal(classifyAudit(JSON.stringify({metadata:{vulnerabilities:{total:0}},vulnerabilities:{}}),0).kind,"clean");
});
