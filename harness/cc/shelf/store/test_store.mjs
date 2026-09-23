// test_store.mjs — the house store's own tests. Run:
//
//     node --test harness/cc/shelf/store/test_store.mjs
//
// (An explicit path: node's test-file discovery patterns do not match the name
// `test_store.mjs`, and `node --test <dir>` is not a directory walk in v22.)
//
// No framework, no fixtures — a Map-backed localStorage stub and node:test.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';

import { houseStore, STORE_VERSION, StoreScopeNotAvailable } from './house-store.mjs';
import { render, MARKER, CLASSIC, ESM } from './build.mjs';

const APP = 'abcdefghijkm'; // 12 chars, all in [a-z2-7]
const OTHER = 'zyxwvutsr234';

/** The three localStorage members the shim uses, and nothing else. */
function stubStorage(initial = {}) {
  const map = new Map(Object.entries(initial));
  return {
    get length() { return map.size; },
    key: (i) => [...map.keys()][i] ?? null,
    getItem: (k) => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => void map.set(String(k), String(v)),
    removeItem: (k) => void map.delete(k),
    _map: map,
  };
}

/** Fresh stub on globalThis for one test; the shim reads it per call. */
function withStorage(initial = {}) {
  const ls = stubStorage(initial);
  globalThis.localStorage = ls;
  return ls;
}

test('version is 1, on the module and on the function', () => {
  assert.equal(STORE_VERSION, 1);
  assert.equal(houseStore.STORE_VERSION, 1);
});

test('user scope round-trips JSON values', () => {
  withStorage();
  const s = houseStore(APP);
  for (const value of [0, 12, -1.5, '', 'hi', true, false, null, [1, 2, 'x'], { a: { b: [null] } }]) {
    s.set('user', 'k', value);
    assert.deepEqual(s.get('user', 'k'), value);
  }
});

test('user scope writes exactly disjorn:<appId>:user:<key>', () => {
  const ls = withStorage();
  houseStore(APP).set('user', 'high score', { n: 3 });
  assert.deepEqual([...ls._map.entries()], [['disjorn:abcdefghijkm:user:high score', '{"n":3}']]);
});

test('a missing key reads undefined, not null', () => {
  withStorage();
  assert.equal(houseStore(APP).get('user', 'nope'), undefined);
});

test('a stored null is not a missing key', () => {
  withStorage();
  const s = houseStore(APP);
  s.set('user', 'k', null);
  assert.equal(s.get('user', 'k'), null);
});

test('del removes one key and leaves the rest', () => {
  withStorage();
  const s = houseStore(APP);
  s.set('user', 'a', 1);
  s.set('user', 'b', 2);
  s.del('user', 'a');
  assert.equal(s.get('user', 'a'), undefined);
  assert.equal(s.get('user', 'b'), 2);
  s.del('user', 'gone-already'); // no throw
});

test('keys lists this app un-prefixed and sorted, ignoring everything else', () => {
  withStorage({
    'disjorn:abcdefghijkm:user:zed': '1',
    'disjorn:abcdefghijkm:user:alpha': '1',
    'disjorn:zyxwvutsr234:user:other-app': '1',
    'disjorn:abcdefghijkm:app:someday': '1',
    'unrelated': '1',
  });
  assert.deepEqual(houseStore(APP).keys('user'), ['alpha', 'zed']);
  assert.deepEqual(houseStore(OTHER).keys('user'), ['other-app']);
});

test('two apps cannot see each other', () => {
  withStorage();
  houseStore(APP).set('user', 'k', 'mine');
  assert.equal(houseStore(OTHER).get('user', 'k'), undefined);
  assert.deepEqual(houseStore(OTHER).keys('user'), []);
});

test('app scope throws StoreScopeNotAvailable from every method', () => {
  withStorage();
  const s = houseStore(APP);
  for (const call of [() => s.get('app', 'k'), () => s.set('app', 'k', 1), () => s.del('app', 'k'), () => s.keys('app')]) {
    assert.throws(call, (err) => {
      assert.equal(err.name, 'StoreScopeNotAvailable');
      assert.equal(err.message, 'app scope arrives with server-side storage');
      assert.ok(err instanceof StoreScopeNotAvailable);
      assert.ok(err instanceof Error);
      return true;
    });
  }
});

test('app scope throws the scope error even with no localStorage at all', () => {
  delete globalThis.localStorage;
  assert.throws(() => houseStore(APP).get('app', 'k'), { name: 'StoreScopeNotAvailable' });
  assert.throws(() => houseStore(APP).get('user', 'k'), { message: /localStorage is not available/ });
});

test('any other scope is a RangeError', () => {
  withStorage();
  const s = houseStore(APP);
  for (const scope of ['User', 'session', '', 'users', null, undefined, 1, {}]) {
    assert.throws(() => s.get(scope, 'k'), RangeError);
    assert.throws(() => s.set(scope, 'k', 1), RangeError);
    assert.throws(() => s.del(scope, 'k'), RangeError);
    assert.throws(() => s.keys(scope), RangeError);
  }
});

test('app id must match ^[a-z2-7]{12}$', () => {
  withStorage();
  const bad = [
    'abcdefghijk',    // 11
    'abcdefghijkmn',  // 13
    'ABCDEFGHIJKM',   // upper
    'abcdefghijk0',   // 0 is not base32
    'abcdefghijk1',
    'abcdefghijk8',
    'abcdefghijk9',
    'abcdefghij-m',
    'abcdefghijk\n',
    'abcdefghijkm\nabcdefghijkm',
    '',
    null,
    undefined,
    12,
    ['abcdefghijkm'],
  ];
  for (const id of bad) assert.throws(() => houseStore(id), RangeError, `accepted ${JSON.stringify(id)}`);
  assert.doesNotThrow(() => houseStore('a2b3c4d5e6f7'));
});

test('keys must be non-empty strings', () => {
  withStorage();
  const s = houseStore(APP);
  for (const key of ['', null, undefined, 3, {}]) {
    assert.throws(() => s.get('user', key), RangeError);
    assert.throws(() => s.set('user', key, 1), RangeError);
    assert.throws(() => s.del('user', key), RangeError);
  }
});

test('undefined is not a storable value', () => {
  withStorage();
  assert.throws(() => houseStore(APP).set('user', 'k', undefined), TypeError);
});

test('the classic build sets window.houseStore and behaves the same', async () => {
  const source = await readFile(CLASSIC, 'utf8');
  const sandbox = { window: {}, localStorage: stubStorage() };
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox, { filename: 'house-store.js' });

  assert.equal(typeof sandbox.window.houseStore, 'function');
  assert.equal(sandbox.window.STORE_VERSION, 1);
  assert.equal(sandbox.window.houseStore.STORE_VERSION, 1);

  const s = sandbox.window.houseStore(APP);
  s.set('user', 'k', { ok: true });
  // JSON, not deepEqual: objects made inside the vm have a different realm's prototype.
  assert.equal(JSON.stringify(s.get('user', 'k')), '{"ok":true}');
  assert.deepEqual([...sandbox.localStorage._map.keys()], ['disjorn:abcdefghijkm:user:k']);
  assert.throws(() => s.get('app', 'k'), { name: 'StoreScopeNotAvailable' });
  assert.throws(() => s.get('nope', 'k'), { name: 'RangeError' });
});

test('the two builds are byte-identical above the build tail', async () => {
  const [classic, esm] = await Promise.all([readFile(CLASSIC, 'utf8'), readFile(ESM, 'utf8')]);
  const head = (src) => src.slice(0, src.indexOf(MARKER) + MARKER.length);
  assert.notEqual(classic.indexOf(MARKER), -1);
  assert.notEqual(esm.indexOf(MARKER), -1);
  assert.equal(head(esm), head(classic));

  // …and the committed .mjs is exactly what build.mjs would write today.
  assert.equal(esm, render(classic));

  // The differing tails say what they should and nothing more.
  assert.match(classic.slice(classic.indexOf(MARKER)), /window\.houseStore = houseStore;/);
  assert.doesNotMatch(classic, /^export /m);
  assert.match(esm.slice(esm.indexOf(MARKER)), /^export \{ houseStore, STORE_VERSION, StoreScopeNotAvailable \};$/m);
  assert.doesNotMatch(esm.slice(esm.indexOf(MARKER)), /window\./);
});
