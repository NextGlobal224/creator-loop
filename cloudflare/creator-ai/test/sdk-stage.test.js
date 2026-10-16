import test from "node:test";
import assert from "node:assert/strict";
import {generateStage, SDK_COMPONENT} from "../src/sdk-stage.js";

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
