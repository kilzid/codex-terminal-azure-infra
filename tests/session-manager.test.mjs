import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
const root = fs.mkdtempSync(path.join(os.tmpdir(), 'terminal-restore-test-'));
process.env.TMUX_TMPDIR = root;
process.env.CODEX_HOME = root;
delete process.env.TMUX;
const manager = await import('../runtime/session-manager.mjs');
const tmux = (...args) => execFileSync('tmux', args, { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] }).trim();
const get = (target, field) => tmux('display-message', '-p', '-t', target, '#{' + field + '}');
const state = path.join(root, 'terminal-restore');
const sleep = ms => new Promise(r => setTimeout(r, ms));
test('checkpoint, lifecycle, restoration and rollback', async () => {
  try {
    const pane = tmux('new-session', '-d', '-s', 'main', '-n', 'first window', '-c', root, '-P', '-F', '#{pane_id}', '/bin/bash');
    tmux('resize-window', '-t', 'main:0', '-x', '140', '-y', '40');
    tmux('split-window', '-d', '-h', '-t', pane, '-c', '/tmp', '/bin/bash');
    tmux('new-window', '-d', '-t', 'main:4', '-n', 'fourth', '-c', root, '/bin/bash');
    tmux('select-window', '-t', 'main:0');
    tmux('select-pane', '-t', pane);
    process.env.TMUX_PANE = pane;
    const pid = Number(get(pane, 'pane_pid'));
    manager.hook({ hook_event_name: 'SessionStart', session_id: 'session_exact' }, pid);
    assert.equal(get(pane, '@ct_restore_thread_id'), 'session_exact');
    // Codex/terminal integrations can overwrite their own pane metadata independently.
    tmux('set-option', '-p', '-t', pane, '@codex_session', 'runtime_only_id');
    tmux('set-option', '-p', '-t', pane, '@codex_pid', '0');
    manager.hook({ hook_event_name: 'SessionEnd', session_id: 'old_session' });
    assert.equal(get(pane, '@ct_restore_thread_id'), 'session_exact');
    manager.hook({ hook_event_name: 'SessionEnd', session_id: 'session_exact' });
    assert.equal(get(pane, '@ct_restore_thread_id'), '');
    assert.equal(get(pane, '@codex_session'), 'runtime_only_id', 'ending a conversation leaves native metadata alone');
    manager.hook({ hook_event_name: 'SessionStart', session_id: 'session_exact' }, pid);
    const transcripts = path.join(root, 'sessions'); fs.mkdirSync(transcripts);
    const transcript = path.join(transcripts, 'rollout-conversation_durable.jsonl');
    assert.equal(manager.conversationId('runtime_id', transcript), null, 'pending transcript is never guessed');
    fs.writeFileSync(transcript, JSON.stringify({ type: 'session_meta', payload: { id: 'conversation_durable' } }) + '\n');
    manager.hook({ hook_event_name: 'SessionStart', session_id: 'runtime_id', transcript_path: transcript }, pid);
    assert.equal(manager.snapshot().windows[0].panes[0].session, 'conversation_durable');
    assert.equal(manager.conversationId('runtime_id', '/etc/passwd'), null, 'event paths stay inside saved sessions');
    manager.hook({ hook_event_name: 'SessionEnd', session_id: 'runtime_id' });
    assert.equal(get(pane, '@ct_restore_thread_id'), '', 'end event clears its resolved conversation');
    manager.hook({ hook_event_name: 'SessionStart', session_id: 'session_exact' }, pid);
    manager.checkpoint();
    const file = path.join(state, 'checkpoint.json');
    const saved = JSON.parse(fs.readFileSync(file));
    assert.equal(saved.windows[0].panes[0].session, 'session_exact');
    assert.equal(saved.windows[0].panes[1].cwd, '/tmp');
    assert.equal(fs.statSync(file).mode & 0o777, 0o600);
    assert.equal(fs.statSync(state).mode & 0o777, 0o700);
    assert.throws(() => manager.validate({ ...saved, version: 2 }));
    const invalid = structuredClone(saved); invalid.windows[0].panes[0].session = 'id; touch /tmp/bad';
    assert.throws(() => manager.validate(invalid));
    manager.checkpoint(true);
    manager.hook({ hook_event_name: 'SessionEnd', session_id: 'session_exact' });
    assert.equal(get(pane, '@ct_restore_thread_id'), 'session_exact', 'frozen lifecycle ignored');
    assert.equal(fs.readFileSync(file, 'utf8'), JSON.stringify(saved));
    // Simulate container replacement: tmux and ephemeral marker disappear, volumes stay.
    tmux('kill-server'); fs.rmSync(path.join(root, 'codex-terminal-frozen'));
    const restored = structuredClone(saved);
    restored.windows[0].panes[1].session = 'session_exact'; // deduplicate
    restored.windows[1].panes[0].cwd = '/directory-that-does-not-exist';
    manager.atomic(path.join(state, 'deployment.json'), restored);
    const bin = path.join(root, 'bin'); fs.mkdirSync(bin);
    fs.writeFileSync(path.join(bin, 'codex'), '#!/bin/sh\nprintf "%s\\n" "$@" >> "$CODEX_HOME/resumes"\nexit 1\n', { mode: 0o700 });
    process.env.PATH = bin + ':' + process.env.PATH;
    assert.equal(manager.restore(), true);
    await sleep(300);
    assert.equal(tmux('list-windows', '-t', 'main', '-F', '#{window_index}:#{window_name}'), '0:first window\n4:fourth');
    assert.equal(get('main:0', 'window_panes'), '2');
    assert.equal(get('main:0', 'window_layout'), saved.windows[0].layout);
    // Hosted CI has no /workspace; tmux falls back to HOME there. Production always mounts it.
    assert.equal(get('main:4', 'pane_current_path'), fs.existsSync('/workspace') ? '/workspace' : os.homedir());
    assert.equal(get('main', 'window_index'), '0');
    assert.match(tmux('capture-pane', '-p', '-t', 'main:0.0'), /shell\s+is ready/);
    assert.deepEqual(fs.readFileSync(path.join(root, 'resumes'), 'utf8').trim().split('\n'), manager.resumeArgs('session_exact'));
    assert.equal(manager.restore(), false, 'repeated startup is inert');
    assert.equal(fs.readFileSync(path.join(root, 'resumes'), 'utf8').trim().split('\n').length, 3);
    // Failed replacement checkpoints cannot replace deployment snapshot used by rollback.
    manager.checkpoint(true);
    assert.deepEqual(JSON.parse(fs.readFileSync(path.join(state, 'deployment.json'))), restored);
    tmux('kill-server'); fs.rmSync(path.join(root, 'codex-terminal-frozen'));
    assert.equal(manager.restore(), true);
    await sleep(300);
    assert.deepEqual(fs.readFileSync(path.join(root, 'resumes'), 'utf8').trim().split('\n'), [...manager.resumeArgs('session_exact'), ...manager.resumeArgs('session_exact')]);
    manager.hook({ hook_event_name: 'SessionEnd', session_id: 'session_exact' });
    tmux('kill-server');
    fs.writeFileSync(path.join(state, 'deployment.json'), 'invalid');
    fs.writeFileSync(file, '{"version":999}');
    assert.equal(manager.restore(), false);
  } finally {
    try { tmux('kill-server'); } catch {}
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('supervisor freezes before forwarding termination signals', async () => {
  const { spawn } = await import('node:child_process');
  const { once } = await import('node:events');
  for (const signal of ['SIGTERM', 'SIGINT']) {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'terminal-supervisor-'));
    const env = { ...process.env, TMUX_TMPDIR: dir, CODEX_HOME: dir };
    const run = (...args) => execFileSync('tmux', args, { env, stdio: ['ignore', 'pipe', 'ignore'] });
    let child;
    try {
      run('new-session', '-d', '-s', 'main', '-c', '/tmp', '/bin/bash');
      const launcher = path.join(dir, 'launcher.mjs');
      fs.writeFileSync(launcher, `import fs from 'node:fs';
        for (const signal of ['SIGTERM', 'SIGINT']) process.on(signal, () => {
          const value = JSON.parse(fs.readFileSync(process.env.CODEX_HOME + '/terminal-restore/checkpoint.json'));
          const frozen = fs.existsSync(process.env.TMUX_TMPDIR + '/codex-terminal-frozen');
          fs.writeFileSync(process.env.CODEX_HOME + '/result', signal + ':' + value.version + ':' + frozen);
          process.exit(0);
        });
        setInterval(() => {}, 1000); console.log('ready');`);
      const code = `import { supervise } from ${JSON.stringify(new URL('../runtime/session-manager.mjs', import.meta.url).href)}; supervise(${JSON.stringify(launcher)});`;
      child = spawn(process.execPath, ['--input-type=module', '-e', code], { env, stdio: ['ignore', 'pipe', 'pipe'] });
      await once(child.stdout, 'data');
      const exited = once(child, 'exit');
      child.kill(signal);
      assert.deepEqual(await exited, [0, null]);
      assert.equal(fs.readFileSync(path.join(dir, 'result'), 'utf8'), signal + ':1:true');
      assert.equal(fs.existsSync(path.join(dir, 'terminal-restore/deployment.json')), false, 'ordinary stops do not pin an old deployment');
    } finally {
      if (child?.exitCode === null) child.kill('SIGKILL');
      try { run('kill-server'); } catch {}
      fs.rmSync(dir, { recursive: true, force: true });
    }
  }
});
