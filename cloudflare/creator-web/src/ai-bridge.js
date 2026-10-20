import nativeTextWorker from "../../creator-ai/src/index.js";
import {formatStage} from "./format-stages.js";
import {SDK_COMPONENT} from "../../creator-ai/src/sdk-stage.js";
import {analysisProfile} from './analysis-audit.js';

export function components(){return JSON.stringify({sdk:SDK_COMPONENT,formats:["TEXT","IMAGE_CAPTION","VIDEO"],protocol:"immutable-source-audit"});}

// Reuse TEXT's reviewed source-first/edit-operation/immutable-audit protocol
// in-process. The Windows-pinned Worker deployment remains unchanged.
export async function stage(binding, model, packetJson, version) {
  const packet=JSON.parse(packetJson);let calls=0,nativeUsage=null;
  const profile=await analysisProfile(packet);
  const counted={async run(...args){if(++calls>1)throw Error("One model call per declared stage");return profile.run({run:async(...nativeArgs)=>{const result=await binding.run(...nativeArgs);nativeUsage=result?.usage??null;return result;}},...args);}};
  const env={AI:counted,CREATOR_MODEL:model,CREATOR_VERSION:{id:version},CREATOR_KEY:"internal-ffi-only"};
  let envelope;
  if(packet.format==="TEXT"){
    const {format,...body}=packet;
    const response=await nativeTextWorker.fetch(new Request("https://internal.invalid/b/generate",{
      method:"POST",headers:{"content-type":"application/json",authorization:"Bearer internal-ffi-only"},body:JSON.stringify(body),
    }),env);
    envelope=await response.json();envelope.ok=response.ok;
  }else{
    try{envelope={...await formatStage(env,packet),ok:true};}
    catch(error){envelope={ok:false,error:"FORMAT_STAGE_REJECTED",stage:packet.stage,retryable:false};}
  }
  return JSON.stringify({...envelope,analysis_audit:profile.receipt(),profile_diagnostic:profile.diagnostic(),...(envelope.ok?{}:{profile_rejected_json:profile.rejectedJSON(),profile_rejected_final_text:profile.rejectedFinalText()}),model_calls:calls,native_usage:nativeUsage});
}
