import test from 'node:test';
import assert from 'node:assert/strict';
import { startupAction } from './codex-startup.mjs';
const ready = { running: true, requests: 0 };

test('hook consent is handled before or after the composer first appears', () => {
  for (const screens of [
    ['Hooks need review\n1 hook is new or changed.', '› Ask Codex to do anything'],
    ['› Ask Codex to do anything', 'Hooks need review\n1 hook is new or changed.', '› Ask Codex to do anything'],
  ]) {
    const actions = screens.map(screen => startupAction(screen, ready));
    assert.equal(actions[screens.findIndex(s => s.includes('Hooks need review'))], 'trust-picker');
    assert.equal(actions.at(-1), 'seed');
  }
});

test('composer wording and model labels do not control readiness', () => {
  for (const output of ['› Ask Codex to do anything', '› Describe your task', '> New conversation', ''])
    assert.equal(startupAction(output, ready), 'seed');
});

test('startup, process absence, cooldown and a submitted request prevent more input', () => {
  assert.equal(startupAction('Waiting for startup', ready), 'wait');
  assert.equal(startupAction('› Describe a task', { ...ready, running: false }), 'wait');
  assert.equal(startupAction('› Describe a task', { ...ready, coolingDown: true }), 'wait');
  assert.equal(startupAction('Working', { ...ready, requests: 1 }), 'wait');
});

test('hook browser actions take priority over prompt submission', () => {
  assert.equal(startupAction('Hooks\nt trust all · enter review · esc close', ready), 'trust-browser');
  assert.equal(startupAction('SessionStart hooks\nManaged (always on)', ready), 'close-browser');
  assert.equal(startupAction('Lifecycle hooks from config and enabled plugins.', ready), 'close-browser');
});
