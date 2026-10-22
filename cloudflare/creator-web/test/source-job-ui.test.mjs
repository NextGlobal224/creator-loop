import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
import {preparePending} from '../public/pending.mjs';
const app=readFileSync(new URL('../public/app.js',import.meta.url),'utf8');
const actual=app.slice(app.indexOf('async function startSourceJob('),app.indexOf('\nasync function api('));
function setup(api){
 const map=new Map(),context={submitting:false,jobBusy:false,projectEpoch:1,$:()=>({value:'project'}),pending:()=>null,inspectPending:async()=>{},api,preparePending,id:()=> 'a'.repeat(32),storageKey:'pending',localStorage:{setItem:(k,v)=>map.set(k,v)},clearPending:()=>map.delete('pending'),watch:async()=>{},jobId:null};
 runInNewContext(actual,context);return {context,map};
}
test('unavailable source grant never submits or loses user state',async()=>{
 const calls=[],{context,map}=setup(async path=>{calls.push(path);return {available:false,message:'Quota/operator missing'};});
 await assert.rejects(context.startSourceJob('project','file'),/Quota\/operator/);
 assert.deepEqual(calls,['/api/source-grant-status']);assert.equal(map.size,0);assert.equal(context.submitting,false);
});
test('concurrent click and changed source during grant read cannot submit old input',async()=>{
 let resolve;const calls=[],{context,map}=setup(async path=>{calls.push(path);return new Promise(r=>resolve=r);});
 let current=true;const first=context.startSourceJob('project','file',()=>current);
 await assert.rejects(context.startSourceJob('project','other'),/đang gửi/);
 current=false;resolve({available:true});await assert.rejects(first,/đã đổi/);
 assert.deepEqual(calls,['/api/source-grant-status']);assert.equal(map.size,0);
});
test('lost enqueue acknowledgment keeps original request and subsequent click only reconciles',async()=>{
 const calls=[],{context,map}=setup(async(path,data,key)=>{calls.push(path);if(path==='/api/source-grant-status')return {available:true};assert.equal(key,'a'.repeat(32));assert.equal(map.size,1);throw Error('lost acknowledgment');});
 await assert.rejects(context.startSourceJob('project','file'),/lost acknowledgment/);
 const saved=JSON.parse(map.get('pending'));assert.equal(saved.data.file_id,'file');assert.equal(saved.key,'a'.repeat(32));
 let reads=0;context.pending=()=>saved;context.inspectPending=async()=>reads++;
 await assert.rejects(context.startSourceJob('project','file'),/Đã kiểm/);
 assert.equal(reads,1);assert.deepEqual(calls,['/api/source-grant-status','/api/source-jobs']);
});
