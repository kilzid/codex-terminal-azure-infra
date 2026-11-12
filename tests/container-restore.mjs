// Two invocations in separate containers sharing only persistent data volumes.
import fs from 'node:fs';
import path from 'node:path';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { spawn } from 'node:child_process';
const managerPath = process.env.SESSION_LAUNCHER || '/opt/codex-session.mjs';
const { snapshot, checkpoint, hook, restore, prepareHerdrEnvironment, herdr } = await import(managerPath);
const config = process.env.CODEX_HOME || '/home/node/.codex';
const pty = createRequire((process.env.TERMINAL_ROOT || '/opt/codex-terminal') + '/package.json')('node-pty');
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
const api = (...args) => JSON.parse(herdr(...args)).result;
async function until(check, label) {
  for (let i = 0; i < 160; i++) { if (check()) return; await pause(100); }
  throw Error('Timed out: ' + label);
}
prepareHerdrEnvironment();
const phase = process.argv[2];
const bin = path.join(config, 'restore-test-bin');
if (phase === 'verify') process.env.PATH = bin + ':' + process.env.PATH;
if (phase === 'verify') assert.equal(restore(), true);
const server = spawn('herdr', ['--session', 'main', 'server'], { stdio: 'ignore' });
let client;
try {
  await until(() => { try { api('api', 'snapshot'); return true; } catch { return false; } }, 'Herdr server');
  if (phase === 'prepare') {
    const created = api('workspace', 'create', '--cwd', '/workspace', '--label', 'saved workspace');
    const pane = created.root_pane.pane_id;
    api('tab', 'rename', created.tab.tab_id, 'saved tab');
    api('pane', 'split', pane, '--direction', 'right', '--cwd', config);
    api('tab', 'create', '--cwd', '/workspace', '--label', 'third');
    api('pane', 'zoom', pane, '--on');
    process.env.HERDR_PANE_ID = pane;
    hook({ hook_event_name: 'SessionStart', session_id: 'container_conversation' }, api('pane', 'process-info', '--pane', pane).process_info.shell_pid);
    hook({ hook_event_name: 'SessionStart', session_id: 'ephemeral', transcript_path: '' }, process.pid);
    hook({ hook_event_name: 'SessionEnd', session_id: 'ephemeral' });
    hook({ hook_event_name: 'SessionStart', session_id: 'unrelated' }, process.pid);
    const saved = snapshot();
    assert.equal(Object.values(saved.workspaces[0].tabs[0].panes)[0].agent_session.value, 'container_conversation');
    checkpoint(true);
    hook({ hook_event_name: 'SessionEnd', session_id: 'container_conversation' });
    assert.equal(Object.values(snapshot().workspaces[0].tabs[0].panes)[0].agent_session.value, 'container_conversation', 'frozen lifecycle events cannot erase recovery state');
    fs.writeFileSync(path.join(config, 'expected.json'), JSON.stringify(saved), { mode: 0o600 });
    fs.mkdirSync(bin);
    fs.writeFileSync(path.join(bin, 'codex'), '#!/bin/sh\nprintf "%s\\n" "$@" >> "$CODEX_HOME/restore-argv"\nexit 1\n', { mode: 0o700 });
  } else {
    // A real TUI client supplies terminal geometry, enabling pending native resumes.
    client = pty.spawn('herdr', ['--session', 'main'], { env: process.env, cols: 120, rows: 40 });
    client.onData(() => {});
    await until(() => fs.existsSync(path.join(config, 'restore-argv')), 'explicit conversation resume');
    const expected = JSON.parse(fs.readFileSync(path.join(config, 'expected.json')));
    const actual = snapshot();
    const shape = value => value.workspaces.map(w => [w.custom_name, w.active_tab, w.tabs.map(t =>
      [t.custom_name, t.zoomed, t.focused, t.layout, Object.values(t.panes).map(p => p.cwd)])]);
    // Raw IDs are remapped by Herdr, so compare tree structure separately below.
    assert.deepEqual(shape(actual), shape(expected));
    assert.deepEqual(fs.readFileSync(path.join(config, 'restore-argv'), 'utf8').trim().split('\n'),
      ['resume', '--dangerously-bypass-approvals-and-sandbox', 'container_conversation']);
    assert.equal(restore(), false, 'a running session must never replay resumes');
    checkpoint(true);
    assert.deepEqual(JSON.parse(fs.readFileSync(path.join(config, 'terminal-restore/herdr-deployment.json'))), expected,
      'failed replacement must retain the deployment snapshot');
    console.log('HERDR_CONTAINER_REPLACEMENT_RESTORE_OK');
  }
} finally {
  try { herdr('session', 'stop', 'main'); } catch {}
  client?.kill();
  if (server.exitCode === null) server.kill();
}
