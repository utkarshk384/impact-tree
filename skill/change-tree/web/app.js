'use strict';

const $ = (id) => document.getElementById(id);
const token = document.querySelector('meta[name="change-tree-token"]').content;
const icons = {
  info: '<path d="M12 11v6"></path><circle cx="12" cy="7" r=".6"></circle>',
  tree: '<path d="M3 12h6M9 12c4 0 4-6 9-6M9 12c4 0 4 6 9 6"></path>',
  folder: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"></path>',
  arrow: '<path d="M4 12h16M14 6l6 6-6 6"></path>',
  chevron: '<path d="M6 9l6 6 6-6"></path>',
  right: '<path d="M9 6l6 6-6 6"></path>',
  links: '<path stroke-dasharray="3 3" d="M3 18C9 18 15 6 21 6"></path>',
  back: '<path d="M20 12H4M10 6l-6 6 6 6"></path>',
  edit: '<path d="M4 20h4L19 9l-4-4L4 16zM13.5 6.5l4 4"></path>',
  lock: '<rect x="5" y="11" width="14" height="9" rx="2"></rect><path d="M8 11V8a4 4 0 0 1 8 0v3"></path>',
  unified: '<rect x="4" y="4" width="16" height="16" rx="2"></rect><path d="M8 9h8M8 13h8M8 17h5"></path>',
  split: '<rect x="3" y="4" width="18" height="16" rx="2"></rect><path d="M12 4v16"></path>',
  gap: '<path d="M8 9l4-4 4 4M8 15l4 4 4-4"></path>',
  warning: '<path d="M12 4 2.5 20h19z"></path><path d="M12 10v4.5"></path><circle cx="12" cy="17.2" r="0.6"></circle>',
  expandUp: '<path d="M12 19V7M7 12l5-5 5 5"></path><path d="M5 4h14"></path>',
  expandDown: '<path d="M12 5v12M7 12l5 5 5-5"></path><path d="M5 20h14"></path>',
  pull: '<circle cx="6" cy="6" r="2.5"></circle><circle cx="6" cy="18" r="2.5"></circle><circle cx="18" cy="18" r="2.5"></circle><path d="M6 8.5v7M18 15.5V10a3 3 0 0 0-3-3h-4"></path>',
  search: '<circle cx="11" cy="11" r="7"></circle><path d="M20 20l-4-4"></path>',
};
const tabs = {commits: 'Commits', branches: 'Branches', origins: 'Origins', worktrees: 'Worktrees', prs: 'PRs'};
const statusWords = {A: 'Added', M: 'Modified', D: 'Deleted'};
const replacements = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'};

let state = {files: [], refs: {}, comparison: {}, layout: {}, loading: true, summaries: {}};
let layout = 'impact';
let links = true;
let selected = null;
let diffView = 'unified';
let collapsed = new Set();
let editing = false;
let draft = null;
let busy = false;
let queuedUpdate = null;
let saveMessage = '';
let saveFailed = false;
let lastFileButton = null;
let canvas = null;
let canvasView = null;

const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (character) => replacements[character]);
// AI text: escape it, then show `names` as inline code.
const aiText = (value) => esc(value).replace(/`([^`\n]+)`/g, '<code>$1</code>');
const icon = (name, size = 14) =>
  `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${icons[name]}</svg>`;
const glyph = (file) => `<span class="chip chip-letter status-${file.status}" aria-label="${statusWords[file.status]}">${file.status}</span>`;
const counts = (file) => `<span class="add">+${file.added}</span> <span class="del">−${file.removed}</span>`;
const blockName = (count) => `${count} ${count === 1 ? 'block' : 'blocks'}`;
const band = (score) => score >= 66 ? 'high' : score >= 33 ? 'medium' : 'low';
const bar = (score) => `<span class="impact-bar" aria-hidden="true"><span style="width:${score}%"></span></span><span class="impact-number" aria-label="Impact ${score}">${score}</span>`;
const selectedFile = () => state.files.find((file) => file.path === selected);

const USED_BY_LIMIT = 60;
function usedByTree(file) {
  const paths = file.usedBy || [];
  if (!paths.length) return '<p class="used-by-empty">No code imports it</p>';
  const depth = file.usedByDepth || {};
  const changed = new Set(state.files.map((item) => item.path));
  const shown = paths.slice(0, USED_BY_LIMIT);
  const root = {dirs: new Map(), files: []};
  for (const path of shown) {
    const parts = path.split('/');
    let node = root;
    for (const part of parts.slice(0, -1)) {
      if (!node.dirs.has(part)) node.dirs.set(part, {dirs: new Map(), files: []});
      node = node.dirs.get(part);
    }
    node.files.push(path);
  }
  const leaf = (path) => {
    const name = path.split('/').pop();
    const distance = depth[path];
    const tag = distance === 1 ? 'imports it' : distance ? `via ${distance - 1} ${distance === 2 ? 'file' : 'files'}` : '';
    const isChanged = changed.has(path);
    return `<li class="ub-file${isChanged ? ' changed' : ''}" title="${esc(path)}${isChanged ? ' · changed in this comparison' : ''}">
      <span class="ub-name">${esc(name)}</span>${isChanged ? '<span class="ub-changed">changed</span>' : ''}${tag ? `<span class="ub-tag${distance === 1 ? ' direct' : ''}">${tag}</span>` : ''}</li>`;
  };
  const branch = (node) => {
    const dirs = [...node.dirs.entries()].sort(([a], [b]) => a.localeCompare(b)).map(([name, child]) => {
      let label = name;
      while (child.dirs.size === 1 && !child.files.length) {
        const [next, grand] = [...child.dirs.entries()][0];
        label += '/' + next;
        child = grand;
      }
      return `<li><span class="ub-dir">${esc(label)}/</span><ul>${branch(child)}</ul></li>`;
    });
    const files = node.files.slice().sort((a, b) => (depth[a] - depth[b]) || a.localeCompare(b)).map(leaf);
    return dirs.join('') + files.join('');
  };
  const direct = paths.filter((path) => depth[path] === 1).length;
  const more = paths.length - shown.length;
  return `<p class="used-by-count">${paths.length} ${paths.length === 1 ? 'file' : 'files'} · ${direct} import it directly</p>
    <ul class="ub-tree" tabindex="0" aria-label="Files that use ${esc(file.name)}. Scroll to see all."><li><span class="ub-dir">${esc(state.repo || 'repository')}/</span><ul>${branch(root)}</ul></li></ul>
    ${more > 0 ? `<p class="used-by-count">${more} more not shown</p>` : ''}`;
}
const summaryFallback = () => state.summaryError || 'Summarizing…';

for (const holder of document.querySelectorAll('[data-icon]')) {
  const name = holder.dataset.icon;
  holder.innerHTML = icon(name, ['arrow', 'links', 'pull', 'search'].includes(name) ? 16 : 14);
}

function totals(files) {
  return files.reduce((sum, file) => ({
    added: sum.added + file.added,
    removed: sum.removed + file.removed,
    blocks: sum.blocks + file.hunks.length,
    score: Math.max(sum.score, file.score),
  }), {added: 0, removed: 0, blocks: 0, score: 0});
}

function refLabel(value, side) {
  const comparison = state.comparison;
  if (value === comparison[side]) return comparison[side + 'Label'];
  if (value === 'WORKTREE') return 'Working tree';
  if (side === 'head') {
    const pull = (state.refs.prs || []).find((item) => item.fetchHead && item.head === value);
    if (pull) return pull.headLabel;
  }
  for (const key of ['commits', 'worktrees', 'branches', 'origins']) {
    const match = (state.refs[key] || []).find((item) => item.value === value);
    if (match) return match.label;
  }
  return value || (side === 'base' ? 'HEAD' : 'Working tree');
}

function renderHeader() {
  $('repo-name').textContent = state.repo ? '/ ' + state.repo : '';
  $('base-label').textContent = refLabel(draft?.base ?? state.comparison.base, 'base') || 'HEAD';
  $('head-label').textContent = refLabel(draft?.head ?? state.comparison.head, 'head') || 'Working tree';
  const reachable = state.files.reduce((top, file) => (!top || file.impact.reach > top.impact.reach) ? file : top, null);
  const manifest = state.files.find((file) => file.changeKind === 'manifest');
  const standalone = state.files.find((file) => file.standalone);
  const example = (file) => '<code>' + esc(file.name) + '</code>';
  $('impact-help').innerHTML = `
    <b>How impact is calculated</b>
    <span class="muted">Each changed file gets a score from 0 to 100 that estimates how far its change can spread. It combines three parts.</span>
    <span class="weights">
      <b>Reach · 60%</b><span>How many files import it, directly or through other files, weighted by distance and scaled to this repository.
        ${reachable?.usedBy.length ? `${example(reachable)} reaches ${reachable.usedBy.length} files and scores <b>${reachable.score}</b>.` : ''}</span>
      <b>Change type · 25%</b><span>Changes to exports, props, dependencies or files that others import count more than edits inside a file.
        ${manifest ? `${example(manifest)} ${esc(manifest.why.split(' · ')[0])} and scores <b>${manifest.score}</b>.` : ''}</span>
      <b>Size · 15%</b><span>Lines added and removed, on a log scale so big rewrites do not dominate.</span>
    </span>
    <span class="tip-footer">The tree puts changed files that other changed files depend on at the trunk, and each file further out than what it depends on.
      Files nothing imports${standalone ? ', like ' + example(standalone) + ' (' + standalone.score + '),' : ''} can sit apart as standalone.
      Low scores are below 33, medium start at 33 and high start at 66.</span>`;
}

function emptyMessage() {
  let title = 'No changes';
  let description = 'Choose another comparison to see changed files.';
  if (state.loading) {
    title = 'Analyzing comparison…';
    description = 'Reading the Git diff and dependency graph.';
  } else if (state.error) {
    title = state.errorCode === 3 ? 'Graph missing' : 'Analysis error';
    description = state.error;
  }
  return `<div class="empty" role="status"><b>${title}</b>${esc(description)}</div>`;
}

const roots = [
  'M44 492 C40 520 22 536 8 560', 'M70 492 C72 530 92 552 104 590', 'M96 492 C110 512 132 520 150 540',
  'M30 492 C20 504 10 508 0 512', 'M58 492 C54 540 46 580 50 640', 'M86 560 C96 572 110 576 124 576',
  'M24 540 C14 552 8 566 6 590',
];
const pathElement = (line) => `<path d="${line}"></path>`;

function canvasKey() {
  const {target, base, head} = state.comparison;
  return 'change-tree/layout/v1/' + JSON.stringify([state.repo, target, base, head]);
}

function drawTree() {
  const map = state.layout;
  const height = map.height - TREE_TOP;
  let dividers = '';
  let labels = '';
  for (let col = 0; col < map.columns; col++) {
    const last = col === map.columns - 1;
    const x = map.columnX[col];
    if (col > 0) dividers += pathElement(`M${x - 60} 146 V${map.branchBottom}`);
    const label = col === 0 ? (last ? 'TRUNK' : 'TRUNK · most depended on') :
      last ? 'LEAVES · least depended on' : col === 1 ? 'BRANCHES' : '';
    if (label) {
      labels += `<div class="tree-label ${col === 0 ? 'trunk' : last ? 'leaves' : ''}" style="left:${x}px">${label}</div>`;
    }
  }
  if (map.standaloneTop !== null) {
    labels += `<div class="tree-label standalone" style="left:${map.trunkX}px;top:${map.standaloneTop - TREE_TOP}px">Standalone · nothing imports these</div>`;
  }
  const drawing = `<svg class="tree-drawing" width="${map.width}" height="${height}" viewBox="0 ${TREE_TOP} ${map.width} ${height}" aria-hidden="true">
    <g fill="none" stroke="#2e2e2e" stroke-width="2" stroke-dasharray="2 6">${dividers}</g>
    <g class="extra-edges" fill="none" stroke="#6e6e6e" stroke-width="2" stroke-linecap="round" stroke-dasharray="4 6" ${links ? '' : 'hidden'}></g>
    <g fill="none" stroke="#6e6e6e" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <g transform="translate(0 ${map.root.y - 408})">${roots.map(pathElement).join('')}</g>
      <g class="main-edges"></g>
    </g>
  </svg>`;
  const cards = state.files.map((file) => {
    const pos = map.positions[file.path];
    const name = file.cardName;
    return `<button type="button" class="tree-card ${file.band}" style="transform:translate(${pos.x}px,${pos.y - TREE_TOP}px)"
      data-action="file" data-path="${esc(file.path)}" aria-pressed="${selected === file.path}"
      title="${esc(file.path)}" aria-label="${esc(file.path)}">
      <span class="card-name">${glyph(file)}<span class="card-filename"><bdi>${esc(name)}</bdi></span></span>
      <span class="card-stats"><span>${counts(file)}</span><span>${blockName(file.hunks.length)}</span></span>
      <span class="card-impact">${bar(file.score)}</span>
    </button>`;
  }).join('');
  const total = totals(state.files);
  return `<div class="tree-viewport" tabindex="0" role="region" aria-label="Impact tree canvas" aria-describedby="canvas-instructions">
    <div class="tree-world">
      ${drawing}
      ${labels}
      <div class="root" style="left:${map.root.x}px;top:${map.root.y - TREE_TOP}px"><b>Root</b><span class="root-counts">+${total.added} −${total.removed}</span></div>
      ${cards}
      <svg class="tree-drawing tree-ports" width="${map.width}" height="${height}" viewBox="0 ${TREE_TOP} ${map.width} ${height}" aria-hidden="true"></svg>
    </div>
    <div class="map-title"><div>Impact tree · trunk on the left, leaves on the right</div><h1>${changedCount()}</h1></div>
    <span class="sr-only" id="canvas-instructions">Use arrow keys to pan, plus and minus to zoom, and 0 to fit the tree. Focus a card and use Alt and the arrow keys to move it.</span>
    <div class="tree-chrome">
      <div class="legend" aria-label="Legend">
        <span><b class="add">A</b> added</span><span><b style="color:#7aa7ff">M</b> modified</span><span><b class="del">D</b> deleted</span>
        <span class="bands">Impact <span class="swatch low"></span>low <span class="swatch medium"></span>medium <span class="swatch high"></span>high</span>
      </div>
      <div class="tree-controls" role="group" aria-label="Canvas controls">
        <button type="button" data-canvas="out" aria-label="Zoom out">−</button>
        <output aria-label="Zoom level" aria-live="off">100%</output>
        <button type="button" data-canvas="in" aria-label="Zoom in">+</button>
        <button type="button" data-canvas="fit" aria-label="Fit tree">Fit</button>
        <button type="button" data-canvas="reset" aria-label="Reset layout">Reset layout</button>
      </div>
    </div>
  </div>`;
}

function changedCount() {
  const count = state.files.length;
  return `${count} ${count === 1 ? 'file' : 'files'} changed`;
}

function directoryRows() {
  const root = {name: state.repo, path: '/', folders: new Map(), files: []};
  for (const file of state.files) {
    let directory = root;
    for (const part of file.path.split('/').slice(0, -1)) {
      if (!directory.folders.has(part)) {
        directory.folders.set(part, {name: part, path: directory.path + part + '/', folders: new Map(), files: []});
      }
      directory = directory.folders.get(part);
    }
    directory.files.push(file);
  }

  const descendants = (directory) => [...directory.files, ...[...directory.folders.values()].flatMap(descendants)];
  const byName = (left, right) => left.name.localeCompare(right.name);
  const guide = (depth, parents, last) => {
    const x = (index) => 20 + index * 20;
    const line = (left, top, width, height) =>
      `<span aria-hidden="true" class="dir-guide" style="left:${left}px;top:${top}px;width:${width}px;height:${height}px"></span>`;
    return parents.map((draw, index) => draw ? line(x(index), 0, 2, 44) : '').join('') +
      (depth ? line(x(depth - 1), 0, 2, last ? 22 : 44) + line(x(depth - 1), 21, 12, 2) : '');
  };

  function visit(item, depth, parents, last) {
    const indented = `style="padding-left:${depth * 20}px"`;
    const guides = guide(depth, parents, last);
    if (!item.folders) {
      return `<button type="button" class="dir-row dir-cols" data-action="file" data-path="${esc(item.path)}">
        ${guides}<span class="dir-name" ${indented}>${glyph(item)}<span class="ellipsis">${esc(item.name)}</span></span>
        <span class="compact">${counts(item)}</span><span class="compact muted">${item.hunks.length}</span>
        <span class="dir-impact ${item.band}">${bar(item.score)}</span>
      </button>`;
    }
    const list = descendants(item);
    const sum = totals(list);
    const open = !collapsed.has(item.path);
    const children = [...[...item.folders.values()].sort(byName), ...item.files.slice().sort(byName)];
    return `<button type="button" class="dir-row dir-cols" data-action="folder" data-path="${esc(item.path)}" aria-expanded="${open}">
      ${guides}<span class="dir-name" ${indented}><span class="muted">${icon(open ? 'chevron' : 'right')}</span>
      <span class="dir-glyph">${icon('folder', 16)}</span><span class="mono ellipsis">${esc(item.name)}${depth ? '/' : ''}</span>
      <span class="subtle">${list.length} ${list.length === 1 ? 'file' : 'files'}</span></span>
      <span class="compact">${counts(sum)}</span><span class="compact muted">${sum.blocks}</span>
      <span class="dir-highest ${band(sum.score)}">highest <b>${sum.score}</b></span>
    </button>` + (open ? children.map((child, i) => visit(child, depth + 1, depth ? [...parents, !last] : [], i === children.length - 1)).join('') : '');
  }
  return visit(root, 0, [], true);
}

function renderWorkspace(keepScroll = true) {
  const workspace = $('workspace');
  const previous = workspace.querySelector('.directory-list');
  if (canvas) {
    const view = canvas.saveView();
    if (view.key === canvasKey()) canvasView = view;
    canvas.destroy();
    canvas = null;
  }
  if (canvasView && canvasView.key !== canvasKey()) canvasView = null;
  if (!state.files.length) {
    workspace.innerHTML = `<div class="map-title"><div>${layout === 'impact' ? 'Impact tree · trunk on the left, leaves on the right' : 'Directory view · changed files grouped by folder'}</div><h1>${changedCount()}</h1></div>${emptyMessage()}`;
  } else if (layout === 'impact') {
    workspace.innerHTML = drawTree();
    canvas = new TreeCanvas(workspace.querySelector('.tree-viewport'), state.layout, state.files, canvasKey(), canvasView);
  } else {
    workspace.innerHTML = `<div class="directory">
      <div class="directory-title"><div>Directory view · changed files grouped by folder</div><h1>${changedCount()}</h1></div>
      <div class="directory-list panel"><div class="dir-cols dir-heading"><span>Name</span><span>Lines</span><span>Blocks</span><span>Impact</span></div>
      ${directoryRows()}</div>
    </div>`;
    if (keepScroll && previous) workspace.querySelector('.directory-list').scrollTop = previous.scrollTop;
  }
  workspace.inert = Boolean(selected);
}

// Open or closed state of the overview's AI summary and task cards. It is kept across reloads.
const FOLD_KEY = 'change-tree/overview-folds/v1';
function foldOpen(name) {
  try {
    return JSON.parse(localStorage.getItem(FOLD_KEY) || '{}')[name] !== false;
  } catch {
    return true;
  }
}
document.addEventListener('toggle', (event) => {
  const fold = event.target.closest?.('details[data-fold]');
  if (!fold) return;
  try {
    const folds = JSON.parse(localStorage.getItem(FOLD_KEY) || '{}');
    folds[fold.dataset.fold] = fold.open;
    localStorage.setItem(FOLD_KEY, JSON.stringify(folds));
  } catch {
    // Without storage the cards still open and close. The state then resets on reload.
  }
}, true);

function renderOverview() {
  const overview = $('overview');
  const previous = overview.querySelector('.overview-inner');
  const comparison = state.summaries.comparison || {};
  const task = state.comparison.task;
  const summary = state.files.length ? comparison.summary || summaryFallback() :
    (state.loading ? 'Reading the Git diff and dependency graph.' : state.error || 'Choose another comparison to see a summary.');
  const stats = totals(state.files);
  overview.innerHTML = `<div class="overview-inner">
    <div class="muted">This comparison</div>
    <h2>${esc(comparison.headline || (state.loading ? 'Analyzing comparison…' : changedCount()))}</h2>
    ${state.comparison.mode === "three-dot" ? `<div class="subtle">${esc(state.comparison.description)}</div>` : ''}
    <details class="ai-card fold" data-fold="summary" ${foldOpen('summary') ? 'open' : ''}>
      <summary><b class="ai-label">AI summary</b><span class="fold-peek">${esc(summary.replace(/`/g, ''))}</span></summary><p>${aiText(summary)}</p></details>
    ${task ? `<details class="task-card fold" data-fold="task" ${foldOpen('task') ? 'open' : ''}>
      <summary><b>Task</b><span class="fold-peek">${esc(task)}</span></summary><p class="task-text">${esc(task)}</p>
      <p class="task-fit"><b class="ai-label">Fit with the task:</b> ${aiText(comparison.taskFit || summaryFallback())}</p></details>` :
    '<button type="button" class="add-task" data-action="task">Add a task summary so the AI can relate these changes to it</button>'}
    <div class="metrics"><div><b>${state.files.length}</b><span class="muted">files</span></div>
      <div><b class="add">+${stats.added}</b><span class="muted">lines added</span></div>
      <div><b class="del">−${stats.removed}</b><span class="muted">lines removed</span></div></div>
    ${saveMessage && !selected ? `<div class="diff-feedback" role="status">${esc(saveMessage)}</div>` : ''}
    ${riskList()}
    ${state.files.length ? `<div class="muted">Review in this order</div><div class="review">${state.files.slice(0, 4).map((file) =>
      `<button type="button" data-action="file" data-path="${esc(file.path)}"><span class="review-score">${file.score}</span>
      <span class="review-detail"><strong title="${esc(file.path)}">${esc(file.cardName)}</strong><span>${esc(file.why)}</span></span></button>`).join('')}</div>` : ''}
  </div>`;
  if (previous) overview.firstElementChild.scrollTop = previous.scrollTop;
  overview.inert = Boolean(selected);
}

// Every risk the summaries flagged, highest impact first.
function riskList() {
  const files = state.summaries.files || {};
  const items = [];
  for (const file of state.files) {
    const entry = files[file.path];
    if (!entry) continue;
    if (entry.risk) items.push({file, text: entry.risk, block: 0});
    (entry.hunks || []).forEach((hunk, index) => {
      if (hunk.risk) items.push({file, text: hunk.risk, block: index + 1, title: hunk.title});
    });
  }
  if (!items.length) return '';
  return `<div class="muted">Possible problems · ${items.length}</div><div class="risks">${items.slice(0, 12).map((item) =>
    `<button type="button" data-action="file" data-path="${esc(item.file.path)}" data-block="${item.block}">
      <span class="risk-icon" aria-hidden="true">${icon('warning')}</span>
      <span class="review-detail"><strong title="${esc(item.file.path)}">${esc(item.file.cardName)}${item.title ? ' · ' + esc(item.title) : ''}</strong>
      <span>${aiText(item.text)}</span></span></button>`).join('')}
    ${items.length > 12 ? `<span class="muted">${items.length - 12} more in the files</span>` : ''}</div>`;
}

function hunkRange(hunk) {
  const start = hunk.newCount ? hunk.newStart : hunk.oldStart;
  const end = start + (hunk.newCount || hunk.oldCount) - 1;
  return end > start ? `Lines ${start}–${end}` : `Lines ${start}`;
}

function normalizeLine(text) {
  return text.replace(/\r$/, '');
}

function hunkText(hunk) {
  return hunk.lines.filter((line) => line.kind === 'add' || line.kind === 'context')
    .map((line) => normalizeLine(line.text)).join('\n');
}

function hunkLines(hunk) {
  let old = hunk.oldStart, next = hunk.newStart;
  return hunk.lines.map(({kind, text}) => {
    const before = ['context', 'del'].includes(kind) ? old++ : '';
    const after = ['context', 'add'].includes(kind) ? next++ : '';
    return {kind, text: normalizeLine(text), before, after};
  });
}

const diffStyles = {add: 'addition', del: 'deletion', context: '', note: 'note', blank: 'blank'};
const diffMarks = {add: '+', del: '−'};

function diffRow(line, numbers) {
  return `<div class="diff-row ${diffStyles[line.kind]}">
    ${numbers.map((number) => `<span class="line-number">${number}</span>`).join('')}
    <span class="line-mark">${diffMarks[line.kind] || ''}</span><span class="line-text">${esc(line.text)}</span>
  </div>`;
}

function splitDiff(lines) {
  const rows = [];
  let removed = [], added = [];
  const blank = {kind: 'blank', text: ''};
  const pair = (left, right) => `<div class="pair">${diffRow(left || blank, [left?.before ?? ''])}${diffRow(right || blank, [right?.after ?? ''])}</div>`;
  function flush() {
    for (let i = 0; i < Math.max(removed.length, added.length); i++) rows.push(pair(removed[i], added[i]));
    removed = [];
    added = [];
  }
  for (const line of lines) {
    if (line.kind === 'del') removed.push(line);
    else if (line.kind === 'add') added.push(line);
    else {
      flush();
      rows.push(pair(line, line));
    }
  }
  flush();
  return rows.join('');
}

const EXPAND_STEP = 20;
const fileLines = new Map();
const gapReveal = new Map();
const gapKey = (index) => `${state.snapshot}|${selected}|${index}`;

// New-side line range and old-number offset of the unchanged gap before hunk `index`
// (index === hunks.length means the stretch after the last hunk).
function gapRange(file, index) {
  const hunks = file.hunks;
  const previous = hunks[index - 1];
  const start = previous ? previous.newStart + previous.newCount : 1;
  if (index < hunks.length) {
    const hunk = hunks[index];
    return {start, end: start + hunk.gap - 1, offset: hunk.newStart - hunk.oldStart};
  }
  const loaded = fileLines.get(`${state.snapshot}|${file.path}`);
  const end = loaded ? loaded.length : Infinity;
  return {start, end, offset: previous ? (previous.newStart + previous.newCount) - (previous.oldStart + previous.oldCount) : 0};
}

function contextRows(lines) {
  return lines.map((line) => diffView === 'split' && !editing
    ? `<div class="pair">${diffRow(line, [line.before])}${diffRow(line, [line.after])}</div>`
    : diffRow(line, [line.before, line.after])).join('');
}

function diffGap(file, index) {
  const {start, end, offset} = gapRange(file, index);
  const size = end - start + 1;
  if (size <= 0) return '';
  const trailing = index === file.hunks.length;
  if (trailing) {
    // Git shows 3 context lines after the last change. Fewer means the file ends there.
    const last = file.hunks[file.hunks.length - 1];
    let context = 0;
    for (let i = last.lines.length - 1; i >= 0 && last.lines[i].kind === 'context'; i--) context++;
    if (context < 3) return '';
  }
  const reveal = gapReveal.get(gapKey(index)) || {top: 0, bottom: 0};
  const loaded = fileLines.get(`${state.snapshot}|${file.path}`);
  const row = (number) => ({kind: 'context', text: normalizeLine(loaded?.[number - 1] ?? ''), before: number - offset, after: number});
  const top = Math.min(reveal.top, size);
  const bottom = Math.min(reveal.bottom, size - top);
  const hidden = size - top - bottom;
  const wrap = (rows) => rows.length ? `<div class="diff-${diffView === 'split' && !editing ? 'split' : 'unified'} diff-expanded">${contextRows(rows)}</div>` : '';
  const topRows = loaded ? Array.from({length: top}, (_, i) => row(start + i)) : [];
  const bottomRows = loaded ? Array.from({length: bottom}, (_, i) => row(end - bottom + 1 + i)) : [];
  let bar = '';
  if (hidden > 0) {
    const count = Number.isFinite(hidden) ? `⋯ ${hidden} unchanged ${hidden === 1 ? 'line' : 'lines'}` : '⋯ Rest of the file';
    const label = Number.isFinite(hidden) ? `${hidden} unchanged lines` : 'the rest of the file';
    const buttons = editing ? '' : [
      index > 0 ? `<button type="button" class="gap-button" data-action="expand" data-gap="${index}" data-direction="down" aria-label="Show ${EXPAND_STEP} more lines after the previous block">${icon('expandDown')}</button>` : '',
      !trailing ? `<button type="button" class="gap-button" data-action="expand" data-gap="${index}" data-direction="up" aria-label="Show ${EXPAND_STEP} more lines before the next block">${icon('expandUp')}</button>` : '',
      `<button type="button" class="gap-button" data-action="expand" data-gap="${index}" data-direction="all" aria-label="Show all ${label}">${icon('gap')}</button>`,
    ].join('');
    bar = `<div class="diff-gap">${buttons || icon('gap')}<span>${count}${index === 0 && !top && !bottom ? ' before this block' : ''}</span></div>`;
  }
  return wrap(topRows) + bar + wrap(bottomRows);
}

async function expandGap(index, direction) {
  const file = selectedFile();
  if (!file) return;
  const cacheKey = `${state.snapshot}|${file.path}`;
  if (!fileLines.has(cacheKey)) {
    const result = await post('lines', {snapshot: state.snapshot, path: file.path, start: 1, end: 1000000});
    fileLines.set(cacheKey, result.lines);
  }
  const {start, end} = gapRange(file, index);
  const size = Math.max(0, end - start + 1);
  const key = gapKey(index);
  const reveal = gapReveal.get(key) || {top: 0, bottom: 0};
  if (direction === 'all') reveal.top = size;
  else if (direction === 'down') reveal.top += EXPAND_STEP;
  else reveal.bottom += EXPAND_STEP;
  gapReveal.set(key, reveal);
  renderFile(true);
}

function diffHunk(hunk, index) {
  const range = hunkRange(hunk);
  const lines = hunkLines(hunk);
  let content;
  if (editing) {
    const text = hunkText(hunk);
    content = `<div class="diff-edit"><span>${range}</span><textarea aria-label="Edit ${range}" spellcheck="false"
      rows="${Math.max(2, Math.min(28, text.split('\n').length))}" data-edit="${index}">\n${esc(text)}</textarea></div>`;
  } else if (diffView === 'split') {
    content = `<div class="diff-split">${splitDiff(lines)}</div>`;
  } else {
    content = `<div class="diff-unified">${lines.map((line) => diffRow(line, [line.before, line.after])).join('')}</div>`;
  }
  return `${hunk.gap ? diffGap(selectedFile(), index) : ''}
    <div class="diff-block" id="blk-${index + 1}" role="group" aria-label="Block ${index + 1}: ${range}">
      ${content}<span class="tip block-tip"><button type="button" class="info" aria-label="AI summary of change ${index + 1}: ${range}" aria-describedby="tip-${index + 1}">${icon('info')}</button>
      <span class="tipbox" role="tooltip" id="tip-${index + 1}"><b data-hunk-title="${index}">${range}</b>
      <small>${range} · +${hunk.added} −${hunk.removed}</small><span data-hunk-summary="${index}"></span></span></span>
    </div>`;
}

function refreshFileSummaries() {
  const file = selectedFile();
  if (!file || !$('file-summary')) return;
  const summary = state.summaries.files?.[file.path] || {};
  $('file-summary').innerHTML = aiText(summary.summary || summaryFallback());
  $('file-why').innerHTML = summary.why ? `<b>Why:</b> ${aiText(summary.why)}` : '';
  $('file-risk').hidden = !summary.risk;
  $('file-risk').innerHTML = summary.risk ? `<b>${icon('warning')} Possible problem</b><span>${aiText(summary.risk)}</span>` : '';
  for (const [index, hunk] of file.hunks.entries()) {
    const entry = summary.hunks?.[index];
    const title = entry?.title || hunkRange(hunk);
    const block = $('blk-' + (index + 1));
    block.setAttribute('aria-label', `Block ${index + 1}: ${title}`);
    block.querySelector('button').setAttribute('aria-label', `AI summary of change ${index + 1}: ${title}`);
    for (const item of $('file-dialog').querySelectorAll(`[data-hunk-title="${index}"]`)) item.textContent = title;
    block.querySelector('[data-hunk-summary]').innerHTML = aiText(entry?.summary || summaryFallback()) +
      (entry?.why ? `<span class="tip-why"><b>Why:</b> ${aiText(entry.why)}</span>` : '') +
      (entry?.risk ? `<span class="tip-risk"><b>${icon('warning')} Possible problem</b> ${aiText(entry.risk)}</span>` : '');
    block.classList.toggle('has-risk', Boolean(entry?.risk));
    const link = $('file-dialog').querySelector(`.blocks [data-block="${index + 1}"]`);
    if (link) {
      link.classList.toggle('has-risk', Boolean(entry?.risk));
      link.title = entry?.risk ? 'Possible problem: ' + entry.risk : '';
    }
  }
}

function renderFile(preserveScroll = false) {
  const dialog = $('file-dialog');
  const file = selectedFile();
  const scrollTop = preserveScroll ? dialog.querySelector('.diff-scroll')?.scrollTop ?? 0 : 0;
  dialog.hidden = !file;
  if (!file) {
    dialog.replaceChildren();
    return;
  }
  const index = state.files.indexOf(file);
  const nextFile = state.files[(index + 1) % state.files.length];
  const previousFile = state.files[(index + state.files.length - 1) % state.files.length];
  const comparison = state.comparison;
  const range = `${comparison.base === 'HEAD' ? 'HEAD' : comparison.baseLabel} → ${comparison.headLabel.toLowerCase() === 'working tree' ? 'working tree' : comparison.headLabel}`;
  const currentBranch = (state.refs.worktrees || []).find((item) => item.value === 'WORKTREE')?.label.split(' · ')[1];
  const editorControls = !comparison.editable ?
    `<span class="read-only" title="Choose the working tree as Compare to edit files here">${icon('lock')} Read only · compare against the working tree to edit</span>` :
    editing ? `<span class="file-editor-label">Editing in the current worktree${currentBranch ? ' · ' + esc(currentBranch) : ''}</span>
      <button type="button" class="pill surface" data-action="discard">Discard</button>
      <button type="button" class="pill primary" data-action="save">Save to file</button>` :
    file.contentHash ? `<button type="button" class="pill edit-button" data-action="edit">${icon('edit')} Edit file</button>` :
    '<span class="read-only">Read only · this file has no editable diff blocks</span>';
  dialog.innerHTML = `
    <aside class="file-side panel" aria-label="File details">
      <button type="button" class="pill surface" data-action="back">${icon('back', 16)} Back to tree</button>
      <div><h2>${esc(file.name)}</h2><div class="file-path compact muted">${esc(file.path)}</div></div>
      <div class="file-pills"><span class="chip status-${file.status}">${statusWords[file.status]}</span><span class="line-pills">${counts(file)}</span></div>
      ${file.note ? `<p class="file-note">${esc(file.note)}</p>` : ''}
      <div class="meter ${file.band}" aria-label="Impact ${file.score} out of 100, ${file.band}">
        <div class="meter-score"><span>Impact</span><b>${file.score}</b><span>/ 100 · ${file.band}</span></div>
        <div class="meter-bar" aria-hidden="true"><span class="fill" style="width:${file.score}%"></span></div>
        <div class="meter-scale"><span>Low</span><span>Medium</span><span>High</span></div>
        <div class="meter-breakdown"><span>Reach <b>${file.impact.reach}</b></span><span>Change type <b>${file.impact.changeType}</b></span>
          <span>Size <b>${file.impact.size}</b></span></div>
        <div class="meter-reason">${esc(file.why)}</div>
      </div>
      <div class="ai-card"><b class="ai-label">AI summary · whole file</b><p id="file-summary"></p><p class="ai-why" id="file-why"></p></div>
      <div class="risk-card" id="file-risk" role="note" hidden></div>
      <section class="used-by" aria-label="Used by"><b>Used by${file.previousImporters?.length ? ' · before deletion' : ''}</b>${usedByTree(file)}</section>
      <div class="blocks-title"><b>Change blocks</b><span>${blockName(file.hunks.length)}</span></div>
      <nav class="blocks" aria-label="Change blocks">${file.hunks.map((hunk, i) => `<a href="#blk-${i + 1}" data-block="${i + 1}">
        <span class="block-index">${i + 1}</span><span class="block-detail"><span data-hunk-title="${i}">${hunkRange(hunk)}</span>
        <small>${hunkRange(hunk)} · ${counts(hunk)}</small></span></a>`).join('')}</nav>
    </aside>
    <div class="diff-panel panel">
      <div class="diff-toolbar">
        <span class="diff-range mono" title="${esc(comparison.description)}">${esc(range)}</span>
        <span class="spacer"></span>${editorControls}
        <div class="segmented" role="group" aria-label="Diff layout">
          <button type="button" data-action="unified" aria-pressed="${!editing && diffView === 'unified'}" ${editing ? 'disabled' : ''}>${icon('unified')} Unified</button>
          <button type="button" data-action="split" aria-pressed="${!editing && diffView === 'split'}" ${editing ? 'disabled' : ''}>${icon('split')} Side by side</button>
        </div>
        <button type="button" class="pill surface file-nav" data-action="previous" ${previousFile === file || editing ? 'disabled' : ''}
          title="Previous file: ${esc(previousFile.path)} (p)" aria-label="Previous file: ${esc(previousFile.cardName)}">Previous file</button>
        <button type="button" class="pill surface file-nav" data-action="next" ${nextFile === file || editing ? 'disabled' : ''}
          title="Next file: ${esc(nextFile.path)} (n)" aria-label="Next file: ${esc(nextFile.cardName)}">Next file</button>
        <span id="save-feedback" class="diff-feedback ${saveFailed ? 'error' : ''}" role="status" ${saveMessage ? '' : 'hidden'}>${esc(saveMessage)}</span>
      </div>
      <div class="diff-scroll" tabindex="0" aria-label="Git diff">
        ${file.hunks.length ? file.hunks.map(diffHunk).join('') + (['A', 'D'].includes(file.status) ? '' : diffGap(file, file.hunks.length)) : `<p class="diff-notice">${esc(file.note || 'No text changes')}</p>`}
      </div>
    </div>`;
  dialog.querySelector('.diff-scroll').scrollTop = scrollTop;
  refreshFileSummaries();
}

function markFile() {
  for (const card of $('workspace').querySelectorAll('.tree-card')) {
    card.setAttribute('aria-pressed', String(card.dataset.path === selected));
  }
  $('workspace').inert = Boolean(selected);
  $('overview').inert = Boolean(selected);
}

function openFile(path, button) {
  selected = path;
  if (button) lastFileButton = button;
  editing = false;
  saveMessage = '';
  saveFailed = false;
  closePicker(false);
  renderFile();
  markFile();
  $('file-dialog').querySelector('[data-action="back"]')?.focus({preventScroll: true});
}

function closeFile() {
  selected = null;
  editing = false;
  saveMessage = '';
  renderFile();
  markFile();
  if (lastFileButton?.isConnected) lastFileButton.focus({preventScroll: true});
}

function pickerButton() {
  return document.querySelector('.range-button');
}

function openPicker(taskFocus = false) {
  if (draft) {
    if (taskFocus) $('task-summary').focus();
    else closePicker();
    return;
  }
  const comparison = state.comparison;
  draft = {base: comparison.base || 'HEAD', head: comparison.head || 'WORKTREE', pr: comparison.pr ?? null, side: 'base', tab: 'branches'};
  $('ref-search').value = '';
  $('task-summary').value = comparison.task || '';
  $('picker-status').textContent = '';
  $('picker-status').classList.remove('error');
  $('picker').hidden = false;
  pickerButton().setAttribute('aria-expanded', 'true');
  renderPicker();
  $(taskFocus ? 'task-summary' : 'ref-search').focus();
}

function closePicker(restoreFocus = true) {
  if (!draft) return;
  draft = null;
  $('picker').hidden = true;
  pickerButton().setAttribute('aria-expanded', 'false');
  renderHeader();
  if (restoreFocus) pickerButton().focus();
}

function pickerItems(kind) {
  const items = state.refs[kind] || [];
  if (kind === 'branches' && draft.side === 'head') {
    return [{value: 'WORKTREE', label: 'Working tree', note: 'Uncommitted changes'}, ...items];
  }
  return items;
}

function renderPicker() {
  if (!draft) return;
  $('pick-base').textContent = refLabel(draft.base, 'base');
  $('pick-head').textContent = refLabel(draft.head, 'head');
  for (const side of ['base', 'head']) {
    $('picker').querySelector(`[data-action="${side}"]`).setAttribute('aria-pressed', String(side === draft.side));
  }
  const currentPr = (state.refs.prs || []).find((item) => item.value === draft.pr);
  $('pr-link').hidden = !currentPr;
  if (currentPr) $('pr-label').textContent = currentPr.label;
  const tabList = $('picker-tabs');
  if (!tabList.firstElementChild) {
    tabList.innerHTML = Object.entries(tabs).map(([key, name]) =>
      `<button type="button" data-action="tab" data-tab="${key}">${name}</button>`).join('');
  }
  for (const button of tabList.children) button.setAttribute('aria-pressed', String(button.dataset.tab === draft.tab));
  renderPickerResults();
  renderHeader();
}

function renderPickerResults() {
  if (!draft) return;
  const results = $('ref-results');
  const query = $('ref-search').value.trim();
  const lower = query.toLowerCase();
  const kinds = query ? Object.keys(tabs) : [draft.tab];
  const matches = kinds.flatMap((kind) => pickerItems(kind).map((item, index) => ({item, kind, index})))
    .filter(({item, kind}) => `${item.label} ${item.note} ${item.path || ''} ${item.value} ${tabs[kind]}`.toLowerCase().includes(lower));
  if (!matches.length) {
    const message = query ? `Nothing matches “${query}”. Try a commit hash, branch name or PR title.` :
      !state.refsReady ? 'Loading references…' : draft.tab === 'prs' ? state.refs.prNote : 'No references in this tab.';
    results.innerHTML = `<div class="no-refs">${esc(message)}</div>`;
    return;
  }
  const focused = document.activeElement;
  const key = focused?.dataset.action === 'ref' ? [focused.dataset.kind, focused.dataset.index] : null;
  const scroll = results.scrollTop;
  results.innerHTML = matches.map(({item, kind, index}) => {
    const value = kind === 'worktrees' && draft.side === 'base' ? item.commit : item.value;
    const on = kind === 'prs' ? draft.pr === item.value : draft[draft.side] === value;
    return `<button type="button" data-action="ref" data-kind="${kind}" data-index="${index}" aria-pressed="${on}">
      ${query ? `<span class="kind">${tabs[kind]}</span>` : ''}
      <span class="ref-name">${esc(item.label)}</span><span class="note-text">${esc(item.note)}
      ${kind === 'worktrees' && draft.side === 'base' ? ' · Uses this worktree’s HEAD' : ''}</span>
      ${kind === 'prs' ? '<span class="sets-both">sets both sides</span>' : ''}
    </button>`;
  }).join('');
  results.scrollTop = scroll;
  if (key) results.querySelector(`[data-kind="${key[0]}"][data-index="${key[1]}"]`)?.focus({preventScroll: true});
}

function selectRef(kind, index) {
  const item = pickerItems(kind)[index];
  if (!item) return;
  if (kind === 'prs') {
    Object.assign(draft, {base: item.base, head: item.head, pr: item.value});
  } else {
    draft.pr = null;
    draft[draft.side] = kind === 'worktrees' && draft.side === 'base' ? item.commit : item.value;
  }
  renderPicker();
}

async function post(action, data) {
  const response = await fetch('/api/' + action, {
    method: 'POST',
    headers: {'Content-Type': 'application/json', 'X-Change-Tree-Token': token},
    body: JSON.stringify(data),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || 'The request failed.');
  return result;
}

function applyState(next, keepScroll = true) {
  if (!keepScroll) {
    canvas?.destroy();
    canvas = null;
    canvasView = null;
  }
  state = next;
  if (selected && !selectedFile()) selected = null;
  renderHeader();
  renderWorkspace(keepScroll);
  renderOverview();
  renderFile(keepScroll);
  renderPicker();
  if (typeof renderChat === 'function') renderChat();
}

async function submitComparison() {
  if (busy || !draft) return;
  busy = true;
  const button = $('picker').querySelector('[data-action="compare"]');
  const status = $('picker-status');
  button.disabled = true;
  status.classList.remove('error');
  status.textContent = 'Analyzing comparison…';
  try {
    const result = await post('compare', {base: draft.base, head: draft.head, pr: draft.pr, task: $('task-summary').value});
    closePicker();
    selected = null;
    editing = false;
    saveMessage = '';
    collapsed = new Set();
    applyState(result, false);
  } catch (error) {
    status.textContent = error.message;
    status.classList.add('error');
  } finally {
    button.disabled = false;
    finishRequest();
  }
}

async function saveEdits() {
  const file = selectedFile();
  if (busy || !file || !editing) return;
  const fields = [...$('file-dialog').querySelectorAll('[data-edit]')];
  const edits = fields.map((field) => ({index: Number(field.dataset.edit), text: field.value}))
    .filter((edit) => edit.text !== hunkText(file.hunks[edit.index]));
  if (!edits.length) {
    editing = false;
    renderFile(true);
    return;
  }
  busy = true;
  const dialog = $('file-dialog');
  const button = dialog.querySelector('[data-action="save"]');
  button.disabled = true;
  for (const field of fields) field.readOnly = true;
  const feedback = $('save-feedback');
  feedback.hidden = false;
  feedback.classList.remove('error');
  feedback.textContent = 'Saving…';
  try {
    const result = await post('save', {path: file.path, hash: file.contentHash, edits, snapshot: state.snapshot});
    editing = false;
    saveFailed = false;
    saveMessage = 'Saved · the diff now reflects your edit';
    applyState(result);
    dialog.querySelector('[data-action="edit"]')?.focus({preventScroll: true});
  } catch (error) {
    saveFailed = true;
    saveMessage = error.message;
    feedback.classList.add('error');
    feedback.textContent = saveMessage;
  } finally {
    button.disabled = false;
    for (const field of fields) field.readOnly = false;
    finishRequest();
  }
}

function finishRequest() {
  busy = false;
  if (queuedUpdate) {
    const update = queuedUpdate;
    queuedUpdate = null;
    receiveUpdate(update);
  }
}

document.addEventListener('click', (event) => {
  const blockLink = event.target.closest('a[data-block]');
  if (blockLink) {
    event.preventDefault();
    const block = $('blk-' + blockLink.dataset.block);
    block.scrollIntoView({block: 'start'});
    block.querySelector(editing ? 'textarea' : 'button').focus({preventScroll: true});
    return;
  }
  const button = event.target.closest('button[data-action]');
  if (!button || busy) return;
  const action = button.dataset.action;
  switch (action) {
    case 'impact':
    case 'directory':
      layout = action;
      for (const tab of document.querySelectorAll('.layout-toggle button')) {
        tab.setAttribute('aria-pressed', String(tab === button));
      }
      renderWorkspace(false);
      break;
    case 'links':
      links = !links;
      button.setAttribute('aria-pressed', String(links));
      document.querySelector('.extra-edges')?.toggleAttribute('hidden', !links);
      break;
    case 'overview':
      closePicker(false);
      if (selected) closeFile();
      else $('overview').classList.toggle('mobile-open');
      break;
    case 'back':
      closeFile();
      break;
    case 'file':
      openFile(button.dataset.path, button);
      if (Number(button.dataset.block) > 0) {
        const block = $('blk-' + button.dataset.block);
        block?.scrollIntoView({block: 'start'});
        block?.querySelector('.block-tip button')?.focus({preventScroll: true});
      }
      break;
    case 'expand':
      expandGap(Number(button.dataset.gap), button.dataset.direction).catch((error) => {
        saveMessage = error.message;
        saveFailed = true;
        renderFile(true);
      });
      break;
    case 'folder': {
      const path = button.dataset.path;
      if (collapsed.has(path)) collapsed.delete(path);
      else collapsed.add(path);
      renderWorkspace();
      [...document.querySelectorAll('.dir-row[data-action="folder"]')].find((row) => row.dataset.path === path)?.focus({preventScroll: true});
      break;
    }
    case 'unified':
    case 'split':
      diffView = action;
      renderFile(true);
      $('file-dialog').querySelector(`[data-action="${action}"]`).focus({preventScroll: true});
      break;
    case 'previous':
    case 'next': {
      const files = state.files;
      const index = files.indexOf(selectedFile()) + (action === 'next' ? 1 : files.length - 1);
      openFile(files[index % files.length].path);
      break;
    }
    case 'edit':
      editing = true;
      saveMessage = '';
      renderFile(true);
      $('file-dialog').querySelector('textarea')?.focus({preventScroll: true});
      break;
    case 'discard':
      editing = false;
      saveMessage = '';
      renderFile(true);
      $('file-dialog').querySelector('[data-action="edit"]')?.focus({preventScroll: true});
      break;
    case 'save':
      saveEdits();
      break;
    case 'picker':
    case 'task':
      openPicker(action === 'task');
      break;
    case 'base':
    case 'head':
      draft.side = action;
      renderPicker();
      break;
    case 'tab':
      draft.tab = button.dataset.tab;
      renderPicker();
      break;
    case 'ref':
      selectRef(button.dataset.kind, Number(button.dataset.index));
      break;
    case 'unlink':
      draft.pr = null;
      renderPicker();
      $('pick-head').parentElement.focus();
      break;
    case 'compare':
      submitComparison();
      break;
  }
});

document.addEventListener('keydown', (event) => {
  if (event.key !== 'Escape' || busy) return;
  if (draft) {
    event.preventDefault();
    closePicker();
  } else if (selected) {
    event.preventDefault();
    closeFile();
  }
});
$('ref-search').addEventListener('input', renderPickerResults);

async function loadState() {
  try {
    const response = await fetch('/api/state');
    if (!response.ok) throw new Error('Could not reach the Change Tree server.');
    const result = await response.json();
    if (!busy) applyState(result);
  } catch (error) {
    if (!busy) applyState({...state, loading: false, error: error.message});
  }
}

function receiveUpdate(update) {
  if (busy) {
    queuedUpdate = update;
    return;
  }
  if (state.snapshot !== update.snapshot) {
    loadState();
    return;
  }
  if (state.refsReady !== update.refsReady) {
    fetch('/api/state').then((response) => response.json()).then((next) => {
      if (next.snapshot === state.snapshot) {
        state = {...state, refs: next.refs, refsReady: next.refsReady};
        renderPicker();
      }
    }).catch(() => {});
  }
  state = {...state, ...update, refsReady: state.refsReady};
  renderOverview();
  refreshFileSummaries();
  if (typeof renderChat === 'function') renderChat();
}

renderWorkspace();
renderOverview();
loadState().then(() => {
  const stream = new EventSource('/api/events');
  stream.addEventListener('message', (event) => receiveUpdate(JSON.parse(event.data)));
});
