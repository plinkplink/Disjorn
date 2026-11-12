// What a logout does with this device's push subscription, and what the next
// login does, against a faked browser, bundled for node:
//
//     node --test client/tests/push_release.test.mjs

import { test, before, after, beforeEach } from 'node:test';
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
export { releaseDevice } from "../src/lib/pushRelease";
export { usePush } from "../src/push";
export { useSession } from "../src/stores/session";
`;

/* The browser: one subscription at most, the notifications on show, and the
   flag that says the reader turned push on here. `log` is every call that
   leaves the page, in order, with the account whose cookie carried it. */
let log;
let subscription;
let shown;
let stored;
let made;
let cookie;
let deleteAnswer;
let subscriptionLookup;

function subscribe() {
  const endpoint = `https://push.example/${++made}`;
  subscription = {
    endpoint,
    toJSON: () => ({ endpoint, keys: { p256dh: 'k', auth: 'a' } }),
    unsubscribe: async () => {
      log.push(`unsubscribe ${endpoint}`);
      subscription = null;
      return true;
    },
  };
  return subscription;
}

const registration = {
  pushManager: {
    getSubscription: () => subscriptionLookup(),
    subscribe: async () => {
      log.push('subscribe');
      return subscribe();
    },
  },
  getNotifications: async () => shown,
};

let m;
let scratch;

before(async () => {
  Object.defineProperty(globalThis, 'navigator', { configurable: true, value: {
    serviceWorker: { getRegistration: async () => registration,
      ready: Promise.resolve(registration) } } });
  globalThis.window = { PushManager: class {}, Notification: class {} };
  globalThis.Notification = { permission: 'granted' };
  Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: {
    getItem: (k) => stored.get(k) ?? null,
    setItem: (k, v) => stored.set(k, v),
    removeItem: (k) => stored.delete(k) } });
  globalThis.location = { protocol: 'https:', host: 'test', hash: '' };
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
  scratch = mkdtempSync(join(tmpdir(), 'push-release-'));
  const file = join(scratch, 'bundle.mjs');
  writeFileSync(file, out.outputFiles[0].text);
  m = await import(pathToFileURL(file).href);
});

after(() => { if (scratch) rmSync(scratch, { recursive: true, force: true }); });

const user = (id, username) => ({ id, username, display_name: username, avatar_path: null,
  status: 'online', is_admin: false, created_at: '2026-10-06T00:00:00Z' });
const USERS = { alice: user(1, 'alice'), bob: user(2, 'bob') };

const answer = (status, body) => ({ ok: status < 300, status, statusText: '',
  json: async () => body });

beforeEach(() => {
  log = [];
  subscription = null;
  shown = [];
  stored = new Map();
  made = 0;
  cookie = null;
  deleteAnswer = () => answer(200, { ok: true, removed: true });
  subscriptionLookup = async () => subscription;
  globalThis.fetch = async (path, init) => {
    const key = `${init.method} ${path}`;
    const body = init.body === undefined ? {} : JSON.parse(init.body);
    if (key === 'POST /auth/login') {
      cookie = body.username;
      return answer(200, USERS[cookie]);
    }
    log.push([key, 'as', cookie, body.endpoint].filter(Boolean).join(' '));
    if (key === 'POST /auth/logout') {
      cookie = null;
      return answer(200, { ok: true });
    }
    if (key === 'DELETE /push/subscribe') return deleteAnswer();
    if (key === 'GET /vapid-public-key') return answer(200, { key: 'AAAA' });
    if (key === 'POST /push/subscribe') return answer(200, { ok: true });
    throw new Error(`unexpected ${key}`);
  };
  m.useSession.setState({ user: null, mustChangePassword: false });
  m.usePush.setState({ status: 'checking', subscribed: null });
});

const signIn = (name) => m.useSession.getState().login(name, 'pw');
const logout = () => m.useSession.getState().logout();

test('the server is asked before the endpoint is killed, and a refusal does not spare it', async () => {
  const calls = [];
  const sub = (unsubscribed) => ({ endpoint: 'e',
    unsubscribe: async () => { calls.push('unsubscribe'); return unsubscribed(); } });
  const forget = async (endpoint) => { calls.push(`forget ${endpoint}`); };

  assert.equal(await m.releaseDevice(sub(() => true), forget), true);
  assert.deepEqual(calls, ['forget e', 'unsubscribe']);
  assert.equal(await m.releaseDevice(sub(() => true), () => Promise.reject(new Error('no'))), true);
  assert.equal(await m.releaseDevice(sub(() => { throw new Error('no'); }), forget), false);
  assert.equal(await m.releaseDevice(null, forget), false);
  assert.deepEqual(calls, ['forget e', 'unsubscribe', 'unsubscribe', 'forget e', 'unsubscribe']);
});

test('a logout releases the device while the session can still ask, then ends it', async () => {
  await signIn('alice');
  subscribe();
  m.usePush.setState({ status: 'enabled', subscribed: true });
  shown = [{ close: () => log.push('close a shown notification') }];

  await logout();
  assert.deepEqual(log, [
    'DELETE /push/subscribe as alice https://push.example/1',
    'unsubscribe https://push.example/1',
    'close a shown notification',
    'POST /auth/logout as alice',
  ]);
  assert.equal(m.usePush.getState().subscribed, false);
  assert.equal(m.useSession.getState().user, null);
});

test('a logout the server refuses or never hears still kills the endpoint and signs out', async () => {
  const failures = {
    refused: () => answer(503, { detail: 'down' }),
    unheard: () => { throw new TypeError('offline'); },
  };
  for (const [name, fail] of Object.entries(failures)) {
    log = [];
    await signIn('alice');
    subscribe();
    deleteAnswer = fail;
    await logout();
    assert.equal(log.at(-2), `unsubscribe https://push.example/${made}`, name);
    assert.equal(log.at(-1), 'POST /auth/logout as alice', name);
    assert.equal(subscription, null, name);
    assert.equal(m.useSession.getState().user, null, name);
  }
});

test('a device without a subscription has nothing to release', async () => {
  await signIn('alice');
  await logout();
  assert.deepEqual(log, ['POST /auth/logout as alice']);
});

test('a release that never answers does not hold the logout', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  await signIn('alice');
  subscriptionLookup = () => new Promise(() => {});
  const done = logout();
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(log, []);
  t.mock.timers.tick(5_000);
  await done;
  assert.deepEqual(log, ['POST /auth/logout as alice']);
  assert.equal(m.useSession.getState().user, null);
});

test('the next account subscribes the device again as its own', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  await signIn('alice');
  subscribe();
  stored.set('disjorn.push.wanted', '1');
  await logout();
  await signIn('bob');
  log = [];

  await m.usePush.getState().sync();
  assert.deepEqual(log, [
    'GET /vapid-public-key as bob',
    'subscribe',
    'POST /push/subscribe as bob https://push.example/2',
  ]);
  assert.equal(m.usePush.getState().subscribed, true);
});

test('a device whose reader never turned push on stays unsubscribed for the next account', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  await signIn('alice');
  subscribe();
  await logout();
  await signIn('bob');
  log = [];

  await m.usePush.getState().sync();
  assert.deepEqual(log, []);
  assert.equal(subscription, null);
});
