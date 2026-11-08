'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const state = {folder: '', page: 1, pages: 1, sequence: 0, indexing: false, searching: false};
  const folderPath = () => $('library-folder').value.trim().replace(/^(["'])(.*)\1$/, '$2');
  let undoBatch = '';
  const controls = ['library-folder', 'library-pick', 'library-index', 'library-metadata', 'library-deduplicate'];
  const searchControls = ['library-date-from', 'library-date-to', 'library-author', 'library-title', 'library-sort', 'library-search-button', 'library-reset'];

  async function request(path, body) {
    let response;
    try {
      response = await fetch(path, {method: body === undefined ? 'GET' : 'POST',
        headers: body === undefined ? {} : {'Content-Type': 'application/json', 'X-Paper-Renamer': 'local'},
        body: body === undefined ? undefined : JSON.stringify(body), cache: 'no-store'});
    } catch (_) { throw new Error('无法连接本地程序，请确认启动窗口仍在运行。'); }
    const result = await response.json();
    if (!response.ok || result.error) throw new Error(result.error || `请求失败（${response.status}）`);
    return result;
  }
  function notice(message, type = 'success') {
    $('library-notice').textContent = message;
    $('library-notice').className = `notice ${type}`;
    $('library-notice').hidden = !message;
  }
  function updateControls() {
    controls.forEach(id => { $(id).disabled = state.indexing; });
    searchControls.forEach(id => { $(id).disabled = state.indexing || state.searching; });
    $('library-prev').disabled = state.indexing || state.searching || state.page <= 1;
    $('library-next').disabled = state.indexing || state.searching || state.page >= state.pages;
    $('library-index').textContent = state.indexing ? '正在建立索引…' : '建立 / 更新索引';
    $('library-restore-duplicates').disabled = state.indexing || !undoBatch;
  }
  function make(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  }
  function clearResults() {
    state.sequence += 1;
    state.folder = '';
    state.searching = false;
    state.page = state.pages = 1;
    $('library-results-body').replaceChildren();
    $('library-table').hidden = true;
    $('library-empty').hidden = false;
    $('library-empty').textContent = '选择文件夹并建立索引，即可检索文献。';
    $('library-count').textContent = '尚未载入文献库';
    $('library-index-info').textContent = '选择文件夹后可读取已保存的索引。';
    $('library-page').textContent = '第 1 / 1 页';
    $('library-duplicates').hidden = true;
    undoBatch = '';
    updateControls();
  }
  function renderDuplicates(report) {
    $('library-duplicates').hidden = !Object.keys(report).length;
    undoBatch = report.undo_batch || '';
    const moved = report.removal?.moved || [];
    const confirmed = report.confirmed || [];
    const suspected = report.suspected || [];
    const failures = [...(report.failures || []), ...(report.removal?.errors || [])];
    $('library-duplicates-summary').textContent = `已核对 ${report.checked || 0} 个 PDF；确定重复 ${confirmed.length} 个，已移除 ${moved.length} 个；${suspected.length} 组内容有差异，保留供核对。${failures.length ? ` ${failures.length} 个文件核对或移除未完成。` : ''}${report.restored ? ' 已执行恢复；取消自动移除后更新索引可重新检索恢复的副本。' : ''}`;
    const details = document.createDocumentFragment();
    const movedPaths = new Set(moved.map(item => item.source));
    const folder = folderPath();
    const relative = path => path.startsWith(folder + '/') ? path.slice(folder.length + 1) : path;
    confirmed.forEach(item => {
      const entry = make('div', 'duplicate-entry');
      entry.append(make('p', 'library-paper-title', item.title),
        make('p', 'file-path', `保留：${relative(item.keep)}`),
        make('p', 'file-path', `${movedPaths.has(item.duplicate) ? '已移除' : '重复副本'}：${relative(item.duplicate)}`),
        make('p', 'reason', item.reason));
      details.append(entry);
    });
    suspected.forEach(item => {
      const entry = make('div', 'duplicate-entry');
      entry.append(make('p', 'reason', item.reason));
      item.sources.forEach(path => entry.append(make('p', 'file-path', relative(path))));
      details.append(entry);
    });
    failures.forEach(item => details.append(make('p', 'reason', `${relative(item.source)}：${item.reason}`)));
    if (!confirmed.length && !suspected.length && !failures.length) details.append(make('p', 'reason', '未发现重复文献。'));
    $('library-duplicates-details').replaceChildren(details);
    updateControls();
  }
  function render(result, folder) {
    const fragment = document.createDocumentFragment();
    result.results.forEach(record => {
      const row = make('tr');
      const dateCell = make('td');
      dateCell.append(make('p', 'file-name', String(record.publication_date || '').slice(0, 4) || '时间未知'));
      if (record.date_source) dateCell.append(make('p', 'file-path', record.date_source));
      const authors = (record.authors || []).filter(Boolean);
      const authorCell = make('td', 'library-authors', authors.join('、') || record.corresponding_author || '作者未识别');
      const titleCell = make('td');
      titleCell.append(make('p', 'library-paper-title', record.title || record.filename), make('p', 'file-path', record.relative_path));
      if (record.journal_abbreviation) titleCell.append(make('span', 'badge', record.journal_abbreviation));
      if (record.preprint_source) titleCell.append(make('span', 'badge', '预印本'));
      if (record.arxiv_id) titleCell.append(make('p', 'file-path', `arXiv:${record.arxiv_id}`));
      if (record.status === 'error' || record.is_research_paper === false) {
        titleCell.append(make('span', `badge ${record.status === 'error' ? 'error' : 'skipped'}`, record.status === 'error' ? '读取失败' : '非论文 PDF'));
        if (record.reason) titleCell.append(make('p', 'reason', record.reason));
      }
      const openCell = make('td');
      const button = make('button', 'button button-secondary', '打开 PDF');
      button.type = 'button';
      button.setAttribute('aria-label', `打开 ${record.title || record.filename}`);
      button.addEventListener('click', async () => {
        button.disabled = true;
        try { await request('/api/library/open', {folder, source: record.source}); notice('已在系统默认 PDF 阅读器中打开文献。'); }
        catch (error) { notice(`${error.message}。如文件位置已变更，请更新索引。`, 'error'); }
        finally { button.disabled = false; }
      });
      openCell.append(button);
      row.append(dateCell, authorCell, titleCell, openCell);
      fragment.append(row);
    });
    $('library-results-body').replaceChildren(fragment);
    $('library-table').hidden = !result.results.length;
    $('library-empty').hidden = Boolean(result.results.length);
    $('library-empty').textContent = !result.indexed ? '此文件夹尚未完成索引，请点击「建立 / 更新索引」。' : result.indexed_total ? '没有匹配的文献，请调整检索条件。' : '此文件夹及子文件夹内未找到 PDF。';
    $('library-count').textContent = `匹配 ${result.total} 篇 / 已索引 ${result.indexed_total} 个 PDF`;
    state.page = result.page;
    state.pages = Math.max(1, Math.ceil(result.total / result.page_size));
    $('library-page').textContent = `第 ${state.page} / ${state.pages} 页 · 每页 ${result.page_size} 篇`;
    $('library-index-info').textContent = result.indexed ? `已保存 ${result.indexed_total} 个 PDF · 上次更新：${new Date(result.updated_at).toLocaleString('zh-CN')}。文件新增、移动或重命名后请更新索引。` : '尚无完整索引。点击「建立 / 更新索引」开始扫描。';
  }
  async function search(page = 1) {
    const folder = folderPath();
    if (!folder) { notice('请先选择文献库文件夹。', 'warning'); $('library-folder').focus(); return; }
    const sequence = ++state.sequence;
    state.searching = true;
    updateControls();
    notice('');
    try {
      const result = await request('/api/library/search', {folder, page, page_size: 50,
        date_from: $('library-date-from').value.trim(), date_to: $('library-date-to').value.trim(),
        author: $('library-author').value.trim(), title: $('library-title').value.trim(), sort: $('library-sort').value});
      if (sequence !== state.sequence || folder !== folderPath()) return;
      state.folder = folder;
      render(result, folder);
      const report = await request('/api/library/duplicates', {folder});
      if (sequence !== state.sequence || folder !== folderPath()) return;
      renderDuplicates(report);
      try { localStorage.setItem('paperwise-library-folder', folder); } catch (_) { /* storage may be disabled */ }
      return true;
    } catch (error) { if (sequence === state.sequence) notice(error.message, 'error'); }
    finally { state.searching = false; updateControls(); }
  }
  function selectView() {
    const library = location.hash === '#library';
    $('rename-view').hidden = library;
    $('library-view').hidden = !library;
    $('notice').hidden = library || !$('notice').textContent;
    $('rename-tab').classList.toggle('active', !library);
    $('library-tab').classList.toggle('active', library);
    $('rename-tab').setAttribute('aria-current', library ? 'false' : 'page');
    $('library-tab').setAttribute('aria-current', library ? 'page' : 'false');
    if (library && folderPath() && state.folder !== folderPath() && !state.indexing) search();
  }
  $('rename-tab').addEventListener('click', () => { location.hash = 'rename'; });
  $('library-tab').addEventListener('click', () => { location.hash = 'library'; });
  window.addEventListener('hashchange', selectView);
  $('library-folder').addEventListener('input', () => { clearResults(); notice(''); });
  $('library-folder').addEventListener('change', () => { if (folderPath()) search(); });
  $('library-pick').addEventListener('click', async () => {
    $('library-pick').disabled = true;
    try {
      const result = await request('/api/pick-folder', {});
      if (result.path) { clearResults(); $('library-folder').value = result.path; await search(); }
    } catch (error) { notice(error.message, 'error'); }
    finally { updateControls(); }
  });
  $('library-search-form').addEventListener('submit', event => { event.preventDefault(); if (!state.indexing && !state.searching) search(); });
  $('library-sort').addEventListener('change', () => search());
  $('library-reset').addEventListener('click', () => {
    ['library-date-from', 'library-date-to', 'library-author', 'library-title'].forEach(id => { $(id).value = ''; });
    $('library-sort').value = 'date_desc';
    search();
  });
  $('library-prev').addEventListener('click', () => search(state.page - 1));
  $('library-next').addEventListener('click', () => search(state.page + 1));
  $('library-restore-duplicates').addEventListener('click', async () => {
    const folder = folderPath();
    state.indexing = true;
    updateControls();
    try {
      const result = await request('/api/library/restore-duplicates', {folder});
      await search();
      notice(`已恢复 ${result.restored.length} 个副本。${result.errors.length ? ` ${result.errors.length} 个恢复失败（原路径被占用或备份有变化），请查看文件夹中的 manifest.json。` : ''}取消自动移除后更新索引，可重新检索恢复的副本。`, result.errors.length ? 'warning' : 'success');
    } catch (error) { notice(error.message, 'error'); }
    finally { state.indexing = false; updateControls(); }
  });
  $('library-index-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (state.indexing) return;
    const folder = folderPath();
    if (!folder) { $('library-folder').focus(); return; }
    const sequence = ++state.sequence;
    state.indexing = true;
    updateControls(); notice('');
    $('library-progress').hidden = false;
    $('library-progress-bar').value = 0;
    $('library-progress-title').textContent = '正在查找文献…';
    $('library-progress-count').textContent = '';
    $('library-progress-file').textContent = '';
    try {
      const started = await request('/api/library/index', {folder, metadata: $('library-metadata').checked, deduplicate: $('library-deduplicate').checked});
      while (sequence === state.sequence) {
        const job = await request(`/api/jobs/${encodeURIComponent(started.job_id)}`);
        const {done, total, current, reused} = job.progress;
        $('library-progress-title').textContent = job.progress.phase === 'duplicates' ? '正在核对全部页面与重复文献…' : total ? '正在建立文献索引…' : '正在查找文献…';
        $('library-progress-count').textContent = `${done} / ${total} · 缓存 ${reused || 0}`;
        $('library-progress-file').textContent = current || '';
        $('library-progress-bar').value = total ? Math.round(done / total * 100) : 0;
        if (job.status === 'error') throw new Error(job.error || '索引未完成');
        if (job.status === 'done') {
          if (await search()) notice(`索引完成，共扫描 ${total} 个 PDF，复用 ${reused || 0} 个已有记录，移除 ${job.duplicates?.removed || 0} 个已核实的重复副本。核对依据见下方报告。`, job.duplicates?.errors ? 'warning' : 'success');
          break;
        }
        await new Promise(resolve => setTimeout(resolve, 700));
      }
    } catch (error) { notice(error.message, 'error'); }
    finally { state.indexing = false; $('library-progress').hidden = true; updateControls(); }
  });
  try { $('library-folder').value = localStorage.getItem('paperwise-library-folder') || ''; } catch (_) { /* optional */ }
  selectView();
  updateControls();
})();
