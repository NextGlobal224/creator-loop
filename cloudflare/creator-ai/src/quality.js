// Model audit is evidence for review, never human approval or proof of truth.
export function auditSchema(body) {
  return {
    type: "object",
    properties: {
      draft_body: {type: "string", const: body},
      issues: {type: "array", items: {type: "string"}},
      paragraphs: {type: "array", items: {
        type: "object",
        properties: {
          text: {type: "string"},
          kind: {type: "string", enum: ["HYPOTHETICAL", "ADVICE", "SOURCE_FACT"]},
          factual_passages: {type: "array", items: {type: "string"}},
        },
        required: ["text", "kind", "factual_passages"],
        additionalProperties: false,
      }},
    },
    required: ["draft_body", "issues", "paragraphs"],
    additionalProperties: false,
  };
}

export function hasHypotheticalOpening(body) {
  return /^Giả sử(?:\s|[.,:;!?])/iu.test(body.trim());
}

export function validAudit(annotation, audit) {
  const body = annotation.draft_body;
  const paragraphs = body.split(/\n\s*\n/u).map(p=>p.trim()).filter(Boolean);
  if (!audit || Object.keys(audit).sort().join(",")!=="draft_body,issues,paragraphs" ||
      audit.draft_body!==body || !Array.isArray(audit.issues) || audit.issues.length ||
      !Array.isArray(audit.paragraphs) || audit.paragraphs.length!==paragraphs.length ||
      !Array.isArray(annotation.factual_claims)) return false;
  return audit.paragraphs.every((item,i)=>{
    if (!item || Object.keys(item).sort().join(",")!=="factual_passages,kind,text" ||
        item.text!==paragraphs[i] || !["HYPOTHETICAL","ADVICE","SOURCE_FACT"].includes(item.kind) ||
        (i===0 && item.kind!=="HYPOTHETICAL") || !Array.isArray(item.factual_passages) ||
        (item.kind==="SOURCE_FACT" && !item.factual_passages.length)) return false;
    return item.factual_passages.every(fact=>typeof fact==="string" && fact.trim() &&
      item.text.split(fact).length===2 && annotation.factual_claims.some(claim=>typeof claim.text==="string" && claim.text.includes(fact)));
  });
}
