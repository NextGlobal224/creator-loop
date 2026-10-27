// Decode original input on Cloudflare Browser Run; never on the user's browser.
// No inference, network source URL, retry or publication permission here.
export const MAX_VIDEO_MS=30000;
export const MAX_VIDEO_BYTES=8*1024*1024;
export const MAX_BROWSER_SECONDS=60;

export async function videoIdentity(base64,expected){
 if(typeof base64!=='string'||base64.length>11184812||base64.length%4||!/^[A-Za-z0-9+/]*={0,2}$/.test(base64)||!/^[a-f0-9]{64}$/.test(expected))throw Error('Invalid bounded video');
 const bytes=Uint8Array.from(atob(base64),c=>c.charCodeAt(0));
 if(bytes.length<16||bytes.length>MAX_VIDEO_BYTES)throw Error('Video exceeds input limit');
 const mp4=String.fromCharCode(...bytes.slice(4,8))==='ftyp',webm=[26,69,223,163].every((v,i)=>bytes[i]===v);
 if(!mp4&&!webm)throw Error('Only MP4/WebM decoding is supported');
 const sha=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',bytes)),b=>b.toString(16).padStart(2,'0')).join('');
 if(sha!==expected)throw Error('Original video bytes changed');
 return {mime:mp4?'video/mp4':'video/webm',sha256:sha,bytes:bytes.length};
}

// This function is serialized into the isolated cloud browser, never to Web UI.
export async function decodeInBrowser(original,mime){
 const bytes=Uint8Array.from(atob(original),c=>c.charCodeAt(0));
 const url=URL.createObjectURL(new Blob([bytes],{type:mime}));
 const video=document.createElement('video');video.muted=true;video.preload='auto';
 const bounded=(promise,ms=12000)=>new Promise((resolve,reject)=>{const timer=setTimeout(()=>reject(Error('Cloud decoder timed out')),ms);Promise.resolve(promise).then(v=>{clearTimeout(timer);resolve(v);},e=>{clearTimeout(timer);reject(e);});});
 const once=event=>new Promise((resolve,reject)=>{const done=()=>{clean();resolve();},fail=()=>{clean();reject(Error('Unsupported or corrupt video'));},clean=()=>{video.removeEventListener(event,done);video.removeEventListener('error',fail);};video.addEventListener(event,done,{once:true});video.addEventListener('error',fail,{once:true});});
 const b64=raw=>{let s='';for(let i=0;i<raw.length;i+=8192)s+=String.fromCharCode(...raw.subarray(i,i+8192));return btoa(s);};
 try{
  const loaded=once('loadeddata');video.src=url;await bounded(loaded);
  if(!Number.isFinite(video.duration)||video.duration<=0||video.duration>30||!video.videoWidth||!video.videoHeight||video.videoWidth*video.videoHeight>8294400)throw Error('Video needs finite duration <=30s and <=4K dimensions');
  const duration_ms=Math.ceil(video.duration*1000);
  const audioContext=new OfflineAudioContext(1,1,16000);
  const decoded=await bounded(audioContext.decodeAudioData(bytes.buffer.slice(0)));
  if(decoded.duration<=0||Math.abs(decoded.duration-video.duration)>0.25||decoded.length>480000)throw Error('Audio/video duration mismatch or unreadable audio track');
  const pcm=new Uint8Array(44+decoded.length*2),view=new DataView(pcm.buffer);
  const ascii=(at,s)=>{for(let i=0;i<s.length;i++)pcm[at+i]=s.charCodeAt(i);};
  ascii(0,'RIFF');view.setUint32(4,pcm.length-8,true);ascii(8,'WAVEfmt ');view.setUint32(16,16,true);view.setUint16(20,1,true);view.setUint16(22,1,true);view.setUint32(24,16000,true);view.setUint32(28,32000,true);view.setUint16(32,2,true);view.setUint16(34,16,true);ascii(36,'data');view.setUint32(40,decoded.length*2,true);
  for(let i=0;i<decoded.length;i++){let sample=0;for(let c=0;c<decoded.numberOfChannels;c++)sample+=decoded.getChannelData(c)[i]/decoded.numberOfChannels;if(!Number.isFinite(sample))throw Error('Nonfinite audio sample');sample=Math.max(-1,Math.min(1,sample));view.setInt16(44+i*2,sample<0?Math.round(sample*32768):Math.round(sample*32767),true);}
  const canvas=document.createElement('canvas'),scale=Math.min(1,480/video.videoWidth,360/video.videoHeight);canvas.width=Math.max(1,Math.round(video.videoWidth*scale));canvas.height=Math.max(1,Math.round(video.videoHeight*scale));
  const ctx=canvas.getContext('2d'),frames=[];let total=0;
  // Explicitly sampled observations, not a claim to have read every frame.
  for(let at_ms=0;at_ms<duration_ms;at_ms+=2000){
   if(at_ms>0){const seek=once('seeked');video.currentTime=at_ms/1000;await bounded(seek,3000);}
   ctx.drawImage(video,0,0,canvas.width,canvas.height);const image=canvas.toDataURL('image/jpeg',0.65).split(',')[1];total+=image.length;
   if(total>1398104||frames.length>=15)throw Error('Sampled video frames exceed bounded payload');
   frames.push({at_ms,end_ms:Math.min(duration_ms,at_ms+1),image});
  }
  return {duration_ms,audio_duration_ms:Math.ceil(decoded.length*1000/16000),sample_rate:16000,audio:b64(pcm),frames,width:video.videoWidth,height:video.videoHeight,visual_sampling_ms:2000,unread:['Hình ảnh chỉ được lấy mẫu mỗi 2 giây; các khung hình giữa các mốc chưa được đọc.'],decoder:'Cloudflare Browser Run / HTMLMediaElement + OfflineAudioContext'};
 }finally{video.pause();video.removeAttribute('src');video.load();URL.revokeObjectURL(url);}
}

let nextCommandId=0;
function command(socket,method,params={},deadline){
 const id=++nextCommandId;return new Promise((resolve,reject)=>{
  const cleanup=()=>{clearTimeout(timer);socket.removeEventListener('message',message);socket.removeEventListener('close',closed);};
  const closed=()=>{cleanup();reject(Error('Cloud decoder connection closed'));};
  const message=event=>{let r;try{r=JSON.parse(event.data);}catch{return;}if(r.id!==id)return;cleanup();r.error?reject(Error('Cloud decoder protocol error')):resolve(r.result);};
  const timer=setTimeout(()=>{cleanup();reject(Error('Cloud decoder deadline'));},Math.max(1,Math.min(method==='Runtime.evaluate'?55000:15000,deadline-Date.now())));
  socket.addEventListener('message',message);socket.addEventListener('close',closed);try{socket.send(JSON.stringify({id,method,params}));}catch{closed();}
 });
}

// Bound the caller's wait, not the provider's session lifetime. A timed-out
// launch/close remains UNKNOWN and holds its reservation. Late owned sessions
// are closed once if their launch eventually settles; no second launch/retry.
function until(promise,deadline,late){
 let expired=false,timer;
 const observed=Promise.resolve(promise).then(value=>{
  if(expired&&late)Promise.resolve().then(()=>late(value)).catch(()=>{});
  return value;
 });
 return Promise.race([observed,new Promise((_,reject)=>{
  timer=setTimeout(()=>{expired=true;reject(Error('Cloud decoder lifecycle deadline'));},Math.max(0,deadline-Date.now()));
 })]).finally(()=>clearTimeout(timer));
}

// Timing overrides exist only for controlled lifecycle tests, never in packets.
export async function ownedVideoSession(binding,use,{workMs=55000,cleanupMs=5000}={}){
 const started=Date.now(),deadline=started+workMs,finishBy=deadline+cleanupMs;
 const receipt={phase:'LAUNCH',launch_outcome:'UNKNOWN',session_id:null,close_status:'UNKNOWN',wait_budget_ms:workMs+cleanupMs};
 let session,socket,closing,value,failed=false;
 const closeSocket=s=>{try{s?.close();}catch{}};
 const closeOwned=id=>{
  if(!closing)closing=until(Promise.resolve().then(()=>binding.closeSession(id)),Math.min(finishBy,Date.now()+cleanupMs))
   .then(r=>{const status=typeof r==='string'?r:r?.status;return status==='closed'?'CLOSED':status==='closing'?'CLOSING':'UNKNOWN';},()=> 'UNKNOWN');
  return closing;
 };
 try{
  // The deadline is armed BEFORE calling launch. Observe late rejection too.
  session=await until(Promise.resolve().then(()=>binding.launch({keepAlive:10000,recording:false,targets:true})),deadline,
   late=>late?.sessionId?closeOwned(late.sessionId):undefined);
  receipt.launch_outcome='RECEIVED';receipt.session_id=session?.sessionId??null;
  if(!session?.sessionId)throw Error('Cloud browser session identity missing');
  const page=session.targets?.find(t=>t.type==='page');
  if(!page?.id)throw Error('Cloud decoder page missing');
  receipt.phase='CONNECT';
  const connection=await until(Promise.resolve().then(()=>binding.connectSession(session.sessionId,{targetId:page.id})),deadline);
  receipt.phase='UPGRADE';
  const response=await until(Promise.resolve().then(()=>connection.webSocket.fetch('https://browser-binding.invalid',{headers:{Upgrade:'websocket'}})),deadline,
   late=>closeSocket(late?.webSocket));
  socket=response.webSocket;if(!socket)throw Error('Cloud decoder socket missing');socket.accept();
  receipt.phase='DECODE';value=await until(Promise.resolve().then(()=>use(socket,deadline)),deadline);
 }catch{failed=true;}
 finally{
  closeSocket(socket);
  if(session?.sessionId)receipt.close_status=await closeOwned(session.sessionId);
 }
 receipt.elapsed_ms=Date.now()-started;
 // A close request accepted as 'closing' is NOT confirmed closed. Never start
 // inference or release Browser Run reservations on that evidence alone.
 return {ok:!failed&&receipt.close_status==='CLOSED',...(failed?{}:{value}),browser_lifecycle:receipt};
}

export async function decodeSourceVideo(binding,packetJSON){
 const p=JSON.parse(packetJSON),identity=await videoIdentity(p.video,p.input_sha256);
 if(p.browser_reserved_seconds!==MAX_BROWSER_SECONDS)throw Error('Missing bounded included browser reservation');
 if(!binding||typeof binding.launch!=='function')throw Error('Cloud video decoder binding unavailable');
 const result=await ownedVideoSession(binding,async(socket,deadline)=>{
  await command(socket,'Network.enable',{},deadline);
  await command(socket,'Network.setBlockedURLs',{urls:['http://*','https://*','ws://*','wss://*']},deadline);
  const expression='('+decodeInBrowser.toString()+')('+JSON.stringify(p.video)+','+JSON.stringify(identity.mime)+')';
  const r=await command(socket,'Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true},deadline);
  if(r.exceptionDetails||!r.result?.value)throw Error('Cloud video cannot be decoded within supported bounds');
  return r.result.value;
 });
 return JSON.stringify({ok:result.ok,browser_lifecycle:result.browser_lifecycle,
  ...(result.ok?{...result.value,input_sha256:identity.sha256,input_bytes:identity.bytes,browser_session_id:result.browser_lifecycle.session_id,browser_reserved_seconds:MAX_BROWSER_SECONDS}:{error:'VIDEO_DECODER_UNCONFIRMED',unknown_outcome:true})});
}
