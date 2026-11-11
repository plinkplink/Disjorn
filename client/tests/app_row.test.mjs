// Which verb an app row and its card offer, bundled for node:
//
//     node --test client/tests/app_row.test.mjs

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
export { appRowAction, canKeepBuilding } from "../src/lib/appRow";
`;

let m;
let scratch;

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
  scratch = mkdtempSync(join(tmpdir(), 'app-row-'));
  const file = join(scratch, 'bundle.mjs');
  writeFileSync(file, out.outputFiles[0].text);
  m = await import(pathToFileURL(file).href);
});

after(() => { if (scratch) rmSync(scratch, { recursive: true, force: true }); });

const ALICE = 1;
const BOB = 2;
const SESSION = { id: 7, channel_id: 70, stage: null, locked_until: '2099-01-01T00:00:00Z' };

function app(status, open_session = null) {
  return { id: 'abc234567xyz', name: 'Tips', status, owner_user_id: ALICE, open_session };
}

test("the owner's draft row starts a session and their live row shows the card", () => {
  assert.deepEqual(m.appRowAction(app('draft'), ALICE), { kind: 'start' });
  assert.deepEqual(m.appRowAction(app('live'), ALICE), { kind: 'card' });
});

test('the owner resumes an open session at any status', () => {
  for (const status of ['draft', 'live']) {
    assert.deepEqual(m.appRowAction(app(status, SESSION), ALICE),
      { kind: 'resume', sessionId: 7 });
  }
});

test("somebody else's row is the card, even while its owner is building", () => {
  for (const viewer of [BOB, null]) {
    assert.deepEqual(m.appRowAction(app('live', SESSION), viewer), { kind: 'card' });
    assert.deepEqual(m.appRowAction(app('draft'), viewer), { kind: 'card' });
  }
});

test('Keep building is offered to the owner of a live app and to nobody else', () => {
  assert.equal(m.canKeepBuilding(app('live'), ALICE), true);
  assert.equal(m.canKeepBuilding(app('draft'), ALICE), false);
  assert.equal(m.canKeepBuilding(app('archived'), ALICE), false);
  assert.equal(m.canKeepBuilding(app('live'), BOB), false);
  assert.equal(m.canKeepBuilding(app('live'), null), false);
});
