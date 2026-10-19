#!/usr/bin/env node
/*
 * Tests for apps-script/Code.gs.
 *
 * Code.gs runs inside a vm context with in-memory stand-ins for the Apps Script
 * services it uses (Drive, properties, UrlFetchApp with a scripted GitHub API, mail,
 * triggers, locks, HtmlService). No npm packages.
 *
 *   node tests/apps-script/run_tests.js
 *
 * Exit code is non-zero when a test fails.
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');

const ROOT = path.resolve(__dirname, '..', '..');
const SCRIPT_DIR = path.join(ROOT, 'apps-script');
const CODE = fs.readFileSync(path.join(SCRIPT_DIR, 'Code.gs'), 'utf8');
const PAGE = fs.readFileSync(path.join(SCRIPT_DIR, 'Page.html'), 'utf8');

const REPO = 'damar510-gif/converter-books';
const TOKEN = 'github_pat_TEST_SECRET';
const OWNER = 'owner@example.com';
const F = {
  root: 'تحويل الكتب',
  inbox: '1- جديد',
  working: '2- قيد التحويل',
  ready: '3- جاهز',
  originals: '4- الأصل',
  problem: '5- مشكلة',
  final: '6- مكتمل'
};

const iso = (t) => new Date(t).toISOString();
// GitHub-style timestamps have whole seconds.
const ghTime = (t) => new Date(t).toISOString().replace(/\.\d{3}Z$/, 'Z');

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

function iterator(list) {
  let i = 0;
  return {
    hasNext: () => i < list.length,
    next: () => {
      if (i >= list.length) throw new Error('Iterator has no more items');
      return list[i++];
    }
  };
}

class MockBlob {
  constructor(data, type, name) {
    this.bytes = Buffer.isBuffer(data) ? Buffer.from(data) : Buffer.from(data === undefined || data === null ? '' : String(data), 'utf8');
    this.type = type || null;
    this.name = name || null;
  }
  getBytes() { return Array.from(this.bytes); }
  getDataAsString() { return this.bytes.toString('utf8'); }
  getName() { return this.name; }
  setName(n) { this.name = n; return this; }
  getContentType() { return this.type; }
  setContentType(t) { this.type = t; return this; }
}

function formatDate(date, tz, pattern) {
  const t = typeof date === 'number' ? date : date.getTime();
  if (!isFinite(t)) throw new Error('Invalid date');
  const leftover = pattern.replace(/yyyy|MM|dd|HH|mm|ss/g, '');
  if (/[A-Za-z]/.test(leftover)) throw new Error('mock formatDate: unsupported pattern ' + pattern);
  const parts = {};
  new Intl.DateTimeFormat('en-US', {
    timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23'
  }).formatToParts(new Date(t)).forEach((p) => { parts[p.type] = p.value; });
  const map = { yyyy: parts.year, MM: parts.month, dd: parts.day, HH: parts.hour, mm: parts.minute, ss: parts.second };
  return pattern.replace(/yyyy|MM|dd|HH|mm|ss/g, (k) => map[k]);
}

function escapeHtml(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function makeGitHub(env) {
  const gh = {
    token: TOKEN,
    authCode: null,        // force 401/403 on every call
    dispatchCode: 204,
    defaultBranch: 'main',
    workflowExists: true,
    networkDown: false,
    tokenExpiry: null,     // value of the GitHub-Authentication-Token-Expiration header
    files: {},             // results branch: path -> string | Buffer
    runs: [],              // convert.yml runs, newest first
    dispatches: [],
    unknown: []
  };
  const base = '/repos/' + REPO;

  gh.handle = (req) => {
    const u = new URL(req.url);
    const headers = {};
    if (gh.tokenExpiry) headers['github-authentication-token-expiration'] = gh.tokenExpiry;
    const json = (code, obj) => ({ code, body: JSON.stringify(obj), headers: Object.assign({ 'Content-Type': 'application/json' }, headers) });

    if (u.origin !== 'https://api.github.com') return json(404, { message: 'wrong host' });
    if (req.headers.Authorization !== 'Bearer ' + gh.token) return json(401, { message: 'Bad credentials' });
    if (gh.authCode) {
      return json(gh.authCode, { message: gh.authCode === 403 ? 'Resource not accessible by personal access token' : 'Bad credentials' });
    }
    if (req.headers['X-GitHub-Api-Version'] !== '2022-11-28') return json(400, { message: 'missing API version' });
    const p = u.pathname;

    if (req.method === 'GET' && p === base) {
      return json(200, { full_name: REPO, private: true, default_branch: gh.defaultBranch });
    }
    if (req.method === 'GET' && p === base + '/actions/workflows/convert.yml') {
      return gh.workflowExists ? json(200, { id: 7, path: '.github/workflows/convert.yml', state: 'active' }) : json(404, { message: 'Not Found' });
    }
    if (req.method === 'POST' && p === base + '/actions/workflows/convert.yml/dispatches') {
      if (req.headers.Accept !== 'application/vnd.github+json') return json(415, { message: 'bad accept' });
      if (gh.dispatchCode !== 204) {
        return json(gh.dispatchCode, { message: gh.dispatchCode === 422 ? 'Unexpected inputs provided: ["drive_file_id"]' : 'Server Error' });
      }
      gh.dispatches.push({ at: env.clock, body: JSON.parse(req.payload), contentType: req.contentType });
      return { code: 204, body: '', headers };
    }
    if (req.method === 'GET' && p === base + '/actions/workflows/convert.yml/runs') {
      const n = Number(u.searchParams.get('per_page') || 30);
      return json(200, { total_count: gh.runs.length, workflow_runs: gh.runs.slice(0, n) });
    }
    if (req.method === 'GET' && p.indexOf(base + '/contents/') === 0) {
      if (u.searchParams.get('ref') !== 'results') return json(404, { message: 'No commit found for the ref' });
      if (req.headers.Accept !== 'application/vnd.github.raw') return json(415, { message: 'tests expect the raw media type' });
      const filePath = p.slice((base + '/contents/').length).split('/').map(decodeURIComponent).join('/');
      if (!Object.prototype.hasOwnProperty.call(gh.files, filePath)) return json(404, { message: 'Not Found' });
      const v = gh.files[filePath];
      return { code: 200, body: Buffer.isBuffer(v) ? v : Buffer.from(v, 'utf8'), headers: Object.assign({ 'Content-Type': 'application/vnd.github.raw' }, headers) };
    }
    gh.unknown.push(req.method + ' ' + p);
    return json(404, { message: 'Not Found' });
  };
  return gh;
}

function makeEnv(options) {
  options = options || {};
  const env = {
    clock: Date.parse(options.start || '2026-10-19T08:00:00Z'),
    logs: [],
    mails: [],
    requests: [],
    triggers: [],
    tokens: 0,
    lockBusy: false,
    lockHeld: false,
    failMove: new Set()
  };
  env.gh = makeGitHub(env);

  // ----- Drive -----
  let nextId = 1;
  const items = new Map();
  const newId = (prefix) => prefix + String(nextId++).padStart(5, '0') + 'aBcDeFgHiJkLmNoPqRsTuVwX';
  // Real DriveApp iterators may include trashed items; the mock does too on purpose.
  const children = (id) => Array.from(items.values()).filter((x) => x.parentId === id);

  class MockFolder {
    constructor(name, parentId) {
      this.id = newId('0F');
      this.name = name;
      this.parentId = parentId;
      this.trashed = false;
      items.set(this.id, this);
    }
    getId() { return this.id; }
    getName() { return this.name; }
    getUrl() { return 'https://drive.google.com/drive/folders/' + this.id; }
    isTrashed() { return this.trashed; }
    setTrashed(v) { this.trashed = !!v; return this; }
    getFoldersByName(n) { return iterator(children(this.id).filter((x) => x instanceof MockFolder && x.name === n)); }
    getFolders() { return iterator(children(this.id).filter((x) => x instanceof MockFolder)); }
    getFiles() { return iterator(children(this.id).filter((x) => x instanceof MockFile)); }
    getFilesByName(name) { return iterator(children(this.id).filter((x) => x instanceof MockFile && x.name === name)); }
    createFolder(n) { return new MockFolder(n, this.id); }
    createFile(a, content, mime) {
      if (typeof a === 'string') {
        return new MockFile({ name: a, mime: mime || 'text/plain', content: Buffer.from(String(content), 'utf8'), parentId: this.id });
      }
      if (!(a instanceof MockBlob)) throw new Error('createFile expects a blob or (name, content, mimeType)');
      return new MockFile({ name: a.getName(), mime: a.getContentType(), content: Buffer.from(a.bytes), parentId: this.id });
    }
  }

  class MockFile {
    constructor(o) {
      this.id = newId('1B');
      this.name = o.name;
      this.mime = o.mime;
      this.content = o.content || Buffer.alloc(0);
      this.size = o.size !== undefined ? o.size : this.content.length;
      this.created = o.created !== undefined ? o.created : env.clock;
      this.updated = o.updated !== undefined ? o.updated : this.created;
      this.parentId = o.parentId;
      this.trashed = false;
      items.set(this.id, this);
    }
    getId() { return this.id; }
    getName() { return this.name; }
    getMimeType() { return this.mime; }
    getSize() { return this.size; }
    getDateCreated() { return new Date(this.created); }
    getLastUpdated() { return new Date(this.updated); }
    getUrl() { return 'https://drive.google.com/file/d/' + this.id + '/view?usp=drivesdk'; }
    isTrashed() { return this.trashed; }
    setTrashed(v) { this.trashed = !!v; return this; }
    getBlob() { return new MockBlob(this.content, this.mime, this.name); }
    getParents() { return iterator([items.get(this.parentId)]); }
    moveTo(folder) {
      if (!(folder instanceof MockFolder)) throw new Error('moveTo expects a folder');
      if (env.failMove.has(this.id)) throw new Error('Access denied: DriveApp.');
      this.parentId = folder.getId();
      return this;
    }
  }

  const myDrive = new MockFolder('My Drive', null);
  const DriveApp = {
    getRootFolder: () => myDrive,
    getFolderById: (id) => {
      const x = items.get(id);
      if (!(x instanceof MockFolder)) throw new Error('No item with the given ID could be found.');
      return x;
    },
    getFileById: (id) => {
      const x = items.get(id);
      if (!(x instanceof MockFile)) throw new Error('No item with the given ID could be found.');
      return x;
    }
  };
  env.drive = { items, myDrive, MockFile, MockFolder, children, DriveApp };

  // ----- Script Properties (9 KB per value, like the real service) -----
  const store = new Map();
  const props = {
    getProperty: (k) => (store.has(k) ? store.get(k) : null),
    setProperty: (k, v) => {
      v = String(v);
      if (Buffer.byteLength(v, 'utf8') > 9 * 1024) throw new Error('Argument too large: value');
      store.set(k, v);
      return props;
    },
    setProperties: (obj, deleteAllOthers) => {
      if (deleteAllOthers) store.clear();
      Object.keys(obj).forEach((k) => props.setProperty(k, obj[k]));
      return props;
    },
    deleteProperty: (k) => { store.delete(k); return props; },
    getProperties: () => Object.fromEntries(store),
    getKeys: () => Array.from(store.keys())
  };
  env.props = store;

  // ----- Drive REST API (setup checks that the workflow will be able to use it) -----
  env.driveApiDisabled = false;
  const driveApi = (req) => {
    if (!/^Bearer ya29\./.test(req.headers.Authorization || '')) return { code: 401, body: '{"error":{"code":401}}', headers: {} };
    if (env.driveApiDisabled) {
      return { code: 403, headers: {}, body: JSON.stringify({ error: { code: 403, status: 'PERMISSION_DENIED',
        message: 'Google Drive API has not been used in project 123 before or it is disabled.', details: [{ reason: 'SERVICE_DISABLED' }] } }) };
    }
    const id = decodeURIComponent(new URL(req.url).pathname.split('/').pop());
    return items.has(id) ? { code: 200, body: JSON.stringify({ id }), headers: {} } : { code: 404, body: '{}', headers: {} };
  };

  // ----- UrlFetchApp -----
  const UrlFetchApp = {
    fetch(url, params) {
      params = params || {};
      const req = {
        url,
        method: String(params.method || 'get').toUpperCase(),
        headers: Object.assign({}, params.headers),
        payload: params.payload,
        contentType: params.contentType,
        muteHttpExceptions: params.muteHttpExceptions,
        at: env.clock
      };
      env.requests.push(req);
      if (env.gh.networkDown) throw new Error('Address unavailable: ' + url);
      const res = url.indexOf('https://www.googleapis.com/drive/v3/files/') === 0 ? driveApi(req) : env.gh.handle(req);
      if (!params.muteHttpExceptions && res.code >= 400) throw new Error('Request failed for ' + url + ' returned code ' + res.code);
      const body = Buffer.isBuffer(res.body) ? res.body : Buffer.from(res.body || '', 'utf8');
      return {
        getResponseCode: () => res.code,
        getContentText: () => body.toString('utf8'),
        getBlob: () => new MockBlob(body, (res.headers || {})['Content-Type'] || 'application/octet-stream', null),
        getHeaders: () => Object.assign({}, res.headers),
        getAllHeaders: () => Object.assign({}, res.headers)
      };
    }
  };

  const ScriptApp = {
    getOAuthToken() { env.tokens++; return 'ya29.drive-token-' + env.tokens; },
    getProjectTriggers: () => env.triggers.slice(),
    deleteTrigger: (t) => { env.triggers = env.triggers.filter((x) => x !== t); },
    newTrigger(fn) {
      return {
        timeBased: () => ({
          everyMinutes: (n) => {
            if ([1, 5, 10, 15, 30].indexOf(n) < 0) throw new Error('everyMinutes accepts 1, 5, 10, 15 or 30');
            return {
              create: () => {
                const t = { fn, minutes: n, getHandlerFunction: () => fn, getUniqueId: () => 'trigger-' + fn + '-' + n };
                env.triggers.push(t);
                return t;
              }
            };
          }
        })
      };
    }
  };

  const MailApp = {
    sendEmail(a, b, c, d) {
      const m = typeof a === 'object' ? Object.assign({}, a) : Object.assign({ to: a, subject: b, body: c }, d || {});
      m.at = env.clock;
      env.mails.push(m);
    },
    getRemainingDailyQuota: () => 100
  };

  const Session = {
    getEffectiveUser: () => ({ getEmail: () => OWNER }),
    getActiveUser: () => ({ getEmail: () => OWNER }),
    getScriptTimeZone: () => 'Asia/Riyadh'
  };

  const LockService = {
    getScriptLock: () => ({
      tryLock: () => {
        if (env.lockBusy || env.lockHeld) return false;
        env.lockHeld = true;
        return true;
      },
      releaseLock: () => { env.lockHeld = false; },
      hasLock: () => env.lockHeld
    })
  };

  const makeOutput = (html) => ({
    content: html,
    title: '',
    meta: {},
    xfo: null,
    setTitle(t) { this.title = t; return this; },
    getTitle() { return this.title; },
    addMetaTag(n, c) { this.meta[n] = c; return this; },
    setXFrameOptionsMode(m) { this.xfo = m; return this; },
    getContent() { return this.content; }
  });
  const HtmlService = {
    XFrameOptionsMode: { DEFAULT: 'DEFAULT', ALLOWALL: 'ALLOWALL' },
    createTemplateFromFile(name) {
      const src = fs.readFileSync(path.join(SCRIPT_DIR, name + '.html'), 'utf8');
      return {
        evaluate() {
          const vars = this;
          const html = src.replace(/<\?(!?=)?([\s\S]*?)\?>/g, (m, kind, expr) => {
            if (!kind) throw new Error('the mock supports only <?= ?> and <?!= ?> scriptlets');
            const key = expr.trim();
            if (!(key in vars)) throw new Error('template variable missing: ' + key);
            return kind === '!=' ? String(vars[key]) : escapeHtml(vars[key]);
          });
          return makeOutput(html);
        }
      };
    },
    createHtmlOutputFromFile: (name) => makeOutput(fs.readFileSync(path.join(SCRIPT_DIR, name + '.html'), 'utf8'))
  };

  const Utilities = {
    formatDate,
    newBlob: (data, type, name) => new MockBlob(data, type, name),
    sleep: () => {}
  };

  const Logger = { log: (...a) => { env.logs.push(a.map(String).join(' ')); } };

  const sandbox = {
    DriveApp, UrlFetchApp, ScriptApp, MailApp, Session, LockService, HtmlService, Utilities, Logger,
    PropertiesService: { getScriptProperties: () => props }
  };
  vm.createContext(sandbox);
  vm.runInContext(CODE, sandbox, { filename: 'Code.gs' });
  sandbox.now_ = () => new Date(env.clock);
  env.ctx = sandbox;
  return env;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function readyEnv(options) {
  const env = makeEnv(options);
  env.props.set('GITHUB_TOKEN', TOKEN);
  env.ctx.setup();
  env.mails.length = 0;
  env.requests.length = 0;
  return env;
}

function folder(env, key) {
  const id = env.props.get('FOLDER_' + key.toUpperCase());
  assert.ok(id, 'folder id stored for ' + key);
  return env.drive.DriveApp.getFolderById(id);
}

function filesIn(env, key) {
  const f = folder(env, key);
  return env.drive.children(f.getId()).filter((x) => x instanceof env.drive.MockFile && !x.trashed);
}

function names(env, key) {
  return filesIn(env, key).map((f) => f.name).sort();
}

const MIME = {
  pdf: 'application/pdf',
  docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  txt: 'text/plain'
};

function addBook(env, name, o) {
  o = o || {};
  const ext = (name.split('.').pop() || '').toLowerCase();
  const age = (o.ageMin !== undefined ? o.ageMin : 10) * 60000;
  const updatedAge = (o.updatedAgeMin !== undefined ? o.updatedAgeMin : (o.ageMin !== undefined ? o.ageMin : 10)) * 60000;
  return new env.drive.MockFile({
    name,
    mime: o.mime || MIME[ext] || 'application/octet-stream',
    content: Buffer.from('%PDF-1.7 fake book ' + name),
    size: o.size,
    created: env.clock - age,
    updated: env.clock - updatedAge,
    parentId: folder(env, o.folder || 'inbox').getId()
  });
}

function jobs(env) {
  return JSON.parse(env.props.get('JOBS') || '{}');
}

function minutes(env, n) {
  env.clock += n * 60000;
}

/** One check; also asserts the global rules: at most one dispatch, one active job, lock released. */
function tick(env) {
  const before = env.gh.dispatches.length;
  const posts = env.requests.filter((r) => r.method === 'POST').length;
  const result = env.ctx.tick();
  assert.ok(env.gh.dispatches.length - before <= 1, 'more than one dispatch in one check');
  assert.ok(env.requests.filter((r) => r.method === 'POST').length - posts <= 1, 'more than one dispatch request in one check');
  const active = Object.values(jobs(env)).filter((j) => j.stage === 'queued' || j.stage === 'running');
  assert.ok(active.length <= 1, 'two active jobs');
  assert.strictEqual(env.lockHeld, false, 'lock released');
  return result;
}

function lastDispatch(env) {
  return env.gh.dispatches[env.gh.dispatches.length - 1];
}

function writeStatus(env, fileId, state, extra) {
  const s = Object.assign({
    state,
    book: '',
    epub: '',
    pages_total: 10,
    pages_done: 10,
    pages_unsure: [],
    pages_missing: [],
    quota_day: formatDate(env.clock, 'America/Los_Angeles', 'yyyy-MM-dd'),
    run_id: 1000 + env.gh.dispatches.length,
    run_number: env.gh.dispatches.length,
    updated_at: ghTime(env.clock)
  }, extra || {});
  env.gh.files['drive-' + fileId + '/status.json'] = JSON.stringify(s);
  return s;
}

function writeEpub(env, fileId, name, bytes) {
  env.gh.files['drive-' + fileId + '/' + name] = bytes || Buffer.from([0x50, 0x4b, 0x03, 0x04, 0x65, 0x70, 0x75, 0x62]);
}

function status(env) {
  return JSON.parse(env.ctx.getStatus());
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

const tests = [];
function test(name, fn) { tests.push({ name, fn }); }

test('setup creates the folders and one 10-minute trigger, stores the default branch', () => {
  const env = makeEnv();
  env.props.set('GITHUB_TOKEN', TOKEN);
  env.gh.defaultBranch = 'trunk';
  env.triggers.push({ fn: 'oldThing', getHandlerFunction: () => 'oldThing' });
  const summary = env.ctx.setup();

  const roots = env.drive.children(env.drive.myDrive.id).filter((x) => x.name === F.root);
  assert.strictEqual(roots.length, 1);
  const subs = env.drive.children(roots[0].id).map((x) => x.name).sort();
  assert.deepStrictEqual(subs, [F.inbox, F.working, F.ready, F.originals, F.problem, F.final].sort());
  assert.strictEqual(env.triggers.length, 1);
  assert.strictEqual(env.triggers[0].fn, 'tick');
  assert.strictEqual(env.triggers[0].minutes, 10);
  assert.strictEqual(env.props.get('DEFAULT_REF'), 'trunk');
  assert.ok(/تم إعداد/.test(summary));
  assert.strictEqual(env.mails.length, 1);
  assert.strictEqual(env.mails[0].to, OWNER);
  assert.ok(/تم الإعداد/.test(env.mails[0].subject));
  assert.ok(env.logs.some((l) => /تم إعداد/.test(l)), 'Arabic summary logged');
});

test('setup is idempotent (reuses folders, still one trigger)', () => {
  const env = readyEnv();
  const ids = ['root', 'inbox', 'working', 'ready', 'originals', 'problem'].map((k) => env.props.get('FOLDER_' + k.toUpperCase()));
  env.ctx.setup();
  env.ctx.setup();
  const ids2 = ['root', 'inbox', 'working', 'ready', 'originals', 'problem'].map((k) => env.props.get('FOLDER_' + k.toUpperCase()));
  assert.deepStrictEqual(ids2, ids);
  assert.strictEqual(env.drive.children(env.drive.myDrive.id).filter((x) => x.name === F.root).length, 1);
  assert.strictEqual(env.drive.children(ids[0]).length, 6);
  assert.strictEqual(env.triggers.length, 1);
});

test('setup reuses a root folder the user already made, ignoring trashed copies', () => {
  const env = makeEnv();
  env.props.set('GITHUB_TOKEN', TOKEN);
  const trashed = new env.drive.MockFolder(F.root, env.drive.myDrive.id).setTrashed(true);
  const mine = new env.drive.MockFolder(F.root, env.drive.myDrive.id);
  const inbox = new env.drive.MockFolder(F.inbox, mine.id);
  env.ctx.setup();
  assert.strictEqual(env.props.get('FOLDER_ROOT'), mine.id);
  assert.strictEqual(env.props.get('FOLDER_INBOX'), inbox.id);
  assert.strictEqual(env.drive.children(trashed.id).length, 0);
});

test('setup without GITHUB_TOKEN throws an Arabic error pointing to step 3', () => {
  const env = makeEnv();
  assert.throws(() => env.ctx.setup(), (e) => /GITHUB_TOKEN/.test(e.message) && /الخطوة 3/.test(e.message));
  assert.strictEqual(env.triggers.length, 0);
  assert.strictEqual(env.drive.children(env.drive.myDrive.id).length, 0);
});

test('setup with a rejected token explains the fix and installs nothing', () => {
  const env = makeEnv();
  env.props.set('GITHUB_TOKEN', 'github_pat_WRONG');
  assert.throws(() => env.ctx.setup(), (e) => /الخطوة 1/.test(e.message));
  assert.strictEqual(env.triggers.length, 0);
});

test('setup stops with Arabic steps when the Drive API is off for the project', () => {
  const env = makeEnv();
  env.props.set('GITHUB_TOKEN', TOKEN);
  env.driveApiDisabled = true;
  assert.throws(() => env.ctx.setup(), (e) => /Drive API/.test(e.message) && /Services/.test(e.message));
  assert.strictEqual(env.triggers.length, 0);
  env.driveApiDisabled = false;
  env.ctx.setup();
  assert.strictEqual(env.triggers.length, 1);
  const check = env.requests.filter((r) => r.url.indexOf('googleapis.com/drive/v3/files/' + env.props.get('FOLDER_ROOT')) >= 0);
  assert.ok(check.length >= 1, 'setup asked the Drive API about the root folder');
});

test('setup warns when the workflow cannot be read (Actions permission)', () => {
  const env = makeEnv();
  env.props.set('GITHUB_TOKEN', TOKEN);
  env.gh.workflowExists = false;
  const summary = env.ctx.setup();
  assert.ok(/Read and write/.test(summary));
  assert.strictEqual(env.triggers.length, 1);
});

test('tick before setup does nothing', () => {
  const env = makeEnv();
  env.props.set('GITHUB_TOKEN', TOKEN);
  assert.strictEqual(env.ctx.tick(), 'not-set-up');
  assert.strictEqual(env.requests.length, 0);
});

test('new PDF: moved to قيد التحويل and dispatched with the contract inputs', () => {
  const env = readyEnv();
  const f = addBook(env, 'تاريخ الأندلس.pdf');
  tick(env);

  assert.deepStrictEqual(names(env, 'inbox'), []);
  assert.deepStrictEqual(names(env, 'working'), ['تاريخ الأندلس.pdf']);
  assert.strictEqual(env.gh.dispatches.length, 1);
  const d = lastDispatch(env);
  assert.deepStrictEqual(d.body, {
    ref: 'main',
    inputs: {
      drive_file_id: f.id,
      drive_token: 'ya29.drive-token-' + env.tokens,
      drive_file_name: 'تاريخ الأندلس.pdf',
      result_dir: 'drive-' + f.id,
      title: 'تاريخ الأندلس'
    }
  });
  assert.strictEqual(d.contentType, 'application/json');
  const req = env.requests.find((r) => r.method === 'POST');
  assert.strictEqual(req.url, 'https://api.github.com/repos/' + REPO + '/actions/workflows/convert.yml/dispatches');
  assert.strictEqual(req.headers.Authorization, 'Bearer ' + TOKEN);
  assert.strictEqual(req.headers.Accept, 'application/vnd.github+json');
  assert.strictEqual(req.headers['X-GitHub-Api-Version'], '2022-11-28');
  assert.strictEqual(req.muteHttpExceptions, true);

  const j = jobs(env)[f.id];
  assert.strictEqual(j.stage, 'running');
  assert.strictEqual(j.attempts, 1);
  assert.strictEqual(j.title, 'تاريخ الأندلس');
  assert.strictEqual(j.resultDir, 'drive-' + f.id);
  assert.strictEqual(j.dispatchedAt, iso(env.clock));
  const logs = env.logs.join('\n');
  assert.ok(logs.indexOf(TOKEN) < 0 && logs.indexOf('ya29.') < 0, 'no token in the log');
});

test('GITHUB_REF overrides the default branch', () => {
  const env = readyEnv();
  env.props.set('GITHUB_REF', 'drive-inbox');
  addBook(env, 'كتاب.pdf');
  tick(env);
  assert.strictEqual(lastDispatch(env).body.ref, 'drive-inbox');
});

test('a file modified less than 2 minutes ago, or empty, is skipped', () => {
  const env = readyEnv();
  const fresh = addBook(env, 'يرفع الآن.pdf', { ageMin: 30, updatedAgeMin: 1 });
  addBook(env, 'فارغ.pdf', { ageMin: 30, size: 0 });
  assert.ok(/idle/.test(tick(env)));
  assert.strictEqual(env.gh.dispatches.length, 0);
  assert.strictEqual(status(env).uploading, 2);
  minutes(env, 2);
  tick(env);
  assert.strictEqual(env.gh.dispatches.length, 1);
  assert.strictEqual(lastDispatch(env).body.inputs.drive_file_id, fresh.id);
  assert.deepStrictEqual(names(env, 'inbox'), ['فارغ.pdf']);
});

test('unsupported files, Google Docs and trashed files are ignored', () => {
  const env = readyEnv();
  addBook(env, 'صورة الغلاف.jpg', { mime: 'image/jpeg' });
  addBook(env, 'ملاحظات', { mime: 'application/vnd.google-apps.document' });
  addBook(env, 'اختصار.pdf', { mime: 'application/vnd.google-apps.shortcut' });
  addBook(env, 'محذوف.pdf').setTrashed(true);
  assert.ok(/idle/.test(tick(env)));
  assert.strictEqual(env.gh.dispatches.length, 0);
  assert.strictEqual(names(env, 'inbox').length, 3);
  const s = status(env);
  assert.strictEqual(s.queued, 0);
  assert.ok(s.unsupported.indexOf('صورة الغلاف.jpg') >= 0);
});

test('docx, txt, upper-case and extension-less files are accepted with the right names', () => {
  const env = readyEnv();
  const a = addBook(env, 'رواية', { mime: 'application/pdf', ageMin: 40 });
  const b = addBook(env, 'مقالات.DOCX', { mime: 'application/octet-stream', ageMin: 30 });
  const c = addBook(env, 'نص.txt', { ageMin: 20 });
  const seen = [];
  [a, b, c].forEach((f) => {
    tick(env);
    const d = lastDispatch(env).body.inputs;
    seen.push([d.drive_file_id, d.drive_file_name, d.title]);
    minutes(env, 5);
    writeStatus(env, f.id, 'done', { epub: 'x.epub' });
    writeEpub(env, f.id, 'x.epub');
    tick(env);
  });
  assert.deepStrictEqual(seen[0], [a.id, 'رواية.pdf', 'رواية']);
  assert.deepStrictEqual(seen[1], [b.id, 'مقالات.DOCX', 'مقالات']);
  assert.deepStrictEqual(seen[2], [c.id, 'نص.txt', 'نص']);
  assert.deepStrictEqual(names(env, 'final').filter((n) => /\.epub$/.test(n)), ['رواية.epub', 'مقالات.epub', 'نص.epub']);
});

test('done: EPUB and report in جاهز, original in الأصل, email with link and unsure pages', () => {
  const env = readyEnv();
  const f = addBook(env, 'كتاب الأمثال.pdf');
  tick(env);
  minutes(env, 30);
  writeStatus(env, f.id, 'done', { epub: 'كتاب الأمثال.epub', pages_total: 246, pages_done: 246, pages_unsure: [41, 12, 40] });
  const bytes = Buffer.from('PK\u0003\u0004 real epub bytes');
  writeEpub(env, f.id, 'كتاب الأمثال.epub', bytes);
  env.gh.files['drive-' + f.id + '/report.md'] = '# تقرير التحويل\n\n- الصفحات المطلوبة: 246\n\n**كل الصفحات اكتملت.**\n';
  assert.strictEqual(tick(env).split('; ')[0], f.id + ': delivered');

  assert.deepStrictEqual(names(env, 'ready'), ['كتاب الأمثال - تقرير.txt', 'كتاب الأمثال.epub']);
  const epub = filesIn(env, 'ready').find((x) => /\.epub$/.test(x.name));
  assert.strictEqual(epub.mime, 'application/epub+zip');
  assert.ok(epub.content.equals(bytes));
  const report = filesIn(env, 'ready').find((x) => /\.txt$/.test(x.name));
  assert.strictEqual(report.mime, 'text/plain');
  assert.ok(/تقرير التحويل/.test(report.content.toString('utf8')));
  assert.deepStrictEqual(names(env, 'originals'), ['كتاب الأمثال.pdf']);
  assert.deepStrictEqual(names(env, 'working'), []);
  assert.strictEqual(jobs(env)[f.id].stage, 'delivered');

  assert.strictEqual(env.mails.length, 1);
  const m = env.mails[0];
  assert.strictEqual(m.to, OWNER);
  assert.strictEqual(m.subject, 'جاهز: كتاب الأمثال');
  assert.ok(m.body.indexOf('https://drive.google.com/file/d/' + epub.id) >= 0, 'EPUB link in the email');
  assert.ok(m.body.indexOf('12، 40-41') >= 0, 'unsure pages listed');
  assert.ok(m.body.indexOf('كل الصفحات اكتملت.') >= 0 && m.body.indexOf('**') < 0, 'short report without markdown');
  assert.ok(/dir="rtl"/.test(m.htmlBody) && /<a href="https:\/\/drive\.google\.com\/file\/d\//.test(m.htmlBody));

  // Nothing happens twice.
  minutes(env, 10);
  tick(env);
  assert.strictEqual(env.mails.length, 1);
  assert.strictEqual(filesIn(env, 'ready').length, 2);
});

test('refine: next day an idle inbox re-runs a book with unsure pages and replaces its EPUB', () => {
  const env = readyEnv();
  const f = addBook(env, 'ب.pdf');
  tick(env);
  minutes(env, 30);
  writeStatus(env, f.id, 'done', { epub: 'ب.epub', pages_total: 10, pages_done: 10, pages_unsure: [3, 4, 5] });
  writeEpub(env, f.id, 'ب.epub', Buffer.from('PK old'));
  tick(env);
  assert.strictEqual(jobs(env)[f.id].stage, 'delivered');
  assert.strictEqual(env.gh.dispatches.length, 1, 'no refinement on the delivery day');
  minutes(env, 10);
  tick(env);
  assert.strictEqual(env.gh.dispatches.length, 1);

  env.clock += 24 * 3600000;
  tick(env);
  assert.strictEqual(env.gh.dispatches.length, 2, 'refinement round dispatched');
  assert.strictEqual(jobs(env)[f.id].stage, 'running');
  minutes(env, 20);
  writeStatus(env, f.id, 'done', { epub: 'ب.epub', pages_total: 10, pages_done: 10, pages_unsure: [5] });
  writeEpub(env, f.id, 'ب.epub', Buffer.from('PK new'));
  const mailsBefore = env.mails.length;
  tick(env);
  const epubs = filesIn(env, 'ready').filter((x) => /\.epub$/.test(x.name) && !x.trashed);
  assert.strictEqual(epubs.length, 1, 'old EPUB replaced');
  assert.ok(epubs[0].content.equals(Buffer.from('PK new')));
  assert.strictEqual(env.mails.length, mailsBefore + 1);
  assert.ok(/تحسّن/.test(env.mails[env.mails.length - 1].subject));

  // A round that does not lower the count ends the refinement for this book.
  env.clock += 24 * 3600000;
  tick(env);
  minutes(env, 20);
  writeStatus(env, f.id, 'done', { epub: 'ب.epub', pages_total: 10, pages_done: 10, pages_unsure: [5] });
  writeEpub(env, f.id, 'ب.epub', Buffer.from('PK same'));
  tick(env);
  assert.strictEqual(jobs(env)[f.id].refineDone, true);
  env.clock += 24 * 3600000;
  const n = env.gh.dispatches.length;
  tick(env);
  assert.strictEqual(env.gh.dispatches.length, n, 'no more rounds');
});

test('judging: a book waiting for the judges is parked, the next book starts, the parked one is judged the next day', () => {
  const env = readyEnv();
  const a = addBook(env, 'أول.pdf');
  tick(env);
  minutes(env, 3);
  const b = addBook(env, 'ثان.pdf');
  minutes(env, 30);
  writeStatus(env, a.id, 'paused', { waiting_for: 'judges', epub: 'أول.epub', pages_judging: [4, 9], pages_unsure: [4, 9] });
  tick(env);
  assert.strictEqual(jobs(env)[a.id].stage, 'judging');
  assert.ok(names(env, 'working').indexOf('أول.pdf') >= 0, 'parked book stays in the working folder');
  assert.strictEqual(env.gh.dispatches.length, 2, 'the next book started right away');
  assert.strictEqual(lastDispatch(env).body.inputs.drive_file_id, b.id);
  assert.ok(env.mails.some((m) => /ينتظر الحكّام/.test(m.subject)));

  minutes(env, 40);
  writeStatus(env, b.id, 'done', { epub: 'ثان.epub' });
  writeEpub(env, b.id, 'ثان.epub');
  tick(env);
  assert.strictEqual(jobs(env)[b.id].stage, 'delivered');
  minutes(env, 10);
  tick(env);
  assert.strictEqual(env.gh.dispatches.length, 2, 'not before the judges quota renews');

  env.clock += 24 * 3600000;
  tick(env);
  assert.strictEqual(env.gh.dispatches.length, 3);
  assert.strictEqual(lastDispatch(env).body.inputs.drive_file_id, a.id, 'parked book first');
  assert.strictEqual(lastDispatch(env).body.inputs.result_dir, 'drive-' + a.id);
  minutes(env, 15);
  writeStatus(env, a.id, 'done', { epub: 'أول.epub', pages_unsure: [9] });
  writeEpub(env, a.id, 'أول.epub');
  tick(env);
  assert.strictEqual(jobs(env)[a.id].stage, 'delivered');
  assert.ok(names(env, 'ready').indexOf('أول.epub') >= 0);
});

test('judgeNow: the judges see a delivered book today; a round cut short by their quota delivers and goes on', () => {
  const env = readyEnv();
  const f = addBook(env, 'ج.pdf');
  tick(env);
  minutes(env, 30);
  writeStatus(env, f.id, 'done', { epub: 'ج.epub', pages_unsure: [1, 2, 3, 4] });
  writeEpub(env, f.id, 'ج.epub', Buffer.from('PK old'));
  tick(env);
  assert.strictEqual(jobs(env)[f.id].stage, 'delivered');
  minutes(env, 10);
  tick(env);
  assert.strictEqual(env.gh.dispatches.length, 1, 'no round on the delivery day by itself');

  env.ctx.judgeNow();
  assert.strictEqual(env.gh.dispatches.length, 2, 'judgeNow starts a round today');
  assert.strictEqual(lastDispatch(env).body.inputs.result_dir, 'drive-' + f.id);
  minutes(env, 20);
  writeStatus(env, f.id, 'paused', { waiting_for: 'judges', epub: 'ج.epub', pages_unsure: [3, 4], pages_judging: [3, 4] });
  writeEpub(env, f.id, 'ج.epub', Buffer.from('PK better'));
  tick(env);
  const epubs = filesIn(env, 'ready').filter((x) => /\.epub$/.test(x.name));
  assert.strictEqual(epubs.length, 1, 'old EPUB replaced right away');
  assert.ok(epubs[0].content.equals(Buffer.from('PK better')));
  assert.ok(/تحسّن/.test(env.mails[env.mails.length - 1].subject));
  assert.strictEqual(jobs(env)[f.id].stage, 'delivered');
  assert.notStrictEqual(jobs(env)[f.id].refineDone, true);

  // Next day: no judge quota at all, nothing improves; the book is not given up on.
  env.clock += 24 * 3600000;
  tick(env);
  assert.strictEqual(env.gh.dispatches.length, 3);
  minutes(env, 20);
  writeStatus(env, f.id, 'paused', { waiting_for: 'judges', epub: 'ج.epub', pages_unsure: [3, 4], pages_judging: [3, 4] });
  writeEpub(env, f.id, 'ج.epub', Buffer.from('PK same'));
  tick(env);
  assert.notStrictEqual(jobs(env)[f.id].refineDone, true, 'a quota-short round does not end refinement');
  env.clock += 24 * 3600000;
  tick(env);
  assert.strictEqual(env.gh.dispatches.length, 4, 'and the rounds go on');
});

test('judgeNow: a book delivered by an older script (no unsure count, no report id) is judged too', () => {
  const env = readyEnv();
  const f = addBook(env, 'د.pdf');
  tick(env);
  minutes(env, 30);
  writeStatus(env, f.id, 'done', { epub: 'د.epub', pages_unsure: [1, 2, 3] });
  writeEpub(env, f.id, 'د.epub', Buffer.from('PK old'));
  env.gh.files['drive-' + f.id + '/report.md'] = 'تقرير قديم';
  tick(env);
  assert.strictEqual(names(env, 'ready').filter((n) => /تقرير/.test(n)).length, 1);
  const all = jobs(env);
  ['unsure', 'reportId', 'refineDay'].forEach((k) => { delete all[f.id][k]; });
  env.props.set('JOBS', JSON.stringify(all));
  minutes(env, 10);
  env.ctx.judgeNow();
  assert.strictEqual(env.gh.dispatches.length, 2, 'round started');
  assert.strictEqual(jobs(env)[f.id].unsureBefore, 3);
  minutes(env, 20);
  writeStatus(env, f.id, 'paused', { waiting_for: 'judges', epub: 'د.epub', pages_unsure: [3], pages_judging: [3] });
  writeEpub(env, f.id, 'د.epub', Buffer.from('PK new'));
  tick(env);
  const ready = names(env, 'ready');
  assert.strictEqual(ready.filter((n) => /تقرير/.test(n)).length, 1, 'old report replaced, not duplicated');
  assert.strictEqual(ready.filter((n) => /\.epub$/.test(n)).length, 1);
});

test('judgeNow with nothing unsure does nothing', () => {
  const env = readyEnv();
  assert.strictEqual(env.ctx.judgeNow(), 'nothing to judge');
  assert.strictEqual(env.gh.dispatches.length, 0);
});

test('done without report.md still delivers; done without the EPUB file is retried', () => {
  const env = readyEnv();
  const f = addBook(env, 'أ.pdf');
  tick(env);
  minutes(env, 20);
  writeStatus(env, f.id, 'done', { epub: 'أ.epub' });   // EPUB missing
  tick(env);
  assert.strictEqual(env.gh.dispatches.length, 2);
  assert.strictEqual(jobs(env)[f.id].attempts, 2);
  minutes(env, 20);
  writeStatus(env, f.id, 'done', { epub: 'أ.epub' });
  writeEpub(env, f.id, 'أ.epub');
  tick(env);
  assert.deepStrictEqual(names(env, 'final'), ['أ.epub']);
  assert.strictEqual(jobs(env)[f.id].stage, 'delivered');
});

test('stale status.json (updated_at before the dispatch) is ignored', () => {
  const env = readyEnv();
  const f = addBook(env, 'قديم.pdf');
  writeStatus(env, f.id, 'done', { epub: 'قديم.epub', updated_at: ghTime(env.clock - 3600000) });
  writeEpub(env, f.id, 'قديم.epub');
  tick(env);
  env.gh.runs = [{ id: 1, status: 'in_progress', created_at: ghTime(env.clock + 10000) }];
  minutes(env, 30);
  assert.ok(/running/.test(tick(env)));
  assert.deepStrictEqual(names(env, 'ready'), []);
  assert.strictEqual(jobs(env)[f.id].stage, 'running');
  assert.strictEqual(env.gh.dispatches.length, 1);
  assert.strictEqual(env.mails.length, 0);
});

test('paused: no restart on the same Pacific day (even after Riyadh midnight), restart next day without counting an attempt', () => {
  const env = readyEnv({ start: '2026-10-19T19:00:00Z' });   // Riyadh 22:00, Pacific 12:00
  const f = addBook(env, 'موسوعة.pdf');
  tick(env);
  const firstToken = lastDispatch(env).body.inputs.drive_token;
  minutes(env, 60);
  writeStatus(env, f.id, 'paused', { quota_day: '2026-10-19', pages_total: 900, pages_done: 300 });
  env.gh.runs = [{ id: 1, status: 'completed', created_at: ghTime(env.clock - 3500000) }];
  tick(env);
  let j = jobs(env)[f.id];
  assert.strictEqual(j.pausedDay, '2026-10-19');
  assert.strictEqual(j.attempts, 1);
  assert.strictEqual(env.gh.dispatches.length, 1);
  assert.strictEqual(env.mails.length, 1);
  assert.ok(/سيكمل غداً/.test(env.mails[0].subject));
  assert.ok(env.mails[0].body.indexOf('10:00') >= 0, 'resume time in Riyadh time (Pacific midnight = 10:00)');
  const s = status(env);
  assert.strictEqual(s.active.paused, true);
  assert.strictEqual(s.active.resumeAt, '2026-10-20T07:00:00.000Z');
  assert.strictEqual(s.active.pagesDone, 300);

  env.clock = Date.parse('2026-10-20T03:00:00Z');   // Riyadh 06:00 on the 20th, Pacific still the 19th
  tick(env);
  assert.strictEqual(env.gh.dispatches.length, 1, 'no restart before Pacific midnight');

  env.clock = Date.parse('2026-10-20T07:05:00Z');   // Pacific 00:05 on the 20th
  tick(env);
  assert.strictEqual(env.gh.dispatches.length, 2);
  assert.notStrictEqual(lastDispatch(env).body.inputs.drive_token, firstToken, 'fresh Drive token');
  j = jobs(env)[f.id];
  assert.strictEqual(j.attempts, 1, 'a quota restart is not an attempt');
  assert.strictEqual(j.pausedDay, '');
  assert.strictEqual(j.stage, 'running');

  // The old paused status is now stale and must not pause the job again.
  env.gh.runs = [{ id: 2, status: 'queued', created_at: ghTime(env.clock + 5000) }];
  minutes(env, 10);
  tick(env);
  assert.strictEqual(jobs(env)[f.id].pausedDay, '');

  // A second pause does not email again.
  minutes(env, 90);
  writeStatus(env, f.id, 'paused', { quota_day: '2026-10-20' });
  env.gh.runs[0].status = 'completed';
  tick(env);
  assert.strictEqual(jobs(env)[f.id].pausedDay, '2026-10-20');
  assert.strictEqual(env.mails.length, 1);
});

test('paused restart that GitHub rejects is retried later and still not counted', () => {
  const env = readyEnv({ start: '2026-10-19T19:00:00Z' });
  const f = addBook(env, 'كبير.pdf');
  tick(env);
  minutes(env, 60);
  writeStatus(env, f.id, 'paused', { quota_day: '2026-10-19' });
  tick(env);
  env.clock = Date.parse('2026-10-20T07:05:00Z');
  env.gh.dispatchCode = 500;
  tick(env);
  assert.strictEqual(jobs(env)[f.id].stage, 'queued');
  assert.strictEqual(env.mails.length, 1, 'a 500 is retried silently (only the pause email)');
  env.gh.dispatchCode = 204;
  minutes(env, 10);
  tick(env);
  const j = jobs(env)[f.id];
  assert.strictEqual(j.stage, 'running');
  assert.strictEqual(j.attempts, 1);
});

test('incomplete: retried up to 3 attempts, then delivered with the suffix and missing pages', () => {
  const env = readyEnv();
  const f = addBook(env, 'ديوان.pdf');
  tick(env);
  for (let attempt = 1; attempt <= 3; attempt++) {
    minutes(env, 40);
    writeStatus(env, f.id, 'incomplete', { epub: 'ديوان.epub', pages_total: 50, pages_done: 46, pages_missing: [11, 5, 9, 10] });
    writeEpub(env, f.id, 'ديوان.epub');
    tick(env);
    if (attempt < 3) {
      assert.strictEqual(jobs(env)[f.id].attempts, attempt + 1);
      assert.strictEqual(filesIn(env, 'ready').length, 0);
    }
  }
  assert.strictEqual(env.gh.dispatches.length, 3);
  assert.deepStrictEqual(names(env, 'ready').filter((n) => /\.epub$/.test(n)), ['ديوان (فيه صفحات ناقصة).epub']);
  assert.deepStrictEqual(names(env, 'originals'), ['ديوان.pdf']);
  assert.strictEqual(env.mails.length, 1);
  assert.strictEqual(env.mails[0].subject, 'جاهز مع صفحات ناقصة: ديوان');
  assert.ok(env.mails[0].body.indexOf('5، 9-11') >= 0);
  assert.strictEqual(status(env).recent[0].incomplete, true);
});

test('failed twice: original goes to مشكلة with a note, and an email', () => {
  const env = readyEnv();
  const f = addBook(env, 'كتاب تالف.pdf');
  tick(env);
  minutes(env, 10);
  writeStatus(env, f.id, 'failed');
  tick(env);
  assert.strictEqual(env.gh.dispatches.length, 2);
  assert.strictEqual(filesIn(env, 'problem').length, 0);
  minutes(env, 10);
  writeStatus(env, f.id, 'failed');
  assert.ok(/problem/.test(tick(env)));

  assert.deepStrictEqual(names(env, 'problem'), ['كتاب تالف - سبب المشكلة.txt', 'كتاب تالف.pdf']);
  const note = filesIn(env, 'problem').find((x) => /\.txt$/.test(x.name));
  assert.ok(/السبب: فشل التحويل مرتين/.test(note.content.toString('utf8')));
  assert.deepStrictEqual(names(env, 'working'), []);
  assert.strictEqual(jobs(env)[f.id].stage, 'problem');
  assert.strictEqual(env.mails.length, 1);
  assert.ok(/مشكلة/.test(env.mails[0].subject));
  assert.ok(env.mails[0].body.indexOf(folder(env, 'problem').getUrl()) >= 0);
  const s = status(env);
  assert.strictEqual(s.recent[0].stage, 'problem');
  assert.ok(s.recent[0].reason.length > 0);
});

test('a book moved back from مشكلة to جديد is converted again from scratch', () => {
  const env = readyEnv();
  const f = addBook(env, 'مرة أخرى.pdf');
  tick(env);
  for (let i = 0; i < 2; i++) { minutes(env, 10); writeStatus(env, f.id, 'failed'); tick(env); }
  assert.strictEqual(jobs(env)[f.id].stage, 'problem');
  f.moveTo(folder(env, 'inbox'));
  minutes(env, 10);
  tick(env);
  const j = jobs(env)[f.id];
  assert.strictEqual(j.stage, 'running');
  assert.strictEqual(j.attempts, 1);
  assert.strictEqual(env.gh.dispatches.length, 3);
});

test('lost dispatch: waits while a run is active, re-dispatches after 20 minutes, then gives up', () => {
  const env = readyEnv();
  const f = addBook(env, 'ضائع.pdf');
  const t0 = env.clock;
  tick(env);
  minutes(env, 10);
  tick(env);
  assert.strictEqual(env.gh.dispatches.length, 1, 'too early to call it lost');
  minutes(env, 11);
  env.gh.runs = [{ id: 1, status: 'in_progress', created_at: ghTime(t0 + 30000) }];
  tick(env);
  assert.strictEqual(env.gh.dispatches.length, 1, 'a run is still going');
  // An older run that is still going is not ours; ours has completed without a status.
  env.gh.runs = [
    { id: 2, status: 'completed', created_at: ghTime(t0 + 30000) },
    { id: 0, status: 'in_progress', created_at: ghTime(t0 - 10 * 60000) }
  ];
  tick(env);
  assert.strictEqual(env.gh.dispatches.length, 2);
  assert.strictEqual(jobs(env)[f.id].attempts, 2);
  minutes(env, 21);
  tick(env);
  assert.strictEqual(jobs(env)[f.id].attempts, 3);
  minutes(env, 21);
  tick(env);
  assert.strictEqual(jobs(env)[f.id].stage, 'problem');
  assert.deepStrictEqual(names(env, 'problem'), ['ضائع - سبب المشكلة.txt', 'ضائع.pdf']);
  assert.strictEqual(env.mails.length, 1);
});

test('no status for more than 8 hours: problem', () => {
  const env = readyEnv();
  const f = addBook(env, 'بطيء.pdf');
  tick(env);
  env.gh.runs = [{ id: 1, status: 'in_progress', created_at: ghTime(env.clock + 30000) }];
  minutes(env, 7 * 60);
  tick(env);
  assert.strictEqual(jobs(env)[f.id].stage, 'running');
  minutes(env, 61);
  tick(env);
  assert.strictEqual(jobs(env)[f.id].stage, 'problem');
  assert.ok(/8 ساعات/.test(env.mails[0].body));
});

test('GitHub 401: the book waits in قيد التحويل and the user gets one email per day', () => {
  const env = readyEnv({ start: '2026-10-19T06:00:00Z' });   // Riyadh 09:00
  const f = addBook(env, 'ينتظر.pdf');
  env.gh.authCode = 401;
  tick(env);
  let j = jobs(env)[f.id];
  assert.strictEqual(j.stage, 'queued');
  assert.ok(/401/.test(j.lastError));
  assert.deepStrictEqual(names(env, 'working'), ['ينتظر.pdf']);
  assert.strictEqual(env.mails.length, 1);
  assert.ok(/رمز GitHub/.test(env.mails[0].subject));
  assert.ok(/GITHUB_TOKEN/.test(env.mails[0].body) && /الخطوة 1/.test(env.mails[0].body));
  for (let i = 0; i < 5; i++) { minutes(env, 10); tick(env); }
  assert.strictEqual(env.mails.length, 1, 'once per day');
  env.clock = Date.parse('2026-10-19T21:30:00Z');   // Riyadh 00:30 the next day
  tick(env);
  assert.strictEqual(env.mails.length, 2, 'again the next day');
  assert.strictEqual(status(env).active.stage, 'queued');

  env.gh.authCode = null;   // the user replaced the token
  minutes(env, 10);
  tick(env);
  j = jobs(env)[f.id];
  assert.strictEqual(j.stage, 'running');
  assert.strictEqual(j.attempts, 1);
  assert.strictEqual(j.lastError, '');
});

test('GitHub 403 while reading the status also warns once a day; a rate limit does not', () => {
  const env = readyEnv();
  const f = addBook(env, 'صلاحيات.pdf');
  tick(env);
  minutes(env, 10);
  env.gh.authCode = 403;
  tick(env);
  tick(env);
  assert.strictEqual(env.mails.length, 1);
  assert.strictEqual(jobs(env)[f.id].stage, 'running', 'job untouched');
  // Rate limit: no new warning.
  env.gh.authCode = null;
  const orig = env.gh.handle;
  env.gh.handle = (req) => ({ code: 403, body: JSON.stringify({ message: 'API rate limit exceeded' }), headers: {} });
  env.clock += 24 * 3600000;
  tick(env);
  env.gh.handle = orig;
  assert.strictEqual(env.mails.length, 1);
});

test('422 (ref without the Drive inputs): error recorded, one email', () => {
  const env = readyEnv();
  const f = addBook(env, 'مدخلات.pdf');
  env.gh.dispatchCode = 422;
  tick(env);
  const j = jobs(env)[f.id];
  assert.strictEqual(j.stage, 'queued');
  assert.ok(/422/.test(j.lastError) && /Unexpected inputs/.test(j.lastError));
  assert.strictEqual(env.mails.length, 1);
  assert.ok(/main/.test(env.mails[0].body));
  minutes(env, 10);
  tick(env);
  assert.strictEqual(env.mails.length, 1);
});

test('network failure on dispatch is retried silently', () => {
  const env = readyEnv();
  const f = addBook(env, 'شبكة.pdf');
  env.gh.networkDown = true;
  tick(env);
  assert.strictEqual(jobs(env)[f.id].stage, 'queued');
  assert.strictEqual(env.mails.length, 0);
  env.gh.networkDown = false;
  minutes(env, 10);
  tick(env);
  assert.strictEqual(jobs(env)[f.id].stage, 'running');
  assert.strictEqual(jobs(env)[f.id].attempts, 1);
});

test('only one active job when several books are queued; the next starts when one finishes', () => {
  const env = readyEnv();
  const b1 = addBook(env, 'الثاني.pdf', { ageMin: 20 });
  const b0 = addBook(env, 'الأول.pdf', { ageMin: 30 });
  const b2 = addBook(env, 'الثالث.pdf', { ageMin: 10 });
  tick(env);
  assert.strictEqual(env.gh.dispatches.length, 1);
  assert.strictEqual(lastDispatch(env).body.inputs.drive_file_id, b0.id, 'oldest first');
  assert.deepStrictEqual(names(env, 'working'), ['الأول.pdf']);
  assert.strictEqual(names(env, 'inbox').length, 2);
  env.gh.runs = [{ id: 1, status: 'in_progress', created_at: ghTime(env.clock + 5000) }];
  for (let i = 0; i < 3; i++) { minutes(env, 10); tick(env); }
  assert.strictEqual(env.gh.dispatches.length, 1);

  writeStatus(env, b0.id, 'done', { epub: 'الأول.epub' });
  writeEpub(env, b0.id, 'الأول.epub');
  tick(env);
  assert.strictEqual(env.gh.dispatches.length, 2, 'next book started in the same check');
  assert.strictEqual(lastDispatch(env).body.inputs.drive_file_id, b1.id);
  assert.deepStrictEqual(names(env, 'working'), ['الثاني.pdf']);
  assert.ok(b2);
});

test('a book left in قيد التحويل without a job is picked up again', () => {
  const env = readyEnv();
  const f = addBook(env, 'يتيم.pdf', { folder: 'working' });
  addBook(env, 'جديد.pdf', { ageMin: 60 });
  tick(env);
  assert.strictEqual(lastDispatch(env).body.inputs.drive_file_id, f.id);
  assert.strictEqual(names(env, 'inbox').length, 1);
});

test('a book that cannot be moved is skipped in favour of the next one', () => {
  const env = readyEnv();
  const bad = addBook(env, 'مقفل.pdf', { ageMin: 30 });
  const good = addBook(env, 'عادي.pdf', { ageMin: 20 });
  env.failMove.add(bad.id);
  tick(env);
  assert.strictEqual(lastDispatch(env).body.inputs.drive_file_id, good.id);
});

test('busy lock: the check is skipped', () => {
  const env = readyEnv();
  addBook(env, 'كتاب.pdf');
  env.lockBusy = true;
  assert.strictEqual(env.ctx.tick(), 'busy');
  assert.strictEqual(env.requests.length, 0);
});

test('token expiry: warned two weeks ahead and three days ahead, once each', () => {
  const env = readyEnv();
  env.gh.tokenExpiry = '2026-10-29 08:00:00 UTC';
  addBook(env, 'كتاب.pdf');
  tick(env);
  const warnings = () => env.mails.filter((m) => /تنتهي صلاحيته/.test(m.subject)).length;
  assert.strictEqual(warnings(), 1);
  assert.strictEqual(env.props.get('TOKEN_EXPIRES'), '2026-10-29T08:00:00Z');
  minutes(env, 10);
  tick(env);
  assert.strictEqual(warnings(), 1);
  env.clock = Date.parse('2026-10-27T08:30:00Z');
  tick(env);
  assert.strictEqual(warnings(), 2);
  tick(env);
  assert.strictEqual(warnings(), 2);
  assert.strictEqual(status(env).tokenExpires, '2026-10-29T08:00:00Z');
});

test('getStatus returns sane JSON for the page', () => {
  const env = readyEnv();
  const f = addBook(env, 'الأول.pdf', { ageMin: 30 });
  addBook(env, 'التالي.pdf', { ageMin: 20 });
  let s = status(env);
  assert.strictEqual(s.ok, true);
  assert.strictEqual(s.active, null);
  assert.strictEqual(s.queued, 2);
  tick(env);
  minutes(env, 75);
  s = status(env);
  assert.strictEqual(s.active.title, 'الأول');
  assert.strictEqual(s.active.stage, 'running');
  assert.strictEqual(s.active.minutes, 75);
  assert.strictEqual(s.queued, 1);
  assert.deepStrictEqual(s.recent, []);
  assert.ok(/^https:\/\/drive\.google\.com\/drive\/folders\//.test(s.links.inbox));
  assert.ok(s.lastTick);
  writeStatus(env, f.id, 'done', { epub: 'الأول.epub' });
  writeEpub(env, f.id, 'الأول.epub');
  tick(env);
  s = status(env);
  assert.strictEqual(s.recent.length, 1);
  assert.strictEqual(s.recent[0].title, 'الأول');
  assert.strictEqual(s.recent[0].stage, 'delivered');
  assert.ok(/^https:\/\/drive\.google\.com\/file\/d\//.test(s.recent[0].url));
  assert.strictEqual(s.active.title, 'التالي');
  const raw = env.ctx.getStatus();
  assert.ok(raw.indexOf(TOKEN) < 0 && raw.indexOf('ya29.') < 0, 'no secrets in the page data');
});

test('the page shows the last 10 finished books, newest first', () => {
  const env = readyEnv();
  for (let i = 1; i <= 12; i++) {
    const f = addBook(env, 'كتاب رقم ' + i + '.pdf');
    tick(env);
    minutes(env, 5);
    writeStatus(env, f.id, 'done', { epub: 'b.epub' });
    writeEpub(env, f.id, 'b.epub');
    tick(env);
    minutes(env, 5);
  }
  const s = status(env);
  assert.strictEqual(s.recent.length, 10);
  assert.strictEqual(s.recent[0].title, 'كتاب رقم 12');
  assert.strictEqual(s.recent[9].title, 'كتاب رقم 3');
});

test('JOBS stays under the 9 KB property limit and keeps the active job', () => {
  const env = readyEnv();
  const long = 'كتاب طويل العنوان جداً في تاريخ الحضارة الإنسانية والفكر والأدب العربي';
  const map = {};
  for (let i = 0; i < 70; i++) {
    const id = '1Z' + String(i).padStart(5, '0') + 'aBcDeFgHiJkLmNoPqRsTuVwX';
    map[id] = {
      fileId: id, name: long + ' ' + i + '.pdf', title: long + ' ' + i, stage: i % 5 ? 'delivered' : 'problem',
      dispatchedAt: iso(env.clock), attempts: 2, pausedDay: '', lastState: 'done', resultDir: 'drive-' + id,
      lastError: 'x'.repeat(300), epubId: 'E' + id, finishedAt: iso(env.clock + i * 60000)
    };
  }
  const active = '1ACTIVEaBcDeFgHiJkLmNoPqRsTuVwXyZ0';
  map[active] = { fileId: active, name: long + '.pdf', title: long, stage: 'running', dispatchedAt: iso(env.clock), attempts: 1, resultDir: 'drive-' + active, createdAt: iso(env.clock) };
  env.ctx.saveJobs_(map);
  const raw = env.props.get('JOBS');
  assert.ok(Buffer.byteLength(raw, 'utf8') <= 8000, 'JOBS is ' + Buffer.byteLength(raw, 'utf8') + ' bytes');
  const saved = JSON.parse(raw);
  const finished = Object.values(saved).filter((j) => j.stage !== 'running');
  assert.ok(finished.length <= 50 && finished.length >= 10, finished.length + ' finished jobs kept');
  assert.ok(saved[active], 'active job kept');
  const newest = finished.map((j) => j.finishedAt).sort().pop();
  assert.strictEqual(newest, iso(env.clock + 69 * 60000), 'newest kept');
});

test('doGet serves the Arabic page with the status embedded safely', () => {
  const env = readyEnv();
  addBook(env, 'عنوان </script><script>alert(1)</script>.pdf');
  tick(env);
  const out = env.ctx.doGet();
  assert.strictEqual(out.getTitle(), 'تحويل الكتب');
  assert.ok(/width=device-width/.test(out.meta.viewport));
  assert.strictEqual(out.xfo, 'DEFAULT');
  const html = out.getContent();
  assert.ok(/<html lang="ar" dir="rtl">/.test(html));
  assert.ok(html.indexOf('افحص الآن') >= 0);
  assert.strictEqual((html.match(/<\/script>/g) || []).length, 1, 'title cannot close the script element');
  const m = /var INITIAL = (.*);\n/.exec(html);
  assert.ok(m, 'initial data present');
  const data = JSON.parse(m[1]);
  assert.ok(/alert/.test(data.active.title));
});

test('Page.html loads nothing from outside and supports dark mode', () => {
  assert.ok(!/<script[^>]+src=/i.test(PAGE));
  assert.ok(!/<link\b/i.test(PAGE));
  assert.ok(!/@import|url\(/i.test(PAGE));
  assert.ok(!/https?:\/\//.test(PAGE));
  assert.ok(/prefers-color-scheme:\s*dark/.test(PAGE));
  assert.ok(/google\.script\.run[\s\S]*\.tick\(\)/.test(PAGE));
  assert.ok(/\.getStatus\(\)/.test(PAGE));
});

test('the page script renders running, paused, finished and error states', () => {
  const env = readyEnv({ start: '2026-10-19T19:00:00Z' });
  const done = addBook(env, 'منتهٍ.pdf', { ageMin: 40 });
  const big = addBook(env, 'موسوعة كبيرة.pdf', { ageMin: 30 });
  addBook(env, 'صورة.png', { mime: 'image/png' });
  tick(env);
  minutes(env, 5);
  writeStatus(env, done.id, 'done', { epub: 'a.epub' });
  writeEpub(env, done.id, 'a.epub');
  tick(env);   // delivers the first, starts the big one
  minutes(env, 95);

  // Minimal DOM: enough for el(), link() and render().
  const byId = {};
  const node = (tag) => ({
    tag, kids: [], className: '', style: {}, own: '', disabled: false,
    set textContent(v) { this.own = String(v); this.kids = []; },
    get textContent() { return [this.own].concat(this.kids.map((k) => k.textContent)).join(' '); },
    appendChild(c) { this.kids.push(c); return c; },
    addEventListener() {}
  });
  const chain = { withSuccessHandler: () => chain, withFailureHandler: () => chain, getStatus() {}, tick() {} };
  const sandbox = {
    document: {
      hidden: false,
      createElement: node,
      createTextNode: (t) => ({ textContent: String(t) }),
      getElementById: (id) => byId[id] || (byId[id] = node('div')),
      addEventListener() {}
    },
    google: { script: { run: chain } },
    setInterval: () => 0
  };
  const html = env.ctx.doGet().getContent();
  const script = /<script>([\s\S]*?)<\/script>/.exec(html)[1];
  vm.createContext(sandbox);
  vm.runInContext(script, sandbox);

  let text = byId.now.textContent;
  assert.ok(text.indexOf('موسوعة كبيرة') >= 0 && /قيد التحويل منذ ساعة و35 دقيقة/.test(text), text);
  assert.ok(/منتهٍ/.test(byId.recent.textContent) && /افتح الكتاب/.test(byId.recent.textContent));
  assert.ok(/صورة\.png/.test(byId.queue.textContent));
  assert.ok(/آخر فحص/.test(byId.sub.textContent));

  writeStatus(env, big.id, 'paused', { quota_day: '2026-10-19', pages_total: 800, pages_done: 200 });
  tick(env);
  sandbox.render(JSON.parse(env.ctx.getStatus()));
  text = byId.now.textContent;
  assert.ok(/متوقف حتى الغد/.test(text) && /200 من 800/.test(text), text);
  assert.strictEqual(byId.now.kids.find((k) => k.className === 'bar').kids[0].style.width, '25%');

  sandbox.render({ ok: false, error: 'boom' });
  assert.ok(/تعذّر قراءة الحالة: boom/.test(byId.now.textContent));
});

test('Pacific midnight is found in summer and winter time', () => {
  const env = makeEnv();
  assert.strictEqual(env.ctx.pacificMidnightAfter_('2026-10-19').toISOString(), '2026-10-20T07:00:00.000Z');
  assert.strictEqual(env.ctx.pacificMidnightAfter_('2026-12-01').toISOString(), '2026-12-02T08:00:00.000Z');
  assert.strictEqual(env.ctx.pacificMidnightAfter_('2026-10-31').toISOString(), '2026-11-01T07:00:00.000Z');
  assert.strictEqual(env.ctx.pacificMidnightAfter_('2026-11-01').toISOString(), '2026-11-02T08:00:00.000Z');
});

test('appsscript.json has the expected manifest', () => {
  const m = JSON.parse(fs.readFileSync(path.join(SCRIPT_DIR, 'appsscript.json'), 'utf8'));
  assert.strictEqual(m.timeZone, 'Asia/Riyadh');
  assert.strictEqual(m.runtimeVersion, 'V8');
  assert.strictEqual(m.exceptionLogging, 'STACKDRIVER');
  assert.deepStrictEqual(m.oauthScopes.slice().sort(), [
    'https://www.googleapis.com/auth/drive',
    'https://www.googleapis.com/auth/script.external_request',
    'https://www.googleapis.com/auth/script.scriptapp',
    'https://www.googleapis.com/auth/script.send_mail',
    'https://www.googleapis.com/auth/userinfo.email'
  ]);
  assert.deepStrictEqual(m.webapp, { executeAs: 'USER_DEPLOYING', access: 'MYSELF' });
});

test('the fake GitHub saw only known endpoints', () => {
  // Re-run a full happy path and make sure every request matched a route.
  const env = readyEnv();
  const f = addBook(env, 'مسار.pdf');
  tick(env);
  minutes(env, 25);
  tick(env);
  writeStatus(env, f.id, 'done', { epub: 'مسار.epub' });
  writeEpub(env, f.id, 'مسار.epub');
  tick(env);
  assert.deepStrictEqual(env.gh.unknown, []);
});

// ---------------------------------------------------------------------------

let failed = 0;
for (const t of tests) {
  try {
    t.fn();
    console.log('PASS  ' + t.name);
  } catch (err) {
    failed++;
    const lines = String(err && err.stack ? err.stack : err).split('\n').slice(0, 6);
    console.log('FAIL  ' + t.name + '\n        ' + lines.join('\n        '));
  }
}
console.log('\n' + (tests.length - failed) + ' of ' + tests.length + ' tests passed' + (failed ? ', ' + failed + ' FAILED' : ''));
process.exit(failed ? 1 : 0);
