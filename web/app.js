'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const state = {records: [], folder: '', busy: false, filter: 'all', generation: 0, lastOperation: null};
  const statusLabels = {ready: '可重命名', review: '需要核对', skipped: '已跳过', error: '读取失败'};
  const confidenceLabels = {high: '高置信度', medium: '中置信度', low: '低置信度'};
  const busyControls = ['folder', 'pick-folder', 'scan-button', 'recursive', 'metadata', 'ai-enabled'];
  const cleanPath = value => value.trim().replace(/^(["'])(.*)\1$/, '$2');
  const renameEligible = record => Boolean(record && record.rename_eligible !== false && record.is_research_paper !== false && !['skipped', 'error'].includes(record.status));
  const recordLocked = record => !renameEligible(record) || Boolean(record.completed);
  const authorSourceLabel = record => ({last_author: '末位作者', manual: record.corresponding_author.trim() ? '手动填写' : '手动留空'}[record.author_source] || '未识别，留空');

  async function request(path, data) {
    let response;
    try {
      response = await fetch(path, {method: data === undefined ? 'GET' : 'POST', headers: data === undefined ? {} : {'Content-Type': 'application/json', 'X-Paper-Renamer': 'local'}, body: data === undefined ? undefined : JSON.stringify(data), cache: 'no-store'});
    } catch (_) { throw new Error('无法连接本地程序，请确认启动窗口仍在运行。'); }
    let result;
    try { result = await response.json(); } catch (_) { throw new Error('本地程序返回了无法读取的响应。请查看启动窗口。'); }
    if (!response.ok || result.error) throw new Error(typeof result.error === 'string' ? result.error : `请求失败（${response.status}）`);
    return result;
  }

  function notice(message, type = 'success') {
    $('notice').textContent = message;
    $('notice').className = `notice${type === 'success' ? '' : ` ${type}`}`;
    $('notice').hidden = !message;
  }
  function setBusy(busy) {
    state.busy = busy;
    busyControls.forEach(id => { $(id).disabled = busy; });
    ['api-base', 'ai-model', 'api-key'].forEach(id => { $(id).disabled = busy || !$('ai-enabled').checked; });
    document.querySelectorAll('.record-checkbox, .metadata-input, .filename-input').forEach(input => {
      const record = state.records.find(row => row.id === input.dataset.recordId);
      input.disabled = busy || recordLocked(record);
    });
    document.querySelectorAll('.filter').forEach(button => { button.disabled = busy; });
    $('select-all').disabled = busy;
    updateSelection();
  }
  function invalidateResults() {
    state.generation += 1;
    state.records = [];
    state.folder = '';
    state.lastOperation = null;
    state.filter = 'all';
    $('results-content').hidden = true;
    $('empty-state').hidden = false;
    $('results-body').replaceChildren();
    $('results-description').textContent = '扫描完成后，识别结果将在这里出现。';
    $('progress-panel').hidden = true;
    document.querySelectorAll('.filter').forEach(button => button.classList.toggle('active', button.dataset.filter === 'all'));
    updateSelection();
  }
  function truncateBytes(value, budget, preserveTrailingPeriods = false) {
    const encoder = new TextEncoder();
    let result = '', used = 0;
    for (const character of value) {
      const size = encoder.encode(character).length;
      if (used + size > Math.max(0, budget)) break;
      result += character; used += size;
    }
    return result.replace(preserveTrailingPeriods ? / +$/g : /[. ]+$/g, '');
  }
  function sanitizeComponent(value, maxBytes = 160, preserveTrailingPeriods = false) {
    let text = String(value || '').normalize('NFC').replace(/[<>:"/\\|?*\u0000-\u001f\u007f]/g, ' ').replace(/\p{Cf}/gu, '').replace(/\s+/g, ' ').replace(/^[. ]+/g, '');
    text = text.replace(preserveTrailingPeriods ? / +$/g : /[. ]+$/g, '');
    if (/^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)/i.test(text)) text = `_${text}`;
    return truncateBytes(text, maxBytes, preserveTrailingPeriods);
  }
  function buildFilename(record) {
    const published = normalizeYear(record.publication_date) || '时间未知';
    const authorName = sanitizeComponent(record.corresponding_author, 128);
    const journal = sanitizeComponent(record.journal_abbreviation, 64, true);
    const title = sanitizeComponent(record.title, 240);
    if (!journal || !title) return '';
    const prefix = [published, authorName, journal].filter(Boolean).join('-');
    const budget = 240 - new TextEncoder().encode(`${prefix}-.pdf`).length;
    const shortenedTitle = truncateBytes(title, budget);
    return shortenedTitle ? `${prefix}-${shortenedTitle}.pdf` : '';
  }
  function normalizeYear(value) {
    const text = String(value || '').trim();
    return /^\d{4}$/.test(text) && Number(text) >= 1 ? text : '';
  }
  function validRecord(record) {
    const filename = String(record.proposed_filename || '').trim();
    return Boolean(renameEligible(record) && filename && /\.pdf$/i.test(filename) && !/[\/\\\u0000-\u001f]/.test(filename) && (!String(record.publication_date || '').trim() || normalizeYear(record.publication_date)) && record.journal_abbreviation.trim() && record.title.trim() && !record.completed);
  }
  function selectedRecords() { return state.records.filter(record => record.selected && validRecord(record)); }
  function updateSelection() {
    const selected = selectedRecords();
    const matchingPath = state.folder && cleanPath($('folder').value) === state.folder;
    $('selection-count').textContent = `已选 ${selected.length} 项`;
    $('apply-summary').textContent = `已选择 ${selected.length} 个文件`;
    $('apply-button').textContent = state.busy ? '处理中…' : selected.length ? `重命名 ${selected.length} 个文件` : '重命名所选文件';
    $('apply-button').disabled = state.busy || !matchingPath || !selected.length;
    $('undo-button').disabled = state.busy || !state.lastOperation || state.lastOperation.folder !== cleanPath($('folder').value);
    $('export-button').disabled = state.busy || !state.records.length;
    const ready = state.records.filter(record => record.status === 'ready' && validRecord(record));
    const count = ready.filter(record => record.selected).length;
    $('select-all').checked = ready.length > 0 && count === ready.length;
    $('select-all').indeterminate = count > 0 && count < ready.length;
  }
  function make(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  }
  function noteFor(record, hint) {
    const valid = validRecord(record);
    if (!renameEligible(record)) { hint.textContent = record.status === 'error' ? '此文件无法读取，暂不能重命名。' : '此文件识别为非论文，已跳过，不能重命名。'; hint.className = 'target-hint'; }
    else if (record.completed) { hint.textContent = record.completedReason || '已重命名；再次处理请重新扫描。'; hint.className = 'target-hint'; }
    else if (!valid) { hint.textContent = '请补全期刊简称、标题和新文件名；发表年份请使用 YYYY，年份和作者均可留空。'; hint.className = 'target-hint invalid'; }
    else if (!record.corresponding_author.trim()) { hint.textContent = '作者已留空：可稍后核对，也可直接勾选重命名，文件名中省略作者。'; hint.className = 'target-hint'; }
    else if (!record.publication_date) { hint.textContent = '未确认发表时间，前缀为「时间未知」；可手动填写。'; hint.className = 'target-hint'; }
    else if (record.author_source === 'last_author') { hint.textContent = '命名使用末位作者全名，可直接批量重命名。'; hint.className = 'target-hint'; }
    else { hint.textContent = record.status === 'ready' ? '信息可直接编辑，勾选后批量重命名。' : '确认论文类型和命名信息后，可手动勾选。'; hint.className = 'target-hint'; }
  }
  function renderRow(record) {
    if (!renameEligible(record)) record.selected = false;
    const row = make('tr', record.selected ? 'selected' : '');
    row.dataset.recordId = record.id;
    if (record.completed) row.classList.add('is-renamed');
    const selectCell = make('td');
    const checkbox = make('input', 'record-checkbox');
    checkbox.type = 'checkbox';
    checkbox.checked = Boolean(record.selected);
    checkbox.dataset.recordId = record.id;
    checkbox.setAttribute('aria-label', `选择 ${record.filename || record.relative_path}`);
    checkbox.disabled = state.busy || recordLocked(record);
    checkbox.addEventListener('change', () => {
      if (checkbox.checked && !validRecord(record)) {
        checkbox.checked = false;
        record.selected = false;
        notice(renameEligible(record) ? '请补全期刊简称、标题及新文件名，并检查年份格式；作者可以留空。' : '非论文或读取失败的 PDF 不具备重命名资格。', 'warning');
        row.querySelector('.metadata-input:placeholder-shown')?.focus();
      } else { record.selected = checkbox.checked; }
      row.classList.toggle('selected', record.selected);
      updateSelection();
    });
    selectCell.append(checkbox);
    const sourceCell = make('td');
    sourceCell.append(make('p', 'file-name', record.filename || record.source.split(/[\\/]/).pop()), make('p', 'file-path', record.relative_path || record.source));
    const flags = make('div', 'record-flags');
    flags.append(make('span', `badge ${record.completed ? '' : record.status}`, record.completed ? (record.completedLabel || '已重命名') : (statusLabels[record.status] || '需要核对')));
    const authorBadge = make('span', 'badge', record.corresponding_author.trim() ? (record.author_source === 'manual' ? '作者已手动填写' : '使用末位作者') : '作者留空，可重命名');
    authorBadge.classList.toggle('review', !record.corresponding_author.trim());
    authorBadge.hidden = !renameEligible(record);
    flags.append(authorBadge);
    if (record.confidence && !record.completed) flags.append(make('span', 'confidence', confidenceLabels[record.confidence] || record.confidence));
    sourceCell.append(flags);
    if (record.reason) sourceCell.append(make('p', 'reason', record.reason));
    const evidence = [...(Array.isArray(record.evidence) ? record.evidence : []), ...(Array.isArray(record.warnings) ? record.warnings : [])];
    if (evidence.length) {
      const details = make('details', 'evidence');
      details.append(make('summary', '', '查看识别依据'));
      const list = make('ul');
      evidence.forEach((item, index) => list.append(make('li', index >= (record.evidence || []).length ? 'warning-item' : '', String(item))));
      details.append(list); sourceCell.append(details);
    }
    const metadataCell = make('td');
    const fields = make('div', 'metadata-fields');
    const targetCell = make('td');
    const filenameInput = make('textarea', 'filename-input');
    filenameInput.value = record.proposed_filename || '';
    filenameInput.rows = 3;
    filenameInput.dataset.recordId = record.id;
    filenameInput.setAttribute('aria-label', `${record.filename} 的新文件名`);
    filenameInput.spellcheck = false;
    filenameInput.disabled = state.busy || recordLocked(record);
    const hint = make('div', 'target-hint');
    const refresh = () => {
      if (!validRecord(record)) { record.selected = false; checkbox.checked = false; }
      row.classList.toggle('selected', record.selected);
      noteFor(record, hint); updateSelection();
    };
    const authorLabel = '末位作者全名（可留空）';
    [['publication_date', '发表年份', 'YYYY，未知可留空'], ['corresponding_author', authorLabel, '未识别可留空，仍可重命名'], ['journal_abbreviation', '期刊简称', '例如 Nat. Commun.'], ['title', '论文标题', '论文原文标题']].forEach(([key, labelText, placeholder]) => {
      const label = make('label', `metadata-field${key === 'title' ? ' full' : ''}`, labelText);
      const input = make('input', 'metadata-input');
      input.type = 'text'; input.value = record[key] || ''; input.placeholder = placeholder; input.spellcheck = false;
      if (key === 'publication_date') { input.maxLength = 4; input.inputMode = 'numeric'; }
      input.dataset.recordId = record.id;
      input.setAttribute('aria-label', `${record.filename} 的${labelText}`);
      input.disabled = state.busy || recordLocked(record);
      input.addEventListener('input', () => {
        record[key] = input.value;
        if (key === 'publication_date') record.date_source = input.value.trim() ? '手动填写' : '';
        if (key === 'corresponding_author') {
          record.author_source = 'manual';
          label.firstChild.textContent = '末位作者全名（可留空）';
          authorBadge.textContent = input.value.trim() ? '作者已手动填写' : '作者留空，可重命名'; authorBadge.hidden = false;
          authorBadge.classList.toggle('review', !input.value.trim());
        }
        record.proposed_filename = buildFilename(record); filenameInput.value = record.proposed_filename; refresh();
      });
      label.append(input); fields.append(label);
    });
    metadataCell.append(fields);
    filenameInput.addEventListener('input', () => { record.proposed_filename = filenameInput.value; refresh(); });
    targetCell.append(filenameInput, hint);
    noteFor(record, hint);
    row.append(selectCell, sourceCell, metadataCell, targetCell);
    return row;
  }
  function renderRecords() {
    const count = status => state.records.filter(record => record.status === status).length;
    $('stat-total').textContent = state.records.length;
    $('stat-ready').textContent = count('ready');
    $('stat-review').textContent = count('review');
    $('stat-skipped').textContent = count('skipped') + count('error');
    const visible = state.records.filter(record => state.filter === 'all' || (state.filter === 'skipped' ? ['skipped', 'error'].includes(record.status) : record.status === state.filter));
    const fragment = document.createDocumentFragment();
    visible.forEach(record => fragment.append(renderRow(record)));
    $('results-body').replaceChildren(fragment);
    $('no-filter-results').hidden = visible.length > 0;
    $('results-content').hidden = false;
    $('empty-state').hidden = true;
    $('results-description').textContent = state.records.length ? '检查命名信息，勾选需要修改的文件。' : '文件夹内未找到 PDF，请检查路径或开启子文件夹扫描。';
    updateSelection();
  }
  function showProgress(progress = {}) {
    const total = Number(progress.total) || 0;
    const done = Number(progress.done) || 0;
    $('progress-title').textContent = total ? '正在读取与识别论文…' : '正在查找 PDF 文件…';
    $('progress-count').textContent = total ? `${done} / ${total}` : '';
    $('progress-bar').style.width = total ? `${Math.min(100, Math.round(done / total * 100))}%` : '0%';
    $('progress-file').textContent = progress.current || '';
  }
  async function poll(jobId, generation) {
    while (generation === state.generation) {
      const job = await request(`/api/jobs/${encodeURIComponent(jobId)}`);
      if (generation !== state.generation) return null;
      showProgress(job.progress);
      if (job.status === 'error') throw new Error(job.error || '扫描未完成，请查看启动窗口。');
      if (job.status === 'done') return job;
      await new Promise(resolve => window.setTimeout(resolve, 700));
    }
    return null;
  }
  $('folder').addEventListener('input', () => { if (state.records.length || state.folder) invalidateResults(); notice(''); });
  $('pick-folder').addEventListener('click', async () => {
    setBusy(true); notice('');
    try {
      const result = await request('/api/pick-folder', {});
      if (result.path) { invalidateResults(); $('folder').value = result.path; }
    } catch (error) { notice(`${error.message}\n也可以直接在路径框中粘贴文件夹的完整路径。`, 'warning'); }
    finally { setBusy(false); }
  });
  $('ai-enabled').addEventListener('change', () => { ['api-base', 'ai-model', 'api-key'].forEach(id => { $(id).disabled = !$('ai-enabled').checked; }); });
  $('scan-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (state.busy) return;
    const folder = cleanPath($('folder').value);
    if (!folder) { $('folder').focus(); return; }
    const aiEnabled = $('ai-enabled').checked;
    if (aiEnabled && (!$('api-base').value.trim() || !$('ai-model').value.trim())) { notice('使用 AI 识别前，请填写 API 地址和模型名称。', 'warning'); $('ai-settings').open = true; return; }
    invalidateResults();
    $('folder').value = folder;
    const generation = state.generation;
    state.folder = folder;
    notice(''); setBusy(true);
    $('progress-panel').hidden = false; showProgress();
    try {
      const started = await request('/api/scan', {folder, recursive: $('recursive').checked, metadata: $('metadata').checked, ai: {enabled: aiEnabled, base_url: $('api-base').value.trim(), model: $('ai-model').value.trim(), api_key: aiEnabled ? $('api-key').value.trim() : ''}});
      const job = await poll(started.job_id, generation);
      if (!job) return;
      state.records = (job.results || []).map((record, index) => {
        const normalized = {...record, id: String(record.id ?? index), status: statusLabels[record.status] ? record.status : 'review', publication_date: String(record.publication_date || '').slice(0, 4), corresponding_author: String(record.corresponding_author || ''), journal_abbreviation: String(record.journal_abbreviation || ''), title: String(record.title || ''), completed: false};
        normalized.proposed_filename = renameEligible(normalized) ? record.proposed_filename || buildFilename(normalized) : '';
        normalized.selected = normalized.status === 'ready' && validRecord(normalized);
        return normalized;
      });
      renderRecords();
      notice(state.records.length ? `扫描完成，共找到 ${state.records.length} 个 PDF。文件名尚未修改，请核对后重命名。` : '扫描完成，未找到 PDF 文件。', state.records.length ? 'success' : 'warning');
    } catch (error) { notice(error.message, 'error'); }
    finally { if (generation === state.generation) { $('progress-panel').hidden = true; setBusy(false); } }
  });
  document.querySelectorAll('.filter').forEach(button => button.addEventListener('click', () => {
    state.filter = button.dataset.filter;
    document.querySelectorAll('.filter').forEach(item => item.classList.toggle('active', item === button));
    renderRecords();
  }));
  $('select-all').addEventListener('change', () => {
    state.records.forEach(record => { if (record.status === 'ready' && validRecord(record)) record.selected = $('select-all').checked; });
    renderRecords();
  });
  function detailText(items) {
    return items.slice(0, 4).map(item => typeof item === 'string' ? item : `${item.source || item.target || item.filename || ''}${item.reason || item.error ? `：${item.reason || item.error}` : ''}`).join('\n');
  }
  $('apply-button').addEventListener('click', async () => {
    if (state.busy || state.folder !== cleanPath($('folder').value)) return;
    const records = selectedRecords();
    if (!records.length) return;
    setBusy(true); notice('');
    try {
      const result = await request('/api/rename', {folder: state.folder, items: records.map(record => ({source: record.source, filename: record.proposed_filename.trim(), fingerprint: record.fingerprint}))});
      const renamed = result.renamed || [], errors = result.errors || [], skipped = result.skipped || [];
      const changed = new Map(renamed.map(item => [item.source, item.target]));
      state.records.forEach(record => {
        if (changed.has(record.source)) { record.completed = true; record.selected = false; record.proposed_filename = String(changed.get(record.source)).split(/[\\/]/).pop(); }
      });
      if (result.log_id && renamed.length) state.lastOperation = {folder: state.folder, log_id: result.log_id};
      renderRecords();
      let message = `已重命名 ${renamed.length} 个文件。`;
      if (skipped.length) message += ` 跳过 ${skipped.length} 个。`;
      if (errors.length) message += ` 失败 ${errors.length} 个。\n${detailText(errors)}`;
      if (skipped.length) message += `\n${detailText(skipped)}`;
      notice(message, errors.length ? 'error' : skipped.length ? 'warning' : 'success');
    } catch (error) { notice(error.message, 'error'); }
    finally { setBusy(false); }
  });
  $('undo-button').addEventListener('click', async () => {
    if (state.busy || !state.lastOperation) return;
    const operation = state.lastOperation;
    setBusy(true); notice('');
    try {
      const result = await request('/api/undo', {folder: operation.folder, log_id: operation.log_id});
      const restored = result.renamed || result.restored || [], errors = result.errors || [], skipped = result.skipped || [];
      // All displayed fingerprints predate this operation; a fresh scan is required.
      state.records.forEach(record => { record.completed = true; record.completedLabel = '请重新扫描'; record.completedReason = '文件状态已变化，请重新扫描。'; record.selected = false; });
      if (!errors.length && !skipped.length) state.lastOperation = null;
      renderRecords();
      let message = `已撤销 ${restored.length} 个文件的重命名。请重新扫描以获取最新文件状态。`;
      if (errors.length) message += `\n${detailText(errors)}`;
      if (skipped.length) message += `\n${detailText(skipped)}`;
      notice(message, errors.length || skipped.length ? 'warning' : 'success');
    } catch (error) { notice(error.message, 'error'); }
    finally { setBusy(false); }
  });
  $('export-button').addEventListener('click', () => {
    const cell = value => {
      let text = String(value ?? '');
      if (/^[\s\uFEFF]*[=+\-@]|^[\t\r\n]/.test(text)) text = `'${text}`;
      return `"${text.replace(/"/g, '""')}"`;
    };
    const rows = [['原文件路径', '状态', '发表年份', '时间来源', '末位作者全名（可留空）', '作者来源', '期刊简称', '标题', '新文件名', '识别说明'], ...state.records.map(record => [record.source, record.completed ? '已处理' : statusLabels[record.status], record.publication_date || '时间未知', record.date_source || '', record.corresponding_author, authorSourceLabel(record), record.journal_abbreviation, record.title, record.proposed_filename, record.reason || ''])];
    const blob = new Blob(['\ufeff', rows.map(row => row.map(cell).join(',')).join('\r\n')], {type: 'text/csv;charset=utf-8;'});
    const link = document.createElement('a'); const url = URL.createObjectURL(blob);
    link.href = url; link.download = `论文命名预览-${new Date().toISOString().slice(0, 10)}.csv`; link.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  updateSelection();
})();
