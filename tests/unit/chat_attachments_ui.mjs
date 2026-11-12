import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const html = readFileSync(new URL("../../static/index.html", import.meta.url), "utf8");
const api = readFileSync(new URL("../../static/js/api.js", import.meta.url), "utf8");
const attachments = readFileSync(new URL("../../static/js/attachments.js", import.meta.url), "utf8");
const flows = readFileSync(new URL("../../static/js/flows.js", import.meta.url), "utf8");
const app = readFileSync(new URL("../../static/js/app.js", import.meta.url), "utf8");

for (const id of ["btnAttachment", "attachmentInput", "attachmentPreview", "attachmentOptions", "attachmentConfidentiality", "attachmentOutbound"]) {
  assert.match(html, new RegExp(`id="${id}"`));
}
assert.match(html, /accept="\.jpg,\.jpeg,\.png,\.webp,\.pdf,\.docx,\.md,\.markdown"/);
assert.doesNotMatch(html, /attachmentInput[^>]*multiple/);
assert.match(html, /仅本次对话/);
assert.match(html, /不勾选则仅本机处理/);
assert.match(api, /attachment_ids: attachmentId \? \[attachmentId\] : \[\]/);
assert.match(api, /form\.append\("allow_outbound", String\(allowOutbound\)\)/);
assert.match(attachments, /files && event\.target\.files\[0\]/);
assert.match(attachments, /uploaded = null;\s+render\("上传失败，可重试"\)/);
assert.doesNotMatch(attachments, /innerHTML/);
assert.match(flows, /attachmentId = await prepareAttachment\(\)/);
assert.match(flows, /if \(sent\) resetAttachments\(\)/);
assert.match(flows, /resetAttachments\(\);/);
assert.match(app, /initAttachments\(\);/);

console.log("chat attachment UI checks passed");
