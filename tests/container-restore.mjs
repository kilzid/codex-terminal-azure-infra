// Two invocations in separate production-sandboxed containers sharing only data volumes.
import fs from 'node:fs';
import path from 'node:path';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { snapshot, checkpoint, hook, restore, resumeArgs } from '/opt/codex-session.mjs';
const config = process.env.CODEX_HOME || '/home/node/.codex';
const tmux = (...args) => execFileSync('tmux', args, { encoding: 'utf8' }).trim();
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
if (process.argv[2] === 'prepare') {
  const pane = tmux('new-session', '-d', '-s', 'main', '-n', 'saved window', '-c', '/workspace', '-P', '-F', '#{pane_id}', '/bin/bash');
  tmux('split-window', '-h', '-d', '-t', pane, '-c', config, '/bin/bash');
  tmux('new-window', '-d', '-t', 'main:3', '-n', 'third', '-c', '/workspace', '/bin/bash');
  tmux('select-window', '-t', 'main:0'); tmux('select-pane', '-t', pane);
  process.env.TMUX_PANE = pane;
  hook({ hook_event_name: 'SessionStart', session_id: 'container_conversation' }, Number(tmux('display-message', '-p', '-t', pane, '#{pane_pid}')));
  checkpoint(true);
  fs.writeFileSync(path.join(config, 'expected.json'), JSON.stringify(snapshot()), { mode: 0o600 });
  const bin = path.join(config, 'restore-test-bin'); fs.mkdirSync(bin);
  fs.writeFileSync(path.join(bin, 'codex'), '#!/bin/sh\nprintf "%s\\n" "$@" >> "$HOME/.codex/restore-argv"\nexit 1\n', { mode: 0o700 });
} else {
  process.env.PATH = path.join(config, 'restore-test-bin') + ':' + process.env.PATH;
  assert.equal(restore(), true);
  await pause(1000);
  const expected = JSON.parse(fs.readFileSync(path.join(config, 'expected.json')));
  const actual = snapshot();
  assert.equal(actual.selected, expected.selected);
  assert.deepEqual(actual.windows.map(w => [w.index, w.name, w.selected, w.panes.map(p => p.cwd)]),
    expected.windows.map(w => [w.index, w.name, w.selected, w.panes.map(p => p.cwd)]));
  assert.deepEqual(fs.readFileSync(path.join(config, 'restore-argv'), 'utf8').trim().split('\n'), resumeArgs('container_conversation'));
  assert.equal(restore(), false);
  console.log('CONTAINER_REPLACEMENT_RESTORE_OK');
}
