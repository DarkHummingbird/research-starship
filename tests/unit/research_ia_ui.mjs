// Real research view functions with a synthetic DOM and empty offline API.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

class ClassList {
  constructor() { this.values = new Set(); }
  toggle(name, force) {
    if (force) this.values.add(name);
    else this.values.delete(name);
  }
  contains(name) { return this.values.has(name); }
}

class Node {
  constructor(tag = 'div', id = '') {
    this.tagName = tag;
    this.id = id;
    this.children = [];
    this.events = {};
    this.dataset = {};
    this.attributes = {};
    this.classList = new ClassList();
    this.parentElement = null;
    this.hidden = false;
    this.disabled = false;
    this.value = '';
    this._text = '';
  }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text; }
  append(...nodes) {
    for (const node of nodes) {
      if (node && typeof node === 'object') node.parentElement = this;
      this.children.push(node);
    }
  }
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  removeAttribute(name) { delete this.attributes[name]; }
  addEventListener(type, fn) { (this.events[type] ||= []).push(fn); }
  async fire(type, event = {}) { for (const fn of this.events[type] || []) await fn(event); }
  querySelector() { return null; }
  closest() { return detailsNode; }
  querySelectorAll(selector) {
    const all = this.children.filter(child => child && typeof child === 'object').flatMap(child => [child, ...(child.querySelectorAll?.(selector) || [])]);
    return selector === '[data-focus-key]' ? all.filter(child => child.dataset?.focusKey) : [];
  }
  contains(node) { return this === node || this.children.some(child => child?.contains?.(node)); }
  click() { for (const fn of this.events.click || []) fn({}); }
  focus() { this.focused = true; document.activeElement = this; }
}

const nodes = new Map();
const get = (id) => {
  if (!nodes.has(id)) nodes.set(id, new Node('div', id));
  return nodes.get(id);
};
const views = ['home', 'library', 'review', 'chat', 'data'].map((name) => {
  const node = get(`${name}View`);
  node.dataset.view = name;
  return node;
});
const navButtons = ['home', 'library', 'review', 'chat', 'data'].map((name) => {
  const node = new Node('button');
  node.dataset.viewTarget = name;
  return node;
});
const tabs = ['analysis', 'source', 'sources', 'parsed', 'raw'].map((name) => {
  const node = new Node('button');
  node.dataset.tab = name;
  if (name !== 'parsed' && name !== 'raw') node.setAttribute('role', 'tab');
  return node;
});
get('libraryPreviewMount').append(get('documentPreview'));
const detailsNode = new Node("details");
const taskCenter = new Node("details");
const taskStatus = new Node('div');
taskStatus.append(get('taskCount'));
const document = {
  body: new Node('body'),
  getElementById: get,
  createElement: (tag) => new Node(tag),
  createTextNode: (text) => ({text}),
  querySelector: (selector) => selector === ".task-center" ? taskCenter : null,
  querySelectorAll(selector) {
    if (selector === '[data-view]') return views;
    if (selector === '[data-view-target]') return navButtons;
    if (selector === '[data-tab]') return tabs;
    if (selector === '[role="tablist"] [role="tab"]') return tabs.slice(0, 3);
    if (selector === '.cross-project:checked') return [];
    return [];
  },
};
const window = {
  location: {hash: ''},
  history: {replaceState(_state, _title, hash) { window.location.hash = hash; }},
  addEventListener() {},
  clearTimeout() {},
  setTimeout() { return 1; },
};
const responses = {
  '/api/session': {},
  '/api/research/projects': {items: []},
  '/api/research/tasks': {items: []},
  '/api/research/capabilities': {
    external_models: [{id: 'qwen-plus', provider: 'qwen', configured: true}],
    local_mode: {enabled: false, l3_available: false, local_generation_available: false, semantic_chunking_available: false},
  },
};
const storage = new Map();
const context = vm.createContext({
  document,
  window,
  sessionStorage: {
    getItem: key => storage.get(key) ?? null,
    setItem: (key, value) => storage.set(key, String(value)),
  },
  FormData: class {},
  fetch: async (path) => {
    assert.ok(Object.hasOwn(responses, path), `Unexpected API request: ${path}`);
    return {ok: true, status: 200, json: async () => ({data: responses[path]})};
  },
});
vm.runInContext(fs.readFileSync('static/research.js', 'utf8'), context, {filename: 'static/research.js'});
await new Promise((resolve) => setImmediate(resolve));
await new Promise((resolve) => setImmediate(resolve));

assert.equal(vm.runInContext('citationExcerpt("Method: A retrieval method.\\nAccuracy: 87.4 percent.", "论文准确率是多少？")', context), 'Accuracy: 87.4 percent.');
assert.equal(vm.runInContext('citationExcerpt("BERT introductory title\\nMore about BERT model\\nincluding the GLUE score to\\n80.5% improvement", "BERT的GLUE得分是多少？")', context), 'including the GLUE score to\n80.5% improvement');

vm.runInContext(`
  addMessage('assistant', '测试回答', [
    {title: '论文A', chapter: 'Method', version: 1, confidentiality: 'L1', quote: '证据句'},
    {title: '论文B', chapter: 'Results', version: 2, confidentiality: 'L2', quote: '第二句'},
  ], 'external:deepseek-flash');
`, context);
const answerArticle = get('chatLog').children[0];
const channelMeta = answerArticle.children.find(child => child.tagName === 'small');
assert.match(channelMeta.textContent, /在线生成：deepseek-flash/);
const sourceCards = answerArticle.children.filter(child => child.className === 'source-card');
assert.equal(sourceCards.length, 2);
assert.equal(sourceCards[0].open, true);
assert.notEqual(sourceCards[1].open, true);

assert.equal(get('homeView').hidden, false);
assert.equal(get('libraryView').hidden, true);
assert.equal(get('reviewView').hidden, true);
assert.equal(navButtons[0].getAttribute('aria-current'), 'page');
assert.equal(get('localModeEnabled').checked, false);
assert.match(get('localModeStatus').textContent, /默认关闭/);

await vm.runInContext('switchView("review")', context);
assert.equal(get('libraryView').hidden, true);
assert.equal(get('reviewView').hidden, false);
assert.equal(get('documentPreview').parentElement, get('reviewPreviewMount'));
assert.equal(navButtons[2].getAttribute('aria-current'), 'page');
assert.equal(navButtons[0].getAttribute('aria-current'), null);

vm.runInContext(`
  state.documents = [
    {id: 'approved', title: '已确认资料', status: 'approved', confidentiality: 'L2', version: 1},
    {id: 'pending', title: '待确认资料', status: 'pending', confidentiality: 'L3', version: 2},
    {id: 'rejected', title: '不保存资料', status: 'rejected', confidentiality: 'L1', version: 1},
  ];
  state.document = null;
  renderDocumentList();
`, context);
assert.equal(get('documentList').children.length, 3);
assert.equal(get('reviewList').children.length, 1);
assert.equal(get('reviewList').children[0].children[0].textContent, '待确认资料');
assert.equal(get('pendingNavCount').textContent, '1');
assert.equal(get('reviewQueueCount').textContent, '1 项待确认');
assert.equal(get('pendingNavCount').classList.contains('has-items'), true);

await vm.runInContext('switchView("library")', context);
assert.equal(get('documentPreview').parentElement, get('libraryPreviewMount'));
assert.equal(get('libraryView').hidden, false);
assert.equal(get('reviewView').hidden, true);

vm.runInContext('toast("已保存到正式记忆")', context);
assert.equal(get('receiptText').textContent, '已保存到正式记忆');
assert.equal(get('receipt').hidden, false);
await get('dismissReceipt').fire('click');
assert.equal(get('receipt').hidden, true);

vm.runInContext('setTask("test", "正在分析", true)', context);
assert.equal(get('taskCount').textContent, '1 项正在运行');
assert.equal(taskStatus.classList.contains('running'), true);
vm.runInContext('setTask("test", "", false)', context);
assert.equal(get('taskCount').textContent, '无运行任务');
assert.equal(taskStatus.classList.contains('running'), false);

let prevented = false;
await tabs[1].fire('keydown', {key: 'ArrowRight', preventDefault() { prevented = true; }});
assert.equal(prevented, true);
assert.equal(tabs[2].focused, true);
assert.equal(vm.runInContext('state.previewTab', context), 'sources');
await tabs[2].fire('keydown', {key: 'Home', preventDefault() {}});
assert.equal(tabs[0].focused, true);
assert.equal(vm.runInContext('state.previewTab', context), 'analysis');

responses['/api/research/tasks'] = {items: [], async_items: [{id:'test-task', kind:'upload', project_name:'课题', project_id:'p', status:'completed', request:{}, result:{}}]};
await vm.runInContext('refreshTasks()', context);
let taskButton = get('taskList').querySelectorAll('[data-focus-key]')[0];
assert.equal(taskButton.getAttribute('aria-label'), '查看结果：资料上传 · 课题');
taskButton.focus();
responses['/api/research/tasks'].async_items[0].error = '状态已更新';
await vm.runInContext('refreshTasks()', context);
assert.equal(document.activeElement.dataset.focusKey, 'async-test-task-view');
assert.notEqual(document.activeElement, taskButton);

await vm.runInContext(`viewAsyncTask({project_id:'', kind:'arxiv_search', status:'completed', result:{candidates:[{title:'候选论文',version:1,summary:'测试摘要',arxiv_id:'2401.12345'}]}, request:{}})`, context);
assert.equal(detailsNode.open, true);
assert.equal(get('arxivCandidates').children.length, 1);
assert.equal(document.activeElement, get('arxivCandidates'));
assert.equal(get('provider').value, 'external');
responses['/api/research/capabilities'].local_mode = {
  enabled: true, l3_available: true, local_generation_available: true, semantic_chunking_available: true,
};
await vm.runInContext('loadCapabilities()', context);
get('queryLevel').value = 'L3';
await get('queryLevel').fire('change');
assert.equal(get('provider').value, 'rag');
assert.equal(get('modelField').hidden, true);

const noteRoot = new Node('article');
context.noteRoot = noteRoot;
vm.runInContext('appendNoteSaver(noteRoot, "需要保存的回答", "研究问题", [])', context);
const noteDetails = noteRoot.children[0];
assert.equal(noteDetails.children[0].textContent, '保存为课题笔记');
assert.equal(noteDetails.children[2].children[1].children[0].value, '需要保存的回答');

vm.runInContext("approvalReturnKey = 'async-test-task-view'; approvalReturnFocus = null", context);
await get('approvalDialog').fire('close');
assert.equal(document.activeElement.dataset.focusKey, 'async-test-task-view');

const autoDoc = {
  id: 'doc-new', title: '自动刷新资料', status: 'pending', confidentiality: 'L2', version: 1,
  document_type: 'tutorial', processing_status: 'ready', analysis_status: 'not_required',
  original_filename: 'auto.md', system_copy_matches_original: true, source_text: '自动刷新内容',
  parsed: {}, analysis: {}, sources: [],
};
context.autoDoc = autoDoc;
responses['/api/research/projects'] = {items: [{id: 'p', name: '课题', document_count: 1, memory_count: 0}]};
responses['/api/research/projects/p/documents'] = {items: [autoDoc]};
responses['/api/research/documents/doc-new'] = autoDoc;
responses['/api/research/tasks'] = {items: [], async_items: [{id: 'auto-task', kind: 'upload', project_name: '课题', project_id: 'p', status: 'completed', request: {}, result: autoDoc}]};
vm.runInContext("state.projectId = 'p'; state.view = 'library'; state.watchingTasks.set('auto-task', {origin: 'upload'})", context);
await vm.runInContext('refreshTasks()', context);
assert.equal(vm.runInContext('state.documents[0].id', context), 'doc-new');
assert.equal(get('pendingNavCount').textContent, '1');

responses['/api/research/projects'] = {items: [
  {id: 'p', name: '当前标签页课题', document_count: 1, memory_count: 0},
  {id: 'q', name: '另一课题', document_count: 0, memory_count: 0},
]};
storage.set('researchProjectId', 'p');
vm.runInContext("state.projectId = 'q'", context);
await vm.runInContext('refreshProjects()', context);
assert.equal(vm.runInContext('state.projectId', context), 'p');
storage.set('researchProjectId', 'another-account-project');
vm.runInContext("state.projectId = 'q'", context);
await vm.runInContext('refreshProjects()', context);
assert.equal(vm.runInContext('state.projectId', context), 'q');
