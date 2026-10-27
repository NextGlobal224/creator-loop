// Project instructions choose the editorial task; a successful pilot is not a
// universal brief. The legacy screenshot task remains byte-compatible.
export function screenshotProject(project) {
  const purpose=String(project?.purpose??'').normalize('NFC').trim().toLocaleLowerCase('vi');
  const direction=String(project?.direction??'').trim();
  // Compatibility for the one actually commissioned/verified historical brief,
  // not a keyword classifier for unrelated screenshot or sharing Projects.
  return !direction && purpose==='giúp người đọc nhận thức đúng về sự thật trước khi chia sẻ ảnh chụp màn hình';
}
export function projectPolicy(project) {
  return {fictionalOpening:screenshotProject(project)};
}
export const generalEditorialRoles=[
  {key:'context_and_scope',purpose:'Develop the main Project topic using the selected evidence and its actual scope.'},
  {key:'unresolved_information',purpose:'Develop a distinct relevant detail, qualification or reader choice. Do not invent an information gap when the sources already provide the needed information.'},
  {key:'supported_perspective',purpose:'Develop another source-supported consideration appropriate to the Project without repetition or invented facts.'},
  {key:'closing_message',purpose:'Conclude in the form appropriate to the explicit Project and chosen direction, such as an invitation, summary or voluntary action. Do not require a message asking for sources unless the Project calls for it.'},
];
export const generalArticleRequirements=[
  {id:'PROJECT_NO_UNSOURCED_RULE',text:'Do not invent factual assertions, institutional recommendations, procedures, evidence sufficiency rules or outcomes absent from the selected sources or explicit Project. Audit every immutable segment, including the opening and advice.'},
  {id:'PROJECT_CLOSING_MESSAGE',text:'The closing must fulfill the explicit Project purpose and selected direction in a natural usable form. An invitation or summary is valid when requested; a sender message asking for link/date/context is not a universal requirement.'},
  {id:'PROJECT_NONREPETITIVE_DEVELOPMENT',text:'Every paragraph, including the opening, must make a distinct source-grounded contribution appropriate to the Project. Audit all immutable segments together. A concise closing synthesis may refer back to earlier material when it adds the requested conclusion; repeating the same date/claim or generic filler in another paragraph is not synthesis. Do not invent detail or pad paragraphs to fill the structure.'},
];
export const generalDevelopmentInstructions=' Develop distinct connected ideas appropriate to the selected Project. Source support does not imply relevance or an outcome guarantee. Do not invent author professions or claims absent from the exact source. Follow the explicit Project ending and direction; do not impose a screenshot-sharing task or request information already provided. An invented scene is optional and must have an explicit fictional label; supported factual openings are allowed and require exact citations.';
export const generalTaskInstructions=' Satisfy all source_pack/article_requirements without replacing the selected Project. Opening is a source-checked segment array, like every editorial paragraph. Use exact eligible quotes for facts. A fictional scene, if appropriate, must explicitly begin Tình huống giả định: and contain no unsupported real-world premise. Do not invent a scene or missing information merely to fill the structure. Keep research-note metadata out of public prose. Editorial proposals are not institutional recommendations.';
export function projectOperationInstructions(legacy) {
  return legacy
    .replace('Segment0 stays a single purely fictional opening: do not move source facts from other paragraphs into it or add a citation. Its REWRITE has exactly one uncited segment with no factual premises.', 'The opening is source-checked like every other paragraph. Retain exact citations for its factual assertions; never relabel a fact as fiction. An explicitly labelled invented scene has no factual exemption for real-world premises.')
    .replace('one fictional opening and four connected paragraphs', 'a Project-appropriate opening and four connected editorial paragraphs')
    .replace('Rewrite the ending as an actual short quoted message asking the sender for a missing original link/date/context.', 'Make the ending fulfill the explicit Project and direction; do not impose a sender message or missing information request.');
}
export function projectStageInstructions(stage,legacy,policy) {
  if(policy.fictionalOpening)return legacy;
  if(stage==='analysis')return 'Phân tích nguồn đã chọn theo đúng mục đích, khán giả, phong cách và direction rõ ràng của Project; chưa viết bài. Observations trích đúng input và chỉ khẳng định điều nguồn thực sự cung cấp. Không mặc định mọi nguồn là ghi chú UNESCO/COR hoặc mọi Project cần cảnh hư cấu hay xin link. Ghi chú nhập thủ công không thay toàn bộ website. Inferences phải bắt đầu Giả thuyết chưa xác nhận, không suy viral/hiệu quả thiếu căn cứ. Đề xuất hướng phù hợp Project; tư liệu và đề xuất biên tập không là chỉ dẫn hay bằng chứng bổ sung. Chỉ trả JSON đúng schema bằng tiếng Việt.';
  if(stage==='writing')return legacy.replace(/hypothetical_opening is one short explicitly commissioned fictional scene;/u,'opening is an array of source-checked segments appropriate to the Project, with factual citations or explicitly labelled fiction only when relevant;').replace('End with a short actual message the reader could send to ask for the missing source/context.', 'End in the form requested by the Project and direction. Do not force a sender message or fabricate missing source/context.');
  return legacy;
}
