'use strict';

const {toggleCommandPalette, commandPaletteKeys} = (() => {
  const dialog = document.createElement('dialog');
  dialog.id = 'command-palette';
  dialog.className = 'command-palette';
  dialog.setAttribute('role', 'dialog');
  dialog.setAttribute('aria-label', 'Command palette');
  dialog.innerHTML = `
    <div class="palette-search">
      ${icon('search', 18)}
      <label class="sr-only" for="palette-query">Search the command palette</label>
      <input id="palette-query" role="combobox" aria-autocomplete="list" aria-expanded="false"
        aria-controls="palette-results" aria-describedby="palette-hint" autocomplete="off" spellcheck="false"
        placeholder="Search files and actions" autofocus>
    </div>
    <p class="palette-hint" id="palette-hint"><kbd>&gt;</kbd> Actions <kbd>@</kbd> Change blocks <kbd>:</kbd> Go to line</p>
    <div id="palette-results" class="palette-results" role="listbox" aria-label="Search results"></div>
    <p class="palette-empty" role="status" hidden></p>`;
  document.body.append(dialog);

  const input = $('palette-query');
  const list = $('palette-results');
  const empty = dialog.querySelector('.palette-empty');
  const fileDialog = $('file-dialog');
  const recentKey = 'change-tree/palette-actions/v1';
  let returnFocus = null;
  let results = [];
  let active = -1;

  function recentActions() {
    try {
      const ids = JSON.parse(localStorage.getItem(recentKey) || '[]');
      return Array.isArray(ids) ? ids.slice(0, 5) : [];
    } catch {
      return [];
    }
  }

  function rememberAction(id) {
    try {
      const ids = [id, ...recentActions().filter((previous) => previous !== id)].slice(0, 5);
      localStorage.setItem(recentKey, JSON.stringify(ids));
    } catch {
      // The palette still works if local storage is unavailable.
    }
  }

  function focusWithoutScroll(element) {
    if (!visible(element) || element.closest('[inert], [hidden]')) return false;
    element.focus({preventScroll: true});
    return document.activeElement === element;
  }

  function restoreFocus() {
    if (focusWithoutScroll(returnFocus)) return;
    if (!help.hidden && focusWithoutScroll(help.querySelector('button'))) return;
    if (!agentPanel.hidden && focusWithoutScroll($('agent-model'))) return;
    if (draft && focusWithoutScroll($('ref-search'))) return;
    if (selected && focusWithoutScroll(fileDialog.querySelector('[data-action="back"]'))) return;
    focusWithoutScroll(pickerButton());
  }

  function toggleCommandPalette() {
    if (dialog.open) {
      closePalette();
      return;
    }
    returnFocus = document.activeElement;
    input.value = '';
    dialog.showModal();
    input.setAttribute('aria-expanded', 'true');
    renderResults();
  }

  function closePalette() {
    dialog.close();
    input.setAttribute('aria-expanded', 'false');
    restoreFocus();
    const origin = returnFocus;
    returnFocus = null;
    return origin;
  }

  function openComparison(task, origin) {
    if (draft) {
      if (task && draft.mode === 'compare') openPicker(true);
      else $('ref-search').focus();
      return;
    }
    pickerReturn = origin;
    openPicker(task);
  }

  function openChangedFile(file, origin) {
    const target = fileTarget(file.path);
    const fallback = !selected && origin?.closest('#workspace, #overview, .header') ? origin : null;
    openFile(file.path, visible(target) ? target : fallback);
  }

  function jumpToBlock(button) {
    if (!button) return;
    button.closest('.diff-block').scrollIntoView({block: 'start'});
    button.focus({preventScroll: true});
  }

  function blockButtons() {
    return [...fileDialog.querySelectorAll('.diff-block .block-tip button')];
  }

  function nextProblem(currentFile, currentBlock) {
    const problems = state.files.flatMap((file, index) => {
      const summary = state.summaries.files?.[file.path];
      const blocks = file.hunks.flatMap((_, index) => summary?.hunks?.[index]?.risk ? [index + 1] : []);
      if (!blocks.length && summary?.risk) blocks.push(0);
      return blocks.map((block) => ({file, index, block}));
    });
    return problems.find(({index, block}) =>
      index > currentFile || (index === currentFile && block > currentBlock)) || problems[0];
  }

  function openProblem(problem, origin) {
    if (selected !== problem.file.path) openChangedFile(problem.file, origin);
    if (problem.block) {
      jumpToBlock($(`blk-${problem.block}`)?.querySelector('.block-tip button'));
    } else {
      const risk = $('file-risk');
      risk.tabIndex = -1;
      risk.focus();
    }
  }

  function overviewTarget() {
    if (draft) closePicker(false);
    if (selected) closeFile();
    const overview = $('overview');
    overview.classList.add('mobile-open');
    const target = overview.querySelector('button, [href]');
    if (!target) overview.tabIndex = -1;
    focus(target || overview);
  }

  function fileFeedback(text, error = false) {
    const feedback = $('save-feedback');
    if (!feedback) return;
    feedback.textContent = text;
    feedback.classList.toggle('error', error);
    feedback.hidden = false;
  }

  async function showUnchanged() {
    const path = selected;
    try {
      await expandAll();
    } catch (error) {
      if (selected === path) fileFeedback(error.message, true);
    }
    if (selected === path && document.activeElement === document.body) {
      focusWithoutScroll(fileDialog.querySelector('.diff-scroll'));
    }
  }

  async function copyPath(path) {
    try {
      await navigator.clipboard.writeText(path);
      if (selected === path) fileFeedback('File path copied.');
    } catch {
      if (selected === path) fileFeedback('Could not copy the file path.', true);
    }
  }

  function commands(store, files) {
    const file = selectedFile();
    const path = selected || returnFocus?.closest('[data-path]')?.dataset.path;
    const current = state.files.findIndex((item) => item.path === path);
    const unviewed = [...files.slice(current + 1), ...files.slice(0, Math.max(current, 0))]
      .find((item) => !item.viewed)?.file;
    const related = returnFocus?.closest('.diff-block')?.querySelector('.block-tip button');
    const currentBlock = related ? Number(related.closest('.diff-block').id.slice(4)) :
      returnFocus?.id === 'file-risk' ? 0 : -1;
    const problem = nextProblem(current, currentBlock);
    const blocks = blockButtons();
    const command = (id, title, run, key) => ({id, title, run, key});
    const buttonCommand = (id, title, button, key) => visible(button) && command(id, title, () => {
      focusWithoutScroll(button);
      button.click();
    }, key);
    const fileCommand = (id, title, key) => buttonCommand(id, title, fileDialog.querySelector(`[data-action="${id}"]`), key);
    const treeCommand = (id, title, key) => {
      const button = canvas?.controls.querySelector(`[data-canvas="${id}"]`);
      return !selected && visible(button) && command(id, title, () => {
        button.click();
        focusWithoutScroll(canvas.viewport);
      }, key);
    };

    return [
      command('compare', 'Choose references…', (origin) => openComparison(false, origin), '/'),
      !state.view && command('task', 'Edit task summary…', (origin) => openComparison(true, origin)),
      !selected && buttonCommand('impact', 'Impact view', document.querySelector('.layout-toggle [data-action="impact"]'), '1'),
      !selected && buttonCommand('directory', 'Directory view', document.querySelector('.layout-toggle [data-action="directory"]'), '2'),
      !selected && canvas && buttonCommand('links', `${links ? 'Hide' : 'Show'} all dependencies`,
        document.querySelector('[data-action="links"]'), 'a'),
      treeCommand('fit', 'Fit tree', 'f / 0'),
      treeCommand('in', 'Zoom in', '+'),
      treeCommand('out', 'Zoom out', '−'),
      treeCommand('reset', 'Reset layout'),
      !state.view && !editing && command('overview', 'Go to overview', overviewTarget, 'o'),
      fileCommand('next', 'Next file', 'n'),
      fileCommand('previous', 'Previous file', 'p'),
      !editing && unviewed && command('unviewed', 'Next unviewed file', (origin) => openChangedFile(unviewed, origin)),
      !editing && problem && command('problem', 'Next possible problem', (origin) => openProblem(problem, origin)),
      blocks.length && command('next-block', 'Next change block', () => jumpToBlock(step(blocks, related, 1)), ']'),
      blocks.length && command('previous-block', 'Previous change block', () => jumpToBlock(step(blocks, related, -1)), '['),
      fileCommand('unified', 'Unified', 'u'),
      fileCommand('split', 'Side by side', 's'),
      fileDialog.querySelector('[data-action="expand"]') && command('unchanged', 'Show all unchanged lines', showUnchanged, 'x'),
      file && command('viewed', isViewed(file, store) ? 'Clear viewed mark' : 'Mark file as viewed', () => toggleViewed(), 'v'),
      Object.keys(store).length && command('clear-viewed', 'Clear all viewed marks of this comparison', () => {
        localStorage.removeItem(viewedKey());
        applyViewed();
      }),
      fileCommand('edit', 'Edit file', 'e'),
      file && command('copy', 'Copy file path', () => copyPath(file.path)),
      fileCommand('back', 'Back to tree', 'b / Esc'),
      !state.view && globalThis.changeChat && command('chat', changeChat.isOpen() ? 'Focus change chat' : 'Open change chat', () => changeChat.open(), 'c / Ctrl J'),
      !state.view && globalThis.changeChat && command('chat-full', changeChat.isFull() ? 'Normal size chat' : 'Full size chat', () => changeChat.toggleFull(), 'Ctrl ⇧ Enter'),
      globalThis.changeChat && (state.chat || []).length && command('chat-clear', 'Clear change chat', () => { changeChat.open(); changeChat.clear(); }),
      !state.view && command('settings', 'Summary agent settings…', () => agentPanel.hidden ? openAgentSettings() : $('agent-model').focus()),
      command('shortcuts', 'Keyboard shortcuts', () => help.hidden ? openHelp() : help.querySelector('button').focus(), '?'),
    ].filter(Boolean);
  }

  function newLineRow(line) {
    const numbers = fileDialog.querySelectorAll(
      '.diff-unified .line-number:nth-child(2), .pair > .diff-row:nth-child(2) .line-number');
    const label = String(line);
    for (const number of numbers) {
      if (number.textContent === label) return number.closest('.diff-row');
    }
    return null;
  }

  async function jumpToLine(file, line) {
    let row = newLineRow(line);
    if (!row) {
      const gap = [...fileDialog.querySelectorAll('[data-action="expand"][data-direction="all"]')].find((button) => {
        const {start, end} = gapRange(file, Number(button.dataset.gap));
        return line >= start && line <= end;
      });
      if (gap) {
        try {
          await expandGap(Number(gap.dataset.gap), 'all');
        } catch (error) {
          if (selected === file.path) {
            fileFeedback(error.message, true);
            focusWithoutScroll(fileDialog.querySelector('.diff-scroll'));
          }
          return;
        }
        if (selected !== file.path) return;
        row = newLineRow(line);
      }
    }
    if (!row) {
      fileFeedback(`Line ${line} is outside the visible file.`, true);
      focusWithoutScroll(fileDialog.querySelector('.diff-scroll'));
      return;
    }
    row.tabIndex = -1;
    row.scrollIntoView({block: 'center', inline: 'nearest'});
    row.focus({preventScroll: true});
  }

  // A forward scan finds a subsequence. A reverse scan tightens it around the same end.
  // Score both so a later run can win without losing a match at the start of a name.
  function fuzzyMatch(text, query) {
    const chars = Array.from(text, (char) => char.toLowerCase());
    const first = [];
    for (let index = 0; index < chars.length && first.length < query.length; index++) {
      if (chars[index] === query[first.length]) first.push(index);
    }
    if (first.length !== query.length) return null;

    const tight = first.slice();
    for (let index = first.at(-1), part = query.length - 1; part >= 0; index--) {
      if (chars[index] === query[part]) tight[part--] = index;
    }
    const score = (positions) => positions.reduce((total, position, index) => {
      const previous = positions[index - 1];
      const wordStart = position === 0 || /[\W_]/u.test(chars[position - 1]);
      return total + 10 + (wordStart ? 7 : 0) +
        (position === previous + 1 ? 9 : 0) - Math.min(index ? position - previous - 1 : position, 12);
    }, 0);
    const firstScore = score(first);
    const tightScore = score(tight);
    return tightScore > firstScore ? {score: tightScore, positions: tight} : {score: firstScore, positions: first};
  }

  function highlighted(text, match) {
    if (!match) return esc(text);
    const chars = [...text];
    let html = '';
    let start = 0;
    for (const position of match.positions) {
      html += esc(chars.slice(start, position).join('')) + `<mark>${esc(chars[position])}</mark>`;
      start = position + 1;
    }
    return html + esc(chars.slice(start).join(''));
  }

  function rank(items, query) {
    if (!query) return items;
    const chars = Array.from(query, (char) => char.toLowerCase());
    const matches = [];
    for (const item of items) {
      const titleMatch = fuzzyMatch(item.title, chars);
      const detailMatch = item.detail && fuzzyMatch(item.detail, chars);
      if (titleMatch || detailMatch) {
        const titleScore = titleMatch ? titleMatch.score + (item.file ? 16 : 0) : -Infinity;
        const score = Math.max(titleScore, detailMatch?.score ?? -Infinity);
        matches.push({...item, titleMatch, detailMatch, score});
      }
    }
    return matches.sort((a, b) => b.score - a.score);
  }

  function option(item, index) {
    const file = item.file;
    return `<div class="palette-option" id="palette-option-${index}" data-result="${index}"
      role="option" aria-selected="false"${file ? ` title="${esc(file.path)}"` : ''}>
      ${file ? glyph(file) : ''}
      <span class="palette-result-text">
        <span class="palette-result-title">${highlighted(item.title, item.titleMatch)}</span>
        ${item.detail ? `<span class="palette-detail">${highlighted(item.detail, item.detailMatch)}</span>` : ''}
      </span>
      ${item.viewed ? '<span class="palette-viewed" role="img" aria-label="Viewed" title="Viewed">✓</span>' : ''}
      ${item.risk ? '<span class="palette-risk" role="img" aria-label="Possible problem" title="Possible problem">⚠</span>' : ''}
      ${file ? `<span class="impact-number ${band(file.score)}" aria-label="Impact ${esc(file.score)}">${esc(file.score)}</span>` : ''}
      ${item.key ? `<kbd>${esc(item.key)}</kbd>` : ''}
    </div>`;
  }

  function renderResults(keepSelection = false) {
    const previous = keepSelection ? results[active] : null;
    const value = input.value.trimStart();
    const mode = value[0];
    const query = ('>@:'.includes(mode) ? value.slice(1) : value).trim();
    const file = selectedFile();
    const summary = state.summaries.files?.[file?.path];
    let message = 'No matching files or actions.';
    if (mode === '@') {
      results = rank((file?.hunks || []).map((hunk, index) => ({
        block: index + 1,
        title: summary?.hunks?.[index]?.title || `Change block ${index + 1}`,
        detail: hunkRange(hunk),
        risk: summary?.hunks?.[index]?.risk,
        run: () => jumpToBlock($(`blk-${index + 1}`)?.querySelector('.block-tip button')),
      })), query);
      message = file ? 'No matching change blocks.' : 'Open a file to find its change blocks.';
    } else if (mode === ':') {
      const available = file?.hunks.length && file.status !== 'D' && !editing;
      const line = /^\d+$/.test(query) ? Number(query) : 0;
      results = available && Number.isSafeInteger(line) && line > 0
        ? [{id: null, title: `Go to line ${line}`, run: () => jumpToLine(file, line)}] : [];
      message = available ? 'Enter a line number in the new version.' : 'Open a file with a new-version diff to go to a line.';
    } else {
      const store = viewedStore() || {};
      const files = state.files.map((file) => ({file, viewed: Object.hasOwn(store, file.path) && isViewed(file, store)}));
      const actions = commands(store, files);
      if (mode === '>') {
        const recent = recentActions();
        const order = (item) => recent.includes(item.id) ? recent.indexOf(item.id) : recent.length;
        results = query ? rank(actions, query) : actions.sort((a, b) => order(a) - order(b));
        message = 'No matching actions.';
      } else {
        const changed = editing ? [] : files.sort((a, b) => Number(a.viewed) - Number(b.viewed)).map(({file, viewed}) => ({
          file, viewed, title: file.cardName, detail: file.path, risk: state.summaries.files?.[file.path]?.risk,
          run: (origin) => openChangedFile(file, origin),
        }));
        results = rank([...changed, ...actions], query);
      }
    }
    list.innerHTML = results.map(option).join('');
    empty.textContent = message;
    empty.hidden = Boolean(results.length);
    list.scrollTop = 0;
    active = -1;
    const same = previous ? results.findIndex((result) => result.file === previous.file &&
      result.id === previous.id && result.block === previous.block && result.title === previous.title) : -1;
    setActive(Math.max(0, same));
  }

  function setActive(index) {
    list.children[active]?.setAttribute('aria-selected', 'false');
    active = results.length ? (index + results.length) % results.length : -1;
    const row = list.children[active];
    if (row) {
      row.setAttribute('aria-selected', 'true');
      for (const element of [input, list]) element.setAttribute('aria-activedescendant', row.id);
      row.scrollIntoView({block: 'nearest'});
    } else {
      for (const element of [input, list]) element.removeAttribute('aria-activedescendant');
    }
  }

  function runResult(index) {
    const result = results[index];
    if (!result || busy) return;
    if (result.id) rememberAction(result.id);
    result.run(closePalette());
  }

  input.addEventListener('input', () => renderResults());
  dialog.addEventListener('cancel', (event) => {
    event.preventDefault();
    closePalette();
  });
  dialog.addEventListener('focusin', (event) => {
    if (event.target !== input) input.focus({preventScroll: true});
  });
  list.addEventListener('mousedown', (event) => {
    if (event.target.closest('[role="option"]')) event.preventDefault();
  });
  list.addEventListener('click', (event) => {
    const row = event.target.closest('[data-result]');
    if (row) runResult(Number(row.dataset.result));
  });

  function commandPaletteKeys(event) {
    const key = event.key;
    if (!dialog.open) {
      if (key === '/' && selected && !busy && !draft && help.hidden && agentPanel.hidden &&
          !event.metaKey && !event.ctrlKey && !event.altKey && !typing(event.target)) {
        event.preventDefault();
        openComparison(false, document.activeElement);
        return true;
      }
      return false;
    }
    event.stopImmediatePropagation();
    if (event.isComposing) return true;
    if (key === 'Escape') closePalette();
    else if (key === 'Enter') runResult(active);
    else if (key === 'ArrowDown' || key === 'ArrowUp') setActive(active + (key === 'ArrowDown' ? 1 : -1));
    else if (key === 'Tab') input.focus();
    else return true;
    event.preventDefault();
    return true;
  }

  const applyBeforePalette = applyState;
  applyState = (...args) => {
    applyBeforePalette(...args);
    if (dialog.open) renderResults(true);
  };
  const receiveBeforePalette = receiveUpdate;
  receiveUpdate = (...args) => {
    receiveBeforePalette(...args);
    if (dialog.open) renderResults(true);
  };

  return {toggleCommandPalette, commandPaletteKeys};
})();
