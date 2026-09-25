// MessageList's attribution span, rendered from a store fixture:
//
//     node --test client/tests/message_list.test.mjs
//
// esbuild (vite's own dependency) bundles the component with React for node;
// react-dom/server renders it. The toolchain is found at
// DISJORN_CLIENT_NODE_MODULES, client/node_modules or /opt/node_modules.

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
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { MessageList } from "../src/components/MessageList";
import { useMessages } from "../src/stores/messages";

export function render(list) {
  // A server render reads the store's initial state, so the fixture goes there.
  useMessages.getInitialState().byChannel[1] =
    { list, loaded: true, reachedStart: true, gaps: [] };
  const noop = () => {};
  return renderToStaticMarkup(createElement(MessageList, {
    channelId: 1, onReply: noop, onEdit: noop, onOpenImage: noop,
    onSummarize: noop,
  }));
}
`;

let render;
let scratch;

before(async () => {
  const nm = nodeModules();
  // esbuild finds its binary by package lookup, which needs a directory named
  // node_modules; the gate's mount is not always one.
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
  scratch = mkdtempSync(join(tmpdir(), 'msglist-'));
  const file = join(scratch, 'bundle.mjs');
  writeFileSync(file, out.outputFiles[0].text);
  ({ render } = await import(pathToFileURL(file).href));
});

after(() => { if (scratch) rmSync(scratch, { recursive: true, force: true }); });

const T0 = Date.parse('2026-09-23T18:31:00Z');

function botMessage(id, { minutes = 0, attribution } = {}) {
  const m = {
    id, channel_id: 1, seq: id, author_type: 'bot', author_id: 2,
    author: { type: 'bot', id: 2, name: 'Gable', avatar_path: null, avatar_url: null },
    content: `reply ${id}`,
    created_at: new Date(T0 + minutes * 60_000).toISOString(),
    edited_at: null, deleted_at: null, reply_to_id: null,
    privacy_flags: {}, emote_refs: [], attachments: [],
  };
  if (attribution !== undefined) m.attribution = attribution;
  return m;
}

function rows(html) {
  return [...html.matchAll(/<div id="msg-(\d+)" class="msg ([^"]*)"/g)]
    .map((r) => ({ id: Number(r[1]), head: r[2].split(' ').includes('msg-head') }));
}

test('renders the span after the time from a fixture with attribution', () => {
  const html = render([botMessage(1, { attribution:
    { model: 'claude-fable-5-1', verified: true, summoner: 'plink' } })]);
  const span = '<span class="msg-attrib" title="model as reported by the posting bot">'
    + 'claude-fable-5-1 · summoned by plink</span>';
  assert.ok(html.includes(span), html);
  const meta = html.slice(html.indexOf('class="msg-meta"'));
  assert.ok(meta.indexOf('</time>') < meta.indexOf('msg-attrib'));
  assert.ok(!html.includes('— gable'));
});

test('an unverified model says so', () => {
  const html = render([botMessage(1, { attribution:
    { model: 'claude-fable-5-1', verified: false, summoner: 'plink' } })]);
  assert.ok(html.includes('>claude-fable-5-1 (pinned; actual unverified) '
    + '· summoned by plink</span>'), html);
});

test('renders nothing without attribution', () => {
  for (const attribution of [undefined, {},
    { model: null, verified: false, summoner: 'claudette' }]) {
    const html = render([botMessage(1, { attribution })]);
    assert.ok(html.includes('class="msg-meta"'));
    assert.ok(!html.includes('msg-attrib'), html);
  }
});

test('an attributed message forces its header inside a group', () => {
  const plain = render([botMessage(1), botMessage(2, { minutes: 1 })]);
  assert.deepEqual(rows(plain), [{ id: 1, head: true }, { id: 2, head: false }]);

  const attributed = render([botMessage(1), botMessage(2, { minutes: 1,
    attribution: { model: 'claude-fable-5-1', verified: true, summoner: 'plink' } })]);
  assert.deepEqual(rows(attributed), [{ id: 1, head: true }, { id: 2, head: true }]);
  assert.equal(attributed.split('msg-attrib').length - 1, 1);
});
