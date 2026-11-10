// Infrastructure session supervisor. Never serializes terminal output or shell commands.
import fs from 'node:fs';
import path from 'node:path';
import { homedir } from 'node:os';
import { execFileSync, spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
const state = path.join(process.env.CODEX_HOME || path.join(homedir(), '.codex'), 'terminal-restore');
const frozen = path.join(process.env.TMUX_TMPDIR || '/tmp', 'codex-terminal-frozen');
let recoveryFailed = false;
const idPattern = /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/;
const panePattern = /^%\d+$/;
const tmux = (...args) => execFileSync('tmux', args, { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'], timeout: 5000 }).replace(/\n$/, '');
export function atomic(file, value) {
  fs.mkdirSync(path.dirname(file), { recursive: true, mode: 0o700 });
  const tmp = file + '.' + process.pid + '.tmp';
  const fd = fs.openSync(tmp, 'wx', 0o600);
  try {
    fs.writeFileSync(fd, JSON.stringify(value)); fs.fsyncSync(fd);
    fs.closeSync(fd); fs.renameSync(tmp, file);
    const dir = fs.openSync(path.dirname(file), 'r');
    try { fs.fsyncSync(dir); } finally { fs.closeSync(dir); }
  } catch (e) { try { fs.closeSync(fd); } catch {} throw e; }
  finally { fs.rmSync(tmp, { force: true }); }
}
export function validate(v) {
  const number = x => Number.isSafeInteger(x) && x >= 0 && x < 100000;
  const text = x => typeof x === 'string' && x.length <= 4096 && !/[\x00-\x1f\x7f]/.test(x);
  if (!v || v.version !== 1 || !Array.isArray(v.windows) || !v.windows.length || v.windows.length > 100 ||
      !number(v.selected) || !v.windows.some(w => w.index === v.selected)) throw Error('invalid checkpoint');
  const indices = new Set();
  for (const w of v.windows) {
    if (!number(w.index) || indices.has(w.index) || !text(w.name) || !w.name ||
        typeof w.layout !== 'string' || !/^[0-9a-f]{4},[0-9x,{}\[\]]+$/.test(w.layout) ||
        !Array.isArray(w.panes) || !w.panes.length || w.panes.length > 100 ||
        !number(w.selected) || w.selected >= w.panes.length || !number(w.zoomed) || w.zoomed > 1) throw Error('invalid window');
    indices.add(w.index);
    for (const p of w.panes) if (!text(p.cwd) || !path.isAbsolute(p.cwd) ||
      (p.session !== null && (typeof p.session !== 'string' || !idPattern.test(p.session)))) throw Error('invalid pane');
  }
  return v;
}
function read(file) { try { return validate(JSON.parse(fs.readFileSync(path.join(state, file), 'utf8'))); } catch { return null; } }
function parent(pid) {
  try { return Number(fs.readFileSync(`/proc/${pid}/stat`, 'utf8').split(') ').at(-1).split(' ')[1]); } catch { return 0; }
}
function descendant(pid, ancestor) {
  for (let i = 0; pid > 1 && i < 100; i++, pid = parent(pid)) if (pid === ancestor) return true;
  return false;
}
function codexPid() {
  for (let pid = process.ppid, i = 0; pid > 1 && i < 100; i++, pid = parent(pid)) {
    try { if (path.basename(fs.readlinkSync(`/proc/${pid}/exe`)) === 'codex') return pid; } catch {}
  }
  return 0;
}
export function hook(event, pid = codexPid()) {
  const pane = process.env.TMUX_PANE;
  if (!panePattern.test(pane || '') || !idPattern.test(event.session_id || '') || fs.existsSync(frozen)) return;
  if (tmux('display-message', '-p', '-t', pane, '#{session_name}') !== 'main') return;
  if (event.hook_event_name === 'SessionStart' && pid && descendant(pid, Number(tmux('display-message', '-p', '-t', pane, '#{pane_pid}')))) {
    tmux('set-option', '-p', '-t', pane, '@codex_session', event.session_id);
    tmux('set-option', '-p', '-t', pane, '@codex_pid', String(pid));
  } else if (event.hook_event_name === 'SessionEnd' && tmux('display-message', '-p', '-t', pane, '#{@codex_session}') === event.session_id) {
    tmux('set-option', '-pu', '-t', pane, '@codex_session');
    tmux('set-option', '-pu', '-t', pane, '@codex_pid');
  }
}
export function snapshot() {
  const get = (target, field) => tmux('display-message', '-p', '-t', target, '#{' + field + '}');
  const windows = tmux('list-windows', '-t', 'main', '-F', '#{window_id}').split('\n').map(wid => {
    const panes = tmux('list-panes', '-t', wid, '-F', '#{pane_id}').split('\n').map(pane => {
      const session = get(pane, '@codex_session');
      return { cwd: get(pane, 'pane_current_path'),
        session: idPattern.test(session) && descendant(Number(get(pane, '@codex_pid')), Number(get(pane, 'pane_pid'))) ? session : null,
        active: get(pane, 'pane_active') === '1' };
    });
    return { index: Number(get(wid, 'window_index')), name: get(wid, 'window_name'),
      layout: get(wid, 'window_layout'), zoomed: Number(get(wid, 'window_zoomed_flag')),
      selected: panes.findIndex(p => p.active), panes: panes.map(({ active, ...p }) => p),
      active: get(wid, 'window_active') === '1' };
  });
  return validate({ version: 1, selected: windows.find(w => w.active).index,
    windows: windows.map(({ active, ...w }) => w) });
}
export function checkpoint(freeze = false) {
  if (fs.existsSync(frozen) || recoveryFailed) return;
  let value;
  try { value = snapshot(); } catch {
    try { tmux('has-session', '-t', '=main'); } catch { return; }
    throw Error('checkpoint unavailable');
  }
  atomic(path.join(state, 'checkpoint.json'), value);
  if (freeze) {
    if (!read('deployment.json')) atomic(path.join(state, 'deployment.json'), value);
    fs.writeFileSync(frozen, '', { mode: 0o600 });
  }
}
const quote = s => "'" + s.replaceAll("'", "'\\''") + "'";
export function resumeArgs(id) {
  if (!idPattern.test(id)) throw Error('invalid session');
  return ['resume', '--dangerously-bypass-approvals-and-sandbox', id];
}
export function resumeCommand(id) {
  return ['codex', ...resumeArgs(id)].map(quote).join(' ') +
    " || printf '\\nConversation could not be resumed; shell is ready.\\n'; exec /bin/bash";
}
export function restore() {
  try { tmux('has-session', '-t', '=main'); return false; } catch {}
  const value = read('deployment.json') || read('checkpoint.json');
  if (!value) return false;
  const seen = new Set();
  try {
    for (const [i, w] of value.windows.entries()) {
      const dirs = w.panes.map(p => { try { if (fs.statSync(p.cwd).isDirectory()) return p.cwd; } catch {} return '/workspace'; });
      const target = 'main:' + w.index;
      const first = i === 0
        ? tmux('new-session', '-d', '-s', 'main', '-c', dirs[0], '-P', '-F', '#{pane_id}', '/bin/bash')
        : tmux('new-window', '-d', '-t', target, '-c', dirs[0], '-P', '-F', '#{pane_id}', '/bin/bash');
      if (i === 0 && Number(tmux('display-message', '-p', '-t', first, '#{window_index}')) !== w.index)
        tmux('move-window', '-s', 'main:', '-t', target);
      tmux('set-option', '-w', '-t', target, 'automatic-rename', 'off');
      tmux('rename-window', '-t', target, '--', w.name);
      const panes = [first];
      tmux('resize-window', '-t', target, '-x', '500', '-y', '500');
      for (let n = 1; n < w.panes.length; n++) {
        panes.push(tmux('split-window', '-d', '-t', target, '-c', dirs[n], '-P', '-F', '#{pane_id}', '/bin/bash'));
        tmux('select-layout', '-t', target, 'tiled');
      }
      const [, width, height] = w.layout.match(/^[0-9a-f]{4},(\d+)x(\d+),/);
      tmux('resize-window', '-t', target, '-x', width, '-y', height);
      tmux('select-layout', '-t', target, w.layout);
      for (let n = 0; n < panes.length; n++) {
        const id = w.panes[n].session;
        if (id && !seen.has(id)) {
          seen.add(id);
          tmux('respawn-pane', '-k', '-t', panes[n], '-c', dirs[n], resumeCommand(id));
        }
      }
      tmux('select-pane', '-t', panes[w.selected]);
      if (w.zoomed) tmux('resize-pane', '-Z', '-t', panes[w.selected]);
    }
    tmux('select-window', '-t', 'main:' + value.selected);
    return true;
  } catch {
    try { tmux('kill-session', '-t', '=main'); } catch {}
    recoveryFailed = true;
    console.error('Terminal restoration failed; saved checkpoint retained.');
    return false;
  }
}
function release() { fs.rmSync(path.join(state, 'deployment.json'), { force: true }); }
export function supervise(launcher = '/opt/codex-terminal/start.mjs') {
  fs.rmSync(frozen, { force: true });
  restore();
  const child = spawn(process.execPath, [launcher], { stdio: 'inherit' });
  let stopping = false;
  const save = () => { try { checkpoint(); } catch {} };
  const timer = setInterval(save, 5000);
  let deadline;
  const stop = signal => {
    if (stopping) return;
    stopping = true;
    clearInterval(timer);
    try { checkpoint(); fs.writeFileSync(frozen, '', { mode: 0o600 }); } catch { console.error('Terminal checkpoint unavailable.'); }
    child.kill(signal);
    deadline = setTimeout(() => child.kill('SIGKILL'), 29000);
  };
  process.on('SIGTERM', () => stop('SIGTERM'));
  process.on('SIGINT', () => stop('SIGINT'));
  child.on('error', () => { clearInterval(timer); console.error('Terminal launcher failed.'); process.exitCode = 1; });
  child.on('exit', code => { clearInterval(timer); clearTimeout(deadline); if (!stopping) save(); process.exitCode = code ?? (stopping ? 0 : 1); });
}
if (process.argv[1] === '-' || (process.argv[1] && fileURLToPath(import.meta.url) === path.resolve(process.argv[1]))) {
  try {
    switch (process.argv[2]) {
      case 'hook': hook(JSON.parse(fs.readFileSync(0, 'utf8'))); break;
      case 'freeze': checkpoint(true); break;
      case 'release': release(); break;
      case 'restore': restore(); break;
      default: supervise();
    }
  } catch { console.error('Terminal session operation failed.'); process.exitCode = 1; }
}
