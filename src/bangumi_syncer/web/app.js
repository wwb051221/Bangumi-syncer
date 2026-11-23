/* Bangumi-syncer web UI — vanilla JS, no build step. */
'use strict';

const CSRF = { 'X-Requested-With': 'bangumi-syncer' };
const PREFS_KEY = 'bs.prefs.v1';
const DEFAULT_PREFS = { theme: 'auto', accent: '#7db3ff', fontScale: 1, density: 'comfortable' };
const ACCENTS = ['#7db3ff', '#6ee7a8', '#c792ea', '#ffb86b', '#ff7eb6', '#8be9fd'];
const TABS = ['overview', 'review', 'mappings', 'accounts', 'logs', 'settings'];

const state = {
  status: null,
  tab: (location.hash || '#overview').slice(1),
  selected: {},
  search: { q: '', results: [], loading: false, error: '' },
  settings: null,
  settingsError: '',
  settingsLoading: false,
  userRows: [],
  prefs: loadPrefs(),
  themeOverride: new URLSearchParams(location.search).get('theme') || '',
  refreshTimer: null,
  logTimer: null,
  runBusy: false,
};

/* ------------------------------------------------------------------ utils */
const esc = (value) =>
  String(value === null || value === undefined ? '' : value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

const num = (value) => (value === null || value === undefined ? 0 : Number(value) || 0);
const $ = (selector) => document.querySelector(selector);

function fmtDuration(seconds) {
  const total = Math.max(0, Math.round(num(seconds)));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h) return h + 'h' + String(m).padStart(2, '0') + 'm';
  if (m) return m + 'm' + String(s).padStart(2, '0') + 's';
  return s + 's';
}

function fmtTime(epoch, tz) {
  if (!epoch) return '-';
  try {
    return new Intl.DateTimeFormat('zh-CN', {
      dateStyle: 'short', timeStyle: 'medium', timeZone: tz || 'Asia/Shanghai',
    }).format(new Date(num(epoch) * 1000));
  } catch (err) {
    return new Date(num(epoch) * 1000).toLocaleString();
  }
}

function countdown(epoch) {
  if (!epoch) return '';
  const seconds = num(epoch) - Math.floor(Date.now() / 1000);
  if (seconds <= 0) return '即将开始';
  return fmtDuration(seconds) + ' 后';
}

function badge(text, kind) {
  return '<span class="badge ' + (kind || 'info') + '">' + esc(text) + '</span>';
}

function toast(message, kind) {
  const wrap = document.getElementById('toasts');
  const el = document.createElement('div');
  el.className = 'toast ' + (kind || '');
  el.textContent = message;
  wrap.appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; el.style.transition = 'opacity .3s'; }, 3200);
  setTimeout(() => el.remove(), 3600);
}

async function apiGet(path) {
  const response = await fetch(path, { headers: { Accept: 'application/json' } });
  if (!response.ok) {
    let detail = response.status + ' ' + response.statusText;
    try { const body = await response.json(); detail = body.error || detail; } catch (err) { /* ignore */ }
    throw new Error(detail);
  }
  return response.json();
}

async function apiSend(method, path, body) {
  const init = { method, headers: { Accept: 'application/json', ...CSRF } };
  if (body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  const response = await fetch(path, init);
  let payload = null;
  try { payload = await response.json(); } catch (err) { /* ignore */ }
  if (!response.ok) {
    throw new Error((payload && payload.error) || (response.status + ' ' + response.statusText));
  }
  return payload;
}

/* ------------------------------------------------------------- appearance */
function loadPrefs() {
  try {
    return { ...DEFAULT_PREFS, ...JSON.parse(localStorage.getItem(PREFS_KEY) || '{}') };
  } catch (err) {
    return { ...DEFAULT_PREFS };
  }
}

function resolvedTheme(pref) {
  if (pref === 'light' || pref === 'dark') return pref;
  return window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
}

function applyPrefs(persist) {
  const root = document.documentElement;
  const pref = state.themeOverride || state.prefs.theme;
  root.dataset.theme = resolvedTheme(state.themeOverride || state.prefs.theme);
  root.dataset.themePref = pref;
  root.dataset.density = state.prefs.density;
  root.style.setProperty('--accent', state.prefs.accent);
  root.style.setProperty('--font-scale', String(state.prefs.fontScale));
  if (persist) {
    try { localStorage.setItem(PREFS_KEY, JSON.stringify(state.prefs)); } catch (err) { /* ignore */ }
  }
  syncAppearanceControls();
  updateLayoutFlag();
}

function updatePrefs(patch) {
  state.prefs = { ...state.prefs, ...patch };
  state.themeOverride = '';  // an explicit choice wins over ?theme=
  applyPrefs(true);
}

function syncAppearanceControls() {
  document.querySelectorAll('[data-role="theme-option"]').forEach((node) => {
    node.classList.toggle('active', node.dataset.value === (state.themeOverride || state.prefs.theme));
  });
  document.querySelectorAll('[data-role="density-option"]').forEach((node) => {
    node.classList.toggle('active', node.dataset.value === state.prefs.density);
  });
  document.querySelectorAll('[data-role="accent-option"]').forEach((node) => {
    node.classList.toggle('active', node.dataset.value.toLowerCase() === state.prefs.accent.toLowerCase());
  });
  const fontInput = document.querySelector('[data-role="font-scale"]');
  if (fontInput) fontInput.value = String(state.prefs.fontScale);
  const fontLabel = document.getElementById('font-scale-value');
  if (fontLabel) fontLabel.textContent = Math.round(state.prefs.fontScale * 100) + '%';
}

function updateLayoutFlag() {
  const root = document.documentElement;
  root.dataset.layout = root.scrollWidth > root.clientWidth + 1 ? 'overflow' : 'ok';
  root.dataset.viewport = window.innerWidth + 'x' + window.innerHeight;
}

/* ---------------------------------------------------------------- refresh */
async function refresh() {
  try {
    state.status = await apiGet('/api/status');
    renderHeader();
    updateLayoutFlag();
    // Do not clobber forms / selections that the user is editing.
    if (state.tab === 'overview' || state.tab === 'accounts') render();
  } catch (err) {
    document.getElementById('app').innerHTML =
      '<div class="card"><h2>无法连接后端</h2><p class="muted">' + esc(err.message) + '</p></div>';
  }
}

async function handleRefresh() {
  await refresh();
  if (state.tab === 'settings') {
    await loadSettings();
  } else {
    render();
  }
  if (state.tab === 'logs') refreshLogs();
  toast('已刷新');
}

function startPolling() {
  clearInterval(state.refreshTimer);
  state.refreshTimer = setInterval(refresh, 5000);
  clearInterval(state.logTimer);
  state.logTimer = setInterval(() => {
    if (state.tab === 'logs' && document.getElementById('logPane')) refreshLogs();
  }, 2000);
}

async function refreshLogs() {
  try {
    const data = await apiGet('/api/logs?limit=400');
    const pane = document.getElementById('logPane');
    if (!pane) return;
    const atBottom = pane.scrollTop + pane.clientHeight >= pane.scrollHeight - 40;
    pane.textContent = (data.lines || []).join('\n');
    if (atBottom) pane.scrollTop = pane.scrollHeight;
  } catch (err) { /* keep quiet while polling */ }
}

/* ------------------------------------------------------------------ header */
function renderHeader() {
  const status = state.status;
  if (!status) return;
  document.getElementById('version').textContent = 'v' + (status.version || '?');
  const mode = document.getElementById('modeBadge');
  mode.innerHTML = status.dry_run ? badge('DRY-RUN', 'dry') : badge('LIVE', 'ok');
  document.getElementById('uptime').textContent = '运行 ' + fmtDuration(status.uptime_seconds);
  const next = status.run_requested
    ? '同步请求已排队'
    : (status.next_run_at ? '下次同步 ' + countdown(status.next_run_at) : '');
  document.getElementById('nextRun').textContent = next;
  document.getElementById('reviewCount').textContent = String((status.review || []).length);
  const runBtn = document.getElementById('runBtn');
  runBtn.disabled = state.runBusy || !!status.run_requested;
  runBtn.textContent = status.run_requested ? '同步中…' : '立即同步';
  document.getElementById('footerInfo').textContent =
    '时区 ' + (status.timezone || '-') + ' · 数据源 ' + ((status.source && status.source.type) || '-');
  document.querySelectorAll('#tabs a').forEach((link) => {
    link.classList.toggle('active', link.dataset.tab === state.tab);
  });
}

/* ------------------------------------------------------------------ views */
function viewOverview() {
  const status = state.status;
  const stats = status.stats || {};
  const source = status.source || {};
  const sourceStats = source.stats || {};
  const last = status.last_result;

  let html = '<section class="section"><div class="grid">';
  const cards = [
    ['映射', num(stats.mappings), (num(stats.mappings_confirmed) + ' 已人工确认')],
    ['已推送集', num(stats.pushed), '写入 Bangumi 的章节'],
    ['失败', num(stats.failed), '最近一轮失败数'],
    ['待复核', (status.review || []).length, '需要人工确认条目'],
  ];
  for (const [label, value, sub] of cards) {
    html += '<div class="card"><div class="label">' + esc(label) + '</div>' +
            '<div class="value">' + esc(value) + '</div>' +
            '<div class="sub">' + esc(sub) + '</div></div>';
  }
  html += '</div></section>';

  html += '<section class="section"><h2>数据源</h2><div class="card">' +
    '<div class="row"><span>类型 <code>' + esc(source.type || '-') + '</code></span>' +
    '<span>模式 <code>' + esc(source.mode || '-') + '</code></span>' +
    '<span>季 <strong>' + num(sourceStats.seasons) + '</strong></span>' +
    '<span>集 <strong>' + num(sourceStats.episodes) + '</strong></span>' +
    '<span>账号 <strong>' + num(sourceStats.users) + '</strong></span>' +
    '<span>路径过滤 <strong>' + num(sourceStats.episodes_filtered_by_path) + '</strong> 集</span></div>' +
    (source.include_paths && source.include_paths.length
      ? '<div class="sub muted" style="margin-top:.4rem">只同步：' + source.include_paths.map(esc).join('、') + '</div>'
      : '<div class="sub muted" style="margin-top:.4rem">未设置目录过滤，会同步所有剧集（建议在「设置」里限定动漫库）</div>') +
    '</div></section>';

  if (last) {
    html += '<section class="section"><h2>最近一次运行</h2><div class="card">' +
      '<div class="row">' +
      badge(last.dry_run ? '演练' : '实际写入', last.dry_run ? 'dry' : 'ok') +
      '<span>用户 <strong>' + num(last.users.length) + '</strong></span>' +
      '<span>匹配 <strong>' + num(last.matched) + '</strong></span>' +
      '<span>推送 <strong>' + num(last.pushed) + '</strong></span>' +
      '<span>跳过 <strong>' + num(last.skipped) + '</strong></span>' +
      '<span>失败 <strong>' + num(last.failed) + '</strong></span>' +
      '<span class="muted">耗时 ' + esc(fmtDuration(last.duration_seconds)) + '</span></div>' +
      (last.error ? '<p class="badge bad" style="margin-top:.6rem">' + esc(last.error) + '</p>' : '') +
      '</div></section>';
  }

  html += '<section class="section"><h2>用户</h2><div class="table-wrap"><table>' +
    '<thead><tr><th>飞牛用户</th><th class="hide-sm">Bangumi 账号</th><th class="num">本轮集数</th>' +
    '<th class="num">匹配</th><th class="num">推送</th><th class="num hide-sm">失败</th><th>状态</th></tr></thead><tbody>';
  const userResults = {};
  (last && last.users ? last.users : []).forEach((item) => { userResults[item.user] = item; });
  for (const user of status.users || []) {
    const result = userResults[user.fnos_user] || null;
    const stateBadge = !user.enabled
      ? badge('已禁用', 'info')
      : (result && result.error ? badge('错误', 'bad') : badge('正常', 'ok'));
    html += '<tr><td>' + esc(user.fnos_user) + '</td><td class="hide-sm">' + esc(user.account) + '</td>' +
      '<td class="num">' + (result ? num(result.series_seen) : '-') + '</td>' +
      '<td class="num">' + (result ? num(result.matched) : '-') + '</td>' +
      '<td class="num">' + (result ? num(result.pushed) : '-') + '</td>' +
      '<td class="num hide-sm">' + (result ? num(result.failed) : '-') + '</td>' +
      '<td>' + stateBadge + '</td></tr>';
  }
  if (!(status.users || []).length) html += '<tr><td colspan="7" class="empty">没有配置用户</td></tr>';
  html += '</tbody></table></div></section>';

  html += '<section class="section"><h2>运行历史</h2><div class="table-wrap"><table>' +
    '<thead><tr><th>开始</th><th>结果</th><th class="num">推送</th><th class="num hide-sm">失败</th>' +
    '<th class="num hide-sm">匹配</th><th class="num hide-sm">待复核</th><th class="num hide-sm">耗时</th><th>消息</th></tr></thead><tbody>';
  for (const run of status.runs || []) {
    html += '<tr><td>' + esc(run.started_at_human || '-') + '</td>' +
      '<td>' + (run.ok ? badge('成功', 'ok') : badge('失败', 'bad')) + '</td>' +
      '<td class="num">' + num(run.pushed) + '</td><td class="num hide-sm">' + num(run.failed) + '</td>' +
      '<td class="num hide-sm">' + num(run.matched) + '</td><td class="num hide-sm">' + num(run.unmatched) + '</td>' +
      '<td class="num hide-sm">' + esc(run.duration || '-') + '</td>' +
      '<td class="muted">' + esc(run.message || '') + '</td></tr>';
  }
  if (!(status.runs || []).length) html += '<tr><td colspan="8" class="empty">还没有运行记录</td></tr>';
  html += '</tbody></table></div></section>';
  return html;
}

function viewReview() {
  const items = state.status.review || [];
  let html = '<section class="section"><h2>待复核（' + items.length + '）</h2>';
  if (!items.length) {
    return html + '<div class="card"><div class="empty">🎉 没有需要人工确认的条目</div></div></section>';
  }
  html += '<p class="muted">自动匹配分数不足或有歧义的条目会停在这里。选中正确的 Bangumi 条目后点「确认映射」，下一轮就会按新映射同步。</p>';
  for (const item of items) {
    const key = item.series_key;
    const selected = state.selected[key] || null;
    html += '<div class="review-card" data-series="' + esc(key) + '" data-account="' + esc(item.account) + '">' +
      '<div class="review-head"><div>' +
      '<div class="review-title">' + esc(item.title || key) + ' <span class="muted">S' + esc(item.season || '?') +
      ' · ' + esc(item.year || '?') + ' · ' + esc(item.episode_count || '?') + ' 集</span></div>' +
      '<div class="review-reason">' + esc(item.reason || '') + '</div>' +
      '<div class="mono muted" style="font-size:.76rem">' + esc(key) + '</div>' +
      '</div><div>' + (item.original_title ? '<span class="muted">' + esc(item.original_title) + '</span>' : '') + '</div></div>';

    const candidates = item.candidates || [];
    if (candidates.length) {
      html += '<div class="candidates">';
      for (const candidate of candidates) {
        const checked = String(selected) === String(candidate.subject_id);
        html += '<label class="candidate' + (checked ? ' selected' : '') + '">' +
          '<input type="radio" name="pick-' + esc(key) + '" value="' + num(candidate.subject_id) + '"' +
          (checked ? ' checked' : '') +
          ' data-action="pick" data-series="' + esc(key) + '" data-subject="' + num(candidate.subject_id) + '">' +
          '<span><span class="mono">' + num(candidate.subject_id) + '</span> ' +
          '<strong>' + esc(candidate.name_cn || candidate.name) + '</strong>' +
          (candidate.name && candidate.name !== candidate.name_cn ? ' <span class="muted">' + esc(candidate.name) + '</span>' : '') +
          '<div class="meta">' + esc(candidate.year || '?') + ' 年 · ' + esc(candidate.eps || '?') + ' 集 · ' +
          '<a href="https://bgm.tv/subject/' + num(candidate.subject_id) + '" target="_blank" rel="noreferrer">bgm.tv</a></div></span>' +
          '<span class="score" style="margin-left:auto">' + Number(candidate.score || 0).toFixed(2) + '</span>' +
          '</label>';
      }
      html += '</div>';
    } else {
      html += '<p class="muted">没有搜索结果，请先在「映射」页用关键词手动搜索条目 ID。</p>';
    }

    html += '<div class="review-actions">' +
      '<label class="field">本地起集<input data-role="from" type="number" min="1" value="1"></label>' +
      '<label class="field">止集<input data-role="to" type="number" min="1" value="' +
      esc(Math.max(num(item.episode_count) || 1, 1)) + '"></label>' +
      '<label class="field">手动 subject id<input data-role="manual" type="number" min="1" placeholder="可选"></label>' +
      '<button class="btn primary" data-action="confirm-map" data-series="' + esc(key) + '">确认映射</button>' +
      '<button class="btn danger" data-action="ignore-map" data-series="' + esc(key) + '">移除候选</button>' +
      '</div></div>';
  }
  return html + '</section>';
}

function viewMappings() {
  const status = state.status;
  const accountOptions = ((status.config && status.config.target && status.config.target.accounts) || [])
    .map((account) => '<option value="' + esc(account.name) + '">' + esc(account.name) + '</option>').join('');
  const search = state.search;

  let html = '<section class="section"><h2>添加 / 修改映射</h2><div class="card">' +
    '<p class="muted">区间映射用于「一季文件夹里塞了多季」的情况，例如 1–24 对应第一季、25–48 对应第二季。</p>' +
    '<form id="mapForm" class="row" style="align-items:flex-end">' +
    '<label class="field">账号<select id="mapAccount">' + accountOptions + '</select></label>' +
    '<label class="field" style="flex:2 1 18rem">series key<input id="mapSeries" placeholder="fnos:xxxxxxxx:s1（从待复核页复制）" required></label>' +
    '<label class="field">Bangumi subject<input id="mapSubject" type="number" min="1" required></label>' +
    '<label class="field">名称（可选）<input id="mapName"></label>' +
    '<label class="field">起集<input id="mapFrom" type="number" min="1" value="1"></label>' +
    '<label class="field">止集<input id="mapTo" type="number" min="1" value="9999"></label>' +
    '<button class="btn primary" type="submit">保存映射</button>' +
    '</form></div></section>';

  html += '<section class="section"><h2>搜索 Bangumi 条目</h2><div class="card">' +
    '<form id="searchForm" class="row" style="align-items:flex-end">' +
    '<label class="field" style="flex:1 1 18rem">关键词<input id="searchQ" placeholder="例如：药屋少女的呢喃" value="' + esc(search.q) + '"></label>' +
    '<button class="btn" type="submit"' + (search.loading ? ' disabled' : '') + '>' + (search.loading ? '搜索中…' : '搜索') + '</button>' +
    '</form>' +
    (search.error ? '<p style="color:var(--bad)">' + esc(search.error) + '</p>' : '') +
    (search.results.length ? '<div class="table-wrap" style="margin-top:.7rem"><table><thead><tr>' +
      '<th>subject</th><th>名称</th><th class="num">年份</th><th class="num">集数</th><th></th></tr></thead><tbody>' +
      search.results.map((hit) =>
        '<tr><td class="mono">' + num(hit.subject_id) + '</td>' +
        '<td>' + esc(hit.name_cn || hit.name) + (hit.name_cn && hit.name_cn !== hit.name ? ' <span class="muted">' + esc(hit.name) + '</span>' : '') + '</td>' +
        '<td class="num">' + esc(hit.year || '?') + '</td><td class="num">' + esc(hit.eps || '?') + '</td>' +
        '<td><button class="btn small" data-action="use-subject" data-subject="' + num(hit.subject_id) + '" data-name="' + esc(hit.name_cn || hit.name) + '">使用</button></td></tr>'
      ).join('') +
      '</tbody></table></div>' : '') +
    '</div></section>';

  html += '<section class="section"><h2>当前映射（' + (status.mappings || []).length + '）</h2>' +
    '<div class="table-wrap"><table><thead><tr><th>账号</th><th>series key</th><th>区间</th>' +
    '<th>subject</th><th class="hide-sm">名称</th><th>来源</th><th class="num hide-sm">已推送</th><th></th></tr></thead><tbody>';
  for (const mapping of status.mappings || []) {
    html += '<tr><td>' + esc(mapping.account) + '</td>' +
      '<td class="mono" style="font-size:.78rem">' + esc(mapping.series_key) + '</td>' +
      '<td>' + num(mapping.ep_from) + '–' + num(mapping.ep_to) + '</td>' +
      '<td><a href="https://bgm.tv/subject/' + num(mapping.subject_id) + '" target="_blank" rel="noreferrer">' + num(mapping.subject_id) + '</a></td>' +
      '<td class="hide-sm">' + esc(mapping.subject_name || '') + '</td>' +
      '<td>' + badge(mapping.strategy + (Number(mapping.confidence) ? ' ' + Number(mapping.confidence).toFixed(2) : ''), mapping.confirmed ? 'ok' : 'info') + '</td>' +
      '<td class="num hide-sm">' + num(mapping.pushed) + '</td>' +
      '<td><button class="btn small danger" data-action="delete-map" data-account="' + esc(mapping.account) +
      '" data-series="' + esc(mapping.series_key) + '" data-from="' + num(mapping.ep_from) + '">删除</button></td></tr>';
  }
  if (!(status.mappings || []).length) html += '<tr><td colspan="8" class="empty">还没有映射，跑一轮同步会自动生成</td></tr>';
  return html + '</tbody></table></div></section>';
}

function viewAccounts() {
  const status = state.status;
  const accounts = status.accounts || [];
  let html = '<section class="section"><h2>Bangumi 账号</h2>' +
    '<p class="muted">令牌保存在状态库里，有效期约 7 天，程序会自动刷新。首次使用或授权被撤销后，在容器里执行下面的命令。</p>' +
    '<div class="table-wrap"><table><thead><tr><th>账号</th><th>令牌</th><th class="hide-sm">过期时间</th><th>Bangumi 用户</th><th>授权命令</th></tr></thead><tbody>';
  for (const account of accounts) {
    const command = 'docker compose run --rm bangumi-syncer oauth --account ' + account.name + ' --manual';
    html += '<tr><td>' + esc(account.name) + '</td>' +
      '<td>' + (account.has_token ? badge('已保存', 'ok') : badge('缺失', 'warn')) + '</td>' +
      '<td class="hide-sm">' + esc(fmtTime(account.expires_at, status.timezone)) + '</td>' +
      '<td>' + esc(account.remote_user || '-') + '</td>' +
      '<td><code style="font-size:.76rem">' + esc(command) + '</code> ' +
      '<button class="btn small" data-action="copy" data-copy="' + esc(command) + '">复制</button></td></tr>';
  }
  if (!accounts.length) {
    html += '<tr><td colspan="5" class="empty">还没有授权过，见「设置 → Bangumi API 获取指引」</td></tr>';
  }
  html += '</tbody></table></div></section>';

  html += '<section class="section"><h2>安全提示</h2><div class="card"><ul class="muted" style="margin:.2rem 0 .2rem 1.1rem;padding:0">' +
    '<li>这个页面没有登录鉴权，请只在局域网内暴露（<code>server.listen</code> 可改为 <code>127.0.0.1:8099</code>）。</li>' +
    '<li>写操作要求自定义请求头，跨站表单无法直接调用。</li>' +
    '<li>页面不会显示令牌内容，配置里的密钥也会打码。</li></ul></div></section>';
  return html;
}

function viewLogs() {
  return '<section class="section"><h2>实时日志</h2>' +
    '<p class="muted">最近 400 行，每 2 秒刷新。</p>' +
    '<pre class="logs" id="logPane">' + esc((state.status.logs || []).join('\n')) + '</pre></section>';
}

/* ---------------------------------------------------------------- settings */
function viewSettings() {
  if (state.settingsError) {
    return '<section class="section"><div class="card"><h2>读取设置失败</h2>' +
      '<p class="muted">' + esc(state.settingsError) + '</p>' +
      '<button class="btn primary" data-action="refresh">重试</button></div></section>';
  }
  if (!state.settings) {
    return '<section class="section"><div class="card"><div class="empty">读取设置中…</div></div></section>';
  }
  const values = state.settings.values || {};
  const accounts = state.settings.accounts || [];
  const source = state.settings.source || { users: [] };
  const sourceError = state.settings.source_error;
  const prefs = state.prefs;

  const option = (role, value, label, current) =>
    '<button type="button" data-action="set-pref" data-pref="' + role + '" data-value="' + esc(value) + '"' +
    ' data-role="' + role + '-option" class="' + (String(current) === String(value) ? 'active' : '') + '">' +
    esc(label) + '</button>';

  let html = '';
  html += '<div class="section"><h2>同步设置</h2><div class="card">' +
    '<div class="form-grid">' +
    '<label class="check"><input type="checkbox" id="set-dry-run"' + (values.dry_run ? ' checked' : '') + '> 演练模式（不写入 Bangumi）</label>' +
    '<label class="field">同步间隔（秒）<input type="number" id="set-interval" min="30" max="86400" value="' + esc(values.interval_seconds) + '"></label>' +
    '<label class="field">看完判定比例<input type="number" id="set-ratio" min="0.05" max="1" step="0.01" value="' + esc(values.watch_ratio) + '"></label>' +
    '<label class="field">最短进度（秒）<input type="number" id="set-minpos" min="0" max="3600" value="' + esc(values.min_position_seconds) + '"></label>' +
    '</div><div class="row" style="margin-top:.8rem">' +
    '<label class="check"><input type="checkbox" id="set-verify"' + (values.verify_remote ? ' checked' : '') + '> 写入前核对远端已有收藏</label>' +
    '<label class="check"><input type="checkbox" id="set-inprogress"' + (values.mark_in_progress ? ' checked' : '') + '> 有进度时设为「在看」</label>' +
    '<label class="check"><input type="checkbox" id="set-completed"' + (values.mark_completed ? ' checked' : '') + '> 全部看完设为「看过」</label>' +
    '</div><p class="hint">改动保存后无需重启，下一轮同步生效。</p></div></div>';

  html += '<div class="section"><h2>匹配</h2><div class="card"><div class="form-grid">' +
    '<label class="field">最低分（0–1）<input type="number" id="set-minscore" min="0" max="1" step="0.01" value="' + esc(values.match ? values.match.min_score : 0.86) + '"></label>' +
    '<label class="field">与第二名最小差距<input type="number" id="set-minmargin" min="0" max="1" step="0.01" value="' + esc(values.match ? values.match.min_margin : 0.06) + '"></label>' +
    '<label class="field">候选数量<input type="number" id="set-searchlimit" min="1" max="20" value="' + esc(values.match ? values.match.search_limit : 8) + '"></label>' +
    '</div><p class="hint">分数低于阈值或前两名过近时不会自动映射，会进入「待复核」。</p></div></div>';

  html += '<div class="section"><h2>媒体库</h2><div class="card"><div class="form-grid wide">' +
    '<label class="field">只同步这些目录（每行一个）<textarea id="set-include" placeholder="/vol1/1000/影片/动漫">' + esc((values.include_paths || []).join('\n')) + '</textarea></label>' +
    '<label class="field">排除目录（每行一个）<textarea id="set-exclude" placeholder="（可选）">' + esc((values.exclude_paths || []).join('\n')) + '</textarea></label>' +
    '</div><div class="row" style="margin-top:.8rem">' +
    '<label class="check"><input type="checkbox" id="set-skip0"' + (values.skip_season_zero ? ' checked' : '') + '> 跳过特别篇（Season 0）</label>' +
    '<label class="field" style="max-width:9rem">每季最少集数<input type="number" id="set-mineps" min="1" max="1000" value="' + esc(values.min_episodes) + '"></label>' +
    '</div><p class="hint">留空表示不过滤；路径是飞牛宿主机的绝对路径。</p></div></div>';

  html += '<div class="section"><h2>飞牛影视用户</h2><div class="card">' +
    '<div class="row" style="justify-content:space-between;align-items:center;margin-bottom:.5rem">' +
    '<span class="muted">来自媒体库的账号（季 ' + num(source.seasons) + ' · 集 ' + num(source.episodes) + '）</span>' +
    '<span class="actions"><button class="btn small" data-action="refresh-users">刷新用户列表</button></span></div>' +
    (sourceError ? '<p style="color:var(--warn);font-size:.84rem">读取媒体库失败：' + esc(sourceError) + '</p>' : '') +
    '<div class="table-wrap"><table><thead><tr><th>用户</th><th class="num">播放记录</th><th class="num hide-sm">已看完</th>' +
    '<th>同步到账号</th><th>单独阈值</th><th></th></tr></thead><tbody>' +
    (state.userRows.length ? state.userRows.map((row) => {
      const accountOptions = accounts.map((name) =>
        '<option value="' + esc(name) + '"' + (name === row.account ? ' selected' : '') + '>' + esc(name) + '</option>').join('');
      return '<tr data-user="' + esc(row.name) + '">' +
        '<td><label class="check"><input type="checkbox" data-role="user-enabled"' + (row.enabled ? ' checked' : '') + '> ' + esc(row.name) + '</label>' +
        (row.missing ? '<div class="hint">媒体库中已不存在</div>' : '') + '</td>' +
        '<td class="num">' + (row.plays === null ? '-' : num(row.plays)) + '</td>' +
        '<td class="num hide-sm">' + (row.finished === null ? '-' : num(row.finished)) + '</td>' +
        '<td><select data-role="user-account">' + accountOptions + '</select></td>' +
        '<td><input type="number" data-role="user-ratio" min="0.05" max="1" step="0.01" placeholder="继承全局" value="' + (row.watch_ratio == null ? '' : esc(row.watch_ratio)) + '"></td>' +
        '<td><button class="btn small ghost" data-action="remove-user" data-user="' + esc(row.name) + '">移除</button></td></tr>';
    }).join('') : '<tr><td colspan="6" class="empty">没有可同步的用户</td></tr>') +
    '</tbody></table></div>' +
    '<p class="hint">勾选即参与同步；勾掉不会删除已有映射，只是不再读取该用户的进度。新发现的用户默认不勾选。</p>' +
    '</div></div>';

  html += '<div class="section"><h2>界面个性与外观</h2><div class="card">' +
    '<div class="split">' +
    '<div><label class="field">主题</label><div class="seg" data-role="theme-seg">' +
    option('theme', 'auto', '跟随系统', prefs.theme) + option('theme', 'light', '浅色', prefs.theme) + option('theme', 'dark', '深色', prefs.theme) +
    '</div><p class="hint">「跟随系统」会随你的浏览器/系统深色模式自动切换。</p></div>' +
    '<div><label class="field">主题色</label><div class="swatches">' +
    ACCENTS.map((color) => '<button type="button" class="swatch' + (prefs.accent.toLowerCase() === color ? ' active' : '') +
      '" data-action="set-pref" data-pref="accent" data-value="' + color + '" data-role="accent-option" style="background:' + color + '" title="' + color + '"></button>').join('') +
    '<input type="color" data-role="accent-custom" value="' + esc(prefs.accent) + '" title="自定义颜色" style="width:2rem;height:2rem;padding:0;border-radius:50%">' +
    '</div><p class="hint">影响链接、按钮、高亮等强调色，只保存在本浏览器。</p></div>' +
    '<div><label class="field">字号 <span id="font-scale-value" class="muted">' + Math.round(prefs.fontScale * 100) + '%</span></label>' +
    '<input type="range" data-role="font-scale" min="0.85" max="1.25" step="0.05" value="' + esc(prefs.fontScale) + '"></div>' +
    '<div><label class="field">密度</label><div class="seg" data-role="density-seg">' +
    option('density', 'comfortable', '舒适', prefs.density) + option('density', 'compact', '紧凑', prefs.density) + '</div></div>' +
    '</div><div class="actions" style="margin-top:.8rem">' +
    '<button class="btn" data-action="reset-prefs">恢复默认外观</button>' +
    '<span class="muted" style="font-size:.8rem">个性化只影响当前浏览器，不写入配置。</span></div></div></div>';

  html += '<div class="section"><h2>Bangumi API 获取指引</h2><div class="card">' +
    '<ol class="steps">' +
    '<li>打开 <a href="https://bgm.tv/dev/apps" target="_blank" rel="noreferrer">bgm.tv/dev/apps</a>，登录后点「创建新应用」。</li>' +
    '<li>回调地址（Redirect URI）填：<code>http://127.0.0.1:8765/oauth/callback</code>' +
    '<button class="btn small" data-action="copy" data-copy="http://127.0.0.1:8765/oauth/callback">复制</button></li>' +
    '<li>创建后复制 <strong>App ID</strong> 与 <strong>App Secret</strong>，写入与 <code>docker-compose.yml</code> 同目录的 <code>.env</code>：' +
    '<pre class="snippet"><code>BGM_CLIENT_ID=你的AppID\nBGM_CLIENT_SECRET=你的AppSecret</code>' +
    '<button class="btn small" data-action="copy" data-copy="BGM_CLIENT_ID=\nBGM_CLIENT_SECRET=">复制模板</button></pre></li>' +
    '<li>让容器读到新环境变量：<pre class="snippet"><code>docker compose up -d</code>' +
    '<button class="btn small" data-action="copy" data-copy="docker compose up -d">复制</button></pre></li>' +
    '<li>在 NAS 上执行授权（会打印一个链接，浏览器打开后把 code 粘回来）：' +
    '<pre class="snippet"><code>docker compose run --rm bangumi-syncer oauth --account main --manual</code>' +
    '<button class="btn small" data-action="copy" data-copy="docker compose run --rm bangumi-syncer oauth --account main --manual">复制</button></pre></li>' +
    '<li>刷新本页，「账号」页应显示「已保存」。访问令牌约 7 天有效，程序会用 refresh token 自动续期。</li>' +
    '</ol><p class="hint">回调地址必须与创建应用时填写的一致；如果应用已存在，可在其设置里修改。' +
    '不要把 App Secret 提交到公开仓库（<code>.env</code> 已被 <code>.gitignore</code> 忽略）。</p>' +
    '</div></div>';

  html += '<div class="actions" style="position:sticky;bottom:0;padding:.8rem 0;background:var(--bg)">' +
    '<button class="btn primary" data-action="save-settings">保存设置</button>' +
    '<button class="btn" data-action="reset-settings">恢复 config.toml 默认</button>' +
    '<span class="muted" style="font-size:.8rem">保存后立即生效（下一轮同步）</span></div>';
  return html;
}

/* ------------------------------------------------------------- settings io */
function splitLines(text) {
  return String(text || '').split('\n').map((line) => line.trim()).filter(Boolean);
}

function buildUserRows() {
  const values = (state.settings && state.settings.values) || {};
  const source = (state.settings && state.settings.source) || { users: [] };
  const accounts = (state.settings && state.settings.accounts) || [];
  const sourceByName = {};
  (source.users || []).forEach((user) => { sourceByName[user.name] = user; });
  const rows = [];
  const seen = {};
  for (const user of values.users || []) {
    const found = sourceByName[user.fnos_user];
    rows.push({
      name: user.fnos_user,
      plays: found ? found.plays : null,
      finished: found ? found.finished : null,
      enabled: !!user.enabled,
      account: user.account,
      watch_ratio: user.watch_ratio,
      missing: !found,
    });
    seen[user.fnos_user] = true;
  }
  for (const user of source.users || []) {
    if (seen[user.name]) continue;
    rows.push({
      name: user.name,
      plays: user.plays,
      finished: user.finished,
      enabled: false,
      account: accounts[0] || 'main',
      watch_ratio: null,
      missing: false,
    });
  }
  state.userRows = rows;
}

function collectSettings() {
  const match = {
    min_score: Number(($('#set-minscore') || {}).value),
    min_margin: Number(($('#set-minmargin') || {}).value),
    search_limit: Number(($('#set-searchlimit') || {}).value),
  };
  return {
    dry_run: !!($('#set-dry-run') || {}).checked,
    interval_seconds: Number(($('#set-interval') || {}).value),
    watch_ratio: Number(($('#set-ratio') || {}).value),
    min_position_seconds: Number(($('#set-minpos') || {}).value),
    verify_remote: !!($('#set-verify') || {}).checked,
    mark_in_progress: !!($('#set-inprogress') || {}).checked,
    mark_completed: !!($('#set-completed') || {}).checked,
    include_paths: splitLines(($('#set-include') || {}).value),
    exclude_paths: splitLines(($('#set-exclude') || {}).value),
    skip_season_zero: !!($('#set-skip0') || {}).checked,
    min_episodes: Number(($('#set-mineps') || {}).value),
    match,
    users: state.userRows.map((row) => ({
      fnos_user: row.name,
      account: row.account,
      enabled: !!row.enabled,
      watch_ratio: row.watch_ratio === '' || row.watch_ratio === null || row.watch_ratio === undefined
        ? null : Number(row.watch_ratio),
    })),
  };
}

async function loadSettings() {
  state.settingsLoading = true;
  state.settingsError = '';
  if (state.tab === 'settings') render();
  try {
    state.settings = await apiGet('/api/settings');
    buildUserRows();
  } catch (err) {
    state.settingsError = err.message;
  } finally {
    state.settingsLoading = false;
    if (state.tab === 'settings') render();
  }
}

async function saveSettings(button) {
  button.disabled = true;
  try {
    const data = await apiSend('PUT', '/api/settings', collectSettings());
    state.settings = { ...state.settings, values: data.values, accounts: data.accounts };
    buildUserRows();
    toast('设置已保存，下一轮同步生效', 'ok');
    render();
    refresh();
  } catch (err) {
    toast('保存失败：' + err.message, 'err');
  } finally {
    button.disabled = false;
  }
}

async function resetSettings() {
  if (!confirm('恢复为 config.toml 里的值？运行时的修改会被清除。')) return;
  try {
    const data = await apiSend('POST', '/api/settings/reset');
    state.settings = { ...state.settings, values: data.values, accounts: data.accounts };
    buildUserRows();
    toast('已恢复 config.toml 默认值');
    render();
    refresh();
  } catch (err) {
    toast('恢复失败：' + err.message, 'err');
  }
}

async function refreshUsers() {
  try {
    const source = await apiGet('/api/source-users?refresh=1');
    state.settings = { ...state.settings, source, source_error: null };
    buildUserRows();
    toast('用户列表已刷新');
    render();
  } catch (err) {
    state.settings = { ...state.settings, source_error: err.message };
    toast('刷新失败：' + err.message, 'err');
    render();
  }
}

/* ----------------------------------------------------------------- render */
function render() {
  if (!state.status) return;
  renderHeader();
  const views = {
    overview: viewOverview,
    review: viewReview,
    mappings: viewMappings,
    accounts: viewAccounts,
    logs: viewLogs,
    settings: viewSettings,
  };
  document.getElementById('app').innerHTML = (views[state.tab] || viewOverview)();
  updateLayoutFlag();
  syncAppearanceControls();
  if (state.tab === 'logs') refreshLogs();
}

function setTab(tab) {
  state.tab = TABS.includes(tab) ? tab : 'overview';
  if (location.hash !== '#' + tab) history.replaceState(null, '', '#' + tab);
  if (state.tab === 'settings' && !state.settings && !state.settingsLoading) loadSettings();
  render();
}

/* ----------------------------------------------------------------- events */
document.addEventListener('click', async (event) => {
  const target = event.target.closest('[data-action]');
  if (!target) return;
  const action = target.dataset.action;

  if (action === 'tab') {
    event.preventDefault();
    setTab(target.dataset.tab);
    return;
  }

  if (action === 'refresh') {
    await handleRefresh();
    return;
  }

  if (action === 'run') {
    try {
      state.runBusy = true;
      target.disabled = true;
      await apiSend('POST', '/api/run');
      toast('已请求同步，稍后查看日志', 'ok');
      setTimeout(refresh, 1200);
    } catch (err) {
      toast('请求失败：' + err.message, 'err');
    } finally {
      state.runBusy = false;
      setTimeout(refresh, 4000);
    }
    return;
  }

  if (action === 'copy') {
    try {
      await navigator.clipboard.writeText(target.dataset.copy || '');
      toast('已复制');
    } catch (err) {
      toast('复制失败，请手动选择文本', 'err');
    }
    return;
  }

  if (action === 'set-pref') {
    const patch = {};
    const value = target.dataset.value;
    patch[target.dataset.pref] = target.dataset.pref === 'fontScale' ? Number(value) : value;
    updatePrefs(patch);
    return;
  }

  if (action === 'reset-prefs') {
    state.prefs = { ...DEFAULT_PREFS };
    state.themeOverride = '';
    applyPrefs(true);
    toast('已恢复默认外观');
    return;
  }

  if (action === 'save-settings') {
    await saveSettings(target);
    return;
  }

  if (action === 'reset-settings') {
    await resetSettings();
    return;
  }

  if (action === 'refresh-users') {
    await refreshUsers();
    return;
  }

  if (action === 'remove-user') {
    const name = target.dataset.user;
    state.userRows = state.userRows.filter((row) => row.name !== name);
    render();
    toast('已从同步列表移除，保存后生效');
    return;
  }

  if (action === 'confirm-map') {
    const card = target.closest('.review-card');
    const seriesKey = target.dataset.series;
    const account = card.dataset.account;
    const from = Number(card.querySelector('[data-role="from"]').value || 1);
    const to = Number(card.querySelector('[data-role="to"]').value || 9999);
    const manual = card.querySelector('[data-role="manual"]').value.trim();
    const subject = manual || (state.selected[seriesKey] || null);
    if (!subject) {
      toast('请先选择一个候选条目，或填入 subject id', 'err');
      return;
    }
    if (from > to) {
      toast('起集不能大于止集', 'err');
      return;
    }
    target.disabled = true;
    try {
      await apiSend('POST', '/api/map', {
        account, series_key: seriesKey, subject_id: Number(subject), ep_from: from, ep_to: to,
      });
      toast('映射已保存，下一轮同步生效', 'ok');
      delete state.selected[seriesKey];
      await refresh();
      render();
    } catch (err) {
      toast('保存失败：' + err.message, 'err');
    } finally {
      target.disabled = false;
    }
    return;
  }

  if (action === 'ignore-map') {
    const card = target.closest('.review-card');
    const seriesKey = target.dataset.series;
    if (!confirm('仅从待复核队列移除（不创建映射），之后如果再次匹配失败会重新出现。继续？')) return;
    try {
      await apiSend('DELETE', '/api/candidates?account=' + encodeURIComponent(card.dataset.account) +
        '&series_key=' + encodeURIComponent(seriesKey));
      toast('已移除候选');
      await refresh();
      render();
    } catch (err) {
      toast('移除失败：' + err.message, 'err');
    }
    return;
  }

  if (action === 'delete-map') {
    const account = target.dataset.account;
    const seriesKey = target.dataset.series;
    const from = target.dataset.from;
    if (!confirm('删除映射 ' + seriesKey + '（起集 ' + from + '）？该区间的推送记录也会清除。')) return;
    try {
      await apiSend('DELETE', '/api/map?account=' + encodeURIComponent(account) +
        '&series_key=' + encodeURIComponent(seriesKey) + '&ep_from=' + encodeURIComponent(from));
      toast('映射已删除');
      await refresh();
      render();
    } catch (err) {
      toast('删除失败：' + err.message, 'err');
    }
    return;
  }

  if (action === 'use-subject') {
    const subjectInput = document.getElementById('mapSubject');
    const nameInput = document.getElementById('mapName');
    if (subjectInput) subjectInput.value = target.dataset.subject;
    if (nameInput) nameInput.value = target.dataset.name || '';
    toast('已填入 subject ' + target.dataset.subject);
  }
});

document.addEventListener('change', (event) => {
  const target = event.target;
  if (target.dataset && target.dataset.action === 'pick') {
    state.selected[target.dataset.series] = Number(target.dataset.subject);
    document.querySelectorAll('.candidate').forEach((node) => node.classList.remove('selected'));
    const label = target.closest('.candidate');
    if (label) label.classList.add('selected');
    return;
  }

  const row = target.closest('tr[data-user]');
  if (row) {
    const name = row.dataset.user;
    const entry = state.userRows.find((item) => item.name === name);
    if (!entry) return;
    if (target.dataset.role === 'user-enabled') entry.enabled = target.checked;
    if (target.dataset.role === 'user-account') entry.account = target.value;
    if (target.dataset.role === 'user-ratio') {
      const raw = target.value.trim();
      entry.watch_ratio = raw === '' ? null : Number(raw);
    }
    return;
  }

  if (target.dataset && target.dataset.role === 'accent-custom') {
    updatePrefs({ accent: target.value });
  }
});

document.addEventListener('input', (event) => {
  const target = event.target;
  if (target.dataset && target.dataset.role === 'font-scale') {
    updatePrefs({ fontScale: Number(target.value) });
  }
});

document.addEventListener('submit', async (event) => {
  if (event.target.id === 'mapForm') {
    event.preventDefault();
    const payload = {
      account: document.getElementById('mapAccount').value,
      series_key: document.getElementById('mapSeries').value.trim(),
      subject_id: Number(document.getElementById('mapSubject').value),
      name: document.getElementById('mapName').value.trim(),
      ep_from: Number(document.getElementById('mapFrom').value || 1),
      ep_to: Number(document.getElementById('mapTo').value || 9999),
    };
    try {
      await apiSend('POST', '/api/map', payload);
      toast('映射已保存');
      document.getElementById('mapSeries').value = '';
      document.getElementById('mapSubject').value = '';
      document.getElementById('mapName').value = '';
      await refresh();
      render();
    } catch (err) {
      toast('保存失败：' + err.message, 'err');
    }
    return;
  }

  if (event.target.id === 'searchForm') {
    event.preventDefault();
    const query = document.getElementById('searchQ').value.trim();
    if (!query) return;
    state.search = { q: query, results: [], loading: true, error: '' };
    render();
    try {
      const data = await apiGet('/api/search?q=' + encodeURIComponent(query));
      state.search = { q: query, results: data.results || [], loading: false, error: '' };
    } catch (err) {
      state.search = { q: query, results: [], loading: false, error: err.message };
    }
    render();
  }
});

window.addEventListener('hashchange', () => {
  const tab = (location.hash || '#overview').slice(1);
  if (tab !== state.tab) setTab(tab);
});

window.addEventListener('resize', updateLayoutFlag);

if (window.matchMedia) {
  const media = window.matchMedia('(prefers-color-scheme: light)');
  const onChange = () => {
    if (!state.themeOverride && state.prefs.theme === 'auto') applyPrefs(false);
  };
  if (media.addEventListener) media.addEventListener('change', onChange);
  else if (media.addListener) media.addListener(onChange);
}

/* ------------------------------------------------------------------- boot */
(async function boot() {
  if (!TABS.includes(state.tab)) state.tab = 'overview';
  applyPrefs(false);
  await refresh();
  if (state.tab === 'settings') await loadSettings();
  render();
  startPolling();
})();
