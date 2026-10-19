// Web-only post-normalization schema constraint. No output is repaired.
export function constrainRewrites(schema,initial){
 const choices=schema.properties?.edits?.items?.oneOf;
 if(!Array.isArray(choices)||!initial?.segments?.length)throw Error('Expected expanded edit schema');
 const covered=new Set();
 for(const branch of choices){
  const fields=branch.properties,index=fields?.index?.const;
  if(!Number.isInteger(index)||!initial.segments[index]||!['KEEP','REWRITE','REMOVE'].includes(fields?.decision?.const))throw Error('Unexpected edit branch');
  if(fields.decision.const!=='REWRITE')continue;
  if(!['index','premise_checks','support','quote_ids','decision','note','replacement'].every(k=>branch.required?.includes(k)))throw Error('Expanded edit receipt missing');
  const items=fields.replacement?.items?.oneOf;
  if(!Array.isArray(items)||!items.length)throw Error('Expanded replacement choices missing');
  for(const item of items){
   if(item.properties?.text?.type!=='string'||!item.required?.includes('text'))throw Error('Replacement text contract missing');
   item.properties.text={...item.properties.text,not:{const:initial.segments[index].text}};
  }
  covered.add(index);
 }
 if(covered.size!==initial.segments.length)throw Error('Incomplete rewrite schema coverage');
}
