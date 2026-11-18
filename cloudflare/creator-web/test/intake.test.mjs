import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {fileKind,materialBytes,maxFileBytes} from '../public/intake.mjs';
test('Unicode filename selects image type and UTF8 Vietnamese keeps exact bytes',async()=>{
 assert.equal(fileKind('Ảnh thử có dấu.png','image/png'),'IMAGE');
 const text='Tư liệu tiếng Việt có dấu và khoảng trắng.';
 assert.deepEqual(await materialBytes(new File([text],'Tư liệu có dấu.txt'),'', 'TEXT'),new TextEncoder().encode(text));
});
test('phone audio without a useful MIME is selected as audio without changing original bytes',async()=>{
 const raw=new Uint8Array([82,73,70,70,4,0,0,0,87,65,86,69]);
 for(const name of ['Ghi âm.WAV','Điện thoại.MP3','Điện thoại.M4A'])for(const mime of ['', 'application/octet-stream']){
  const file=new File([raw],name,{type:mime});
  assert.equal(fileKind(file.name,file.type),'AUDIO');
  assert.deepEqual(await materialBytes(file,'','AUDIO'),raw);
 }
 assert.equal(fileKind('note.txt',''),'TEXT');
 assert.equal(fileKind('not-audio.wav','video/mp4'),'VIDEO');
 assert.equal(fileKind('not-audio.mp3','video/mp4'),'VIDEO');
 assert.equal(fileKind('not-audio.m4a','text/plain'),'TEXT');
});
test('file picker lists phone audio extensions even without MIME detection',async()=>{
 const html=await readFile(new URL('../public/index.html',import.meta.url),'utf8');
 const accept=html.match(/<input id="material-file"[^>]*\baccept="([^"]+)"/)?.[1]?.split(',');
 for(const extension of ['.wav','.mp3','.m4a'])assert.ok(accept?.includes(extension));
});
test('invalid text/image/backup inputs identify the actual correction',async()=>{
 await assert.rejects(materialBytes(new File([new Uint8Array([255,216,255])],'ảnh.jpg'),'', 'TEXT'),/UTF-8/);
 await assert.rejects(materialBytes(new File(['<svg/>'],'ảnh.svg'),'', 'IMAGE'),/PNG/);
 await assert.rejects(materialBytes(new File(['tiếng Việt'.normalize('NFD')],'text.txt'),'', 'TEXT'),/NFC/);
 await assert.rejects(materialBytes(new File(['{"format":"CREATOR_WEB_BACKUP_V1"}'],'backup.json'),'', 'TEXT'),/Khôi phục/);
});
test('oversize is rejected before reading or uploading',async()=>{
 let read=false;await assert.rejects(materialBytes({size:maxFileBytes+1,arrayBuffer(){read=true;}},'', 'TEXT'),/8 MiB/);assert.equal(read,false);
});
test('supported image signatures are recognized without calling them image quality proof',async()=>{
 for(const bytes of [[137,80,78,71,13,10,26,10],[255,216,255],[82,73,70,70,0,0,0,0,87,69,66,80]]){
  const raw=new Uint8Array(bytes);assert.deepEqual(await materialBytes(new File([raw],'format fixture'),'', 'IMAGE'),raw);
 }
});
