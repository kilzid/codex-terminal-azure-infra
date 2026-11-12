// Runs in two separate sandboxed containers; no OpenAI credentials or paid requests.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import http from 'node:http';
import { spawn } from 'node:child_process';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
const phase = process.argv[2];
assert.ok(['prepare', 'verify'].includes(phase));
const home = process.env.CODEX_HOME;
assert.ok(home && home.endsWith('/real-restore-test'));
fs.mkdirSync(home, { recursive: true, mode: 0o700 });
// Prevent the parent coding session's daemon/thread variables from entering the test.
const keep = new Set(['PATH', 'HOME', 'USER', 'SHELL', 'CODEX_HOME', 'TERM', 'TMPDIR', 'TERMINAL_ROOT', 'SESSION_LAUNCHER']);
for (const key of Object.keys(process.env)) if (!keep.has(key)) delete process.env[key];
process.env.TERM = 'xterm-256color';
const managerPath = process.env.SESSION_LAUNCHER || '/opt/codex-session.mjs';
const { checkpoint, restore, snapshot, prepareHerdrEnvironment, herdr } = await import(managerPath);
const pty = createRequire((process.env.TERMINAL_ROOT || '/opt/codex-terminal') + '/package.json')('node-pty');
prepareHerdrEnvironment();
const api = (...args) => JSON.parse(herdr(...args)).result;
let pane;
const screen = () => herdr('pane', 'read', pane, '--lines', '200');
const sessionId = () => Object.values(snapshot().workspaces[0].tabs[0].panes)[0].agent_session?.value;
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
async function until(check, label) {
  for (let i = 0; i < 160; i++) { if (await check()) return; await pause(250); }
  throw Error('Timed out: ' + label);
}
let requests = 0;
const text = 'RESTORE_REAL_CODEX_OK';
const item = { id: 'msg_restore_test', type: 'message', status: 'completed', role: 'assistant',
  content: [{ type: 'output_text', text, annotations: [] }] };
const server = http.createServer((req, res) => {
  req.resume();
  if (req.method === 'GET') {
    res.setHeader('content-type', 'application/json');
    res.end(JSON.stringify({ object: 'list', data: [{ id: 'gpt-5.4', object: 'model', owned_by: 'test' }] })); return;
  }
  requests++;
  res.writeHead(200, { 'content-type': 'text/event-stream', 'cache-control': 'no-cache' });
  const send = event => res.write('event: ' + event.type + '\ndata: ' + JSON.stringify(event) + '\n\n');
  send({ type: 'response.created', response: { id: 'resp_restore_test', object: 'response', status: 'in_progress', output: [] } });
  send({ type: 'response.output_item.added', output_index: 0, item: { ...item, status: 'in_progress', content: [] } });
  send({ type: 'response.output_text.delta', output_index: 0, content_index: 0, item_id: item.id, delta: text });
  send({ type: 'response.output_item.done', output_index: 0, item });
  send({ type: 'response.completed', response: { id: 'resp_restore_test', object: 'response', status: 'completed',
    output: [item], usage: { input_tokens: 10, output_tokens: 2, total_tokens: 12 } } });
  res.end();
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const configPath = path.join(home, 'config.toml');
if (phase === 'verify') {
  // Preserve persisted hook consent while changing only the local stub's port.
  const previous = fs.readFileSync(configPath, 'utf8');
  fs.writeFileSync(configPath, previous.replace(/^base_url = .*$/m, `base_url = "http://127.0.0.1:${server.address().port}/v1"`));
} else fs.writeFileSync(configPath, `model = "gpt-5.4"
check_for_update_on_startup = false
model_provider = "restore_test"
[model_providers.restore_test]
name = "Local restoration test"
base_url = "http://127.0.0.1:${server.address().port}/v1"
wire_api = "responses"
requires_openai_auth = false
supports_websockets = false
[features]
hooks = true
daemon_auto_start = false
plugins = false
remote_plugin = false
plugin_discovery_prefetch = false
`, { mode: 0o600 });
fs.writeFileSync(path.join(home, 'herdr/config.toml'), 'onboarding = false\n[session]\nresume_agents_on_restore = true\n[update]\nversion_check = false\nmanifest_check = false\n');
// Include the native integration installed by the production launcher.
const { prepareHerdr } = await import(pathToFileURL(path.join(process.env.TERMINAL_ROOT || '/opt/codex-terminal', 'herdr.mjs')));
Object.assign(process.env, prepareHerdr().env);
// Local execution uses the same managed hook without changing /etc or live state.
if (process.env.SESSION_LAUNCHER) {
  const file = path.join(home, 'hooks.json');
  const config = JSON.parse(fs.readFileSync(file));
  for (const event of ['SessionStart', 'SessionEnd']) {
    config.hooks[event] ||= [];
    const command = 'node ' + managerPath + ' hook';
    if (!config.hooks[event].some(group => group.hooks?.some(hook => hook.command === command)))
      config.hooks[event].unshift({ hooks: [{ type: 'command', command, timeout: 3 }] });
  }
  fs.writeFileSync(file, JSON.stringify(config));
}
const codexRunning = () => {
  try { return api('pane', 'process-info', '--pane', pane).process_info.foreground_processes.some(p => p.name === 'codex'); }
  catch { return false; } // Restored PTYs are initialized when the client attaches.
};
if (phase === 'verify') assert.equal(restore(), true);
const background = spawn('herdr', ['--session', 'main', 'server'], { stdio: 'ignore' });
let client;
try {
  if (phase === 'prepare') {
    await until(() => { try { api('api', 'snapshot'); return true; } catch { return false; } }, 'Herdr server');
    pane = api('workspace', 'create', '--cwd', '/workspace', '--label', 'real Codex test').root_pane.pane_id;
    client = pty.spawn('herdr', ['--session', 'main'], { env: process.env, cols: 120, rows: 40 });
    client.onData(() => {});
    herdr('pane', 'run', pane, 'codex --dangerously-bypass-approvals-and-sandbox');
    await until(() => screen().includes('Ask Codex'), 'Codex input readiness');
    await pause(1000);
    herdr('pane', 'run', pane, 'Local restoration test. Do not execute tools.');
    await until(async () => {
      const output = screen();
      // Only this disposable test home's known hooks are trusted by the fixture.
      if (output.includes('Hooks need review')) {
        herdr('pane', 'send-keys', pane, '2');
        await pause(250);
        herdr('pane', 'send-keys', pane, 'enter');
      } else if (output.includes('t trust all')) {
        herdr('pane', 'send-keys', pane, 't');
        await pause(250);
        herdr('pane', 'send-keys', pane, 'esc');
      } else if (output.includes('Lifecycle hooks from config')) herdr('pane', 'send-keys', pane, 'esc');
      else if (output.includes('SessionStart hooks')) herdr('pane', 'send-keys', pane, 'esc');
      else if (!requests && output.includes('Ask Codex') && !output.includes('Waiting for startup'))
        herdr('pane', 'run', pane, 'Local restoration test. Do not execute tools.');
      return output.includes(text) && sessionId();
    }, 'saved Codex reply and lifecycle mapping');
    const walk = dir => fs.readdirSync(dir, { withFileTypes: true }).flatMap(entry =>
      entry.isDirectory() ? walk(path.join(dir, entry.name)) : [path.join(dir, entry.name)]);
    let id;
    await until(() => {
      id = sessionId();
      if (!id || !fs.existsSync(path.join(home, 'sessions'))) return false;
      const transcripts = walk(path.join(home, 'sessions')).filter(file => path.basename(file).includes(id));
      if (transcripts.length !== 1) return false;
      const raw = fs.readFileSync(transcripts[0], 'utf8');
      try { return JSON.parse(raw.split('\n')[0]).payload.id === id && raw.includes(text); } catch { return false; }
    }, 'durable transcript for the exact mapped conversation');
    checkpoint(true);
    fs.writeFileSync(path.join(home, 'expected-thread.json'), JSON.stringify({ id }), { mode: 0o600 });
    console.log('REAL_CODEX_CONVERSATION_CHECKPOINT_OK');
  } else {
    const expected = JSON.parse(fs.readFileSync(path.join(home, 'expected-thread.json'), 'utf8'));
    await until(() => { try { pane = api('api', 'snapshot').snapshot.panes[0]?.pane_id; return Boolean(pane); } catch { return false; } }, 'restored Herdr pane');
    client = pty.spawn('herdr', ['--session', 'main'], { env: process.env, cols: 120, rows: 40 });
    client.onData(() => {});
    // No input is sent during verification: the restored conversation must wait.
    await until(() => codexRunning() && sessionId() === expected.id &&
      screen().includes(text), 'exact real Codex conversation resume');
    await pause(1000);
    assert.equal(requests, 0, 'automatic restoration must not submit a prompt or run inference');
    assert.equal(sessionId(), expected.id);
    console.log('REAL_CODEX_EXACT_CONVERSATION_RESTORE_WITHOUT_PROMPT_OK');
  }
} catch (error) {
  // These tests contain only synthetic data; never use this diagnostic on production panes.
  try { console.error(JSON.stringify({ phase, screen: screen(), mapped: sessionId() })); } catch {}
  throw error;
} finally {
  try { herdr('session', 'stop', 'main'); } catch {}
  client?.kill();
  if (background.exitCode === null) background.kill();
  server.closeAllConnections(); server.close();
}
