#!/usr/bin/env node
/**
 * Batch upload to IMA - using imaApi module directly
 */
const { execSync } = require('child_process');
const fs = require('fs');
const path = require('path');
const { imaApi } = require('C:/Users/Administrator/.codex/skills/ima-skill/ima_api.cjs');

const SKILL_DIR = 'C:/Users/Administrator/.codex/skills/ima-skill';
const KB_ID = 'cNeijXKPr5_vcrfxdWrbk4zkoJj6G--DfcFS0g9H5oE=';

const FILES = [
  'D:/Knowledge/02_Projects/Active/量化策略/01_策略定义.md',
  'D:/Knowledge/02_Projects/Active/量化策略/02_信号日志.md',
  'D:/Knowledge/02_Projects/Active/量化策略/04_决策记录.md',
  'D:/Knowledge/02_Projects/Active/量化策略/K3_v2_GPT外审_结论_20260814.md',
  'D:/Knowledge/02_Projects/Active/量化策略/K3_v2_GPT外审_请求_20260814.md',
  'D:/Knowledge/03_Resources/Scripts/ndx_T_signal_v2.py',
];

const creds = {
  clientId: fs.readFileSync(require('os').homedir() + '/.config/ima/client_id', 'utf8').trim(),
  apiKey: fs.readFileSync(require('os').homedir() + '/.config/ima/api_key', 'utf8').trim(),
};

async function uploadFile(filePath) {
  const fileName = path.basename(filePath);
  const fileSize = fs.statSync(filePath).size;
  const fileExt = path.extname(filePath).slice(1);
  
  console.log(`\n=== ${fileName} (${fileSize} bytes) ===`);
  
  // Preflight
  const pre = execSync(`node knowledge-base/scripts/preflight-check.cjs --file "${filePath}"`, { cwd: SKILL_DIR, encoding: 'utf8' });
  const mt = JSON.parse(pre);
  if (!mt.pass) { console.log(`  SKIP: ${mt.reason}`); return false; }
  console.log(`  media_type=${mt.media_type}`);
  
  // Create media
  let createResp = await imaApi('openapi/wiki/v1/create_media', {
    file_name: fileName,
    file_size: fileSize,
    content_type: mt.content_type,
    knowledge_base_id: KB_ID,
    file_ext: fileExt,
  }, creds);
  if (typeof createResp === 'string') createResp = JSON.parse(createResp);
  if (createResp.code !== 0) { console.log(`  FAILED create_media: code=${createResp.code} msg=${createResp.msg}`); return false; }
  const mediaId = createResp.data.media_id;
  const cosCred = createResp.data.cos_credential;
  console.log(`  media_id=${mediaId.substring(0, 20)}...`);
  
  // COS upload
  execSync(
    `node knowledge-base/scripts/cos-upload.cjs ` +
    `--file "${filePath}" ` +
    `--secret-id "${cosCred.secret_id}" ` +
    `--secret-key "${cosCred.secret_key}" ` +
    `--token "${cosCred.token}" ` +
    `--bucket "${cosCred.bucket_name}" ` +
    `--region "${cosCred.region}" ` +
    `--cos-key "${cosCred.cos_key}" ` +
    `--content-type "${mt.content_type}" ` +
    `--start-time "${cosCred.start_time}" ` +
    `--expired-time "${cosCred.expired_time}" ` +
    `--timeout 300000`,
    { cwd: SKILL_DIR, encoding: 'utf8', stdio: ['pipe', 'pipe', 'pipe'] }
  );
  console.log(`  COS upload OK`);
  
  // Add knowledge
  let addResp = await imaApi('openapi/wiki/v1/add_knowledge', {
    media_type: mt.media_type,
    media_id: mediaId,
    title: fileName,
    knowledge_base_id: KB_ID,
    file_info: {
      cos_key: cosCred.cos_key,
      file_size: fileSize,
      file_name: fileName,
    },
  }, creds);
  if (typeof addResp === 'string') addResp = JSON.parse(addResp);
  if (addResp.code === 0) {
    console.log(`  ✓ SUCCESS`);
    return true;
  } else {
    console.log(`  FAILED add_knowledge: ${addResp.msg}`);
    return false;
  }
}

(async () => {
  let success = 0, failed = 0;
  for (const f of FILES) {
    try {
      if (await uploadFile(f)) success++; else failed++;
    } catch (e) {
      console.log(`  ERROR: ${e.message}`);
      failed++;
    }
  }
  console.log(`\n=== Done: ${success} success, ${failed} failed ===`);
})();
