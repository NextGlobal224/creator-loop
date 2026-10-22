// Invoked only by a durable, reserved source job. No retries or alternate model.
export const AUDIO_MODEL='@cf/openai/whisper-large-v3-turbo';
export async function transcribeSource(binding,packetJSON,deployment){
 const packet=JSON.parse(packetJSON);
 if(!packet||typeof packet!=='object'||Array.isArray(packet)||typeof packet.request_id!=='string'||typeof packet.input_sha256!=='string'||typeof deployment!=='string'||!deployment.trim()||!/^[a-f0-9]{32}$/.test(packet.request_id)||!/^[a-f0-9]{64}$/.test(packet.input_sha256)||packet.model!==AUDIO_MODEL)throw Error('Invalid source transcription identity');
 if(typeof packet.audio!=='string'||packet.audio.length>11184812||!Number.isInteger(packet.duration_ms)||packet.duration_ms<1||packet.duration_ms>120000)throw Error('Unbounded source audio');
 if(packet.audio.length%4!==0||!/^[A-Za-z0-9+/]*={0,2}$/.test(packet.audio))throw Error('Invalid base64 audio');
 const binary=Uint8Array.from(atob(packet.audio),c=>c.charCodeAt(0));
 if(binary.length<44||binary.length>8*1024*1024)throw Error('Unbounded source audio');
 const hash=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',binary)),b=>b.toString(16).padStart(2,'0')).join('');
 if(hash!==packet.input_sha256)throw Error('Source bytes changed');
 let calls=0;
 try{
  calls++;
  const native=await binding.run(AUDIO_MODEL,{audio:packet.audio,language:'vi',task:'transcribe',vad_filter:false,condition_on_previous_text:false});
  return JSON.stringify({ok:true,model:AUDIO_MODEL,worker_version:deployment,request_id:packet.request_id,input_sha256:hash,model_calls:calls,native_response:native,native_usage:native?.usage??null});
 }catch(error){
  // Provider failure can still consume usage. The durable caller retains the
  // reservation and UNKNOWN request; it must not retry on a missing response.
  return JSON.stringify({ok:false,model:AUDIO_MODEL,worker_version:deployment,request_id:packet.request_id,input_sha256:hash,model_calls:calls,native_usage:null,unknown_outcome:true,error:'SOURCE_TRANSCRIPTION_OUTCOME_UNKNOWN'});
 }
}
