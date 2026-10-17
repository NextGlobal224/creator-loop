import {generateText, jsonSchema, Output, stepCountIs} from "ai";
import {createWorkersAI} from "workers-ai-provider";

export const SDK_COMPONENT = "workers-ai-provider@4.0.0/ai@7.0.142";

// The native binding may ignore the SDK's abort signal. Bound our wait too;
// this does not promise cancellation of already submitted remote inference.
async function abortableBinding(invoke, signal) {
  if(!signal) return invoke();
  if(signal.aborted) throw new DOMException("SDK stage aborted", "AbortError");
  let onAbort;
  const aborted=new Promise((_,reject)=>{
    onAbort=()=>reject(new DOMException("SDK stage aborted", "AbortError"));
    signal.addEventListener("abort",onAbort,{once:true});
  });
  try { return await Promise.race([Promise.resolve().then(()=>{
    if(signal.aborted) throw new DOMException("SDK stage aborted", "AbortError");
    return invoke();
  }),aborted]); }
  finally { signal.removeEventListener("abort",onAbort); }
}

// Native JSON decoding rejects uniqueItems (observed native8007). Keep the full
// schema in the SDK/prompt; app revisionIssues/bindAudit enforce uniqueness.
// Adapt only the binding schema; no output is repaired or silently deduplicated.
function nativeDecodingSchema(schema) {
  if(!schema || typeof schema!=="object" || Array.isArray(schema)) return schema;
  const result={...schema};delete result.uniqueItems;
  for(const key of ["properties","patternProperties","$defs","definitions"])
    if(result[key]) result[key]=Object.fromEntries(Object.entries(result[key]).map(([name,value])=>[name,nativeDecodingSchema(value)]));
  for(const key of ["items","additionalProperties","not","if","then","else","contains"])
    if(result[key] && !Array.isArray(result[key])) result[key]=nativeDecodingSchema(result[key]);
  for(const key of ["allOf","anyOf","oneOf","prefixItems"])
    if(Array.isArray(result[key])) result[key]=result[key].map(nativeDecodingSchema);
  // Actual ce273a1 decoding emitted only index/decision/replacement from a
  // partial oneOf branch, omitting the parent object's required receipts.
  // Expand only our flat const/array-bound alternatives into complete objects.
  // This preserves the intersection, unlike replacing the parent with a branch.
  if(result.type==="object" && result.properties && Array.isArray(result.oneOf) &&
     result.oneOf.every(branch=>branch && Object.keys(branch).every(key=>["properties","required"].includes(key)) && branch.properties &&
       Object.entries(branch.properties).every(([name,constraints])=>Object.hasOwn(result.properties,name) &&
         Object.keys(constraints).every(key=>["const","minItems","maxItems"].includes(key))))){
    const base={...result};delete base.oneOf;
    return {oneOf:result.oneOf.map(branch=>({...base,properties:Object.fromEntries(Object.entries(base.properties).map(([name,property])=>{
      const constraints=branch.properties[name]??{},merged={...property,...constraints};
      for(const [key,combine] of [["minItems",Math.max],["maxItems",Math.min]])
        if(property[key]!==undefined && constraints[key]!==undefined)merged[key]=combine(property[key],constraints[key]);
      return [name,merged];
    })),required:[...new Set([...(base.required??[]),...(branch.required??[])])]}))};
  }
  return result;
}


// Direct binding only: no REST shim, gateway, fallback, tools or stream retry.
// The counter is also a fail-closed boundary if an SDK/provider ever adds calls.
export async function generateStage(env, {instructions, input, schema, budget, temperature, signal, reasoningEffort="low"}, observe) {
  if(!["low","medium","high"].includes(reasoningEffort)) throw new Error("Invalid reasoning effort");
  let calls=0,received=false,nativeFailure=null;
  const binding={run:async(model, inputs, options)=>{
    if(++calls!==1 || model!==env.CREATOR_MODEL || inputs.stream===true) throw new Error("Unexpected SDK model call");
    let result;
    const nativeInputs=inputs.response_format?.type==="json_schema"?
      {...inputs,response_format:{...inputs.response_format,json_schema:nativeDecodingSchema(inputs.response_format.json_schema)}}:inputs;
    try { result=await abortableBinding(()=>env.AI.run(model,nativeInputs,options),signal); }
    catch(error) {
      if(signal?.aborted) throw error;
      // Classify the native exception before SDK wrapping; never export its text.
      const message=String(error?.message ?? "");
      const prefix=message.match(/^(?:[A-Za-z]+Error:\s*)?(\d{4,5}):/u);
      const terms={SCHEMA:/schema/iu,UNIQUE_ITEMS:/uniqueItems/iu,MIN_ITEMS:/minItems/iu,
        MAX_ITEMS:/maxItems/iu,CONSTRAINT:/constraint/iu,UNSUPPORTED:/unsupported|not supported/iu,
        VALIDATION:/validat/iu,JSON:/json/iu,ARRAY:/array/iu,INTEGER:/integer/iu,
        TIMEOUT:/timed? ?out|timeout/iu,QUOTA:/quota|daily|allocation/iu,
        RATE_LIMIT:/rate.?limit/iu,CAPACITY:/capacity/iu,AUTH:/auth/iu,
        MEMORY:/memory/iu,MAX_TOKENS:/max.?tokens/iu,INFERENCE:/inference/iu,
        UPSTREAM:/upstream/iu,INTERNAL:/internal/iu};
      nativeFailure={code:prefix?Number(prefix[1]):null,flags:Object.entries(terms).filter(([,pattern])=>pattern.test(message)).map(([flag])=>flag)};
      throw error;
    }
    received=true;
    observe(result);
    return result;
  }};
  const provider=createWorkersAI({binding});
  const settings=env.CREATOR_MODEL.startsWith("@cf/openai/gpt-oss-")?{reasoning_effort:reasoningEffort}:
    env.CREATOR_MODEL==="@cf/qwen/qwen3-30b-a3b-fp8"?{chat_template_kwargs:{enable_thinking:false}}:{};
  try {
    const result=await generateText({
      model:provider(env.CREATOR_MODEL,settings),
      system:instructions,
      prompt:input+"\nReturn only JSON matching this schema:\n"+JSON.stringify(schema),
      output:Output.object({schema:jsonSchema(schema)}),
      maxOutputTokens:budget,temperature,maxRetries:0,stopWhen:stepCountIs(1),
      abortSignal:signal,
      experimental_telemetry:{isEnabled:false},
    });
    if(calls!==1 || result.steps.length!==1 || result.toolCalls.length || result.finishReason!=="stop") throw new Error("Incomplete SDK stage");
    // SDK output parsing must not silently repair/extract a fenced/malformed reply.
    const strict=JSON.parse(result.text);
    if(JSON.stringify(strict)!==JSON.stringify(result.output)) throw new Error("SDK changed stage JSON");
    return {value:result.output,text:result.text,calls};
  } catch(error) {
    // Fixed operational fields only; never include prompts, exception text or reasoning.
    const failure=error instanceof Error?error:new Error("SDK stage failed");
    failure.stage_diagnostic={native_failure:nativeFailure,phase:calls===0?"SDK_BEFORE_BINDING":received?"SDK_AFTER_BINDING":"NATIVE_BINDING",binding_calls:calls,...(signal?.aborted?{signal_aborted:true}:{})};
    throw failure;
  }
}
