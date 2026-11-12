// Unread bookkeeping, the tab title, and the channels store and socket that
// feed them, bundled for node:
//
//     node --test client/tests/unread.test.mjs

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
export * from "../src/lib/unread";
export { useApps } from "../src/stores/apps";
export { useChannels } from "../src/stores/channels";
export { useSession } from "../src/stores/session";
export { DisjornSocket, socket } from "../src/ws";
`;

class FakeSocket {
  static OPEN = 1;
  static made = [];
  constructor() {
    this.readyState = FakeSocket.OPEN;
    FakeSocket.made.push(this);
  }
  send() {}
  close() {}
}

let m;
let scratch;
let focused = false;

before(async () => {
  globalThis.WebSocket = FakeSocket;
  globalThis.location = { protocol: 'https:', host: 'test', hash: '' };
  globalThis.document = { hasFocus: () => focused };
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
  scratch = mkdtempSync(join(tmpdir(), 'unread-'));
  const file = join(scratch, 'bundle.mjs');
  writeFileSync(file, out.outputFiles[0].text);
  m = await import(pathToFileURL(file).href);
});

after(() => { if (scratch) rmSync(scratch, { recursive: true, force: true }); });

const ALICE = { id: 1, username: 'alice', display_name: 'Alice', avatar_path: null,
  status: 'online', is_admin: false, created_at: '2026-10-06T00:00:00Z' };

function row(id, unread, lastSeq, type = 'text') {
  return { id, type, name: `c${id}`, dm_user_id: null, unread,
    last_message: lastSeq === null ? null : { seq: lastSeq, snippet: 's',
      author_type: 'user', author_id: 2, created_at: '2026-10-06T00:00:00Z' } };
}

/** The row as the server counted it with the reader's cursor at `cursor`. */
const at = (r, cursor) => ({ ...r, last_read_seq: cursor });

function message(channelId, seq, authorId = 2) {
  return { id: channelId * 1000 + seq, channel_id: channelId, seq, content: 'hi',
    author_type: 'user', author_id: authorId, created_at: '2026-10-06T00:00:00Z' };
}

/* Each GET /channels waits for the test to answer it; each read is recorded
   and answered by `readStatus`, stored as `storedAs` says. */
let lists;
let reads;
let readStatus;
let storedAs;

function answerNextList(rows, which = 0) {
  const [pending] = lists.splice(which, 1);
  pending({ ok: true, status: 200, statusText: '', json: async () => rows });
}

const settle = () => new Promise((resolve) => setImmediate(resolve));

async function load(rows) {
  const done = m.useChannels.getState().refresh();
  answerNextList(rows);
  await done;
}

const unreadOf = (id) => m.useChannels.getState().channels.find((c) => c.id === id).unread;

function fresh() {
  focused = false;
  lists = [];
  reads = [];
  readStatus = 200;
  storedAs = (seq) => seq;
  FakeSocket.made = [];
  globalThis.fetch = (path, init) => {
    if (init.method === 'GET' && path === '/channels') {
      return new Promise((resolve) => lists.push(resolve));
    }
    const read = /^\/channels\/(\d+)\/read$/.exec(path);
    if (init.method === 'PUT' && read) {
      const seq = JSON.parse(init.body).seq;
      reads.push([Number(read[1]), seq]);
      return Promise.resolve({ ok: readStatus === 200, status: readStatus, statusText: '',
        json: async () => ({ channel_id: Number(read[1]), last_read_seq: storedAs(seq) }) });
    }
    throw new Error(`unexpected ${init.method} ${path}`);
  };
  m.useSession.setState({ user: ALICE });
  m.useChannels.setState({ channels: [], unreadState: {}, activeChannelId: null,
    atBottom: {}, loaded: false });
  m.useApps.setState({ openBuildSessionId: null });
  m.socket.disconnect();
  m.socket.sendFocus(null);
}

beforeEach(fresh);

test('the title is the sum of the rows the sidebar draws, and nothing else', () => {
  const rows = [row(1, 2, 9, 'main_feed'), row(2, 1, 4), row(3, 0, 7, 'dm_1to1'),
    row(4, 65, 65, 'app_build')];
  assert.equal(m.totalUnread(rows), 3);
  assert.equal(m.tabTitle(rows), '(3) Disjorn');
  assert.equal(m.tabTitle([row(4, 65, 65, 'app_build')]), 'Disjorn');
  assert.equal(m.tabTitle([]), 'Disjorn');
  assert.deepEqual(rows.filter(m.isSidebarRow).map((r) => r.id), [1, 2, 3]);
});

test('the app badge is set to the total, cleared at zero, and left alone where unsupported', () => {
  assert.equal(m.badgeCall(3, true), 'set');
  assert.equal(m.badgeCall(0, true), 'clear');
  assert.equal(m.badgeCall(3, false), null);
  assert.equal(m.badgeCall(0, false), null);

  const calls = [];
  const nav = { setAppBadge: async (n) => { calls.push(['set', n]); },
    clearAppBadge: async () => { calls.push(['clear']); } };
  m.syncAppBadge(nav, 4);
  m.syncAppBadge(nav, 0);
  assert.deepEqual(calls, [['set', 4], ['clear']]);

  m.syncAppBadge({}, 4);
  m.syncAppBadge(undefined, 4);
  m.syncAppBadge({ setAppBadge: nav.setAppBadge }, 4);
  assert.equal(calls.length, 2);
});

test('a badge the platform refuses is dropped without a throw or a stray rejection', async () => {
  const stray = [];
  const onStray = (err) => stray.push(err);
  process.on('unhandledRejection', onStray);
  try {
    const throws = () => { throw new Error('no'); };
    const rejects = () => Promise.reject(new Error('no'));
    m.syncAppBadge({ setAppBadge: throws, clearAppBadge: throws }, 2);
    m.syncAppBadge({ setAppBadge: throws, clearAppBadge: throws }, 0);
    m.syncAppBadge({ setAppBadge: rejects, clearAppBadge: rejects }, 2);
    m.syncAppBadge({ setAppBadge: rejects, clearAppBadge: rejects }, 0);
    m.syncAppBadge({ setAppBadge: () => undefined, clearAppBadge: () => undefined }, 2);
    await settle();
    await settle();
  } finally {
    process.off('unhandledRejection', onStray);
  }
  assert.deepEqual(stray, []);
});

test('a frame the list already counted is not counted twice', () => {
  const s = m.listed(undefined, row(1, 3, 10));
  assert.equal(m.unreadCount(m.created(s, 10, false)), 3);
  assert.equal(m.unreadCount(m.created(s, 11, false)), 4);
  assert.equal(m.unreadCount(m.created(m.created(s, 11, false), 11, false)), 4);
});

test('a frame that beat an older list is still counted, in any arrival order', () => {
  let s = m.created(m.listed(undefined, row(1, 0, 10)), 12, false);
  s = m.created(s, 11, false);
  assert.equal(m.unreadCount(s), 2);
  assert.equal(m.unreadCount(m.listed(s, row(1, 0, 10))), 2);
  assert.equal(m.unreadCount(m.listed(s, row(1, 1, 11))), 2);
  assert.equal(m.unreadCount(m.listed(s, row(1, 2, 12))), 2);
});

test('a list computed before our read reached the server cannot bring the badge back', () => {
  const s = m.markedRead(m.listed(undefined, row(1, 3, 10)), 10);
  assert.equal(m.unreadCount(s), 0);
  assert.equal(m.unreadCount(m.listed(s, at(row(1, 3, 10), 7))), 0);
  assert.equal(m.unreadCount(m.listed(s, at(row(1, 2, 12), 10))), 2);
});

test('a list older than our read counts only what arrived above the read', () => {
  // Read through 10 here; the server counted 8..12 with its cursor still at 7.
  const read = m.markedRead(m.listed(undefined, at(row(1, 3, 10), 7)), 10);
  const stale = at(row(1, 5, 12), 7);

  const framesFirst = m.listed(m.created(m.created(read, 11, false), 12, false), stale);
  assert.equal(m.unreadCount(framesFirst), 2);

  const listFirst = m.listed(read, stale);
  assert.equal(m.unreadCount(listFirst), 0);
  assert.equal(m.unreadCount(m.created(m.created(listFirst, 11, false), 12, false)), 2);

  // Once the read has reached the server, its count is the count.
  assert.equal(m.unreadCount(m.listed(framesFirst, at(row(1, 2, 12), 10))), 2);
  assert.equal(m.unreadCount(m.listed(framesFirst, at(row(1, 0, 12), 12))), 0);
});

test('a row without a cursor never poisons the count', () => {
  const s = m.listed(m.markedRead(m.listed(undefined, row(1, 3, 10)), 10), row(1, 2, 12));
  assert.equal(m.unreadCount(s), 0);
  assert.equal(s.read, 10);
});

test('a read made on another device clears the count at the next list', () => {
  const s = m.created(m.listed(undefined, row(1, 3, 10)), 11, false);
  assert.equal(m.unreadCount(s), 4);
  assert.equal(m.unreadCount(m.listed(s, row(1, 0, 11))), 0);
});

test('reading on arrival, or posting, reads everything up to that message', () => {
  let s = m.created(m.listed(undefined, row(1, 3, 10)), 11, false);
  s = m.created(s, 12, true);
  assert.equal(m.unreadCount(s), 0);
  assert.equal(m.unreadCount(m.created(s, 11, false)), 0);
  assert.equal(m.unreadCount(m.created(s, 13, false)), 1);
});

test('deleting an unread arrival uncounts it, and only it', () => {
  const s = m.created(m.created(m.listed(undefined, row(1, 1, 10)), 11, false), 12, false);
  assert.equal(m.unreadCount(m.deleted(s, 12)), 2);
  assert.equal(m.unreadCount(m.deleted(m.deleted(s, 12), 12)), 2);
  assert.equal(m.unreadCount(m.deleted(s, 10)), 3);
  assert.equal(m.deleted(undefined, 5), undefined);
});

test('marking read covers a newest message that was deleted after it arrived', () => {
  const s = m.deleted(m.created(m.listed(undefined, row(1, 0, 10)), 11, false), 11);
  assert.equal(m.markedRead(s, 10).read, 11);
});

test('a refused read lets the next list restore the count', () => {
  const before = m.listed(undefined, row(1, 3, 10));
  const s = m.readRefused(m.markedRead(before, 10), 10, before.read);
  assert.equal(m.unreadCount(m.listed(s, row(1, 3, 10))), 3);
  const later = m.markedRead(m.markedRead(before, 10), 12);
  assert.equal(m.readRefused(later, 10, before.read).read, 12);
});

test('the store counts a message once whichever of list and frame lands first', async () => {
  const st = m.useChannels.getState();
  await load([row(1, 2, 10)]);
  st.onMessageCreate(message(1, 10), false);
  assert.equal(unreadOf(1), 2);

  const done = st.refresh();
  st.onMessageCreate(message(1, 11), false);
  answerNextList([row(1, 2, 10)]);
  await done;
  assert.equal(unreadOf(1), 3);

  await load([row(1, 3, 11)]);
  assert.equal(unreadOf(1), 3);
});

test('a list that lands after a newer one is dropped', async () => {
  const st = m.useChannels.getState();
  const older = st.refresh();
  const newer = st.refresh();
  answerNextList([row(1, 0, 12)], 1);
  await newer;
  answerNextList([row(1, 2, 12)], 0);
  await older;
  assert.equal(unreadOf(1), 0);
});

test('a read racing a list stays read, and sends the highest seq it knows', async () => {
  const st = m.useChannels.getState();
  await load([row(1, 0, 10)]);
  st.onMessageCreate(message(1, 11), false);
  st.onMessageDelete(1, 11);
  st.onMessageCreate(message(1, 12), false);
  const done = st.refresh();
  await st.markRead(1, 10);
  assert.deepEqual(reads, [[1, 12]]);
  answerNextList([row(1, 2, 12)]);
  await done;
  assert.equal(unreadOf(1), 0);
  assert.equal(m.tabTitle(m.useChannels.getState().channels), 'Disjorn');
});

test('a read the server refused comes back with the next list', async () => {
  const st = m.useChannels.getState();
  await load([row(1, 3, 10)]);
  readStatus = 503;
  await st.markRead(1, 10);
  assert.equal(unreadOf(1), 0);
  await load([row(1, 3, 10)]);
  assert.equal(unreadOf(1), 3);
});

test('a list that predates the read counts only the message that landed after it', async () => {
  const st = m.useChannels.getState();
  await load([at(row(1, 3, 10), 7)]);
  const done = st.refresh();
  await st.markRead(1, 10);
  st.onMessageCreate(message(1, 11), false);
  answerNextList([at(row(1, 4, 11), 7)]);
  await done;
  assert.equal(unreadOf(1), 1);
  assert.equal(m.tabTitle(m.useChannels.getState().channels), '(1) Disjorn');

  await load([at(row(1, 1, 11), 10)]);
  assert.equal(unreadOf(1), 1);
});

test('a read the server stored lower than asked does not outrank the next list', async () => {
  const st = m.useChannels.getState();
  await load([row(1, 3, 10)]);
  storedAs = () => 8;
  await st.markRead(1, 10);
  assert.equal(m.useChannels.getState().unreadState[1].read, 8);
  await load([at(row(1, 1, 10), 8)]);
  assert.equal(unreadOf(1), 1);
});

test('a channel that left the list takes its bookkeeping with it', async () => {
  const st = m.useChannels.getState();
  await load([row(1, 0, 10), row(2, 0, 5)]);
  await st.markRead(2, 5);
  st.removeChannel(2);
  assert.deepEqual(Object.keys(m.useChannels.getState().unreadState), ['1']);
  await load([row(1, 0, 10), row(2, 5, 5)]);
  assert.equal(unreadOf(2), 5);
});

test('the socket counts others\' messages, not mine, and uncounts a deleted one', async () => {
  await load([row(1, 0, 10), row(9, 0, 3, 'app_build')]);
  const socket = new m.DisjornSocket();
  socket.connect();
  const frame = (f) => FakeSocket.made[0].onmessage({ data: JSON.stringify(f) });
  frame({ type: 'ready' });

  frame({ type: 'message_create', message: message(1, 11) });
  frame({ type: 'message_create', message: message(1, 12) });
  assert.equal(unreadOf(1), 2);
  frame({ type: 'message_delete', channel_id: 1, id: 1012, seq: 12 });
  assert.equal(unreadOf(1), 1);

  frame({ type: 'message_create', message: message(1, 13, ALICE.id) });
  frame({ type: 'channel_read', channel_id: 1, last_read_seq: 13 });
  await settle();
  assert.equal(unreadOf(1), 0);
  assert.deepEqual(reads, []);

  frame({ type: 'message_create', message: message(9, 4) });
  assert.equal(unreadOf(9), 1);
  assert.equal(m.tabTitle(m.useChannels.getState().channels), 'Disjorn');
  socket.disconnect();
});

function openSocket() {
  const socket = new m.DisjornSocket();
  socket.connect();
  const frame = (f) => FakeSocket.made[0].onmessage({ data: JSON.stringify(f) });
  frame({ type: 'ready' });
  return { socket, frame };
}

const title = () => m.tabTitle(m.useChannels.getState().channels);

test('a read on another device leaves the messages above it; an older one is ignored', () => {
  let s = m.listed(undefined, row(1, 2, 10));
  for (const seq of [11, 12, 13]) s = m.created(s, seq, false);
  const read = m.readThrough(s, 12);
  assert.equal(m.unreadCount(read), 1);
  assert.equal(m.readThrough(read, 12), read);
  assert.equal(m.readThrough(read, 11), read);
  assert.equal(m.readThrough(undefined, 5), undefined);
});

test('a channel_read frame drops the badge and the title on a tab nobody is looking at', async () => {
  await load([row(1, 2, 10), row(2, 1, 4)]);
  const { socket, frame } = openSocket();
  frame({ type: 'message_create', message: message(1, 11) });
  frame({ type: 'message_create', message: message(1, 12) });
  assert.equal(title(), '(5) Disjorn');

  frame({ type: 'channel_read', channel_id: 1, last_read_seq: 11 });
  assert.equal(unreadOf(1), 1);
  assert.equal(title(), '(2) Disjorn');
  frame({ type: 'channel_read', channel_id: 1, last_read_seq: 10 });
  assert.equal(unreadOf(1), 1);
  frame({ type: 'channel_read', channel_id: 1, last_read_seq: 12 });
  assert.equal(title(), '(1) Disjorn');
  frame({ type: 'channel_read', channel_id: 7, last_read_seq: 3 });

  assert.deepEqual(reads, []);
  assert.equal(lists.length, 0);
  socket.disconnect();
});

test('a read inside the range the list counted asks the server what is left', async () => {
  const st = m.useChannels.getState();
  await load([row(1, 3, 10)]);
  st.onChannelRead(1, 9);
  assert.equal(lists.length, 1);
  answerNextList([at(row(1, 1, 10), 9)]);
  await settle();
  assert.equal(unreadOf(1), 1);
});

test('a channel is being read only with its list on screen, uncovered and at the bottom', () => {
  const facts = { windowFocused: true, activeChannelId: 1, focusedChannelId: 1,
    buildModalOpen: false, atBottom: true };
  assert.equal(m.isReading(1, facts), true);
  assert.equal(m.isReading(2, facts), false);
  for (const off of [{ windowFocused: false }, { activeChannelId: 2 },
    { focusedChannelId: null }, { focusedChannelId: 9 }, { buildModalOpen: true },
    { atBottom: false }]) {
    assert.equal(m.isReading(1, { ...facts, ...off }), false, JSON.stringify(off));
  }
});

test('a message is read on arrival only in front of the reader, and later once it is', async () => {
  await load([row(1, 0, 10)]);
  const st = m.useChannels.getState();
  st.setActive(1);
  focused = true;
  const { socket, frame } = openSocket();
  socket.sendFocus(1);
  st.setAtBottom(1, true);
  let seq = 10;
  const sent = [];

  frame({ type: 'message_create', message: message(1, ++seq) });
  await settle();
  sent.push([1, seq]);
  assert.equal(unreadOf(1), 0);
  assert.deepEqual(reads, sent);

  /* Each pair hides the newest message from the reader, then shows it again:
     scrolled up, Settings or the Plan Room, the build modal, another window. */
  const covers = [
    [() => st.setAtBottom(1, false), () => st.setAtBottom(1, true)],
    [() => socket.sendFocus(null), () => socket.sendFocus(1)],
    [() => m.useApps.setState({ openBuildSessionId: 5 }),
      () => m.useApps.setState({ openBuildSessionId: null })],
    [() => { focused = false; }, () => { focused = true; }],
  ];
  for (const [cover, uncover] of covers) {
    cover();
    frame({ type: 'message_create', message: message(1, ++seq) });
    await settle();
    assert.equal(unreadOf(1), 1);
    assert.equal(title(), '(1) Disjorn');
    assert.equal(socket.readActive(), false);
    assert.deepEqual(reads, sent);

    uncover();
    assert.equal(socket.readActive(), true);
    await settle();
    sent.push([1, seq]);
    assert.equal(unreadOf(1), 0);
    assert.equal(title(), 'Disjorn');
    assert.deepEqual(reads, sent);
    assert.equal(socket.readActive(), false);
  }
  socket.disconnect();
});

/* A phone comes back on a dead socket: the focus handler looks while the count
   is still the stale 0, and only the list that follows carries the message
   that arrived meanwhile. `cover` hides the channel from the reader first. */
async function resume(cover = () => {}) {
  await load([at(row(1, 0, 10), 10), row(2, 0, 4)]);
  const st = m.useChannels.getState();
  st.setActive(1);
  focused = true;
  m.socket.connect();
  FakeSocket.made[0].onmessage({ data: JSON.stringify({ type: 'ready' }) });
  m.socket.sendFocus(1);
  st.setAtBottom(1, true);
  cover(st);
  assert.equal(m.socket.readActive(), false);
  assert.deepEqual(reads, []);
  await load([at(row(1, 1, 11), 10), row(2, 0, 4)]);
  await settle();
}

test('a list that brings unread for the channel in front of the reader reads it', async () => {
  await resume();
  assert.deepEqual(reads, [[1, 11]]);
  assert.equal(unreadOf(1), 0);
  assert.equal(title(), 'Disjorn');

  await load([at(row(1, 0, 11), 11), row(2, 0, 4)]);
  await settle();
  assert.deepEqual(reads, [[1, 11]]);
  assert.equal(title(), 'Disjorn');
});

test('a list that brings unread for a channel the reader cannot see leaves it unread', async () => {
  const covers = {
    'scrolled up': (st) => st.setAtBottom(1, false),
    'under Settings or the Plan Room': () => m.socket.sendFocus(null),
    'under the build modal': () => m.useApps.setState({ openBuildSessionId: 5 }),
    'window not focused': () => { focused = false; },
    'another channel open': (st) => {
      st.setActive(2);
      m.socket.sendFocus(2);
      st.setAtBottom(2, true);
    },
  };
  for (const [name, cover] of Object.entries(covers)) {
    fresh();
    await resume(cover);
    assert.deepEqual(reads, [], name);
    assert.equal(unreadOf(1), 1, name);
    assert.equal(title(), '(1) Disjorn', name);
  }
});

test('the list a reconnect fetches reads the open channel too', async (t) => {
  await resume((st) => st.setAtBottom(1, false));
  m.useChannels.getState().setAtBottom(1, true);
  const channelLists = globalThis.fetch;
  globalThis.fetch = (path, init) => (path === '/channels' || init.method !== 'GET'
    ? channelLists(path, init)
    : Promise.resolve({ ok: true, status: 200, statusText: '',
      json: async () => ({ apps: [], quota: null, proposals: [], truncated: false }) }));

  t.mock.timers.enable({ apis: ['setTimeout'] });
  FakeSocket.made[0].onclose({ code: 1006 });
  t.mock.timers.tick(2_000);
  FakeSocket.made[1].onmessage({ data: JSON.stringify({ type: 'ready' }) });
  await settle();
  answerNextList([at(row(1, 1, 11), 10), row(2, 0, 4)]);
  await settle();
  assert.deepEqual(reads, [[1, 11]]);
  assert.equal(title(), 'Disjorn');
});

test('the list a partial read on another device asks for reads the open channel too', async () => {
  await resume((st) => st.setAtBottom(1, false));
  const st = m.useChannels.getState();
  await load([at(row(1, 3, 13), 10), row(2, 0, 4)]);
  st.setAtBottom(1, true);
  st.onChannelRead(1, 12);
  answerNextList([at(row(1, 1, 13), 12), row(2, 0, 4)]);
  await settle();
  await settle();
  assert.deepEqual(reads, [[1, 13]]);
  assert.equal(title(), 'Disjorn');
});
