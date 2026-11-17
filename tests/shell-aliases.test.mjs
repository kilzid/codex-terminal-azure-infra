import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { prepareShell } from '../runtime/session-manager.mjs';

test('literal ?? alias survives home replacement and passes arguments to codex --yolo', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'persistent-alias-test-'));
  try {
    const config = path.join(root, 'persistent config');
    const bin = path.join(root, 'bin'); fs.mkdirSync(bin);
    fs.writeFileSync(path.join(bin, 'codex'), '#!/bin/sh\nprintf "%s\\n" "$@"\n', { mode: 0o700 });
    for (const name of ['first-home', 'replacement-home']) {
      const home = path.join(root, name); fs.mkdirSync(home);
      fs.writeFileSync(path.join(home, '.bashrc'), '# Existing shell settings\nexport PERSONAL_SETTING=kept\n');
      fs.writeFileSync(path.join(home, 'zz'), 'two-character filename must not capture ??');
      const aliases = prepareShell(home, config);
      const before = fs.readFileSync(path.join(home, '.bashrc'), 'utf8');
      prepareShell(home, config);
      assert.equal(fs.readFileSync(path.join(home, '.bashrc'), 'utf8'), before, 'startup is idempotent');
      assert.ok(before.startsWith('# Existing shell settings\nexport PERSONAL_SETTING=kept\n'));
      assert.equal(fs.statSync(aliases).mode & 0o777, 0o600);
      const output = execFileSync('bash', ['--noprofile', '--rcfile', path.join(home, '.bashrc'), '-ic', '?? hello'],
        { cwd: home, env: { ...process.env, HOME: home, CODEX_HOME: config, PATH: bin + ':' + process.env.PATH },
          encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] });
      assert.equal(output, '--yolo\nhello\n');
      fs.rmSync(home, { recursive: true, force: true });
    }
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test('startup preserves edited personal aliases and quotes persistent paths', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'personal-alias-test-'));
  try {
    const home = path.join(root, 'home'); fs.mkdirSync(home);
    const config = path.join(root, "owner's config");
    const aliases = prepareShell(home, config);
    const personal = "alias '??'='printf custom'\nalias project='pwd'\n";
    fs.writeFileSync(aliases, personal);
    prepareShell(home, config);
    assert.equal(fs.readFileSync(aliases, 'utf8'), personal);
    const output = execFileSync('bash', ['--noprofile', '--rcfile', path.join(home, '.bashrc'), '-ic', '??'],
      { env: { ...process.env, HOME: home }, encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] });
    assert.equal(output, 'custom');
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});
