import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import worker from "../src/index.js";

function audit(body, issues=[]) {
  return {draft_body:body,issues,paragraphs:body.split(/\n\s*\n/u).map(p=>p.trim()).filter(Boolean).map((text,i)=>({text,kind:i===0?"HYPOTHETICAL":"ADVICE",factual_passages:[]}))};
}
test("GPT OSS uses Responses input with no tools and keeps reasoning out of the article",async()=>{
  const body="Giả sử bạn đang cân nhắc gửi ảnh trong nhóm. "+"Hãy hỏi đường dẫn gốc trước khi quyết định chia sẻ. ".repeat(10);
  const captured=[];
  const env={CREATOR_KEY:"fixture",CREATOR_MODEL:"@cf/openai/gpt-oss-120b",CREATOR_VERSION:{id:"pinned-version"},AI:{run:async(model,options)=>{
    captured.push(options);
    return {output:[{type:"reasoning",summary:[{text:"private reasoning fixture"}]},{type:"message",content:[{type:"output_text",text:captured.length<3?body:JSON.stringify(captured.length===3?{draft_body:body,factual_claims:[]}:audit(body))}]}],usage:{input_tokens:12,output_tokens:30}};
  }}};
  const response=await worker.fetch(new Request("https://example.test/b/generate",{method:"POST",headers:{authorization:"Bearer fixture","content-type":"application/json"},body:JSON.stringify({prompt:"selected notes",writing_prompt:"writing only",schema:{type:"object",properties:{draft_body:{type:"string"}}}})}),env);
  assert.equal(response.status,200);
  assert.equal(captured.length,4);
  for(const options of captured){
    assert.deepEqual(options.tools,[]);
    assert.deepEqual(options.reasoning,{effort:"low"});
    assert.equal(options.messages,undefined);
    assert.equal(options.response_format,undefined);
    assert.equal(options.input.length,1);
    assert.equal(options.input[0].role,"user");
    assert.equal(typeof options.instructions,"string");
    assert.ok(options.instructions.length>100);
  }
  assert.equal(captured[0].max_output_tokens,2200);
  assert.equal(captured[1].max_output_tokens,2200);
  assert.equal(captured[2].max_output_tokens,4096);
  assert.equal(captured[3].max_output_tokens,4096);
  assert.equal(captured[0].input[0].content,"writing only");
  assert.ok(captured[1].input[0].content.includes(body));
  assert.ok(captured[2].input[0].content.includes(body));
  const result=await response.json();
  assert.equal(result.stages[0].body,body);
  assert.equal(result.result.response,JSON.stringify({draft_body:body,factual_claims:[]}));
  assert.deepEqual(result.quality_audit,audit(body));
  assert.equal(result.stages[3].stage,"source_audit");
  assert.equal(result.worker_version,"pinned-version");
});
test("supported structured model receives the actual schema, not json_object hint",async()=>{
  const schema={type:"object",properties:{draft_body:{type:"string"}},required:["draft_body"]};
  const body="Giả sử bạn nhận được một ảnh. "+"Selected text stays unchanged in the annotation stage. ".repeat(10);
  let actual;let calls=0;
  const env={CREATOR_KEY:"fixture",CREATOR_MODEL:"@cf/meta/fixture",CREATOR_VERSION:{id:"version"},AI:{run:async(model,options)=>{calls++;if(calls===3)actual=options;return {response:calls<3?body:JSON.stringify(calls===3?{draft_body:body,factual_claims:[]}:audit(body))};}}};
  const response=await worker.fetch(new Request("https://example.test/b/generate",{method:"POST",headers:{authorization:"Bearer fixture","content-type":"application/json"},body:JSON.stringify({prompt:"selected",writing_prompt:"selected",schema})}),env);
  assert.equal(response.status,200);
  assert.equal(calls,4);
  assert.deepEqual(actual.response_format,{type:"json_schema",json_schema:{...schema,properties:{draft_body:{type:"string",const:body}}}});
  assert.equal(actual.stream,false);
});
test("no auth or over-budget streamed input cannot call model",async()=>{
  let calls=0;
  const env={CREATOR_KEY:"fixture",CREATOR_MODEL:"model",AI:{run:async()=>{calls++;return {};}}};
  const unauthorized=await worker.fetch(new Request("https://example.test/b/generate",{method:"POST",body:"{}"}),env);
  assert.equal(unauthorized.status,401);
  const large=await worker.fetch(new Request("https://example.test/b/generate",{method:"POST",headers:{authorization:"Bearer fixture","content-type":"application/json"},body:JSON.stringify({prompt:"x".repeat(70000),schema:{}})}),env);
  assert.equal(large.status,413);assert.equal(calls,0);
});
test("model error is finite and cannot silently retry or use template",async()=>{
  let calls=0;
  const env={CREATOR_KEY:"fixture",CREATOR_MODEL:"model",AI:{run:async()=>{calls++;throw new Error("private error");}}};
  const response=await worker.fetch(new Request("https://example.test/b/generate",{method:"POST",headers:{authorization:"Bearer fixture","content-type":"application/json"},body:JSON.stringify({prompt:"selected",writing_prompt:"selected",schema:{type:"object"}})}),env);
  assert.equal(response.status,502);assert.equal(calls,1);assert.deepEqual(await response.json(),{error:"WORKERS_AI_FAILED",retryable:false});
});
test("annotation cannot rewrite the edited article even when schema is only a prompt",async()=>{
  const body="Giả sử bạn nhận được một ảnh. "+"The same text must reach the canonical Draft unchanged. ".repeat(10);
  let calls=0;
  const env={CREATOR_KEY:"fixture",CREATOR_MODEL:"@cf/openai/gpt-oss-120b",AI:{run:async()=>{
    calls++;
    return {response:calls<3?body:JSON.stringify({draft_body:body+" An unauthorized rewrite."})};
  }}};
  const response=await worker.fetch(new Request("https://example.test/b/generate",{method:"POST",headers:{authorization:"Bearer fixture","content-type":"application/json"},body:JSON.stringify({prompt:"selected",writing_prompt:"selected",schema:{type:"object",properties:{draft_body:{type:"string"}}}})}),env);
  assert.equal(response.status,502);
  assert.equal(calls,3);
  assert.deepEqual(await response.json(),{error:"WORKERS_AI_FAILED",retryable:false});
});

test("real failed v11 pilot cannot lose its hypothetical label or trigger another model retry",async()=>{
  const body=readFileSync(new URL("../../../tests/fixtures/workers_b_v11_failed.txt",import.meta.url),"utf8").trim();
  let calls=0;
  const env={CREATOR_KEY:"fixture",CREATOR_MODEL:"@cf/openai/gpt-oss-120b",AI:{run:async()=>{calls++;return {response:body};}}};
  const response=await worker.fetch(new Request("https://example.test/b/generate",{method:"POST",headers:{authorization:"Bearer fixture","content-type":"application/json"},body:JSON.stringify({prompt:"selected",writing_prompt:"selected",schema:{type:"object",properties:{draft_body:{type:"string"}}}})}),env);
  assert.equal(response.status,422);
  assert.equal(calls,2);
  assert.equal((await response.json()).reason,"HYPOTHETICAL_LABEL_MISSING");
});

test("independent audit rejects unsupported source assertions and omitted coverage without repair",async()=>{
  const body="Giả sử bạn nhận được một ảnh. "+"Hãy hỏi đường dẫn gốc và kiểm tra ngày công bố. ".repeat(10);
  for(const report of [audit(body,["Unsupported chart origin and efficacy promise"]),{...audit(body),paragraphs:[]},null]){
    let calls=0;
    const env={CREATOR_KEY:"fixture",CREATOR_MODEL:"@cf/openai/gpt-oss-120b",AI:{run:async()=>{
      calls++;return {response:calls<3?body:JSON.stringify(calls===3?{draft_body:body,factual_claims:[]}:report)};
    }}};
    const response=await worker.fetch(new Request("https://example.test/b/generate",{method:"POST",headers:{authorization:"Bearer fixture","content-type":"application/json"},body:JSON.stringify({prompt:"selected",writing_prompt:"selected",schema:{type:"object",properties:{draft_body:{type:"string"}}}})}),env);
    assert.equal(response.status,422);
    assert.equal(calls,4);
    assert.equal((await response.json()).reason,"SOURCE_AUDIT_FAILED");
  }
});
