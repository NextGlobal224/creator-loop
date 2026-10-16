import {generateText, jsonSchema, Output, stepCountIs} from "ai";
import {createWorkersAI} from "workers-ai-provider";

export const SDK_COMPONENT = "workers-ai-provider@4.0.0/ai@7.0.142";

// Direct binding only: no REST shim, gateway, fallback, tools or stream retry.
// The counter is also a fail-closed boundary if an SDK/provider ever adds calls.
export async function generateStage(env, {instructions, input, schema, budget, temperature, signal}, observe) {
  let calls=0;
  const binding={run:async(model, inputs, options)=>{
    if(++calls!==1 || model!==env.CREATOR_MODEL || inputs.stream===true) throw new Error("Unexpected SDK model call");
    const result=await env.AI.run(model,inputs,options);
    observe(result);
    return result;
  }};
  const provider=createWorkersAI({binding});
  const settings=env.CREATOR_MODEL.startsWith("@cf/openai/gpt-oss-")?{reasoning_effort:"low"}:
    env.CREATOR_MODEL==="@cf/qwen/qwen3-30b-a3b-fp8"?{chat_template_kwargs:{enable_thinking:false}}:{};
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
}
