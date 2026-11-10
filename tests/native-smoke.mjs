// Run inside the pinned image under the production container sandbox.
import assert from 'node:assert/strict';
import { spawn, execFileSync } from 'node:child_process';
import { createRequire } from 'node:module';
import { mkdtemp, rm } from 'node:fs/promises';
import { once } from 'node:events';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import http from 'node:http';
const root = process.env.TERMINAL_ROOT || '/opt/codex-terminal';
const { hashPassword } = await import(pathToFileURL(join(root, 'password.mjs')));
const WebSocket = createRequire(join(root, 'start.mjs'))('ws');
const base = 'http://127.0.0.1:' + (process.env.SMOKE_PORT || '7681');
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
async function until(check, label) {
  for (let i = 0; i < 120; i++) { if (await check()) return; await pause(250); }
  throw Error('Timed out: ' + label);
}
const app = http.createServer((req, res) => res.end(req.url));
await new Promise(resolve => app.listen(3000, '127.0.0.1', resolve));
try {
  for (const authenticated of [false, true]) {
    const config = await mkdtemp('/tmp/remobi-smoke-');
    const env = { ...process.env, TMUX_TMPDIR: config, CODEX_HOME: config, APP_PORTS: '3000',
      AUTH_PASSWORD: '', AUTH_USERNAME: authenticated ? 'smoke' : '',
      AUTH_PASSWORD_HASH: authenticated ? await hashPassword('smoke-password') : '' };
    delete env.TMUX; delete env.TMUX_PANE;
    const tmux = (...args) => execFileSync('tmux', args, { env, encoding: 'utf8' }).trim();
    const child = spawn(process.execPath, [join(root, 'start.mjs')], { env, stdio: 'inherit' });
    const exit = once(child, 'exit');
    const clients = new Set();
    let cookie = '';
    const request = (path, options = {}) => fetch(base + path, { redirect: 'manual', ...options,
      headers: { cookie, origin: base, ...options.headers } });
    async function connect(external = false) {
      let output = '', snapshot = false;
      const ws = new WebSocket(base.replace('http:', 'ws:') + '/ws', {
        headers: { cookie, origin: external ? 'https://terminal.example.com' : base,
          ...(external ? { host: 'terminal.example.com', 'x-forwarded-proto': 'https' } : {}) } });
      clients.add(ws);
      ws.on('message', raw => {
        const msg = JSON.parse(raw);
        if (msg.type === 'snapshot') snapshot = true;
        if (['snapshot', 'output'].includes(msg.type)) output += msg.data;
      });
      await Promise.race([once(ws, 'open'), pause(10000).then(() => { throw Error('WebSocket open timeout'); })]);
      await until(() => snapshot, 'PTY snapshot');
      return { ws, output: () => output, send: data => ws.send(JSON.stringify(data)) };
    }
    try {
      await until(async () => {
        assert.equal(child.exitCode, null, 'launcher exited during startup');
        try {
          if (authenticated && !cookie) {
            assert.equal((await request('/')).status, 303);
            const login = await request('/login', { method: 'POST', body: new URLSearchParams({ username: 'smoke', password: 'smoke-password' }) });
            assert.equal(login.status, 303);
            cookie = login.headers.get('set-cookie').split(';')[0];
          }
          return (await request('/')).ok;
        } catch { return false; }
      }, 'Remobi startup');
      assert.match(await (await request('/')).text(), /<html/i);
      const terminal = await connect();
      terminal.send({ type: 'resize', cols: 100, rows: 30 });
      // Split marker prevents input echo from satisfying the output assertion.
      terminal.send({ type: 'input', data: "printf 'PTY_%s:%s\\n' OK \"$PWD\"\r" });
      await until(() => terminal.output().includes('PTY_OK:/workspace'), 'PTY command');
      await until(() => tmux('display-message', '-p', '-t', 'main', '#{pane_width}') === '100', 'PTY resize');
      const closed = once(terminal.ws, 'close'); terminal.ws.close(); await closed;
      const reconnected = await connect(true);
      await until(() => reconnected.output().includes('PTY_OK:/workspace'), 'reconnect snapshot');
      const shared = await connect();
      reconnected.send({ type: 'input', data: "printf 'SHARED_%s\\n' OK\r" });
      await until(() => shared.output().includes('SHARED_OK'), 'shared tmux session');
      for (const origin of ['https://foreign.example', 'null', undefined]) {
        const foreign = new WebSocket(base.replace('http:', 'ws:') + '/ws', { headers: { cookie, ...(origin ? { origin } : {}) } });
        clients.add(foreign);
        const rejected = new Promise(resolve => { foreign.once('unexpected-response', (_req, res) => { res.resume(); foreign.terminate(); resolve(res.statusCode); }); foreign.once('open', () => resolve(101)); foreign.on('error', () => {}); });
        assert.equal(await Promise.race([rejected, pause(5000).then(() => 0)]), 401, 'foreign/missing origin rejected');
      }
      assert.equal(await (await request('/apps/3000/ready?yes=1')).text(), '/apps/3000/ready?yes=1');
      assert.equal((await request('/apps/3001/')).status, 404);
      child.kill('SIGTERM');
      assert.deepEqual(await Promise.race([exit, pause(8000).then(() => { throw Error('Shutdown timeout'); })]), [0, null]);
      await until(() => [...clients].every(ws => ws.readyState === WebSocket.CLOSED), 'socket shutdown');
      console.log(`Native Remobi smoke passed (auth=${authenticated})`);
    } finally {
      for (const ws of clients) ws.terminate();
      if (child.exitCode === null) { child.kill('SIGKILL'); await exit; }
      try { tmux('kill-server'); } catch { /* already stopped */ }
      await rm(config, { recursive: true, force: true });
    }
  }
} finally { app.closeAllConnections(); app.close(); }
