// The unfurl cache's retry and dead-image rules, and the card drawn from it:
//
//     node --test client/tests/unfurl.test.mjs

import { test, before, after, beforeEach, afterEach } from 'node:test';
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
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { UnfurlCard } from "../src/components/UnfurlCard";
import {
  DEAD_IMAGE_MS, cardImage, retryDelayMs, settled, shouldFetch, useUnfurl,
} from "../src/stores/unfurl";

export { DEAD_IMAGE_MS, cardImage, retryDelayMs, settled, shouldFetch, useUnfurl };

export function card(url) {
  // A server render reads the store's initial state, so the live state goes there.
  useUnfurl.getInitialState().byUrl = useUnfurl.getState().byUrl;
  return renderToStaticMarkup(createElement(UnfurlCard, { url, onSummarize: () => {} }));
}
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
    jsx: 'automatic', nodePaths: [nm], logLevel: 'silent',
    define: { 'process.env.NODE_ENV': '"production"' },
    banner: { js: "import { createRequire } from 'node:module';"
      + ' const require = createRequire(import.meta.url);' },
  });
  scratch = mkdtempSync(join(tmpdir(), 'unfurl-'));
  const file = join(scratch, 'bundle.mjs');
  writeFileSync(file, out.outputFiles[0].text);
  m = await import(pathToFileURL(file).href);
});

after(() => { if (scratch) rmSync(scratch, { recursive: true, force: true }); });

const LINK = 'https://example.com/reel';
const OLD = 'https://cdn.example/old.jpg?oe=1';
const NEW = 'https://cdn.example/new.jpg?oe=2';
const page = (image_url, title = 'A reel') =>
  ({ url: LINK, title, description: null, image_url });
const EMPTY = page(null, null);

const realFetch = globalThis.fetch;
const realNow = Date.now;
let clock;
let asked;

// Each queued answer is a body (200) or a status number; the last one repeats.
function server(...answers) {
  globalThis.fetch = async (path) => {
    asked.push(path);
    const a = answers.length > 1 ? answers.shift() : answers[0];
    return typeof a === 'number'
      ? new Response('{}', { status: a })
      : new Response(JSON.stringify(a), { status: 200 });
  };
}

const settle = () => new Promise((resolve) => setImmediate(resolve));
const entry = () => m.useUnfurl.getState().byUrl[LINK];

beforeEach(() => {
  clock = 1_000_000;
  asked = [];
  Date.now = () => clock;
  m.useUnfurl.setState({ byUrl: {} });
});

afterEach(() => {
  globalThis.fetch = realFetch;
  Date.now = realNow;
});

test('the retry backoff starts at thirty seconds and stops growing at thirty minutes', () => {
  assert.deepEqual([1, 2, 3, 4, 9].map(m.retryDelayMs),
    [30_000, 120_000, 480_000, 1_800_000, 1_800_000]);
});

test('an entry with a card is never fetched again; one without is, once its retry is due', () => {
  const hit = m.settled(undefined, page(OLD), 0);
  const miss = m.settled(undefined, EMPTY, 0);
  const failed = m.settled(undefined, null, 0);
  assert.equal(m.shouldFetch(undefined, 0), true);
  assert.equal(m.shouldFetch(hit, 1e12), false);
  for (const e of [miss, failed]) {
    assert.equal(m.shouldFetch(e, 29_999), false);
    assert.equal(m.shouldFetch(e, 30_000), true);
    assert.equal(m.shouldFetch({ ...e, status: 'loading' }, 30_000), false);
  }
  assert.equal(m.settled(miss, EMPTY, 0).retryAt, 120_000);
  assert.equal(m.settled(miss, page(OLD), 0).misses, 0);
});

test('a failed fetch is tried again after the backoff and then draws the card', async () => {
  server(500, page(OLD));
  const { ensure } = m.useUnfurl.getState();
  ensure(LINK);
  await settle();
  assert.equal(entry().status, 'error');
  assert.equal(m.card(LINK), '');

  ensure(LINK);
  assert.equal(asked.length, 1);

  clock += 30_000;
  ensure(LINK);
  await settle();
  assert.equal(asked.length, 2);
  assert.match(m.card(LINK), /A reel/);
  assert.equal(entry().retryAt, null);
});

test('an unfurl that came back empty is asked for again, not kept for the session', async () => {
  server(EMPTY, page(OLD));
  m.useUnfurl.getState().ensure(LINK);
  await settle();
  assert.equal(m.card(LINK), '');
  clock += 30_000;
  m.useUnfurl.getState().ensure(LINK);
  await settle();
  assert.match(m.card(LINK), /<img class="unfurl-thumb"/);
});

test('an image that fails to load leaves the card and brings a refreshed one in', async () => {
  server(page(OLD), page(NEW));
  m.useUnfurl.getState().ensure(LINK);
  await settle();
  assert.ok(m.card(LINK).includes(`src="${OLD}"`));

  let reply;
  globalThis.fetch = (path) => {
    asked.push(path);
    return new Promise((resolve) => { reply = resolve; });
  };
  m.useUnfurl.getState().imageFailed(LINK, OLD);
  const bare = m.card(LINK);
  assert.match(bare, /A reel/);
  assert.ok(!bare.includes('<img'));
  assert.ok(asked[1].endsWith('&refresh=true'));

  reply(new Response(JSON.stringify(page(NEW)), { status: 200 }));
  await settle();
  assert.ok(m.card(LINK).includes(`src="${NEW}"`));
});

test('a second dead image inside the window costs no second refresh, and the mark expires', async () => {
  server(page(OLD), page(NEW));
  const { ensure, imageFailed } = m.useUnfurl.getState();
  ensure(LINK);
  await settle();
  imageFailed(LINK, OLD);
  await settle();
  clock += 1_000;
  imageFailed(LINK, NEW);
  await settle();
  assert.equal(asked.length, 2);
  assert.ok(!m.card(LINK).includes('<img'));

  clock += m.DEAD_IMAGE_MS;
  assert.ok(m.card(LINK).includes(`src="${NEW}"`));
  imageFailed(LINK, NEW);
  assert.equal(asked.length, 3);
});

test('a refresh that fails or finds nothing keeps the card that is on screen', async () => {
  for (const answer of [500, EMPTY]) {
    m.useUnfurl.setState({ byUrl: {} });
    server(page(OLD), answer);
    m.useUnfurl.getState().ensure(LINK);
    await settle();
    m.useUnfurl.getState().imageFailed(LINK, OLD);
    await settle();
    const html = m.card(LINK);
    assert.match(html, /A reel/);
    assert.ok(!html.includes('<img'));
  }
});

test('a card whose only content was the dead image is not drawn at all', () => {
  const only = { ...m.settled(undefined, page(OLD, null), clock), deadImage: OLD, deadAt: clock };
  assert.equal(m.cardImage(only, clock), null);
  m.useUnfurl.setState({ byUrl: { [LINK]: only } });
  assert.equal(m.card(LINK), '');
});
