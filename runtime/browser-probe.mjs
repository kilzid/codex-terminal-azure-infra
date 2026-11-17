// Isolated browser health check. No user browser session, project, or external URL.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import http from 'node:http';
import { execFileSync } from 'node:child_process';
import { createRequire } from 'node:module';

let browser, server, directory;
try {
  assert.equal(process.getuid(), 1000, 'browser runs as the terminal user');
  execFileSync('playwright-cli', ['--version'], { timeout: 15000, stdio: 'pipe' });
  const globalRoot = execFileSync('npm', ['root', '-g'], { encoding: 'utf8', timeout: 15000 }).trim();
  const require = createRequire(path.join(globalRoot, '@playwright/cli/package.json'));
  const { chromium } = require('playwright');
  const config = JSON.parse(fs.readFileSync(path.join(os.homedir(), '.playwright/cli.config.json'), 'utf8'));
  const launch = config.browser.launchOptions;
  assert.equal(launch.headless, true);
  assert.equal(launch.executablePath, chromium.executablePath());
  assert.ok(launch.executablePath.startsWith('/opt/playwright-browsers/'));
  fs.accessSync(launch.executablePath, fs.constants.X_OK);

  server = http.createServer((_req, res) => {
    res.setHeader('content-type', 'text/html; charset=utf-8');
    res.end('<!doctype html><title>Browser check</title><button onclick="document.querySelector(\'output\').textContent=\'BROWSER_CLICK_OK\'">Check</button><output>Ready</output>');
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  // Use the installed CLI's own Playwright and browser defaults. Container
  // capabilities, seccomp, AppArmor and memory limits remain enforced.
  browser = await chromium.launch({ ...launch, timeout: 30000 });
  const page = await browser.newPage();
  await page.goto('http://127.0.0.1:' + server.address().port, { timeout: 15000 });
  assert.equal(await page.title(), 'Browser check');
  await page.getByRole('button', { name: 'Check', exact: true }).click({ timeout: 10000 });
  assert.equal(await page.locator('output').textContent(), 'BROWSER_CLICK_OK');
  directory = fs.mkdtempSync(path.join(os.tmpdir(), 'codex-browser-probe-'));
  const screenshot = await page.screenshot({ path: path.join(directory, 'check.png'), timeout: 15000 });
  assert.deepEqual([...screenshot.subarray(0, 8)], [137, 80, 78, 71, 13, 10, 26, 10]);
  console.log('BROWSER_CHROMIUM_NAVIGATION_SCREENSHOT_OK');
} catch {
  console.error('BROWSER_PROBE_FAILED');
  process.exitCode = 1;
} finally {
  await browser?.close();
  server?.closeAllConnections();
  server?.close();
  if (directory) fs.rmSync(directory, { recursive: true, force: true });
}
