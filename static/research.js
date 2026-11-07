const state = {
  projects: [],
  projectId: "",
  documents: [],
  document: null,
  conversationId: "",
  previewTab: "source",
  pendingChat: null,
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
  return (detail && detail.message) || fallback;
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
}

function statusLabel(status) {
  return { pending: "待审核", approved: "已确认", rejected: "已拒绝" }[status] || status;
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

function renderPreview() {
  const doc = state.document;
  if (!doc) { $("documentPreview").hidden = true; return; }
  $("documentPreview").hidden = false;
  $("documentTitle").textContent = doc.title;
  $("documentInfo").textContent = `${doc.original_filename} · ${doc.confidentiality} · v${doc.version} · SHA-256 ${doc.sha256.slice(0, 12)}… · 副本校验${doc.system_copy_matches_original ? "一致" : "失败"}`;
  $("documentStatus").textContent = statusLabel(doc.status);
  $("documentStatus").className = `status-badge ${doc.status}`;
  for (const tab of document.querySelectorAll(".tab")) {
    const active = tab.dataset.tab === state.previewTab;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", String(active));
  }
  const content = state.previewTab === "source" ? doc.source_text
    : state.previewTab === "parsed" ? JSON.stringify(doc.parsed, null, 2)
      : JSON.stringify(doc.sources, null, 2);
  $("previewContent").textContent = content;
  const pending = doc.status === "pending";
  $("approveDocument").disabled = !pending;
  $("rejectDocument").disabled = !pending;
  $("reviewHint").textContent = pending ? "确认前不会进入检索或正式科研记忆。" : `当前状态：${statusLabel(doc.status)}`;
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
  setBusy(form, true);
  try {
    const doc = await api(`/api/research/projects/${state.projectId}/documents`, { method: "POST", body: data });
    form.reset();
    $("confidentiality").value = "L2";
    await loadDocuments(doc.id);
    toast(doc.duplicate_of ? "发现相同内容；已新建待审版本，请核对重复资料" : "解析完成，当前仅处于本地待审状态");
  } catch (error) { toast(error.message); }
  finally { setBusy(form, false); }
});

$("approveDocument").addEventListener("click", () => review(true).catch((error) => toast(error.message)));
$("rejectDocument").addEventListener("click", () => review(false).catch((error) => toast(error.message)));
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
