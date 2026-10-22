import test from 'node:test';
import assert from 'node:assert/strict';
import {createHash,webcrypto} from 'node:crypto';
import {AUDIO_MODEL,transcribeSource} from '../src/source-audio.js';
globalThis.crypto??=webcrypto;
// Transport protocol fixture only: actual PCM validation belongs to the domain
// probe before reservation. This does not establish source-reading quality.
const bytes=Buffer.alloc(44),packet={request_id:'a'.repeat(32),input_sha256:createHash('sha256').update(bytes).digest('hex'),model:AUDIO_MODEL,duration_ms:1000,audio:bytes.toString('base64')};
test('one native transcription preserves response, binding and unknown usage',async()=>{
 let calls=0;const native={text:'Đoạn chưa được duyệt.',segments:[{start:0,end:1,text:'Đoạn chưa được duyệt.'}]};
 const result=JSON.parse(await transcribeSource({async run(model,input){calls++;assert.equal(model,AUDIO_MODEL);assert.equal(input.task,'transcribe');assert.equal(input.language,'vi');return native;}},JSON.stringify(packet),'qa-deployment'));
 assert.equal(calls,1);assert.deepEqual(result.native_response,native);assert.equal(result.native_usage,null);assert.equal(result.request_id,packet.request_id);assert.equal(result.input_sha256,packet.input_sha256);assert.equal(result.worker_version,'qa-deployment');assert.equal(result.model_calls,1);
});
test('provider rejection retains one unknown call; no fallback or retry',async()=>{
 let calls=0;const result=JSON.parse(await transcribeSource({async run(){calls++;throw Error('provider failure');}},JSON.stringify(packet),'qa-deployment'));
 assert.equal(calls,1);assert.equal(result.ok,false);assert.equal(result.unknown_outcome,true);assert.equal(result.native_usage,null);assert.equal(result.model_calls,1);assert(!JSON.stringify(result).includes('provider failure'));
});
test('wrong bytes/model/request or unbounded audio are rejected before inference',async()=>{
 for(const change of [{input_sha256:'0'.repeat(64)},{input_sha256:[packet.input_sha256]},{model:'other'},{request_id:'bad'},{request_id:[packet.request_id]},{audio:'??'},{duration_ms:120001},{duration_ms:0},{audio:''}]){
  await assert.rejects(transcribeSource({run(){assert.fail('must not call model');}},JSON.stringify({...packet,...change}),'qa-deployment'));
 }
 for(const deployment of [null,{},[],1,' '])await assert.rejects(transcribeSource({run(){assert.fail('must not call model');}},JSON.stringify(packet),deployment));
 for(const input of ['null','[]','1'])await assert.rejects(transcribeSource({run(){assert.fail('must not call model');}},input,'qa-deployment'));
});
