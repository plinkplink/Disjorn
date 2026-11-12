// The per-user stores and module-level values and what empties them, bundled for node:
//
//     node --test client/tests/per_user_reset.test.mjs

import { test, before, after, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
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
export { forgetPerUserValues, perUserValueNames } from "../src/lib/perUserState";
export { socket } from "../src/ws";
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

class FakeSocket {
  static OPEN = 1;
  static made = [];
  constructor() {
    this.readyState = FakeSocket.OPEN;
    this.sent = [];
    FakeSocket.made.push(this);
  }
  send(frame) { this.sent.push(JSON.parse(frame)); }
  close() {}
}

let m;
let scratch;

before(async () => {
  globalThis.WebSocket = FakeSocket;
  globalThis.location = { protocol: 'https:', host: 'test', hash: '', pathname: '/', search: '' };
  globalThis.history = { replaceState: (_state, _title, url) => {
    location.hash = url.includes('#') ? url.slice(url.indexOf('#')) : '';
  } };
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
  FakeSocket.made = [];
  location.hash = '';
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

async function signIn(who) {
  routes['POST /auth/login'] = who;
  assert.equal(await m.useSession.getState().login(who.username, 'pw'), true);
}

async function handOver() {
  await m.useSession.getState().logout();
  await signIn(BOB);
}

/* Module-level declarations that can hold a value between calls: every `let`,
   every collection or instance created empty, every store outside stores/. */
const HOLDERS = [
  /^(?:export )?let (\w+)/,
  /^(?:export )?const (\w+)[^=]*= (?:\{\}|\[\]|new \w+(?:<[^>]*>)?\(\))/,
];
const STORE = /^(?:export )?const (\w+) = create</;

/* Held for the device or the server, wiring set once at load, a counter that
   only has to be unique, or an empty constant: not one account's. */
const NOT_PER_USER = [
  'api:account', 'api:onRotationRequired', 'api:onUnauthenticated',
  'components/ChibiPicker:botsOnce', 'components/Composer:uploadCounter',
  'components/Markdown:NO_CHIBIS', 'components/MessageList:EMPTY_GAPS',
  'components/MessageList:EMPTY_LIST', 'components/MessageList:EMPTY_NAMES',
  'install:deferredPrompt', 'install:useInstall', 'lib/perUserState:registered',
  'push:usePush', 'push:vapidKey', 'pwa:listeners', 'pwa:updateWaiting',
  'sidebar:useSidebarSections', 'sounds:ctx', 'sounds:lastPlayedAt',
  'sounds:useSoundSettings', 'stores/apps:syntheticStageId',
  'stores/channels:onListApplied',
];

test('every module-level value is forgotten with the account or listed as not one account\'s', () => {
  const src = join(CLIENT, 'src');
  const found = [];
  for (const file of readdirSync(src, { recursive: true }).filter((f) => /\.tsx?$/.test(f))) {
    const module = file.replace(/\.tsx?$/, '');
    for (const line of readFileSync(join(src, file), 'utf8').split('\n')) {
      const hit = HOLDERS.map((re) => re.exec(line)).find(Boolean)
        ?? (module.startsWith('stores/') ? null : STORE.exec(line));
      if (hit) found.push(`${module}:${hit[1]}`);
    }
  }
  assert.deepEqual(found.sort(), [...m.perUserValueNames(), ...NOT_PER_USER].sort());
});

test('the next account climbs its own first-load ladder', async () => {
  await signIn(ALICE);
  routes['GET /channels'] = 'held';
  const channels = m.stores.channels;
  const alices = channels.getState().ensureLoaded();
  await handOver();
  const bobs = channels.getState().ensureLoaded();
  assert.equal(held.length, 2);

  held[0](answer(503, { detail: 'down' }));
  await alices;
  assert.equal(channels.getState().loadFailed, false);
  held[1](answer(200, [ROW]));
  await bobs;
  assert.equal(channels.getState().loaded, true);
  assert.equal(held.length, 2);
});

test('forgetting alone outdates a channel list already asked for', async () => {
  await signIn(ALICE);
  routes['GET /channels'] = 'held';
  const list = m.stores.channels.getState().refresh();
  m.forgetPerUserValues();
  held[0](answer(200, [ROW]));
  await list;
  assert.deepEqual(m.stores.channels.getState().channels, []);
});

test('a roster fetch left in flight does not block the next account\'s', async () => {
  await signIn(ALICE);
  routes['GET /channels/1/members'] = 'held';
  const alices = m.stores.members.getState().refresh(1);
  await handOver();
  const bobs = m.stores.members.getState().refresh(1);
  assert.equal(held.length, 2);
  for (const give of held) give(answer(200, [{ type: 'user', id: 3, name: 'Carol' }]));
  await Promise.all([alices, bobs]);
});

test('a channel the last account left or deleted is no longer this tab\'s own doing', async () => {
  await signIn(ALICE);
  routes['POST /channels/4/leave'] = { ok: true, left: true };
  routes['DELETE /channels/9'] = { ok: true };
  await m.stores.membership.getState().leave(4);
  await m.stores.channelDelete.getState().remove(9);
  await handOver();

  const channel = { name: 'plans', visibility: 'private' };
  m.stores.membership.getState().onMemberRemove({ channel_id: 4, member_type: 'user',
    member_id: BOB.id, by_user_id: null, channel });
  assert.equal(m.stores.membership.getState().notice.kind, 'removed');
  m.stores.channels.getState().setActive(9);
  m.stores.channelDelete.getState().onChannelDelete({ channel_id: 9, by_user_id: null, channel });
  assert.equal(m.stores.channelDelete.getState().notice.channelId, 9);
});

test('the socket does not report the last account\'s channel as the next account\'s focus', async () => {
  const ready = (ws) => ws.onmessage({ data: JSON.stringify({ type: 'ready' }) });
  await signIn(ALICE);
  m.socket.connect();
  ready(FakeSocket.made[0]);
  m.socket.sendFocus(7);
  assert.deepEqual(FakeSocket.made[0].sent, [{ op: 'focus', channel_id: 7 }]);

  await m.useSession.getState().logout();
  m.socket.disconnect();
  await signIn(BOB);
  m.socket.connect();
  ready(FakeSocket.made[1]);
  assert.deepEqual(FakeSocket.made[1].sent, []);
  m.socket.disconnect();
});

test('an account that leaves takes its route; a link opened signed out keeps its own', async () => {
  await signIn(ALICE);
  location.hash = '#/channels/7';
  await m.useSession.getState().logout();
  assert.equal(location.hash, '');

  location.hash = '#/channels/3';
  await signIn(BOB);
  assert.equal(location.hash, '#/channels/3');

  globalThis.fetch = async () => answer(401, { detail: 'Not authenticated' });
  await assert.rejects(m.stores.channels.getState().refresh());
  assert.equal(location.hash, '');
});
