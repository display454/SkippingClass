/* 语音转录工坊 —— 前端交互 */

const state = {
  config: null,
  catalog: [],
  provider: 'volc',
  jobs: [],
  cards: new Map(),
  previewTab: 'rendered',
  previewContent: '',
  polling: null,
};

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

/* ---------------- 工具 ---------------- */

function esc(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

let toastTimer = null;
function toast(msg, isError = false) {
  const el = $('#toast');
  el.textContent = msg;
  el.classList.toggle('err', isError);
  el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), 3400);
}

const FMT_LABEL = { md: 'Markdown', docx: 'Word', txt: '文本' };

const DL_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4v11m0 0 4-4m-4 4-4-4"/><path d="M5 19h14"/></svg>';

/* 复制到剪贴板：把转录稿直接粘进笔记/文档，比下载再打开快 */
function copyText(text) {
  const fallback = () => {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
    ta.remove();
    return ok;
  };

  if (navigator.clipboard && window.isSecureContext) {
    return navigator.clipboard.writeText(text).then(() => true).catch(fallback);
  }
  return Promise.resolve(fallback());
}

/* ---------------- 引擎 ---------------- */

async function loadCatalog() {
  const r = await fetch('/api/providers');
  state.catalog = await r.json();
  return state.catalog;
}

function renderEngines(readyMap) {
  const box = $('#engineList');
  box.innerHTML = state.catalog.map(p => {
    const ready = readyMap ? !!readyMap[p.id] : true;
    return `
      <label class="engine-card ${p.id === state.provider ? 'active' : ''}" data-id="${p.id}">
        <div class="ec-top">
          <span class="ec-name">${esc(p.name)}</span>
          <i class="ec-dot ${ready ? 'ok' : ''}"></i>
        </div>
        <p class="ec-sum">${esc(p.summary)}</p>
        <div class="ec-tags">
          <span>${p.supports_timestamps ? '带时间戳' : '无时间戳'}</span>
          <span>单段 ≤ ${p.max_segment_minutes} 分</span>
        </div>
      </label>`;
  }).join('');

  $$('.engine-card', box).forEach(card => {
    card.onclick = () => {
      state.provider = card.dataset.id;
      $$('.engine-card', box).forEach(c => c.classList.toggle('active', c === card));
      applyEnginePanels();
    };
  });
}

function applyEnginePanels() {
  $$('.engine-config').forEach(block => {
    block.classList.toggle('hidden', block.dataset.engine !== state.provider);
  });
  const spec = state.catalog.find(p => p.id === state.provider);
  const label = spec ? spec.name : state.provider;
  $('#dzEngine').textContent = label;

  if (spec) {
    const userSeg = Number($('#segmentMinutes').value);
    const real = Math.min(userSeg, spec.max_segment_minutes);
    $('#segmentValue').textContent = real < userSeg
      ? `${real} 分钟（受引擎限制）` : `${userSeg} 分钟`;
  }
}

/* ---------------- 健康与配置 ---------------- */

async function loadHealth() {
  try {
    const r = await fetch('/api/health');
    const d = await r.json();
    const chip = $('#chipEngine');
    if (d.ffmpeg) {
      chip.textContent = 'FFmpeg 就绪';
      chip.className = 'chip ok';
    } else {
      chip.textContent = '缺少 FFmpeg';
      chip.className = 'chip err';
      chip.title = d.warning || '';
    }
  } catch (e) {
    $('#chipEngine').textContent = '服务未连接';
    $('#chipEngine').className = 'chip err';
  }
}

async function loadConfig() {
  const r = await fetch('/api/config');
  const cfg = await r.json();
  state.config = cfg;
  state.provider = cfg.provider || 'volc';
  applyConfigToForm(cfg);
  updateStatusChips(cfg);
  return cfg;
}

function updateStatusChips(cfg) {
  const chipKey = $('#chipKey');
  if (cfg.mock) {
    chipKey.textContent = '演示模式';
    chipKey.className = 'chip warn';
  } else if (cfg.ready) {
    chipKey.textContent = '引擎就绪';
    chipKey.className = 'chip ok';
  } else {
    chipKey.textContent = '未配置凭证';
    chipKey.className = 'chip err';
  }

  // 引擎没配好时在拖拽区下面挂一条提示，不要自动弹设置抽屉抢焦点
  $('#setupNote').classList.toggle('hidden', !!cfg.ready || !!cfg.mock);
}

function applyConfigToForm(cfg) {
  const P = cfg.providers || {};

  // 火山
  const v = P.volc || {};
  setSegValue('#authMode', v.auth_mode || 'api_key');
  setSegValue('#volcMode', v.mode || 'standard');
  toggleAuthFields(v.auth_mode || 'api_key');
  updateModeTip((v.mode || 'standard') === 'flash');
  $('#modelVersion').value = (v.resource_id || '').includes('seedasr') ? '20' : '10';
  $('#volcApiKey').value = '';
  $('#volcApiKey').placeholder = v.api_key ? `已配置 ${v.api_key}（留空不修改）` : '粘贴控制台的 API Key';
  $('#volcAppId').value = v.app_id || '';
  $('#volcAccessKey').value = '';
  $('#volcAccessKey').placeholder = v.access_key ? `已配置 ${v.access_key}（留空不修改）` : '旧版控制台的 Access Token';
  $('#enableItn').checked = !!v.enable_itn;
  $('#enablePunc').checked = !!v.enable_punc;
  $('#enableDdc').checked = !!v.enable_ddc;
  $('#enableSpeaker').checked = !!v.enable_speaker_info;

  // OpenAI 兼容
  const o = P.openai || {};
  $('#oaiApiKey').value = '';
  $('#oaiApiKey').placeholder = o.api_key ? `已配置 ${o.api_key}（留空不修改）` : 'sk-...';
  $('#oaiBaseUrl').value = o.base_url || 'https://api.openai.com/v1';
  $('#oaiModel').value = o.model || 'whisper-1';
  $('#oaiPrompt').value = o.prompt || '';

  // 百炼
  const d = P.dashscope || {};
  $('#dsApiKey').value = '';
  $('#dsApiKey').placeholder = d.api_key ? `已配置 ${d.api_key}（留空不修改）` : 'sk-...';
  $('#dsBaseUrl').value = d.base_url || 'https://dashscope.aliyuncs.com/compatible-mode/v1';
  $('#dsModel').value = d.model || 'qwen3-asr-flash';

  // 本地
  const l = P.local || {};
  $('#localModel').value = l.model || 'small';
  $('#localDevice').value = l.device || 'cpu';
  $('#localVad').checked = l.vad !== false;

  // 本地 SenseVoice
  const sv = P.sensevoice || {};
  $('#svThreads').value = sv.threads || 0;
  $('#svItn').checked = sv.enable_itn !== false;
  refreshSenseVoice(cfg.ready_map && cfg.ready_map.sensevoice);

  // 通用
  $('#language').value = cfg.language || '';
  $('#segmentMinutes').value = cfg.segment_minutes || 20;
  $('#includeTimeline').checked = !!cfg.include_timeline;
  $('#mockMode').checked = !!cfg.mock;
  const outs = cfg.outputs || ['md', 'docx'];
  $('#outMd').checked = outs.includes('md');
  $('#outDocx').checked = outs.includes('docx');
  $('#outTxt').checked = outs.includes('txt');

  renderEngines(cfg.ready_map);
  applyEnginePanels();
}

function setSegValue(sel, value) {
  $$(`${sel} button`).forEach(b => b.classList.toggle('active', b.dataset.value === value));
}
function segValue(sel) {
  const b = $(`${sel} button.active`);
  return b ? b.dataset.value : null;
}
function toggleAuthFields(mode) {
  const apiKey = mode === 'api_key';
  $('#fieldApiKey').classList.toggle('hidden', !apiKey);
  $('#fieldLegacy').classList.toggle('hidden', apiKey);
}
function updateModeTip(isFlash) {
  $('#modeTip').textContent = isFlash
    ? '极速版一次请求直接返回，单段不超过 2 小时 / 100MB，适合快速出结果。'
    : '标准版支持 5 小时长音频，异步提交后轮询结果，稳定但稍慢。';
}
function computeResource(mode, ver) {
  if (mode === 'flash') return ver === '20' ? 'volc.seedasr.auc_turbo' : 'volc.bigasr.auc_turbo';
  return ver === '20' ? 'volc.seedasr.auc' : 'volc.bigasr.auc';
}

async function saveConfig() {
  const outputs = [];
  if ($('#outMd').checked) outputs.push('md');
  if ($('#outDocx').checked) outputs.push('docx');
  if ($('#outTxt').checked) outputs.push('txt');
  if (!outputs.length) { toast('至少要选择一种导出格式', true); return; }

  const volc = {
    auth_mode: segValue('#authMode'),
    mode: segValue('#volcMode'),
    resource_id: computeResource(segValue('#volcMode'), $('#modelVersion').value),
    enable_itn: $('#enableItn').checked,
    enable_punc: $('#enablePunc').checked,
    enable_ddc: $('#enableDdc').checked,
    enable_speaker_info: $('#enableSpeaker').checked,
    app_id: $('#volcAppId').value.trim(),
  };
  if ($('#volcApiKey').value.trim()) volc.api_key = $('#volcApiKey').value.trim();
  if ($('#volcAccessKey').value.trim()) volc.access_key = $('#volcAccessKey').value.trim();

  const openai = {
    base_url: $('#oaiBaseUrl').value.trim(),
    model: $('#oaiModel').value.trim() || 'whisper-1',
    prompt: $('#oaiPrompt').value.trim(),
  };
  if ($('#oaiApiKey').value.trim()) openai.api_key = $('#oaiApiKey').value.trim();

  const dashscope = {
    base_url: $('#dsBaseUrl').value.trim(),
    model: $('#dsModel').value.trim() || 'qwen3-asr-flash',
  };
  if ($('#dsApiKey').value.trim()) dashscope.api_key = $('#dsApiKey').value.trim();

  const local = {
    model: $('#localModel').value,
    device: $('#localDevice').value,
    vad: $('#localVad').checked,
  };

  const sensevoice = {
    threads: Number($('#svThreads').value) || 0,
    enable_itn: $('#svItn').checked,
  };

  const payload = {
    provider: state.provider,
    language: $('#language').value,
    segment_minutes: Number($('#segmentMinutes').value),
    include_timeline: $('#includeTimeline').checked,
    outputs: outputs,
    mock: $('#mockMode').checked,
    providers: { volc, openai, dashscope, local, sensevoice },
  };

  const r = await fetch('/api/config', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const d = await r.json();
  if (!d.ok) { toast('保存失败', true); return; }

  state.config = d.config;
  state.provider = d.config.provider;
  applyConfigToForm(d.config);
  updateStatusChips(d.config);
  toast('设置已保存');
  closeDrawer();
}

/* ---------------- 上传 ---------------- */

function uploadFiles(files) {
  if (!files || !files.length) return;

  const fd = new FormData();
  for (const f of files) fd.append('files', f);

  const bar = $('#dzProgress');
  bar.classList.add('show');
  bar.firstElementChild.style.width = '0%';

  const xhr = new XMLHttpRequest();
  xhr.open('POST', '/api/upload');
  xhr.upload.onprogress = (e) => {
    if (e.lengthComputable) {
      bar.firstElementChild.style.width = `${(e.loaded / e.total) * 100}%`;
    }
  };
  xhr.onload = () => {
    bar.firstElementChild.style.width = '100%';
    setTimeout(() => { bar.classList.remove('show'); bar.firstElementChild.style.width = '0%'; }, 500);

    let d = {};
    try { d = JSON.parse(xhr.responseText); } catch (e) { /* ignore */ }

    if (xhr.status >= 400 || !d.ok) {
      toast(d.error || `上传失败（${xhr.status}）`, true);
      return;
    }
    (d.rejected || []).forEach(r => toast(`${r.name}：${r.reason}`, true));
    if (d.accepted && d.accepted.length) {
      toast(`已加入队列：${d.accepted.length} 个文件`);
      refreshJobs();
    }
  };
  xhr.onerror = () => {
    bar.classList.remove('show');
    toast('上传出错，请检查服务是否仍在运行', true);
  };
  xhr.send(fd);
}

/* ---------------- 任务列表 ---------------- */

async function refreshJobs() {
  try {
    const r = await fetch('/api/jobs');
    state.jobs = await r.json();
    renderJobs();
  } catch (e) { /* 服务重启中 */ }
}

function renderJobs() {
  const container = $('#jobs');
  $('#taskCount').textContent = state.jobs.length;
  $('#emptyState').classList.toggle('show', state.jobs.length === 0);

  const seen = new Set();
  for (const job of state.jobs) {
    seen.add(job.id);
    let el = state.cards.get(job.id);
    if (!el) {
      el = buildCard();
      state.cards.set(job.id, el);
      container.appendChild(el);
    }
    updateCard(el, job);
  }
  for (const [id, el] of state.cards) {
    if (!seen.has(id)) { el.remove(); state.cards.delete(id); }
  }
}

function buildCard() {
  const el = document.createElement('div');
  el.className = 'job';
  el.innerHTML = `
    <div class="job-icon"><span class="ext"></span></div>
    <div class="job-main">
      <div class="job-title"><span class="job-name"></span></div>
      <div class="job-meta"></div>
      <div class="bar"><i></i></div>
      <div class="job-status">
        <span class="badge"></span>
        <span class="stage"></span>
      </div>
      <div class="job-error hidden"></div>
    </div>
    <div class="job-actions"></div>
  `;
  return el;
}

function updateCard(el, job) {
  $('.job-name', el).textContent = job.filename;
  $('.ext', el).textContent = (job.filename.split('.').pop() || '?').slice(0, 4);

  const metaParts = [];
  if (job.size > 0) metaParts.push(job.size_text);
  if (job.duration > 0) metaParts.push(job.duration_text);
  if (job.segments_total > 0) metaParts.push(`${job.segments_done}/${job.segments_total} 段`);
  if (job.engine) metaParts.push(job.engine);
  if (job.estimated_time) metaParts.push('时间轴估算');
  $('.job-meta', el).innerHTML = metaParts.map(esc).join('<span class="dot"></span>');

  const bar = $('.bar', el);
  $('.bar > i', el).style.width = `${Math.max(2, Math.min(100, job.progress || 0))}%`;
  bar.className = 'bar';
  if (job.status === 'done') bar.classList.add('done');
  else if (job.status === 'failed') bar.classList.add('failed');
  else bar.classList.add('striped');

  const badge = $('.badge', el);
  badge.textContent = job.status_label;
  badge.className = 'badge';
  if (job.status === 'done') badge.classList.add('done');
  else if (job.status === 'failed') badge.classList.add('failed');
  else if (job.status === 'canceled') badge.classList.add('canceled');
  else badge.classList.add('working');

  $('.stage', el).textContent = (job.status === 'done' || job.status === 'canceled')
    ? (job.elapsed ? `用时 ${job.elapsed.toFixed(0)} 秒` : '')
    : (job.stage || '');

  const icon = $('.job-icon', el);
  icon.className = 'job-icon';
  if (job.status === 'done') icon.classList.add('done');
  if (job.status === 'failed') icon.classList.add('failed');

  const errEl = $('.job-error', el);
  if (job.error) {
    errEl.textContent = job.error;
    errEl.classList.remove('hidden');
  } else {
    errEl.classList.add('hidden');
  }

  renderActions(el, job);
}

function renderActions(el, job) {
  const box = $('.job-actions', el);
  const sig = `${job.status}|${(job.outputs || []).join(',')}`;
  if (box.dataset.sig === sig) return;
  box.dataset.sig = sig;
  box.innerHTML = '';

  const mkBtn = (label, onClick, icon) => {
    const b = document.createElement('button');
    b.className = 'dl';
    b.innerHTML = `${icon || ''}<span>${label}</span>`;
    b.onclick = onClick;
    return b;
  };

  if (job.status === 'done') {
    (job.outputs || []).forEach(fmt => {
      box.appendChild(mkBtn(FMT_LABEL[fmt] || fmt, () => {
        window.location.href = `/api/download/${job.id}/${fmt}`;
      }, DL_ICON));
    });
    box.appendChild(mkBtn('压缩包', () => {
      window.location.href = `/api/download/${job.id}/zip`;
    }));
    box.appendChild(mkBtn('预览', () => openPreview(job)));

    const del = document.createElement('button');
    del.className = 'icon-btn';
    del.textContent = '✕';
    del.title = '删除任务';
    del.onclick = () => deleteJob(job.id);
    box.appendChild(del);
    return;
  }

  if (job.status === 'failed' || job.status === 'canceled') {
    box.appendChild(mkBtn('重试', () => retryJob(job.id)));
    const del = document.createElement('button');
    del.className = 'icon-btn';
    del.textContent = '✕';
    del.onclick = () => deleteJob(job.id);
    box.appendChild(del);
    return;
  }

  box.appendChild(mkBtn('取消', () => cancelJob(job.id)));
}

async function cancelJob(id) {
  await fetch(`/api/jobs/${id}/cancel`, { method: 'POST' });
  refreshJobs();
}
async function retryJob(id) {
  const r = await fetch(`/api/jobs/${id}/retry`, { method: 'POST' });
  const d = await r.json();
  if (!d.ok) { toast(d.error || '重试失败', true); return; }
  refreshJobs();
}
async function deleteJob(id) {
  // 删任务会连输出文件一起删，先问一句：半小时转出来的东西不该被误点带走
  const job = state.jobs.find(j => j.id === id);
  const ok = await confirmDialog(
    '删除任务',
    `「${job ? job.filename : '这个任务'}」的转写结果和输出文件会被一起删掉，删了不能恢复。`,
    '删除'
  );
  if (!ok) return;

  const el = state.cards.get(id);
  if (el) { el.style.opacity = '0'; el.style.transform = 'translateX(-8px)'; }
  await fetch(`/api/jobs/${id}`, { method: 'DELETE' });
  setTimeout(refreshJobs, 160);
}

/* 轻量确认框：和「手动复制」用同一套弹层骨架，不额外引入组件库 */
function confirmDialog(title, text, okLabel) {
  return new Promise(resolve => {
    const wrap = document.createElement('div');
    wrap.className = 'modal open';
    wrap.innerHTML = `
      <div class="modal-card slim">
        <div class="modal-head"><h2>${esc(title)}</h2></div>
        <div class="modal-body"><p class="tip" style="margin:0">${esc(text)}</p></div>
        <div class="modal-foot">
          <button class="btn btn-line" data-act="no">取消</button>
          <button class="btn btn-solid" data-act="yes">${esc(okLabel || '确定')}</button>
        </div>
      </div>`;
    document.body.appendChild(wrap);

    const done = v => { wrap.remove(); resolve(v); };
    wrap.querySelector('[data-act="yes"]').onclick = () => done(true);
    wrap.querySelector('[data-act="no"]').onclick = () => done(false);
    wrap.onclick = e => { if (e.target === wrap) done(false); };
    document.addEventListener('keydown', function onKey(e) {
      if (e.key === 'Escape') { done(false); document.removeEventListener('keydown', onKey); }
    });
    wrap.querySelector('[data-act="yes"]').focus();
  });
}

/* ---------------- 预览 ---------------- */

async function openPreview(job) {
  state.previewTab = 'rendered';
  $('#modalTitle').textContent = job.filename.replace(/\.[^.]+$/, '');
  $('#modalMeta').textContent =
    `${job.duration_text} · ${job.segments_total} 段 · ${job.engine || ''}`;
  $$('#modalTabs button').forEach(b => b.classList.toggle('active', b.dataset.tab === 'rendered'));
  $('#modalBody').innerHTML = '<p class="empty-tip sm">加载中…</p>';
  $('#modal').classList.add('open');
  $('#scrim').classList.add('show');

  const r = await fetch(`/api/jobs/${job.id}/preview`);
  const d = await r.json();
  if (!d.ok) {
    $('#modalBody').innerHTML = `<p class="empty-tip sm" style="color:var(--err)">${esc(d.error || '无法读取结果')}</p>`;
    return;
  }
  state.previewContent = d.content || '';
  if (d.format === 'docx') {
    $('#modalBody').innerHTML = '<p class="empty-tip sm">Word 文件不支持在线预览，请直接下载查看。</p>';
    return;
  }
  paintPreview();
}

function paintPreview() {
  const body = $('#modalBody');
  body.innerHTML = state.previewTab === 'raw'
    ? `<pre>${esc(state.previewContent)}</pre>`
    : `<div class="md">${renderMarkdown(state.previewContent)}</div>`;
}

function renderMarkdown(md) {
  const lines = String(md).replace(/\r\n/g, '\n').split('\n');
  const out = [];
  let inFront = false, frontDone = false, inList = false, quote = [];

  const flushQuote = () => {
    if (quote.length) {
      out.push(`<blockquote>${quote.map(esc).join('<br>')}</blockquote>`);
      quote = [];
    }
  };
  const flushList = () => {
    if (inList) { out.push('</ul>'); inList = false; }
  };

  for (const raw of lines) {
    if (!frontDone && raw.trim() === '---') {
      if (!inFront) { inFront = true; continue; }
      inFront = false; frontDone = true; continue;
    }
    if (inFront) continue;

    const line = raw.trim();
    if (!line) { flushQuote(); flushList(); continue; }

    if (line.startsWith('## ')) { flushQuote(); flushList(); out.push(`<h2>${esc(line.slice(3))}</h2>`); continue; }
    if (line.startsWith('# ')) { flushQuote(); flushList(); out.push(`<h1>${esc(line.slice(2))}</h1>`); continue; }
    if (line.startsWith('> ') || line === '>') { flushList(); if (line.length > 1) quote.push(line.slice(2)); continue; }
    if (line.startsWith('- ')) {
      flushQuote();
      if (!inList) { out.push('<ul>'); inList = true; }
      let content = line.slice(2);
      let stamp = '';
      const m = content.match(/^`(\[[^\]]+\][^`]*)`\s*(.*)$/);
      if (m) { stamp = `<code>${esc(m[1].trim())}</code>`; content = m[2]; }
      out.push(`<li>${stamp}<span>${esc(content)}</span></li>`);
      continue;
    }
    if (line === '---') { flushQuote(); flushList(); out.push('<hr>'); continue; }

    flushQuote(); flushList();
    out.push(`<p>${esc(line)}</p>`);
  }
  flushQuote(); flushList();
  return out.join('');
}

/* 浏览器不给剪贴板权限时退化成手动复制 */
function copyFallback(text) {
  const wrap = document.createElement('div');
  wrap.className = 'modal open';
  wrap.innerHTML = `
    <div class="modal-card slim">
      <div class="modal-head"><h2>手动复制</h2></div>
      <div class="modal-body">
        <p class="tip" style="margin-top:0">浏览器没允许直接写剪贴板。下面已经全选，按 Ctrl / ⌘ + C 即可。</p>
        <textarea class="copy-area" rows="12" readonly></textarea>
      </div>
      <div class="modal-foot"><button class="btn btn-line" data-act="close">关闭</button></div>
    </div>`;
  document.body.appendChild(wrap);

  const ta = wrap.querySelector('textarea');
  ta.value = text;
  ta.focus();
  ta.select();

  const close = () => wrap.remove();
  wrap.querySelector('[data-act="close"]').onclick = close;
  wrap.onclick = e => { if (e.target === wrap) close(); };
}

/* ---------------- 抽屉 ---------------- */

function openDrawer() {
  $('#drawer').classList.add('open');
  $('#scrim').classList.add('show');
  $('#drawer').setAttribute('aria-hidden', 'false');
}
function closeDrawer() {
  $('#drawer').classList.remove('open');
  if (!$('#modal').classList.contains('open')) $('#scrim').classList.remove('show');
  $('#drawer').setAttribute('aria-hidden', 'true');
}
function closeModal() {
  $('#modal').classList.remove('open');
  if (!$('#drawer').classList.contains('open')) $('#scrim').classList.remove('show');
}

/* ---------------- 事件 ---------------- */

function bind() {
  const dz = $('#dropzone');
  const input = $('#fileInput');

  dz.addEventListener('click', () => input.click());
  input.addEventListener('change', () => { uploadFiles(input.files); input.value = ''; });

  ['dragenter', 'dragover'].forEach(evt => dz.addEventListener(evt, e => {
    e.preventDefault(); e.stopPropagation(); dz.classList.add('dragover');
  }));
  ['dragleave', 'drop'].forEach(evt => dz.addEventListener(evt, e => {
    e.preventDefault(); e.stopPropagation();
    if (evt === 'dragleave' && dz.contains(e.relatedTarget)) return;
    dz.classList.remove('dragover');
  }));
  dz.addEventListener('drop', e => uploadFiles(Array.from(e.dataTransfer.files || [])));

  window.addEventListener('dragover', e => e.preventDefault());
  window.addEventListener('drop', e => e.preventDefault());

  $('#btnSettings').onclick = openDrawer;
  $('#btnSetup').onclick = openDrawer;
  $('#btnCloseDrawer').onclick = closeDrawer;
  $('#scrim').onclick = () => { closeDrawer(); closeModal(); };
  $('#btnCloseModal').onclick = closeModal;
  $('#btnSave').onclick = saveConfig;
  $('#btnCopyMd').onclick = async () => {
    if (!state.previewContent) { toast('还没有可复制的内容'); return; }
    const ok = await copyText(state.previewContent);
    if (ok) toast('已复制全文');
    else copyFallback(state.previewContent);
  };

  $$('#authMode button').forEach(b => b.onclick = () => {
    setSegValue('#authMode', b.dataset.value);
    toggleAuthFields(b.dataset.value);
  });
  $$('#volcMode button').forEach(b => b.onclick = () => {
    setSegValue('#volcMode', b.dataset.value);
    updateModeTip(b.dataset.value === 'flash');
  });

  $('#segmentMinutes').addEventListener('input', applyEnginePanels);

  $$('#modalTabs button').forEach(b => b.onclick = () => {
    state.previewTab = b.dataset.tab;
    $$('#modalTabs button').forEach(x => x.classList.toggle('active', x === b));
    paintPreview();
  });

  $('#btnDownloadAll').onclick = () => {
    const done = state.jobs.filter(j => j.status === 'done' && j.outputs.length);
    if (!done.length) { toast('还没有已完成的任务'); return; }
    window.location.href = '/api/download-all';
  };

  $('#btnClearDone').onclick = async () => {
    const targets = state.jobs.filter(j => ['done', 'failed', 'canceled'].includes(j.status));
    if (!targets.length) { toast('没有可清除的任务'); return; }
    const ok = await confirmDialog(
      '清除已结束的任务',
      `会删除 ${targets.length} 个任务以及它们的输出文件，删了不能恢复。还没下载的先下下来。`,
      '清除'
    );
    if (!ok) return;
    for (const j of targets) await fetch(`/api/jobs/${j.id}`, { method: 'DELETE' });
    refreshJobs();
    toast(`已清除 ${targets.length} 个任务`);
  };

  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') { closeDrawer(); closeModal(); }
  });

  // 滚起来才给顶栏加分隔线，静止时保持干净
  const topbar = $('.topbar');
  const onScroll = () => topbar.classList.toggle('stuck', window.scrollY > 4);
  window.addEventListener('scroll', onScroll, { passive: true });
  onScroll();
}

/* ---------------- 启动 ---------------- */

async function init() {
  bind();
  await loadHealth();
  await loadCatalog();
  await loadConfig();
  await refreshJobs();

  state.polling = setInterval(() => {
    if (document.hidden) return;
    refreshJobs();
  }, 1500);
}

document.addEventListener('DOMContentLoaded', init);

/* ---------------- 本地 SenseVoice ---------------- */

async function refreshSenseVoice(ready) {
  const dot = $('#svDot');
  const text = $('#svStateText');
  if (!dot || !text) return;

  if (ready) {
    dot.className = 'sv-dot ok';
    text.textContent = '模型已就绪，可以直接转录';
    $('#btnSvDownload').classList.add('hidden');
    return;
  }

  dot.className = 'sv-dot warn';
  text.textContent = '模型还没下载';
  $('#btnSvDownload').classList.remove('hidden');

  try {
    const r = await fetch('/api/local/status');
    const d = await r.json();
    if (d.asr && d.asr.ready) {
      dot.className = 'sv-dot ok';
      text.textContent = '模型已就绪，可以直接转录';
      $('#btnSvDownload').classList.add('hidden');
    } else if (d.asr && d.asr.installed_pkg === false) {
      dot.className = 'sv-dot bad';
      text.textContent = '缺少依赖，先运行 pip install sherpa-onnx';
    }
  } catch (e) { /* 忽略 */ }
}

async function downloadSenseVoice() {
  const btn = $('#btnSvDownload');
  btn.disabled = true;
  btn.textContent = '下载中…';

  try {
    await fetch('/api/local/asr/download', { method: 'POST' });
  } catch (e) {
    btn.disabled = false;
    btn.textContent = '重新下载';
    toast('无法开始下载', true);
    return;
  }

  const timer = setInterval(async () => {
    try {
      const r = await fetch('/api/local/asr/download');
      const d = await r.json();
      const job = d.job || {};
      if (job.status === 'running') {
        $('#svStateText').textContent = `${job.stage || '下载中'} ${Math.round(job.pct || 0)}%`;
        return;
      }
      clearInterval(timer);
      btn.disabled = false;
      btn.textContent = '下载模型（约 230MB）';
      if (job.status === 'done') {
        toast('本地模型已就绪');
        refreshSenseVoice(true);
      } else if (job.status === 'failed') {
        toast(job.error || '下载失败', true);
        refreshSenseVoice(false);
      }
    } catch (e) { /* 下一轮再试 */ }
  }, 900);
}

document.addEventListener('click', e => {
  if (e.target.id === 'btnSvDownload') downloadSenseVoice();
});
