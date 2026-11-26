// Review aids: "Viewed" marks per file, and highlights of every match of the selected text.
// Loaded after app.js and keys.js. Wraps the page's render functions to add the marks after each render.

// ---- Viewed files ----------------------------------------------------------------------------
// A mark belongs to one comparison (base and compare refs and commits). A new comparison starts clean.
// A mark also stores a hash of the file's diff, so it clears when that file's diff changes.

function viewedKey() {
  const c = state.comparison || {};
  return 'change-tree/viewed/v1/' + JSON.stringify([state.repo, c.base, c.head, c.diffBase, c.headCommit]);
}

function diffHash(file) {
  let hash = 5381;
  const text = file.status + '\n' + file.hunks.map((hunk) =>
    `${hunk.oldStart},${hunk.newStart}\n` + hunk.lines.map((line) => line.kind[0] + line.text).join('\n')).join('\n@@\n');
  for (let i = 0; i < text.length; i++) hash = ((hash << 5) + hash + text.charCodeAt(i)) | 0;
  return (hash >>> 0).toString(36);
}

function viewedStore() {
  try {
    return JSON.parse(localStorage.getItem(viewedKey()) || '{}');
  } catch {
    return {};
  }
}

function isViewed(file, store = viewedStore()) {
  return Boolean(file) && store[file.path] === diffHash(file);
}

function setViewed(file, value) {
  const store = viewedStore();
  if (value) store[file.path] = diffHash(file);
  else delete store[file.path];
  try {
    if (Object.keys(store).length) localStorage.setItem(viewedKey(), JSON.stringify(store));
    else localStorage.removeItem(viewedKey());
  } catch {
    // Storage can be full or blocked. The mark then lasts only until the page reloads.
  }
  applyViewed();
}

function toggleViewed(path = selected) {
  const file = state.files.find((item) => item.path === path);
  if (file) setViewed(file, !isViewed(file));
}

function applyViewed() {
  const store = viewedStore();
  const viewed = new Set(state.files.filter((file) => isViewed(file, store)).map((file) => file.path));
  for (const element of document.querySelectorAll('#workspace [data-path], #overview [data-action="file"][data-path]')) {
    if (element.matches('.dir-row[data-action="folder"]')) continue;
    element.classList.toggle('viewed', viewed.has(element.dataset.path));
  }
  // Progress line under the title of the tree and the directory view.
  const title = document.querySelector('#workspace .map-title');
  if (title && state.files.length) {
    let progress = title.querySelector('.viewed-progress');
    if (!progress) {
      progress = document.createElement('div');
      progress.className = 'viewed-progress';
      title.append(progress);
    }
    progress.textContent = `${viewed.size} of ${state.files.length} viewed`;
  }
  // The Viewed checkbox in the file view.
  const toggle = document.querySelector('#file-dialog [data-review="viewed"]');
  if (toggle) {
    const on = viewed.has(selected);
    toggle.setAttribute('aria-pressed', String(on));
    toggle.querySelector('.viewed-box').textContent = on ? '✓' : '';
  }
}

function addViewedButton() {
  const toolbar = document.querySelector('#file-dialog .diff-toolbar');
  if (!toolbar || toolbar.querySelector('[data-review="viewed"]')) return;
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'pill viewed-toggle';
  button.dataset.review = 'viewed';
  button.setAttribute('aria-pressed', 'false');
  button.setAttribute('aria-keyshortcuts', 'v');
  button.title = 'Mark this file as viewed (v). The mark stays until the compared refs change.';
  button.innerHTML = '<span class="viewed-box" aria-hidden="true"></span>Viewed';
  toolbar.querySelector('.segmented')?.before(button);
}

document.addEventListener('click', (event) => {
  if (event.target.closest('[data-review="viewed"]')) toggleViewed();
});

// ---- Match highlights ---------------------------------------------------------------------
// Select a word (double-click) or any text on one line in the diff. Every match in the open file
// is highlighted with the CSS Custom Highlight API, so the diff markup does not change.

const canHighlight = typeof CSS !== 'undefined' && CSS.highlights && typeof Highlight === 'function';
let matchTerm = '';

function matchCount(count) {
  const toolbar = document.querySelector('#file-dialog .diff-toolbar');
  if (!toolbar) return;
  let badge = toolbar.querySelector('.match-count');
  if (!count) {
    badge?.remove();
    return;
  }
  if (!badge) {
    badge = document.createElement('span');
    badge.className = 'match-count';
    badge.setAttribute('role', 'status');
    toolbar.querySelector('.diff-range')?.after(badge);
  }
  badge.textContent = `${count} ${count === 1 ? 'match' : 'matches'} for “${matchTerm.length > 24 ? matchTerm.slice(0, 23) + '…' : matchTerm}”`;
}

function clearMatches() {
  matchTerm = '';
  if (canHighlight) CSS.highlights.delete('ct-match');
  matchCount(0);
}

function highlightMatches(term) {
  const scroll = document.querySelector('#file-dialog .diff-scroll');
  if (!canHighlight || !scroll || !term) return clearMatches();
  matchTerm = term;
  const escaped = term.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  // A whole word matches whole words only. Other text matches anywhere.
  const pattern = new RegExp(/^\w+$/.test(term) ? `(?<![\\w$])${escaped}(?![\\w$])` : escaped, 'g');
  const ranges = [];
  for (const cell of scroll.querySelectorAll('.line-text')) {
    const node = cell.firstChild;
    if (!node || node.nodeType !== Node.TEXT_NODE) continue;
    pattern.lastIndex = 0;
    for (let match; (match = pattern.exec(node.data)); ) {
      if (!match[0]) {
        pattern.lastIndex++;
        continue;
      }
      const range = new Range();
      range.setStart(node, match.index);
      range.setEnd(node, match.index + match[0].length);
      ranges.push(range);
      if (ranges.length > 5000) break;
    }
  }
  CSS.highlights.set('ct-match', new Highlight(...ranges));
  // In side by side, an unchanged line shows on both sides. Count it once (the left copy).
  const counted = ranges.filter((range) => {
    const row = range.startContainer.parentElement.closest('.diff-row');
    const pair = row?.parentElement;
    return !(pair?.classList.contains('pair') && pair.lastElementChild === row && !row.matches('.addition, .deletion'));
  });
  matchCount(counted.length);
}

function selectedTerm() {
  const selection = document.getSelection();
  if (!selection || selection.isCollapsed || !selection.rangeCount) return '';
  const range = selection.getRangeAt(0);
  const scroll = document.querySelector('#file-dialog .diff-scroll');
  if (!scroll || !scroll.contains(range.commonAncestorContainer)) return '';
  const text = selection.toString();
  const trimmed = text.trim();
  if (!trimmed || trimmed.length > 200 || /[\r\n]/.test(trimmed)) return '';
  return trimmed;
}

let selectionTimer = 0;
document.addEventListener('selectionchange', () => {
  clearTimeout(selectionTimer);
  selectionTimer = setTimeout(() => {
    if (!selected) return;
    const term = selectedTerm();
    // Matches stay after the selection ends. A plain click in the diff or Esc clears them.
    if (term && term !== matchTerm) highlightMatches(term);
  }, 120);
});
document.addEventListener('mousedown', (event) => {
  if (matchTerm && event.detail === 1 && event.target.closest('#file-dialog .diff-scroll') && !event.shiftKey) {
    setTimeout(() => {
      if (!selectedTerm()) clearMatches();
    }, 0);
  }
});

// ---- Hooks into the page's render functions ------------------------------------------------

const renderFileBefore = renderFile;
renderFile = (...args) => {
  renderFileBefore(...args);
  if (!selected) {
    clearMatches();
    return;
  }
  addViewedButton();
  applyViewed();
  if (matchTerm) highlightMatches(matchTerm);
};
const openFileBefore = openFile;
openFile = (...args) => {
  clearMatches();
  openFileBefore(...args);
};
const renderWorkspaceBefore = renderWorkspace;
renderWorkspace = (...args) => {
  renderWorkspaceBefore(...args);
  applyViewed();
};
const renderOverviewBefore = renderOverview;
renderOverview = (...args) => {
  renderOverviewBefore(...args);
  applyViewed();
};

// Keyboard: v marks the open file as viewed. Esc clears matches before it closes the file.
document.addEventListener('keydown', (event) => {
  if (event.metaKey || event.ctrlKey || event.altKey || !selected || draft || busy) return;
  if (event.target.matches?.('input, textarea, select')) return;
  if (event.key === 'v') {
    event.preventDefault();
    event.stopImmediatePropagation();
    toggleViewed();
  } else if (event.key === 'Escape' && matchTerm) {
    event.preventDefault();
    event.stopImmediatePropagation();
    document.getSelection()?.removeAllRanges();
    clearMatches();
  }
}, true);

applyViewed();
