// The sidebar's collapsible sections and the store that remembers them,
// bundled for node:
//
//     node --test client/tests/sidebar_sections.test.mjs

import { test, before, after } from 'node:test';
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
export * from "../src/lib/sidebarSections";
export { useSidebarSections } from "../src/sidebar";
`;

let m;
let scratch;
let bundle;
let loads = 0;

before(async () => {
  const nm = nodeModules();
  const bin = join(nm, '@esbuild', `${process.platform}-${process.arch}`, 'bin', 'esbuild');
  if (!process.env.ESBUILD_BINARY_PATH && existsSync(bin)) process.env.ESBUILD_BINARY_PATH = bin;
  const esbuild = createRequire(import.meta.url)(join(nm, 'esbuild'));
  const out = await esbuild.build({
    stdin: { contents: ENTRY, resolveDir: HERE, loader: 'js' },
    bundle: true, platform: 'node', format: 'esm', write: false,
    nodePaths: [nm], logLevel: 'silent',
  });
  scratch = mkdtempSync(join(tmpdir(), 'sidebar-sections-'));
  bundle = join(scratch, 'bundle.mjs');
  writeFileSync(bundle, out.outputFiles[0].text);
  m = await bootWith(memoryStorage());
});

after(() => {
  delete globalThis.localStorage;
  if (scratch) rmSync(scratch, { recursive: true, force: true });
});

function memoryStorage(initial = {}) {
  const data = new Map(Object.entries(initial));
  return {
    data,
    getItem: (k) => (data.has(k) ? data.get(k) : null),
    setItem: (k, v) => { data.set(k, String(v)); },
  };
}

// The store reads storage once, at import: a fresh module is a page load.
async function bootWith(storage) {
  globalThis.localStorage = storage;
  loads += 1;
  return import(`${pathToFileURL(bundle).href}?load=${loads}`);
}

const channel = (id, unread = 0) => ({ id, type: 'text', unread });
const dm = (id, unread = 0) => ({ id, type: 'dm_1to1', unread });
const ids = (rows) => rows.map((r) => r.id);
const unreadOf = (rows) => rows.reduce((sum, r) => sum + r.unread, 0);

test('an open section shows every row and carries no badge', () => {
  const rows = [channel(1, 4), channel(2), channel(3, 1)];
  assert.deepEqual(ids(m.visibleRows(rows, false, 2)), [1, 2, 3]);
  assert.equal(m.collapsedBadge(rows, false, 2), 0);
});

test('a folded section keeps the row the reader is on', () => {
  const rows = [channel(1), channel(2), channel(3)];
  assert.deepEqual(ids(m.visibleRows(rows, true, 2)), [2]);
  assert.equal(m.collapsedBadge(rows, true, 2), 0);
});

test('a folded section hides every row when the reader is elsewhere', () => {
  const rows = [channel(1), channel(2)];
  assert.deepEqual(ids(m.visibleRows(rows, true, 9)), []);
  assert.deepEqual(ids(m.visibleRows(rows, true, null)), []);
});

test('unread in a folded channel goes to the header, not to a row', () => {
  const rows = [channel(1, 4), channel(2), channel(3, 1)];
  assert.deepEqual(ids(m.visibleRows(rows, true, 2)), [2]);
  assert.equal(m.collapsedBadge(rows, true, 2), 5);
});

test('an unread DM stays in sight under a folded header', () => {
  const rows = [dm(1, 2), dm(2), dm(3), dm(4, 1)];
  assert.deepEqual(ids(m.visibleRows(rows, true, 3)), [1, 3, 4]);
  assert.equal(m.collapsedBadge(rows, true, 3), 0);
});

test('a folded section counts each unread message once across rows and header', () => {
  const sections = [
    [channel(1, 4), channel(2, 7), channel(3)],
    [dm(4, 2), dm(5), dm(6, 150)],
    [],
  ];
  for (const rows of sections) {
    for (const activeId of [null, ...ids(rows), 99]) {
      const shown = unreadOf(m.visibleRows(rows, true, activeId));
      assert.equal(shown + m.collapsedBadge(rows, true, activeId), unreadOf(rows));
    }
  }
});

test('a large count is capped the way a row badge is', () => {
  assert.equal(m.badgeText(1), '1');
  assert.equal(m.badgeText(99), '99');
  assert.equal(m.badgeText(100), '99+');
});

test('nothing stored means nothing folded', () => {
  assert.deepEqual(m.parseCollapsed(null), m.ALL_OPEN);
  assert.deepEqual(m.ALL_OPEN, { channels: false, apps: false, dms: false });
});

test('corrupt storage reads as nothing folded', () => {
  for (const raw of ['', '{', 'null', '7', '"dms"', '{"dms":true}', 'true']) {
    assert.deepEqual(m.parseCollapsed(raw), m.ALL_OPEN, raw);
  }
});

test('unknown entries in the stored list are ignored', () => {
  assert.deepEqual(m.parseCollapsed('["dms","voice",3,null,["apps"]]'),
    { channels: false, apps: false, dms: true });
});

test('every combination of folded sections survives a round trip', () => {
  for (const channels of [false, true]) {
    for (const apps of [false, true]) {
      for (const dms of [false, true]) {
        const state = { channels, apps, dms };
        assert.deepEqual(m.parseCollapsed(m.serializeCollapsed(state)), state);
      }
    }
  }
});

test('folding one section leaves the others as they were', () => {
  const start = { channels: false, apps: true, dms: false };
  const next = m.toggled(start, 'dms');
  assert.deepEqual(next, { channels: false, apps: true, dms: true });
  assert.deepEqual(start, { channels: false, apps: true, dms: false });
  assert.deepEqual(m.toggled(next, 'dms'), start);
});

test('a fold is written under the versioned key and read back on the next load', async () => {
  const storage = memoryStorage();
  const first = await bootWith(storage);
  assert.equal(first.SECTIONS_STORAGE_KEY, 'disjorn.sidebar.collapsed.v1');
  assert.deepEqual(first.useSidebarSections.getState().collapsed, first.ALL_OPEN);
  first.useSidebarSections.getState().toggle('apps');
  first.useSidebarSections.getState().toggle('dms');
  assert.equal(storage.data.get(first.SECTIONS_STORAGE_KEY), '["apps","dms"]');

  const second = await bootWith(storage);
  assert.deepEqual(second.useSidebarSections.getState().collapsed,
    { channels: false, apps: true, dms: true });
  second.useSidebarSections.getState().toggle('apps');
  assert.equal(storage.data.get(second.SECTIONS_STORAGE_KEY), '["dms"]');
});

test('a device with corrupt storage starts with every section open', async () => {
  const loaded = await bootWith(memoryStorage({ 'disjorn.sidebar.collapsed.v1': '{oops' }));
  assert.deepEqual(loaded.useSidebarSections.getState().collapsed, loaded.ALL_OPEN);
});

test('storage that refuses reads and writes still folds for the session', async () => {
  const refusing = {
    getItem() { throw new Error('denied'); },
    setItem() { throw new Error('quota'); },
  };
  const loaded = await bootWith(refusing);
  assert.deepEqual(loaded.useSidebarSections.getState().collapsed, loaded.ALL_OPEN);
  loaded.useSidebarSections.getState().toggle('channels');
  assert.equal(loaded.useSidebarSections.getState().collapsed.channels, true);
});

test('a page with no localStorage at all still loads', async () => {
  const loaded = await bootWith(undefined);
  assert.deepEqual(loaded.useSidebarSections.getState().collapsed, loaded.ALL_OPEN);
  loaded.useSidebarSections.getState().toggle('dms');
  assert.equal(loaded.useSidebarSections.getState().collapsed.dms, true);
});
