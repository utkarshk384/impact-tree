// Keyboard navigation for the whole page. Loaded after app.js, and uses its state.
// Actions reuse the page's own buttons (button.click()), so mouse and keyboard stay in step.

const SHORTCUTS = [
  ['Anywhere', [
    ['?', 'Show these shortcuts'],
    ['Ctrl K  ⌘ K', 'Open or close the command palette'],
    ['/', 'Open the compare picker'],
    ['1  /  2', 'Impact view / Directory view'],
    ['j  /  k', 'Focus the next / previous file in review order'],
    ['a', 'Show or hide all dependencies'],
    ['o', 'Go to the overview'],
    ['f', 'Fit the tree'],
    ['Esc', 'Close the open panel'],
  ]],
  ['Impact tree', [
    ['Arrow keys', 'Move to the nearest card in that direction'],
    ['Enter', 'Open the file'],
    ['Alt + arrow keys', 'Move the card'],
    ['+  /  −  /  0', 'Zoom in / zoom out / fit (canvas focused)'],
  ]],
  ['Directory', [
    ['↑  /  ↓', 'Previous / next row'],
    ['→  /  ←', 'Open / close a folder, or go to its parent'],
    ['Home  /  End', 'First / last row'],
  ]],
  ['File view', [
    ['n  /  p', 'Next / previous file'],
    [']  /  [', 'Next / previous change block, with its AI summary'],
    ['u  /  s', 'Unified / side by side'],
    ['x', 'Show all unchanged lines'],
    ['v', 'Mark the file as viewed, or clear the mark'],
    ['Select text', 'Highlight every match in the file. Esc clears.'],
    ['e', 'Edit the file (working tree only)'],
    ['Ctrl S', 'Save the edit'],
    ['b  or  Esc', 'Back to the tree'],
  ]],
  ['Command palette', [
    ['↑  /  ↓', 'Move through the results'],
    ['Enter', 'Run the selected result'],
    ['>  @  :', 'Search actions, find change blocks, or go to a line'],
    ['Esc', 'Close the palette and restore focus'],
  ]],
  ['Compare picker', [
    ['↓  /  ↑', 'Move through the results'],
    ['Enter', 'Pick the result. A Base pick moves on to Compare.'],
    ['←  /  →', 'Switch tabs, or switch Base and Compare'],
    ['Ctrl Enter', 'Compare'],
  ]],
];

const help = document.createElement('section');
help.className = 'shortcuts';
help.id = 'shortcuts';
help.hidden = true;
help.setAttribute('role', 'dialog');
help.setAttribute('aria-modal', 'true');
help.setAttribute('aria-labelledby', 'shortcuts-title');
help.innerHTML = `<div class="shortcuts-head"><h2 id="shortcuts-title">Keyboard shortcuts</h2>
  <button type="button" class="pill surface" data-key-action="close-help">Close</button></div>
  <div class="shortcuts-grid">${SHORTCUTS.map(([group, rows]) => `<div><h3>${group}</h3><dl>${rows.map(([keys, text]) =>
    `<div><dt>${keys.split('  ').map((key) => `<kbd>${esc(key.trim())}</kbd>`).join(' ')}</dt><dd>${esc(text)}</dd></div>`).join('')}</dl></div>`).join('')}</div>`;
document.body.append(help);

const helpButton = document.createElement('button');
helpButton.type = 'button';
helpButton.className = 'pill keys-button';
helpButton.dataset.keyAction = 'open-help';
helpButton.setAttribute('aria-label', 'Keyboard shortcuts');
helpButton.setAttribute('aria-keyshortcuts', '?');
helpButton.innerHTML = '<kbd>?</kbd>';
document.querySelector('.overview-button')?.before(helpButton);

let helpReturn = null;
let pickerReturn = null;

// After Ctrl K / Cmd K, closing the picker puts focus back where it was (for example in the file view).
const closePickerBefore = closePicker;
closePicker = (restoreFocus = true) => {
  const target = pickerReturn;
  pickerReturn = null;
  const back = restoreFocus && target?.isConnected && !target.closest('[hidden], [inert]');
  closePickerBefore(restoreFocus && !back);
  if (back) target.focus({preventScroll: true});
};
function openHelp() {
  helpReturn = document.activeElement;
  help.hidden = false;
  help.querySelector('button').focus();
}
function closeHelp() {
  help.hidden = true;
  if (helpReturn?.isConnected) helpReturn.focus();
}
document.addEventListener('click', (event) => {
  const action = event.target.closest('[data-key-action]')?.dataset.keyAction;
  if (action === 'open-help') openHelp();
  if (action === 'close-help') closeHelp();
});

const typing = (element) => element?.matches?.('input, textarea, select, [contenteditable="true"]');
const visible = (element) => element && !element.disabled && element.offsetParent !== null;
const focus = (element) => {
  if (!element) return false;
  element.focus({preventScroll: false});
  element.scrollIntoView?.({block: 'nearest', inline: 'nearest'});
  return true;
};
const clickIn = (root, selector) => {
  const button = root.querySelector(selector);
  if (!visible(button)) return false;
  button.click();
  return true;
};

// Keep Tab inside an open dialog.
function trapTab(event, dialog) {
  const items = [...dialog.querySelectorAll('button, [href], input, textarea, select, [tabindex]:not([tabindex="-1"])')]
    .filter((item) => visible(item) && !item.closest('[hidden]'));
  if (!items.length) return;
  const first = items[0], last = items[items.length - 1];
  if (!dialog.contains(document.activeElement)) {
    event.preventDefault();
    focus(event.shiftKey ? last : first);
  } else if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    focus(last);
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    focus(first);
  }
}

// Move between items in a list with arrow keys (wraps at the ends).
function step(items, current, delta) {
  if (!items.length) return null;
  const index = items.indexOf(current);
  if (index < 0) return items[delta > 0 ? 0 : items.length - 1];
  return items[(index + delta + items.length) % items.length];
}

// Nearest tree card in a direction, by card centres.
function nearestCard(card, [dx, dy]) {
  const from = card.getBoundingClientRect();
  const cx = from.left + from.width / 2, cy = from.top + from.height / 2;
  let best = null, bestScore = Infinity;
  for (const other of document.querySelectorAll('#workspace .tree-card')) {
    if (other === card) continue;
    const rect = other.getBoundingClientRect();
    const ox = rect.left + rect.width / 2 - cx, oy = rect.top + rect.height / 2 - cy;
    const along = ox * dx + oy * dy;
    if (along <= 1) continue;
    const across = Math.abs(ox * dy - oy * dx);
    const score = along + across * 2.5;
    if (score < bestScore) {
      bestScore = score;
      best = other;
    }
  }
  return best;
}

const ARROW = {ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1]};

function fileTarget(path) {
  const escaped = CSS.escape(path);
  return document.querySelector(`#workspace [data-action="file"][data-path="${escaped}"]`) ||
    document.querySelector(`#overview [data-action="file"][data-path="${escaped}"]`);
}

function reviewStep(delta) {
  const files = state.files;
  if (!files.length) return;
  const currentPath = document.activeElement?.closest?.('[data-path]')?.dataset.path;
  let index = files.findIndex((file) => file.path === currentPath);
  index = index < 0 ? (delta > 0 ? 0 : files.length - 1) : (index + delta + files.length) % files.length;
  focus(fileTarget(files[index].path));
}

function pickerKeys(event) {
  const picker = $('picker');
  const target = event.target;
  if (event.key === 'Tab') return trapTab(event, picker);
  if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
    event.preventDefault();
    clickIn(picker, '[data-action="compare"]');
    return;
  }
  const results = [...$('ref-results').querySelectorAll('[data-action="ref"]')];
  if (target.id === 'ref-search') {
    if (event.key === 'ArrowDown' && results.length) {
      event.preventDefault();
      focus(results[0]);
    } else if (event.key === 'Enter' && results.length) {
      event.preventDefault();
      pickResult(results.find((item) => item.getAttribute('aria-pressed') === 'true' && !target.value) || results[0]);
    }
    return;
  }
  if (target.dataset.action === 'ref') {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      const index = results.indexOf(target);
      if (event.key === 'ArrowUp' && index === 0) focus($('ref-search'));
      else focus(step(results, target, event.key === 'ArrowDown' ? 1 : -1));
    } else if (event.key === 'Home' || event.key === 'End') {
      event.preventDefault();
      focus(results[event.key === 'Home' ? 0 : results.length - 1]);
    } else if (event.key === 'Enter') {
      event.preventDefault();
      pickResult(target);
    }
    return;
  }
  if ((event.key === 'ArrowLeft' || event.key === 'ArrowRight') && target.closest('#picker-tabs, .picker-sides')) {
    event.preventDefault();
    const group = [...target.closest('#picker-tabs, .picker-sides').querySelectorAll('button')];
    const next = step(group, target, event.key === 'ArrowRight' ? 1 : -1);
    next.click();
    focus(next);
  }
}

// Pick a result. After a Base pick, move to the Compare side so the next search sets it.
function pickResult(button) {
  const wasBase = draft?.side === 'base' && button.dataset.kind !== 'prs';
  button.click();
  if (wasBase && draft) {
    draft.side = 'head';
    $('ref-search').value = '';
    renderPicker();
    focus($('ref-search'));
    $('picker-status').textContent = 'Base set. Now choose Compare, or press Ctrl Enter to compare.';
  } else {
    focus($('ref-search'));
  }
}

function directoryKeys(event, row) {
  const rows = [...document.querySelectorAll('#workspace .dir-row')];
  const isFolder = row.dataset.action === 'folder';
  const open = row.getAttribute('aria-expanded') === 'true';
  const path = row.dataset.path;
  const refocus = () => focus(document.querySelector(`#workspace .dir-row[data-path="${CSS.escape(path)}"]`));
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') focus(step(rows, row, event.key === 'ArrowDown' ? 1 : -1));
  else if (event.key === 'Home' || event.key === 'End') focus(rows[event.key === 'Home' ? 0 : rows.length - 1]);
  else if (event.key === 'ArrowRight' && isFolder && !open) { row.click(); refocus(); }
  else if (event.key === 'ArrowRight' && isFolder) focus(step(rows, row, 1));
  else if (event.key === 'ArrowLeft' && isFolder && open && path !== '/') { row.click(); refocus(); }
  else if (event.key === 'ArrowLeft') {
    const parent = isFolder ? path.replace(/[^/]+\/$/, '') : '/' + path.split('/').slice(0, -1).map((part) => part + '/').join('');
    if (parent === path) return false;
    focus(document.querySelector(`#workspace .dir-row[data-action="folder"][data-path="${CSS.escape(parent)}"]`));
  } else return false;
  event.preventDefault();
  return true;
}

function fileKeys(event) {
  const dialog = $('file-dialog');
  const key = event.key;
  if (key === 'Tab') return trapTab(event, dialog);
  if ((event.metaKey || event.ctrlKey) && key.toLowerCase() === 's') {
    event.preventDefault();
    clickIn(dialog, '[data-action="save"]');
    return;
  }
  if (typing(event.target) || event.metaKey || event.ctrlKey || event.altKey) return;
  const blocks = [...dialog.querySelectorAll('.diff-block .block-tip button')];
  const current = event.target.closest?.('.diff-block')?.querySelector('.block-tip button');
  let handled = true;
  if (key === 'n') handled = clickIn(dialog, '[data-action="next"]');
  else if (key === 'p') handled = clickIn(dialog, '[data-action="previous"]');
  else if (key === 'u') handled = clickIn(dialog, '[data-action="unified"]');
  else if (key === 's') handled = clickIn(dialog, '[data-action="split"]');
  else if (key === 'e') handled = clickIn(dialog, '[data-action="edit"]');
  else if (key === 'b') handled = clickIn(dialog, '[data-action="back"]');
  else if (key === ']' || key === '[') {
    const next = step(blocks, current, key === ']' ? 1 : -1);
    handled = !!next;
    if (next) {
      next.closest('.diff-block').scrollIntoView({block: 'start'});
      next.focus({preventScroll: true});
    }
  } else if (key === 'x') expandAll();
  else handled = false;
  if (handled) event.preventDefault();
  if (handled && 'npusb'.includes(key)) requestAnimationFrame(() => focusFileStart(key));
}

async function expandAll() {
  for (let guard = 0; guard < 200; guard++) {
    const button = $('file-dialog').querySelector('[data-action="expand"][data-direction="all"]');
    if (!button) return;
    const index = Number(button.dataset.gap);
    await expandGap(index, 'all');
  }
}

function focusFileStart(key) {
  const dialog = $('file-dialog');
  if (dialog.hidden) return;
  const pressed = key === 'u' ? '[data-action="unified"]' : key === 's' ? '[data-action="split"]' : null;
  focus((pressed && dialog.querySelector(pressed)) || dialog.querySelector('[data-action="back"]'));
}

function globalKeys(event) {
  const key = event.key;
  if (event.metaKey || event.ctrlKey || event.altKey || typing(event.target)) return;
  const row = event.target.closest?.('#workspace .dir-row');
  if (row && directoryKeys(event, row)) return;
  const card = event.target.closest?.('#workspace .tree-card');
  if (card && ARROW[key]) {
    event.preventDefault();
    focus(nearestCard(card, ARROW[key]));
    return;
  }
  let handled = true;
  if (key === '?') openHelp();
  else if (key === '/') openPicker();
  else if (key === '1') clickIn(document, '.layout-toggle [data-action="impact"]');
  else if (key === '2') clickIn(document, '.layout-toggle [data-action="directory"]');
  else if (key === 'a') clickIn(document, '[data-action="links"]');
  else if (key === 'f') canvas?.fit?.();
  else if (key === 'j' || key === 'k') reviewStep(key === 'j' ? 1 : -1);
  else if (key === 'o') {
    const overview = $('overview');
    overview.classList.add('mobile-open');
    focus(overview.querySelector('button, [href]'));
  } else handled = false;
  if (handled) event.preventDefault();
}

document.addEventListener('keydown', (event) => {
  if (event.defaultPrevented) return;
  // Handle the palette before the other panels, including when an input has focus.
  if ((event.metaKey || event.ctrlKey) && !event.altKey && !event.shiftKey && event.key.toLowerCase() === 'k') {
    event.preventDefault();
    event.stopImmediatePropagation();
    if (!event.repeat) toggleCommandPalette();
    return;
  }
  if (commandPaletteKeys(event) || busy) return;
  if (!help.hidden) {
    if (event.key === 'Escape' || event.key === '?') {
      event.preventDefault();
      event.stopImmediatePropagation();
      closeHelp();
    } else if (event.key === 'Tab') trapTab(event, help);
    return;
  }
  if (event.key === 'Escape') return; // app.js closes the picker or the file view.
  if (draft) return pickerKeys(event);
  if (selected) return fileKeys(event);
  globalKeys(event);
}, true);
