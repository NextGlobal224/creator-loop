import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {createHash} from "node:crypto";
import worker from "../src/index.js";
import {auditSchema,compileEditedArticle,compileSourcedArticle,quoteCatalog,hasHypotheticalOpening} from "../src/quality.js";
const data={format:"TEXT",purpose:"Kiểm chứng",materials:[{material_id:"m",text:"Ghi chú nguồn công khai"}]};
const writingPrompt="chỉ trả prose tiếng Việt, không JSON hoặc phân tích.\n"+JSON.stringify(data);
const analysis={observations:[{material_id:"m",quote:data.materials[0].text,observation:"Ghi chú đã chọn."}],inferences:["Giả thuyết biên tập chưa xác nhận."],direction:"Hỏi đường dẫn nguồn gốc trước khi chia sẻ."};
const schema={type:"object",properties:{observations:{type:"array"},inferences:{type:"array"},direction:{type:"string"},draft_body:{type:"string"},factual_claims:{type:"array"}}};
function draft(){
  const typed={hypothetical_opening:"Bạn nhận được một ảnh chụp thông báo thiếu đường dẫn gốc.",editorial_paragraphs:[
    "Hãy hỏi người gửi về đường dẫn gốc, ngày công bố và đơn vị đăng thông báo trước khi quyết định chia sẻ.",
    "Khi mở được bản gốc, hãy đọc phần trước và sau đoạn chụp, chú ý phạm vi áp dụng thay vì đoán theo tiêu đề.",
    "Hãy đối chiếu nội dung cụ thể và nguồn bằng chứng; nhiều bài lặp lại cùng một tin chưa phải là nguồn độc lập.",
    "Nếu vẫn thiếu căn cứ, hãy giữ Chưa xác nhận và nhắn người gửi: bạn có đường dẫn gốc để mình kiểm tra không?",
  ]};return {typed,body:compileEditedArticle(typed),written:{...typed,editorial_paragraphs:typed.editorial_paragraphs.map(text=>[{text,source_quote_id:-1}])}};
}
function audit(body,issues=[]){return {article_sha256:createHash("sha256").update(body).digest("hex"),issues,paragraphs:body.split(/\n\s*\n/u).map((_,index)=>({index,kind:index===0?"HYPOTHETICAL":"ADVICE",factual_passages:[]}))};}
function request(extra={}){return new Request("https://example.test/b/generate",{method:"POST",headers:{authorization:"Bearer fixture","content-type":"application/json"},body:JSON.stringify({prompt:"notes",writing_prompt:writingPrompt,schema,...extra})});}
function environment(outputs,model="@cf/openai/gpt-oss-120b"){
  const captured=[];
  return {captured,env:{CREATOR_KEY:"fixture",CREATOR_MODEL:model,CREATOR_VERSION:{id:"pinned-version"},AI:{run:async(_,options)=>{
    captured.push(options);const value=outputs[captured.length-1];if(value instanceof Error)throw value;
    return {output:[{type:"reasoning",summary:[{text:"private reasoning fixture"}]},{type:"message",content:[{type:"output_text",text:typeof value==="string"?value:JSON.stringify(value)}]}],usage:{input_tokens:12,output_tokens:30}};
  }}}};
}
test("source analysis precedes writing; conflicting prose instructions never enter JSON stages",async()=>{
  const {body,written}=draft();const {captured,env}=environment([analysis,written,written,audit(body)]);
  const response=await worker.fetch(request(),env);assert.equal(response.status,200);assert.equal(captured.length,4);
  for(const options of captured){
    assert.equal(options.input,undefined);assert.equal(options.instructions,undefined);
    assert.equal(options.max_output_tokens,undefined);assert.equal(options.messages[0].role,"system");assert.equal(options.messages[1].role,"user");
    assert.equal(options.messages[1].content.includes("chỉ trả prose"),false);assert.ok(options.messages[1].content.includes(JSON.stringify(data)));
  }
  assert.ok(captured[1].messages[1].content.includes(JSON.stringify(analysis)));
  assert.deepEqual(captured.map(c=>c.max_tokens),[2600,3000,3000,2200]);
  const result=await response.json();assert.deepEqual(result.stages.map(s=>s.stage),["analysis","writing","editing","source_audit"]);
  assert.equal(result.stages[1].body,body);assert.equal(JSON.parse(result.result.response).draft_body,body);
  assert.equal(result.quality_audit.draft_body,body);assert.deepEqual(result.quality_audit.issues,[]);assert.equal(result.worker_version,"pinned-version");
  assert.equal(result.result.response.includes("private reasoning fixture"),false);
});
test("structured model gets the actual stage schemas",async()=>{
  const {body,written}=draft();const {captured,env}=environment([analysis,written,written,audit(body)],"@cf/aisingapore/gemma-sea-lion-v4-27b-it");
  assert.equal((await worker.fetch(request(),env)).status,200);
  assert.deepEqual(captured[0].response_format.json_schema.required,["observations","inferences","direction"]);
  assert.equal(captured[1].response_format.json_schema.properties.editorial_paragraphs.minItems,4);
  assert.equal(captured[3].response_format.json_schema.properties.article_sha256.const,createHash("sha256").update(body).digest("hex"));
});
test("AI.run JSON mode object response keeps roles, budgets and source checks",async()=>{
  const {body,written}=draft();const outputs=[analysis,written,written,audit(body)];let calls=0;
  const {env}=environment([]);env.AI.run=async(_,options)=>{
    assert.equal(options.messages[0].role,"system");assert.equal(options.response_format.type,"json_schema");
    assert.ok(options.max_tokens>0);assert.equal(options.input,undefined);
    return {response:outputs[calls++],usage:{input_tokens:12,output_tokens:30}};
  };
  const result=await worker.fetch(request(),env);assert.equal(result.status,200);assert.equal(calls,4);
  assert.equal(JSON.parse((await result.json()).result.response).draft_body,body);
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
});
test("observed prose-only editor failure is rejected with exact stage and no repair",async()=>{
  const failed=readFileSync(new URL("../../../tests/fixtures/workers_b_v11_failed.txt",import.meta.url),"utf8").trim();
  const {env,captured}=environment([analysis,failed]);const response=await worker.fetch(request(),env);
  assert.equal(response.status,502);assert.equal(captured.length,2);const failure=await response.json();
  assert.equal(failure.stage,"writing");assert.equal(failure.stage_output,failed);assert.equal(hasHypotheticalOpening(failed),false);
});
test("audit cannot rewrite article or omit paragraphs",async()=>{
  const {body,written}=draft();for(const report of [{...audit(body),draft_body:body+" changed"},{...audit(body),paragraphs:[]}]){
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
  const {body,written}=draft();const report=audit(body);report.paragraphs[1].kind="SOURCE_FACT";report.paragraphs[1].factual_passages=[body.split("\n\n")[1]];
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
  assert.equal(schema.properties.paragraphs.items.properties.text,undefined);
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
