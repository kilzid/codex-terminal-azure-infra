// Run inside the built image: node /tmp/smoke.mjs
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createRequire } from 'node:module';
import { mkdtemp, rm } from 'node:fs/promises';
import { once } from 'node:events';
import http from 'node:http';
import { hashPassword } from '/opt/codex-terminal/password.mjs';
const WebSocket = createRequire('/opt/codex-terminal/package.json')('ws');
const base = 'http://127.0.0.1:7681';
const app = http.createServer((req, res) => res.end(req.url));
await new Promise(resolve => app.listen(3000, '127.0.0.1', resolve));
try {
  for (const authenticated of [false, true]) {
    const config = await mkdtemp('/tmp/termbeam-smoke-');
    const child = spawn(process.execPath, ['/opt/codex-terminal/start.mjs'], {
      env: { ...process.env, CODEX_HOME: config, APP_PORTS: '3000', AUTH_USERNAME: authenticated ? 'smoke' : '', AUTH_PASSWORD_HASH: authenticated ? await hashPassword('smoke-password') : '' },
      stdio: 'inherit',
    });
    const exit = once(child, 'exit');
    let cookie = '';
    const request = (path, options = {}) => fetch(base + path, { redirect: 'manual', ...options, headers: { cookie, origin: base, ...options.headers } });
    const api = async (path, method, body) => {
      const response = await request(path, { method, headers: { 'content-type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) });
      assert.ok(response.ok, `${path}: ${response.status} ${await response.clone().text()}`);
      return response.status === 204 ? null : response.json();
    };
    try {
      // Wait for gateway and native PTY initialization, including slower QEMU runs.
      let ready = false;
      for (let attempt = 0; attempt < 120; attempt++) {
        assert.equal(child.exitCode, null, 'launcher exited during startup');
        try {
          if (authenticated && !cookie) {
            const login = await request('/login', { method: 'POST', body: new URLSearchParams({ username: 'smoke', password: 'smoke-password' }) });
            assert.equal(login.status, 303);
            cookie = login.headers.get('set-cookie').split(';')[0];
          }
          const response = await request('/api/sessions');
          if (response.ok && (await response.json()).length === 1) { ready = true; break; }
        } catch { /* startup may still be binding */ }
        await new Promise(resolve => setTimeout(resolve, 250));
      }
      assert.ok(ready, 'default PTY did not start');
      assert.match(await (await request('/')).text(), /<html/i);
      const [first] = await api('/api/sessions');
      assert.equal(first.cwd, '/workspace');
      assert.equal(first.shell, '/bin/bash');
      const second = await api('/api/sessions', 'POST', { name: 'second', shell: '/bin/bash', cwd: '/workspace' });
      assert.equal((await api('/api/sessions')).length, 2);
      await api(`/api/sessions/${second.id}`, 'PATCH', { name: 'renamed' });
      assert.equal((await api('/api/sessions')).find(s => s.id === second.id).name, 'renamed');
      async function connect() {
        const ws = new WebSocket(base.replace('http:', 'ws:') + '/ws', { headers: { cookie, origin: base } });
        await once(ws, 'open');
        let output = '';
        ws.on('message', raw => { const msg = JSON.parse(raw); if (['output', 'replay'].includes(msg.type)) output += msg.data; });
        const attached = new Promise(resolve => ws.on('message', raw => { if (JSON.parse(raw).type === 'attached') resolve(); }));
        ws.send(JSON.stringify({ type: 'attach', sessionId: first.id }));
        await attached;
        ws.send(JSON.stringify({ type: 'resize', cols: 100, rows: 30 }));
        return { ws, output: () => output };
      }
      const terminal = await connect();
      // Split marker prevents shell input echo from satisfying the output check.
      terminal.ws.send(JSON.stringify({ type: 'input', data: "printf 'PTY_%s:%s\\n' OK \"$PWD\"\r" }));
      for (let i = 0; i < 100 && !terminal.output().includes('PTY_OK:/workspace'); i++) await new Promise(resolve => setTimeout(resolve, 50));
      assert.match(terminal.output(), /PTY_OK:\/workspace/);
      terminal.ws.close(); await once(terminal.ws, 'close');
      const reconnected = await connect();
      for (let i = 0; i < 100 && !reconnected.output().includes('PTY_OK:/workspace'); i++) await new Promise(resolve => setTimeout(resolve, 50));
      assert.match(reconnected.output(), /PTY_OK:\/workspace/);
      reconnected.ws.close(); await once(reconnected.ws, 'close');
      assert.equal(await (await request('/apps/3000/ready?yes=1')).text(), '/apps/3000/ready?yes=1');
      assert.equal((await request('/apps/3001/')).status, 404);
      assert.equal((await request('/preview/3000/')).status, 404);
      assert.equal((await request('/api/update', { method: 'POST' })).status, 403);
      // Browser cross-site reads carry Fetch Metadata. Origin alone on a safe
      // GET is intentionally permitted by TermBeam for non-browser clients.
      assert.equal((await request('/api/sessions', { headers: { origin: 'https://foreign.example', 'sec-fetch-site': 'cross-site' } })).status, 403);
      assert.equal((await request('/api/sessions', {
        method: 'POST',
        headers: { origin: 'https://foreign.example', 'content-type': 'application/json' },
        body: JSON.stringify({ name: 'must-not-be-created' }),
      })).status, 403);
      assert.equal((await api('/api/sessions')).length, 2);
      await api(`/api/sessions/${second.id}`, 'DELETE');
      assert.equal((await api('/api/sessions')).length, 1);
      child.kill('SIGTERM');
      const result = await Promise.race([exit, new Promise((_, reject) => setTimeout(() => reject(Error('Shutdown timed out')), 8000).unref())]);
      assert.deepEqual(result, [0, null]);
      console.log(`Native TermBeam smoke passed (auth=${authenticated})`);
    } finally {
      if (child.exitCode === null) { child.kill('SIGKILL'); await exit; }
      await rm(config, { recursive: true, force: true });
    }
  }
} finally { app.closeAllConnections(); app.close(); }
