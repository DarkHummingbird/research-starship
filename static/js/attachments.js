import { $, h } from "./core.js?v=__APP_VERSION__";
import { api } from "./api.js?v=__APP_VERSION__";

let selectedFile = null;
let uploaded = null;
let uploading = false;

const MODE_LABELS = {
  deepseek_vision: "DeepSeek 视觉理解",
  local_ocr: "本地 OCR，仅文字识别",
  online_text: "在线模型处理解析片段",
  local_text: "仅本机处理",
};

function render(status = "") {
  const preview = $("attachmentPreview");
  const options = $("attachmentOptions");
  preview.replaceChildren();
  const file = selectedFile || uploaded;
  preview.hidden = !file;
  options.hidden = !file || !!uploaded;
  if (!file) return;
  const name = file.filename || file.name;
  const mode = uploaded ? MODE_LABELS[uploaded.processing_mode] || "仅本次对话" : "仅本次对话，不自动保存";
  const remove = h("button", {
    type: "button", class: "attachment-remove", "aria-label": `移除附件 ${name}`, text: "×",
    onClick: async () => {
      if (uploading) return;
      if (uploaded) await api.deleteAttachment(uploaded.id);
      resetAttachments();
    },
  });
  preview.append(
    h("div", { class: "attachment-card" }, [
      h("span", { class: "attachment-name", text: name }),
      h("span", { class: "attachment-status", text: status || (uploaded ? "可以使用" : "待上传") }),
      remove,
    ]),
    h("p", { class: "attachment-scope", text: uploaded?.error || mode }),
  );
}

export function resetAttachments() {
  selectedFile = null;
  uploaded = null;
  uploading = false;
  $("attachmentInput").value = "";
  render();
}

export function initAttachments() {
  $("btnAttachment").addEventListener("click", () => $("attachmentInput").click());
  $("attachmentInput").addEventListener("change", (event) => {
    const file = event.target.files && event.target.files[0];
    if (!file) return;
    selectedFile = file;
    uploaded = null;
    render();
  });
  $("attachmentConfidentiality").addEventListener("change", (event) => {
    const localOnly = event.target.value === "L3";
    $("attachmentOutbound").disabled = localOnly;
    if (localOnly) $("attachmentOutbound").checked = false;
  });
  resetAttachments();
}

export function hasAttachment() {
  return Boolean(selectedFile || uploaded);
}

export async function prepareAttachment() {
  if (uploaded) return uploaded.id;
  if (!selectedFile) return "";
  uploading = true;
  render("正在上传并读取");
  try {
    const response = await api.uploadAttachment(
      selectedFile,
      $("attachmentConfidentiality").value,
      $("attachmentOutbound").checked,
    );
    uploaded = response.data.attachment;
    if (uploaded.status !== "ready") throw new Error(uploaded.error || "附件处理失败，请重新选择");
    selectedFile = null;
    $("attachmentInput").value = "";
    render();
    return uploaded.id;
  } catch (error) {
    uploaded = null;
    render("上传失败，可重试");
    throw error;
  } finally {
    uploading = false;
  }
}
