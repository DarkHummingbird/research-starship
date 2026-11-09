const VIEW_CONTENT = {
  library: ["你的研究资料", "资料库", "导入、阅读并整理当前课题的资料。"],
  review: ["人工确认", "待我确认", "只处理需要核对的内容，确认后才会进入正式科研记忆。"],
  chat: ["可信回答", "课题问答", "基于当前课题已确认的资料提问，并回到原文核对。"],
  data: ["你的数据", "数据管理", "管理备份、历史版本、撤销、冲突和回收站。"],
};
const initialView = window.location.hash.slice(1);
const state = {
  projects: [],
  projectId: "",
  documents: [],
  document: null,
  conversationId: "",
  previewTab: "source",
  pendingChat: null,
  generation: null,
  view: Object.hasOwn(VIEW_CONTENT, initialView) ? initialView : "library",
  tasks: new Map(),
  persistedTasks: [],
  asyncTasks: [],
  watchingTasks: new Map(),
};

const $ = (id) => document.getElementById(id);

function toast(message) {
  $("toast").textContent = message;
  $("toast").hidden = false;
  $("receiptText").textContent = message;
  $("receipt").hidden = false;
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

const TASK_LABELS = {
  projectForm: "正在创建课题",
  uploadForm: "正在上传资料",
  chatForm: "正在回答问题",
  arxivForm: "正在检索公开论文",
  githubForm: "正在读取仓库资料",
  generationForm: "正在生成研究辅助内容",
};

function setTask(id, label, active) {
  if (active) state.tasks.set(id, label);
  else state.tasks.delete(id);
  updateTaskCount();
}

function updateTaskCount() {
  const count = state.tasks.size + state.persistedTasks.reduce((total, task) => total + task.summary.queued + task.summary.processing + (task.summary.cancelling || 0), 0) + state.asyncTasks.filter(task => ['queued', 'processing', 'cancelling'].includes(task.status)).length;
  const approvals = state.asyncTasks.filter(task => task.status === 'awaiting_approval').length;
  $("taskCount").textContent = `${count ? `${count} 项正在运行` : '无运行任务'}${approvals ? ` · ${approvals} 项待确认` : ''}`;
  $("taskCount").parentElement.classList.toggle("running", count > 0);
}

async function refreshTasks() {
  const result = await api("/api/research/tasks");
  state.persistedTasks = result.items;
  state.asyncTasks = result.async_items || [];
  updateTaskCount();
  for (const task of state.asyncTasks) {
    const watch = state.watchingTasks.get(task.id);
    if (!watch || (task.project_id && task.project_id !== state.projectId) || !['completed', 'failed', 'cancelled', 'unknown', 'awaiting_approval'].includes(task.status)) continue;
    state.watchingTasks.delete(task.id);
    if (watch.origin === 'chat' && (watch.conversationId || '') !== state.conversationId) {
      toast('先前的问答已有结果，请在任务中心查看');
      continue;
    }
    if (task.status === 'awaiting_approval') showApproval(task.result, task.request, task.id);
    else if (task.status === 'completed' && task.kind === 'chat' && watch.origin === 'paper' && state.document?.id === watch.documentId) renderPaperAnswer(task.result, task.request.message);
    else if (task.status === 'completed' && task.kind === 'chat' && watch.origin === 'chat') {
      state.conversationId = task.result.conversation_id;
      $('conversationLabel').textContent = `会话 ${state.conversationId.slice(0, 12)}…`;
      addMessage('assistant', task.result.answer, task.result.sources, task.result.model, task.request.message);
    } else if (task.status === 'completed' && watch.origin === 'external') toast('外部问答已完成，请在任务中心查看结果');
    else if (task.status === 'completed' && ['upload', 'arxiv_search', 'arxiv_import', 'github_import', 'webpage_import', 'restore_preview', 'restore_confirm', 'backup_full', 'backup_light'].includes(task.kind)) { toast('任务已完成，可在任务中心查看结果'); }
    else if (task.status === 'completed' && task.kind === 'generation') { state.generation = task.result; renderGeneration(); const panel = $('generationForm').closest('details'); if (panel) panel.open = true; }
    else if (task.status !== 'completed') toast(`任务${task.status === 'cancelled' ? '已停止' : '未完成'}：${task.error || '请查看任务中心'}`);
  }
  const snapshot = JSON.stringify([result.items, state.asyncTasks]);
  if (snapshot === state.taskSnapshot) return;
  state.taskSnapshot = snapshot;
  const list = $("taskList");
  const focusedKey = document.activeElement && list.contains(document.activeElement) ? document.activeElement.dataset.focusKey : null;
  list.replaceChildren();
  if (!result.items.length && !state.asyncTasks.length) { list.textContent = "暂无后台任务。"; if (focusedKey) document.querySelector('.task-center summary')?.focus(); return; }
  const labels = {queued: "排队中", processing: "正在处理", cancelling: "正在停止", pending_review: "完成，待确认", failed: "失败", cancelled: "已停止", completed: "已完成", awaiting_approval: "等待外发确认", unknown: "结果未知，请核对"};
  for (const task of result.items) {
    const section = document.createElement("section");
    section.className = "task-entry";
    const heading = document.createElement("strong");
    heading.textContent = `${task.project_name} · ${task.items.length} 份资料`;
    section.append(heading);
    for (const item of task.items) {
      const row = document.createElement("div");
      row.className = "task-item";
      const description = document.createElement("p");
      description.id = `batch-status-${item.id}`;
      const itemStatus = item.operation === "reindex" && item.status === "pending_review" ? "索引已重建" : (labels[item.status] || item.status);
      description.textContent = `${item.document_title}${item.operation === "reindex" ? "（重建索引）" : ""}：${itemStatus}${item.error ? ` · ${item.error}` : ""}`;
      row.append(description);
      if (["queued", "processing", "failed"].includes(item.status)) {
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = item.status === "failed" ? "手动重试" : "停止任务";
        button.dataset.focusKey = `batch-${item.id}-action`;
        button.setAttribute('aria-label', `${button.textContent}：${item.document_title}`);
        button.setAttribute('aria-describedby', description.id);
        button.addEventListener("click", async () => {
          button.disabled = true;
          try {
            const action = item.status === "failed" ? "retry" : "cancel";
            await api(`/api/research/batch-items/${item.id}/${action}`, {method: "POST", body: "{}"});
            await refreshTasks();
            toast(action === "cancel" ? "已请求停止；已完成的步骤可能保留" : "已手动重新提交任务");
          } catch (error) { toast(error.message); button.disabled = false; await refreshTasks(); }
        });
        row.append(button);
      }
      if (item.status === "pending_review") {
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = "查看资料";
        button.dataset.focusKey = `batch-${item.id}-view`;
        button.setAttribute('aria-label', `查看资料：${item.document_title}`);
        button.setAttribute('aria-describedby', description.id);
        button.addEventListener("click", async () => {
          try {
            if (state.projectId !== task.project_id) await selectProject(task.project_id);
            await switchView(item.operation === "reindex" ? "library" : "review");
            await loadDocuments(item.document_id, "analysis");
            document.querySelector(".task-center").open = false;
            $("documentTitle").focus();
          } catch (error) { toast(error.message); }
        });
        row.append(button);
      }
      section.append(row);
    }
    list.append(section);
  }
  for (const task of state.asyncTasks) {
    const section = document.createElement('section'); section.className = 'task-entry';
    const kinds = {chat:'课题问答', generation:'研究辅助', upload:'资料上传', arxiv_search:'论文检索', arxiv_import:'论文导入', github_import:'仓库导入', webpage_import:'网页导入', restore_preview:'备份检查', restore_confirm:'备份恢复', backup_full:'完整备份', backup_light:'轻量备份'};
    const title = document.createElement('strong'); title.textContent = `${kinds[task.kind] || '任务'} · ${task.project_name || '当前课题'}`;
    const row = document.createElement('div'); row.className = 'task-item';
    const text = document.createElement('p'); text.id = `async-status-${task.id}`; text.textContent = `${labels[task.status] || task.status}：${task.request.message || task.request.query || task.request.kind || ''}${task.error ? ` · ${task.error}` : ''}`;
    row.append(text);
    if (['completed', 'awaiting_approval'].includes(task.status) || (task.kind === 'generation' && task.status === 'failed' && task.result.generation_id)) {
      const view = document.createElement('button'); view.type = 'button'; view.textContent = task.status === 'awaiting_approval' ? '查看外发确认' : task.status === 'failed' ? '查看待修正稿' : '查看结果';
      view.dataset.focusKey = `async-${task.id}-view`; view.setAttribute('aria-label', `${view.textContent}：${title.textContent}`); view.setAttribute('aria-describedby', text.id);
      view.addEventListener('click', () => viewAsyncTask(task).catch(error => toast(error.message))); row.append(view);
    }
    const guarded = ['upload','arxiv_search','arxiv_import','github_import','webpage_import','restore_preview','restore_confirm','backup_full','backup_light'].includes(task.kind) || task.request.provider === 'external';
    if (['queued', 'processing', 'awaiting_approval', 'failed'].includes(task.status) && !(guarded && ['processing','failed'].includes(task.status))) {
      const action = document.createElement('button'); action.type = 'button';
      action.textContent = task.status === 'failed' ? '手动重试' : '停止任务';
      action.dataset.focusKey = `async-${task.id}-action`; action.setAttribute('aria-label', `${action.textContent}：${title.textContent}`); action.setAttribute('aria-describedby', text.id);
      if (task.status === 'failed' && task.request.provider === 'external') action.disabled = true;
      action.addEventListener('click', async () => {
        action.disabled = true;
        try { await api(`/api/research/async-tasks/${task.id}/${task.status === 'failed' ? 'retry' : 'cancel'}`, {method:'POST', body:'{}'}); await refreshTasks(); }
        catch (error) { toast(error.message); action.disabled = false; }
      }); row.append(action);
    }
    section.append(title, row);
    if (['queued', 'processing', 'cancelling', 'awaiting_approval'].includes(task.status)) list.insertBefore(section, list.firstChild);
    else list.append(section);
  }
  if (focusedKey) {
    const target = [...list.querySelectorAll('[data-focus-key]')].find(node => node.dataset.focusKey === focusedKey);
    (target || document.querySelector('.task-center summary'))?.focus();
  }
}

function setBusy(form, busy) {
  for (const element of form.querySelectorAll("button, input, textarea, select")) element.disabled = busy;
  if (TASK_LABELS[form.id]) setTask(form.id, TASK_LABELS[form.id], busy);
}

function renderProjects() {
  const switcher = $("projectSwitcher");
  switcher.replaceChildren();
  if (!state.projects.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "尚未创建课题";
    switcher.append(option);
  }
  for (const project of state.projects) {
    const option = document.createElement("option");
    option.value = project.id;
    option.textContent = `${project.name}（${project.document_count} 份资料）`;
    switcher.append(option);
  }
  switcher.disabled = !state.projects.length;
  switcher.value = state.projectId;
  const current = state.projects.find((item) => item.id === state.projectId);
  $("currentProject").textContent = current ? current.name : "请先创建课题";
  $("projectMeta").textContent = current ? (current.goal || "可直接上传资料，研究目标以后再补充。") : "每个课题的资料、审核与记忆独立保存。";
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
    $("notice").textContent = "第一次使用：先新建课题，再上传一篇公开资料；系统分析后，只需核对并确认要保存的内容。";
    $("notice").hidden = false;
  } else if (state.projectId) {
    $("notice").hidden = true;
  }
  renderProjects();
}

function renderView() {
  for (const view of document.querySelectorAll("[data-view]")) view.hidden = view.dataset.view !== state.view;
  for (const button of document.querySelectorAll("[data-view-target]")) {
    const active = button.dataset.viewTarget === state.view;
    button.classList.toggle("active", active);
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  }
  const [eyebrow, title, description] = VIEW_CONTENT[state.view];
  $("viewEyebrow").textContent = eyebrow;
  $("viewTitle").textContent = title;
  $("viewDescription").textContent = description;
  $("batchPending").hidden = state.view !== "review";
  $("newConversationButton").hidden = state.view !== "chat";
  $("refreshGovernance").hidden = state.view !== "data";
  const mount = state.view === "review" ? $("reviewPreviewMount") : $("libraryPreviewMount");
  if ($("documentPreview").parentElement !== mount) mount.append($("documentPreview"));
  renderDocumentList();
  renderPreview();
}

async function switchView(view) {
  if (!Object.hasOwn(VIEW_CONTENT, view)) return;
  state.view = view;
  window.history.replaceState(null, "", `#${view}`);
  renderView();
  if ((view === "library" || view === "review") && state.projectId) await loadDocuments();
  if (view === "data" && state.projectId) await loadGovernance();
}

async function selectProject(projectId) {
  state.projectId = projectId;
  state.conversationId = "";
  state.document = null;
  state.documents = [];
  $("conversationLabel").textContent = "新会话（发送问题后创建）";
  $("chatLog").innerHTML = '<p class="empty">新会话不会把聊天内容自动写成科研记忆。</p>';
  renderProjects();
  renderView();
  await loadDocuments();
  if (state.view === "data") await loadGovernance();
}

function statusLabel(status) {
  return { pending: "待确认", approved: "已确认", rejected: "不保存", deleted: "回收站" }[status] || status;
}

function confidentialityLabel(level) {
  return { L1: "公开", L2: "内部", L3: "保密" }[level] || level;
}

function documentTypeLabel(type) {
  return { paper: "论文", tutorial: "教程", repository: "仓库资料" }[type] || type;
}

function documentButton(doc) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = `document-item${state.document && state.document.id === doc.id ? " active" : ""}`;
  button.setAttribute("aria-pressed", String(!!state.document && state.document.id === doc.id));
  const title = document.createElement("strong");
  title.textContent = doc.title;
  const meta = document.createElement("small");
  meta.textContent = `${confidentialityLabel(doc.confidentiality)} · 版本 ${doc.version} · ${statusLabel(doc.status)}`;
  button.append(title, meta);
  button.addEventListener("click", () => openDocument(doc.id).then(() => $("documentTitle").focus()).catch((error) => toast(error.message)));
  return button;
}

function renderDocumentList() {
  const list = $("documentList");
  list.replaceChildren();
  if (!state.documents.length) list.innerHTML = '<p class="empty">暂无资料。上传后只会进入待确认状态。</p>';
  for (const doc of state.documents) list.append(documentButton(doc));

  const pending = state.documents.filter((doc) => doc.status === "pending");
  const reviewList = $("reviewList");
  reviewList.replaceChildren();
  if (!pending.length) reviewList.innerHTML = '<p class="empty">没有待确认资料。新上传或重新分析的资料会出现在这里。</p>';
  for (const doc of pending) reviewList.append(documentButton(doc));
  $("pendingNavCount").textContent = String(pending.length);
  $("pendingNavCount").setAttribute("aria-label", `${pending.length} 项待确认`);
  $("pendingNavCount").classList.toggle("has-items", pending.length > 0);
  $("reviewQueueCount").textContent = `${pending.length} 项待确认`;
  $("batchPending").disabled = !pending.length;
  const project = state.projects.find((item) => item.id === state.projectId);
  $("memoryCount").textContent = `${project ? project.memory_count : 0} 条正式记忆`;
}

async function loadDocuments(preferredId = "", preferredTab = "") {
  if (!state.projectId) {
    state.documents = [];
    state.document = null;
    renderDocumentList();
    $("documentPreview").hidden = true;
    return;
  }
  const result = await api(`/api/research/projects/${state.projectId}/documents`);
  state.documents = result.items;
  await refreshProjects(state.projectId);
  const candidates = state.view === "review" ? state.documents.filter((doc) => doc.status === "pending") : state.documents;
  const requestedId = preferredId || (state.document && state.document.id);
  const selected = (candidates.find((doc) => doc.id === requestedId) || candidates[0]);
  state.document = null;
  renderDocumentList();
  if (selected) await openDocument(selected.id, preferredTab);
  else $("documentPreview").hidden = true;
}

async function openDocument(documentId, preferredTab = "") {
  if (state.highlightUrl) { URL.revokeObjectURL(state.highlightUrl); state.highlightUrl = null; }
  state.document = await api(`/api/research/documents/${documentId}`);
  const doc = state.document;
  state.previewTab = preferredTab || (state.view === "review" && doc.status === "pending" && doc.document_type === "paper" ? "analysis" : "source");
  renderDocumentList();
  renderPreview();
}

const ANALYSIS_FIELDS = [
  ["method", "方法"], ["baseline", "基线方法"], ["datasets", "数据集"],
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
  const locate = document.createElement("button");
  locate.type = "button";
  locate.textContent = "在原文中定位";
  locate.addEventListener("click", () => locatePaperSource(doc, select.value, quote.value));
  card.append(locate);
  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "danger";
  remove.textContent = "删除此候选";
  remove.addEventListener("click", () => { const section = card.parentElement; card.remove(); section?.querySelector("button:last-child")?.focus(); });
  card.append(remove);
  return card;
}

async function locatePaperSource(doc, chunkId, quote) {
    const current = state.document;
    $("highlightStatus").textContent = "正在定位原文…";
    try {
      const response = await fetch(`/api/research/documents/${doc.id}/highlight`, {
        method: "POST", credentials: "same-origin",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({expected_version: doc.version, chunk_id: chunkId, quote}),
      });
      if (!response.ok) {
        const result = await response.json();
        throw new Error(result.detail?.message || "无法可靠定位，请人工核对原文。");
      }
      const blob = await response.blob();
      if (state.document !== current) return;
      if (state.highlightUrl) URL.revokeObjectURL(state.highlightUrl);
      state.highlightUrl = URL.createObjectURL(blob);
      $("highlightImage").src = state.highlightUrl;
      $("highlightImage").alt = `原文高亮页面。选中引文：${quote}`;
      $("highlightImage").hidden = false;
      $("pdfPreview").hidden = true;
      const note = response.headers.get("X-Source-Extraction") === "offline_ocr" ? "（按识别文字行定位，请核对原图）" : "";
      $("highlightStatus").textContent = `已高亮第 ${response.headers.get("X-Source-Page")} 页原文${note}`;
    } catch (error) {
      if (state.document !== current) return;
      $("highlightImage").hidden = true;
      $("pdfPreview").hidden = false;
      $("highlightStatus").textContent = error.message;
    }
}

function renderConfirmedAnalysis(doc) {
  const editor = $("analysisEditor");
  editor.replaceChildren();
  for (const [fieldName, label] of ANALYSIS_FIELDS) {
    const facts = doc.analysis?.[fieldName] || [];
    if (!facts.length) continue;
    const heading = document.createElement("h4");
    heading.textContent = label;
    editor.append(heading);
    for (const fact of facts) {
      const item = document.createElement("div");
      item.className = "review-issue";
      const statement = document.createElement("p");
      statement.textContent = fact.statement || "未填写事实陈述";
      item.append(statement);
      if (fact.evidence?.chunk_id && fact.evidence?.quote) {
        const locate = document.createElement("button");
        locate.type = "button";
        locate.textContent = "在原文中定位";
        locate.addEventListener("click", () => locatePaperSource(doc, fact.evidence.chunk_id, fact.evidence.quote));
        item.append(locate);
      }
      editor.append(item);
    }
  }
  if (!editor.childElementCount) editor.textContent = "暂无可展示的核心内容。";
}

function renderAnalysisReviewQueue(doc) {
  const queue = $("analysisReviewQueue");
  queue.replaceChildren();
  state.reviewResolutions = new Map();
  const issues = (Array.isArray(doc.analysis?.suspicious) ? doc.analysis.suspicious : []).filter(issue => !issue.startsWith("人工审核记录：") && !issue.includes("条模型候选因来源校验失败被隔离"));
  queue.hidden = !issues.length;
  if (!issues.length) return;
  const heading = document.createElement("h4");
  heading.textContent = `需要核对（${issues.length}）`;
  queue.append(heading);
  let raw = null;
  try {
    const text = (doc.raw_model_output || "").replace(/^```(?:json)?\s*|\s*```$/gi, "").trim();
    raw = JSON.parse(text.slice(text.indexOf("{"), text.lastIndexOf("}") + 1));
  } catch { /* 原始输出可能不是有效 JSON，保留问题说明供人工查看。 */ }
  for (const issue of issues) {
    const item = document.createElement("div");
    item.className = "review-issue";
    const description = document.createElement("p");
    description.textContent = issue;
    item.append(description);
    const match = /^已隔离：(method|baseline|datasets|metrics|limitations)\/(\d+):/.exec(issue);
    const fact = match && raw?.[match[1]]?.[Number(match[2])];
    if (fact && typeof fact === "object") {
      const detail = document.createElement("p");
      detail.textContent = `候选：${typeof fact.statement === "string" ? fact.statement : "内容需在诊断中查看"}`;
      item.append(detail);
      const add = document.createElement("button");
      add.type = "button";
      add.textContent = "修正此候选";
      const decision = document.createElement("p");
      decision.setAttribute("role", "status");
      let card = null;
      add.addEventListener("click", () => {
        const section = $("analysisEditor").querySelector(`[data-field="${match[1]}"]`);
        if (!section) return;
        const group = section.closest("details");
        if (group) group.open = true;
        if (!card || !$('analysisEditor').contains(card)) {
          card = analysisFactCard(doc, match[1], fact);
          section.append(card);
        }
        state.reviewResolutions.set(issue, {action: "corrected", card});
        decision.textContent = "已选择修正：请修改下方字段和引文，再保存。";
        card.querySelector('textarea')?.focus();
      });
      const discard = document.createElement("button");
      discard.type = "button";
      discard.textContent = "舍弃此候选";
      discard.addEventListener("click", () => {
        if (card) card.remove();
        state.reviewResolutions.set(issue, {action: "discarded"});
        decision.textContent = "已选择舍弃；保存后不会写入此候选。";
      });
      item.append(add, discard, decision);
    } else if (match) {
      const discard = document.createElement("button");
      discard.type = "button";
      discard.textContent = "舍弃无法读取的候选";
      discard.addEventListener("click", () => {
        state.reviewResolutions.set(issue, {action: "discarded"});
        discard.textContent = "已选择舍弃";
      });
      item.append(discard);
    }
    queue.append(item);
  }
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
  const otherFields = document.createElement("details");
  const otherSummary = document.createElement("summary");
  otherSummary.textContent = "其他字段（可展开检查）";
  otherFields.append(otherSummary);
  editor.append(otherFields);
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
      const card = analysisFactCard(doc, fieldName);
      section.insertBefore(card, add);
      card.querySelector('textarea')?.focus();
    });
    section.append(add);
    const needsReview = facts.some(fact => fact.evidence?.match_type === "normalized_text");
    (needsReview ? editor : otherFields).append(section);
  }
}

function factFromCard(card) {
    const fact = {
      statement: card.querySelector('[data-role="statement"]').value.trim(),
      evidence: {chunk_id: card.querySelector('[data-role="chunk"]').value, quote: card.querySelector('[data-role="quote"]').value},
    };
    if (card.dataset.matchType) fact.evidence.match_type = card.dataset.matchType;
    if (card.dataset.field === "metrics") {
      fact.value = card.querySelector('[data-role="value"]').value.trim() || null;
      fact.unit = card.querySelector('[data-role="unit"]').value.trim() || null;
    }
    return fact;
}

function collectAnalysisEditor(doc) {
  const analysis = {
    schema_version: "research-paper/v1",
    record_type: "paper_statement",
    suspicious: $("analysisSuspicious").value.split("\n").map(item => item.trim()).filter(Boolean),
  };
  for (const [fieldName] of ANALYSIS_FIELDS) analysis[fieldName] = [];
  for (const card of $("analysisEditor").querySelectorAll(".fact-card")) {
    analysis[card.dataset.field].push(factFromCard(card));
  }
  return analysis;
}

function formatAnalysis(analysis) {
  if (!analysis || typeof analysis !== "object") return "暂无结构化分析。可在原文中阅读，或等待本地分析完成。";
  const lines = [];
  if ((analysis.suspicious || []).length) {
    lines.push("【需要特别核对】", ...analysis.suspicious.map((item) => `• ${item}`), "");
  }
  for (const [fieldName, label] of ANALYSIS_FIELDS) {
    const facts = analysis[fieldName] || [];
    lines.push(`【${label}】`);
    if (!facts.length) lines.push("无候选内容");
    for (const fact of facts) {
      const value = fieldName === "metrics" && fact.value != null ? `（${fact.value}${fact.unit || ""}）` : "";
      lines.push(`• ${fact.statement || "未填写事实陈述"}${value}`);
      if (fact.evidence && fact.evidence.quote) lines.push(`  原文：${fact.evidence.quote}`);
    }
    lines.push("");
  }
  return lines.join("\n").trim() || "暂无结构化分析。";
}

function renderPreview() {
  const doc = state.document;
  if (!doc) { $("documentPreview").hidden = true; return; }
  $("documentPreview").hidden = false;
  const processing = { ready: "读取完成", needs_ocr: "需要文字识别", processing: "正在读取", failed: "读取失败" }[doc.processing_status] || doc.processing_status;
  const analysis = { ready: "分析完成", needs_review: "需要复核", not_required: "无需论文分析", pending: "等待分析", processing: "正在分析", failed: "分析失败" }[doc.analysis_status] || doc.analysis_status;
  $("documentTitle").textContent = doc.title;
  $("documentInfo").textContent = `${doc.original_filename} · ${documentTypeLabel(doc.document_type)} · ${confidentialityLabel(doc.confidentiality)} · 版本 ${doc.version} · ${processing} · ${analysis} · 副本校验${doc.system_copy_matches_original ? "一致" : "失败"}`;
  $("documentStatus").textContent = statusLabel(doc.status);
  $("documentStatus").className = `status-badge ${doc.status}`;
  for (const tab of document.querySelectorAll("[data-tab]")) {
    const active = tab.dataset.tab === state.previewTab;
    tab.classList.toggle("active", active);
    if (tab.getAttribute("role") === "tab") { tab.setAttribute("aria-selected", String(active)); tab.tabIndex = active || (!['analysis', 'source', 'sources'].includes(state.previewTab) && tab.dataset.tab === 'source') ? 0 : -1; }
  }
  $("diagnostics").open = state.previewTab === "parsed" || state.previewTab === "raw";
  const content = state.previewTab === "source" ? doc.source_text
    : state.previewTab === "parsed" ? JSON.stringify(doc.parsed, null, 2)
      : state.previewTab === "analysis" ? formatAnalysis(doc.analysis)
        : state.previewTab === "raw" ? (doc.raw_model_output || "没有原始模型输出")
          : JSON.stringify(doc.sources, null, 2);
  $("previewContent").textContent = content;
  if (["analysis", "source", "sources"].includes(state.previewTab)) { $("previewPanel").setAttribute("aria-labelledby", `tab-${state.previewTab}`); $("previewPanel").removeAttribute("aria-label"); }
  else { $("previewPanel").removeAttribute("aria-labelledby"); $("previewPanel").setAttribute("aria-label", state.previewTab === "raw" ? "原始模型输出" : "解析结果"); }
  $("previewContent").contentEditable = "false";
  const editingAnalysis = state.previewTab === "analysis" && doc.document_type === "paper" && doc.status === "pending";
  const isPaper = doc.document_type === "paper";
  $("paperQuestionPanel").hidden = !isPaper;
  if (state.paperQuestionDocument !== doc.id) {
    state.paperQuestionDocument = doc.id;
    $("paperQuestionResult").replaceChildren();
    $("paperQuestion").value = "";
    $("paperQuestionPanel").open = false;
  }
  const showingPdf = doc.original_filename.toLowerCase().endsWith(".pdf");
  $("paperSourcePane").hidden = !isPaper && !showingPdf;
  $("paperWorkspace").classList.toggle("is-paper", isPaper);
  $("paperText").hidden = !isPaper || showingPdf;
  if (isPaper && !showingPdf) $("paperText").textContent = doc.source_text;
  $("highlightImage").hidden = true;
  $("highlightStatus").textContent = "选择字段查看对应原文。";
  const readingAnalysis = state.previewTab === "analysis" && isPaper && doc.status !== "pending";
  $("previewContent").hidden = editingAnalysis || readingAnalysis || (showingPdf && state.previewTab === "source");
  $("pdfPreview").hidden = !showingPdf || (!isPaper && state.previewTab !== "source");
  if (showingPdf) $("pdfPreview").src = `/api/research/documents/${doc.id}/source-file`;
  $("analysisEditor").hidden = !editingAnalysis && !readingAnalysis;
  $("analysisReviewQueue").hidden = !editingAnalysis;
  if (editingAnalysis) { renderAnalysisEditor(doc); renderAnalysisReviewQueue(doc); }
  else if (readingAnalysis) renderConfirmedAnalysis(doc);
  $("saveAnalysis").hidden = !editingAnalysis;
  const pending = doc.status === "pending";
  const ready = doc.processing_status === "ready" && (doc.document_type !== "paper" || doc.analysis_status === "ready");
  const quarantined = doc.analysis_status === "needs_review";
  $("approveDocument").hidden = !pending;
  $("rejectDocument").hidden = !pending;
  $("approveDocument").disabled = !pending || !ready;
  $("rejectDocument").disabled = !pending;
  $("analyzeDocument").hidden = doc.document_type !== "paper" || doc.analysis_status === "ready" || doc.processing_status !== "ready";
  $("ocrDocument").hidden = doc.processing_status !== "needs_ocr";
  $("reviewHint").textContent = pending ? `确认前不会进入检索或正式科研记忆。${ready ? "当前可以确认。" : quarantined ? "部分候选已隔离；请对照原文复核并保存修正，再确认入库。" : "请先完成文字识别和结构化分析。"}` : `当前状态：${statusLabel(doc.status)}`;
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
  if (state.document.analysis_status === "needs_review") {
    if (!window.confirm("重新分析会替换当前待审核候选，确定继续吗？")) return;
    try { await runBatchAndWait([state.document.id], true); }
    catch (error) { toast(error.message); }
    return;
  }
  try { await runBatchAndWait([state.document.id]); }
  catch (error) { toast(error.message); }
}

async function saveAnalysisCorrection() {
  if (!state.document) return;
  try {
    const analysis = collectAnalysisEditor(state.document);
    const issues = (state.document.analysis?.suspicious || []).filter(item => item.startsWith("已隔离："));
    const resolutions = issues.map(issue => {
      const selected = state.reviewResolutions?.get(issue);
      if (!selected || (selected.action === "corrected" && !$('analysisEditor').contains(selected.card))) throw new Error("请逐项选择修正或舍弃，修正字段不能删除。");
      return {issue, action: selected.action, ...(selected.card ? {fact: factFromCard(selected.card)} : {})};
    });
    const result = await api(`/api/research/documents/${state.document.id}/analysis`, {
      method: "PUT",
      body: JSON.stringify({ expected_version: state.document.version, analysis, resolutions }),
    });
    await loadDocuments(result.id, "analysis");
    toast("人工修正已通过字段格式和来源校验，请继续核对");
  } catch (error) { toast(`人工修正未通过：${error.message}`); }
}

async function ocrCurrentDocument() {
  if (!state.document) return;
  try { await runBatchAndWait([state.document.id]); }
  catch (error) { toast(error.message); }
}

function addMessage(role, content, sources = [], model = "", question = "") {
  if ($("chatLog").querySelector(".empty")) $("chatLog").replaceChildren();
  const article = document.createElement("article");
  article.className = `message ${role}`;
  article.setAttribute("aria-label", role === "user" ? "你的问题" : "助手回答");
  const text = document.createElement("p");
  text.textContent = content;
  article.append(text);
  if (model) {
    const meta = document.createElement("small");
    meta.textContent = "请结合以下来源核对回答";
    article.append(meta);
  }
  for (const source of sources) {
    const details = document.createElement("details");
    details.className = "source-card";
    const summary = document.createElement("summary");
    summary.textContent = `来源：${source.title} · ${source.chapter} · 版本 ${source.version} · ${confidentialityLabel(source.confidentiality)}`;
    details.append(summary);
    appendCitation(details, source, question);
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

function citationExcerpt(quote, question = "") {
  // The highlighted excerpt is shown separately from the full retrieved passage.
  const lines = String(quote || "").split("\n").map(line => line.trim()).filter(line => line.length >= 12);
  const terms = (question.toLowerCase().match(/[a-z][a-z0-9-]{2,}/g) || []);
  const translations = [[/准确率|准确度/, "accuracy"], [/方法|模型|架构/, "method model architecture"],
    [/数据集/, "dataset"], [/基线/, "baseline"], [/注意力/, "attention"], [/参数/, "parameter"],
    [/局限|限制/, "limitation"], [/上下文/, "context"], [/翻译/, "translation"], [/检索/, "retrieval"]];
  for (const [pattern, words] of translations) if (pattern.test(question)) terms.push(...words.split(" "));
  const ranked = lines.map((line, index) => ({line, index, score: terms.reduce((score, term) => {
    if (!line.toLowerCase().includes(term)) return score;
    return score + 1 / lines.filter(candidate => candidate.toLowerCase().includes(term)).length;
  }, 0)}));
  ranked.sort((a, b) => b.score - a.score || a.index - b.index);
  const best = ranked.find(item => item.score > 0);
  if (best) {
    const next = lines[best.index + 1];
    const full = String(quote || "");
    if (/多少|得分|准确率|长度|数量|比例/.test(question) && next && /^\d/.test(next)) {
      const combined = `${best.line}\n${next}`;
      if (combined.length <= 300 && full.includes(combined)) return combined;
    }
    return best.line.slice(0, 200);
  }
  return lines.find(line => line.length >= 35 && line.length <= 300)?.slice(0, 200)
    || lines[0]?.slice(0, 200)
    || String(quote || "").trim().slice(0, 200);
}

function appendCitation(container, source, question = "") {
  const quote = document.createElement("blockquote");
  quote.textContent = source.quote;
  container.append(quote);
  if (!source.document_id || !source.chunk_id || !source.version || !source.locator?.page) return;
  const excerpt = citationExcerpt(source.quote, question);
  if (!excerpt) return;
  const label = document.createElement("p");
  label.textContent = `将定位这段摘录：${excerpt}`;
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = "定位并高亮原文";
  const result = document.createElement("div");
  result.className = "citation-highlight";
  result.setAttribute("role", "status");
  let imageUrl = null;
  button.addEventListener("click", async () => {
    button.disabled = true;
    result.textContent = "正在定位原文…";
    try {
      const response = await fetch(`/api/research/documents/${source.document_id}/highlight`, {
        method: "POST", credentials: "same-origin", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({expected_version: source.version, chunk_id: source.chunk_id, quote: excerpt}),
      });
      if (!response.ok) {
        let error = {};
        try { error = await response.json(); } catch { /* retain a useful fallback */ }
        throw new Error(errorMessage(error, "无法可靠定位，请打开原文人工核对。"));
      }
      const blob = await response.blob();
      if (imageUrl) URL.revokeObjectURL(imageUrl);
      imageUrl = URL.createObjectURL(blob);
      result.replaceChildren();
      const caption = document.createElement("p");
      const note = response.headers.get("X-Source-Extraction") === "offline_ocr" ? "；扫描件按识别行定位，请核对原图" : "";
      caption.textContent = `已高亮第 ${response.headers.get("X-Source-Page")} 页的所选摘录${note}。`;
      const image = document.createElement("img");
      image.alt = "来源原文高亮页面";
      image.src = imageUrl;
      const displayedUrl = imageUrl;
      image.addEventListener("load", () => { URL.revokeObjectURL(displayedUrl); if (imageUrl === displayedUrl) imageUrl = null; }, {once: true});
      result.append(caption, image);
    } catch (error) {
      result.textContent = `${error.message} 请使用下方原文链接核对。`;
    } finally { button.disabled = false; }
  });
  container.append(label, button, result);
}

let approvalReturnFocus = null;
let approvalReturnKey = '';
function showApproval(result, originalBody, asyncTaskId = '') {
  if ($('approvalDialog').open) { toast('另有外发请求等待确认，请在任务中心查看'); return; }
  approvalReturnFocus = document.activeElement;
  approvalReturnKey = approvalReturnFocus?.dataset?.focusKey || '';
  state.pendingChat = { body: originalBody, approval: result.approval, asyncTaskId };
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
  if (!$('approvalDialog').open) $("approvalDialog").showModal();
}

$("approvalDialog").addEventListener("close", () => {
  const replacement = approvalReturnKey && [...$("taskList").querySelectorAll('[data-focus-key]')].find(node => node.dataset.focusKey === approvalReturnKey);
  const target = replacement || (approvalReturnFocus?.isConnected ? approvalReturnFocus : null);
  if (target && target !== document.body) target.focus();
  else document.querySelector('.task-center summary')?.focus();
  approvalReturnFocus = null;
  approvalReturnKey = '';
});

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
  addMessage("assistant", result.answer, result.sources, result.model, body.message);
  return result;
}

async function viewAsyncTask(task) {
  if (task.project_id && state.projectId !== task.project_id) await selectProject(task.project_id);
  if (task.status === 'awaiting_approval') { showApproval(task.result, task.request, task.id); return; }
  let focusTarget = $('viewTitle');
  if (task.kind === 'restore_preview') {
    await switchView('data'); const result = task.result; state.restorePreview = result;
    $('restoreSummary').textContent = `备份包含 ${result.project_count} 个课题、${result.document_count} 份资料、${result.memory_count} 条记忆、${result.version_count} 条历史版本。${result.notice}`;
    $('restorePreview').hidden = false; $('restoreConfirmed').checked = false; $('restoreCommit').disabled = true; focusTarget = $('restoreSummary');
  } else if (task.kind === 'restore_confirm') {
    await refreshProjects(); await switchView('data'); toast(task.result.message);
  } else if (task.kind === 'backup_full' || task.kind === 'backup_light') {
    await switchView('data'); const link = $(task.kind === 'backup_full' ? 'downloadFullBackup' : 'downloadLightBackup');
    link.href = task.result.download_url; link.hidden = false; focusTarget = link; toast('备份已生成，可以下载');
  } else if (task.kind === 'arxiv_search') {
    await switchView('library'); $('arxivCandidates').closest('details').open = true; renderCandidates(task.result.candidates || []); focusTarget = $('arxivCandidates');
  } else if (['upload','arxiv_import','github_import','webpage_import'].includes(task.kind)) {
    const doc = task.result.document || task.result; await switchView('library'); await loadDocuments(doc.id); focusTarget = $('documentTitle');
    if (task.result.analysis_warning) toast(task.result.analysis_warning);
  } else {
  await switchView('chat');
  if (task.kind === 'chat') {
    $('chatLog').replaceChildren();
    addMessage('user', task.request.message);
    addMessage('assistant', task.result.answer, task.result.sources, task.result.model, task.request.message);
    state.conversationId = task.result.conversation_id || '';
    $('conversationLabel').textContent = state.conversationId ? `会话 ${state.conversationId.slice(0, 12)}…` : '尚未开始';
  } else { state.generation = task.result.generation_id ? await api(`/api/research/generations/${task.result.generation_id}`) : task.result; renderGeneration(); const panel = $('generationForm').closest('details'); if (panel) panel.open = true; }
  }
  document.querySelector('.task-center').open = false;
  focusTarget.focus();
}

function renderPaperAnswer(result, message) {
  const output = $('paperQuestionResult'); output.replaceChildren();
  const answer = document.createElement('p'); answer.textContent = result.answer; output.append(answer);
  for (const source of result.sources || []) {
    const entry = document.createElement('details'); const summary = document.createElement('summary');
    summary.textContent = `来源：${source.title} · ${source.chapter} · 版本 ${source.version}`;
    entry.append(summary); appendCitation(entry, source, message);
    if (source.document_id) {
      const link = document.createElement('a'); const page = source.locator?.page;
      link.href = `/api/research/documents/${source.document_id}/source-file${page ? `#page=${page}` : ''}`;
      link.target = '_blank'; link.rel = 'noopener'; link.textContent = page ? `打开原文第 ${page} 页` : '打开原文'; entry.append(link);
    }
    output.append(entry);
  }
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
$("cancelProject").addEventListener("click", () => { $("projectForm").hidden = true; $("newProjectButton").focus(); });
$("projectSwitcher").addEventListener("change", (event) => selectProject(event.currentTarget.value).catch((error) => toast(error.message)));
$("dataManagementShortcut").addEventListener("click", () => switchView("data").then(() => $("viewTitle").focus()).catch((error) => toast(error.message)));
$("dismissReceipt").addEventListener("click", () => { $("receipt").hidden = true; });
for (const button of document.querySelectorAll("[data-view-target]")) {
  button.addEventListener("click", () => switchView(button.dataset.viewTarget).then(() => $("viewTitle").focus()).catch((error) => toast(error.message)));
}
window.addEventListener("hashchange", () => {
  const view = window.location.hash.slice(1);
  if (Object.hasOwn(VIEW_CONTENT, view) && view !== state.view) switchView(view).catch((error) => toast(error.message));
});

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
    const task = await api(`/api/research/projects/${state.projectId}/document-tasks`, { method: "POST", body: data });
    form.reset();
    $("confidentiality").value = "L2";
    $("chunkingStrategy").value = "auto";
    state.watchingTasks.set(task.id, {origin:'upload'}); await refreshTasks();
    toast('文件已接收，正在处理；可在任务中心查看进度');
  } catch (error) { toast(error.message); }
  finally { setBusy(form, false); }
});

$("approveDocument").addEventListener("click", () => review(true).catch((error) => toast(error.message)));
$("rejectDocument").addEventListener("click", () => review(false).catch((error) => toast(error.message)));
$("analyzeDocument").addEventListener("click", analyzeCurrentDocument);
$("saveAnalysis").addEventListener("click", saveAnalysisCorrection);
$("ocrDocument").addEventListener("click", ocrCurrentDocument);
for (const tab of document.querySelectorAll("[data-tab]")) {
  tab.addEventListener("click", () => { state.previewTab = tab.dataset.tab; renderPreview(); });
}
const previewTabs = [...document.querySelectorAll('[role="tablist"] [role="tab"]')];
for (const tab of previewTabs) {
  tab.addEventListener('keydown', (event) => {
    const index = previewTabs.indexOf(tab);
    let target = index;
    if (event.key === 'ArrowRight') target = (index + 1) % previewTabs.length;
    else if (event.key === 'ArrowLeft') target = (index - 1 + previewTabs.length) % previewTabs.length;
    else if (event.key === 'Home') target = 0;
    else if (event.key === 'End') target = previewTabs.length - 1;
    else return;
    event.preventDefault(); previewTabs[target].click(); previewTabs[target].focus();
  });
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
  addMessage("user", question, [], `问题保密程度：${confidentialityLabel(body.query_confidentiality)}`);
  $("question").value = "";
  const form = event.currentTarget;
  setBusy(form, true);
  try {
    const task = await api(`/api/research/projects/${state.projectId}/chat-tasks`, {method:'POST', body: JSON.stringify(body)});
    state.watchingTasks.set(task.id, {origin:'chat', conversationId: body.conversation_id || ''});
    await refreshTasks();
    toast('问题已提交，可在任务中心查看进度和结果');
  }
  catch (error) {
    addMessage("assistant", `请求未完成：${error.message}。未声称外发或写入成功。`);
    if (error.detail && error.detail.approval) showApproval({ approval: error.detail.approval }, body);
  }
  finally { setBusy(form, false); }
});

$("paperQuestionForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const doc = state.document;
  const projectId = state.projectId;
  const message = $("paperQuestion").value.trim();
  if (!doc || !projectId || !message) return;
  const form = event.currentTarget;
  setBusy(form, true);
  $("paperQuestionResult").textContent = "正在查找已确认资料中的依据…";
  try {
    const task = await api(`/api/research/projects/${projectId}/chat-tasks`, {
      method: "POST", body: JSON.stringify({message, conversation_id: null, cross_project_ids: [], provider: "local", query_confidentiality: doc.confidentiality}),
    });
    state.watchingTasks.set(task.id, {origin:'paper', documentId:doc.id});
    await refreshTasks();
  } catch (error) {
    if (state.document?.id === doc.id && state.projectId === projectId) $("paperQuestionResult").textContent = `查找未完成：${error.message}`;
  } finally { setBusy(form, false); }
});

$("confirmOutbound").addEventListener("click", async (event) => {
  event.preventDefault();
  if (!state.pendingChat) return;
  const { body, approval, asyncTaskId } = state.pendingChat;
  try {
    await api(`/api/research/outbound/${approval.id}/decision`, { method: "POST", body: JSON.stringify({ approved: true }) });
    $("approvalDialog").close();
    state.pendingChat = null;
    if (asyncTaskId) {
      await api(`/api/research/async-tasks/${asyncTaskId}/resume`, {method:'POST', body:'{}'});
      state.watchingTasks.set(asyncTaskId, {origin:'external'});
      await refreshTasks();
    } else await sendChat({ ...body, conversation_id: state.conversationId, task_id: approval.task_id, approval_id: approval.id });
  } catch (error) { toast(error.message); }
});

function renderCandidates(candidates) {
  const container = $('arxivCandidates'); container.replaceChildren();
  if (!candidates.length) { container.textContent = '没有找到候选论文。'; return; }
  for (const candidate of candidates) {
    const card = document.createElement('div'); card.className = 'candidate-card';
    const title = document.createElement('strong'); title.textContent = `${candidate.title}（v${candidate.version}）`;
    const summary = document.createElement('p'); summary.textContent = candidate.summary.slice(0, 220);
    const button = document.createElement('button'); button.textContent = '选择并下载此版本';
    button.addEventListener('click', async () => {
      button.disabled = true;
      try {
        const task = await api(`/api/research/projects/${state.projectId}/source-tasks/arxiv/import`, {method:'POST', body:JSON.stringify({arxiv_id:candidate.arxiv_id, version:candidate.version, confidentiality:'L1'})});
        state.watchingTasks.set(task.id, {origin:'source'}); await refreshTasks(); toast('下载已提交，请在任务中心查看');
      } catch (error) { toast(error.message); } finally { button.disabled = false; }
    });
    card.append(title, summary, button); container.append(card);
  }
}
$('arxivForm').addEventListener('submit', async (event) => {
  event.preventDefault(); if (!state.projectId) { toast('请先选择课题'); return; }
  const form = event.currentTarget; setBusy(form, true);
  try {
    const task = await api(`/api/research/projects/${state.projectId}/source-tasks/arxiv/search`, {method:'POST', body:JSON.stringify({query:$('arxivQuery').value.trim(), confidentiality:'L1'})});
    state.watchingTasks.set(task.id, {origin:'source'}); await refreshTasks(); toast('检索已提交，请在任务中心查看');
  } catch (error) { toast(error.message); } finally { setBusy(form, false); }
});
$('githubForm').addEventListener('submit', async (event) => {
  event.preventDefault(); if (!state.projectId) { toast('请先选择课题'); return; }
  const form = event.currentTarget; setBusy(form, true);
  try {
    const paths = $('githubPaths').value.split(',').map(item => item.trim()).filter(Boolean);
    const task = await api(`/api/research/projects/${state.projectId}/source-tasks/github/import`, {method:'POST', body:JSON.stringify({repository:$('githubRepo').value.trim(), paths, confidentiality:'L1'})});
    state.watchingTasks.set(task.id, {origin:'source'}); await refreshTasks(); toast('导入已提交，请在任务中心查看');
  } catch (error) { toast(error.message); } finally { setBusy(form, false); }
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
    const task = await api(`/api/research/projects/${state.projectId}/generation-tasks`, {
      method: "POST",
      body: JSON.stringify({
        kind: $("generationKind").value,
        query: $("generationQuery").value.trim(),
        document_ids: state.document ? [state.document.id] : [],
      }),
    });
    state.watchingTasks.set(task.id, {origin:'generation'});
    await refreshTasks();
    toast('研究辅助任务已提交，可在任务中心查看');
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
    toast("修正内容已通过校验，可导出或整理为待确认资料");
  } catch (error) { toast(error.message); }
});

$("archiveGeneration").addEventListener("click", async () => {
  if (!state.generation) return;
  try {
    const document = await api(`/api/research/generations/${state.generation.id}/archive`, { method: "POST", body: "{}" });
    await loadDocuments(document.id);
    toast("已整理为待确认资料，尚未进入正式记忆");
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

async function runBatchAndWait(ids, reanalyze = false) {
    const result = await api(`/api/research/projects/${state.projectId}/batches`, {
      method: "POST", body: JSON.stringify({ document_ids: ids, reanalyze }),
    });
    toast(`已提交 ${ids.length} 份资料；分析在后台进行，仍可查看其他内容`);
    await refreshTasks();
    return result;
}

async function autoAnalyzeDocument(doc) {
  if (doc.document_type !== "paper") return;
  if (doc.processing_status === "needs_ocr") {
    toast("这份文件有需要识别的页面，请点“本地文字识别”；识别前不会确认入库");
    return;
  }
  if (doc.processing_status === "ready" && doc.analysis_status !== "ready") {
    await runBatchAndWait([doc.id]);
  }
}

$("batchPending").addEventListener("click", async () => {
  const ids = state.documents.filter((item) => item.status === "pending").map((item) => item.id);
  if (!ids.length) { toast("没有可处理的待确认资料"); return; }
  try { await runBatchAndWait(ids); }
  catch (error) { toast(error.message); }
});

async function startBackup(kind) {
  try {
    const task = await api(`/api/research/backup-tasks/${kind}`, {method:'POST', body:JSON.stringify({confirmed:true})});
    state.watchingTasks.set(task.id, {origin:'backup'}); await refreshTasks(); toast('备份已提交，请在任务中心查看');
  } catch (error) { toast(error.message); }
}
$('exportLightLibrary').addEventListener('click', () => startBackup('light'));
$('exportLibrary').addEventListener('click', () => startBackup('full'));

$("restoreFile").addEventListener("change", () => {
  state.restorePreview = null;
  $("restorePreview").hidden = true;
  $("restoreConfirmed").checked = false;
  $("restoreCommit").disabled = true;
});
$("restorePreviewForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const data = new FormData();
  data.append("file", $("restoreFile").files[0]);
  state.restorePreview = null;
  $("restorePreview").hidden = true;
  $("restoreConfirmed").checked = false;
  $("restoreCommit").disabled = true;
  setBusy(form, true);
  try {
    const task = await api("/api/research/restore-tasks/preview", {method: "POST", body: data});
    state.watchingTasks.set(task.id, {origin:"restore"}); await refreshTasks(); toast("备份检查已提交，请在任务中心查看");
  } catch (error) { toast(`备份检查未通过：${error.message}`); }
  finally { setBusy(form, false); }
});
$("restoreConfirmed").addEventListener("change", () => {
  $("restoreCommit").disabled = !state.restorePreview?.can_restore || !$("restoreConfirmed").checked;
});
$("restoreCommit").addEventListener("click", async () => {
  const preview = state.restorePreview;
  if (!preview?.can_restore || !$("restoreConfirmed").checked) return;
  $("restoreCommit").disabled = true;
  try {
    const task = await api("/api/research/restore-tasks/confirm", {method: "POST", body: JSON.stringify({token: preview.token, sha256: preview.sha256, confirmed: true})});
    state.restorePreview = null;
    $("restorePreview").hidden = true;
    state.watchingTasks.set(task.id, {origin:"restore"}); await refreshTasks();
    toast("恢复已提交，请在任务中心查看");
  } catch (error) { toast(`恢复未完成：${error.message}`); $("restoreCommit").disabled = false; }
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
    await refreshTasks();
    if (window.setInterval) window.setInterval(() => refreshTasks().catch(() => {}), 3000);
    renderView();
    if (state.projectId) await selectProject(state.projectId);
  } catch (error) {
    $("notice").hidden = false;
    $("notice").textContent = `科研页面初始化失败：${error.message}`;
  }
}

start();
