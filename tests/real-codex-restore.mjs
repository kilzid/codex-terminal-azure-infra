// Runs in two separate sandboxed containers; no OpenAI credentials or paid requests.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import http from 'node:http';
import { execFileSync } from 'node:child_process';
const phase = process.argv[2];
assert.ok(['prepare', 'verify'].includes(phase));
const home = process.env.CODEX_HOME;
assert.ok(home && home.endsWith('/real-restore-test'));
fs.mkdirSync(home, { recursive: true, mode: 0o700 });
const tmp = '/tmp/real-codex-restore-tmux';
fs.mkdirSync(tmp, { recursive: true, mode: 0o700 });
const env = { PATH: process.env.PATH, HOME: '/home/node', USER: 'node', SHELL: '/bin/bash',
  CODEX_HOME: home, TMUX_TMPDIR: tmp, TERM: 'xterm-256color' };
process.env.TMUX_TMPDIR = tmp;
delete process.env.TMUX; delete process.env.TMUX_PANE;
const { checkpoint, restore, snapshot } = await import('/opt/codex-session.mjs');
const tmux = (...args) => execFileSync('tmux', args, { env, encoding: 'utf8' }).trim();
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
async function until(check, label) {
  for (let i = 0; i < 160; i++) { if (check()) return; await pause(250); }
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
fs.writeFileSync(path.join(home, 'config.toml'), `model = "gpt-5.4"
model_provider = "restore_test"
[model_providers.restore_test]
name = "Local restoration test"
base_url = "http://127.0.0.1:${server.address().port}/v1"
wire_api = "responses"
requires_openai_auth = false
supports_websockets = false
`, { mode: 0o600 });
const get = field => tmux('display-message', '-p', '-t', 'main', '#{' + field + '}');
function codexRunning() {
  const pending = [Number(get('pane_pid'))];
  const seen = new Set();
  while (pending.length && seen.size < 256) {
    const pid = pending.pop(); if (seen.has(pid)) continue; seen.add(pid);
    try {
      if (path.basename(fs.readlinkSync('/proc/' + pid + '/exe')) === 'codex') return true;
      const children = fs.readFileSync('/proc/' + pid + '/task/' + pid + '/children', 'utf8').trim();
      if (children) pending.push(...children.split(/\s+/).map(Number));
    } catch { /* A child may have just exited. */ }
  }
  return false;
}
try {
  if (phase === 'prepare') {
    tmux('new-session', '-d', '-s', 'main', '-x', '120', '-y', '40', '-c', '/workspace',
      'codex --dangerously-bypass-approvals-and-sandbox');
    await until(() => tmux('capture-pane', '-p', '-t', 'main').includes('Ask Codex'), 'Codex input readiness');
    assert.ok(get('socket_path').startsWith(tmp + '/'), 'only the isolated test pane receives seed input');
    tmux('send-keys', '-t', 'main', '-l', 'Local restoration test. Do not execute tools.');
    tmux('send-keys', '-t', 'main', 'C-m');
    await pause(600); tmux('send-keys', '-t', 'main', 'C-m');
    await until(() => tmux('capture-pane', '-p', '-t', 'main').includes(text) && get('@ct_restore_thread_id'), 'saved Codex reply and lifecycle mapping');
    const saved = snapshot();
    const id = saved.windows[0].panes[0].session;
    assert.ok(id, 'a conversation is mapped');
    const walk = dir => fs.readdirSync(dir, { withFileTypes: true }).flatMap(entry =>
      entry.isDirectory() ? walk(path.join(dir, entry.name)) : [path.join(dir, entry.name)]);
    const transcripts = walk(path.join(home, 'sessions')).filter(file => path.basename(file).includes(id));
    assert.equal(transcripts.length, 1, 'mapped conversation ID identifies a durable Codex transcript');
    const metadata = JSON.parse(fs.readFileSync(transcripts[0], 'utf8').split('\n')[0]);
    assert.equal(metadata.payload.id, id);
    checkpoint(true);
    fs.writeFileSync(path.join(home, 'expected-thread.json'), JSON.stringify({ id }), { mode: 0o600 });
    console.log('REAL_CODEX_CONVERSATION_CHECKPOINT_OK');
  } else {
    const expected = JSON.parse(fs.readFileSync(path.join(home, 'expected-thread.json'), 'utf8'));
    assert.equal(restore(), true);
    // No input is sent during verification: the restored conversation must wait.
    await until(() => codexRunning() && get('@ct_restore_thread_id') === expected.id &&
      tmux('capture-pane', '-p', '-t', 'main').includes(text), 'exact real Codex conversation resume');
    await pause(1000);
    assert.equal(requests, 0, 'automatic restoration must not submit a prompt or run inference');
    assert.equal(snapshot().windows[0].panes[0].session, expected.id);
    console.log('REAL_CODEX_EXACT_CONVERSATION_RESTORE_WITHOUT_PROMPT_OK');
  }
} catch (error) {
  try { console.error(JSON.stringify({ phase, codexRunning: codexRunning(), mapped: Boolean(get('@ct_restore_thread_id')),
    priorReplyVisible: tmux('capture-pane', '-p', '-t', 'main').includes(text) })); } catch {}
  throw error;
} finally { server.closeAllConnections(); server.close(); }
