// The realtime socket against a rotation-pending session, bundled for node:
//
//     node --test client/tests/ws_rotation.test.mjs

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
  scratch = mkdtempSync(join(tmpdir(), 'ws-rotation-'));
  const file = join(scratch, 'bundle.mjs');
  writeFileSync(file, out.outputFiles[0].text);
  m = await import(pathToFileURL(file).href);
});

after(() => { if (scratch) rmSync(scratch, { recursive: true, force: true }); });

beforeEach(() => {
  FakeSocket.made = [];
  m.useSession.setState({ mustChangePassword: false });
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
