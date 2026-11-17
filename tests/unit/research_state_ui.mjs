import assert from "node:assert/strict";
import fs from "node:fs";

const html = fs.readFileSync("static/research.html", "utf8");
const js = fs.readFileSync("static/research_state.js", "utf8");
const main = fs.readFileSync("static/research.js", "utf8");

for (const id of [
  "homeView", "nextStepPanel", "nextStepAction", "researchInventory", "onlineModelList", "localComponentList", "refreshLocalComponents", "projectStateForm", "researchQuestion", "nextAction", "stateEntryForm",
  "stateEntryKind", "stateEntryAbbreviation", "confirmedConclusions", "openQuestions", "evidenceGaps", "todoList", "termsList",
  "experimentForm", "experimentDataVersion", "experimentCodeVersion", "experimentCards", "generationProvider", "generationModel",
  "reviewStateList", "synthesisForm", "synthesisDocuments", "synthesisProvider",
  "synthesisModel", "synthesisResult", "synthesisSources", "synthesisCoverage", "saveSynthesisNote", "addOpenQuestion",
]) {
  assert.match(html, new RegExp(`id="${id}"`));
}
assert.match(html, /research_state\.js/);
assert.match(js, /window\.researchLoadState = loadState/);
assert.match(js, /expected_version: experiment\.version/);
assert.match(js, /kind: "open_question"/);
assert.match(js, /provider: \$\("synthesisProvider"\)\.value/);
assert.match(js, /status === "awaiting_outbound_approval"/);
assert.match(js, /未自动修改基线/);
assert.match(js, /尚未进入正式检索/);
assert.match(main, /home: \["持续研究", "课题首页"/);
assert.match(main, /window\.researchShowApproval = showApproval/);
assert.match(main, /window\.researchRefreshPendingCount = renderDocumentList/);
assert.match(html, /id="projectMenuOptions" class="project-menu-options" role="listbox"/);
assert.match(main, /function openProjectMenu\(\)/);
assert.match(main, /event\.key === "Escape"/);
assert.match(main, /function renderModelCenter\(\)/);
assert.match(js, /function renderNextStep\(\)/);
assert.match(js, /summary\.processing/);
assert.match(js, /summary\.actionRequired/);
assert.match(js, /系统正在处理/);
assert.match(main, /processing: processing\.length, actionRequired: actionRequired\.length, ready: actionable\.length/);
assert.match(main, /window\.researchSwitchView = switchView/);
assert.match(js, /window\.researchRefreshPendingCount\?\.\(\)/);
assert.match(html, /id="synthesisRisk" class="warning" hidden>请复核，存在幻觉风险/);
assert.match(js, /\$\("synthesisRisk"\)\.hidden = !synthesisResult\.summary/);
assert.match(js, /stateLoadSequence/);
assert.match(js, /button\.disabled = true;\n  try \{\n    await api\(`\/api\/research\/synthesis\//);
assert.match(js, /window\.researchRefreshDocuments\?\.\(\)/);
assert.match(js, /experiment\.result_source_refs/);
assert.match(js, /projectState\.todo/);
assert.match(main, /发送规模/);
assert.match(html, /id="localModeEnabled" type="checkbox" role="switch"/);
assert.match(html, /从 arXiv 导入论文/);
assert.match(html, /从 GitHub 导入仓库资料/);
assert.doesNotMatch(html, /从 arXiv 或 GitHub 添加公开资料/);
assert.match(main, /local_mode_disabled/);
assert.match(main, /localGeneration\.disabled = !generationReady/);
assert.match(main, /option\.disabled = !mode\.l3_available/);
assert.match(main, /window\.researchAppendCitation = appendCitation/);
assert.doesNotMatch(js, /innerHTML/);
console.log("research state UI checks passed");
