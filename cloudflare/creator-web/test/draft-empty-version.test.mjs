import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
import {savedSelection,changedDraftText} from '../public/pending.mjs';

// Execute the real form renderer: a saved empty value must not become the
// workflow's old proposal and trigger the unsaved-edit guard on Project change.
const app=readFileSync(new URL('../public/app.js',import.meta.url),'utf8');
const renderer=app.slice(app.indexOf('function version(){'),app.indexOf('\nfunction preview(){'));
test('saved empty direction/body reopen exactly; real edits still need confirmation',()=>{
 for(const output_format of ['TEXT','IMAGE_CAPTION']){
  const saved={draft_version_id:'v2',version_no:2,body_text:'',direction:'',image_file_id:null,image_provenance:'',image_usage_note:''};
  const workflow={versions:[saved],direction_text:'Old direction',llm_proposal:{draft_body:'Old proposal'},output_format};
  const fields=new Map();
  const $=selector=>{if(!fields.has(selector))fields.set(selector,{value:'',options:[]});return fields.get(selector);};
  $('#versions').value='v2';
  const context={workflow,$,savedSelection,editorRevision:0,displayedVersion:'proposal',preview(){},option(){assert.fail('no image should be added');}};
  runInNewContext(renderer+'\nversion();',context);
  const editor={body_text:$('#editor').value,direction:$('#editor-direction').value,image_file_id:$('#image').value,image_provenance:$('#image-source').value,image_usage_note:$('#image-rights').value};
  assert.equal(editor.body_text,'');assert.equal(editor.direction,'');
  assert.equal(changedDraftText(workflow,saved,editor),false);
  assert.equal(changedDraftText(workflow,saved,{...editor,direction:'User edit'}),true);
  assert.equal(changedDraftText(workflow,saved,{...editor,body_text:'User edit'}),true);
  $('#versions').value='proposal';runInNewContext('version();',context);
  assert.equal($('#editor').value,'Old proposal');
  assert.equal($('#editor-direction').value,'Old direction');
 }
});
