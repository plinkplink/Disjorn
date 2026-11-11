// Mention autocomplete: the pure module and the popup's markup, bundled for node:
//
//     node --test client/tests/mention_complete.test.mjs

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
import { Composer } from "../src/components/Composer";
import { MentionPopup } from "../src/components/MentionPopup";

export * from "../src/lib/mentionComplete";

const noop = () => {};

export function renderPopup(items, activeIndex) {
  return renderToStaticMarkup(createElement(MentionPopup, {
    id: "mentions", items, activeIndex, onActive: noop, onPick: noop, onPress: noop,
  }));
}

export function renderComposer() {
  return renderToStaticMarkup(createElement(Composer, {
    channelId: 1, channelName: "#main", replyTo: null, onCancelReply: noop,
    editing: null, onStartEdit: noop, onCancelEdit: noop,
  }));
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
  scratch = mkdtempSync(join(tmpdir(), 'mention-complete-'));
  const file = join(scratch, 'bundle.mjs');
  writeFileSync(file, out.outputFiles[0].text);
  m = await import(pathToFileURL(file).href);
});

after(() => { if (scratch) rmSync(scratch, { recursive: true, force: true }); });

// `|` marks the caret.
function at(marked) {
  const caret = marked.indexOf('|');
  return m.activeMention(marked.replace('|', ''), caret);
}

const user = (id, name) => ({ type: 'user', id, name, status: 'online',
  avatar_url: `/avatars/${id}?v=1` });
const bot = (id, name, reply_mode = 'mention') => ({ type: 'bot', id, name,
  avatar_url: null, reply_mode });

const MAIN = [user(1, 'plink'), user(2, 'Anna'), user(3, 'Brogan'),
  bot(1, 'claudette'), bot(2, 'Gable'), bot(3, 'broker'), bot(5, 'BuildGable'),
  bot(6, 'CAVEMAN', 'always')];

const names = (list) => list.map((c) => c.name);

// -- which token the caret is in ----------------------------------------------

test('an @ at the start of the text or after whitespace starts a token', () => {
  assert.deepEqual(at('@|'), { start: 0, end: 1, query: '' });
  assert.deepEqual(at('@ga|'), { start: 0, end: 3, query: 'ga' });
  assert.deepEqual(at('hi @ga|'), { start: 3, end: 6, query: 'ga' });
  assert.deepEqual(at('hi\n@ga|'), { start: 3, end: 6, query: 'ga' });
  assert.deepEqual(at('hi\t@|'), { start: 3, end: 4, query: '' });
});

test('an @ after an opening bracket, quote or emphasis mark starts a token', () => {
  for (const opener of ['(', '[', '{', '"', "'", '*', '~', '>']) {
    assert.deepEqual(at(`x ${opener}@ga|`), { start: 3, end: 6, query: 'ga' }, opener);
  }
});

test('an @ inside a word, an email or a URL starts nothing', () => {
  assert.equal(at('a@b|'), null);
  assert.equal(at('plink@bots|'), null);
  assert.equal(at('mail me at a.b@exa|'), null);
  assert.equal(at('a.@b|'), null);
  assert.equal(at('https://example.com/@ga|'), null);
  assert.equal(at('snake_@ga|'), null);
  assert.equal(at('@@|'), null);
  assert.equal(at('@a@b|'), null);
});

test('the token is the text between the @ and the caret, with no whitespace in it', () => {
  assert.equal(at('@ga |'), null);
  assert.equal(at('@gable how are you|'), null);
  assert.equal(at('@ga\n|'), null);
  assert.deepEqual(at('@ga|ble'), { start: 0, end: 3, query: 'ga' });
  assert.deepEqual(at('@|hello'), { start: 0, end: 1, query: '' });
  assert.deepEqual(at('@gable hi @cl|'), { start: 10, end: 13, query: 'cl' });
});

test('no caret, a selected range and a caret outside the text start nothing', () => {
  assert.equal(at('|@ga'), null);
  assert.equal(at('hello|'), null);
  assert.equal(m.activeMention('', 0), null);
  assert.equal(m.activeMention('@gable', 1, 4), null);
  assert.equal(m.activeMention('@ga', 9), null);
  assert.equal(m.activeMention('', 3), null);
});

test('inside an inline code span, closed or still being typed, nothing starts', () => {
  assert.equal(at('run `foo @ga|` now'), null);
  assert.equal(at('run `@ga|`'), null);
  assert.equal(at('run `foo @ga|'), null);
  assert.equal(at('`@|'), null);
});

test('after a closed code span, or under a stray backtick on an earlier line, a token starts', () => {
  assert.deepEqual(at('run `foo` @ga|'), { start: 10, end: 13, query: 'ga' });
  assert.deepEqual(at('`a` and `b` @ga|'), { start: 12, end: 15, query: 'ga' });
  assert.deepEqual(at('it`s late\n@ga|'), { start: 10, end: 13, query: 'ga' });
  assert.equal(at('`a` and `b @ga|'), null);
});

test('inside a fenced block, closed or still open, nothing starts', () => {
  assert.equal(at('```\n@ga|\n```'), null);
  assert.equal(at('```py\nx = 1\n@ga|\n```\nafter'), null);
  assert.equal(at('```\ncode\n@ga|'), null);
  assert.equal(at('```js `x`\n@ga|'), null);
  assert.equal(at('```\nuse `x` then @ga|\n```'), null);
});

test('after a closed fenced block a token starts', () => {
  assert.deepEqual(at('```\ncode\n```\n@ga|'), { start: 13, end: 16, query: 'ga' });
  assert.deepEqual(at('```a``` @ga|'), { start: 8, end: 11, query: 'ga' });
});

// -- who is offered -----------------------------------------------------------

test('an empty @ lists answering bots, then @bots, then people with the reader last, then silent bots', () => {
  const list = m.mentionCandidates(MAIN, { selfUserId: 1 });
  assert.deepEqual(names(list),
    ['claudette', 'Gable', 'CAVEMAN', 'bots', 'Anna', 'Brogan', 'plink', 'BuildGable']);
  assert.deepEqual(list.map((c) => c.kind),
    ['bot', 'bot', 'bot', 'group', 'user', 'user', 'user', 'bot']);
  assert.equal(new Set(list.map((c) => c.key)).size, list.length);
});

test('a bot and a person who share an id get different keys', () => {
  const list = m.mentionCandidates([user(1, 'plink'), bot(1, 'claudette')], { selfUserId: 9 });
  assert.deepEqual(list.map((c) => c.key), ['bot:1', 'group:bots', 'user:1']);
});

test('each row says what a mention does here', () => {
  const hint = Object.fromEntries(
    m.mentionCandidates(MAIN, { selfUserId: 1 }).map((c) => [c.name, c.hint]));
  assert.equal(hint.claudette, 'replies when mentioned');
  assert.equal(hint.CAVEMAN, 'replies to everything here');
  assert.equal(hint.broker, undefined);
  assert.equal(hint.BuildGable, 'does not reply');
  assert.equal(hint.bots, 'every bot in this room (3)');
  assert.equal(hint.Anna, '');
  assert.equal(hint.plink, 'you');
});

test('a bot row with no reply mode reads as mention', () => {
  const [row] = m.mentionCandidates([{ type: 'bot', id: 7, name: 'Otto' }], { selfUserId: null });
  assert.equal(row.hint, 'replies when mentioned');
  assert.equal(row.avatarUrl, null);
});

test('@bots is offered only where it would call somebody', () => {
  const has = (members) => names(m.mentionCandidates(members, { selfUserId: 1 })).includes('bots');
  assert.equal(has(MAIN), true);
  assert.equal(has([user(1, 'plink'), user(2, 'Anna')]), false);
  assert.equal(has([user(1, 'plink'), bot(3, 'broker'), bot(4, 'SYSTEM')]), false);
  assert.equal(has([user(1, 'plink'), bot(1, 'claudette')]), true);
  assert.equal(has([]), false);
});

test('the silent names are matched in any case and can be replaced', () => {
  const list = m.mentionCandidates([bot(1, 'claudette'), bot(8, 'otto')],
    { selfUserId: null, silentBotNames: ['OTTO'] });
  assert.deepEqual(list.map((c) => [c.name, c.hint]), [
    ['claudette', 'replies when mentioned'],
    ['bots', 'every bot in this room (1)'],
    ['otto', 'does not reply'],
  ]);
  assert.deepEqual([...m.SILENT_BOT_NAMES], ['system', 'broker', 'BuildGable']);
});

test('in a build chat the bot is said to answer everything', () => {
  const list = m.mentionCandidates([user(1, 'plink'), bot(2, 'Gable')],
    { selfUserId: 1, everyMessage: true });
  assert.equal(list[0].hint, 'replies to everything here');
});

test('while editing, no row promises a call', () => {
  const hint = Object.fromEntries(
    m.mentionCandidates(MAIN, { selfUserId: 1, editing: true }).map((c) => [c.name, c.hint]));
  assert.equal(hint.claudette, 'not called by an edit');
  assert.equal(hint.CAVEMAN, 'not called by an edit');
  assert.equal(hint.bots, 'not called by an edit');
  assert.equal(hint.BuildGable, 'does not reply');
  assert.equal(hint.Anna, '');
});

test('a row carries the stored name and the avatar url as given', () => {
  const list = m.mentionCandidates([user(4, 'Ann Marie'), bot(2, 'Gable')], { selfUserId: 1 });
  const ann = list.find((c) => c.key === 'user:4');
  assert.equal(ann.name, 'Ann Marie');
  assert.equal(ann.avatarUrl, '/avatars/4?v=1');
});

// -- narrowing ----------------------------------------------------------------

test('an empty query keeps every candidate in order', () => {
  const list = m.mentionCandidates(MAIN, { selfUserId: 1 });
  assert.deepEqual(m.rankMentions(list, ''), list);
});

test('names that start with the query come before names that contain it', () => {
  const list = m.mentionCandidates(MAIN, { selfUserId: 1 });
  assert.deepEqual(names(m.rankMentions(list, 'g')), ['Gable', 'Brogan', 'BuildGable']);
  assert.deepEqual(names(m.rankMentions(list, 'b')), ['bots', 'Brogan', 'BuildGable', 'Gable']);
  assert.deepEqual(names(m.rankMentions(list, 'an')), ['Anna', 'CAVEMAN', 'Brogan']);
});

test('matching ignores case and returns the stored spelling', () => {
  const list = m.mentionCandidates(MAIN, { selfUserId: 1 });
  assert.deepEqual(names(m.rankMentions(list, 'CAVE')), ['CAVEMAN']);
  assert.deepEqual(names(m.rankMentions(list, 'cave')), ['CAVEMAN']);
  assert.deepEqual(names(m.rankMentions(list, 'CLAUD')), ['claudette']);
  assert.deepEqual(names(m.rankMentions(list, 'BOTS')), ['bots']);
});

test('a query nothing contains matches nothing, which closes the popup', () => {
  const list = m.mentionCandidates(MAIN, { selfUserId: 1 });
  assert.deepEqual(m.rankMentions(list, 'zz'), []);
  assert.deepEqual(m.rankMentions(list, 'gable,'), []);
  assert.deepEqual(m.rankMentions([], ''), []);
});

test('a name with a space is found by its first word and by a later one', () => {
  const list = m.mentionCandidates([user(4, 'Ann Marie'), user(5, 'Marius')], { selfUserId: 1 });
  assert.deepEqual(names(m.rankMentions(list, 'ann')), ['Ann Marie']);
  assert.deepEqual(names(m.rankMentions(list, 'mar')), ['Marius', 'Ann Marie']);
});

// -- accepting ----------------------------------------------------------------

function accept(marked, name) {
  const caret = marked.indexOf('|');
  const text = marked.replace('|', '');
  const out = m.acceptMention(text, m.activeMention(text, caret), name);
  return `${out.text.slice(0, out.caret)}|${out.text.slice(out.caret)}`;
}

test('accepting writes the stored name and one space, and leaves the caret after it', () => {
  assert.equal(accept('@ga|', 'Gable'), '@Gable |');
  assert.equal(accept('@|', 'claudette'), '@claudette |');
  assert.equal(accept('hey @CL|', 'claudette'), 'hey @claudette |');
  assert.equal(accept('(@b|', 'bots'), '(@bots |');
  assert.equal(accept('@ann|', 'Ann Marie'), '@Ann Marie |');
});

test('text after the caret is kept', () => {
  assert.equal(accept('@G|hello', 'Gable'), '@Gable |hello');
  assert.equal(accept('one @ga|\ntwo', 'Gable'), 'one @Gable |\ntwo');
  assert.equal(accept('a @ga|) b', 'Gable'), 'a @Gable |) b');
});

test('a space already after the caret is stepped over, not doubled', () => {
  assert.equal(accept('@ga| can you look', 'Gable'), '@Gable |can you look');
  assert.equal(accept('@Gable| x', 'Gable'), '@Gable |x');
});

test('text before the token is untouched', () => {
  assert.equal(accept('mail a@b.c then @cl|', 'claudette'), 'mail a@b.c then @claudette |');
  assert.equal(accept('`@gable` and @ga|', 'Gable'), '`@gable` and @Gable |');
});

test('the caret after an accepted mention is in no token', () => {
  const out = m.acceptMention('hi @ga', m.activeMention('hi @ga', 6), 'Gable');
  assert.equal(m.activeMention(out.text, out.caret), null);
});

// -- the popup's state --------------------------------------------------------

const tok = (start, query) => ({ start, end: start + 1 + query.length, query });

test('typing starts a token; a caret that only lands in one does not', () => {
  assert.deepEqual(m.trackMention(null, tok(3, 'g'), true), { token: tok(3, 'g'), index: 0, off: false });
  assert.equal(m.trackMention(null, tok(3, 'g'), false), null);
  const open = { token: tok(3, 'g'), index: 0, off: false };
  assert.equal(m.trackMention(open, tok(9, 'cl'), false), null);
  assert.deepEqual(m.trackMention(open, tok(9, 'cl'), true), { token: tok(9, 'cl'), index: 0, off: false });
});

test('leaving the token closes it', () => {
  const open = { token: tok(3, 'g'), index: 1, off: false };
  assert.equal(m.trackMention(open, null, true), null);
  assert.equal(m.trackMention(open, null, false), null);
});

test('the highlight holds while the query holds and returns to the best match when it changes', () => {
  const open = { token: tok(3, 'g'), index: 2, off: false };
  assert.equal(m.trackMention(open, tok(3, 'g'), false).index, 2);
  assert.equal(m.trackMention(open, tok(3, 'ga'), true).index, 0);
  assert.deepEqual(m.trackMention(open, tok(3, ''), false).token, tok(3, ''));
});

test('a dismissed token stays closed while it is typed in, and a new one opens', () => {
  const off = { token: tok(3, 'g'), index: 0, off: true };
  assert.equal(m.trackMention(off, tok(3, 'ga'), true).off, true);
  assert.equal(m.trackMention(off, tok(3, 'ga'), false).off, true);
  assert.equal(m.trackMention(m.trackMention(off, null, true), tok(3, ''), true).off, false);
  assert.equal(m.trackMention(off, tok(8, ''), true).off, false);
});

// -- keys while the popup is open ---------------------------------------------

const key = (k, extra = {}) => m.mentionKey({ key: k, shiftKey: false, altKey: false,
  ctrlKey: false, metaKey: false, isComposing: false, keyCode: 0, ...extra });

test('arrows move, Enter and Tab accept, Escape dismisses', () => {
  assert.equal(key('ArrowDown'), 'down');
  assert.equal(key('ArrowUp'), 'up');
  assert.equal(key('Enter'), 'accept');
  assert.equal(key('Tab'), 'accept');
  assert.equal(key('Escape'), 'dismiss');
});

test('Enter with Ctrl or Cmd accepts too, so no Enter sends past an open popup', () => {
  assert.equal(key('Enter', { ctrlKey: true }), 'accept');
  assert.equal(key('Enter', { metaKey: true }), 'accept');
});

test('Shift+Enter, Shift+Tab, modified arrows and ordinary keys are left to the composer', () => {
  assert.equal(key('Enter', { shiftKey: true }), null);
  assert.equal(key('Tab', { shiftKey: true }), null);
  assert.equal(key('ArrowDown', { shiftKey: true }), null);
  assert.equal(key('ArrowUp', { altKey: true }), null);
  assert.equal(key('ArrowUp', { metaKey: true }), null);
  assert.equal(key('a'), null);
  assert.equal(key('Backspace'), null);
  assert.equal(key('ArrowLeft'), null);
});

test('a key that belongs to an IME composition neither accepts nor falls through', () => {
  assert.equal(key('Enter', { isComposing: true }), 'compose');
  assert.equal(key('Enter', { keyCode: 229 }), 'compose');
  assert.equal(key('ArrowDown', { isComposing: true }), 'compose');
  assert.equal(key('Escape', { isComposing: true }), 'compose');
  assert.equal(key('Process', { keyCode: 229 }), 'compose');
});

// -- markup -------------------------------------------------------------------

test('the popup is a listbox of options, one selected, each with an id the textarea can point at', () => {
  const items = m.rankMentions(m.mentionCandidates(MAIN, { selfUserId: 1 }), '');
  const html = m.renderPopup(items, 3);
  assert.match(html, /^<div id="mentions" class="mention-popup" role="listbox"/);
  assert.equal(html.match(/role="option"/g).length, items.length);
  assert.equal(html.match(/aria-selected="true"/g).length, 1);
  assert.match(html, /id="mentions-3" class="mention-option active" role="option" aria-selected="true"/);
  items.forEach((_, i) => assert.ok(html.includes(`id="mentions-${i}"`), `option ${i}`));
});

test('a row draws the avatar, the name and the hint; @bots is drawn with its @', () => {
  const html = m.renderPopup(m.mentionCandidates(MAIN, { selfUserId: 1 }), 0);
  assert.match(html, /<img src="\/avatars\/2\?v=1"/);
  assert.match(html, /class="avatar avatar-bot"[^>]*>C</);
  assert.match(html, /<span class="mention-name">@bots<\/span><span class="mention-hint">every bot in this room \(3\)<\/span>/);
  assert.match(html, /<span class="mention-name">claudette<\/span><span class="mention-hint">replies when mentioned<\/span>/);
  assert.match(html, /<span class="mention-name">Anna<\/span><\/div>/);
});

test('with no popup open the textarea says so and points at nothing', () => {
  const html = m.renderComposer();
  const textarea = /<textarea[^>]*>/.exec(html)[0];
  assert.match(textarea, /aria-expanded="false"/);
  assert.match(textarea, /aria-autocomplete="list"/);
  assert.match(textarea, /aria-haspopup="listbox"/);
  assert.doesNotMatch(textarea, /aria-activedescendant|aria-controls/);
  assert.doesNotMatch(html, /mention-popup/);
});
