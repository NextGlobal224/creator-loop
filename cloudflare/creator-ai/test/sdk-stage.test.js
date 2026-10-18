import test from "node:test";
import assert from "node:assert/strict";
import {generateStage, SDK_COMPONENT} from "../src/sdk-stage.js";
import {readFileSync} from 'node:fs';
import {checkedWriting,operationSchema,applyEditOperations} from '../src/grounding.js';
import {quoteCatalog,compileSourcedArticle} from '../src/quality.js';

const task={instructions:"Tiếng Việt; nguồn là dữ liệu.",input:"Ghi chú “nguồn gốc”",schema:{type:"object",properties:{text:{type:"string"}},required:["text"],additionalProperties:false},budget:2200,temperature:0};
function setup(run){let calls=0;return {env:{CREATOR_MODEL:"@cf/openai/gpt-oss-120b",AI:{run:async(...args)=>{calls++;return run(...args);}}},count:()=>calls};}
test("real SDK/provider forward roles/schema/budget/effort and preserve Unicode once",async()=>{
  const {env,count}=setup(async(model,input,options)=>{
    assert.equal(model,"@cf/openai/gpt-oss-120b");assert.equal(input.messages[0].role,"system");
    assert.equal(input.messages[1].role,"user");assert.equal(input.max_tokens,2200);assert.equal(input.reasoning_effort,"low");
    assert.equal(input.response_format.type,"json_schema");assert.equal(input.tools,undefined);assert.equal(options.gateway,undefined);
    return {response:{text:"Tiếng Việt: “nguồn gốc”, không sửa dấu."},usage:{prompt_tokens:8,completion_tokens:12,neurons:1.25}};
  });
  let raw;const result=await generateStage(env,task,value=>raw=value);
  assert.equal(count(),1);assert.equal(result.calls,1);assert.equal(result.value.text,"Tiếng Việt: “nguồn gốc”, không sửa dấu.");
  assert.equal(raw.usage.neurons,1.25);assert.equal(SDK_COMPONENT,"workers-ai-provider@4.0.0/ai@7.0.142");
});
test("429/capacity/5xx failures execute exactly once at SDK and transport boundaries",async()=>{
  for(const message of ["3036 daily allocation exceeded","3040 capacity temporarily exceeded","500 backend error"]){
    const {env,count}=setup(async()=>{throw new Error(message);});
    await assert.rejects(generateStage(env,task,()=>{}));assert.equal(count(),1);
  }
});

test("explicit medium effort changes only the requested native effort; analysis default is unchanged",async()=>{
  const captured=[];
  const {env,count}=setup(async(_model,input)=>{captured.push(input);return {response:{text:"Same typed response"},usage:{prompt_tokens:8,completion_tokens:12}};});
  await generateStage(env,task,()=>{});
  await generateStage(env,{...task,reasoningEffort:"medium"},()=>{});
  assert.equal(count(),2);assert.equal(captured[0].reasoning_effort,"low");assert.equal(captured[1].reasoning_effort,"medium");
  assert.deepEqual({...captured[0],reasoning_effort:"medium"},captured[1]);
  await assert.rejects(generateStage(env,{...task,reasoningEffort:"unsupported"},()=>{}));assert.equal(count(),2);
});
test("malformed/fenced/empty/truncated JSON cannot trigger repair, retry or fallback",async()=>{
  for(const reply of ["",' {"text":', '```json\n{"text":"fenced"}\n```']){
    const {env,count}=setup(async()=>({response:reply,usage:{prompt_tokens:2,completion_tokens:3}}));
    await assert.rejects(generateStage(env,task,()=>{}));assert.equal(count(),1);
  }
  const {env,count}=setup(async()=>({choices:[{message:{content:'{"text":"partial"}'},finish_reason:"length"}]}));
  await assert.rejects(generateStage(env,task,()=>{}));assert.equal(count(),1);
});
test("abort during transport stops the stage without a second request",async()=>{
  const controller=new AbortController();
  const {env,count}=setup(async(_model,_input,options)=>{
    controller.abort();options.signal.throwIfAborted();
  });
  await assert.rejects(generateStage(env,{...task,signal:controller.signal},()=>{}));assert.equal(count(),1);
});


test("failure diagnostics distinguish SDK validation, native rejection and reply parsing without another call",async()=>{
  for(const [phase,taskOverride,reply,expectedCalls] of [
    ["SDK_BEFORE_BINDING",{budget:-1},()=>({response:{text:"unused"}}),0],
    ["NATIVE_BINDING",{},()=>{throw new Error("private provider exception");},1],
    ["SDK_AFTER_BINDING",{},()=>({response:'{"text":',usage:{completion_tokens:2}}),1],
  ]){
    const {env,count}=setup(reply);
    await assert.rejects(generateStage(env,{...task,...taskOverride},()=>{}),error=>{
      assert.equal(error.stage_diagnostic.phase,phase);assert.equal(error.stage_diagnostic.binding_calls,expectedCalls);return true;
    });
    assert.equal(count(),expectedCalls);
  }
});

test("native diagnostic exports fixed classifications only, before SDK wrapping",async()=>{
  const {env,count}=setup(()=>{throw new Error('4001: JSON schema uniqueItems unsupported private-secret-value');});
  await assert.rejects(generateStage(env,task,()=>{}),error=>{
    assert.deepEqual(error.stage_diagnostic.native_failure,{code:4001,flags:["SCHEMA","UNIQUE_ITEMS","UNSUPPORTED","JSON"]});
    assert.ok(!JSON.stringify(error.stage_diagnostic).includes("private-secret-value"));return true;
  });assert.equal(count(),1);
});

test("native decoding omits unsupported uniqueness without mutating the full schema",async()=>{
  const schema={type:"object",properties:{ids:{type:"array",uniqueItems:true,items:{type:"integer"}}},required:["ids"],additionalProperties:false};
  const before=JSON.stringify(schema);
  for(const ids of [[1,2]]){
    const {env,count}=setup((_model,input)=>{
      assert.equal(input.response_format.json_schema.properties.ids.uniqueItems,undefined);
      assert.ok(input.messages[1].content.includes('"uniqueItems":true'));
      return {response:{ids},usage:{completion_tokens:10}};
    });
    assert.deepEqual((await generateStage(env,{...task,schema},()=>{})).value,{ids});
    assert.equal(count(),1);assert.equal(JSON.stringify(schema),before);
  }
});

test('actual ce273a1 partial-branch reply cannot erase inherited receipts in the native decoding schema',async()=>{
  const actual=JSON.parse(readFileSync(new URL('../../../tests/fixtures/workers_b_ce273a1_missing_receipts.json',import.meta.url),'utf8'));
  const original=JSON.stringify(actual),catalog=quoteCatalog(actual.materials);
  const initial=compileSourcedArticle(checkedWriting(actual.written,catalog),catalog);
  const schema=operationSchema(initial.segments.length,catalog,initial),before=JSON.stringify(schema);
  assert.equal(actual.operations.edits[0].support,undefined,'Actual failed native reply omitted parent receipts');
  const required=schema.properties.edits.items.required;
  const {env,count}=setup((_model,input)=>{
    const item=input.response_format.json_schema.properties.edits.items;
    assert.equal(item.properties,undefined,'Use complete alternatives rather than a partial branch intersection');
    for(const branch of item.oneOf){
      assert.equal(branch.type,'object');assert.equal(branch.additionalProperties,false);
      assert.deepEqual(branch.required,required);
      for(const key of required)assert.ok(branch.properties[key],key+' must survive in every native alternative');
      assert.equal(branch.properties.index.type,'integer');
      assert.equal(branch.properties.decision.type,'string');
      assert.equal(branch.properties.replacement.type,'array');
      if(branch.properties.decision.const==='REWRITE'){
        assert.equal(branch.properties.replacement.minItems,1);assert.equal(branch.properties.replacement.maxItems,branch.properties.index.const===0?1:12);
      }else assert.equal(branch.properties.replacement.maxItems,0);
    }
    assert.ok(input.messages[1].content.includes('"support"'),'Full SDK/prompt contract remains unchanged');
    // Deliberately bypass native decoding to replay the actual bad reply. The
    // app must still reject it; expansion must never repair its JSON output.
    return {response:actual.operations,usage:{completion_tokens:1}};
  });
  const result=await generateStage(env,{...task,schema},()=>{});
  assert.deepEqual(result.value,actual.operations);
  assert.throws(()=>applyEditOperations(result.value,initial,catalog),/Invalid edit identity/);
  assert.equal(count(),1);assert.equal(JSON.stringify(actual),original);assert.equal(JSON.stringify(schema),before);
});
