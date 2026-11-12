// The /stt client call: its timeout and the errors it hands the mic button,
// bundled for node:
//
//     node --test client/tests/stt.test.mjs

import { test, before, after, afterEach } from 'node:test';
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
export { ApiError, transcribeAudio, transcribeTimeoutMs } from "../src/api";
`;

let m;
let scratch;
const realFetch = globalThis.fetch;

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
  scratch = mkdtempSync(join(tmpdir(), 'stt-'));
  const file = join(scratch, 'bundle.mjs');
  writeFileSync(file, out.outputFiles[0].text);
  m = await import(pathToFileURL(file).href);
});

after(() => { if (scratch) rmSync(scratch, { recursive: true, force: true }); });
afterEach(() => { globalThis.fetch = realFetch; });

const clip = () => new Blob(['opus'], { type: 'audio/webm' });

test('the wait has a floor for a loading model and grows with the recording', () => {
  assert.equal(m.transcribeTimeoutMs(0), 60_000);
  assert.equal(m.transcribeTimeoutMs(15_000), 90_000);
  assert.equal(m.transcribeTimeoutMs(-5), 60_000);
});

test('a server that never answers is given up on, with a sentence saying so', async () => {
  let signal;
  globalThis.fetch = (_url, init) => new Promise((_resolve, reject) => {
    signal = init.signal;
    signal.addEventListener('abort', () => reject(new Error('aborted')));
  });
  await assert.rejects(m.transcribeAudio(clip(), 'voice.webm', 20), (err) => {
    assert.ok(err instanceof m.ApiError);
    assert.equal(err.status, 0);
    assert.match(err.detail, /timed out/);
    return true;
  });
  assert.equal(signal.aborted, true);
});

test('a dropped connection is not reported as a timeout', async () => {
  globalThis.fetch = () => Promise.reject(new TypeError('fetch failed'));
  await assert.rejects(m.transcribeAudio(clip(), 'voice.webm', 5_000), (err) => {
    assert.equal(err.status, 0);
    assert.match(err.detail, /Network error/);
    return true;
  });
});

test("the server's own sentence reaches the caller with its status", async () => {
  globalThis.fetch = async () => new Response(
    JSON.stringify({ detail: 'That recording could not be read — try recording again' }),
    { status: 400, headers: { 'content-type': 'application/json' } },
  );
  await assert.rejects(m.transcribeAudio(clip(), 'voice.webm'), (err) => {
    assert.equal(err.status, 400);
    assert.equal(err.detail, 'That recording could not be read — try recording again');
    return true;
  });
});

test('an answer in time is returned and the upload carries the recording', async () => {
  let sent;
  globalThis.fetch = async (url, init) => {
    sent = { url, init };
    return new Response(JSON.stringify({ text: 'hello there' }), { status: 200 });
  };
  assert.deepEqual(await m.transcribeAudio(clip(), 'voice.webm', 50), { text: 'hello there' });
  assert.equal(sent.url, '/stt');
  assert.equal(sent.init.body.get('audio').name, 'voice.webm');
  await new Promise((resolve) => setTimeout(resolve, 80));
  assert.equal(sent.init.signal.aborted, false);
});
