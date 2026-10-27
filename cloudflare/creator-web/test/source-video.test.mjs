// Protocol/guard doubles only: no local video decoding and no cloud/model call.
import test from 'node:test';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {videoIdentity,decodeSourceVideo,ownedVideoSession} from '../src/source-video-codec.js';
import {analyzeVideoFrames} from '../src/source-video.js';
import {boundedGrant} from '../public/operator-grant.mjs';
const raw=Buffer.concat([Buffer.from([0,0,0,24]),Buffer.from('ftypisom'),Buffer.alloc(32)]),hash=createHash('sha256').update(raw).digest('hex');
const packet={video:raw.toString('base64'),input_sha256:hash,browser_reserved_seconds:60};
function binding({exception=false,closeEarly=false}={}){
 const commands=[],closed=[];class Socket extends EventTarget{
  accept(){}
  close(){this.dispatchEvent(new Event('close'));}
  send(text){const q=JSON.parse(text);assert(Number.isSafeInteger(q.id));commands.push(q);queueMicrotask(()=>{
   if(closeEarly)return this.close();
   const event=new Event('message');event.data=JSON.stringify({id:q.id,result:q.method==='Runtime.evaluate'?(exception?{exceptionDetails:{text:'fixture decoder failure'}}:{result:{value:{duration_ms:1000,audio:'fixture-not-real-audio',frames:[]}}}):{}});this.dispatchEvent(event);
  });}
 }
 const socket=new Socket();return {commands,closed,launch:async options=>{assert.equal(options.keepAlive,10000);assert.equal(options.recording,false);return {sessionId:'owned-fixture',targets:[{id:'fixture-page',type:'page'}]};},connectSession:async(id,options)=>{assert.equal(id,'owned-fixture');assert.equal(options.targetId,'fixture-page');return {webSocket:{fetch:async()=>({webSocket:socket})}};},closeSession:async id=>{closed.push(id);return {status:'closed'};}};
}
test('Video original hash and real container bounds are checked before any browser',async()=>{
 assert.equal((await videoIdentity(packet.video,hash)).mime,'video/mp4');
 for(const [b,h] of [[packet.video,'f'.repeat(64)],[Buffer.from('<html>bad</html>').toString('base64'),hash],['!',hash],[Buffer.alloc(8*1024*1024+1).toString('base64'),hash]])await assert.rejects(videoIdentity(b,h));
});
test('Missing included browser reservation never launches',async()=>{
 let launches=0;await assert.rejects(decodeSourceVideo({launch(){launches++;}},JSON.stringify({...packet,browser_reserved_seconds:undefined})));assert.equal(launches,0);
});
test('Cloud-only decoder blocks external requests and closes exactly its session',async()=>{
 const b=binding(),r=JSON.parse(await decodeSourceVideo(b,JSON.stringify(packet)));
 assert.equal(r.ok,true);assert.equal(r.browser_lifecycle.close_status,'CLOSED');assert.equal(r.input_sha256,hash);assert.equal(r.browser_session_id,'owned-fixture');assert.deepEqual(b.closed,['owned-fixture']);
 assert.deepEqual(b.commands.map(c=>c.method),['Network.enable','Network.setBlockedURLs','Runtime.evaluate']);
 assert.deepEqual(b.commands[1].params.urls,['http://*','https://*','ws://*','wss://*']);
});
test('Decoder failures/closed sockets close owned session with no retry',async()=>{
 for(const options of [{exception:true},{closeEarly:true}]){const b=binding(options),r=JSON.parse(await decodeSourceVideo(b,JSON.stringify(packet)));assert.equal(r.ok,false);assert.equal(r.browser_lifecycle.close_status,'CLOSED');assert.deepEqual(b.closed,['owned-fixture']);}
});

test('Lifecycle bounds a never-settling launch and closes late owned launch once',async()=>{
 let resolveLaunch,launches=0,used=0;const b=binding();
 b.launch=()=>{launches++;return new Promise(resolve=>{resolveLaunch=resolve;});};
 const result=await ownedVideoSession(b,()=>{used++;},{workMs:15,cleanupMs:10});
 assert.equal(result.ok,false);assert.equal(result.browser_lifecycle.phase,'LAUNCH');
 assert.equal(result.browser_lifecycle.launch_outcome,'UNKNOWN');assert.equal(result.browser_lifecycle.close_status,'UNKNOWN');
 resolveLaunch({sessionId:'late-owned',targets:[]});
 await new Promise(resolve=>setTimeout(resolve,5));
 assert.equal(launches,1);assert.equal(used,0);assert.deepEqual(b.closed,['late-owned']);
 // A late side effect cannot rewrite the receipt that was returned/persisted.
 assert.equal(result.browser_lifecycle.close_status,'UNKNOWN');
});

test('Hung connect/upgrade share launch deadline and retain cleanup receipt',async()=>{
 for(const phase of ['CONNECT','UPGRADE']){
  const b=binding();let used=0;
  if(phase==='CONNECT')b.connectSession=()=>new Promise(()=>{});
  else b.connectSession=async()=>({webSocket:{fetch:()=>new Promise(()=>{})}});
  const r=await ownedVideoSession(b,()=>{used++;},{workMs:15,cleanupMs:10});
  assert.equal(r.ok,false);assert.equal(r.browser_lifecycle.phase,phase);
  assert.equal(r.browser_lifecycle.close_status,'CLOSED');assert.deepEqual(b.closed,['owned-fixture']);assert.equal(used,0);
 }
});

test('Late socket upgrade closes only its socket after deadline',async()=>{
 const b=binding();let resolveUpgrade,lateClosed=0;
 b.connectSession=async()=>({webSocket:{fetch:()=>new Promise(resolve=>{resolveUpgrade=resolve;})}});
 const r=await ownedVideoSession(b,()=>assert.fail('late decoder'),{workMs:15,cleanupMs:10});
 resolveUpgrade({webSocket:{close(){lateClosed++;}}});await new Promise(resolve=>setTimeout(resolve,5));
 assert.equal(r.ok,false);assert.equal(lateClosed,1);assert.deepEqual(b.closed,['owned-fixture']);
});

test('Hung/rejected/unconfirmed close cannot report success or hang the caller',async()=>{
 for(const close of [()=>new Promise(()=>{}),()=>Promise.reject(Error('fixture')),async()=>({status:'closing'}),async()=>undefined]){
  const b=binding();let calls=0;b.closeSession=id=>{calls++;assert.equal(id,'owned-fixture');return close();};
  const r=await ownedVideoSession(b,async()=>({decoded:true}),{workMs:15,cleanupMs:10});
  assert.equal(r.ok,false);assert.ok(['UNKNOWN','CLOSING'].includes(r.browser_lifecycle.close_status));assert.equal(calls,1);
 }
});
test('Video vision keeps all timestamps/images and exactly one fixed model request',async()=>{
 let calls=0;const b={run:async(model,p)=>{calls++;assert.equal(model,'@cf/google/gemma-4-26b-a4b-it');assert.equal(p.messages[0].content.filter(x=>x.type==='image_url').length,2);return {choices:[],usage:{neurons:2}};}};
 const p={request_id:'a'.repeat(32),input_sha256:hash,duration_ms:2500,frames:[{at_ms:0,image:'YQ=='},{at_ms:2000,image:'Yg=='}]};
 const r=JSON.parse(await analyzeVideoFrames(b,JSON.stringify(p),'fixture-version'));assert.equal(calls,1);assert.equal(r.native_usage.neurons,2);
 await assert.rejects(analyzeVideoFrames(b,JSON.stringify({...p,frames:p.frames.slice(1)}),'fixture-version'));assert.equal(calls,1);
});
test('Video native failure retains unknown usage without retry/fallback',async()=>{
 let calls=0;const r=JSON.parse(await analyzeVideoFrames({run:async()=>{calls++;throw Error('fixture provider');}},JSON.stringify({request_id:'a'.repeat(32),input_sha256:hash,duration_ms:1000,frames:[{at_ms:0,image:'YQ=='}]}),'fixture-version'));
 assert.equal(calls,1);assert.equal(r.unknown_outcome,true);assert.equal(r.native_usage,null);
});
test('Video grant requires fresh same-day included AI AND Browser Run evidence',()=>{
 const now=Date.UTC(2026,9,27,8),q={remaining:8000,observedAt:now,confirmed:true,accountPlan:'Paid',includedOnlyConfirmed:true,purpose:'SOURCE_VIDEO_ANALYSIS',browserRemaining:1000,browserObservedAt:now,browserIncludedConfirmed:true};
 const g=boundedGrant(q,now);assert.equal(g.remaining_calls,2);assert.equal(g.browser_forecast_seconds,600);assert.equal(g.max_extra_charge_usd,0);
 for(const change of [{browserRemaining:599},{browserRemaining:Infinity},{browserIncludedConfirmed:false},{browserObservedAt:now-120001},{accountPlan:'Free'}])assert.throws(()=>boundedGrant({...q,...change},now));
});
