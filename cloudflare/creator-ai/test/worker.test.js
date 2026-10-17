import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {createHash} from "node:crypto";
import worker from "../src/index.js";
import {auditSchema,bindAudit,validAudit,compileEditedArticle,compileSourcedArticle as compilePlainArticle,quoteCatalog,hasHypotheticalOpening,publicArticleIssues,uncitedAssertionIssues} from "../src/quality.js";
function compileSourcedArticle(value,catalog){
  return compilePlainArticle({...value,editorial_paragraphs:value.editorial_paragraphs.map(p=>p.map(({premise_checks,...segment})=>segment))},catalog);
}
import {checkedWriting,articleRequirements,editorialRoles} from "../src/grounding.js";
const data={format:"TEXT",purpose:"Kiểm chứng",materials:[{material_id:"m",text:"Ghi chú nguồn công khai"}]};
test("actual v24 metadata-leak output remains rejected despite source citation and prior empty audit",()=>{
  const actual=readFileSync(new URL("../../../tests/fixtures/workers_b_v24_metadata_failed.txt",import.meta.url),"utf8");
  assert.ok(actual.includes("Hướng biên tập:"));assert.ok(publicArticleIssues(actual).length);
  assert.deepEqual(publicArticleIssues("Tình huống giả định: một tin nhắn.\n\nHãy hỏi người gửi đường dẫn gốc."),[]);
});
const writingPrompt="chỉ trả prose tiếng Việt, không JSON hoặc phân tích.\n"+JSON.stringify(data);
const analysis={observations:[{material_id:"m",quote:data.materials[0].text,observation:"Ghi chú đã chọn."}],inferences:["Giả thuyết biên tập chưa xác nhận."],direction:"Hỏi đường dẫn nguồn gốc trước khi chia sẻ."};
const schema={type:"object",properties:{observations:{type:"array"},inferences:{type:"array"},direction:{type:"string"},draft_body:{type:"string"},factual_claims:{type:"array"}}};
function draft(){
  const typed={hypothetical_opening:"Bạn nhận được một ảnh chụp thông báo thiếu đường dẫn gốc.",editorial_paragraphs:[
    "Hãy hỏi người gửi về đường dẫn gốc, ngày công bố và đơn vị đăng thông báo trước khi quyết định chia sẻ.",
    "Khi mở được bản gốc, hãy đọc phần trước và sau đoạn chụp, chú ý phạm vi áp dụng thay vì đoán theo tiêu đề.",
    "Hãy đối chiếu nội dung cụ thể và nguồn bằng chứng; nhiều bài lặp lại cùng một tin chưa phải là nguồn độc lập.",
    "Nếu vẫn thiếu căn cứ, hãy giữ Chưa xác nhận và nhắn người gửi: bạn có đường dẫn gốc để mình kiểm tra không?",
  ]};return {typed,body:compileEditedArticle(typed),written:{...typed,editorial_paragraphs:typed.editorial_paragraphs.map(text=>[{text,source_quote_id:-1,premise_checks:[]}])}};
}
function requirementReceipts(offset=0){return articleRequirements.map((r,i)=>({index:offset+i,verdict:"ADDRESSED",segment_indexes:r.id==="PROJECT_CLOSING_MESSAGE"?[4]:[1,2,3,4],reason:"Synthetic task coverage only; not a semantic quality assessment."}));}
function audit(body,issues=[]){return {finding_checks:requirementReceipts(),article_sha256:createHash("sha256").update(body).digest("hex"),segments:body.split(/\n\s*\n/u).map((_,index)=>({index,kind:index===0?"HYPOTHETICAL":"ADVICE",issues:index===0?issues:[]}))};}
function asPlan(value){return value && Array.isArray(value.editorial_paragraphs)?{...value,editorial_paragraphs:Object.fromEntries(editorialRoles.map((role,i)=>[role.key,value.editorial_paragraphs[i]]))}:value;}
function request(extra={}){if(extra.previous)extra={...extra,previous:extra.previous.map(p=>p.stage==="writing"?{...p,value:asPlan(p.value)}:p)};return new Request("https://example.test/b/generate",{method:"POST",headers:{authorization:"Bearer fixture","content-type":"application/json"},body:JSON.stringify({prompt:"notes",writing_prompt:writingPrompt,schema,...extra})});}
// Synthetic valid edit receipts for protocol fixtures, not source-quality QA.
function fixtureChecks(first, edited){
  const catalog=quoteCatalog(data.materials);
  const before=compileSourcedArticle(checkedWriting(first,catalog),catalog),after=compileSourcedArticle({hypothetical_opening:edited.hypothetical_opening,editorial_paragraphs:edited.editorial_paragraphs.map(p=>p.map(({premise_checks,...segment})=>segment))},catalog);
  return before.segments.map((s,index)=>{
    const same=after.segments.find(t=>t.text===s.text);
    const target=same??after.segments[index];
    return {index,support:index===0?"HYPOTHETICAL":s.citation?"SOURCE_SUPPORTED":"SUGGESTION",
      quote_ids:s.citation?[0]:[],decision:same?"KEEP":target?"REWRITE":"REMOVE",
      final_segment_indexes:target?[target.index]:[],note:"Synthetic protocol fixture; no semantic quality claim."};
  });
}
function fixtureOperations(first,edited){
  const before=compileSourcedArticle(checkedWriting(first,quoteCatalog(data.materials)),quoteCatalog(data.materials));
  const after=compileSourcedArticle({hypothetical_opening:edited.hypothetical_opening,editorial_paragraphs:edited.editorial_paragraphs.map(p=>p.map(({premise_checks,...segment})=>segment))},quoteCatalog(data.materials));
  const receipt=edited.source_checks??fixtureChecks(first,edited);
  return {source_issues:edited.source_issues??[],edits:receipt.map(check=>{
    const {final_segment_indexes,...rest}=check;
    return {...rest,premise_checks:check.support==="SOURCE_SUPPORTED"?[{assertion:before.segments[check.index].text,quote_ids:check.quote_ids,supported:true}]:[],replacement:check.decision==="REWRITE"?final_segment_indexes.map(index=>({text:after.segments[index].text,source_quote_id:after.segments[index].citation?0:-1,premise_checks:after.segments[index].citation?[{assertion:after.segments[index].text,quote_ids:[0],supported:true}]:[]})):[]};
  })};
}
function environment(outputs,model="@cf/openai/gpt-oss-120b"){
  const captured=[];
  return {captured,env:{CREATOR_KEY:"fixture",CREATOR_MODEL:model,CREATOR_VERSION:{id:"pinned-version"},AI:{run:async(_,options)=>{
    captured.push(options);let value=outputs[captured.length-1];if(value instanceof Error)throw value; if(captured.length===3 && value && typeof value==="object" && value.editorial_paragraphs)value=fixtureOperations(outputs[1],value);
    if(options.response_format?.json_schema?.properties?.editorial_paragraphs?.type==="object")value=asPlan(value);
    return {choices:[{message:{content:typeof value==="string"?value:JSON.stringify(value),reasoning_content:"private reasoning fixture"},finish_reason:"stop"}],usage:{prompt_tokens:12,completion_tokens:30}};
  }}}};
}
test("source analysis precedes writing; conflicting prose instructions never enter JSON stages",async()=>{
  const {body,written}=draft();const {captured,env}=environment([analysis,written,written,audit(body)]);
  const response=await worker.fetch(request(),env);assert.equal(response.status,200);assert.equal(captured.length,4);
  for(const [index,options] of captured.entries()){
    assert.equal(options.input,undefined);assert.equal(options.instructions,undefined);assert.equal(options.reasoning_effort,index===0?"low":"medium");
    assert.equal(options.max_output_tokens,undefined);assert.equal(options.messages[0].role,"system");assert.equal(options.messages[1].role,"user");
    assert.equal(options.messages[1].content.includes("chỉ trả prose"),false);assert.ok(options.messages[1].content.includes(data.materials[0].text));
  }
  for(const i of [1,2]){
    assert.equal(captured[i].messages[0].content.includes(data.materials[0].text),false);
    assert.equal(captured[i].messages[1].content.includes(analysis.inferences[0]),false);
    assert.equal(captured[i].messages[1].content.includes(analysis.observations[0].observation),false);
    assert.ok(captured[i].messages[1].content.includes("UNVERIFIED_PROPOSAL_NOT_SOURCE_EVIDENCE"));
  }
  assert.deepEqual(captured.map(c=>c.max_tokens),[2600,3000,4500,2200]);
  const result=await response.json();assert.deepEqual(result.stages.map(s=>s.stage),["analysis","writing","editing","source_audit"]);
  assert.equal(result.stages[1].body,body);assert.equal(JSON.parse(result.result.response).draft_body,body);
  assert.equal(result.quality_audit.draft_body,body);assert.deepEqual(result.quality_audit.issues,[]);assert.equal(result.worker_version,"pinned-version");
  assert.equal(result.result.response.includes("private reasoning fixture"),false);
});
test("structured model gets the actual stage schemas",async()=>{
  const {body,written}=draft();const {captured,env}=environment([analysis,written,written,audit(body)],"@cf/aisingapore/gemma-sea-lion-v4-27b-it");
  assert.equal((await worker.fetch(request(),env)).status,200);
  assert.deepEqual(captured[0].response_format.json_schema.required,["observations","inferences","direction"]);
  assert.deepEqual(captured[1].response_format.json_schema.properties.editorial_paragraphs.required,editorialRoles.map(r=>r.key));
  assert.equal(captured[1].response_format.json_schema.properties.hypothetical_opening.minLength,1);
  assert.equal(captured[3].response_format.json_schema.properties.article_sha256.const,createHash("sha256").update(body).digest("hex"));
});
test("explicit Qwen model keeps JSON stage contract and bounded non-thinking input",async()=>{
  const {body,written}=draft();const {captured,env}=environment([analysis,written,written,audit(body)],"@cf/qwen/qwen3-30b-a3b-fp8");
  assert.equal((await worker.fetch(request(),env)).status,200);
  assert.equal(captured.length,4);assert.deepEqual(captured.map(c=>c.max_tokens),[2600,3000,4500,2200]);
  for(const options of captured){assert.deepEqual(options.chat_template_kwargs,{enable_thinking:false});assert.equal(options.reasoning_effort,undefined);assert.equal(options.response_format.type,"json_schema");assert.ok(options.messages[1].content.includes("Return only JSON matching this schema"));}
});
test("AI.run JSON mode object response keeps roles, budgets and source checks",async()=>{
  const {body,written}=draft();const outputs=[analysis,written,{source_checks:fixtureChecks(written,written),source_issues:[],...written},audit(body)];let calls=0;
  const {env}=environment([]);env.AI.run=async(_,options)=>{
    assert.equal(options.messages[0].role,"system");assert.equal(options.response_format.type,"json_schema");
    assert.ok(options.max_tokens>0);assert.equal(options.input,undefined);
    const value=outputs[calls++];return {response:calls===3?fixtureOperations(written,value):calls===2?asPlan(value):value,usage:{input_tokens:12,output_tokens:30}};
  };
  const result=await worker.fetch(request(),env);assert.equal(result.status,200);assert.equal(calls,4);
  assert.equal(JSON.parse((await result.json()).result.response).draft_body,body);
});
test("unsupported direct-root results fail closed without provider fallback",async()=>{
  const {body,written}=draft();const outputs=[analysis,written,{source_checks:fixtureChecks(written,written),source_issues:[],...written},audit(body)];let calls=0;
  const {env}=environment([]);env.AI.run=async()=>({...outputs[calls++],usage:{input_tokens:12,output_tokens:30}});
  assert.equal((await worker.fetch(request(),env)).status,502);assert.equal(calls,1);
  env.AI.run=async()=>({...analysis,unexpected_private_field:"must not be accepted"});
  assert.equal((await worker.fetch(request(),env)).status,502);
});
test("unsupported text-completion envelope fails closed without provider fallback",async()=>{
  const {body,written}=draft();const outputs=[analysis,written,{source_checks:fixtureChecks(written,written),source_issues:[],...written},audit(body)];let calls=0;
  const {env}=environment([]);env.AI.run=async()=>({choices:[{index:0,text:JSON.stringify(outputs[calls++]),finish_reason:"stop"}],usage:{prompt_tokens:12,completion_tokens:30}});
  assert.equal((await worker.fetch(request(),env)).status,502);assert.equal(calls,1);
});
test("no auth, over-budget input or missing selected context cannot call model",async()=>{
  const {env,captured}=environment([]);
  assert.equal((await worker.fetch(new Request("https://example.test/b/generate",{method:"POST",body:"{}"}),env)).status,401);
  assert.equal((await worker.fetch(request({writing_prompt:"x".repeat(70000)}),env)).status,413);
  assert.equal((await worker.fetch(request({writing_prompt:"No selected data"}),env)).status,400);assert.equal(captured.length,0);
});
test("remote exception cannot leak; stage failure is finite",async()=>{
  const {env,captured}=environment([new Error("private exception")]);const response=await worker.fetch(request(),env);
  assert.equal(response.status,502);assert.equal(captured.length,1);const failure=await response.json();assert.equal(failure.stage,"analysis");
  assert.equal(failure.retryable,false);assert.equal(JSON.stringify(failure).includes("private exception"),false);
  assert.deepEqual(JSON.parse(failure.stage_output).stage_diagnostic,{phase:"NATIVE_BINDING",binding_calls:1,native_failure:{code:null,flags:[]}});
});
test("empty or provider-failed output retains allowlisted diagnosis without secrets or reasoning",async()=>{
  for(const [output,code,received] of [[new Error("3036 daily allocation private-key"),3036,false],[null,null,true]]){
    const {env}=environment([]);env.AI.run=async()=>{
      if(output instanceof Error)throw output;
      return {output:[{type:"reasoning",summary:[{text:"private reasoning"}]}],usage:{completion_tokens:2200,total_tokens:3500,private_key:"secret"}};
    };
    const response=await worker.fetch(request(),env);assert.equal(response.status,502);
    const raw=await response.json();const diagnostic=JSON.parse(raw.stage_output);
    assert.equal(diagnostic.provider_code,code);assert.equal(diagnostic.response_received,received);
    assert.equal(JSON.stringify(raw).includes("private-key"),false);assert.equal(JSON.stringify(raw).includes("private reasoning"),false);
    if(received){assert.equal(diagnostic.usage.completion_tokens,2200);assert.deepEqual(diagnostic.output_types,["reasoning"]);assert.equal(diagnostic.usage.private_key,undefined);}
  }
});
test("observed prose-only editor failure is rejected with exact stage and no repair",async()=>{
  const failed=readFileSync(new URL("../../../tests/fixtures/workers_b_v11_failed.txt",import.meta.url),"utf8").trim();
  const {env,captured}=environment([analysis,failed]);const response=await worker.fetch(request(),env);
  assert.equal(response.status,502);assert.equal(captured.length,2);const failure=await response.json();
  assert.equal(failure.stage,"writing");assert.equal(failure.stage_output.includes("private reasoning fixture"),false);assert.equal(hasHypotheticalOpening(failed),false);
});

test("actual bdf3d07 empty opening is rejected without relocating prose or calling later stages",async()=>{
  const actual=JSON.parse(readFileSync(new URL("../../../tests/fixtures/workers_b_bdf3d07_empty_opening.json",import.meta.url),"utf8"));
  const before=JSON.stringify(actual.written);
  assert.equal(actual.written.hypothetical_opening,"");
  assert.ok(actual.written.editorial_paragraphs[0][0].text.startsWith("Giả sử"));
  const {env,captured}=environment([analysis,actual.written]);
  const response=await worker.fetch(request({writing_prompt:"notes\n"+JSON.stringify({...data,materials:actual.materials})}),env);
  assert.equal(response.status,502);assert.equal(captured.length,2);
  const failure=await response.json();
  assert.equal(failure.stage,"writing");assert.equal(failure.retryable,false);
  assert.equal(failure.reason,"STAGE_OUTPUT_INVALID");
  assert.deepEqual(JSON.parse(JSON.parse(failure.stage_output).selected_writing_output),asPlan(actual.written));
  assert.equal(JSON.stringify(actual.written),before,"No synthetic repair or first-paragraph extraction");
});
test("audit cannot rewrite article or omit paragraphs",async()=>{
  const {body,written}=draft();for(const report of [{...audit(body),draft_body:body+" changed"},{...audit(body),segments:[]}]){
    const {env,captured}=environment([analysis,written,written,report]);const response=await worker.fetch(request(),env);
    assert.equal(response.status,422);assert.equal(captured.length,4);assert.equal((await response.json()).candidate_body,body);
  }
});
test("reported unsupported efficacy or note-writer attribution cannot pass",async()=>{
  const {body,written}=draft();const issues=["Unsupported search efficacy", "Hướng biên tập wrongly attributed to UNESCO"];
  const {env,captured}=environment([analysis,written,written,audit(body,issues)]);const response=await worker.fetch(request(),env);
  assert.equal(response.status,422);assert.equal(captured.length,4);const failure=await response.json();
  assert.deepEqual(failure.audit_issues,issues);assert.equal(failure.retryable,false);
});
test("audited factual passage requires a Claim annotation",async()=>{
  const {body,written}=draft();const report=audit(body);report.segments[1].kind="SOURCE_FACT";
  const {env}=environment([analysis,written,written,report]);assert.equal((await worker.fetch(request(),env)).status,422);
});
test("composition preserves prose and uses one visible fictional label",()=>{
  const {typed,body}=draft();assert.equal(body.split("\n\n")[0],"Tình huống giả định: "+typed.hypothetical_opening);
  assert.deepEqual(body.split("\n\n").slice(1),typed.editorial_paragraphs);
  assert.equal(compileEditedArticle({...typed,hypothetical_opening:"Tình huống giả định: "+typed.hypothetical_opening}),body);
  for(const invalid of [{draft_body:body},{...typed,editorial_paragraphs:[]},{...typed,hypothetical_opening:"line1\nline2"}])assert.throws(()=>compileEditedArticle(invalid));
});

test("four stage requests execute each model step once and preserve the verified predecessor chain",async()=>{
  const {body,written}=draft();const {env,captured}=environment([analysis,written,written,audit(body)]);
  const first=await worker.fetch(request({stage:"analysis",previous:[]}),env);
  assert.equal(first.status,200);const a=(await first.json()).stage_result;
  assert.equal(a.stage,"analysis");assert.equal(captured.length,1);
  const second=await worker.fetch(request({stage:"writing",previous:[a]}),env);
  assert.equal(second.status,200);const b=(await second.json()).stage_result;
  assert.equal(b.stage,"writing");assert.equal(captured.length,2);
  const edit=await worker.fetch(request({stage:"editing",previous:[a,b]}),env);
  assert.equal(edit.status,200);const c=(await edit.json()).stage_result;assert.equal(captured.length,3);
  const third=await worker.fetch(request({stage:"source_audit",previous:[a,b,c]}),env);
  assert.equal(third.status,200);assert.equal(captured.length,4);
  const result=await third.json();assert.equal(JSON.parse(result.result.response).draft_body,body);
  assert.deepEqual(result.stages.map(s=>s.stage),["analysis","writing","editing","source_audit"]);
});
test("missing or reordered predecessor is rejected before model execution",async()=>{
  const {env,captured}=environment([]);
  for(const previous of [[],[{stage:"writing",value:{x:1}}]]){
    assert.equal((await worker.fetch(request({stage:"writing",previous}),env)).status,400);
  }
  assert.equal(captured.length,0);
});

test("Vietnamese text is never regenerated through Unicode const in audit schema",()=>{
  const body="Tình huống giả định: hỏi nguồn và giữ Chưa xác nhận.";
  const hash=createHash("sha256").update(body).digest("hex");
  const schema=auditSchema(hash,1);
  assert.equal(JSON.stringify(schema).includes(body),false);
  assert.equal(schema.properties.article_sha256.const,hash);
  assert.equal(schema.properties.segments.items.properties.text,undefined);
});

test("one declared editor receives risky passages and its output is audited, never patched by the app",async()=>{
  const {body,written}=draft();
  const bad={...written,editorial_paragraphs:[...written.editorial_paragraphs]};
  bad.editorial_paragraphs[3]=[{...written.editorial_paragraphs[3][0],text:written.editorial_paragraphs[3][0].text+" Việc này sẽ giúp bạn tránh lan truyền tin giả."}];
  const {env,captured}=environment([analysis,bad,written,audit(body)]);
  const response=await worker.fetch(request(),env);assert.equal(response.status,200);assert.equal(captured.length,4);
  assert.ok(captured[2].messages[1].content.includes("sẽ giúp bạn tránh lan truyền"));
  const output=await response.json();assert.ok(output.stages[1].body.includes("sẽ giúp bạn tránh lan truyền"));
  assert.equal(output.stages[2].body,body);assert.equal(JSON.parse(output.result.response).draft_body,body);
});
test("editor receives concrete source/metadata warnings and must return findings with the revised text",async()=>{
  const {body,written}=draft();const bad={...written,editorial_paragraphs:structuredClone(written.editorial_paragraphs)};
  bad.editorial_paragraphs[0][0].text="Hướng biên tập: hãy truy cập công cụ này.";
  const {env,captured}=environment([analysis,bad,{...written,source_issues:["Internal note and unspecified tool"]},{...audit(body),finding_checks:[{index:0,verdict:"ADDRESSED",segment_indexes:[1],reason:"The revised paragraph asks for the source link; internal heading and unspecified tool were removed."},...requirementReceipts(1)]}]);
  const response=await worker.fetch(request(),env);assert.equal(response.status,200);
  assert.ok(captured[2].messages[1].content.includes("Internal research-note headings"));
  assert.ok(captured[2].messages[1].content.includes("ambiguous tool referents"));
  assert.deepEqual((await response.json()).stages[2].source_issues,["Internal note and unspecified tool"]);
  const outputs=[analysis,written,written];let calls=0;
  env.AI.run=async()=>({response:asPlan(outputs[calls++])});
  assert.equal((await worker.fetch(request(),env)).status,502);assert.equal(calls,3);
});

test("inline citations bind exact original Unicode quotes through catalog IDs",()=>{
  const {written}=draft();const catalog=quoteCatalog(data.materials);
  const cited={text:"Ghi chú nguồn công khai",source_quote_id:0};
  const value={...written,editorial_paragraphs:[[cited],...written.editorial_paragraphs.slice(1)]};
  const output=compileSourcedArticle(value,catalog);
  assert.ok(output.body.includes(cited.text));assert.deepEqual(output.claims,[{text:cited.text,material_ids:["m"],source_quote:data.materials[0].text}]);
  assert.throws(()=>compileSourcedArticle({...value,factual_claims:[{text:"not in article"}]},catalog));
  for(const invalid of [{...cited,source_quote_id:1},{...cited,source_quote_id:-2},{...cited,source_quote_id:"0"},{...cited,source_quote:"invented"}]){
    assert.throws(()=>compileSourcedArticle({...value,editorial_paragraphs:[[invalid],...written.editorial_paragraphs.slice(1)]},catalog));
  }
  assert.throws(()=>compileSourcedArticle({...value,editorial_paragraphs:[[cited,cited],...written.editorial_paragraphs.slice(1)]},catalog));
  assert.deepEqual(quoteCatalog([{material_id:"u",text:"  “Nguồn gốc?”\r\n\nDòng hai.  "}]),[{material_id:"u",source_quote:"“Nguồn gốc?”"},{material_id:"u",source_quote:"Dòng hai."}]);
});
test("every inline segment is audited once; immutable text and source Claim survive composition",()=>{
  const {written}=draft();const value={...written,editorial_paragraphs:[[
    {text:"Hãy đọc ghi chú. ",source_quote_id:-1},{text:data.materials[0].text,source_quote_id:0}],...written.editorial_paragraphs.slice(1)]};
  const {body,claims,segments}=compileSourcedArticle(value,quoteCatalog(data.materials));
  assert.ok(body.includes("Hãy đọc ghi chú. Ghi chú nguồn công khai"));assert.equal(segments.length,6);
  const hash=createHash("sha256").update(body).digest("hex");
  const raw={finding_checks:[],article_sha256:hash,segments:segments.map((s,i)=>({index:i,kind:i===0?"HYPOTHETICAL":i===2?"SOURCE_FACT":"ADVICE",issues:[]}))};
  const bound=bindAudit(body,hash,raw,segments);assert.equal(validAudit({draft_body:body,factual_claims:claims},bound),true);
  assert.deepEqual(segments[2].citation,claims[0]);
  const hidden=structuredClone(raw);hidden.segments[2].kind="ADVICE";
  assert.equal(bindAudit(body,hash,hidden,segments),null,"A cited assertion cannot evade source checking as advice");
  assert.deepEqual(bound.paragraphs[1].factual_passages,[claims[0].text]);
  assert.equal(bindAudit(body,hash,{...raw,segments:raw.segments.slice(1)},segments),null);
  assert.equal(bindAudit(body,hash,{...raw,segments:raw.segments.map(s=>({...s,index:0}))},segments),null);
  const unsupported=structuredClone(raw);unsupported.segments[1].kind="SOURCE_FACT";
  assert.equal(validAudit({draft_body:body,factual_claims:claims},bindAudit(body,hash,unsupported,segments)),false);
  const flagged=structuredClone(raw);flagged.segments[2].issues=["Quote does not support meaning"];
  assert.equal(validAudit({draft_body:body,factual_claims:claims},bindAudit(body,hash,flagged,segments)),false);
});


test("actual SDK pilot false-empty audit remains FAIL without modifying saved output",()=>{
  const actual=JSON.parse(readFileSync(new URL("../../../tests/fixtures/workers_b_v25_uncited_assertions.json",import.meta.url),"utf8"));
  const before=JSON.stringify(actual);assert.deepEqual(actual.audit.issues,[]);
  assert.equal(uncitedAssertionIssues(actual.content.draft_body,actual.content.factual_claims).length,2);
  assert.equal(JSON.stringify(actual),before);
  const statement="Ghi chú nói khả năng sai lệch sẽ cao hơn.";
  assert.deepEqual(uncitedAssertionIssues(statement,[{text:statement}]),[],"Citation still requires separate entailment audit");
  assert.deepEqual(uncitedAssertionIssues("Hãy hỏi người gửi đường dẫn gốc để đối chiếu."),[]);
});

test("every editorial finding needs an explicit text-bound audit disposition",()=>{
  const {written}=draft();const composed=compileSourcedArticle(written,quoteCatalog(data.materials));
  const hash=createHash("sha256").update(composed.body).digest("hex");
  const findings=["Selected introduction does not describe these steps"];
  const raw=audit(composed.body);
  assert.equal(bindAudit(composed.body,hash,raw,composed.segments,findings),null);
  raw.finding_checks=[{index:0,verdict:"UNRESOLVED",segment_indexes:[1],reason:"The assertion is still present, including inside advice."}];
  let bound=bindAudit(composed.body,hash,raw,composed.segments,findings);
  assert.equal(validAudit({draft_body:composed.body,factual_claims:[]},bound),false);
  assert.deepEqual(bound.issues,findings,"Empty segment issues cannot erase unresolved findings");
  raw.finding_checks[0]={index:0,verdict:"ADDRESSED",segment_indexes:[1],reason:"Revised text asks for the missing source instead of claiming unavailable steps."};
  bound=bindAudit(composed.body,hash,raw,composed.segments,findings);
  assert.equal(validAudit({draft_body:composed.body,factual_claims:[]},bound),true);
  assert.equal(bound.finding_checks[0].finding,findings[0]);
  assert.equal(bound.finding_checks[0].segments[0].text,composed.segments[1].text);
  for(const change of [{index:1},{reason:" "},{segment_indexes:[]},{segment_indexes:[100]},{segment_indexes:[1,1]}]){
    const invalid=structuredClone(raw);Object.assign(invalid.finding_checks[0],change);
    assert.equal(bindAudit(composed.body,hash,invalid,composed.segments,findings),null);
  }
});

test("saved v25 editor findings cannot be discarded by the old empty model audit",()=>{
  const saved=JSON.parse(readFileSync(new URL("../../../tests/fixtures/workers_b_v25_editor_findings.json",import.meta.url),"utf8"));
  const failed=JSON.parse(readFileSync(new URL("../../../tests/fixtures/workers_b_v25_uncited_assertions.json",import.meta.url),"utf8"));
  const before=JSON.stringify(saved);
  const catalog=saved.catalog;
  const composed=compileSourcedArticle({hypothetical_opening:saved.edited.hypothetical_opening,editorial_paragraphs:saved.edited.editorial_paragraphs},catalog);
  assert.equal(composed.body,failed.content.draft_body);
  const hash=createHash("sha256").update(composed.body).digest("hex");
  const raw={article_sha256:hash,finding_checks:[],segments:composed.segments.map((s,index)=>({index,kind:index===0?"HYPOTHETICAL":s.citation?"SOURCE_FACT":"ADVICE",issues:[]}))};
  assert.ok(saved.edited.source_issues.length>0);
  assert.equal(bindAudit(composed.body,hash,raw,composed.segments,saved.edited.source_issues),null,"Missing finding coverage rejects before any keyword check");
  assert.equal(JSON.stringify(saved),before);
});

test("uncited probability or absolute cannot evade audit under advice; no fourth model call",async()=>{
  for(const text of ["Nếu không rõ, khả năng sai lệch sẽ cao hơn.","Kiểm tra đa nguồn luôn là cách an toàn nhất."]){
    const {written}=draft();written.editorial_paragraphs[0].push({text,source_quote_id:-1,premise_checks:[]});
    const {env,captured}=environment([analysis,written,written]);const response=await worker.fetch(request(),env);
    assert.equal(response.status,422);assert.equal(captured.length,3);
    const failed=await response.json();assert.equal(failed.retryable,false);assert.ok(failed.candidate_body.includes(text));assert.ok(failed.audit_issues.length);
    const composed=compileSourcedArticle(written,quoteCatalog(data.materials));
    const bound=bindAudit(composed.body,createHash("sha256").update(composed.body).digest("hex"),audit(composed.body),composed.segments);
    // The old model report also has wrong segment count, which fails closed.
    assert.equal(bound,null);
  }
});

test("writing rejects a declared unsupported premise before editing or audit can relabel it",async()=>{
  for(const segment of [
    {source_quote_id:-1,text:"A suggested action establishes an outcome.",premise_checks:[{assertion:"The action establishes the outcome.",quote_ids:[],supported:false}]},
    {source_quote_id:-1,text:"Uncited empirical premise.",premise_checks:[{assertion:"An empirical premise.",quote_ids:[0],supported:true}]},
    {source_quote_id:0,text:"A factual sentence without an assertion check.",premise_checks:[]},
  ]){
    const {written}=draft();written.editorial_paragraphs[0].push(segment);
    const {env,captured}=environment([analysis,written]);
    const response=await worker.fetch(request(),env);
    assert.equal(response.status,502);assert.equal(captured.length,2);
    const failed=await response.json();assert.equal(failed.stage,"writing");assert.equal(failed.retryable,false);
    const diagnostic=JSON.parse(failed.stage_output);
    assert.deepEqual(JSON.parse(diagnostic.selected_writing_output),asPlan(written));
    assert.equal(JSON.stringify(failed).includes("private reasoning fixture"),false);
  }
});

test("duplicate audit finding mappings remain rejected after native schema adaptation",()=>{
  const {body,written}=draft();const composed=compileSourcedArticle(written,quoteCatalog(data.materials));
  const hash=createHash("sha256").update(body).digest("hex");const raw=audit(body);
  raw.finding_checks=[{index:0,verdict:"ADDRESSED",segment_indexes:[1,1],reason:"Duplicate structural mapping"}];
  assert.equal(bindAudit(body,hash,raw,composed.segments,["Check a passage"]),null);
});

test("generation has no source-conflicting minimum length; editing owns each replacement",async()=>{
  const {body,written}=draft();const {captured,env}=environment([analysis,written,written,audit(body)]);
  assert.equal((await worker.fetch(request(),env)).status,200);
  for(const index of [1,2]){
    assert.ok(captured[index].messages[0].content.includes("no minimum length"));
    assert.ok(!captured[index].messages[0].content.includes("250–350"));
  }
  assert.deepEqual(Object.keys(captured[2].response_format.json_schema.properties),["source_issues","edits"]);
  assert.ok(captured[2].messages[0].content.includes("Do not output a second article"));
});

test("task requirements are audited even when the editor claims no source issues",async()=>{
  const {body,written}=draft();
  const {captured,env}=environment([analysis,written,written,audit(body)]);
  const response=await worker.fetch(request(),env);assert.equal(response.status,200);
  assert.equal(captured[3].response_format.json_schema.properties.finding_checks.minItems,3);
  for(const requirement of articleRequirements){
    assert.ok(captured[1].messages[1].content.includes(requirement.id));
    assert.ok(captured[2].messages[1].content.includes(requirement.id));
    assert.ok(captured[3].messages[1].content.includes(requirement.id));
  }
  const result=await response.json();
  assert.deepEqual(result.stages[2].source_issues,[]);
  assert.deepEqual(result.stages[2].task_requirements,articleRequirements.map(r=>r.id));
  assert.deepEqual(result.quality_audit.finding_checks.map(c=>c.finding),articleRequirements.map(r=>r.id));
  const omitted={...audit(body),finding_checks:[]};
  const rejected=environment([analysis,written,written,omitted]);
  const failed=await worker.fetch(request(),rejected.env);
  assert.ok([422,502].includes(failed.status));assert.equal(rejected.captured.length,4);
});

test('native effort is unchanged for analysis and medium for all three generative quality stages',async()=>{
  const {body,written}=draft();const {captured,env}=environment([analysis,written,written,audit(body)],'@cf/openai/gpt-oss-120b');
  assert.equal((await worker.fetch(request(),env)).status,200);
  assert.deepEqual(captured.map(c=>c.reasoning_effort),['low','medium','medium','medium']);
  assert.deepEqual(captured.map(c=>c.max_tokens),[2600,3000,4500,2200]);
});

test('native writing and replacement schemas correlate selected quotes and premise IDs',async()=>{
  const {body,written}=draft();const {captured,env}=environment([analysis,written,written,audit(body)]);
  assert.equal((await worker.fetch(request(),env)).status,200);
  const writing=editorialRoles.map(role=>captured[1].response_format.json_schema.properties.editorial_paragraphs.properties[role.key].items.oneOf);
  const editBranches=captured[2].response_format.json_schema.properties.edits.items.oneOf;
  for(const branch of editBranches.filter(b=>b.properties.index.const===0 && b.properties.decision.const==='REWRITE'))assert.deepEqual(branch.properties.replacement.items.oneOf.map(b=>b.properties.source_quote_id.const),[-1]);
  const replacements=editBranches.filter(b=>!(b.properties.index.const===0 && b.properties.decision.const==='REWRITE')).map(branch=>branch.properties.replacement.items.oneOf);
  for(const branches of [...writing,...replacements]){
    assert.deepEqual(branches.map(b=>b.properties.source_quote_id.const),[-1,0]);
    assert.equal(branches[0].properties.premise_checks.maxItems,0);
    assert.equal(branches[1].properties.premise_checks.items.properties.quote_ids.items.const,0);
  }
});

test('actual medium mixed-quote output still stops before editing when native schema is ignored',async()=>{
  const actual=JSON.parse(readFileSync(new URL('../../../tests/fixtures/workers_b_d7315a2_mixed_context.json',import.meta.url),'utf8'));
  const groups=new Map();for(const q of actual.catalog)groups.set(q.material_id,[...(groups.get(q.material_id)??[]),q.source_quote]);
  const materials=[...groups].map(([material_id,lines])=>({material_id,text:lines.join('\n')}));
  const {env,captured}=environment([actual.writing]);
  const response=await worker.fetch(request({stage:'writing',previous:[{stage:'analysis',value:analysis,usage:{}}],writing_prompt:'Selected data:\n'+JSON.stringify({format:'TEXT',materials})}),env);
  assert.equal(response.status,502);assert.equal(captured.length,1);
  const failure=await response.json();assert.equal(failure.stage,'writing');assert.equal(failure.retryable,false);
  const branches=captured[0].response_format.json_schema.properties.editorial_paragraphs.properties.context_and_scope.items.oneOf;
  assert.deepEqual(branches.map(b=>b.properties.source_quote_id.const),[-1,3,8,9]);
});
