import test from 'node:test';
import assert from 'node:assert/strict';
import {mountSourceReview} from '../public/source-review.mjs';

class Element {
 constructor(){this.value='';this.checked=false;this.disabled=false;this.children=[];this.textContent='';}
 pause(){} removeAttribute(){} setAttribute(){} scrollIntoView(){} focus(){} setSelectionRange(){}
 append(...items){this.children.push(...items)} replaceChildren(...items){this.children=items}
 querySelectorAll(){return this.children.flatMap(label=>label.children??[]).filter(child=>child.type==='checkbox'&&child.checked)}
}
test('source-use pending belongs to project and file, including reopen and failure',async()=>{
 const oldDocument=globalThis.document,oldOption=globalThis.Option;
 globalThis.document={createElement:()=>new Element()};globalThis.Option=class extends Element {constructor(label,value){super();this.value=value}};
 try{
  const elements=new Map(),root={querySelector:s=>{if(!elements.has(s))elements.set(s,new Element());return elements.get(s)}};
  let project='A',message='';const pending=new Map(),saved=new Map(),calls=[];
  const api=async(url,body)=>{
   if(!body)return {media_type:'TEXT',text:'source',source_extraction:null,evidence_versions:[{evidence_id:'e',evidence_version_id:'v1',version_no:1,review_state:'PENDING',locator_type:'TEXT_RANGE',locator_data:'{"start":0,"end":2}',content:'so'}],source_use:saved.get(project)??null};
   calls.push(body);return new Promise((resolve,reject)=>pending.set(body.project_id,{resolve:()=>{saved.set(body.project_id,{state:body.state,version_no:1,use_version_id:'u',evidence_version_ids:body.evidence_version_ids});resolve({})},reject}));
  };
  const ui=mountSourceReview({api,root,getProject:()=>project,afterChange:async()=>{},say:s=>message=s});
  const open=async(p,file='file-'+p)=>{project=p;await ui.open(file)},choose=()=>{root.querySelector('#source-evidence').children[0].children[0].checked=true;root.querySelector('#source-confirm').checked=true};
  await open('A');choose();const a=root.querySelector('#source-allow').onclick();
  await open('B');assert.equal(root.querySelector('#source-allow').disabled,false);choose();const b=root.querySelector('#source-allow').onclick();
  pending.get('A').resolve();await a;assert.equal(root.querySelector('#source-allow').disabled,true);
  await open('A');assert.equal(root.querySelector('#source-allow').disabled,false);await open('B');assert.equal(root.querySelector('#source-allow').disabled,true);
  pending.get('B').resolve();await b;assert.match(root.querySelector('#source-use-status').textContent,/SELECTED/);assert.equal(root.querySelector('#source-allow').disabled,false);
  await open('C');choose();const c=root.querySelector('#source-allow').onclick();await open('D');await open('C');assert.equal(root.querySelector('#source-allow').disabled,true);
  pending.get('C').reject(Error('Không lưu được nguồn'));await c;assert.equal(message,'Không lưu được nguồn');assert.equal(root.querySelector('#source-allow').disabled,false);
  await open('E','file-1');choose();const e1=root.querySelector('#source-allow').onclick(),first=pending.get('E');
  await open('E','file-2');choose();const e2=root.querySelector('#source-allow').onclick(),second=pending.get('E');
  first.resolve();await e1;assert.equal(root.querySelector('#source-allow').disabled,true);second.resolve();await e2;assert.equal(root.querySelector('#source-allow').disabled,false);
  assert.equal(calls.length,5);
 }finally{globalThis.document=oldDocument;globalThis.Option=oldOption}
});

for(const [mediaType,track] of [['AUDIO','audio'],['VIDEO','audio'],['VIDEO','video']])test(`reopening ${mediaType}/${track} keeps its correction pending and refreshes the result`,async()=>{
 const oldDocument=globalThis.document,oldOption=globalThis.Option;
 globalThis.document={createElement:()=>new Element()};globalThis.Option=class extends Element {constructor(label,value){super();this.value=value}};
 try{
  const elements=new Map(),root={querySelector:s=>{if(!elements.has(s))elements.set(s,new Element());return elements.get(s)}};
  let version=1,resolveSave,rejectSave,message='',calls=0;
  const api=async(url,body)=>{
   if(!body)return {media_type:mediaType,text:null,source_extraction:null,evidence_versions:[{evidence_id:'e',evidence_version_id:'v'+version,version_no:version,review_state:'PENDING',locator_type:'TIME_RANGE',locator_data:JSON.stringify({track,start_ms:100,end_ms:200}),content:'source'}],source_use:null};
   calls++;return new Promise((resolve,reject)=>{resolveSave=()=>{version=2;resolve({})};rejectSave=()=>reject(Error('Không lưu được transcript'))});
  };
  const ui=mountSourceReview({api,root,getProject:()=> 'A',afterChange:async()=>{},say:s=>message=s});
  const select=()=>root.querySelector('#source-evidence').children[1].onclick();
  await ui.open('file');select();root.querySelector('#source-transcript').value='corrected';root.querySelector('#source-reason').value='reason';
  const save=root.querySelector('#source-audio-save').onclick();await ui.open('file');select();
  assert.equal(root.querySelector('#source-audio-save').disabled,true);
  resolveSave();await save;
  assert.match(root.querySelector('#source-evidence').children[0].children[1].textContent,/v2/);
  assert.match(message,/Đã lưu phiên bản transcript/);assert.equal(calls,1);
  select();root.querySelector('#source-transcript').value='retry';const failed=root.querySelector('#source-audio-save').onclick();await ui.open('file');select();
  assert.equal(root.querySelector('#source-audio-save').disabled,true);rejectSave();await failed;
  assert.equal(message,'Không lưu được transcript');assert.equal(root.querySelector('#source-audio-save').disabled,false);assert.equal(calls,2);
 }finally{globalThis.document=oldDocument;globalThis.Option=oldOption}
});
