// Infrastructure session supervisor. Never serializes terminal output or shell commands.
import fs from 'node:fs';
import path from 'node:path';
import { homedir } from 'node:os';
import { execFileSync, spawn, spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
const state = path.join(process.env.CODEX_HOME || path.join(homedir(), '.codex'), 'terminal-restore');
const frozen = path.join(process.env.TMUX_TMPDIR || process.env.TMPDIR || '/tmp', 'codex-terminal-frozen');
let recoveryFailed = false;
const herdrBackend = spawnSync('herdr', ['--version'], { stdio: 'ignore', timeout: 5000 }).status === 0;
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
export function conversationId(sessionId, transcriptPath) {
  // Use only the transcript supplied by this lifecycle event, never the latest file.
  if (!transcriptPath) return idPattern.test(sessionId || '') ? sessionId : null;
  let fd;
  try {
    const sessions = fs.realpathSync(path.join(path.dirname(state), 'sessions')) + path.sep;
    const file = fs.realpathSync(transcriptPath);
    if (!file.startsWith(sessions)) return null;
    fd = fs.openSync(file, 'r');
    const buffer = Buffer.alloc(1024 * 1024);
    const size = fs.readSync(fd, buffer, 0, buffer.length, 0);
    const first = buffer.subarray(0, size).toString('utf8').split('\n')[0];
    const metadata = JSON.parse(first);
    const id = metadata.payload?.id;
    return metadata.type === 'session_meta' && idPattern.test(id || '') && path.basename(file).includes(id) ? id : null;
  } catch { return null; }
  finally { if (fd !== undefined) fs.closeSync(fd); }
}
function legacyHook(event, pid = codexPid()) {
  const pane = process.env.TMUX_PANE;
  if (!panePattern.test(pane || '') || !idPattern.test(event.session_id || '') || fs.existsSync(frozen)) return;
  if (tmux('display-message', '-p', '-t', pane, '#{session_name}') !== 'main') return;
  if (event.hook_event_name === 'SessionStart' && pid && descendant(pid, Number(tmux('display-message', '-p', '-t', pane, '#{pane_pid}')))) {
    // The CLI also starts ephemeral sessions in this process. They explicitly
    // supply an empty transcript path and must not replace a durable mapping.
    if (Object.hasOwn(event, 'transcript_path') && !event.transcript_path) return;
    tmux('set-option', '-p', '-t', pane, '@ct_restore_thread_id', conversationId(event.session_id, event.transcript_path) || '');
    tmux('set-option', '-p', '-t', pane, '@ct_restore_hook_id', event.session_id);
    tmux('set-option', '-p', '-t', pane, '@ct_restore_transcript', event.transcript_path || '');
    tmux('set-option', '-p', '-t', pane, '@ct_restore_pid', String(pid));
  } else if (event.hook_event_name === 'SessionEnd' && tmux('display-message', '-p', '-t', pane, '#{@ct_restore_hook_id}') === event.session_id) {
    tmux('set-option', '-pu', '-t', pane, '@ct_restore_thread_id');
    tmux('set-option', '-pu', '-t', pane, '@ct_restore_pid');
    tmux('set-option', '-pu', '-t', pane, '@ct_restore_hook_id');
    tmux('set-option', '-pu', '-t', pane, '@ct_restore_transcript');
  }
}
function legacySnapshot() {
  const get = (target, field) => tmux('display-message', '-p', '-t', target, '#{' + field + '}');
  const windows = tmux('list-windows', '-t', 'main', '-F', '#{window_id}').split('\n').map(wid => {
    const panes = tmux('list-panes', '-t', wid, '-F', '#{pane_id}').split('\n').map(pane => {
      const session = conversationId(get(pane, '@ct_restore_thread_id') || get(pane, '@ct_restore_hook_id'), get(pane, '@ct_restore_transcript'));
      return { cwd: get(pane, 'pane_current_path'),
        session: idPattern.test(session) && descendant(Number(get(pane, '@ct_restore_pid')), Number(get(pane, 'pane_pid'))) ? session : null,
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
function legacyCheckpoint(freeze = false) {
  if (fs.existsSync(frozen) || recoveryFailed) return;
  let value;
  try { value = legacySnapshot(); } catch {
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
  resumeArgs(id);
  return [process.execPath, fileURLToPath(import.meta.url), 'resume', id].map(quote).join(' ') +
    " || printf '\\nConversation could not be resumed; shell is ready.\\n'; exec /bin/bash";
}
export function resume(id) {
  const child = spawn('codex', resumeArgs(id), { stdio: 'inherit' });
  // A resumed TUI may wait for input before emitting SessionStart. Bind only
  // the checkpoint's explicit ID to the process we launch, without input.
  if (child.pid) hook({ hook_event_name: 'SessionStart', session_id: id }, child.pid);
  for (const signal of ['SIGTERM', 'SIGINT']) process.on(signal, () => child.kill(signal));
  child.on('error', () => { console.error('Conversation could not be resumed; shell is ready.'); process.exitCode = 1; });
  child.on('exit', code => {
    try {
      if (herdrBackend) hook({ hook_event_name: 'SessionEnd', session_id: id });
      else {
        const pane = process.env.TMUX_PANE;
        if (panePattern.test(pane || '') && tmux('display-message', '-p', '-t', pane, '#{@ct_restore_pid}') === String(child.pid))
          legacyHook({ hook_event_name: 'SessionEnd', session_id: id });
      }
    } catch {}
    if (code && herdrBackend) console.error('Conversation could not be resumed; shell is ready.');
    process.exitCode = code ?? 1;
  });
}
function legacyRestore() {
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
      // resize-window sets manual sizing; let browser PTY resizes work after attach.
      tmux('set-option', '-w', '-t', target, 'window-size', 'latest');
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
// Herdr owns PTYs and native layout restoration. Keep management checkpoints
// separate from its autosave so a failed deployment cannot replace rollback state.
const codexHome = path.dirname(state);
const herdrRoot = path.join(codexHome, 'herdr');
const herdrSession = path.join(herdrRoot, 'sessions', 'main', 'session.json');
const herdrEnv = () => ({ ...process.env, XDG_CONFIG_HOME: path.join(codexHome, 'terminal-xdg'), HERDR_SESSION: 'main' });
export function prepareHerdrEnvironment() {
  Object.assign(process.env, herdrEnv());
  fs.mkdirSync(herdrRoot, { recursive: true, mode: 0o700 });
  fs.mkdirSync(process.env.XDG_CONFIG_HOME, { recursive: true, mode: 0o700 });
  const link = path.join(process.env.XDG_CONFIG_HOME, 'herdr');
  try { fs.symlinkSync(herdrRoot, link, 'dir'); } catch (error) {
    if (error.code !== 'EEXIST' || fs.realpathSync(link) !== fs.realpathSync(herdrRoot)) throw Error('conflicting Herdr directory');
  }
}
export function herdr(...args) {
  return execFileSync('herdr', ['--session', 'main', ...args], {
    env: herdrEnv(), encoding: 'utf8', timeout: 5000, stdio: ['ignore', 'pipe', 'ignore'], maxBuffer: 2 * 1024 * 1024,
  }).trim();
}
function api(...args) {
  const response = JSON.parse(herdr(...args));
  if (response.error || !response.result) throw Error('Herdr request failed');
  return response.result;
}
const mappingFile = pane => path.join(state, 'herdr-panes', pane + '.json');
function mapping(pane) {
  try { return JSON.parse(fs.readFileSync(mappingFile(pane), 'utf8')); } catch { return null; }
}
export function hook(event, pid = codexPid()) {
  if (!herdrBackend) return legacyHook(event, pid);
  const pane = process.env.HERDR_PANE_ID;
  if (!/^w[A-Z0-9]+:p[A-Z0-9]+$/.test(pane || '') || !idPattern.test(event.session_id || '') || fs.existsSync(frozen)) return;
  if (event.hook_event_name === 'SessionStart') {
    if (Object.hasOwn(event, 'transcript_path') && !event.transcript_path) return;
    const shell = api('pane', 'process-info', '--pane', pane).process_info.shell_pid;
    if (!pid || !descendant(pid, shell)) return;
    atomic(mappingFile(pane), { pid, hook: event.session_id,
      session: conversationId(event.session_id, event.transcript_path), transcript: event.transcript_path || '' });
  } else if (event.hook_event_name === 'SessionEnd' && mapping(pane)?.hook === event.session_id) {
    fs.rmSync(mappingFile(pane), { force: true });
  }
}
function resumePane(cwd, id) {
  const pane = { cwd };
  if (id) {
    pane.agent_session = { source: 'herdr:codex', agent: 'codex', kind: 'id', value: id };
    pane.agent_resume = { source: 'codex-infra', agent: 'codex',
      argv: ['node', process.env.SESSION_LAUNCHER || '/opt/codex-session.mjs', 'resume', id] };
  }
  return pane;
}
// CLI split IDs encode BSP paths; pane order follows the same depth-first walk.
function layoutTree(layout, ids) {
  const splits = new Map(layout.splits.map(split => [split.id.replace(/^split_\d+_/, '').replace(/^root$/, ''), split]));
  let next = 0;
  function node(key = '') {
    const split = splits.get(key);
    if (!split) {
      if (next >= ids.length) throw Error('invalid Herdr layout');
      return { Pane: ids[next++] };
    }
    return { Split: { direction: split.direction === 'right' ? 'Horizontal' : 'Vertical',
      ratio: split.ratio, first: node(key + '0'), second: node(key + '1') } };
  }
  const tree = node();
  if (next !== ids.length) throw Error('invalid Herdr layout');
  return tree;
}
export function snapshot() {
  if (!herdrBackend) return legacySnapshot();
  const live = api('api', 'snapshot').snapshot;
  const seen = new Set();
  let raw = 0;
  const workspaces = live.workspaces.map(ws => {
    const tabs = live.tabs.filter(tab => tab.workspace_id === ws.workspace_id).map(tab => {
      const layout = live.layouts.find(item => item.tab_id === tab.tab_id);
      const panes = {}, ids = [], publicNumbers = {};
      for (const item of layout.panes) {
        const pane = live.panes.find(p => p.pane_id === item.pane_id);
        const id = ++raw; ids.push(id);
        const tracked = mapping(item.pane_id);
        let session = null;
        if (tracked && idPattern.test(tracked.session || '')) {
          const shell = api('pane', 'process-info', '--pane', item.pane_id).process_info.shell_pid;
          if (descendant(tracked.pid, shell)) session = conversationId(tracked.session, tracked.transcript);
        }
        if (seen.has(session)) session = null;
        if (session) seen.add(session);
        panes[id] = resumePane(pane.foreground_cwd || pane.cwd || '/workspace', session);
        if (pane.label) panes[id].label = pane.label;
        publicNumbers[id] = decodeNumber(item.pane_id.split(':p')[1]);
      }
      return { custom_name: tab.label, layout: layoutTree(layout, ids), panes, zoomed: layout.zoomed,
        focused: ids[layout.panes.findIndex(p => p.pane_id === layout.focused_pane_id)], root_pane: ids[0],
        _publicNumbers: publicNumbers, _number: tab.number };
    });
    const public_pane_numbers = Object.assign({}, ...tabs.map(tab => tab._publicNumbers));
    const public_tab_numbers = tabs.map(tab => tab._number);
    for (const tab of tabs) { delete tab._publicNumbers; delete tab._number; }
    return { id: ws.workspace_id, custom_name: ws.label, identity_cwd: Object.values(tabs[0].panes)[0].cwd,
      public_pane_numbers, public_tab_numbers, tabs,
      active_tab: live.tabs.filter(t => t.workspace_id === ws.workspace_id).findIndex(t => t.tab_id === ws.active_tab_id) };
  });
  const selected = live.workspaces.findIndex(ws => ws.workspace_id === live.focused_workspace_id);
  return validateHerdr({ version: 3, workspaces, active: Math.max(0, selected), selected: Math.max(0, selected) });
}
function decodeNumber(value) {
  const alphabet = '123456789ABCDEFGHJKMNPQRSTVWXYZ0';
  return [...value].reduce((n, ch) => n * alphabet.length + alphabet.indexOf(ch) + 1, 0);
}
export function validateHerdr(value) {
  if (value?.version !== 3 || !Array.isArray(value.workspaces) || !value.workspaces.length || value.workspaces.length > 100 ||
      !Number.isInteger(value.active) || !value.workspaces[value.active]) throw Error('invalid Herdr checkpoint');
  const seen = new Set();
  let count = 0;
  for (const ws of value.workspaces) {
    if (!Array.isArray(ws.tabs) || !ws.tabs.length || !ws.tabs[ws.active_tab]) throw Error('invalid Herdr tabs');
    for (const tab of ws.tabs) {
      const ids = new Set();
      function walk(node, depth = 0) {
        if (depth > 100) throw Error('invalid Herdr layout');
        if (Number.isSafeInteger(node?.Pane) && node.Pane > 0 && tab.panes?.[node.Pane] && !ids.has(node.Pane)) { ids.add(node.Pane); return; }
        const s = node?.Split;
        if (!s || !['Horizontal', 'Vertical'].includes(s.direction) || !(s.ratio > 0 && s.ratio < 1)) throw Error('invalid Herdr layout');
        walk(s.first, depth + 1); walk(s.second, depth + 1);
      }
      walk(tab.layout);
      if (ids.size !== Object.keys(tab.panes).length || !ids.has(tab.focused) || !ids.has(tab.root_pane)) throw Error('invalid Herdr panes');
      for (const pane of Object.values(tab.panes)) {
        if (++count > 10000 || !path.isAbsolute(pane.cwd || '') || /[\x00-\x1f]/.test(pane.cwd)) throw Error('invalid Herdr directory');
        // Only infrastructure-authored Codex resumes are accepted; never replay
        // saved arbitrary launch commands or terminal input during restoration.
        delete pane.launch_argv;
        const id = pane.agent_session?.agent === 'codex' && pane.agent_session.kind === 'id' ? pane.agent_session.value : null;
        delete pane.agent_resume; delete pane.agent_session;
        if (idPattern.test(id || '') && !seen.has(id)) { Object.assign(pane, resumePane(pane.cwd, id)); seen.add(id); }
      }
    }
  }
  return value;
}
export function checkpoint(freeze = false) {
  if (!herdrBackend) return legacyCheckpoint(freeze);
  if (fs.existsSync(frozen) || recoveryFailed) return;
  let value;
  try { value = snapshot(); } catch (error) {
    // A fresh server has no workspaces until the first browser attaches.
    const live = api('api', 'snapshot').snapshot;
    if (!live.workspaces.length) return;
    throw error;
  }
  atomic(path.join(state, 'herdr-checkpoint.json'), value);
  if (freeze) {
    if (!fs.existsSync(path.join(state, 'herdr-deployment.json'))) atomic(path.join(state, 'herdr-deployment.json'), value);
    fs.writeFileSync(frozen, '', { mode: 0o600 });
  }
}
// tmux windows become tabs in one Herdr workspace. The source is retained for rollback.
export function migrateLegacy(value) {
  validate(value);
  let raw = 0;
  const tabs = value.windows.map(w => {
    const ids = w.panes.map(() => ++raw);
    let leaf = 0;
    const text = w.layout.slice(5);
    let offset = 0;
    function parse() {
      const match = /^(\d+)x(\d+),\d+,\d+/.exec(text.slice(offset));
      if (!match) throw Error('invalid tmux layout');
      offset += match[0].length;
      const marker = text[offset];
      if (marker !== '{' && marker !== '[') {
        const pane = /^,\d+/.exec(text.slice(offset));
        if (!pane || leaf >= ids.length) throw Error('invalid tmux pane');
        offset += pane[0].length;
        return { tree: { Pane: ids[leaf++] }, size: [Number(match[1]), Number(match[2])] };
      }
      offset++;
      const children = [parse()];
      while (text[offset] === ',') { offset++; children.push(parse()); }
      if (text[offset++] !== (marker === '{' ? '}' : ']')) throw Error('invalid tmux split');
      const axis = marker === '{' ? 0 : 1;
      function fold(items) {
        if (items.length === 1) return items[0].tree;
        const total = items.reduce((sum, child) => sum + child.size[axis], 0);
        return { Split: { direction: axis === 0 ? 'Horizontal' : 'Vertical', ratio: items[0].size[axis] / total,
          first: items[0].tree, second: fold(items.slice(1)) } };
      }
      return { tree: fold(children), size: [Number(match[1]), Number(match[2])] };
    }
    const layout = parse().tree;
    if (offset !== text.length || leaf !== ids.length) throw Error('invalid tmux layout');
    return { custom_name: w.name, layout, panes: Object.fromEntries(w.panes.map((p, i) => [ids[i], resumePane(p.cwd, p.session)])),
      zoomed: Boolean(w.zoomed), focused: ids[w.selected], root_pane: ids[0] };
  });
  return validateHerdr({ version: 3, workspaces: [{ identity_cwd: '/workspace', custom_name: 'main', tabs,
    active_tab: value.windows.findIndex(w => w.index === value.selected) }], active: 0, selected: 0 });
}
export function restore() {
  if (!herdrBackend) return legacyRestore();
  prepareHerdrEnvironment();
  try { api('api', 'snapshot'); return false; } catch { /* no running server */ }
  let value;
  for (const file of ['herdr-deployment.json', 'herdr-checkpoint.json']) {
    const target = path.join(state, file);
    if (fs.existsSync(target)) { value = validateHerdr(JSON.parse(fs.readFileSync(target, 'utf8'))); break; }
  }
  if (!value && !fs.existsSync(herdrSession)) {
    const legacy = read('deployment.json') || read('checkpoint.json');
    if (legacy) value = migrateLegacy(legacy);
  }
  if (!value && fs.existsSync(herdrSession)) value = validateHerdr(JSON.parse(fs.readFileSync(herdrSession, 'utf8')));
  if (!value) return false;
  for (const ws of value.workspaces) for (const tab of ws.tabs) for (const pane of Object.values(tab.panes)) {
    try { if (fs.statSync(pane.cwd).isDirectory()) continue; } catch {}
    pane.cwd = '/workspace';
  }
  // Pane IDs and PIDs belong to the previous server and must never map new shells.
  fs.rmSync(path.join(state, 'herdr-panes'), { recursive: true, force: true });
  atomic(herdrSession, value);
  return true;
}

function release() {
  for (const name of ['deployment.json', 'herdr-deployment.json']) fs.rmSync(path.join(state, name), { force: true });
}
export function supervise(launcher = path.join(process.env.TERMINAL_ROOT || '/opt/codex-terminal', 'start.mjs')) {
  fs.rmSync(frozen, { force: true });
  if (herdrBackend) prepareHerdrEnvironment();
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
      case 'resume': resume(process.argv[3]); break;
      default: supervise();
    }
  } catch { console.error('Terminal session operation failed.'); process.exitCode = 1; }
}
