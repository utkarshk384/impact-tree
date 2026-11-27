// Summary agent settings: the model and effort the skill's agent passes to each summary sub-agent.
const agentButton = $('agent-button');
const agentPanel = $('agent-settings');

function agentLabel(settings = state.settings || {}) {
  const chosen = agentCatalog().models.find((item) => item.value === settings.model);
  const model = chosen ? chosen.label : settings.model && settings.model !== 'default' ? settings.model : 'session model';
  const effort = settings.effort && settings.effort !== 'default' ? ` · ${settings.effort}` : '';
  return `AI · ${model}${effort}`;
}

function renderAgentButton() {
  $('agent-label').textContent = agentLabel();
}

const EFFORT_NAMES = {low: 'Low', medium: 'Medium', high: 'High', xhigh: 'Extra high', max: 'Max', ultra: 'Ultra'};

function agentCatalog() {
  return state.agentCatalog || {agent: 'claude', label: 'Claude Code', models: [], efforts: []};
}

// Fill the effort list with the levels that the chosen model supports.
function fillEfforts(chosen) {
  const options = agentCatalog();
  const model = options.models.find((item) => item.value === $('agent-model').value);
  const levels = model ? model.efforts : options.efforts;
  const keep = levels.includes(chosen) ? chosen : 'default';
  $('agent-effort').innerHTML = ['default', ...levels].map((level) =>
    `<option value="${esc(level)}">${level === 'default' ? 'Default' : esc(EFFORT_NAMES[level] || level)}</option>`).join('');
  $('agent-effort').value = keep;
}

function openAgentSettings() {
  const settings = state.settings || {model: 'default', effort: 'default'};
  const options = agentCatalog();
  $('agent-host').textContent = '· ' + options.label;
  $('agent-model').innerHTML = `<option value="default">Same as the ${esc(options.label)} session</option>` +
    options.models.map((item) => `<option value="${esc(item.value)}">${esc(item.label)}${item.label !== item.value ? ' · ' + esc(item.value) : ''}</option>`).join('');
  $('agent-model').value = options.models.some((item) => item.value === settings.model) ? settings.model : 'default';
  fillEfforts(settings.effort);
  $('agent-hint').textContent = options.agent === 'codex'
    ? 'Models from your Codex account (~/.codex/models_cache.json).'
    : 'Models that the Claude Code Agent tool can start.';
  $('agent-status').textContent = '';
  agentPanel.hidden = false;
  agentButton.setAttribute('aria-expanded', 'true');
  $('agent-model').focus();
}

function closeAgentSettings(restore = true) {
  if (agentPanel.hidden) return;
  agentPanel.hidden = true;
  agentButton.setAttribute('aria-expanded', 'false');
  if (restore) agentButton.focus();
}

async function saveAgentSettings() {
  const status = $('agent-status');
  status.classList.remove('error');
  status.textContent = 'Saving…';
  try {
    const result = await post('settings', {model: $('agent-model').value || 'default', effort: $('agent-effort').value});
    state = {...state, settings: result.settings};
    renderAgentButton();
    closeAgentSettings();
  } catch (error) {
    status.classList.add('error');
    status.textContent = error.message;
  }
}

agentButton.addEventListener('click', () => (agentPanel.hidden ? openAgentSettings() : closeAgentSettings()));
$('agent-cancel').addEventListener('click', () => closeAgentSettings());
$('agent-save').addEventListener('click', saveAgentSettings);
$('agent-model').addEventListener('change', () => fillEfforts($('agent-effort').value));
agentPanel.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') {
    event.preventDefault();
    event.stopPropagation();
    closeAgentSettings();
  } else if (event.key === 'Enter' && event.target.tagName !== 'BUTTON' && event.target.tagName !== 'SELECT') {
    event.preventDefault();
    saveAgentSettings();
  } else if (event.key === 'Tab') {
    trapTab(event, agentPanel);
  }
}, true);
document.addEventListener('pointerdown', (event) => {
  if (!agentPanel.hidden && !agentPanel.contains(event.target) && !agentButton.contains(event.target)) closeAgentSettings(false);
});

// Keep the label current when the server reports new settings.
const previousReceive = receiveUpdate;
receiveUpdate = (update) => {
  previousReceive(update);
  renderAgentButton();
};
const previousApply = applyState;
applyState = (...args) => {
  previousApply(...args);
  if (state.view) closeAgentSettings(false);
  renderAgentButton();
};
renderAgentButton();
