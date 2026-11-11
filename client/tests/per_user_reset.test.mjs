// The per-user stores and what empties them, bundled for node:
//
//     node --test client/tests/per_user_reset.test.mjs

import { test, before, after, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, mkdtempSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
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
export { PER_USER_STORES } from "../src/stores/perUser";
export { useSession } from "../src/stores/session";
import { useApprovals } from "../src/stores/approvals";
import { useApps } from "../src/stores/apps";
import { useChannelDelete } from "../src/stores/channelDelete";
import { useChannels } from "../src/stores/channels";
import { useMembers } from "../src/stores/members";
import { useMembership } from "../src/stores/membership";
import { useMessages } from "../src/stores/messages";
import { usePresence } from "../src/stores/presence";
import { useUnfurl } from "../src/stores/unfurl";
export const stores = { approvals: useApprovals, apps: useApps,
  channelDelete: useChannelDelete, channels: useChannels, members: useMembers,
  membership: useMembership, messages: useMessages, presence: usePresence,
  unfurl: useUnfurl };
`;

let m;
let scratch;

before(async () => {
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
  scratch = mkdtempSync(join(tmpdir(), 'per-user-'));
  const file = join(scratch, 'bundle.mjs');
  writeFileSync(file, out.outputFiles[0].text);
  m = await import(pathToFileURL(file).href);
});

after(() => { if (scratch) rmSync(scratch, { recursive: true, force: true }); });

const user = (id, username) => ({ id, username, display_name: username, avatar_path: null,
  status: 'online', is_admin: false, created_at: '2026-10-06T00:00:00Z' });
const ALICE = user(1, 'alice');
const BOB = user(2, 'bob');

const ROW = { id: 1, type: 'dm_1to1', name: 'Carol', dm_user_id: 3, unread: 2, last_read_seq: 3,
  last_message: { seq: 5, snippet: 'for alice only', author_type: 'user', author_id: 3,
    created_at: '2026-10-06T00:00:00Z' } };
const MESSAGE = { id: 50, channel_id: 1, seq: 5, content: 'for alice only', author_type: 'user',
  author_id: 3, created_at: '2026-10-06T00:00:00Z' };

/* `routes` answers by "METHOD path"; a function value is called per request,
   and a `held` path waits for the test to answer it. */
let routes;
let held;

const answer = (status, body) => ({ ok: status < 300, status, statusText: '',
  json: async () => body });

beforeEach(() => {
  held = [];
  routes = {
    'POST /auth/login': ALICE,
    'POST /auth/logout': { ok: true },
    'GET /channels': [ROW],
    'GET /channels/1/members': [{ type: 'user', id: 3, name: 'Carol' }],
  };
  globalThis.fetch = (path, init) => {
    const key = `${init.method} ${path.split('?')[0]}`;
    if (key === 'GET /channels/1/messages') return Promise.resolve(answer(200, [MESSAGE]));
    if (routes[key] === 'held') return new Promise((resolve) => held.push(resolve));
    if (key in routes) return Promise.resolve(answer(200, routes[key]));
    throw new Error(`unexpected ${key}`);
  };
  m.useSession.setState({ user: null, mustChangePassword: false });
});

/* One entry per per-user store, so a store added to the list is also put here. */
async function fillAsAlice() {
  assert.equal(await m.useSession.getState().login('alice', 'pw'), true);
  const s = m.stores;
  await s.channels.getState().refresh();
  s.channels.getState().setActive(1);
  await s.messages.getState().ensureLoaded(1);
  await s.members.getState().refresh(1);
  s.presence.getState().setStatus(3, 'online');
  s.unfurl.setState({ byUrl: { 'https://example.test/': { status: 'done' } } });
  s.apps.setState({ apps: [{ id: 'a1', name: "Alice's app" }], loaded: true });
  s.approvals.setState({ status: 'ready', proposals: [{ id: 7 }] });
  s.channelDelete.setState({ notice: { channelId: 9, channelName: 'gone', isPrivate: true } });
  s.membership.setState({ notice: { kind: 'added', channelId: 4 } });
  for (const [name, store] of Object.entries(m.PER_USER_STORES)) {
    assert.notEqual(store.getState(), store.getInitialState(), `${name} holds nothing`);
  }
}

function assertAllEmpty() {
  for (const [name, store] of Object.entries(m.PER_USER_STORES)) {
    assert.equal(store.getState(), store.getInitialState(), `${name} kept its state`);
  }
  const channels = m.stores.channels.getState();
  assert.deepEqual(channels.channels, []);
  assert.deepEqual(channels.unreadState, {});
  assert.equal(channels.activeChannelId, null);
  assert.equal(channels.loaded, false);
  assert.deepEqual(m.stores.messages.getState().byChannel, {});
  assert.deepEqual(m.stores.members.getState().byChannel, {});
  assert.deepEqual(m.stores.unfurl.getState().byUrl, {});
}

test('every store file but the session is on the reset list', () => {
  const files = readdirSync(join(CLIENT, 'src', 'stores'))
    .map((f) => f.replace(/\.ts$/, ''))
    .filter((f) => f !== 'session' && f !== 'perUser');
  assert.deepEqual(Object.keys(m.PER_USER_STORES).sort(), files.sort());
  assert.deepEqual(Object.keys(m.stores).sort(), files.sort());
});

test('logging out empties every per-user store', async () => {
  await fillAsAlice();
  assert.equal(m.stores.channels.getState().channels[0].last_message.snippet, 'for alice only');
  assert.equal(m.stores.messages.getState().byChannel[1].list.length, 1);

  await m.useSession.getState().logout();
  assert.equal(m.useSession.getState().user, null);
  assertAllEmpty();
});

test('a session that ends on a 401 empties them too', async () => {
  await fillAsAlice();
  globalThis.fetch = async () => answer(401, { detail: 'Not authenticated' });
  await assert.rejects(m.stores.channels.getState().refresh());
  assert.equal(m.useSession.getState().user, null);
  assertAllEmpty();
});

test('a session that changes hands without a logout empties them too', async () => {
  await fillAsAlice();
  routes['GET /me'] = BOB;
  assert.equal(await m.useSession.getState().recheck(), true);
  assert.equal(m.useSession.getState().user.id, BOB.id);
  assertAllEmpty();
});

test('an answer still in flight at logout never reaches the next account', async () => {
  await fillAsAlice();
  routes['GET /channels'] = 'held';
  const list = m.stores.channels.getState().refresh();
  const refused = assert.rejects(list, { status: 0 });

  await m.useSession.getState().logout();
  routes['POST /auth/login'] = BOB;
  assert.equal(await m.useSession.getState().login('bob', 'pw'), true);
  held[0](answer(200, [ROW]));
  await refused;
  assertAllEmpty();
});

test("the last account's late 401 does not sign the next account out", async () => {
  await fillAsAlice();
  routes['GET /channels'] = 'held';
  const refused = assert.rejects(m.stores.channels.getState().refresh(), { status: 0 });

  await m.useSession.getState().logout();
  routes['POST /auth/login'] = BOB;
  await m.useSession.getState().login('bob', 'pw');
  held[0](answer(401, { detail: 'Not authenticated' }));
  await refused;
  assert.equal(m.useSession.getState().user.id, BOB.id);
});
