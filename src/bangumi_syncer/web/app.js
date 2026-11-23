/* Bangumi-syncer web UI — vanilla JS, no build step. */
'use strict';

const CSRF = { 'X-Requested-With': 'bangumi-syncer' };

const state = {
  status: null,
  tab: (location.hash || '#overview').slice(1),
  selected: {},   // series_key -> subject_id (review queue)
  search: { q: '', results: [], loading: false, error: '', picked: null },
  refreshTimer: null,
  logTimer: null,
  runBusy: false,
  lastLogAt: 0,
};

/* ------------------------------------------------------------------ utils */
const esc = (value) =>
  String(value === null || value === undefined ? '' : value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

const num = (value) => (value === null || value === undefined ? 0 : Number(value) || 0);

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

/* ---------------------------------------------------------------- refresh */
async function refresh() {
  try {
    state.status = await apiGet('/api/status');
    render();
  } catch (err) {
    document.getElementById('app').innerHTML =
      '<div class="card"><h2>无法连接后端</h2><p class="muted">' + esc(err.message) + '</p></div>';
  }
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
  const reviewCount = document.getElementById('reviewCount');
  reviewCount.textContent = String((status.review || []).length);
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
      : '') +
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
    '<thead><tr><th>飞牛用户</th><th>Bangumi 账号</th><th class="num">本轮集数</th>' +
    '<th class="num">匹配</th><th class="num">推送</th><th class="num">失败</th><th>状态</th></tr></thead><tbody>';
  const userResults = {};
  (last && last.users ? last.users : []).forEach((item) => { userResults[item.user] = item; });
  for (const user of status.users || []) {
    const result = userResults[user.fnos_user] || null;
    const stateBadge = !user.enabled
      ? badge('已禁用', 'info')
      : (result && result.error ? badge('错误', 'bad') : badge('正常', 'ok'));
    html += '<tr><td>' + esc(user.fnos_user) + '</td><td>' + esc(user.account) + '</td>' +
      '<td class="num">' + (result ? num(result.series_seen) : '-') + '</td>' +
      '<td class="num">' + (result ? num(result.matched) : '-') + '</td>' +
      '<td class="num">' + (result ? num(result.pushed) : '-') + '</td>' +
      '<td class="num">' + (result ? num(result.failed) : '-') + '</td>' +
      '<td>' + stateBadge + '</td></tr>';
  }
  if (!(status.users || []).length) html += '<tr><td colspan="7" class="empty">没有配置用户</td></tr>';
  html += '</tbody></table></div></section>';

  html += '<section class="section"><h2>运行历史</h2><div class="table-wrap"><table>' +
    '<thead><tr><th>开始</th><th>结果</th><th class="num">推送</th><th class="num">失败</th>' +
    '<th class="num">匹配</th><th class="num">待复核</th><th class="num">耗时</th><th>消息</th></tr></thead><tbody>';
  for (const run of status.runs || []) {
    html += '<tr><td>' + esc(run.started_at_human || '-') + '</td>' +
      '<td>' + (run.ok ? badge('成功', 'ok') : badge('失败', 'bad')) + '</td>' +
      '<td class="num">' + num(run.pushed) + '</td><td class="num">' + num(run.failed) + '</td>' +
      '<td class="num">' + num(run.matched) + '</td><td class="num">' + num(run.unmatched) + '</td>' +
      '<td class="num">' + esc(run.duration || '-') + '</td>' +
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
  const accounts = (status.config && status.config.target && status.config.target.accounts) || [];
  const accountOptions = accounts.map((account) =>
    '<option value="' + esc(account.name) + '">' + esc(account.name) + '</option>').join('');
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
    '<th>subject</th><th>名称</th><th>来源</th><th class="num">已推送</th><th></th></tr></thead><tbody>';
  for (const mapping of status.mappings || []) {
    html += '<tr><td>' + esc(mapping.account) + '</td>' +
      '<td class="mono" style="font-size:.78rem">' + esc(mapping.series_key) + '</td>' +
      '<td>' + num(mapping.ep_from) + '–' + num(mapping.ep_to) + '</td>' +
      '<td><a href="https://bgm.tv/subject/' + num(mapping.subject_id) + '" target="_blank" rel="noreferrer">' + num(mapping.subject_id) + '</a></td>' +
      '<td>' + esc(mapping.subject_name || '') + '</td>' +
      '<td>' + badge(mapping.strategy + (Number(mapping.confidence) ? ' ' + Number(mapping.confidence).toFixed(2) : ''), mapping.confirmed ? 'ok' : 'info') + '</td>' +
      '<td class="num">' + num(mapping.pushed) + '</td>' +
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
    '<div class="table-wrap"><table><thead><tr><th>账号</th><th>令牌</th><th>过期时间</th><th>Bangumi 用户</th><th>授权命令</th></tr></thead><tbody>';
  for (const account of accounts) {
    const command = 'docker compose run --rm bangumi-syncer oauth --account ' + account.name + ' --manual';
    html += '<tr><td>' + esc(account.name) + '</td>' +
      '<td>' + (account.has_token ? badge('已保存', 'ok') : badge('缺失', 'warn')) + '</td>' +
      '<td>' + esc(fmtTime(account.expires_at, status.timezone)) + '</td>' +
      '<td>' + esc(account.remote_user || '-') + '</td>' +
      '<td><code style="font-size:.76rem">' + esc(command) + '</code> ' +
      '<button class="btn small" data-action="copy" data-copy="' + esc(command) + '">复制</button></td></tr>';
  }
  if (!accounts.length) {
    html += '<tr><td colspan="5" class="empty">还没有授权过。先执行 <code>docker compose run --rm bangumi-syncer oauth --account main --manual</code></td></tr>';
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

function viewSettings() {
  const status = state.status;
  const config = status.config || {};
  return '<section class="section"><h2>运行配置（只读）</h2>' +
    '<p class="muted">修改 <code>config.toml</code> 后重启容器生效。这里的 client_secret 已打码。</p>' +
    '<pre class="json">' + esc(JSON.stringify(config, null, 2)) + '</pre></section>' +
    '<section class="section"><h2>命令行</h2><div class="card">' +
    '<pre class="json">' + esc([
      'docker compose logs -f',
      'docker compose run --rm bangumi-syncer status',
      'docker compose run --rm bangumi-syncer review',
      'docker compose run --rm bangumi-syncer search -k "关键词"',
      'docker compose run --rm bangumi-syncer run --once',
    ].join('\n')) + '</pre></div></section>';
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
  const view = views[state.tab] || viewOverview;
  document.getElementById('app').innerHTML = view();
  if (state.tab === 'logs') refreshLogs();
}

function setTab(tab) {
  state.tab = tab;
  if (location.hash !== '#' + tab) history.replaceState(null, '', '#' + tab);
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
        account, series_key: seriesKey, subject_id: Number(subject),
        ep_from: from, ep_to: to,
      });
      toast('映射已保存，下一轮同步生效', 'ok');
      delete state.selected[seriesKey];
      await refresh();
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
    return;
  }
});

document.addEventListener('change', (event) => {
  const target = event.target;
  if (target.dataset && target.dataset.action === 'pick') {
    state.selected[target.dataset.series] = Number(target.dataset.subject);
    document.querySelectorAll('.candidate').forEach((node) => node.classList.remove('selected'));
    const label = target.closest('.candidate');
    if (label) label.classList.add('selected');
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
    } catch (err) {
      toast('保存失败：' + err.message, 'err');
    }
    return;
  }

  if (event.target.id === 'searchForm') {
    event.preventDefault();
    const query = document.getElementById('searchQ').value.trim();
    if (!query) return;
    state.search = { q: query, results: [], loading: true, error: '', picked: null };
    render();
    try {
      const data = await apiGet('/api/search?q=' + encodeURIComponent(query));
      state.search = { q: query, results: data.results || [], loading: false, error: '', picked: null };
    } catch (err) {
      state.search = { q: query, results: [], loading: false, error: err.message, picked: null };
    }
    render();
  }
});

window.addEventListener('hashchange', () => {
  const tab = (location.hash || '#overview').slice(1);
  if (tab !== state.tab) setTab(tab);
});

/* ------------------------------------------------------------------- boot */
(async function boot() {
  if (!['overview', 'review', 'mappings', 'accounts', 'logs', 'settings'].includes(state.tab)) {
    state.tab = 'overview';
  }
  await refresh();
  startPolling();
})();
