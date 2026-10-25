/* 不听课 · 前端交互
   视图有三种：欢迎页 / 课堂详情 / 课程视图，三者互斥。 */

const state = {
  courses: [],
  lessons: [],
  unassigned: 0,          // 未分类课堂数（后端单独统计）
  lesson: null,
  course: null,
  view: 'welcome',
  mode: 'extreme',
  contents: {},
  sumContent: null,
  sumContentCourse: '',   // 缓存属于哪门课的汇总正文，切课要作废
  config: null,
  local: null,
  polling: null,
  sumPolling: null,
  search: '',
  transcriptFor: '',
  transcriptText: null,
  collapsed: new Set(JSON.parse(localStorage.getItem('bt.collapsed') || '[]')),
  pendingMode: null,
};

const RUNNING = ['parsing', 'transcribing', 'analyzing', 'rendering'];
const PROVIDER_LABEL = {
  volc: '火山引擎 · 豆包',
  openai: 'OpenAI Whisper',
  dashscope: '阿里云百炼',
  sensevoice: '本地 SenseVoice',
  local: '本地 faster-whisper',
};

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

/* ---------------- 基础工具 ---------------- */

function esc(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function toast(msg, kind = '') {
  const box = $('#toasts');
  const el = document.createElement('div');
  el.className = `toast ${kind}`;
  el.textContent = msg;
  box.appendChild(el);
  requestAnimationFrame(() => el.classList.add('show'));
  setTimeout(() => {
    el.classList.remove('show');
    setTimeout(() => el.remove(), 240);
  }, kind === 'err' ? 5200 : 3200);
}

function fmtDur(sec) {
  const t = Math.max(0, Math.round(Number(sec) || 0));
  const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = t % 60;
  const p = n => String(n).padStart(2, '0');
  return h ? `${h}:${p(m)}:${p(s)}` : `${m}:${p(s)}`;
}

function fmtSize(b) {
  b = Number(b) || 0;
  if (b >= 1073741824) return (b / 1073741824).toFixed(2) + ' GB';
  if (b >= 1048576) return (b / 1048576).toFixed(1) + ' MB';
  if (b >= 1024) return Math.round(b / 1024) + ' KB';
  return b + ' B';
}

function timeAgo(ts) {
  if (!ts) return '';
  const d = Date.now() / 1000 - ts;
  if (d < 60) return '刚刚';
  if (d < 3600) return `${Math.floor(d / 60)} 分钟前`;
  if (d < 86400) return `${Math.floor(d / 3600)} 小时前`;
  if (d < 86400 * 30) return `${Math.floor(d / 86400)} 天前`;
  const dt = new Date(ts * 1000);
  return `${dt.getMonth() + 1}-${dt.getDate()}`;
}

async function api(url, opts = {}) {
  try {
    const r = await fetch(url, opts);
    let data;
    try {
      data = await r.json();
    } catch (e) {
      return { ok: false, error: `服务返回异常（HTTP ${r.status}）` };
    }
    if (!r.ok && data && data.ok !== false) data = { ok: false, error: `HTTP ${r.status}` };
    return data;
  } catch (e) {
    return { ok: false, error: '连不上本地服务，确认程序窗口还开着' };
  }
}

const DL_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4.5v10m0 0 3.5-3.5M12 14.5 8.5 11"/><path d="M5 19h14"/></svg>';

/* 复制到剪贴板：把复习指导直接粘进 Notion / 飞书 / 备忘录，比下载文件顺手 */
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

async function copyMarkdown() {
  const l = state.lesson;
  const text = l ? state.contents[state.mode] : null;
  if (text == null) { toast('还没有可复制的内容', 'err'); return; }
  const ok = await copyText(text);
  if (ok) toast('已复制 Markdown 原文', 'ok');
  else copyFallbackDialog(text);
}

/* 浏览器不给剪贴板权限时（非 https、无用户手势、隐私模式），
   退化成「自己按 Ctrl+C」，总比一句冷冰冰的失败提示强。 */
function copyFallbackDialog(text) {
  const wrap = document.createElement('div');
  wrap.className = 'modal open';
  wrap.innerHTML = `
    <div class="modal-card narrow">
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
  document.addEventListener('keydown', function esc(e) {
    if (e.key === 'Escape') { close(); document.removeEventListener('keydown', esc); }
  });
}

/* ---------------- 数据加载 ---------------- */

async function loadAll(keep) {
  const [coursesRes, lessonsRes] = await Promise.all([
    api('/api/courses'),
    api('/api/lessons'),
  ]);

  state.courses = (coursesRes && coursesRes.courses) || [];
  state.unassigned = (coursesRes && coursesRes.unassigned) || 0;
  state.lessons = Array.isArray(lessonsRes) ? lessonsRes : [];

  renderTree();

  const target = keep || (state.lesson && state.lesson.id);
  const targetCourse = state.course && state.course.id;

  if (state.view === 'course' && targetCourse) {
    const still = state.courses.find(c => c.id === targetCourse);
    if (still) { await openCourse(targetCourse, true); return; }
  }

  if (target && state.lessons.some(l => l.id === target)) {
    await openLesson(target, true);
  } else if (state.view === 'welcome' && state.lessons.length) {
    // 有内容时直接打开最近的一堂课，省一次点击
    await openLesson(state.lessons[0].id, true);
  } else if (state.view === 'lesson') {
    // 当前课堂被删了
    showWelcome();
  } else if (!state.lessons.length) {
    showWelcome();
  }
}

/* ---------------- 侧栏树 ---------------- */

function lessonsOf(cid) {
  return state.lessons.filter(l => (l.course_id || '') === cid);
}

function matchesSearch(lesson) {
  if (!state.search) return true;
  const q = state.search.toLowerCase();
  // search_text 由后端拼好：课堂名 + 主题 + 全部考点标题
  return (lesson.search_text || lesson.name || '').toLowerCase().includes(q);
}

function lessonRow(l) {
  const active = state.lesson && state.lesson.id === l.id && state.view === 'lesson' ? ' active' : '';
  let dot = '';
  if (RUNNING.includes(l.status)) dot = 'running';
  else if (l.status === 'done') dot = l.stale ? 'stale' : 'done';
  else if (l.status === 'failed') dot = 'failed';

  const bits = [];
  if (l.status === 'done') bits.push(`${l.points || 0} 考点`);
  else bits.push(l.status_label);

  return `
    <div class="lesson-item${active}" data-lid="${l.id}">
      <div class="li-name" title="${esc(l.name)}">${esc(l.name)}</div>
      <div class="li-meta">
        <i class="li-dot ${dot}"></i>
        <span>${esc(bits.join(' · '))}</span>
        <span class="li-time">${timeAgo(l.updated)}</span>
      </div>
      <button class="li-del" data-del="${l.id}" title="删除">✕</button>
    </div>`;
}

function courseNode(course, items, isUnassigned) {
  const cid = isUnassigned ? '' : course.id;
  const collapsed = state.collapsed.has(cid || '__none__') ? ' collapsed' : '';
  const active = state.course && state.course.id === cid && state.view === 'course' ? ' active' : '';
  const name = isUnassigned ? '未分类' : course.name;
  const color = isUnassigned ? 'var(--ink-4)' : (course.color || 'var(--accent)');
  const filtered = state.search ? items.filter(matchesSearch) : items;

  if (state.search && !filtered.length && !(state.search && name.toLowerCase().includes(state.search.toLowerCase()))) {
    return '';
  }

  return `
    <div class="course-node${collapsed}" data-cid="${esc(cid)}">
      <div class="node-course${active}" data-open="${esc(cid)}">
        <span class="caret">▾</span>
        <span class="course-dot" style="background:${color}"></span>
        <span class="node-name" title="${esc(name)}">${esc(name)}</span>
        <span class="node-count">${items.length}</span>
        ${isUnassigned ? '' : `<button class="node-add" data-add="${esc(cid)}" title="在这门课下新建课堂">+</button>`}
      </div>
      <div class="node-lessons">
        ${filtered.map(lessonRow).join('')}
        <div class="li-new" data-add="${esc(cid)}">＋ 新建课堂</div>
      </div>
    </div>`;
}

function renderTree() {
  const box = $('#tree');
  const parts = [];

  for (const course of state.courses) {
    parts.push(courseNode(course, lessonsOf(course.id), false));
  }

  const loose = lessonsOf('');
  if (loose.length || !state.courses.length) {
    parts.push(courseNode({ name: '未分类' }, loose, true));
  }

  const html = parts.join('');
  if (!html.trim()) {
    box.innerHTML = '<p class="tree-empty">没有匹配的课堂</p>';
    return;
  }
  box.innerHTML = html;

  $$('.node-course', box).forEach(el => {
    el.onclick = e => {
      if (e.target.closest('[data-add]')) return;
      const cid = el.dataset.open;
      if (cid) { openCourse(cid); return; }
      // 未分类没有课程页，点一下只做折叠展开
      const node = el.closest('.course-node');
      node.classList.toggle('collapsed');
      const key = node.dataset.cid || '__none__';
      if (node.classList.contains('collapsed')) state.collapsed.add(key);
      else state.collapsed.delete(key);
      localStorage.setItem('bt.collapsed', JSON.stringify([...state.collapsed]));
    };
  });

  $$('.caret', box).forEach(el => {
    el.onclick = e => {
      e.stopPropagation();
      const node = el.closest('.course-node');
      const cid = node.dataset.cid || '__none__';
      node.classList.toggle('collapsed');
      if (node.classList.contains('collapsed')) state.collapsed.add(cid);
      else state.collapsed.delete(cid);
      localStorage.setItem('bt.collapsed', JSON.stringify([...state.collapsed]));
    };
  });

  $$('.lesson-item', box).forEach(el => {
    el.onclick = e => {
      if (e.target.closest('[data-del]')) return;
      openLesson(el.dataset.lid);
    };
  });

  $$('[data-del]', box).forEach(btn => {
    btn.onclick = async e => {
      e.stopPropagation();
      const id = btn.dataset.del;
      const lesson = state.lessons.find(l => l.id === id);
      const ok = await confirmDialog(
        '删除这堂课？',
        `「${lesson ? lesson.name : '未命名'}」的讲义、录音和已生成的结果都会一起删掉，无法恢复。`,
        '删除'
      );
      if (!ok) return;
      await api(`/api/lessons/${id}`, { method: 'DELETE' });
      stopPolling();
      if (state.lesson && state.lesson.id === id) {
        state.lesson = null;
        showWelcome();
      }
      toast('已删除');
      loadAll();
    };
  });

  $$('[data-add]', box).forEach(btn => {
    btn.onclick = e => {
      e.stopPropagation();
      openNewModal('lesson', btn.dataset.add || '');
    };
  });
}

/* ---------------- 视图切换 ---------------- */

function showWelcome() {
  state.view = 'welcome';
  state.lesson = null;
  state.course = null;
  $('#welcome').classList.remove('hidden');
  $('#detail').classList.add('hidden');
  $('#courseView').classList.add('hidden');
  stopPolling();
  stopSumPolling();
  renderTree();
}

function showDetail() {
  state.view = 'lesson';
  $('#welcome').classList.add('hidden');
  $('#detail').classList.remove('hidden');
  $('#courseView').classList.add('hidden');
}

function showCourseView() {
  state.view = 'course';
  $('#welcome').classList.add('hidden');
  $('#detail').classList.add('hidden');
  $('#courseView').classList.remove('hidden');
}

/* ---------------- 课堂详情 ---------------- */

async function openLesson(id, silent = false) {
  const d = await api(`/api/lessons/${id}`);
  if (!d.ok) {
    if (!silent) toast(d.error || '课堂不存在', 'err');
    return;
  }
  const changed = !state.lesson || state.lesson.id !== id;

  state.lesson = d.lesson;
  state.course = state.courses.find(c => c.id === (d.lesson.course_id || '')) || null;
  if (changed) {
    state.contents = {};
    state.mode = state.pendingMode || 'extreme';
    state.pendingMode = null;
  }

  showDetail();
  renderDetail();

  if (RUNNING.includes(state.lesson.status)) startPolling();
  else stopPolling();
}

function renderDetail() {
  const l = state.lesson;
  if (!l) return;

  const courseName = l.course_id
    ? (state.courses.find(c => c.id === l.course_id) || {}).name
    : '未分类';
  const caret = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M6 9l6 6 6-6"/></svg>';
  $('#crumb').innerHTML =
    `<a href="#" id="crumbCourse">${esc(courseName)}</a>` +
    `<button class="crumb-pick" id="crumbPick" title="归到别的课程">${caret}</button>`;

  $('#lessonName').textContent = l.name;

  const meta = [];
  if (l.materials.length) meta.push(`讲义 ${l.materials.length} 份`);
  if (l.audios.length) meta.push(`录音 ${l.audios.length} 段`);
  if (l.duration > 0) meta.push(`共 ${fmtDur(l.duration)}`);
  if (l.points) meta.push(`${l.points} 个考点`);
  meta.push(l.status_label);
  if (l.stale) meta.push('结果待更新');
  $('#lessonMeta').textContent = meta.join(' · ');

  $('#staleNotice').classList.toggle('hidden', !l.stale);

  renderRunPanel();
  renderMaterials();
  renderAudios();
  renderResult();
  renderTranscript();
  renderTree();
}

/* 转录全文：默认收起，展开时才去取，长课堂也不会拖慢首屏 */
function renderTranscript() {
  const l = state.lesson;
  const show = !!l.has_transcript;
  $('#transcript').classList.toggle('hidden', !show);
  if (!show) return;

  if (state.transcriptFor !== l.id) {
    state.transcriptFor = l.id;
    state.transcriptText = null;
    $('#transcriptBody').classList.add('hidden');
    $('#transcriptBody').textContent = '';
    $('#btnTranscript').setAttribute('aria-expanded', 'false');
  }
  $('#transcriptMeta').textContent = '想核对老师原话时看这里';
}

async function toggleTranscript() {
  const l = state.lesson;
  if (!l) return;
  const body = $('#transcriptBody');
  const btn = $('#btnTranscript');

  if (!body.classList.contains('hidden')) {
    body.classList.add('hidden');
    btn.setAttribute('aria-expanded', 'false');
    return;
  }

  body.classList.remove('hidden');
  btn.setAttribute('aria-expanded', 'true');

  if (state.transcriptText === null) {
    body.textContent = '加载中…';
    const d = await api(`/api/lessons/${l.id}/transcript`);
    state.transcriptText = d.ok ? (d.text || '') : (d.error || '读取失败');
    if (!state.transcriptText) state.transcriptText = '这堂课还没有转录文本';
  }
  body.innerHTML = transcriptHtml(state.transcriptText);
}

/* 转录稿是「[00:12:34] 正文」格式，把时间戳拆出来单独排，
   不然整段都是等宽字体，读起来像日志。 */
function transcriptHtml(text) {
  return String(text).split('\n').map(line => {
    const m = line.match(/^\[(\d{2}:\d{2}:\d{2})\]\s*(.*)$/);
    if (m) {
      return `<div class="t-line"><span class="t-ts">${m[1]}</span>` +
        `<span class="t-txt">${esc(m[2])}</span></div>`;
    }
    return `<div class="t-line"><span class="t-txt">${esc(line)}</span></div>`;
  }).join('');
}

function renderRunPanel() {
  const l = state.lesson;
  const panel = $('#runPanel');
  const btn = $('#btnRun');
  const cancel = $('#btnCancel');
  const running = RUNNING.includes(l.status);

  if (running) {
    panel.classList.remove('hidden');
    $('#runStage').textContent = l.stage || '处理中';
    $('#runPct').textContent = `${Math.round(l.progress || 0)}%`;
    $('#runBar').classList.remove('fail');
    $('#runBar').style.width = `${Math.max(2, Math.min(100, l.progress || 0))}%`;
    $('#runError').classList.add('hidden');
  } else if (l.status === 'failed' && l.error) {
    panel.classList.remove('hidden');
    $('#runStage').textContent = '执行失败';
    $('#runPct').textContent = '';
    $('#runBar').style.width = '100%';
    $('#runBar').classList.add('fail');
    $('#runError').textContent = l.error;
    $('#runError').classList.remove('hidden');
  } else {
    panel.classList.add('hidden');
    $('#runBar').classList.remove('fail');
    $('#runBar').style.width = '0';
  }

  cancel.classList.toggle('hidden', !running);
  btn.disabled = running;

  const label = btn.querySelector('span');
  if (label) label.textContent = running ? '分析中…' : (l.guides.length ? '重新分析' : '开始分析');

  const missing = [];
  if (!l.materials.length) missing.push('讲义');
  if (!l.audios.length) missing.push('录音');
  if (missing.length) {
    btn.disabled = true;
    btn.title = `还缺${missing.join('和')}`;
  } else {
    btn.title = l.guides.length ? '会覆盖已有的复习指导' : '';
  }
}

function renderMaterials() {
  const l = state.lesson;
  $('#matCount').textContent = l.materials.length;
  const box = $('#matList');

  if (!l.materials.length) {
    box.innerHTML = '<p class="empty-tip">还没导入讲义</p>';
    return;
  }

  box.innerHTML = l.materials.map(m => {
    const ext = (m.filename.match(/\.([^.]+)$/) || [, ''])[1].toUpperCase();
    let status;
    if (m.status === 'parsed') {
      const bits = [];
      if (m.page_count) bits.push(`${m.page_count} 页`);
      if (m.chars) bits.push(`${m.chars} 字`);
      if (!m.can_shot) bits.push('不支持截图');
      status = bits.join(' · ') || '已解析';
    } else if (m.status === 'failed') {
      status = m.error || '解析失败';
    } else {
      status = '解析中…';
    }
    const warn = (m.warnings || []).length ? `<div class="fi-warn">${esc(m.warnings[0])}</div>` : '';
    return `
      <div class="file-item">
        <i class="fi-icon">${esc(ext.slice(0, 4))}</i>
        <div class="fi-body">
          <div class="fi-name" title="${esc(m.filename)}">${esc(m.filename)}</div>
          <div class="fi-meta ${m.status === 'failed' ? 'bad' : ''}">${esc(status)}</div>
          ${warn}
        </div>
        <button class="fi-act del" data-mid="${m.id}" title="移除">✕</button>
      </div>`;
  }).join('');

  $$('[data-mid]', box).forEach(b => {
    b.onclick = async () => {
      await api(`/api/lessons/${l.id}/materials/${b.dataset.mid}`, { method: 'DELETE' });
      openLesson(l.id);
    };
  });
}

function renderAudios() {
  const l = state.lesson;
  $('#audCount').textContent = l.audios.length;
  const box = $('#audList');

  if (!l.audios.length) {
    box.innerHTML = '<p class="empty-tip">还没导入录音</p>';
    return;
  }

  const play = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M8 5.5l10 6.5-10 6.5z"/></svg>';

  box.innerHTML = l.audios.map(a => {
    const ext = (a.filename.match(/\.([^.]+)$/) || [, ''])[1];
    let status;
    if (a.status === 'done') status = `已转录 · ${fmtDur(a.duration)}`;
    else if (a.status === 'failed') status = a.error || '转录失败';
    else if (a.duration > 0) status = `待转录 · ${fmtDur(a.duration)} · ${fmtSize(a.size)}`;
    else status = fmtSize(a.size);
    return `
      <div class="file-item" data-audio="${a.id}">
        <i class="fi-icon audio">${esc((ext || '').slice(0, 4).toUpperCase())}</i>
        <div class="fi-body">
          <div class="fi-name" title="${esc(a.filename)}">${esc(a.filename)}</div>
          <div class="fi-meta ${a.status === 'failed' ? 'bad' : ''}">${esc(status)}</div>
        </div>
        <button class="fi-act" data-play="${a.id}" title="试听">${play}</button>
        <button class="fi-act del" data-aid="${a.id}" title="移除">✕</button>
      </div>`;
  }).join('');

  $$('[data-aid]', box).forEach(b => {
    b.onclick = async () => {
      await api(`/api/lessons/${l.id}/audios/${b.dataset.aid}`, { method: 'DELETE' });
      openLesson(l.id);
    };
  });

  $$('[data-play]', box).forEach(b => {
    b.onclick = () => togglePlayer(b, l.id);
  });
}

/* 试听：点一下在文件下面展开播放器，再点收起。
   复习时听到「老师这句没听清」，不用切出去找录音文件。 */
function togglePlayer(btn, lessonId) {
  const row = btn.closest('.file-item');
  const existing = row.nextElementSibling;
  if (existing && existing.classList.contains('audio-slot')) {
    existing.remove();
    btn.classList.remove('on');
    return;
  }

  $$('.audio-slot').forEach(el => el.remove());
  $$('[data-play]').forEach(el => el.classList.remove('on'));

  const slot = document.createElement('div');
  slot.className = 'audio-slot';
  const audio = document.createElement('audio');
  audio.controls = true;
  audio.autoplay = true;
  audio.preload = 'metadata';
  audio.src = `/api/lessons/${lessonId}/audios/${btn.dataset.play}/file`;
  slot.appendChild(audio);
  row.after(slot);
  btn.classList.add('on');

  audio.addEventListener('ended', () => {
    slot.remove();
    btn.classList.remove('on');
  });
}

/* ---------------- 结果 ---------------- */

function renderResult() {
  const l = state.lesson;
  const box = $('#result');
  if (!l.guides || !l.guides.length) {
    box.classList.add('hidden');
    return;
  }
  box.classList.remove('hidden');

  const has = m => l.guides.includes(`${m}_md`);
  $$('#modeTabs button').forEach(b => {
    const ok = has(b.dataset.mode);
    b.classList.toggle('active', b.dataset.mode === state.mode);
    b.disabled = !ok;
  });
  if (!has(state.mode)) {
    state.mode = has('detailed') ? 'detailed' : 'extreme';
    $$('#modeTabs button').forEach(b => b.classList.toggle('active', b.dataset.mode === state.mode));
  }

  const formats = ['md', 'docx'].filter(f => l.guides.includes(`${state.mode}_${f}`));
  $('#dlGroup').innerHTML = formats.map(f => `
    <a class="btn btn-line btn-sm" href="/api/lessons/${l.id}/download/${state.mode}/${f}">
      ${DL_ICON}${f === 'md' ? 'Markdown' : 'Word'}
    </a>`).join('');

  loadPreview();
}

async function loadPreview() {
  const l = state.lesson;
  if (!l) return;
  const box = $('#resultBody');

  if (state.contents[state.mode] !== undefined) {
    box.innerHTML = mdToHtml(state.contents[state.mode], l.id);
    return;
  }

  const key = `${l.id}:${state.mode}`;
  if (box.dataset.key === key) return;
  box.dataset.key = key;
  box.innerHTML = '<p class="empty-tip sm">加载中…</p>';

  const d = await api(`/api/lessons/${l.id}/preview?mode=${state.mode}`);
  if (!d.ok) {
    box.innerHTML = `<p class="empty-tip sm">${esc(d.error || '该版本还没有生成')}</p>`;
    return;
  }
  state.contents[state.mode] = d.content || '';
  if (state.lesson && state.lesson.id === l.id && state.mode === d.mode) {
    box.innerHTML = mdToHtml(d.content || '', l.id);
  }
}

/* ---------------- Markdown 渲染 ---------------- */

function inlineHtml(text, lessonId) {
  let t = esc(text);
  t = t.replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, (m, alt, src) => {
    let url = src;
    if (!/^(https?:)?\/\//.test(url) && !url.startsWith('/')) {
      url = `/api/lessons/${lessonId}/asset/${url}`;
    }
    return `<img src="${url}" alt="${alt}">`;
  });
  t = t.replace(/`([^`]+)`/g, '<code>$1</code>');
  t = t.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  t = t.replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>');
  return t;
}

function mdToHtml(md, lessonId) {
  const lines = String(md || '').replace(/\r\n/g, '\n').split('\n');
  const out = [];
  let inFront = false, frontDone = false;
  let list = null, quote = [], para = [];
  let i = 0;

  const flushPara = () => {
    if (para.length) { out.push(`<p>${inlineHtml(para.join(' '), lessonId)}</p>`); para = []; }
  };
  const flushQuote = () => {
    if (quote.length) {
      out.push(`<blockquote>${quote.map(q => inlineHtml(q, lessonId)).join('<br>')}</blockquote>`);
      quote = [];
    }
  };
  const flushList = () => { if (list) { out.push(`</${list}>`); list = null; } };
  const flushAll = () => { flushPara(); flushQuote(); flushList(); };

  const isRow = s => /^\|.*\|$/.test(s);
  const isSep = s => /^\|[\s:|-]+\|$/.test(s);

  while (i < lines.length) {
    const line = lines[i].trim();
    i += 1;

    if (!frontDone && line === '---') {
      if (!inFront) { inFront = true; continue; }
      inFront = false; frontDone = true; continue;
    }
    if (inFront) continue;

    if (!line) { flushAll(); continue; }

    // 表格
    if (isRow(line) && i < lines.length && isSep(lines[i].trim())) {
      flushAll();
      const cells = s => s.trim().replace(/^\||\|$/g, '').split('|').map(c => c.trim());
      const head = cells(line);
      i += 1;
      const rows = [];
      while (i < lines.length && isRow(lines[i].trim())) {
        rows.push(cells(lines[i].trim()));
        i += 1;
      }
      out.push(
        '<table><thead><tr>' +
        head.map(h => `<th>${inlineHtml(h, lessonId)}</th>`).join('') +
        '</tr></thead><tbody>' +
        rows.map(r => '<tr>' + r.map(c => `<td>${inlineHtml(c, lessonId)}</td>`).join('') + '</tr>').join('') +
        '</tbody></table>'
      );
      continue;
    }

    if (line.startsWith('### ')) { flushAll(); out.push(`<h3>${inlineHtml(line.slice(4), lessonId)}</h3>`); continue; }
    if (line.startsWith('## ')) { flushAll(); out.push(`<h2>${inlineHtml(line.slice(3), lessonId)}</h2>`); continue; }
    if (line.startsWith('# ')) { flushAll(); out.push(`<h1>${inlineHtml(line.slice(2), lessonId)}</h1>`); continue; }

    if (line.startsWith('> ')) { flushPara(); flushList(); quote.push(line.slice(2)); continue; }

    const imgOnly = line.match(/^!\[([^\]]*)\]\(([^)\s]+)\)$/);
    if (imgOnly) {
      flushAll();
      let url = imgOnly[2];
      if (!/^(https?:)?\/\//.test(url) && !url.startsWith('/')) {
        url = `/api/lessons/${lessonId}/asset/${url}`;
      }
      out.push(`<figure><img src="${url}" alt="${esc(imgOnly[1])}" loading="lazy">` +
        `<figcaption>${esc(imgOnly[1])}</figcaption></figure>`);
      continue;
    }

    if (/^[-*] /.test(line)) {
      flushPara(); flushQuote();
      if (list !== 'ul') { flushList(); out.push('<ul>'); list = 'ul'; }
      out.push(`<li>${inlineHtml(line.slice(2), lessonId)}</li>`);
      continue;
    }
    if (/^\d+\.\s/.test(line)) {
      flushPara(); flushQuote();
      if (list !== 'ol') { flushList(); out.push('<ol>'); list = 'ol'; }
      out.push(`<li>${inlineHtml(line.replace(/^\d+\.\s+/, ''), lessonId)}</li>`);
      continue;
    }

    // 缩进的续行并入上一条列表项
    if (/^\s{2,}/.test(lines[i - 1] || '') && list && out.length) {
      const last = out[out.length - 1];
      if (last.startsWith('<li>')) {
        out[out.length - 1] = last.replace(/<\/li>$/, '<br>' + inlineHtml(line, lessonId) + '</li>');
        continue;
      }
    }

    if (/^[-*_]{3,}$/.test(line)) { flushAll(); out.push('<hr>'); continue; }

    flushList(); flushQuote();
    para.push(line);
  }

  flushAll();
  return out.join('');
}

/* ---------------- 上传 ---------------- */

async function uploadFiles(kind, files) {
  const l = state.lesson;
  if (!l || !files || !files.length) return;

  const isMat = kind === 'materials';
  const drop = isMat ? $('#matDrop') : $('#audDrop');
  const span = drop.querySelector('span');
  const original = span.textContent;
  const fd = new FormData();
  Array.from(files).forEach(f => fd.append('files', f));

  span.textContent = `上传 ${files.length} 个文件…`;
  drop.style.pointerEvents = 'none';
  drop.style.opacity = '.6';

  const d = await api(`/api/lessons/${l.id}/${kind}`, { method: 'POST', body: fd });

  drop.style.pointerEvents = '';
  drop.style.opacity = '';
  span.textContent = original;

  if (!d.ok) { toast(d.error || '上传失败', 'err'); return; }

  const added = (d.added || []).length;
  const rejected = d.rejected || [];
  if (added) toast(`已添加 ${added} 个文件`, 'ok');
  if (rejected.length) toast(`${rejected.length} 个文件被拒绝：${rejected[0].name} ${rejected[0].reason}`, 'err');

  await openLesson(l.id);
  await loadAll(l.id);
  if (isMat) watchIngest();
}

function watchIngest() {
  let n = 0;
  const timer = setInterval(async () => {
    n += 1;
    const l = state.lesson;
    if (!l || state.view !== 'lesson') { clearInterval(timer); return; }
    await openLesson(l.id, true);
    const busy = state.lesson && state.lesson.materials.some(m => m.status === 'pending');
    if (!busy || n > 120) clearInterval(timer);
  }, 1000);
}

/* ---------------- 执行 ---------------- */

async function runLesson(force = false) {
  const l = state.lesson;
  if (!l) return;

  if (l.guides && l.guides.length && !force) {
    const ok = await confirmDialog(
      '重新分析会覆盖现有结果',
      '已经生成的极限版和详细版会被替换。材料没变的话没必要重跑。',
      '重新分析'
    );
    if (!ok) return;
  }

  if (!state.config || !state.config.llm_ready) {
    await openDrawer();
    toast('先配置大模型', 'err');
    return;
  }

  $('#btnRun').disabled = true;
  const d = await api(`/api/lessons/${l.id}/run`, { method: 'POST' });
  if (!d.ok) {
    toast(d.error || '无法开始', 'err');
    renderRunPanel();
    if ((d.error || '').includes('大模型') || (d.error || '').includes('语音')) await openDrawer();
    return;
  }
  await openLesson(l.id);
  startPolling();
}

async function cancelLesson() {
  const l = state.lesson;
  if (!l) return;
  $('#btnCancel').disabled = true;
  const d = await api(`/api/lessons/${l.id}/cancel`, { method: 'POST' });
  $('#btnCancel').disabled = false;
  if (!d.ok) { toast(d.error || '无法取消', 'err'); return; }
  toast('正在停止，当前这一步跑完就会退出');
}

function startPolling() {
  if (state.polling) return;
  state.polling = setInterval(async () => {
    const l = state.lesson;
    if (!l || state.view !== 'lesson') { stopPolling(); return; }
    const d = await api(`/api/lessons/${l.id}`);
    if (!d.ok) return;
    const prev = state.lesson.status;
    state.lesson = d.lesson;
    renderDetail();

    if (!RUNNING.includes(d.lesson.status)) {
      stopPolling();
      if (d.lesson.status === 'done') {
        state.contents = {};
        toast('复习指导已生成', 'ok');
        loadAll(d.lesson.id);
      } else if (d.lesson.status === 'canceled') {
        toast('已停止');
        loadAll(d.lesson.id);
      }
    } else if (prev !== d.lesson.status) {
      loadAll(d.lesson.id);
    }
  }, 1300);
}

function stopPolling() {
  if (state.polling) { clearInterval(state.polling); state.polling = null; }
}

/* ---------------- 课程视图 ---------------- */

async function openCourse(id, silent = false) {
  const course = state.courses.find(c => c.id === id);
  if (!course) { if (!silent) toast('课程不存在', 'err'); return; }

  state.course = course;
  state.lesson = null;
  showCourseView();
  renderCourseView();
  renderTree();

  if (course.job && course.job.status === 'running') startSumPolling();
}

function renderCourseView() {
  const c = state.course;
  if (!c) return;

  $('#courseName').textContent = c.name;

  const meta = [];
  if (c.term) meta.push(c.term);
  meta.push(`${c.lessons} 堂课`);
  if (c.done) meta.push(`${c.done} 堂已分析`);
  if (c.points) meta.push(`${c.points} 个考点`);
  if (c.summary && c.summary.generated) meta.push(`总复习生成于 ${timeAgo(c.summary.generated)}`);
  $('#courseMeta').textContent = meta.join(' · ');

  const items = lessonsOf(c.id);
  const box = $('#courseLessons');
  if (!items.length) {
    box.innerHTML = '<p class="empty-tip">这门课下还没有课堂</p>';
  } else {
    box.innerHTML = `<div class="clist">${items.map(l => `
      <div class="clist-row" data-lid="${l.id}">
        <div class="clist-name">
          <div>${esc(l.name)}</div>
          <div class="clist-sub">${l.duration ? fmtDur(l.duration) + ' · ' : ''}${l.materials.length} 份讲义 · ${timeAgo(l.updated)}</div>
        </div>
        <span class="clist-status">${l.status === 'done' ? `${l.points} 考点` : l.status_label}</span>
      </div>`).join('')}</div>`;

    $$('.clist-row', box).forEach(row => {
      row.onclick = () => openLesson(row.dataset.lid);
    });
  }

  const hasSummary = c.summary && c.summary.files && c.summary.files.summary_md;
  $('#sumResult').classList.toggle('hidden', !hasSummary);
  if (hasSummary) {
    $('#sumDl').innerHTML = ['md', 'docx'].map(f => `
      <a class="btn btn-line btn-sm" href="/api/courses/${c.id}/summary/download/${f}">
        ${DL_ICON}${f === 'md' ? 'Markdown' : 'Word'}
      </a>`).join('');
    loadSummaryPreview();
  }

  const btn = $('#btnSummary');
  const label = btn.querySelector('span');
  if (label) label.textContent = hasSummary ? '重新汇总' : '生成期末总复习';
  btn.disabled = !c.done;
  btn.title = c.done ? '' : '先至少分析完一堂课';
}

async function loadSummaryPreview() {
  const c = state.course;
  if (!c) return;
  const box = $('#sumBody');
  if (state.sumContent !== null && state.sumContentCourse === c.id) {
    if (!box.dataset.rendered) {
      box.innerHTML = mdToHtml(state.sumContent, '');
      box.dataset.rendered = '1';
    }
    return;
  }
  const d = await api(`/api/courses/${c.id}/summary/preview`);
  if (!d.ok) return;
  state.sumContent = d.content || '';
  state.sumContentCourse = c.id;
  box.innerHTML = mdToHtml(d.content || '', '');
  box.dataset.rendered = '1';
}

async function makeSummary() {
  const c = state.course;
  if (!c) return;

  const ok = await confirmDialog(
    '生成期末总复习？',
    `会把这门课下 ${c.done} 堂课已分析的考点合并去重，做成一份总复习。不会重新跑转写。`,
    '生成'
  );
  if (!ok) return;

  $('#sumPanel').classList.remove('hidden');
  $('#sumStage').textContent = '合并考点';
  $('#sumError').classList.add('hidden');
  $('#sumBar').classList.remove('fail');

  const d = await api(`/api/courses/${c.id}/summary`, { method: 'POST' });
  if (!d.ok) {
    $('#sumStage').textContent = '汇总失败';
    $('#sumError').textContent = d.error || '无法生成';
    $('#sumError').classList.remove('hidden');
    return;
  }
  startSumPolling();
}

function startSumPolling() {
  if (state.sumPolling) return;
  $('#sumPanel').classList.remove('hidden');
  $('#sumStage').textContent = '合并考点';

  state.sumPolling = setInterval(async () => {
    const c = state.course;
    if (!c || state.view !== 'course') { stopSumPolling(); return; }
    const d = await api(`/api/courses/${c.id}/summary`);
    if (!d.ok) return;
    const job = d.job || {};
    if (job.stage) $('#sumStage').textContent = job.stage;

    if (job.status === 'failed') {
      stopSumPolling();
      $('#sumStage').textContent = '汇总失败';
      $('#sumBar').classList.add('fail');
      $('#sumError').textContent = job.error || '未知错误';
      $('#sumError').classList.remove('hidden');
      return;
    }

    if (job.status === 'done' || (!job.status && d.summary && d.summary.files)) {
      stopSumPolling();
      $('#sumPanel').classList.add('hidden');
      $('#sumBar').classList.remove('fail');
      state.sumContent = null;
      const coursesRes = await api('/api/courses');
      state.courses = (coursesRes && coursesRes.courses) || [];
      state.course = state.courses.find(x => x.id === c.id) || state.course;
      renderCourseView();
      toast('总复习已生成', 'ok');
    }
  }, 900);
}

function stopSumPolling() {
  if (state.sumPolling) { clearInterval(state.sumPolling); state.sumPolling = null; }
}

/* ---------------- 设置 ---------------- */

async function openDrawer() {
  await loadConfig();
  await loadLocal();
  $('#drawer').classList.add('open');
  $('#drawer').setAttribute('aria-hidden', 'false');
  $('#scrim').classList.add('show');
  setTimeout(() => $('#llmBase').focus(), 260);
}

function closeDrawer() {
  $('#drawer').classList.remove('open');
  $('#drawer').setAttribute('aria-hidden', 'true');
  if (!$('#newModal').classList.contains('open')) $('#scrim').classList.remove('show');
}

async function loadConfig() {
  const cfg = await api('/api/config');
  if (!cfg || !cfg.llm) { toast('读取设置失败', 'err'); return; }
  state.config = cfg;

  $('#presetList').innerHTML = (cfg.presets || []).map(p => `
    <button data-preset="${p.id}" data-url="${esc(p.base_url)}" data-hint="${esc(p.hint)}"
      class="${cfg.llm.preset === p.id ? 'active' : ''}">${esc(p.name)}</button>`).join('');

  $$('#presetList button').forEach(b => {
    b.onclick = () => {
      $$('#presetList button').forEach(x => x.classList.toggle('active', x === b));
      const hint = (b.dataset.hint || '').replace(/^模型填\s*/, '');
      if (b.dataset.url) $('#llmBase').value = b.dataset.url;
      $('#llmHint').textContent = hint;
      $('#llmModel').placeholder = hint || '模型名';
      applyLocalVisibility(b.dataset.preset);
    };
  });

  $('#llmBase').value = cfg.llm.base_url || '';
  $('#llmKey').value = cfg.llm.api_key || '';
  $('#llmModel').value = cfg.llm.model || '';

  const active = (cfg.presets || []).find(p => p.id === cfg.llm.preset);
  const hint = active ? (active.hint || '').replace(/^模型填\s*/, '') : '控制台的模型 ID';
  $('#llmHint').textContent = hint;
  $('#llmModel').placeholder = hint || '模型名';
  applyLocalVisibility(cfg.llm.preset);

  $('#maxPoints').value = cfg.guide.extreme_max_points || 12;
  $('#maxPointsVal').textContent = `${$('#maxPoints').value} 条`;
  $('#summaryTop').value = cfg.guide.summary_top || 15;
  $('#summaryTopVal').textContent = `${$('#summaryTop').value} 条`;
  $('#withShots').checked = cfg.guide.detailed_with_shots !== false;

  renderAsrOptions(cfg);
}

function applyLocalVisibility(preset) {
  const isLocal = preset === 'ollama';
  $('#ollamaCard').classList.toggle('hidden', !isLocal);
  $('#keyField').classList.toggle('hidden', isLocal);
  if (isLocal) probeOllama();
}

function renderAsrOptions(cfg) {
  const current = (cfg.asr_info || {}).provider || 'volc';
  const opts = [
    ['volc', '火山引擎 · 豆包'],
    ['sensevoice', '本地 SenseVoice（离线）'],
    ['dashscope', '阿里云百炼'],
    ['openai', 'OpenAI Whisper'],
    ['local', '本地 faster-whisper'],
  ];
  $('#asrProvider').innerHTML = opts.map(([id, name]) =>
    `<option value="${id}" ${id === current ? 'selected' : ''}>${name}</option>`).join('');

  const ai = cfg.asr_info || {};
  const label = PROVIDER_LABEL[ai.provider] || ai.provider;

  if (current === 'sensevoice') {
    // 下面那张卡片已经写了引擎名，这里不再重复，只报状态
    $('#asrInfo').textContent = ai.ready
      ? '已就绪 · 完全离线，不花钱'
      : '模型还没下载，点下面按钮一次就好';
  } else if (current === 'local') {
    $('#asrInfo').textContent = ai.ready ? '已就绪 · 完全离线，不花钱' : '还缺 faster-whisper 依赖';
  } else {
    const hasKey = !!(ai.has_key || {})[current];
    $('#asrInfo').textContent = ai.ready
      ? `${label} · 已就绪`
      : `${label} · 还没配好${hasKey ? '（把下面缺的补上）' : ''}`;
  }

  // 云端引擎才需要凭证；本地引擎不需要，别让人填无用的东西
  applyAsrVisibility(current, cfg);
}

const ASR_DEFAULTS = {
  openai: ['https://api.openai.com/v1', 'whisper-1'],
  dashscope: ['https://dashscope.aliyuncs.com/compatible-mode/v1', 'qwen3-asr-flash'],
};

function applyAsrVisibility(current, cfg) {
  const ai = ((cfg || state.config || {}).asr_info) || {};
  const local = current === 'sensevoice' || current === 'local';
  const simple = current === 'volc';

  $('#svCard').classList.toggle('hidden', current !== 'sensevoice');
  $('#asrCreds').classList.toggle('hidden', local);
  if (local) return;

  $('#asrBaseField').classList.toggle('hidden', simple);
  $('#asrModelField').classList.toggle('hidden', simple);
  $('#asrKey').value = '';
  $('#asrKey').placeholder = (ai.has_key || {})[current]
    ? '已保存，留空表示不修改'
    : '粘贴这个引擎的 API Key';
  if (simple) return;

  // 切到别的引擎时，给它的常见默认值，省得对着空框发呆
  const fallback = ASR_DEFAULTS[current] || ['', ''];
  const same = ai.provider === current;
  $('#asrBaseUrl').value = (same && ai.base_url) || fallback[0];
  $('#asrModel').value = (same && ai.model) || fallback[1];
}

async function probeOllama() {
  $('#ollamaState').textContent = '检测中';
  $('#ollamaState').className = 'pill';
  const d = await api(`/api/llm/ollama?base_url=${encodeURIComponent($('#llmBase').value.trim())}`);
  if (d.running) {
    $('#ollamaState').textContent = `已连接 · ${d.models.length} 个模型`;
    $('#ollamaState').className = 'pill ok';
    $('#ollamaModels').innerHTML = d.models.map(m =>
      `<option value="${esc(m.name)}">${esc(m.name)} · ${fmtSize(m.size)}</option>`).join('');
    if (d.models.length) {
      $('#ollamaNote').textContent = `已装：${d.models.map(m => m.name).join('、')}`;
    } else {
      $('#ollamaNote').textContent = 'Ollama 在跑但还没装模型，先在终端执行 ollama pull qwen3:8b';
    }
  } else {
    $('#ollamaState').textContent = '未连接';
    $('#ollamaState').className = 'pill bad';
    const s = (state.local && state.local.suggest_model) || { name: 'qwen3:8b', note: '' };
    $('#ollamaNote').textContent =
      `连不上 ${d.url || '127.0.0.1:11434'}。装好 Ollama 后运行 ollama pull ${s.name}。${d.error || ''}`;
  }
}

async function loadLocal() {
  const d = await api('/api/local/status');
  if (!d || !d.asr) return;
  state.local = d;

  const a = d.asr;
  const pill = $('#svState');
  if (a.ready) {
    pill.textContent = '已安装';
    pill.className = 'pill ok';
    $('#svNote').textContent = `模型已就绪（${fmtSize(a.size_bytes)}），纯 CPU 约 20 倍实时。`;
  } else if (a.installed_pkg === false) {
    pill.textContent = '缺少依赖';
    pill.className = 'pill bad';
    $('#svNote').textContent = '需要先安装：pip install sherpa-onnx';
  } else {
    pill.textContent = '未下载';
    pill.className = 'pill warn';
    $('#svNote').textContent = '约 230MB 模型，中文识别比 Whisper 强，纯 CPU 约 20 倍实时。';
  }
  $('#btnDownloadModel').classList.toggle('hidden', !!a.ready);

  if (d.suggest_model) {
    const s = d.suggest_model;
    $('#ollamaNote').dataset.suggest = s.name;
  }
}

async function downloadModel() {
  const btn = $('#btnDownloadModel');
  btn.disabled = true;
  btn.textContent = '下载中…';
  $('#svBarWrap').classList.remove('hidden');

  const d = await api('/api/local/asr/download', { method: 'POST' });
  if (!d.ok) {
    btn.disabled = false;
    btn.textContent = '重新下载';
    toast('无法开始下载', 'err');
    return;
  }

  const timer = setInterval(async () => {
    const s = await api('/api/local/asr/download');
    const job = (s && s.job) || {};
    const pct = Number(job.pct || 0);
    $('#svBar').style.width = `${Math.max(3, pct)}%`;
    $('#svNote').textContent = job.stage || '下载中';
    if (job.status === 'done') {
      clearInterval(timer);
      $('#svBarWrap').classList.add('hidden');
      btn.disabled = false;
      btn.textContent = '重新下载';
      await loadLocal();
      await loadConfig();
      toast('本地模型已就绪', 'ok');
    } else if (job.status === 'failed') {
      clearInterval(timer);
      $('#svBarWrap').classList.add('hidden');
      btn.disabled = false;
      btn.textContent = '重试';
      toast(job.error || '下载失败', 'err');
    }
  }, 900);
}

async function saveConfig() {
  const preset = ($('#presetList .active') || {}).dataset;
  const asrProvider = $('#asrProvider').value;

  const payload = {
    llm: {
      preset: preset ? preset.preset : 'custom',
      base_url: $('#llmBase').value.trim(),
      api_key: $('#llmKey').value.trim(),
      model: $('#llmModel').value.trim(),
    },
    guide: {
      extreme_max_points: Number($('#maxPoints').value),
      summary_top: Number($('#summaryTop').value),
      detailed_with_shots: $('#withShots').checked,
    },
    asr: { provider: asrProvider },
  };

  // 只在真的填了东西时才写凭证，避免把「留空表示不修改」变成清空
  if (asrProvider !== 'sensevoice' && asrProvider !== 'local') {
    const entry = {};
    const key = $('#asrKey').value.trim();
    if (key) entry.api_key = key;
    if (asrProvider !== 'volc') {
      const base = $('#asrBaseUrl').value.trim();
      const model = $('#asrModel').value.trim();
      if (base) entry.base_url = base;
      if (model) entry.model = model;
    }
    if (Object.keys(entry).length) payload.asr.providers = { [asrProvider]: entry };
  }

  const d = await api('/api/config', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });

  if (!d.ok) { toast(d.error || '保存失败', 'err'); return; }
  state.config = d.config;
  $('#setupHint').classList.toggle('hidden', !!d.config.llm_ready);
  closeDrawer();
  toast(d.config.llm_ready ? '设置已保存' : '已保存，但大模型还没配完整', d.config.llm_ready ? 'ok' : 'err');
}

/* ---------------- 备份与恢复 ---------------- */

function bindBackup() {
  $('#btnRestore').onclick = () => $('#restoreInput').click();

  $('#restoreInput').onchange = async e => {
    const file = e.target.files && e.target.files[0];
    e.target.value = '';
    if (!file) return;

    const ok = await confirmDialog(
      '恢复备份会覆盖当前数据',
      `导入「${file.name}」后，现有的课程和课堂会被替换。替换前的数据会自动另存一份 data_before_restore_* 目录，可以找回。`,
      '恢复'
    );
    if (!ok) return;

    const fd = new FormData();
    fd.append('file', file);
    toast('正在恢复，课堂多的话要等一会');
    const d = await api('/api/restore', { method: 'POST', body: fd });
    if (!d.ok) { toast(d.error || '恢复失败', 'err'); return; }

    closeDrawer();
    state.lesson = null;
    state.course = null;
    showWelcome();
    await loadAll();
    toast(`已恢复 ${d.courses} 门课、${d.lessons} 堂课`, 'ok');
  };
}

/* ---------------- 弹层 ---------------- */

let modalMode = 'lesson';
let modalCourse = '';
let assignAfterCreate = '';       // 从「归到课程」里新建课程时，建完把当前课堂挂进去

/* ---------------- 归到课程 ---------------- */

function closeCourseMenu() {
  const menu = $('#courseMenu');
  if (menu) menu.classList.add('hidden');
}

function openCourseMenu(anchor) {
  const l = state.lesson;
  if (!l) return;
  const menu = $('#courseMenu');
  const cur = l.course_id || '';
  const items = [{ id: '', name: '未分类' }].concat(state.courses);

  menu.innerHTML = items.map(c =>
    `<button data-id="${c.id}" class="${c.id === cur ? 'on' : ''}">${esc(c.name)}</button>`).join('') +
    '<button class="new" data-new="1">＋ 新建课程并归入</button>';
  menu.classList.remove('hidden');

  const r = anchor.getBoundingClientRect();
  const w = menu.offsetWidth || 180;
  menu.style.left = `${Math.max(8, Math.min(r.left, window.innerWidth - w - 12))}px`;
  menu.style.top = `${Math.min(r.bottom + 6, window.innerHeight - menu.offsetHeight - 12)}px`;

  menu.querySelectorAll('button').forEach(b => {
    b.onclick = async () => {
      closeCourseMenu();
      if (b.dataset.new) {
        assignAfterCreate = l.id;
        openNewModal('course');
        return;
      }
      await moveLessonTo(b.dataset.id || '');
    };
  });
}

async function moveLessonTo(courseId, lessonId = '') {
  const id = lessonId || (state.lesson && state.lesson.id) || '';
  if (!id) return;
  const cur = state.lesson && state.lesson.id === id ? (state.lesson.course_id || '') : null;
  if (cur === courseId) return;
  const d = await api(`/api/lessons/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ course_id: courseId }),
  });
  if (!d.ok) { toast(d.error || '移动失败', 'err'); return; }
  const name = courseId
    ? (state.courses.find(c => c.id === courseId) || {}).name || '课程'
    : '未分类';
  await loadAll(id);
  toast(`已归到「${name}」`, 'ok');
}

function openNewModal(mode = 'lesson', courseId = '') {
  modalMode = mode;
  modalCourse = courseId;
  const isCourse = mode === 'course';

  $('#newTitle').textContent = isCourse ? '新建课程' : '新建课堂';
  $('#newNameLabel').textContent = isCourse ? '课程名称' : '课堂名称';
  $('#newName').placeholder = isCourse ? '例如：机器学习' : '例如：第 5 讲 · 卷积神经网络';
  $('#newCourseField').classList.toggle('hidden', isCourse);
  $('#newTip').textContent = isCourse
    ? '课程用来把同一门课的多堂课收在一起，方便期末合并成一份总复习。'
    : '一个课堂 = 这节课的所有讲义 + 所有录音，可以放多份。';
  $('#btnConfirmNew').textContent = isCourse ? '创建课程' : '创建课堂';

  if (!isCourse) {
    $('#newCourse').innerHTML = '<option value="">不归类</option>' +
      state.courses.map(c => `<option value="${c.id}" ${c.id === courseId ? 'selected' : ''}>${esc(c.name)}</option>`).join('');
  }

  $('#newModal').classList.add('open');
  $('#newModal').setAttribute('aria-hidden', 'false');
  $('#scrim').classList.add('show');
  $('#newName').value = '';
  setTimeout(() => $('#newName').focus(), 80);
}

function closeNewModal() {
  $('#newModal').classList.remove('open');
  $('#newModal').setAttribute('aria-hidden', 'true');
  if (!$('#drawer').classList.contains('open')) $('#scrim').classList.remove('show');
}

async function confirmSubmit() {
  const name = $('#newName').value.trim();
  const btn = $('#btnConfirmNew');
  btn.disabled = true;

  if (modalMode === 'course') {
    if (!name) { toast('给课程起个名字', 'err'); btn.disabled = false; return; }
    const d = await api('/api/courses', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    });
    btn.disabled = false;
    if (!d.ok) { toast(d.error || '创建失败', 'err'); return; }
    const pending = assignAfterCreate;
    assignAfterCreate = '';
    closeNewModal();
    await loadAll();
    if (pending) {
      await moveLessonTo(d.course.id, pending);
      return;
    }
    await openCourse(d.course.id);
    toast('课程已创建', 'ok');
    return;
  }

  const courseId = $('#newCourse').value;
  const d = await api('/api/lessons', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, course_id: courseId }),
  });
  btn.disabled = false;
  if (!d.ok) { toast(d.error || '创建失败', 'err'); return; }
  closeNewModal();
  await loadAll(d.lesson.id);
  toast('课堂已创建，导入讲义和录音');
}

/* 自定义确认框，替代原生 confirm */
function confirmDialog(title, body, okLabel = '确定') {
  return new Promise(resolve => {
    const wrap = document.createElement('div');
    wrap.className = 'modal open';
    wrap.innerHTML = `
      <div class="modal-card narrow">
        <div class="modal-head"><h2>${esc(title)}</h2></div>
        <div class="modal-body"><p class="tip" style="font-size:12.5px;line-height:1.8">${esc(body)}</p></div>
        <div class="modal-foot">
          <button class="btn btn-line" data-act="no">取消</button>
          <button class="btn btn-solid" data-act="yes">${esc(okLabel)}</button>
        </div>
      </div>`;
    document.body.appendChild(wrap);
    $('#scrim').classList.add('show');

    const done = v => {
      wrap.remove();
      if (!$('#drawer').classList.contains('open') && !$('#newModal').classList.contains('open')) {
        $('#scrim').classList.remove('show');
      }
      resolve(v);
    };
    wrap.querySelector('[data-act="no"]').onclick = () => done(false);
    wrap.querySelector('[data-act="yes"]').onclick = () => done(true);
    wrap.onclick = e => { if (e.target === wrap) done(false); };
    setTimeout(() => wrap.querySelector('[data-act="yes"]').focus(), 60);
  });
}

/* 就地重命名 */
function startRename(el, current, onDone) {
  el.contentEditable = 'true';
  el.focus();
  const range = document.createRange();
  range.selectNodeContents(el);
  const sel = window.getSelection();
  sel.removeAllRanges();
  sel.addRange(range);

  const finish = async save => {
    el.contentEditable = 'false';
    const value = el.textContent.trim();
    if (save && value && value !== current) await onDone(value);
    else el.textContent = current;
  };
  el.onblur = () => finish(true);
  el.onkeydown = e => {
    if (e.key === 'Enter') { e.preventDefault(); el.blur(); }
    if (e.key === 'Escape') { e.preventDefault(); el.onblur = null; finish(false); }
  };
}

/* ---------------- 事件 ---------------- */

function bindDrop(dropSel, inputSel, kind) {
  const drop = $(dropSel);
  const input = $(inputSel);

  drop.addEventListener('click', () => {
    if (!state.lesson) return;
    input.click();
  });
  input.addEventListener('change', () => {
    uploadFiles(kind, input.files);
    input.value = '';
  });

  ['dragenter', 'dragover'].forEach(evt => drop.addEventListener(evt, e => {
    e.preventDefault(); e.stopPropagation();
    if (state.lesson) drop.classList.add('dragover');
  }));
  ['dragleave', 'drop'].forEach(evt => drop.addEventListener(evt, e => {
    e.preventDefault(); e.stopPropagation();
    if (evt === 'dragleave' && drop.contains(e.relatedTarget)) return;
    drop.classList.remove('dragover');
  }));
  drop.addEventListener('drop', e => {
    if (!state.lesson) return;
    uploadFiles(kind, e.dataTransfer.files);
  });
}

function bind() {
  bindDrop('#matDrop', '#matInput', 'materials');
  bindDrop('#audDrop', '#audInput', 'audios');

  window.addEventListener('dragover', e => e.preventDefault());
  window.addEventListener('drop', e => e.preventDefault());

  $('#btnNew').onclick = () => openNewModal('lesson', state.course ? state.course.id : '');
  $('#btnWelcomeNew').onclick = () => openNewModal('lesson', '');
  $('#btnNewCourse').onclick = () => openNewModal('course');
  $('#btnCancelNew').onclick = closeNewModal;
  $('#btnConfirmNew').onclick = confirmSubmit;
  $('#newName').addEventListener('keydown', e => {
    if (e.key === 'Enter') confirmSubmit();
  });

  $('#btnRun').onclick = () => runLesson(false);
  $('#btnCancel').onclick = cancelLesson;
  $('#btnStaleRun').onclick = () => runLesson(true);

  $('#btnRename').onclick = () => {
    if (!state.lesson) return;
    startRename($('#lessonName'), state.lesson.name, async name => {
      await api(`/api/lessons/${state.lesson.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name }),
      });
      toast('已重命名', 'ok');
      await loadAll(state.lesson.id);
    });
  };
  $('#lessonName').ondblclick = () => $('#btnRename').click();

  $('#btnRenameCourse').onclick = () => {
    if (!state.course) return;
    startRename($('#courseName'), state.course.name, async name => {
      await api(`/api/courses/${state.course.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name }),
      });
      toast('已重命名', 'ok');
      state.course.name = name;
      await loadAll();
    });
  };

  $('#btnSummary').onclick = makeSummary;

  /* 删课程只删分组，课堂回到「未分类」——建错名字、学期结束都用得上 */
  $('#btnDeleteCourse').onclick = async () => {
    const c = state.course;
    if (!c) return;
    const n = c.lessons || 0;
    const ok = await confirmDialog(
      `删除课程「${c.name}」？`,
      n
        ? `这门课下的 ${n} 堂课不会被删除，会回到「未分类」，之后随时能再归到别的课程。`
        : '这门课下还没有课堂，删除后不可恢复。',
      '删除课程'
    );
    if (!ok) return;

    const d = await api(`/api/courses/${c.id}`, { method: 'DELETE' });
    if (!d.ok) { toast(d.error || '删除失败', 'err'); return; }
    state.course = null;
    state.lesson = null;
    showWelcome();
    await loadAll();
    toast(d.moved_lessons ? `课程已删除，${d.moved_lessons} 堂课回到未分类` : '课程已删除', 'ok');
  };

  $('#btnCopy').onclick = copyMarkdown;
  $('#btnTranscript').onclick = toggleTranscript;
  $('#btnSetup').onclick = openDrawer;
  bindBackup();

  document.addEventListener('click', e => {
    if (e.target.closest('#crumbCourse')) {
      e.preventDefault();
      if (state.lesson && state.lesson.course_id) openCourse(state.lesson.course_id);
      else toast('这堂课还没归到课程');
      return;
    }
    if (e.target.closest('#crumbPick')) {
      e.preventDefault();
      openCourseMenu(e.target.closest('#crumbPick'));
      return;
    }
    if (!e.target.closest('#courseMenu')) closeCourseMenu();
  });

  $('#btnSettings').onclick = openDrawer;
  $('#btnCloseDrawer').onclick = closeDrawer;
  $('#btnSave').onclick = saveConfig;
  $('#btnDownloadModel').onclick = downloadModel;

  $('#maxPoints').addEventListener('input', e => {
    $('#maxPointsVal').textContent = `${e.target.value} 条`;
  });
  $('#summaryTop').addEventListener('input', e => {
    $('#summaryTopVal').textContent = `${e.target.value} 条`;
  });
  $('#asrProvider').addEventListener('change', e => {
    applyAsrVisibility(e.target.value);
  });

  $$('#modeTabs button').forEach(b => {
    b.onclick = () => {
      if (b.disabled) return;
      state.mode = b.dataset.mode;
      try {
        const url = new URL(location.href);
        url.searchParams.set('mode', state.mode);
        history.replaceState(null, '', url);
      } catch (e) { /* 忽略 */ }
      renderResult();
    };
  });

  $('#scrim').onclick = () => { closeDrawer(); closeNewModal(); };

  const search = $('#search');
  search.addEventListener('input', e => {
    state.search = e.target.value.trim();
    renderTree();
  });

  document.addEventListener('keydown', e => {
    const meta = e.metaKey || e.ctrlKey;
    if (meta && e.key.toLowerCase() === 'k') {
      e.preventDefault();
      search.focus();
      search.select();
      return;
    }
    if (e.key === 'Escape') {
      if ($('#drawer').classList.contains('open')) { closeDrawer(); return; }
      if ($('#newModal').classList.contains('open')) { closeNewModal(); return; }
      if (document.activeElement === search && search.value) {
        search.value = '';
        state.search = '';
        renderTree();
        return;
      }
    }
  });
}

/* ---------------- 启动 ---------------- */

async function init() {
  bind();

  const want = new URLSearchParams(location.search).get('mode');
  if (want === 'detailed' || want === 'extreme') state.pendingMode = want;

  const health = await api('/api/health');
  if (health && health.version) {
    $('#sideVer').textContent = `v${health.version}`;
    $('#aboutVer').textContent =
      `不听课 v${health.version} · 与「语音转录工坊」同属一套课堂工具`;
  }

  await loadConfig();
  await loadAll();

  if (!state.lessons.length) showWelcome();

  // 没配大模型时只提示，不自动弹抽屉——首次打开就抢走焦点很讨厌
  const needSetup = !!state.config && !state.config.llm_ready;
  $('#setupHint').classList.toggle('hidden', !needSetup);
}

init();
