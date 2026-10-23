import test from 'node:test';
import assert from 'node:assert/strict';
import {generateStage,stageDeadline,stageSignal} from '../src/sdk-stage.js';
import {safeFailure} from '../src/quality.js';

const settings={instructions:'Offline signal boundary',input:'Fixture only',schema:{type:'object',properties:{ok:{type:'boolean'}},required:['ok'],additionalProperties:false},budget:20,temperature:0};
test('Web can use the explicit bounded alarm deadline while Windows keeps its default',()=>{
  assert.equal(stageDeadline({}),85000);
  assert.equal(stageDeadline({CREATOR_STAGE_TIMEOUT_MS:120000}),120000);
  for(const value of [0,120001,Infinity,'120000'])assert.throws(()=>stageDeadline({CREATOR_STAGE_TIMEOUT_MS:value}),/bounded stage deadline/);
});
async function bounded(promise){
  let timer;
  try{return await Promise.race([promise,new Promise((_,reject)=>{timer=setTimeout(()=>reject(new Error('Abort did not settle a non-cooperative binding')),1000);})]);}
  finally{clearTimeout(timer);}
}
test('signal abort bounds a native binding that never settles, with one invocation and fixed safe diagnostics',async()=>{
  const controller=new AbortController();let entered;const ready=new Promise(resolve=>{entered=resolve;});let calls=0,observed=0;
  const pending=generateStage({CREATOR_MODEL:'@cf/openai/gpt-oss-120b',AI:{run:()=>{calls++;entered();return new Promise(()=>{});}}},{...settings,signal:controller.signal},()=>{observed++;});
  await ready;controller.abort(new Error('private authorization text must not be exported'));
  await assert.rejects(bounded(pending),error=>{
    assert.equal(error.name,'AbortError');assert.equal(error.stage_diagnostic.phase,'NATIVE_BINDING');assert.equal(error.stage_diagnostic.binding_calls,1);assert.equal(error.stage_diagnostic.signal_aborted,true);
    assert.equal(safeFailure(error,null).stage_diagnostic.signal_aborted,true);
    assert.equal(JSON.stringify(safeFailure(error,null)).includes('private authorization'),false);return true;
  });
  assert.equal(calls,1);assert.equal(observed,0);
});
test('late native completion after abort cannot observe output or start a second request',async()=>{
  const controller=new AbortController();let entered,complete;const ready=new Promise(resolve=>{entered=resolve;});let calls=0,observed=0;
  const pending=generateStage({CREATOR_MODEL:'@cf/openai/gpt-oss-120b',AI:{run:()=>{calls++;entered();return new Promise(resolve=>{complete=resolve;});}}},{...settings,signal:controller.signal},()=>{observed++;});
  await ready;controller.abort();await assert.rejects(bounded(pending),{name:'AbortError'});
  complete({response:{ok:true},usage:{prompt_tokens:1,completion_tokens:1}});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(calls,1);assert.equal(observed,0);
});

test('request cancellation is distinct from the configured deadline without exporting its reason',async()=>{
  const controller=new AbortController();let entered;const ready=new Promise(resolve=>{entered=resolve;});let calls=0;
  const env={CREATOR_MODEL:'@cf/openai/gpt-oss-120b',AI:{run:()=>{calls++;entered();return new Promise(()=>{});}}};
  const pending=generateStage(env,{...settings,signal:stageSignal(env,controller.signal)},()=>assert.fail('No response'));
  await ready;controller.abort(new Error('private request reason'));
  await assert.rejects(bounded(pending),error=>{
    const safe=safeFailure(error,null);
    assert.equal(safe.stage_diagnostic.abort_source,'REQUEST_SIGNAL');
    assert.equal(safe.stage_diagnostic.abort_reason,'OTHER');
    assert.equal(safe.stage_diagnostic.deadline_ms,85000);
    assert(Number.isInteger(safe.stage_diagnostic.stage_elapsed_ms));
    assert.equal(safe.response_received,false);assert.equal(JSON.stringify(safe).includes('private'),false);return true;
  });
  assert.equal(calls,1);
});

test('deadline expiry of a non-cooperative binding retains provenance and never retries',async t=>{
  const deadline=new AbortController(),nativeTimeout=AbortSignal.timeout;
  t.mock.method(AbortSignal,'timeout',ms=>{assert.equal(ms,120000);return deadline.signal;});
  let entered;const ready=new Promise(resolve=>{entered=resolve;});let calls=0;
  const env={CREATOR_MODEL:'@cf/openai/gpt-oss-120b',CREATOR_STAGE_TIMEOUT_MS:120000,AI:{run:()=>{calls++;entered();return new Promise(()=>{});}}};
  const signal=stageSignal(env,new AbortController().signal);
  // Only shorten the controlled test clock; the production timeout stays 120s.
  t.mock.restoreAll();assert.equal(AbortSignal.timeout,nativeTimeout);
  const pending=generateStage(env,{...settings,signal},()=>assert.fail('No response'));
  await ready;deadline.abort(new DOMException('private timeout reason','TimeoutError'));
  await assert.rejects(bounded(pending),error=>{
    const safe=safeFailure(error,null);
    assert.equal(safe.stage_diagnostic.phase,'NATIVE_BINDING');
    assert.equal(safe.stage_diagnostic.abort_source,'STAGE_DEADLINE');
    assert.equal(safe.stage_diagnostic.abort_reason,'TimeoutError');
    assert.equal(safe.stage_diagnostic.deadline_ms,120000);
    assert.equal(safe.response_received,false);assert.deepEqual(safe.usage,{});
    assert.equal(JSON.stringify(safe).includes('private'),false);return true;
  });
  assert.equal(calls,1);
});
