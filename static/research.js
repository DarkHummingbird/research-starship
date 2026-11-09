const state = {
  projects: [],
  projectId: "",
  documents: [],
  document: null,
  conversationId: "",
  previewTab: "source",
  pendingChat: null,
  generation: null,
};

const $ = (id) => document.getElementById(id);

function toast(message) {
  $("toast").textContent = message;
  $("toast").hidden = false;
  window.clearTimeout(toast.timer);
  toast.timer = window.setTimeout(() => { $("toast").hidden = true; }, 5000);
}

function errorMessage(payload, fallback) {
  const detail = payload && payload.detail;
  if (typeof detail === "string") return detail;
  const friendly = {
    document_parse_failed: "这个文件没有读取成功。原文件没有改变，请换文件或选择本地识别。",
    document_processing_incomplete: "还有页面没有识别完成，请先完成本地识别再确认。",
    paper_analysis_required: "论文内容还未整理好，请等待本地分析完成。",
    paper_analysis_invalid: "部分内容和原文对不上，已在预览中标出；请修改或删除后再保存。",
    document_version_changed: "内容刚发生变化，请刷新后再操作。",
    generation_validation_failed: "生成内容没有通过来源检查，请修改后再保存。",
    generation_requires_correction: "这份内容还没通过检查，请先修正再导出或归档。",
    local_model_unavailable: "本地模型暂时不可用。资料已保留，请检查模型后再继续。",
    local_ocr_failed: "本地文字识别失败，原文件仍在，请稍后手动重试。",
    external_call_failed_no_retry: "外部服务这次没有成功。为保护私有资料，系统没有自动重发；请稍后手动重试。",
    batch_split_required: "一批最多 5 篇，请拆成两批。",
    approval_scope_changed: "内容或接收方已经变化，请重新确认外发范围。",
    overwrite_confirmation_required: "已有同名文件，请再次确认后才能覆盖。",
    export_confirmation_required: "请明确确认后再打包整库。",
  };
  return (detail && detail.code && friendly[detail.code]) || (detail && detail.message) || fallback;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin",
    ...options,
    headers: options.body instanceof FormData ? options.headers : {
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
  });
  let payload = {};
  try { payload = await response.json(); } catch { payload = {}; }
  if (!response.ok) {
    const error = new Error(errorMessage(payload, `请求失败（HTTP ${response.status}）`));
    error.status = response.status;
    error.detail = payload.detail;
    throw error;
  }
  return payload.data;
}

function setBusy(form, busy) {
  for (const element of form.querySelectorAll("button, input, textarea, select")) element.disabled = busy;
}

function renderProjects() {
  const list = $("projectList");
  list.replaceChildren();
  if (!state.projects.length) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "从新建一个隔离课题开始。";
    list.append(empty);
  }
  for (const project of state.projects) {
    const button = document.createElement("button");
    button.className = `project-button${project.id === state.projectId ? " active" : ""}`;
    const name = document.createElement("strong");
    name.textContent = project.name;
    const meta = document.createElement("span");
    meta.textContent = `${project.document_count} 份资料 · ${project.memory_count} 条正式记忆`;
    button.append(name, meta);
    button.addEventListener("click", () => selectProject(project.id));
    list.append(button);
  }
  const current = state.projects.find((item) => item.id === state.projectId);
  $("currentProject").textContent = current ? current.name : "请先创建课题";
  $("projectMeta").textContent = current ? (current.goal || "未填写研究目标；可直接导入教程。") : "每个课题的资料、审核与记忆独立保存。";
  $("newConversationButton").disabled = !current;
  renderCrossProjects();
}

function renderCrossProjects() {
  const container = $("crossProjectList");
  container.replaceChildren();
  const others = state.projects.filter((item) => item.id !== state.projectId);
  $("scopeDescription").textContent = state.projectId ? "默认仅检索当前课题；勾选才会跨课题。" : "尚未选择课题";
  for (const project of others) {
    const label = document.createElement("label");
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.value = project.id;
    checkbox.className = "cross-project";
    const text = document.createElement("span");
    text.textContent = `同时检索：${project.name}`;
    label.append(checkbox, text);
    container.append(label);
  }
}

async function refreshProjects(preferredId = "") {
  const result = await api("/api/research/projects");
  state.projects = result.items;
  if (preferredId) state.projectId = preferredId;
  if (!state.projectId && state.projects.length) state.projectId = state.projects[0].id;
  if (!state.projects.length) {
    $("notice").textContent = "第一次使用：先点左上角新建课题，再上传一篇公开资料；系统分析后，只需核对并确认要保存的内容。";
    $("notice").hidden = false;
  } else if (state.projectId) {
    $("notice").hidden = true;
  }
  renderProjects();
}

async function selectProject(projectId) {
  state.projectId = projectId;
  state.conversationId = "";
  state.document = null;
  $("conversationLabel").textContent = "新会话（发送问题后创建）";
  $("chatLog").innerHTML = '<p class="empty">新会话不会把聊天内容自动写成科研记忆。</p>';
  renderProjects();
  await loadDocuments();
  await loadGovernance();
}

function statusLabel(status) {
  return { pending: "待审核", approved: "已确认", rejected: "已拒绝", deleted: "回收站" }[status] || status;
}

function renderDocumentList() {
  const list = $("documentList");
  list.replaceChildren();
  if (!state.documents.length) {
    list.innerHTML = '<p class="empty">暂无资料。导入后只会进入待审状态。</p>';
  }
  for (const doc of state.documents) {
    const button = document.createElement("button");
    button.className = `document-item${state.document && state.document.id === doc.id ? " active" : ""}`;
    const title = document.createElement("strong");
    title.textContent = doc.title;
    const meta = document.createElement("small");
    meta.textContent = `${doc.confidentiality} · v${doc.version} · ${statusLabel(doc.status)}`;
    button.append(title, meta);
    button.addEventListener("click", () => openDocument(doc.id));
    list.append(button);
  }
  const project = state.projects.find((item) => item.id === state.projectId);
  $("memoryCount").textContent = `${project ? project.memory_count : 0} 条正式记忆`;
}

async function loadDocuments(preferredId = "") {
  if (!state.projectId) return;
  const result = await api(`/api/research/projects/${state.projectId}/documents`);
  state.documents = result.items;
  await refreshProjects(state.projectId);
  const selected = preferredId || (state.document && state.document.id) || (state.documents[0] && state.documents[0].id);
  state.document = null;
  renderDocumentList();
  if (selected) await openDocument(selected);
  else $("documentPreview").hidden = true;
}

async function openDocument(documentId) {
  state.document = await api(`/api/research/documents/${documentId}`);
  state.previewTab = "source";
  renderDocumentList();
  renderPreview();
}

const ANALYSIS_FIELDS = [
  ["method", "方法"], ["baseline", "Baseline"], ["datasets", "数据集"],
  ["metrics", "指标"], ["limitations", "局限"],
];

function appendAnalysisControl(card, label, control) {
  const wrapper = document.createElement("label");
  wrapper.append(document.createTextNode(label), control);
  card.append(wrapper);
}

function analysisFactCard(doc, fieldName, fact = {}) {
  const card = document.createElement("div");
  card.className = "fact-card";
  card.dataset.field = fieldName;
  card.dataset.matchType = (fact.evidence && fact.evidence.match_type) || "";
  const statement = document.createElement("textarea");
  statement.rows = 2;
  statement.value = fact.statement || "";
  statement.dataset.role = "statement";
  appendAnalysisControl(card, "事实陈述", statement);
  if (fieldName === "metrics") {
    const value = document.createElement("input");
    value.value = fact.value ?? "";
    value.dataset.role = "value";
    appendAnalysisControl(card, "数值（可选）", value);
    const unit = document.createElement("input");
    unit.value = fact.unit ?? "";
    unit.dataset.role = "unit";
    appendAnalysisControl(card, "单位（可选）", unit);
  }
  const evidence = fact.evidence || {};
  const select = document.createElement("select");
  select.dataset.role = "chunk";
  for (const source of doc.sources || []) {
    const option = document.createElement("option");
    option.value = source.id;
    option.textContent = `${source.chapter} · ${source.id}`;
    option.selected = source.id === evidence.chunk_id;
    select.append(option);
  }
  appendAnalysisControl(card, "来源片段", select);
  const quote = document.createElement("textarea");
  quote.rows = 3;
  quote.value = evidence.quote || "";
  quote.dataset.role = "quote";
  appendAnalysisControl(card, "逐字引文", quote);
  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "danger";
  remove.textContent = "删除此候选";
  remove.addEventListener("click", () => card.remove());
  card.append(remove);
  return card;
}

function renderAnalysisEditor(doc) {
  const editor = $("analysisEditor");
  editor.replaceChildren();
  const suspicious = document.createElement("textarea");
  suspicious.id = "analysisSuspicious";
  suspicious.className = "analysis-suspicious";
  suspicious.rows = 2;
  suspicious.value = (doc.analysis.suspicious || []).join("\n");
  appendAnalysisControl(editor, "可疑项/复核说明（每行一项）", suspicious);
  for (const [fieldName, label] of ANALYSIS_FIELDS) {
    const section = document.createElement("section");
    section.className = "analysis-field";
    section.dataset.field = fieldName;
    const heading = document.createElement("h4");
    heading.textContent = label;
    section.append(heading);
    const facts = doc.analysis[fieldName] || [];
    if (!facts.length) {
      const empty = document.createElement("p");
      empty.className = "empty";
      empty.textContent = "无候选；可留空或新增一条。";
      section.append(empty);
    }
    for (const fact of facts) section.append(analysisFactCard(doc, fieldName, fact));
    const add = document.createElement("button");
    add.type = "button";
    add.textContent = `新增${label}候选`;
    add.addEventListener("click", () => {
      const empty = section.querySelector(".empty");
      if (empty) empty.remove();
      section.insertBefore(analysisFactCard(doc, fieldName), add);
    });
    section.append(add);
    editor.append(section);
  }
}

function collectAnalysisEditor(doc) {
  const analysis = {
    schema_version: "research-paper/v1",
    record_type: "paper_statement",
    suspicious: $("analysisSuspicious").value.split("\n").map(item => item.trim()).filter(Boolean),
  };
  for (const [fieldName] of ANALYSIS_FIELDS) analysis[fieldName] = [];
  for (const card of $("analysisEditor").querySelectorAll(".fact-card")) {
    const fact = {
      statement: card.querySelector('[data-role="statement"]').value.trim(),
      evidence: {
        chunk_id: card.querySelector('[data-role="chunk"]').value,
        quote: card.querySelector('[data-role="quote"]').value,
      },
    };
    const matchType = card.dataset.matchType;
    if (matchType) fact.evidence.match_type = matchType;
    if (card.dataset.field === "metrics") {
      const value = card.querySelector('[data-role="value"]').value.trim();
      const unit = card.querySelector('[data-role="unit"]').value.trim();
      fact.value = value || null;
      fact.unit = unit || null;
    }
    analysis[card.dataset.field].push(fact);
  }
  return analysis;
}

function renderPreview() {
  const doc = state.document;
  if (!doc) { $("documentPreview").hidden = true; return; }
  $("documentPreview").hidden = false;
  $("documentTitle").textContent = doc.title;
  $("documentInfo").textContent = `${doc.original_filename} · ${doc.document_type} · ${doc.confidentiality} · v${doc.version} · 解析 ${doc.processing_status} · 分析 ${doc.analysis_status} · 副本校验${doc.system_copy_matches_original ? "一致" : "失败"}`;
  $("documentStatus").textContent = statusLabel(doc.status);
  $("documentStatus").className = `status-badge ${doc.status}`;
  for (const tab of document.querySelectorAll(".tab")) {
    const active = tab.dataset.tab === state.previewTab;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", String(active));
  }
  const content = state.previewTab === "source" ? doc.source_text
    : state.previewTab === "parsed" ? JSON.stringify(doc.parsed, null, 2)
      : state.previewTab === "analysis" ? JSON.stringify(doc.analysis, null, 2)
        : state.previewTab === "raw" ? (doc.raw_model_output || "没有原始模型输出")
          : JSON.stringify(doc.sources, null, 2);
  $("previewContent").textContent = content;
  $("previewContent").contentEditable = "false";
  const editingAnalysis = state.previewTab === "analysis" && doc.document_type === "paper" && doc.status === "pending";
  const showingPdf = state.previewTab === "source" && doc.original_filename.toLowerCase().endsWith(".pdf");
  $("previewContent").hidden = editingAnalysis || showingPdf;
  $("pdfPreview").hidden = !showingPdf;
  if (showingPdf) $("pdfPreview").src = `/api/research/documents/${doc.id}/source-file`;
  $("analysisEditor").hidden = !editingAnalysis;
  if (editingAnalysis) renderAnalysisEditor(doc);
  $("saveAnalysis").hidden = !editingAnalysis;
  const pending = doc.status === "pending";
  const ready = doc.processing_status === "ready" && (doc.document_type !== "paper" || doc.analysis_status === "ready");
  const quarantined = doc.analysis_status === "needs_review";
  $("approveDocument").disabled = !pending || !ready;
  $("rejectDocument").disabled = !pending;
  $("analyzeDocument").hidden = doc.document_type !== "paper" || doc.analysis_status === "ready" || doc.processing_status !== "ready";
  $("ocrDocument").hidden = doc.processing_status !== "needs_ocr";
  $("reviewHint").textContent = pending ? `确认前不会进入检索或正式科研记忆。${ready ? "当前可审核。" : quarantined ? "部分候选已隔离；请对照原始输出复核并保存，再确认入库。" : "请先完成 OCR 和结构化分析。"}` : `当前状态：${statusLabel(doc.status)}`;
}

async function review(approved) {
  if (!state.document) return;
  const doc = state.document;
  const result = await api(`/api/research/documents/${doc.id}/review`, {
    method: "POST",
    body: JSON.stringify({ expected_version: doc.version, approved }),
  });
  toast(approved ? "已写入当前课题的正式记忆" : "已拒绝，未写入正式记忆");
  await loadDocuments(result.id);
}

async function analyzeCurrentDocument() {
  if (!state.document) return;
  $("analyzeDocument").disabled = true;
  try {
    const result = await api(`/api/research/documents/${state.document.id}/analysis`, { method: "POST", body: "{}" });
    state.previewTab = "analysis";
    await loadDocuments(result.id);
    toast("本地结构化分析完成，请逐项复核后再确认");
  } catch (error) { toast(error.message); }
  finally { $("analyzeDocument").disabled = false; }
}

async function saveAnalysisCorrection() {
  if (!state.document) return;
  try {
    const analysis = collectAnalysisEditor(state.document);
    const result = await api(`/api/research/documents/${state.document.id}/analysis`, {
      method: "PUT",
      body: JSON.stringify({ expected_version: state.document.version, analysis }),
    });
    await loadDocuments(result.id);
    toast("人工修正已通过 Schema 和来源校验，请继续审核");
  } catch (error) { toast(`人工修正未通过：${error.message}`); }
}

async function ocrCurrentDocument() {
  if (!state.document) return;
  $("ocrDocument").disabled = true;
  try {
    const result = await api(`/api/research/documents/${state.document.id}/ocr`, { method: "POST", body: "{}" });
    await loadDocuments(result.id);
    toast("本地 OCR 完成，请复核识别文本和页码");
  } catch (error) { toast(error.message); }
  finally { $("ocrDocument").disabled = false; }
}

function addMessage(role, content, sources = [], model = "") {
  if ($("chatLog").querySelector(".empty")) $("chatLog").replaceChildren();
  const article = document.createElement("article");
  article.className = `message ${role}`;
  const text = document.createElement("p");
  text.textContent = content;
  article.append(text);
  if (model) {
    const meta = document.createElement("small");
    meta.textContent = model;
    article.append(meta);
  }
  for (const source of sources) {
    const details = document.createElement("details");
    details.className = "source-card";
    const summary = document.createElement("summary");
    summary.textContent = `来源：${source.title} · ${source.chapter} · v${source.version} · ${source.confidentiality}`;
    const quote = document.createElement("blockquote");
    quote.textContent = source.quote;
    details.append(summary, quote);
    if (source.document_id) {
      const link = document.createElement("a");
      link.className = "source-file-link";
      const page = source.locator && source.locator.page;
      link.href = `/api/research/documents/${source.document_id}/source-file${page ? `#page=${page}` : ""}`;
      link.target = "_blank";
      link.rel = "noopener";
      link.textContent = page ? `打开原始文件（第 ${page} 页）` : "打开/下载原始文件";
      details.append(link);
    }
    article.append(details);
  }
  $("chatLog").append(article);
  $("chatLog").scrollTop = $("chatLog").scrollHeight;
}

function showApproval(result, originalBody) {
  state.pendingChat = { body: originalBody, approval: result.approval };
  const approval = result.approval;
  const meta = [
    ["实际服务商", approval.provider],
    ["模型 / 目标", `${approval.model_id} · ${approval.destination}`],
    ["用途", approval.purpose],
    ["操作", approval.operation],
    ["材料等级", approval.confidentiality],
    ["任务", approval.task_id],
  ];
  $("approvalMeta").replaceChildren();
  for (const [key, value] of meta) {
    const dt = document.createElement("dt");
    dt.textContent = key;
    const dd = document.createElement("dd");
    dd.textContent = value;
    $("approvalMeta").append(dt, dd);
  }
  $("approvalFragments").replaceChildren();
  for (const fragment of approval.scope.fragments) {
    const pre = document.createElement("pre");
    pre.textContent = `${fragment.chapter} · ${fragment.document_id || "检索词"} · v${fragment.version}\n${fragment.snippet}`;
    $("approvalFragments").append(pre);
  }
  $("approvalDialog").showModal();
}

async function sendChat(body) {
  const result = await api(`/api/research/projects/${state.projectId}/chat`, {
    method: "POST", body: JSON.stringify(body),
  });
  state.conversationId = result.conversation_id;
  $("conversationLabel").textContent = `会话 ${state.conversationId.slice(0, 12)}…`;
  if (result.status === "awaiting_outbound_approval") {
    showApproval(result, body);
    return result;
  }
  addMessage("assistant", result.answer, result.sources, result.model);
  return result;
}

$("projectForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  setBusy(form, true);
  try {
    const project = await api("/api/research/projects", {
      method: "POST", body: JSON.stringify({ name: $("projectName").value, goal: $("projectGoal").value }),
    });
    form.reset();
    form.hidden = true;
    await refreshProjects(project.id);
    await selectProject(project.id);
    toast("课题已创建；资料默认与其他课题隔离");
  } catch (error) { toast(error.message); }
  finally { setBusy(form, false); }
});

$("newProjectButton").addEventListener("click", () => { $("projectForm").hidden = false; $("projectName").focus(); });
$("cancelProject").addEventListener("click", () => { $("projectForm").hidden = true; });

$("uploadForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!state.projectId) { toast("请先创建或选择课题"); return; }
  const form = event.currentTarget;
  const data = new FormData();
  data.append("file", $("fileInput").files[0]);
  data.append("confidentiality", $("confidentiality").value);
  data.append("document_type", $("documentType").value);
  data.append("chunking_strategy", $("chunkingStrategy").value);
  setBusy(form, true);
  try {
    const doc = await api(`/api/research/projects/${state.projectId}/documents`, { method: "POST", body: data });
    form.reset();
    $("confidentiality").value = "L2";
    $("chunkingStrategy").value = "auto";
    await loadDocuments(doc.id);
    toast(doc.duplicate_of ? "发现相同内容；已新建待审版本，请核对重复资料" : "文件已保存，开始本地分析；分析结果仍需你核对");
    await autoAnalyzeDocument(doc);
  } catch (error) { toast(error.message); }
  finally { setBusy(form, false); }
});

$("approveDocument").addEventListener("click", () => review(true).catch((error) => toast(error.message)));
$("rejectDocument").addEventListener("click", () => review(false).catch((error) => toast(error.message)));
$("analyzeDocument").addEventListener("click", analyzeCurrentDocument);
$("saveAnalysis").addEventListener("click", saveAnalysisCorrection);
$("ocrDocument").addEventListener("click", ocrCurrentDocument);
for (const tab of document.querySelectorAll(".tab")) {
  tab.addEventListener("click", () => { state.previewTab = tab.dataset.tab; renderPreview(); });
}

$("provider").addEventListener("change", () => { $("modelField").hidden = $("provider").value !== "external"; });
$("newConversationButton").addEventListener("click", () => {
  state.conversationId = "";
  $("conversationLabel").textContent = "新会话（发送问题后创建）";
  $("chatLog").innerHTML = '<p class="empty">这是新会话。仅检索已确认记忆，不读取上一会话的未审核内容。</p>';
});

$("chatForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!state.projectId) { toast("请先选择课题"); return; }
  const question = $("question").value.trim();
  if (!question) return;
  const body = {
    message: question,
    conversation_id: state.conversationId || null,
    cross_project_ids: [...document.querySelectorAll(".cross-project:checked")].map((item) => item.value),
    provider: $("provider").value,
    model_id: $("modelId").value.trim(),
    query_confidentiality: $("queryLevel").value,
  };
  addMessage("user", question, [], `检索词 ${body.query_confidentiality}`);
  $("question").value = "";
  const form = event.currentTarget;
  setBusy(form, true);
  try { await sendChat(body); }
  catch (error) {
    addMessage("assistant", `请求未完成：${error.message}。未声称外发或写入成功。`);
    if (error.detail && error.detail.approval) showApproval({ approval: error.detail.approval }, body);
  }
  finally { setBusy(form, false); }
});

$("confirmOutbound").addEventListener("click", async (event) => {
  event.preventDefault();
  if (!state.pendingChat) return;
  const { body, approval } = state.pendingChat;
  try {
    await api(`/api/research/outbound/${approval.id}/decision`, { method: "POST", body: JSON.stringify({ approved: true }) });
    $("approvalDialog").close();
    state.pendingChat = null;
    await sendChat({ ...body, conversation_id: state.conversationId, task_id: approval.task_id, approval_id: approval.id });
  } catch (error) { toast(error.message); }
});

$("arxivForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!state.projectId) { toast("请先选择课题"); return; }
  const form = event.currentTarget;
  setBusy(form, true);
  try {
    const result = await api(`/api/research/projects/${state.projectId}/sources/arxiv/search`, {
      method: "POST", body: JSON.stringify({ query: $("arxivQuery").value.trim(), confidentiality: "L1" }),
    });
    if (result.status !== "completed") { toast("该检索需要单独外发审批"); return; }
    const container = $("arxivCandidates");
    container.replaceChildren();
    for (const candidate of result.candidates) {
      const card = document.createElement("div");
      card.className = "candidate-card";
      const title = document.createElement("strong");
      title.textContent = `${candidate.title}（v${candidate.version}）`;
      const summary = document.createElement("p");
      summary.textContent = candidate.summary.slice(0, 220);
      const button = document.createElement("button");
      button.textContent = "选择并下载此版本";
      button.addEventListener("click", async () => {
        button.disabled = true;
        try {
          const imported = await api(`/api/research/projects/${state.projectId}/sources/arxiv/import`, {
            method: "POST", body: JSON.stringify({ arxiv_id: candidate.arxiv_id, version: candidate.version, confidentiality: "L1" }),
          });
          await loadDocuments(imported.document.id);
          toast("已下载选定版本，开始本地分析；结果仍需核对");
          await autoAnalyzeDocument(imported.document);
        } catch (error) { toast(error.message); }
        finally { button.disabled = false; }
      });
      card.append(title, summary, button);
      container.append(card);
    }
  } catch (error) { toast(error.message); }
  finally { setBusy(form, false); }
});

$("githubForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!state.projectId) { toast("请先选择课题"); return; }
  const form = event.currentTarget;
  setBusy(form, true);
  try {
    const paths = $("githubPaths").value.split(",").map((item) => item.trim()).filter(Boolean);
    const result = await api(`/api/research/projects/${state.projectId}/sources/github/import`, {
      method: "POST", body: JSON.stringify({ repository: $("githubRepo").value.trim(), paths, confidentiality: "L1" }),
    });
    await loadDocuments(result.document.id);
    toast(`已固定到提交 ${result.document.repository.commit.slice(0, 12)}，内容只读`);
  } catch (error) { toast(error.message); }
  finally { setBusy(form, false); }
});

function renderGeneration() {
  const generation = state.generation;
  if (!generation) { $("generationResult").hidden = true; return; }
  const rejected = generation.status === "rejected";
  $("generationContent").textContent = generation.content;
  $("generationContent").contentEditable = rejected ? "true" : "false";
  $("correctGeneration").hidden = !rejected;
  $("archiveGeneration").disabled = rejected;
  $("exportGeneration").disabled = rejected;
  $("downloadGeneration").hidden = true;
  $("generationResult").hidden = false;
}

$("generationForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!state.projectId) { toast("请先选择课题"); return; }
  const form = event.currentTarget;
  setBusy(form, true);
  try {
    state.generation = await api(`/api/research/projects/${state.projectId}/generations`, {
      method: "POST",
      body: JSON.stringify({
        kind: $("generationKind").value,
        query: $("generationQuery").value.trim(),
        document_ids: state.document ? [state.document.id] : [],
      }),
    });
    renderGeneration();
  } catch (error) {
    if (error.detail && error.detail.generation_id) {
      state.generation = await api(`/api/research/generations/${error.detail.generation_id}`);
      renderGeneration();
      toast("生成内容未通过校验，请修正后重新提交");
    } else {
      toast(error.message);
    }
  }
  finally { setBusy(form, false); }
});

$("correctGeneration").addEventListener("click", async () => {
  if (!state.generation) return;
  try {
    state.generation = await api(`/api/research/generations/${state.generation.id}`, {
      method: "PUT", body: JSON.stringify({ content: $("generationContent").textContent }),
    });
    renderGeneration();
    toast("修正内容已通过校验，可导出或整理为待审核资料");
  } catch (error) { toast(error.message); }
});

$("archiveGeneration").addEventListener("click", async () => {
  if (!state.generation) return;
  try {
    const document = await api(`/api/research/generations/${state.generation.id}/archive`, { method: "POST", body: "{}" });
    await loadDocuments(document.id);
    toast("已整理为待审核资料，尚未进入正式记忆");
  } catch (error) { toast(error.message); }
});

$("exportGeneration").addEventListener("click", async () => {
  if (!state.generation) return;
  try {
    const result = await api(`/api/research/generations/${state.generation.id}/export`, {
      method: "POST", body: JSON.stringify({ filename: `${state.generation.kind}${state.generation.kind === "latex" ? ".tex" : ".md"}` }),
    });
    $("downloadGeneration").href = result.download_url;
    $("downloadGeneration").hidden = false;
    toast(`已导出本地文件：${result.filename}，可点击下载`);
  } catch (error) { toast(error.message); }
});

function governanceCard(container, title, detail, actions = []) {
  const card = document.createElement("div");
  card.className = "governance-card";
  const heading = document.createElement("strong");
  heading.textContent = title;
  const text = document.createElement("p");
  text.textContent = detail;
  card.append(heading, text);
  if (actions.length) {
    const row = document.createElement("div");
    row.className = "button-row";
    for (const action of actions) {
      const button = document.createElement("button");
      button.textContent = action.label;
      button.addEventListener("click", action.run);
      row.append(button);
    }
    card.append(row);
  }
  container.append(card);
}

async function loadGovernance() {
  if (!state.projectId) return;
  const [conflicts, changes, recycle, versions] = await Promise.all([
    api(`/api/research/projects/${state.projectId}/conflicts`),
    api(`/api/research/projects/${state.projectId}/changes`),
    api(`/api/research/projects/${state.projectId}/recycle-bin`),
    state.document ? api(`/api/research/documents/${state.document.id}/versions`) : Promise.resolve({ items: [] }),
  ]);
  const conflictList = $("conflictList");
  conflictList.replaceChildren();
  if (!conflicts.items.length) conflictList.innerHTML = '<p class="empty">暂无冲突</p>';
  for (const conflict of conflicts.items.filter((item) => item.status === "pending")) {
    governanceCard(
      conflictList,
      `${conflict.candidate_fact.statement} ↔ ${conflict.existing_fact.statement}`,
      `同条件指标冲突。当前值 ${conflict.candidate_fact.value}，已有值 ${conflict.existing_fact.value}。`,
      [
        { label: "覆盖旧值", run: () => resolveConflict(conflict, "overwrite") },
        { label: "放弃本次", run: () => resolveConflict(conflict, "discard") },
        { label: "两者并列", run: () => resolveConflict(conflict, "keep_both") },
        { label: "人工修正", run: () => resolveConflict(conflict, "edit") },
      ],
    );
  }
  const changeList = $("changeList");
  changeList.replaceChildren();
  if (!changes.items.length) changeList.innerHTML = '<p class="empty">暂无变更</p>';
  for (const change of changes.items) {
    governanceCard(changeList, `${change.operation} · v${change.document_version}`, change.created_at,
      change.undoable && !change.undone_at ? [{ label: "撤销", run: () => undoChange(change) }] : []);
  }
  const recycleList = $("recycleList");
  recycleList.replaceChildren();
  if (!recycle.items.length) recycleList.innerHTML = '<p class="empty">回收站为空</p>';
  for (const item of recycle.items) {
    governanceCard(recycleList, item.title, `剩余约 ${item.remaining_days} 天；到期后仍需再次确认清理`,
      [{ label: "恢复", run: () => restoreDeleted(item) }]);
  }
  const versionList = $("versionList");
  versionList.replaceChildren();
  if (!versions.items.length) versionList.innerHTML = '<p class="empty">暂无历史快照</p>';
  if (state.document) {
    for (const version of versions.items) {
      governanceCard(versionList, `v${version.version} · ${version.status}`, version.created_at,
        version.version !== state.document.version ? [{ label: "恢复为新版本", run: () => restoreVersion(version) }] : []);
    }
  }
}

async function resolveConflict(conflict, resolution) {
  try {
    const result = await api(`/api/research/conflicts/${conflict.id}/resolve`, {
      method: "POST", body: JSON.stringify({ resolution, edited_fact: resolution === "edit" ? conflict.candidate_fact : null }),
    });
    toast(result.next_action === "review_candidate" ? "冲突决定已保存，请审核候选资料后生效" : "冲突处理已保存");
    await loadDocuments(conflict.candidate_document_id);
    await loadGovernance();
  } catch (error) { toast(error.message); }
}

async function undoChange(change) {
  try {
    await api(`/api/research/changes/${change.id}/undo`, { method: "POST", body: JSON.stringify({ confirmed: true }) });
    toast("已恢复快照为新版本，历史记录保留");
    await loadDocuments();
    await loadGovernance();
  } catch (error) { toast(error.message); }
}

async function restoreDeleted(item) {
  try {
    await api(`/api/research/documents/${item.id}/restore`, { method: "POST", body: JSON.stringify({ confirmed: true }) });
    toast("资料已恢复，检索状态已同步");
    await loadDocuments(item.id);
    await loadGovernance();
  } catch (error) { toast(error.message); }
}

async function restoreVersion(version) {
  if (!state.document) return;
  try {
    await api(`/api/research/documents/${state.document.id}/restore-version`, {
      method: "POST",
      body: JSON.stringify({ target_version: version.version, expected_version: state.document.version, confirmed: true }),
    });
    toast("历史内容已恢复为新版本，请重新审核");
    await loadDocuments();
    await loadGovernance();
  } catch (error) { toast(error.message); }
}

$("refreshGovernance").addEventListener("click", () => loadGovernance().catch((error) => toast(error.message)));

async function runBatchAndWait(ids) {
  const result = await api(`/api/research/projects/${state.projectId}/batches`, {
    method: "POST", body: JSON.stringify({ document_ids: ids }),
  });
  toast(`已提交 ${ids.length} 份资料；分析在后台进行，仍可查看其他内容`);
  const deadline = Date.now() + 120000;
  let current = result;
  while (Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 500));
    current = await api(`/api/research/batches/${result.id}`);
    if (current.status.startsWith("completed")) {
      toast(`处理结束：${current.summary.pending_review} 份待核对，${current.summary.failed} 份失败`);
      await loadDocuments();
      return current;
    }
    toast(`正在处理：等待 ${current.summary.queued} 份，处理中 ${current.summary.processing} 份`);
  }
  toast("任务仍在后台运行，可以稍后刷新查看；无需停在当前页面");
  return current;
}

async function autoAnalyzeDocument(doc) {
  if (doc.document_type !== "paper") return;
  if (doc.processing_status === "needs_ocr") {
    toast("这份文件有需要识别的页面，请点“本地 OCR”；识别前不会确认入库");
    return;
  }
  if (doc.processing_status === "ready" && doc.analysis_status !== "ready") {
    await runBatchAndWait([doc.id]);
  }
}

$("batchPending").addEventListener("click", async () => {
  const ids = state.documents.filter((item) => item.status === "pending" || item.status === "rejected").map((item) => item.id);
  if (!ids.length) { toast("没有可处理的待审资料"); return; }
  try { await runBatchAndWait(ids); }
  catch (error) { toast(error.message); }
});

$("exportLightLibrary").addEventListener("click", async () => {
  try {
    const result = await api("/api/research/export/light", {
      method: "POST", body: JSON.stringify({ confirmed: true }),
    });
    $("downloadLightBackup").href = result.download_url;
    $("downloadLightBackup").hidden = false;
    toast(`轻量备份已创建：${result.memory_count} 条当前有效记忆；不含原件和历史版本`);
  } catch (error) { toast(error.message); }
});

$("exportLibrary").addEventListener("click", async () => {
  try {
    const result = await api("/api/research/export", { method: "POST", body: JSON.stringify({ confirmed: true }) });
    toast(`整库已在本地打包：${result.filename}；缺件 ${result.missing_originals.length} 项`);
  } catch (error) { toast(error.message); }
});

$("deleteCurrent").addEventListener("click", async () => {
  if (!state.document || !window.confirm("确认将当前资料移入回收站？30 天内可恢复。")) return;
  try {
    await api(`/api/research/documents/${state.document.id}/delete`, {
      method: "POST", body: JSON.stringify({ expected_version: state.document.version, confirmed: true }),
    });
    state.document = null;
    await loadDocuments();
    await loadGovernance();
  } catch (error) { toast(error.message); }
});

async function start() {
  try {
    await api("/api/session", { method: "POST", body: "{}" });
    await refreshProjects();
    if (state.projectId) await selectProject(state.projectId);
    else renderProjects();
  } catch (error) {
    $("notice").hidden = false;
    $("notice").textContent = `科研页面初始化失败：${error.message}`;
  }
}

start();
