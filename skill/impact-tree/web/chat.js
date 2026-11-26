'use strict';

const renderChat = (() => {
  const spark = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round" aria-hidden="true"><path d="M10 3l1.8 5.2L17 10l-5.2 1.8L10 17l-1.8-5.2L3 10l5.2-1.8L10 3zM18 13l1 3 3 1-3 1-1 3-1-3-3-1 3-1 1-3z"></path></svg>';
  const launcher = document.createElement('button');
  launcher.type = 'button';
  launcher.className = 'chat-launcher';
  launcher.setAttribute('aria-label', 'Open change chat');
  launcher.setAttribute('aria-controls', 'chat-panel');
  launcher.setAttribute('aria-expanded', 'false');
  launcher.setAttribute('aria-keyshortcuts', 'C Control+J Meta+J');
  launcher.title = 'Change chat (C, or Ctrl J / ⌘ J)';
  launcher.innerHTML = spark;

  const panel = document.createElement('section');
  panel.id = 'chat-panel';
  panel.className = 'chat-panel';
  panel.hidden = true;
  panel.setAttribute('role', 'dialog');
  panel.setAttribute('aria-labelledby', 'chat-title');
  panel.innerHTML = `
    <header class="chat-header">
      <span class="chat-icon">${spark}</span>
      <h2 id="chat-title">Change chat</h2>
      <button type="button" class="chat-expand" aria-pressed="false" aria-label="Full size chat" aria-keyshortcuts="Control+Shift+Enter Meta+Shift+Enter" title="Full size (Ctrl ⇧ Enter / ⌘ ⇧ Enter)"></button>
      <button type="button" class="chat-clear">Clear</button>
      <button type="button" class="chat-close" aria-label="Close chat">×</button>
    </header>
    <div class="chat-scroll">
      <div class="chat-empty">
        <p>Ask a question about this comparison.</p>
        <div class="chat-suggestions"></div>
      </div>
      <div class="chat-thread" role="log" aria-label="Chat messages" aria-live="polite"></div>
    </div>
    <form class="chat-form">
      <div class="chat-status" role="status" hidden></div>
      <div class="chat-context" hidden>
        <span class="chat-context-label"></span>
        <button type="button" class="chat-context-remove" aria-label="Remove page context">×</button>
      </div>
      <div class="chat-input">
        <label class="sr-only" for="chat-message">Ask about this change</label>
        <textarea id="chat-message" rows="2" maxlength="4000" placeholder="Ask about this change…"></textarea>
        <button type="submit" class="chat-send" aria-label="Send message">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 19V5m-6 6 6-6 6 6"></path></svg>
        </button>
      </div>
    </form>`;
  document.body.append(launcher, panel);

  const find = (selector) => panel.querySelector(selector);
  const input = find('textarea');
  const scroll = find('.chat-scroll');
  const thread = find('.chat-thread');
  const empty = find('.chat-empty');
  const suggestions = find('.chat-suggestions');
  const status = find('.chat-status');
  const chip = find('.chat-context');
  const clear = find('.chat-clear');
  const send = find('.chat-send');
  const rows = new Map();
  let returnFocus = null;
  let snapshot;
  let contextFile = null;
  let contextBlock;
  let selectedText = '';
  let dismissedContext = '';
  let requestPending = false;
  let requestError = '';
  let changedFiles;
  let mentionPattern;

  function button(label, className, action) {
    const element = document.createElement('button');
    element.type = 'button';
    element.className = className;
    element.textContent = label;
    element.addEventListener('click', action);
    return element;
  }

  function open() {
    if (panel.hidden) {
      returnFocus = document.activeElement;
      captureContext();
      panel.hidden = false;
      launcher.setAttribute('aria-expanded', 'true');
      launcher.setAttribute('aria-label', 'Close change chat');
      render();
      scroll.scrollTop = scroll.scrollHeight;
    }
    input.focus({preventScroll: true});
  }

  // Full size: the panel fills the page below the header. The choice is kept across reloads.
  const FULL_KEY = 'change-tree/chat-full/v1';
  const expand = find('.chat-expand');
  const expandIcon = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14 4h6v6M10 20H4v-6M20 4l-7 7M4 20l7-7"></path></svg>';
  const shrinkIcon = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 10h-6V4M4 14h6v6M14 10l7-7M10 14l-7 7"></path></svg>';
  function setFull(value) {
    panel.classList.toggle('full', value);
    expand.setAttribute('aria-pressed', String(value));
    expand.setAttribute('aria-label', value ? 'Normal size chat' : 'Full size chat');
    expand.innerHTML = value ? shrinkIcon : expandIcon;
    try {
      localStorage.setItem(FULL_KEY, value ? '1' : '0');
    } catch {
      // The size then resets on reload.
    }
  }
  function toggleFull() {
    const stick = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 40;
    setFull(!panel.classList.contains('full'));
    if (panel.hidden) open();
    else input.focus({preventScroll: true});
    if (stick) scroll.scrollTop = scroll.scrollHeight;
  }
  let fullSaved = false;
  try {
    fullSaved = localStorage.getItem(FULL_KEY) === '1';
  } catch {
    fullSaved = false;
  }
  setFull(fullSaved);
  expand.addEventListener('click', toggleFull);

  // Up arrow in an empty input brings back the last question, to edit and send again.
  function lastQuestion() {
    const items = (state.chat || []).filter((item) => item.role === 'user');
    return items.length ? items[items.length - 1].text : '';
  }

  function close() {
    panel.hidden = true;
    launcher.setAttribute('aria-expanded', 'false');
    launcher.setAttribute('aria-label', 'Open change chat');
    const target = returnFocus?.isConnected && returnFocus !== document.body && !returnFocus.closest('[hidden], [inert]')
      ? returnFocus : launcher;
    target.focus({preventScroll: true});
  }

  function captureContext() {
    if (contextFile !== selected) {
      contextFile = selected;
      contextBlock = undefined;
      selectedText = '';
    }
    const selection = window.getSelection();
    const anchor = selection?.anchorNode?.parentElement;
    if (anchor?.closest('#chat-panel, .chat-launcher')) return;
    const text = anchor?.closest('#file-dialog, #workspace, #overview') ? selection.toString().trim() : '';
    const active = document.activeElement;
    if (text || (active !== launcher && !panel.contains(active))) selectedText = Array.from(text).slice(0, 2000).join('');
    const focused = active.closest('.diff-block');
    if (focused) contextBlock = Number(focused.id.slice(4)) - 1;
  }

  function pageContext() {
    const context = {};
    if (selected) {
      context.path = selected;
      if (contextBlock !== undefined && contextFile === selected) context.block = contextBlock;
    }
    if (selectedText) context.selectedText = selectedText;
    return context;
  }

  function renderContext() {
    const context = pageContext();
    const parts = [];
    if (context.path) parts.push(context.path);
    if (context.block !== undefined) parts.push(`Block ${context.block + 1}`);
    if (context.selectedText) {
      const preview = context.selectedText.replace(/\s+/g, ' ');
      parts.push(`“${preview.slice(0, 64)}${preview.length > 64 ? '…' : ''}”`);
    }
    chip.hidden = !parts.length || dismissedContext === JSON.stringify(context);
    const label = find('.chat-context-label');
    label.textContent = parts.join(' · ');
    label.title = parts.join(' · ');
  }

  function renderSuggestions() {
    const questions = ['Summarize the riskiest change', ...(selected ? [`Why did ${selected} change?`] : []),
      'What should I test?'];
    if (suggestions.dataset.questions === JSON.stringify(questions)) return;
    suggestions.dataset.questions = JSON.stringify(questions);
    suggestions.replaceChildren(...questions.map((question) => button(question, 'chat-question', () => {
      input.focus();
      sendMessage(question);
    })));
  }

  function openReference(path, line, source) {
    const file = state.files.find((item) => item.path === path);
    if (!file || busy) return;
    if (editing) {
      requestError = 'Save or discard the current edit before opening a reference.';
      renderControls();
      return;
    }
    if (selected !== path) openFile(path);
    const dialog = $('file-dialog');
    returnFocus = dialog.querySelector('[data-action="back"]');
    if (line) {
      dialog.querySelector('.chat-line-target')?.classList.remove('chat-line-target');
      const old = file.status === 'D';
      const row = [...dialog.querySelectorAll('.diff-row')].find((item) => {
        const numbers = item.querySelectorAll('.line-number');
        const pair = item.parentElement;
        if (pair.classList.contains('pair') && item !== (old ? pair.firstElementChild : pair.lastElementChild)) return false;
        return Number(numbers[old || numbers.length === 1 ? 0 : 1]?.textContent) === line;
      });
      row?.classList.add('chat-line-target');
      row?.scrollIntoView({block: 'center', inline: 'nearest'});
    }
    source.focus({preventScroll: true});
  }

  function reference(path, line, label = line ? `${path}:${line}` : path) {
    if (!changedFiles.has(path)) return document.createTextNode(label);
    const link = button(label, 'chat-reference', () => openReference(path, line, link));
    link.title = line ? `Open ${path} at line ${line}` : `Open ${path}`;
    return link;
  }

  function fileTarget(target) {
    if (changedFiles.has(target)) return [target, null];
    const match = target.match(/^(.*?)(?::|#L)([1-9]\d*)$/);
    return match && changedFiles.has(match[1]) ? [match[1], Number(match[2])] : null;
  }

  function appendText(parent, value) {
    let start = 0;
    for (const match of mentionPattern ? value.matchAll(mentionPattern) : []) {
      parent.append(value.slice(start, match.index), reference(match[1], Number(match[2])));
      start = match.index + match[0].length;
    }
    parent.append(value.slice(start));
  }

  // Text is inserted through DOM text nodes. Only these Markdown forms create elements.
  function inline(parent, value) {
    let start = 0;
    for (const match of value.matchAll(/`([^`\n]+)`|\*\*(.+?)\*\*|\[([^\]\n]+)\]\(([^)\n]+)\)/g)) {
      appendText(parent, value.slice(start, match.index));
      if (match[1]) {
        const code = document.createElement('code');
        appendText(code, match[1]);
        parent.append(code);
      } else if (match[2]) {
        const bold = document.createElement('strong');
        inline(bold, match[2]);
        parent.append(bold);
      } else {
        const target = fileTarget(match[4]);
        if (target) parent.append(reference(...target, match[3]));
        else parent.append(match[0]);
      }
      start = match.index + match[0].length;
    }
    appendText(parent, value.slice(start));
  }

  function markdown(parent, text) {
    const lines = text.replace(/\r\n?/g, '\n').split('\n');
    const listItem = /^\s*(?:([-*+])|(\d+)\.)\s+(.+)$/;
    let index = 0;
    while (index < lines.length) {
      const line = lines[index++];
      if (!line.trim()) continue;
      if (/^\s*```/.test(line)) {
        const content = [];
        while (index < lines.length && !/^\s*```\s*$/.test(lines[index])) content.push(lines[index++]);
        index++;
        const pre = document.createElement('pre');
        const code = document.createElement('code');
        code.textContent = content.join('\n');
        pre.append(code);
        parent.append(pre);
      } else if (listItem.test(line)) {
        let item = line.match(listItem);
        const ordered = Boolean(item[2]);
        const list = document.createElement(ordered ? 'ol' : 'ul');
        if (ordered) list.start = Number(item[2]);
        while (true) {
          const li = document.createElement('li');
          inline(li, item[3]);
          list.append(li);
          item = lines[index]?.match(listItem);
          if (!item || Boolean(item[2]) !== ordered) break;
          index++;
        }
        parent.append(list);
      } else {
        const paragraph = [line];
        while (index < lines.length && lines[index].trim() && !/^\s*```/.test(lines[index]) && !listItem.test(lines[index])) {
          paragraph.push(lines[index++]);
        }
        const p = document.createElement('p');
        inline(p, paragraph.join(' '));
        parent.append(p);
      }
    }
  }

  function messageRow(item) {
    const row = document.createElement('article');
    row.className = `chat-message chat-${item.role}`;
    row.setAttribute('aria-label', item.role === 'user' ? 'You' : 'Change chat reply');
    const bubble = document.createElement('div');
    bubble.className = 'chat-bubble';
    if (item.role === 'user') {
      bubble.textContent = item.text;
    } else {
      markdown(bubble, item.text);
      if (item.references?.length) {
        const references = document.createElement('div');
        references.className = 'chat-references';
        for (const {path, line} of item.references) references.append(reference(path, line));
        bubble.append(references);
      }
    }
    row.append(bubble);
    if (item.status === 'failed') {
      const failure = document.createElement('div');
      failure.className = 'chat-failure';
      failure.append('Reply failed.', button('Retry', 'chat-retry', () => chatRequest('chat/retry', {id: item.id})));
      row.append(failure);
    }
    return row;
  }

  function render() {
    const switched = snapshot !== state.snapshot;
    if (switched || !changedFiles) {
      snapshot = state.snapshot;
      contextFile = null;
      contextBlock = undefined;
      selectedText = '';
      dismissedContext = '';
      requestError = '';
      rows.clear();
      thread.replaceChildren();
      changedFiles = new Set(state.files.map((file) => file.path));
      const paths = [...changedFiles].sort((a, b) => b.length - a.length)
        .map((path) => path.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
      mentionPattern = paths.length ? new RegExp('(?<![\\w./\\\\-])(' + paths.join('|') + '):([1-9]\\d*)(?=$|[\\s\'"\x60<>\\[\\](),;.!?])', 'g') : null;
    }
    const messages = state.chat || [];
    const stickToBottom = switched || scroll.scrollHeight - scroll.clientHeight - scroll.scrollTop < 40;
    const ids = new Set(messages.map((item) => item.id));
    for (const [id, value] of rows) {
      if (ids.has(id)) continue;
      value.row.remove();
      rows.delete(id);
    }
    messages.forEach((item, index) => {
      let entry = rows.get(item.id);
      if (!entry || entry.failed !== (item.status === 'failed')) {
        const row = messageRow(item);
        entry?.row.replaceWith(row);
        entry = {row, failed: item.status === 'failed'};
        rows.set(item.id, entry);
      }
      const position = thread.children[index];
      if (entry.row !== position) thread.insertBefore(entry.row, position || null);
    });
    empty.hidden = messages.length > 0;
    if (!empty.hidden) renderSuggestions();
    renderContext();
    renderControls();
    if (stickToBottom) scroll.scrollTop = scroll.scrollHeight;
  }

  function renderControls() {
    const ready = Boolean(state.snapshot && !state.loading && !state.error);
    clear.disabled = !ready || requestPending || !state.chat?.length;
    send.disabled = !ready || requestPending || !input.value.trim();
    for (const retry of thread.querySelectorAll('.chat-retry')) retry.disabled = requestPending;
    const message = requestError || (state.chatStatus === 'pending' ? 'Thinking…' : '');
    if (status.textContent !== message) status.textContent = message;
    status.hidden = !message;
    status.classList.toggle('chat-error', Boolean(requestError));
  }

  async function chatRequest(action, data) {
    if (requestPending || state.loading || !state.snapshot || state.error) return false;
    const requestSnapshot = state.snapshot;
    requestPending = true;
    requestError = '';
    renderControls();
    try {
      await post(action, {snapshot: requestSnapshot, ...data});
      return true;
    } catch (error) {
      if (state.snapshot === requestSnapshot) requestError = error.message;
      return false;
    } finally {
      requestPending = false;
      renderControls();
    }
  }

  async function sendMessage(message) {
    if (!message.trim()) return;
    const draftText = input.value;
    const context = pageContext();
    const sent = await chatRequest('chat', {
      message, context: dismissedContext === JSON.stringify(context) ? {} : context,
    });
    if (sent && input.value === draftText) input.value = '';
    renderControls();
  }

  launcher.addEventListener('click', () => panel.hidden ? open() : close());
  find('.chat-close').addEventListener('click', close);
  clear.addEventListener('click', async () => {
    input.focus();
    await chatRequest('chat/clear', {});
  });
  find('.chat-context-remove').addEventListener('click', () => {
    dismissedContext = JSON.stringify(pageContext());
    renderContext();
    input.focus();
  });
  find('form').addEventListener('submit', (event) => {
    event.preventDefault();
    sendMessage(input.value);
  });
  input.addEventListener('input', renderControls);
  launcher.addEventListener('pointerdown', captureContext);
  document.addEventListener('selectionchange', () => {
    captureContext();
    renderContext();
  });
  document.addEventListener('focusin', () => {
    captureContext();
    renderContext();
  });
  new MutationObserver(() => {
    captureContext();
    renderContext();
    if (!empty.hidden) renderSuggestions();
  }).observe($('file-dialog'), {childList: true});

  window.addEventListener('keydown', (event) => {
    if (event.defaultPrevented || event.isComposing || draft
        || document.querySelector('.shortcuts:not([hidden]), #command-palette[open]')) return;
    const command = (event.ctrlKey || event.metaKey) && !event.altKey;
    // Ctrl J / Cmd J works everywhere: open the chat, move focus to it, or close it when it has focus.
    if (command && !event.shiftKey && event.key.toLowerCase() === 'j') {
      event.preventDefault();
      event.stopPropagation();
      if (panel.hidden) open();
      else if (panel.contains(document.activeElement)) close();
      else input.focus({preventScroll: true});
      return;
    }
    // Ctrl Shift Enter / Cmd Shift Enter: full size on or off.
    if (command && event.shiftKey && event.key === 'Enter') {
      event.preventDefault();
      event.stopPropagation();
      toggleFull();
      return;
    }
    if (!panel.hidden && panel.contains(event.target)) {
      if (event.target === input && event.key === 'ArrowUp' && !input.value && !command) {
        const text = lastQuestion();
        if (text) {
          event.preventDefault();
          event.stopPropagation();
          input.value = text;
          input.setSelectionRange(text.length, text.length);
          renderControls();
          return;
        }
      }
      if (event.key === 'PageUp' || event.key === 'PageDown') {
        event.preventDefault();
        event.stopPropagation();
        scroll.scrollBy({top: (event.key === 'PageUp' ? -0.85 : 0.85) * scroll.clientHeight});
        return;
      }
      if (command && (event.key === 'Home' || event.key === 'End')) {
        event.preventDefault();
        event.stopPropagation();
        scroll.scrollTop = event.key === 'Home' ? 0 : scroll.scrollHeight;
        return;
      }
      if (command && event.shiftKey && event.key === 'Backspace') {
        event.preventDefault();
        event.stopPropagation();
        clear.click();
        return;
      }
    }
    if (!panel.hidden && event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      close();
      return;
    }
    if (!event.ctrlKey && !event.metaKey && !event.altKey && !event.shiftKey && event.key.toLowerCase() === 'c'
        && !event.target.closest('input, textarea, select, [contenteditable="true"]')) {
      event.preventDefault();
      event.stopPropagation();
      open();
      return;
    }
    if (!panel.contains(event.target) && event.target !== launcher) return;
    if (event.target === input && event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      event.stopPropagation();
      sendMessage(input.value);
    } else if (!event.ctrlKey && !event.metaKey) {
      // The file view's keyboard handler traps Tab and handles single keys.
      event.stopPropagation();
    }
  }, true);

  render();
  // Used by the command palette.
  globalThis.changeChat = {open, close, toggleFull, clear: () => clear.click(), isOpen: () => !panel.hidden,
    isFull: () => panel.classList.contains('full')};
  return render;
})();
