#!/usr/bin/env node
/**
 * Obsidian Vault → IMA 增量更新（仅上传过去 24h 修改过的文件）
 * 由 Hermes cron 每日 04:00 调用
 */
const { execSync } = require('child_process');
const fs = require('fs');
const path = require('path');
const { imaApi } = require('C:/Users/Administrator/.codex/skills/ima-skill/ima_api.cjs');

const SKILL_DIR = 'C:/Users/Administrator/.codex/skills/ima-skill';
const VAULT_DIR = 'D:/Knowledge';
const KB_ID = 'cNeijXKPr5_vcrfxdWrbk4zkoJj6G--DfcFS0g9H5oE=';
const STATE_FILE = path.join(__dirname, '.ima_sync_state.json');

const creds = {
  clientId: fs.readFileSync(require('os').homedir() + '/.config/ima/client_id', 'utf8').trim(),
  apiKey: fs.readFileSync(require('os').homedir() + '/.config/ima/api_key', 'utf8').trim(),
};

let state = {};
try { state = JSON.parse(fs.readFileSync(STATE_FILE, 'utf8')); } catch {}

function findRecentFiles(dir, sinceMs) {
  let results = [];
  let entries = [];
  try { entries = fs.readdirSync(dir, { withFileTypes: true }); } catch { return results; }
  for (const entry of entries) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name.startsWith('.') || entry.name === '__pycache__') continue;
      results = results.concat(findRecentFiles(full, sinceMs));
    } else if (entry.name.endsWith('.md')) {
      try {
        const mtime = fs.statSync(full).mtimeMs;
        if (mtime > sinceMs) results.push(full);
      } catch {}
    }
  }
  return results;
}

function getFileHash(filePath) {
  const stat = fs.statSync(filePath);
  return `${stat.size}_${stat.mtimeMs}`;
}

async function uploadFile(filePath) {
  const fileName = path.basename(filePath);
  const fileSize = fs.statSync(filePath).size;
  const pre = execSync(`node knowledge-base/scripts/preflight-check.cjs --file "${filePath}"`, { cwd: SKILL_DIR, encoding: 'utf8' });
  const mt = JSON.parse(pre);
  if (!mt.pass) return { skip: true };
  let createResp = await imaApi('openapi/wiki/v1/create_media', {
    file_name: fileName, file_size: fileSize, content_type: mt.content_type,
    knowledge_base_id: KB_ID, file_ext: 'md',
  }, creds);
  if (typeof createResp === 'string') createResp = JSON.parse(createResp);
  if (createResp.code !== 0) return { error: createResp.msg };
  const cosCred = createResp.data.cos_credential;
  execSync(`node knowledge-base/scripts/cos-upload.cjs --file "${filePath}" --secret-id "${cosCred.secret_id}" --secret-key "${cosCred.secret_key}" --token "${cosCred.token}" --bucket "${cosCred.bucket_name}" --region "${cosCred.region}" --cos-key "${cosCred.cos_key}" --content-type "${mt.content_type}" --start-time "${cosCred.start_time}" --expired-time "${cosCred.expired_time}" --timeout 300000`, { cwd: SKILL_DIR, encoding: 'utf8', stdio: ['pipe', 'pipe', 'pipe'] });
  let addResp = await imaApi('openapi/wiki/v1/add_knowledge', {
    media_type: mt.media_type, media_id: createResp.data.media_id, title: fileName,
    knowledge_base_id: KB_ID,
    file_info: { cos_key: cosCred.cos_key, file_size: fileSize, file_name: fileName },
  }, creds);
  if (typeof addResp === 'string') addResp = JSON.parse(addResp);
  return { success: addResp.code === 0 };
}

(async () => {
  const since = Date.now() - 24 * 3600 * 1000;
  const files = findRecentFiles(VAULT_DIR, since);
  console.log(`[IMA-SYNC] ${files.length} files modified in last 24h`);
  
  let uploaded = 0, failed = 0;
  for (let i = 0; i < files.length; i++) {
    const f = files[i];
    const relPath = path.relative(VAULT_DIR, f);
    const hash = getFileHash(f);
    if (state[relPath] === hash) continue;
    if (i > 0 && i % 5 === 0) { console.log('  (rate-limit pause 3s)'); await new Promise(r => setTimeout(r, 3000)); }
    try {
      const r = await uploadFile(f);
      if (r.success) { state[relPath] = hash; uploaded++; console.log(`  ✓ ${relPath}`); }
      else { failed++; console.log(`  ✗ ${relPath}: ${r.error || 'unknown'}`); }
    } catch (e) { failed++; console.log(`  ✗ ${relPath}: ${e.message}`); }
  }
  fs.writeFileSync(STATE_FILE, JSON.stringify(state, null, 2), 'utf8');
  console.log(`[IMA-SYNC] Done: ${uploaded} uploaded, ${failed} failed`);
})();
