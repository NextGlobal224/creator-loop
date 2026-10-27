// Web-only constrained-decoder shape. SDK/domain validation retains the full
// source-discriminated schema. No response is repaired or given a positive audit.
import {sourcedSegmentSchema,factualQuoteIds} from '../../creator-ai/src/grounding.js';

export function writingDecodingSchema(schema,catalog){
 const expected=sourcedSegmentSchema(catalog),ids=factualQuoteIds(catalog);
 if(!ids.length)throw Error('Writing requires factual source excerpts');
 const result=structuredClone(schema),roles=result.properties?.editorial_paragraphs?.properties;
 if(!roles||Object.keys(roles).length!==4)throw Error('Unexpected writing decoder roles');
 const arrays=[...Object.values(roles)];
 if(result.properties.opening)arrays.push(result.properties.opening);
 else if(result.properties.hypothetical_opening?.type!=='string')throw Error('Unexpected writing decoder opening');
 for(const array of arrays){
  if(array.type!=='array'||JSON.stringify(array.items)!==JSON.stringify(expected))throw Error('Unexpected source segment schema');
  // The observed failure stalled inside a nested source branch's quote_ids
  // until the unchanged output cap. Avoid repeating the full object per ID in
  // native grammar. Cross-field source/receipt constraints stay mandatory in
  // checkedWriting/checkedSegment and the SDK's original schema after return.
  const factual=structuredClone(expected.oneOf[1]);
  factual.properties.source_quote_id={type:'integer',enum:[-1,...ids]};
  factual.properties.premise_checks.minItems=0;
  factual.properties.premise_checks.items.properties.quote_ids.items={type:'integer',enum:ids};
  array.items=factual;
 }
 return result;
}
