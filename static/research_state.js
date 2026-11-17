const $ = (id) => document.getElementById(id);
let projectId = "";
let projectState = null;
let synthesisResult = null;
let stateLoadSequence = 0;

function notify(message) {
  $("receiptText").textContent = message;
  $("receipt").hidden = false;
  $("toast").textContent = message;
  $("toast").hidden = false;
  window.setTimeout(() => { $("toast").hidden = true; }, 5000);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin", ...options,
    headers: {"Content-Type": "application/json", ...(options.headers || {})},
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(payload.detail?.message || `请求失败（HTTP ${response.status}）`);
    error.code = payload.detail?.code || "";
    throw error;
  }
  return payload.data;
}

function empty(container, text = "暂无内容") {
  container.replaceChildren();
  const item = document.createElement("p");
  item.className = "empty";
  item.textContent = text;
  container.append(item);
}

function entryCard(entry) {
  const card = document.createElement("article");
  card.className = "state-entry";
  const title = document.createElement("strong");
  title.textContent = entry.content + (entry.abbreviation ? `（${entry.abbreviation}）` : "");
  const detail = document.createElement("p");
  detail.textContent = entry.definition || (entry.status === "pending_review" ? "来源变化，需要复核" : "");
  card.append(title, detail);
  if (entry.source_refs?.length) {
    const source = document.createElement("small");
    source.textContent = `来源：${entry.source_refs[0].title || entry.source_refs[0].document_id} · v${entry.source_refs[0].version}`;
    card.append(source);
  }
  const actions = document.createElement("div");
  actions.className = "button-row";
  for (const [label, status] of entry.status === "pending_review" ? [["复核完成", "active"], ["废弃", "archived"]] : entry.status === "resolved" ? [["重新打开", "active"], ["归档", "archived"]] : [["标记已解决", "resolved"], ["归档", "archived"]]) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = label;
    button.addEventListener("click", async () => {
      button.disabled = true;
      const requestedProject = projectId;
      try {
        await api(`/api/research/state-entries/${entry.id}`, {
          method: "PATCH", body: JSON.stringify({expected_version: entry.version, status}),
        });
        if (projectId === requestedProject) await loadState(requestedProject);
        notify("课题状态已更新");
      } catch (error) { notify(error.message); button.disabled = false; }
    });
    actions.append(button);
  }
  card.append(actions);
  return card;
}

function renderEntries(id, entries, emptyText) {
  const container = $(id);
  container.replaceChildren();
  if (!entries?.length) { empty(container, emptyText); return; }
  for (const entry of entries) container.append(entryCard(entry));
}

function renderNextStep() {
  const button = $("nextStepAction");
  button.disabled = false;
  let step;
  if (!projectId) {
    step = {
      action: "project", label: "创建课题", title: "先创建一个研究课题",
      description: "课题会独立保存资料、研究状态和实验复盘。", context: "开始研究",
    };
  } else if (!projectState) {
    step = {action: "", label: "正在恢复", title: "正在恢复课题状态", description: "读取完成后会告诉你最值得先做的一步。", context: "请稍候"};
  } else {
    const summary = window.researchDocumentSummary?.projectId === projectId ? window.researchDocumentSummary : null;
    if (!summary) {
      step = {action: "", label: "正在读取", title: "正在核对资料与审核状态", description: "避免把其他课题的待办显示到这里。", context: "请稍候"};
    } else if (!summary.total) {
      step = {
        action: "add", label: "添加第一份资料", title: "先添加一份研究资料",
        description: "上传并核对原文；只有你确认后，资料才参与问答和综合。", context: "0 份资料",
      };
    } else if (summary.ready) {
      step = {
        action: "review", label: `确认 ${summary.ready} 份资料`, title: `有 ${summary.ready} 份资料等你确认`,
        description: "解析和结构化处理已就绪；核对来源后再决定是否进入正式课题知识。", context: `${summary.ready} 份可人工确认`,
      };
    } else if (summary.actionRequired) {
      step = {
        action: "review", label: "处理资料问题", title: `有 ${summary.actionRequired} 份资料需要先处理问题`,
        description: "扫描识别或分析失败不能直接确认入库；请查看错误、处理识别或重新分析。", context: `${summary.actionRequired} 份需要人工处理`,
      };
    } else if (projectState.counts.pending_review) {
      step = {
        action: "review", label: "复核来源变化", title: `有 ${projectState.counts.pending_review} 项关联内容需要复核`,
        description: "来源变化不会自动改写结论；核对引文后再决定是否恢复。", context: `${projectState.counts.pending_review} 项待复核`,
      };
    } else if (summary.processing) {
      step = {
        action: "tasks", label: "查看处理进度", title: `系统正在处理 ${summary.processing} 份资料`,
        description: "无需重复上传；可以先处理其他课题。需要外发审批时，也会在任务中心明确提示。", context: `${summary.processing} 份系统处理中`,
      };
    } else if (!projectState.project.research_question) {
      step = {
        action: "question", label: "写下核心问题", title: "明确当前最想回答的研究问题",
        description: "一个清楚的问题，会让后续检索、综合和实验都更有方向。", context: `${summary.total} 份已整理资料`,
      };
    } else if (projectState.project.next_action) {
      step = {
        action: "chat", label: "继续查找证据", title: projectState.project.next_action,
        description: `这是你已经保存的下一步。当前问题：${projectState.project.research_question}`,
        context: `${summary.total} 份资料 · 继续研究`,
      };
    } else {
      step = {
        action: "next", label: "确定下一步", title: "研究状态已恢复，确定一个下一步",
        description: `当前核心问题：${projectState.project.research_question}`, context: `${summary.total} 份资料 · 待规划`,
      };
    }
  }
  $("nextStepTitle").textContent = step.title;
  $("nextStepDescription").textContent = step.description;
  $("nextStepContext").textContent = step.context;
  button.textContent = step.label;
  button.dataset.action = step.action;
  button.disabled = !step.action;
}

function renderState() {
  if (!projectState) return;
  const project = projectState.project;
  $("researchQuestion").value = project.research_question || "";
  $("nextAction").value = project.next_action || "";
  $("projectGoalDisplay").textContent = project.goal || "研究目标可后补。";
  const baseline = projectState.baseline;
  $("baselineSummary").textContent = baseline
    ? `当前基线：${baseline.title} · v${baseline.version}${baseline.review_required ? ` · 待复核：${baseline.review_reason || "当前版本未确认"}。基线仍保留，未自动替换。` : ""}`
    : "当前基线：尚未指定";
  $("baselineSummary").classList.toggle("warning", Boolean(baseline?.review_required));
  const counts = projectState.counts;
  $("projectStateCounts").textContent = `${counts.pending_review} 项待复核 · ${counts.confirmed_conclusions} 条已确认结论`;
  window.researchPendingReviewCount = counts.pending_review;
  window.researchRefreshPendingCount?.();
  const conclusions = $("confirmedConclusions");
  conclusions.replaceChildren();
  if (!projectState.confirmed_conclusions.length) empty(conclusions, "暂无已确认结论");
  for (const note of projectState.confirmed_conclusions) {
    const item = document.createElement("article");
    item.className = "state-entry";
    const title = document.createElement("strong");
    title.textContent = note.title;
    const content = document.createElement("p");
    content.textContent = note.content.slice(0, 240);
    item.append(title, content);
    conclusions.append(item);
  }
  renderEntries("openQuestions", projectState.open_question, "暂无未解决问题");
  renderEntries("evidenceGaps", projectState.evidence_gap, "暂无证据缺口");
  renderEntries("todoList", projectState.todo, "暂无待办");
  renderEntries("termsList", projectState.term, "暂无术语");
  renderEntries("reviewStateList", projectState.pending_review, "暂无关联结论需要复核");
  const pendingExperiments = projectState.pending_experiments || [];
  if (pendingExperiments.length) {
    const container = $("reviewStateList");
    if (!projectState.pending_review.length) container.replaceChildren();
    for (const experiment of pendingExperiments) container.append(experimentCard(experiment));
  }
  renderExperiments();
  renderNextStep();
}

async function loadState(id) {
  const requestedProject = id || "";
  const changedProject = requestedProject !== projectId;
  const sequence = ++stateLoadSequence;
  const saveButton = $("projectStateForm").querySelector("button");
  saveButton.disabled = true;
  projectId = requestedProject;
  projectState = null;
  renderNextStep();
  if (changedProject) {
    synthesisResult = null;
    renderSynthesis();
  }
  try {
    if (!projectId) return null;
    const result = await api(`/api/research/projects/${projectId}/state`);
    if (sequence !== stateLoadSequence || projectId !== requestedProject) return null;
    projectState = result;
    renderState();
    await Promise.all([
      loadSynthesisDocuments(requestedProject, sequence),
      loadSynthesisModels(),
    ]);
    return result;
  } catch (error) {
    if (sequence === stateLoadSequence && projectId === requestedProject) notify(error.message);
    return null;
  } finally {
    if (sequence === stateLoadSequence && projectId === requestedProject) saveButton.disabled = !projectState;
  }
}

$("projectStateForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!projectState) { notify("课题状态正在恢复，请稍后保存"); return; }
  const button = event.currentTarget.querySelector("button");
  const requestedProject = projectId;
  button.disabled = true;
  try {
    const result = await api(`/api/research/projects/${requestedProject}/state`, {
      method: "PATCH", body: JSON.stringify({
        research_question: $("researchQuestion").value,
        next_action: $("nextAction").value,
        expected_updated_at: projectState.project.updated_at,
      }),
    });
    if (projectId !== requestedProject) return;
    projectState = result;
    renderState();
    notify("研究状态已保存；重开课题后可继续查看");
  } catch (error) { notify(error.message); }
  finally { if (projectId === requestedProject && projectState) button.disabled = false; }
});

$("stateEntryKind").addEventListener("change", () => {
  const isTerm = $("stateEntryKind").value === "term";
  $("stateEntryAbbreviationField").hidden = !isTerm;
  if (!isTerm) $("stateEntryAbbreviation").value = "";
});

$("stateEntryForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!projectId) return;
  const form = event.currentTarget;
  const button = form.querySelector("button[type=submit]");
  const requestedProject = projectId;
  button.disabled = true;
  try {
    await api(`/api/research/projects/${requestedProject}/state/entries`, {
      method: "POST", body: JSON.stringify({
        kind: $("stateEntryKind").value,
        content: $("stateEntryContent").value,
        definition: $("stateEntryDefinition").value,
        abbreviation: $("stateEntryAbbreviation").value,
      }),
    });
    form.reset();
    $("stateEntryAbbreviationField").hidden = true;
    if (projectId === requestedProject) await loadState(requestedProject);
    notify("已添加到课题状态");
  } catch (error) { notify(error.message); }
  finally { button.disabled = false; }
});

function experimentField(experiment, name, labelText, value, multiline = false) {
  const label = document.createElement("label");
  label.textContent = labelText;
  const control = document.createElement(multiline ? "textarea" : "input");
  control.name = name;
  control.value = value || "";
  control.setAttribute("aria-label", `${experiment.title} ${labelText}`);
  if (multiline) control.rows = 2;
  label.append(control);
  return label;
}

function experimentCard(experiment) {
  const card = document.createElement("article");
  card.className = "experiment-card";
  const title = document.createElement("h4");
  title.textContent = experiment.title;
  card.append(title);
  if (experiment.status === "pending_review") {
    const review = document.createElement("p");
    review.className = "experiment-review";
    review.textContent = `结果来源待复核：${experiment.review_reason || "资料已变化"}。实验结果和当前基线均保留，未自动修改。`;
    card.append(review);
  }
  const details = document.createElement("details");
  details.className = "experiment-details";
  details.open = experiment.status === "pending_review";
  const summary = document.createElement("summary");
  summary.textContent = "实验详情与复盘";
  const fields = document.createElement("div");
  fields.className = "experiment-fields";
  details.append(summary, fields);
  const controls = {};
  for (const [name, label, multiline] of [
    ["hypothesis", "假设", true], ["variables", "变量", true], ["controls", "对照", true],
    ["data_version", "数据版本", false], ["code_version", "代码版本", false],
    ["result", "结果（用户回填）", true], ["failure_reason", "失败原因", true], ["next_step", "下一步", true],
  ]) {
    const field = experimentField(experiment, name, label, experiment[name], multiline);
    controls[name] = field.querySelector("input,textarea");
    fields.append(field);
  }
  const labels = {idea: "设想", in_progress: "进行中", succeeded: "成功", failed: "失败", pending_review: "待复核"};
  const statusLabel = document.createElement("label");
  statusLabel.textContent = "实验状态";
  const status = document.createElement("select");
  status.setAttribute("aria-label", `${experiment.title} 状态`);
  for (const [value, label] of Object.entries(labels)) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    status.append(option);
  }
  status.value = experiment.status === "pending_review"
    ? (experiment.status_before_review || "idea")
    : experiment.status;
  statusLabel.append(status);
  fields.append(statusLabel);
  const sources = document.createElement("div");
  sources.className = "experiment-sources";
  const sourceTitle = document.createElement("strong");
  sourceTitle.textContent = "结果来源";
  sources.append(sourceTitle);
  if (!experiment.result_source_refs?.length) {
    const none = document.createElement("p");
    none.textContent = "未关联结果来源；结果仍需人工核对。";
    sources.append(none);
  }
  for (const source of experiment.result_source_refs || []) {
    const item = document.createElement("details");
    const sourceSummary = document.createElement("summary");
    sourceSummary.textContent = `${source.title} · v${source.version} · ${source.chapter}`;
    const quote = document.createElement("blockquote");
    quote.textContent = source.quote;
    const open = document.createElement("button");
    open.type = "button";
    open.textContent = "打开资料原文";
    open.addEventListener("click", () => window.researchOpenDocument?.(source));
    item.append(sourceSummary, quote, open);
    sources.append(item);
  }
  fields.append(sources);
  const save = document.createElement("button");
  save.type = "button";
  save.textContent = experiment.status === "pending_review" ? "复核并保存实验复盘" : "保存实验复盘";
  save.addEventListener("click", async () => {
    save.disabled = true;
    const requestedProject = projectId;
    try {
      await api(`/api/research/experiments/${experiment.id}`, {
        method: "PATCH", body: JSON.stringify({
          expected_version: experiment.version, status: status.value,
          fields: Object.fromEntries(Object.entries(controls).map(([name, control]) => [name, control.value])),
        }),
      });
      if (projectId === requestedProject) await loadState(requestedProject);
      notify("实验复盘已保存，未自动修改基线");
    } catch (error) { notify(error.message); save.disabled = false; }
  });
  fields.append(save);
  card.append(details);
  return card;
}

function renderExperiments() {
  const experiments = projectState?.experiments || [];
  const summary = projectState?.experiment_summary || {};
  $("experimentSummary").textContent = `${experiments.length} 个实验 · ${summary.in_progress || 0} 进行中 · ${summary.failed || 0} 失败 · ${summary.pending_review || 0} 待复核`;
  const container = $("experimentCards");
  container.replaceChildren();
  if (!experiments.length) { empty(container, "暂无实验卡片；新建内容只属于实验设想"); return; }
  for (const experiment of experiments) container.append(experimentCard(experiment));
}

$("experimentForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!projectId) return;
  const form = event.currentTarget;
  const button = form.querySelector("button[type=submit]");
  const requestedProject = projectId;
  button.disabled = true;
  try {
    await api(`/api/research/projects/${requestedProject}/experiments`, {
      method: "POST", body: JSON.stringify({
        title: $("experimentName").value,
        hypothesis: $("experimentHypothesis").value,
        variables: $("experimentVariables").value,
        controls: $("experimentControls").value,
        data_version: $("experimentDataVersion").value,
        code_version: $("experimentCodeVersion").value,
        next_step: $("experimentNextStep").value,
      }),
    });
    form.reset();
    if (projectId === requestedProject) await loadState(requestedProject);
    notify("已保存为实验设想，未执行实验");
  } catch (error) { notify(error.message); }
  finally { button.disabled = false; }
});

async function loadSynthesisDocuments(requestedProject = projectId, sequence = stateLoadSequence) {
  const container = $("synthesisDocuments");
  if (requestedProject !== projectId || sequence !== stateLoadSequence) return;
  container.replaceChildren();
  if (!requestedProject) return;
  try {
    const result = await api(`/api/research/projects/${requestedProject}/documents`);
    if (requestedProject !== projectId || sequence !== stateLoadSequence) return;
    const documents = result.items.filter(item => item.status === "approved" && item.approved_version === item.version);
    if (!documents.length) { empty(container, "暂无已确认资料；请先确认至少两份资料"); return; }
    for (const item of documents) {
      const label = window.document.createElement("label");
      const checkbox = window.document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.value = item.id;
      checkbox.className = "synthesis-document";
      label.append(checkbox, window.document.createTextNode(`${item.title} · v${item.version}`));
      container.append(label);
    }
  } catch (error) {
    if (requestedProject === projectId && sequence === stateLoadSequence) notify(error.message);
  }
}

async function loadSynthesisModels() {
  try {
    const result = await api("/api/research/capabilities");
    const select = $("synthesisModel");
    select.replaceChildren();
    for (const model of (result.external_models || []).filter(item => item.configured && (item.kind || "chat") === "chat")) {
      const option = document.createElement("option");
      option.value = model.id;
      option.textContent = `${model.provider} · ${model.id}`;
      select.append(option);
    }
  } catch (error) { notify(error.message); }
}

$("synthesisProvider").addEventListener("change", () => {
  $("synthesisModelField").hidden = $("synthesisProvider").value !== "external";
});

function renderSynthesisSources() {
  const container = $("synthesisSources");
  container.replaceChildren();
  for (const row of synthesisResult?.documents || []) {
    for (const fact of row.facts) {
      const source = {
        document_id: row.document_id, version: row.version, chunk_id: fact.chunk_id,
        title: row.title, chapter: fact.chapter, quote: fact.quote, locator: fact.locator,
      };
      const item = document.createElement("details");
      const summary = document.createElement("summary");
      summary.textContent = `${row.title} · v${row.version} · ${fact.field_label} · ${fact.chapter}`;
      const quote = document.createElement("blockquote");
      quote.textContent = fact.quote;
      item.append(summary, quote);
      window.researchAppendCitation?.(item, source, $("synthesisQuery").value);
      container.append(item);
    }
  }
}

function renderSynthesis() {
  if (!synthesisResult) { $("synthesisResult").hidden = true; return; }
  const budget = synthesisResult.evidence_budget;
  $("synthesisCoverage").textContent = budget
    ? `实际选取 ${synthesisResult.documents.length} 份资料、${budget.selected_facts} 个片段、${budget.selected_characters} 字符；单资料上限 ${budget.max_document_characters}，总上限 ${budget.max_total_characters}。${budget.truncated ? "部分内容已截断，未选内容不会外发。" : "未触发截断。"}`
    : "";
  $("synthesisContent").textContent = synthesisResult.content || "";
  $("synthesisSummary").textContent = synthesisResult.summary || (synthesisResult.comparability || []).join("\n");
  $("synthesisRisk").hidden = !synthesisResult.summary;
  $("saveSynthesisNote").disabled = !synthesisResult.generation_id;
  renderSynthesisSources();
  $("synthesisResult").hidden = false;
}

async function submitSynthesis(body, taskId = "", approvalId = "", requestedProject = projectId) {
  const result = await api(`/api/research/projects/${requestedProject}/synthesis`, {
    method: "POST", body: JSON.stringify({...body, task_id: taskId, approval_id: approvalId}),
  });
  if (projectId !== requestedProject) return;
  if (result.status === "awaiting_outbound_approval") {
    if (!window.researchShowApproval) throw new Error("外发审批组件尚未加载，请刷新后重试");
    window.researchShowApproval({approval: result.approval}, body, "", async approval => {
      await submitSynthesis(body, approval.task_id, approval.id, requestedProject);
    });
    return;
  }
  synthesisResult = result;
  renderSynthesis();
  notify("跨文献综合已生成；内容仍需核对，尚未自动入库");
}

$("synthesisForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const ids = [...document.querySelectorAll(".synthesis-document:checked")].map(item => item.value);
  if (ids.length < 2) { notify("请至少选择两份已确认资料"); return; }
  const requestedProject = projectId;
  const body = {
    document_ids: ids, query: $("synthesisQuery").value,
    provider: $("synthesisProvider").value, model_id: $("synthesisModel").value,
  };
  const button = event.currentTarget.querySelector("button[type=submit]");
  button.disabled = true;
  try { await submitSynthesis(body, "", "", requestedProject); }
  catch (error) { notify(error.message); }
  finally { button.disabled = false; }
});

$("saveSynthesisNote").addEventListener("click", async () => {
  if (!synthesisResult?.generation_id || $("saveSynthesisNote").disabled) return;
  const button = $("saveSynthesisNote");
  const generationId = synthesisResult.generation_id;
  const requestedProject = projectId;
  button.disabled = true;
  try {
    await api(`/api/research/synthesis/${generationId}/save-note`, {
      method: "POST", body: JSON.stringify({title: "跨文献综合笔记"}),
    });
    if (projectId === requestedProject) {
      await loadState(requestedProject);
      await window.researchRefreshDocuments?.();
      notify("已保存为待确认课题笔记，尚未进入正式检索");
    }
  } catch (error) {
    if (projectId === requestedProject && error.code === "synthesis_source_changed") {
      synthesisResult = null;
      renderSynthesis();
    } else if (projectId === requestedProject && synthesisResult?.generation_id === generationId) {
      button.disabled = false;
    }
    notify(error.message);
  }
});

$("addOpenQuestion").addEventListener("click", async () => {
  const content = $("question").value.trim();
  if (!content) { notify("请先输入研究问题"); return; }
  const requestedProject = projectId;
  try {
    await api(`/api/research/projects/${requestedProject}/state/entries`, {
      method: "POST", body: JSON.stringify({kind: "open_question", content}),
    });
    if (projectId === requestedProject) await loadState(requestedProject);
    notify("已加入未解决问题，可在课题首页查看");
  } catch (error) { notify(error.message); }
});

$("nextStepAction").addEventListener("click", async () => {
  const action = $("nextStepAction").dataset.action;
  try {
    if (action === "project") $("newProjectButton").click();
    else if (action === "question") $("researchQuestion").focus();
    else if (action === "next") $("nextAction").focus();
    else if (action === "add") { await window.researchSwitchView?.("library"); $("fileInput").focus(); }
    else if (action === "review") { await window.researchSwitchView?.("review"); $("viewTitle").focus(); }
    else if (action === "chat") { await window.researchSwitchView?.("chat"); $("question").focus(); }
    else if (action === "tasks") { document.querySelector(".task-center").open = true; document.querySelector(".task-center summary").focus(); }
  } catch (error) { notify(error.message); }
});

window.researchRenderNextStep = renderNextStep;
window.researchLoadState = loadState;
if (window.researchProjectId) loadState(window.researchProjectId);
