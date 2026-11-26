// Summary agent settings: the model and effort the skill's agent passes to each summary sub-agent.
const agentButton = $('agent-button');
const agentPanel = $('agent-settings');

function agentLabel(settings = state.settings || {}) {
  const model = settings.model && settings.model !== 'default' ? settings.model : 'session model';
  const effort = settings.effort && settings.effort !== 'default' ? ` · ${settings.effort}` : '';
  return `AI · ${model}${effort}`;
}

function renderAgentButton() {
  $('agent-label').textContent = agentLabel();
}

function openAgentSettings() {
  const settings = state.settings || {model: 'default', effort: 'default'};
  $('agent-model').value = settings.model;
  $('agent-effort').value = settings.effort;
  $('agent-status').textContent = '';
  agentPanel.hidden = false;
  agentButton.setAttribute('aria-expanded', 'true');
  $('agent-model').focus();
  $('agent-model').select();
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
    const result = await post('settings', {model: $('agent-model').value.trim() || 'default', effort: $('agent-effort').value});
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
agentPanel.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') {
    event.preventDefault();
    event.stopPropagation();
    closeAgentSettings();
  } else if (event.key === 'Enter' && event.target.tagName !== 'BUTTON') {
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
  renderAgentButton();
};
renderAgentButton();
