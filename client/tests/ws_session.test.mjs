// The realtime socket and the session store when a session is walled off or
// gone, bundled for node:
//
//     node --test client/tests/ws_session.test.mjs

import { test, before, after, beforeEach, mock } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const CLIENT = dirname(HERE);

function nodeModules() {
  const found = [process.env.DISJORN_CLIENT_NODE_MODULES,
    join(CLIENT, 'node_modules'), '/opt/node_modules']
    .find((d) => d && existsSync(join(d, 'esbuild')));
  if (!found) throw new Error('no client toolchain (esbuild) found');
  return found;
}

const ENTRY = `
export { DisjornSocket } from "../src/ws";
export { useSession } from "../src/stores/session";
export { fetchMe, login } from "../src/api";
`;

class FakeSocket {
  static OPEN = 1;
  static made = [];
  constructor(url) {
    this.url = url;
    this.readyState = 0;
    this.closed = false;
    FakeSocket.made.push(this);
  }
  send() {}
  close() { this.closed = true; }
}

let m;
let scratch;

before(async () => {
  globalThis.WebSocket = FakeSocket;
  globalThis.location = { protocol: 'https:', host: 'test', hash: '' };
  globalThis.document = { hasFocus: () => false };
  const nm = nodeModules();
  const bin = join(nm, '@esbuild', `${process.platform}-${process.arch}`, 'bin', 'esbuild');
  if (!process.env.ESBUILD_BINARY_PATH && existsSync(bin)) process.env.ESBUILD_BINARY_PATH = bin;
  const esbuild = createRequire(import.meta.url)(join(nm, 'esbuild'));
  const out = await esbuild.build({
    stdin: { contents: ENTRY, resolveDir: HERE, loader: 'js' },
    bundle: true, platform: 'node', format: 'esm', write: false,
    jsx: 'automatic', nodePaths: [nm], logLevel: 'silent',
    define: { 'process.env.NODE_ENV': '"production"' },
    banner: { js: "import { createRequire } from 'node:module';"
      + ' const require = createRequire(import.meta.url);' },
  });
  scratch = mkdtempSync(join(tmpdir(), 'ws-session-'));
  const file = join(scratch, 'bundle.mjs');
  writeFileSync(file, out.outputFiles[0].text);
  m = await import(pathToFileURL(file).href);
});

after(() => { if (scratch) rmSync(scratch, { recursive: true, force: true }); });

const ALICE = { id: 1, username: 'alice', display_name: 'Alice', avatar_path: null,
  status: 'online', is_admin: false, created_at: '2026-10-06T00:00:00Z' };

function answer(status, body) {
  globalThis.fetch = async () => ({
    ok: status >= 200 && status < 300, status, statusText: '',
    json: async () => body,
  });
}

const settle = () => new Promise((resolve) => setImmediate(resolve));

beforeEach(() => {
  FakeSocket.made = [];
  m.useSession.setState({ mustChangePassword: false, user: ALICE });
});

test('a 4403 close stops the socket and raises the rotation screen', (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const socket = new m.DisjornSocket();
  socket.connect();
  assert.equal(FakeSocket.made.length, 1);
  FakeSocket.made[0].onclose({ code: 4403 });
  t.mock.timers.tick(120_000);
  socket.wake(true);
  assert.equal(FakeSocket.made.length, 1);
  assert.equal(m.useSession.getState().mustChangePassword, true);
});

test('the shell connecting again after the rotation opens a fresh socket', (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const socket = new m.DisjornSocket();
  socket.connect();
  FakeSocket.made[0].onclose({ code: 4403 });
  socket.disconnect();
  socket.connect();
  assert.equal(FakeSocket.made.length, 2);
});

test('any other close still reconnects with backoff', (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const socket = new m.DisjornSocket();
  socket.connect();
  FakeSocket.made[0].onclose({ code: 1006 });
  t.mock.timers.tick(2_000);
  assert.equal(FakeSocket.made.length, 2);
  assert.equal(m.useSession.getState().mustChangePassword, false);
  socket.disconnect();
});

test('a 4401 close with a dead session signs out instead of retrying', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  answer(401, { detail: 'Not authenticated' });
  const socket = new m.DisjornSocket();
  socket.connect();
  FakeSocket.made[0].onclose({ code: 4401 });
  await settle();
  assert.equal(m.useSession.getState().user, null);
  t.mock.timers.tick(120_000);
  assert.equal(FakeSocket.made.length, 1);
});

test('a 4401 close with a live session reconnects after the check', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  answer(200, ALICE);
  const socket = new m.DisjornSocket();
  socket.connect();
  FakeSocket.made[0].onclose({ code: 4401 });
  await settle();
  assert.equal(m.useSession.getState().user.username, 'alice');
  t.mock.timers.tick(2_000);
  assert.equal(FakeSocket.made.length, 2);
  socket.disconnect();
});

test('any 401 from the API signs the store out, but a failed login does not', async () => {
  answer(401, { detail: 'Invalid username or password' });
  await assert.rejects(m.login('alice', 'wrong'));
  assert.equal(m.useSession.getState().user.username, 'alice');
  answer(401, { detail: 'Not authenticated' });
  await assert.rejects(m.fetchMe());
  assert.equal(m.useSession.getState().user, null);
});
