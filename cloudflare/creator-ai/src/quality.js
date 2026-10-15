// Model audit is evidence for review, never human approval or proof of truth.
export function auditSchema(articleHash, paragraphCount) {
  return {
    type: "object",
    properties: {
      article_sha256: {type: "string", const: articleHash},
      issues: {type: "array", items: {type: "string"}},
      paragraphs: {type: "array", minItems:paragraphCount,maxItems:paragraphCount,items: {
        type: "object",
        properties: {
          index: {type: "integer",enum:Array.from({length:paragraphCount},(_,i)=>i)},
          kind: {type: "string", enum: ["HYPOTHETICAL", "ADVICE", "SOURCE_FACT"]},
          factual_passages: {type: "array", items: {type: "string"}},
        },
        required: ["index", "kind", "factual_passages"],
        additionalProperties: false,
      }},
    },
    required: ["article_sha256", "issues", "paragraphs"],
    additionalProperties: false,
  };
}

export function bindAudit(body,articleHash,audit) {
  const paragraphs=body.split(/\n\s*\n/u).map(p=>p.trim()).filter(Boolean);
  if(!audit || Object.keys(audit).sort().join(",")!=="article_sha256,issues,paragraphs" || audit.article_sha256!==articleHash || !Array.isArray(audit.issues) || !Array.isArray(audit.paragraphs) || audit.paragraphs.length!==paragraphs.length) return null;
  if(audit.paragraphs.some((item,i)=>!item || Object.keys(item).sort().join(",")!=="factual_passages,index,kind" || item.index!==i)) return null;
  // Reattach the immutable original text; the model never rewrites/copies it.
  return {draft_body:body,issues:audit.issues,paragraphs:audit.paragraphs.map((item,i)=>({text:paragraphs[i],kind:item.kind,factual_passages:item.factual_passages}))};
}

export function hasHypotheticalOpening(body) {
  return /^(?:Giả sử(?:\s|[.,:;!?])|Tình huống giả định:)/iu.test(body.trim());
}

export function compileEditedArticle(edited) {
  if (!edited || Object.keys(edited).sort().join(",")!=="editorial_paragraphs,hypothetical_opening" ||
      typeof edited.hypothetical_opening!=="string" || !edited.hypothetical_opening.trim() ||
      edited.hypothetical_opening.includes("\n") || !Array.isArray(edited.editorial_paragraphs) ||
      edited.editorial_paragraphs.length!==4 || edited.editorial_paragraphs.some(p=>typeof p!=="string" || !p.trim() || p.includes("\n"))) {
    throw new Error("Invalid typed editorial structure");
  }
  // Label the explicitly commissioned fictional scene at composition time.
  // This does not label the four editorial paragraphs or license facts in them.
  const opening=edited.hypothetical_opening.trim();
  return [/^Tình huống giả định:/iu.test(opening)?opening:"Tình huống giả định: "+opening,...edited.editorial_paragraphs.map(p=>p.trim())].join("\n\n");
}

// Citations travel with the exact text being composed, not with a second
// model-written copy of a sentence which can drift away from the article.
export function quoteCatalog(materials) {
  return materials.flatMap(m=>m.text.split(/\r?\n/u).map(text=>text.trim()).filter(Boolean).map(source_quote=>({material_id:m.material_id,source_quote})));
}

export function compileSourcedArticle(value, catalog) {
  if (!value || Object.keys(value).sort().join(",")!=="editorial_paragraphs,hypothetical_opening" ||
      !Array.isArray(value.editorial_paragraphs) || value.editorial_paragraphs.length!==4) throw new Error("Invalid sourced article");
  const claims=[];
  const paragraphs=value.editorial_paragraphs.map(segments=>{
    if (!Array.isArray(segments) || !segments.length || segments.length>12) throw new Error("Invalid segments");
    return segments.map(segment=>{
      if (!segment || Object.keys(segment).sort().join(",")!=="source_quote_id,text" ||
          typeof segment.text!=="string" || !segment.text.trim() || segment.text.includes("\n") ||
          !Number.isInteger(segment.source_quote_id) || segment.source_quote_id < -1 ||
          segment.source_quote_id>=catalog.length) throw new Error("Invalid source segment");
      if (segment.source_quote_id>=0) {
        const quote=catalog[segment.source_quote_id];
        claims.push({text:segment.text,material_ids:[quote.material_id],source_quote:quote.source_quote});
      }
      return segment.text;
    }).join("");
  });
  const body=compileEditedArticle({hypothetical_opening:value.hypothetical_opening,editorial_paragraphs:paragraphs});
  if (claims.length>30 || claims.some(c=>body.split(c.text).length!==2)) throw new Error("Ambiguous cited passage");
  return {body,claims};
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
