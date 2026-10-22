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
    this.bytes = Buffer.isBuffer(data) || Array.isArray(data) ? Buffer.from(data) : Buffer.from(data === undefined || data === null ? '' : String(data), 'utf8');
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
    unknown: [],
    // Files in the default branch (the helper's program).
    repoFiles: {}
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
      const ref = u.searchParams.get('ref');
      const store = ref === 'results' ? gh.files : ref === gh.defaultBranch ? gh.repoFiles : null;
      if (!store) return json(404, { message: 'No commit found for the ref' });
      const filePath = p.slice((base + '/contents/').length).split('/').map(decodeURIComponent).join('/');
      if (req.headers.Accept === 'application/vnd.github+json') {
        // A directory listing.
        const prefix = filePath + '/';
        const list = Object.keys(store).filter((k) => k.indexOf(prefix) === 0 && k.slice(prefix.length).indexOf('/') < 0)
          .map((k) => ({ type: 'file', name: k.slice(prefix.length), path: k,
            sha: require('crypto').createHash('sha1').update(store[k]).digest('hex') }));
        return list.length ? json(200, list) : json(404, { message: 'Not Found' });
      }
      if (req.headers.Accept !== 'application/vnd.github.raw') return json(415, { message: 'tests expect the raw media type' });
      if (!Object.prototype.hasOwnProperty.call(store, filePath)) return json(404, { message: 'Not Found' });
      const v = store[filePath];
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
    setContent(text) { this.content = Buffer.from(String(text), 'utf8'); this.updated = env.clock; return this; }
    setSharing(access, permission) { this.sharing = access + '/' + permission; return this; }
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
    },
    Access: { ANYONE_WITH_LINK: 'ANYONE_WITH_LINK' },
    Permission: { VIEW: 'VIEW' }
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
        getContent: () => Array.from(body),
        getBlob: () => new MockBlob(body, (res.headers || {})['Content-Type'] || 'application/octet-stream', null),
        getHeaders: () => Object.assign({}, res.headers),
        getAllHeaders: () => Object.assign({}, res.headers)
      };
    }
  };

  env.serviceUrl = 'https://script.google.com/macros/s/AKfyTEST/exec';
  const ScriptApp = {
    getOAuthToken() { env.tokens++; return 'ya29.drive-token-' + env.tokens; },
    getService: () => ({ getUrl: () => env.serviceUrl }),
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
    getUserLock: () => ({
      tryLock: () => {
        if (env.userLockHeld) return false;
        env.userLockHeld = true;
        return true;
      },
      releaseLock: () => { env.userLockHeld = false; }
    }),
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
    base64Encode: (bytes) => Buffer.from(bytes).toString('base64'),
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
    // Written by the current workflow; a status without it is from an older one.
    review_items: 0,
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

test('no free judges: a delivered book with unsure pages is never run again; its places wait for the user and their models', () => {
  const env = readyEnv();
  const f = addBook(env, 'ب.pdf');
  tick(env);
  minutes(env, 30);
  writeStatus(env, f.id, 'done', { epub: 'ب.epub', pages_total: 10, pages_done: 10, pages_unsure: [3, 4, 5] });
  writeEpub(env, f.id, 'ب.epub', Buffer.from('PK old'));
  tick(env);
  assert.strictEqual(jobs(env)[f.id].stage, 'delivered');
  assert.ok(names(env, 'ready').indexOf('ب.epub') >= 0, 'stays in 3- جاهز while pages are unsure');
  for (let day = 0; day < 3; day++) {
    env.clock += 24 * 3600000;
    tick(env);
  }
  assert.strictEqual(env.gh.dispatches.length, 1, 'no improvement round');
  assert.ok(!env.mails.some((m) => /الحكّام|تحسّن/.test(m.subject + m.body)));
});

test('judges of an older version: a run waiting for them delivers the book; a book parked for them is delivered after one more run', () => {
  const env = readyEnv();
  const a = addBook(env, 'أول.pdf');
  tick(env);
  minutes(env, 30);
  writeStatus(env, a.id, 'paused', { waiting_for: 'judges', epub: 'أول.epub', pages_judging: [4, 9], pages_unsure: [4, 9] });
  writeEpub(env, a.id, 'أول.epub');
  tick(env);
  assert.strictEqual(jobs(env)[a.id].stage, 'delivered');
  assert.ok(names(env, 'ready').indexOf('أول.epub') >= 0);
  assert.ok(!env.mails.some((m) => /الحكّام/.test(m.subject + m.body)));

  // Parked for the judges by the older script: it runs once more and is delivered.
  const b = addBook(env, 'ثان.pdf');
  tick(env);
  const all = jobs(env);
  all[b.id].stage = 'judging';
  all[b.id].judgeDay = '2026-10-21';
  env.props.set('JOBS', JSON.stringify(all));
  minutes(env, 10);
  tick(env);
  assert.strictEqual(env.gh.dispatches.length, 3);
  assert.strictEqual(lastDispatch(env).body.inputs.drive_file_id, b.id, 'run again at once');
  minutes(env, 15);
  writeStatus(env, b.id, 'done', { epub: 'ثان.epub', pages_unsure: [9] });
  writeEpub(env, b.id, 'ثان.epub');
  tick(env);
  assert.strictEqual(jobs(env)[b.id].stage, 'delivered');
});

test('reader wait: a book waiting only for one reader is parked, the next starts, both come back the next day oldest first', () => {
  const env = readyEnv();
  const a = addBook(env, 'أول.pdf');
  tick(env);
  minutes(env, 3);
  const b = addBook(env, 'ثان.pdf');
  minutes(env, 30);
  writeStatus(env, a.id, 'paused', { waiting_for: 'reader', readers_left: ['gemini-3.5-flash-lite'], epub: 'أول.epub',
    pages_waiting: [3, 4] });
  tick(env);
  assert.strictEqual(jobs(env)[a.id].stage, 'readerWait');
  assert.ok(names(env, 'working').indexOf('أول.pdf') >= 0, 'parked book stays in the working folder');
  assert.strictEqual(env.gh.dispatches.length, 2, 'the next book started right away');
  assert.strictEqual(lastDispatch(env).body.inputs.drive_file_id, b.id);
  assert.strictEqual(env.mails.filter((m) => /ينتظر القارئ الثاني/.test(m.subject)).length, 1);

  // The next book waits for that reader too.
  minutes(env, 30);
  writeStatus(env, b.id, 'paused', { waiting_for: 'reader', readers_left: ['gemini-3.5-flash-lite'], epub: 'ثان.epub',
    pages_waiting: [7] });
  tick(env);
  assert.strictEqual(jobs(env)[b.id].stage, 'readerWait');
  minutes(env, 10);
  tick(env);
  assert.strictEqual(env.gh.dispatches.length, 2, 'not before the quota renews');
  const s = status(env);
  assert.deepStrictEqual(s.parked.map((p) => [p.title, p.stage]).sort(), [['أول', 'readerWait'], ['ثان', 'readerWait']]);
  assert.ok(!s.recent.some((j) => j.title === 'أول' || j.title === 'ثان'), 'parked books are not listed as ready');

  // A new Pacific day: the older book first, then the other in the check that delivers it.
  env.clock += 24 * 3600000;
  tick(env);
  assert.strictEqual(env.gh.dispatches.length, 3);
  assert.strictEqual(lastDispatch(env).body.inputs.drive_file_id, a.id);
  minutes(env, 15);
  writeStatus(env, a.id, 'done', { epub: 'أول.epub' });
  writeEpub(env, a.id, 'أول.epub');
  tick(env);
  assert.strictEqual(jobs(env)[a.id].stage, 'delivered');
  assert.strictEqual(env.gh.dispatches.length, 4);
  assert.strictEqual(lastDispatch(env).body.inputs.drive_file_id, b.id);
});

test('reader wait: with both readers out the book keeps its turn; waiting for a reader for a month gives up', () => {
  const env = readyEnv();
  const a = addBook(env, 'أ.pdf');
  tick(env);
  addBook(env, 'ب.pdf');
  minutes(env, 30);
  writeStatus(env, a.id, 'paused', { epub: 'أ.epub', pages_waiting: [3] });
  tick(env);
  assert.ok(jobs(env)[a.id].pausedDay, 'paused, holding its turn');
  assert.strictEqual(env.gh.dispatches.length, 1, 'no other book starts');

  const env2 = readyEnv();
  const c = addBook(env2, 'ج.pdf');
  tick(env2);
  for (let day = 0; day <= 30; day++) {
    minutes(env2, 30);
    writeStatus(env2, c.id, 'paused', { waiting_for: 'reader', epub: 'ج.epub', pages_waiting: [3] });
    tick(env2);
    env2.clock += 24 * 3600000;
    tick(env2);
  }
  assert.strictEqual(jobs(env2)[c.id].stage, 'problem');
  assert.ok(env2.mails.some((m) => /مشكلة في تحويل/.test(m.subject) && /القارئ الثاني/.test(m.body)));
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
  assert.strictEqual(s.active.fileId, f.id);   // the page finds the helper's progress by it

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
    setAttribute() {},
    addEventListener() {}
  });
  const find = (n, cls) => n.className.split(' ').indexOf(cls) >= 0 ? n
    : (n.kids || []).reduce((hit, k) => hit || find(k, cls), null);
  const chain = { withSuccessHandler: () => chain, withFailureHandler: () => chain, getStatus() {}, tick() {} };
  const sandbox = {
    document: {
      hidden: false,
      createElement: node,
      createTextNode: (t) => ({ textContent: String(t) }),
      getElementById: (id) => byId[id] || (byId[id] = node('div')),
      documentElement: node('html'),
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
  assert.strictEqual(find(byId.now, 'meter').kids[0].style.width, '25%');

  sandbox.render({ ok: false, error: 'boom' });
  assert.ok(/تعذّر قراءة الحالة: boom/.test(byId.now.textContent));
});

// ---------------------------------------------------------------------------
// Review by hand
// ---------------------------------------------------------------------------

const PLACE_BASE = 'base00000000';

function reviewItems(n, page, seen) {
  page = page || 3;
  const items = [];
  for (let i = 1; i <= n; i++) {
    items.push({ id: 'p' + page + '-' + ('000000000' + i).slice(-10), fp: 'diff|كلمه' + i + '|كلمة', kind: 'diff', n: i,
      before: 'قبل ', core: 'كلمة' + i, after: ' بعد', pos: 10 * i, options: [{ label: 'other', text: 'كلمه' }],
      boxes: [[10, 20, 30, 40]], col: [0, 600], located: true });
  }
  return { v: 1, book: 'b', book_hash: 'abc', answers_seen: seen || '', generated_at: ghTime(Date.now()),
    pages: n ? [{ page, base: PLACE_BASE, img: 'p' + String(page).padStart(4, '0') + '-0123456789.jpg', w: 600, h: 750,
      more: 0, items }] : [] };
}

function putItems(env, fileId, items) {
  env.gh.files['drive-' + fileId + '/review/items.json'] = JSON.stringify(items);
}

function answerFor(item, verdict, text) {
  const a = { page: 3, kind: item.kind, fp: item.fp, before: item.before, core: item.core, after: item.after,
    pos: item.pos, base: PLACE_BASE, place: '', verdict };
  if (verdict === 'fix') a.text = text;
  return a;
}

/** A book delivered with `n` places open on page 3. */
function deliverWithPlaces(env, name, n, extra) {
  const f = addBook(env, name);
  tick(env);
  minutes(env, 30);
  const items = reviewItems(n);
  putItems(env, f.id, items);
  writeStatus(env, f.id, 'done', Object.assign({ epub: 'b.epub', pages_unsure: [3], review_items: n, review_seen: '' }, extra));
  writeEpub(env, f.id, 'b.epub', Buffer.from('PK first'));
  tick(env);
  assert.strictEqual(jobs(env)[f.id].stage, 'delivered');
  return { f, items };
}

function rv(env, fileId) {
  return JSON.parse(env.props.get('RV_' + fileId) || 'null');
}

function answersFile(env, fileId) {
  const root = env.drive.DriveApp.getFolderById(env.props.get('FOLDER_ROOT'));
  const dir = env.drive.children(root.getId()).find((x) => x.name === 'بيانات المراجعة');
  assert.ok(dir, 'answers folder made');
  return env.drive.children(dir.getId()).find((x) => x.name === 'review-' + fileId + '.json');
}

function epubsIn(env, key) {
  return filesIn(env, key).filter((x) => /\.epub$/.test(x.name));
}

test('review: places listed, answers saved in Drive, sent once and applied in a review round', () => {
  const env = readyEnv();
  env.ctx.doGet();
  const { f, items } = deliverWithPlaces(env, 'مراجعة.pdf', 2);
  const link = 'https://script.google.com/macros/s/AKfyTEST/exec?review=' + f.id;
  const mail = env.mails[env.mails.length - 1];
  assert.ok(/جاهز للمراجعة: 2 موضعاً/.test(mail.body) && mail.body.indexOf(link) >= 0, mail.body);
  let s = status(env);
  assert.deepStrictEqual(s.reviews, [{ fileId: f.id, title: 'مراجعة', open: 2, saved: 0, pending: false, note: '' }]);

  let r = JSON.parse(env.ctx.getReview(f.id));
  assert.strictEqual(r.ok, true);
  assert.strictEqual(r.title, 'مراجعة');
  assert.deepStrictEqual(r.pages[0].items.map((x) => x.core), ['كلمة1', 'كلمة2']);
  const one = items.pages[0].items[0];
  const saved = JSON.parse(env.ctx.saveAnswers(f.id, JSON.stringify({ [one.id]: answerFor(one, 'keep') })));
  assert.deepStrictEqual(saved, { ok: true, saved: 1, pending: 1 });
  assert.strictEqual(env.lockHeld, false, 'lock released');
  const file = answersFile(env, f.id);
  const stored = JSON.parse(file.content.toString('utf8'));
  assert.strictEqual(stored.fileId, f.id);
  assert.strictEqual(stored.answers[one.id].verdict, 'keep');
  assert.strictEqual(stored.answers[one.id].at, iso(env.clock));
  r = JSON.parse(env.ctx.getReview(f.id));
  assert.deepStrictEqual(r.pages[0].items.map((x) => x.core), ['كلمة2'], 'an answered place waits for the next run');
  assert.strictEqual(r.saved, 1);
  assert.strictEqual(status(env).reviews[0].saved, 1);

  const sent = JSON.parse(env.ctx.sendReview(f.id));
  assert.strictEqual(sent.started, true);
  const d = lastDispatch(env);
  assert.strictEqual(d.body.inputs.review, file.id + ':only');
  assert.strictEqual(d.body.inputs.result_dir, 'drive-' + f.id);
  assert.strictEqual(jobs(env)[f.id].humanRound, true);
  const n = env.gh.dispatches.length;
  JSON.parse(env.ctx.sendReview(f.id));
  assert.strictEqual(env.gh.dispatches.length, n, 'a second press starts no second round');
  s = status(env);
  assert.strictEqual(s.reviews[0].pending, true);
  assert.strictEqual(s.reviews[0].saved, 0);

  minutes(env, 6);
  const later = reviewItems(2, 3, iso(env.clock - 6 * 60000));
  later.pages[0].items.shift();
  putItems(env, f.id, later);
  writeStatus(env, f.id, 'done', { epub: 'b.epub', pages_unsure: [3], review_items: 1, review_kept: 1, review_applied: 0,
    review_seen: later.answers_seen });
  writeEpub(env, f.id, 'b.epub', Buffer.from('PK reviewed'));
  tick(env);
  const epubs = epubsIn(env, 'ready');
  assert.strictEqual(epubs.length, 1, 'old EPUB replaced in its folder');
  assert.ok(epubs[0].content.equals(Buffer.from('PK reviewed')));
  const j = jobs(env)[f.id];
  assert.strictEqual(j.stage, 'delivered');
  assert.strictEqual(j.humanRound, undefined);
  assert.strictEqual(j.refines, undefined, 'a review round is not an improvement round');
  assert.ok(names(env, 'originals').indexOf('مراجعة.pdf') >= 0);
  const done = env.mails[env.mails.length - 1];
  assert.ok(/طُبّقت مراجعتك/.test(done.subject));
  assert.ok(/0 تصحيحاً و1 تأكيداً/.test(done.body) && /1 ← 1/.test(done.body), done.body);
  assert.ok(/لا تحذف الملف الأصلي/.test(done.body));
  s = status(env);
  assert.deepStrictEqual(s.reviews, [{ fileId: f.id, title: 'مراجعة', open: 1, saved: 0, pending: false, note: '' }]);
  r = JSON.parse(env.ctx.getReview(f.id));
  assert.deepStrictEqual(r.pages[0].items.map((x) => x.core), ['كلمة2']);
});

test('a book reaches 6- مكتمل only when nothing is unsure: it stays in جاهز until the review', () => {
  const env = readyEnv();
  const { f, items } = deliverWithPlaces(env, 'منتهٍ.pdf', 1);
  assert.strictEqual(epubsIn(env, 'final').length, 0, 'still unsure: stays in جاهز');
  assert.strictEqual(epubsIn(env, 'ready').length, 1);
  assert.ok(/لا تحذف الملف الأصلي/.test(env.mails[env.mails.length - 1].body));
  env.clock += 24 * 3600000;
  const rounds = env.gh.dispatches.length;
  tick(env);
  assert.strictEqual(env.gh.dispatches.length, rounds, 'no round by itself');

  env.ctx.saveAnswers(f.id, JSON.stringify({ [items.pages[0].items[0].id]: answerFor(items.pages[0].items[0], 'fix', 'كلمه') }));
  env.ctx.sendReview(f.id);
  minutes(env, 6);
  putItems(env, f.id, reviewItems(0));
  writeStatus(env, f.id, 'done', { epub: 'b.epub', pages_unsure: [], review_items: 0, review_applied: 1 });
  writeEpub(env, f.id, 'b.epub', Buffer.from('PK fixed'));
  tick(env);
  assert.strictEqual(epubsIn(env, 'ready').length, 0);
  const finals = epubsIn(env, 'final');
  assert.strictEqual(finals.length, 1);
  assert.ok(finals[0].content.equals(Buffer.from('PK fixed')));
  assert.ok(/يمكنك الآن حذف ملفه الأصلي/.test(env.mails[env.mails.length - 1].body));
  assert.deepStrictEqual(status(env).reviews, []);

  const g = deliverWithPlaces(env, 'جاهز.pdf', 1);
  env.ctx.saveAnswers(g.f.id, JSON.stringify({ [g.items.pages[0].items[0].id]: answerFor(g.items.pages[0].items[0], 'keep') }));
  env.ctx.sendReview(g.f.id);
  minutes(env, 6);
  writeStatus(env, g.f.id, 'done', { epub: 'b.epub', pages_unsure: [], review_items: 0, review_kept: 1 });
  writeEpub(env, g.f.id, 'b.epub', Buffer.from('PK all checked'));
  tick(env);
  assert.strictEqual(jobs(env)[g.f.id].final, true, 'nothing unsure left: finished');
  assert.ok(epubsIn(env, 'final').some((x) => x.content.equals(Buffer.from('PK all checked'))));
  assert.ok(/نقلت الكتاب إلى «6- مكتمل»/.test(env.mails[env.mails.length - 1].body));
});

test('a review round that fails leaves the book as it was, tries once more, then tells the user once', () => {
  const env = readyEnv();
  const { f, items } = deliverWithPlaces(env, 'فشل.pdf', 1);
  env.ctx.saveAnswers(f.id, JSON.stringify({ [items.pages[0].items[0].id]: answerFor(items.pages[0].items[0], 'keep') }));
  env.ctx.sendReview(f.id);
  assert.strictEqual(env.gh.dispatches.length, 2);
  minutes(env, 6);
  writeStatus(env, f.id, 'failed', { epub: '' });
  const mails = env.mails.length;
  tick(env);
  assert.strictEqual(env.gh.dispatches.length, 3, 'tried again at once');
  assert.strictEqual(rv(env, f.id).tries, 1);
  assert.strictEqual(env.mails.length, mails, 'no message after the first failure');
  // The second try is lost: no run and no status.
  minutes(env, 25);
  tick(env);
  assert.strictEqual(env.gh.dispatches.length, 3, 'no third try');
  assert.strictEqual(rv(env, f.id).x, 'failed');
  assert.strictEqual(env.mails.length, mails + 1);
  assert.ok(/تعذّر تطبيق المراجعة/.test(env.mails[mails].subject));
  const j = jobs(env)[f.id];
  assert.strictEqual(j.stage, 'delivered');
  assert.ok(epubsIn(env, 'ready')[0].content.equals(Buffer.from('PK first')), 'the old book is kept');
  assert.ok(names(env, 'originals').indexOf('فشل.pdf') >= 0);
  assert.deepStrictEqual(names(env, 'problem'), []);
  assert.strictEqual(status(env).reviews[0].note, 'failed');
  // Pressing send again tries again.
  env.clock += 3600000;
  assert.strictEqual(JSON.parse(env.ctx.sendReview(f.id)).started, true);
  assert.strictEqual(env.gh.dispatches.length, 4);
  minutes(env, 6);
  writeStatus(env, f.id, 'paused', { quota_day: '2026-10-19' });
  tick(env);
  // Never parked until tomorrow like a paused book: tried again at once.
  assert.strictEqual(jobs(env)[f.id].stage, 'running');
  assert.strictEqual(jobs(env)[f.id].pausedDay, '');
  assert.strictEqual(env.gh.dispatches.length, 5);
  assert.ok(epubsIn(env, 'ready')[0].content.equals(Buffer.from('PK first')));
});

test('the answers go with every run of the book, and review rounds come first', () => {
  const env = readyEnv();
  const { f, items } = deliverWithPlaces(env, 'أ.pdf', 2, { pages_unsure: [3, 4] });
  const one = items.pages[0].items[0];
  env.ctx.saveAnswers(f.id, JSON.stringify({ [one.id]: answerFor(one, 'keep') }));
  const answersId = rv(env, f.id).a;

  // Another book is converting when the user sends: the review waits, then goes before the inbox.
  const b = addBook(env, 'ب.pdf');
  tick(env);
  assert.strictEqual(lastDispatch(env).body.inputs.drive_file_id, b.id);
  const two = items.pages[0].items[1];
  env.ctx.saveAnswers(f.id, JSON.stringify({ [two.id]: answerFor(two, 'fix', 'كلمة') }));
  assert.strictEqual(JSON.parse(env.ctx.sendReview(f.id)).started, false);
  addBook(env, 'ج.pdf');
  minutes(env, 30);
  writeStatus(env, b.id, 'done', { epub: 'ب.epub' });
  writeEpub(env, b.id, 'ب.epub');
  tick(env);
  const d = lastDispatch(env);
  assert.strictEqual(d.body.inputs.drive_file_id, f.id, 'the review round first');
  assert.strictEqual(d.body.inputs.review, answersId + ':only');
});

test('finished books from before the review get their places once; without saved pages it stays quiet', () => {
  const env = readyEnv();
  const f = addBook(env, 'قديم.pdf');
  tick(env);
  minutes(env, 30);
  // An older workflow: no review numbers.
  writeStatus(env, f.id, 'done', { epub: 'b.epub', pages_unsure: [5], review_items: undefined });
  writeEpub(env, f.id, 'b.epub', Buffer.from('PK old 2'));
  const before = env.gh.dispatches.length;
  tick(env);
  assert.ok(epubsIn(env, 'ready')[0].content.equals(Buffer.from('PK old 2')), 'delivered');
  assert.strictEqual(env.gh.dispatches.length, before + 1, 'idle: its places are prepared');
  assert.strictEqual(lastDispatch(env).body.inputs.review, ':only');
  minutes(env, 6);
  const mails = env.mails.length;
  writeStatus(env, f.id, 'paused', { pages_waiting: [1, 2] });
  tick(env);
  assert.strictEqual(rv(env, f.id).x, 'no-data');
  assert.strictEqual(env.mails.length, mails, 'no message');
  assert.ok(epubsIn(env, 'ready')[0].content.equals(Buffer.from('PK old 2')), 'the book is unchanged');
  env.clock += 24 * 3600000;
  const n = env.gh.dispatches.length;
  tick(env);
  assert.strictEqual(env.gh.dispatches.length, n, 'prepared once only');
  assert.strictEqual(status(env).reviews[0].note, 'no-data');

  // With saved pages: the places are made and the user is told; the book stays as it is.
  const g = addBook(env, 'قديم ثان.pdf');
  tick(env);
  minutes(env, 30);
  writeStatus(env, g.id, 'done', { epub: 'g.epub', pages_unsure: [2], review_items: undefined });
  writeEpub(env, g.id, 'g.epub', Buffer.from('PK g2'));
  tick(env);
  assert.strictEqual(lastDispatch(env).body.inputs.drive_file_id, g.id);
  minutes(env, 6);
  writeStatus(env, g.id, 'done', { epub: 'g.epub', pages_unsure: [2], review_items: 3, review_applied: 0, review_kept: 0 });
  writeEpub(env, g.id, 'g.epub', Buffer.from('PK g3'));
  tick(env);
  assert.ok(epubsIn(env, 'ready').some((x) => x.content.equals(Buffer.from('PK g2'))), 'old EPUB kept');
  assert.ok(!epubsIn(env, 'ready').some((x) => x.content.equals(Buffer.from('PK g3'))));
  assert.ok(/جاهز للمراجعة/.test(env.mails[env.mails.length - 1].subject));
  assert.strictEqual(rv(env, g.id).o, 3);
});

test('review data: ids, image names and answers are checked', () => {
  const env = readyEnv();
  const { f, items } = deliverWithPlaces(env, 'تحقق.pdf', 1);
  assert.strictEqual(JSON.parse(env.ctx.getReview('../x')).ok, false);
  assert.strictEqual(JSON.parse(env.ctx.getReviewImage(f.id, '../status.json')).ok, false);
  assert.strictEqual(JSON.parse(env.ctx.getReviewImage(f.id, 'p0003-0123456789.jpg')).ok, false, '404');
  env.gh.files['drive-' + f.id + '/review/p0003-0123456789.jpg'] = Buffer.from([0xff, 0xd8, 0xff, 0xe0]);
  const img = JSON.parse(env.ctx.getReviewImage(f.id, 'p0003-0123456789.jpg'));
  assert.deepStrictEqual(img, { ok: true, src: 'data:image/jpeg;base64,/9j/4A==' });

  const item = items.pages[0].items[0];
  const bad = [
    { [item.id]: Object.assign(answerFor(item, 'keep'), { verdict: 'maybe' }) },
    { [item.id]: answerFor(item, 'fix', 'ب'.repeat(2001)) },
    { [item.id]: Object.assign(answerFor(item, 'keep'), { page: 4 }) },
    { 'p3-rest-1': { kind: 'rest', page: 3, verdict: 'keep', ids: Array(26).fill(item.id) } },
    { 'p3-x': answerFor(item, 'keep') },
    [answerFor(item, 'keep')]
  ];
  bad.forEach((b) => assert.strictEqual(JSON.parse(env.ctx.saveAnswers(f.id, JSON.stringify(b))).ok, false, JSON.stringify(b).slice(0, 80)));
  assert.strictEqual(rv(env, f.id).a, undefined, 'nothing saved');
  const rest = { kind: 'rest', page: 3, verdict: 'keep', ids: [item.id], fps: [item.fp] };
  assert.strictEqual(JSON.parse(env.ctx.saveAnswers(f.id, JSON.stringify({ 'p3-rest-k1': rest }))).ok, true);
  env.lockBusy = true;
  assert.deepStrictEqual(JSON.parse(env.ctx.saveAnswers(f.id, JSON.stringify({ 'p3-rest-k1': rest }))), { ok: false, busy: true });
  env.lockBusy = false;
  assert.deepStrictEqual(JSON.parse(env.ctx.getReview(f.id)).pages, [], 'the rest of the page is answered');
});

test('the page address is kept from /exec only, and ?review= opens a book', () => {
  const env = readyEnv();
  env.serviceUrl = 'https://script.google.com/macros/s/AKfyTEST/dev';
  env.ctx.doGet();
  assert.strictEqual(env.props.get('PAGE_URL'), undefined);
  env.serviceUrl = null;
  env.ctx.doGet(undefined);
  env.serviceUrl = 'https://script.google.com/macros/s/AKfyTEST/exec';
  const id = '1BxyzABCDEFGHIJKLMNOP';
  const html = env.ctx.doGet({ parameter: { review: id } }).getContent();
  assert.strictEqual(env.props.get('PAGE_URL'), env.serviceUrl);
  const data = JSON.parse(/var INITIAL = (.*);\n/.exec(html)[1]);
  assert.strictEqual(data.openReview, id);
  const odd = JSON.parse(/var INITIAL = (.*);\n/.exec(env.ctx.doGet({ parameter: { review: '<x>' } }).getContent())[1]);
  assert.strictEqual(odd.openReview, '');
});

test('review without the original: one message and no run; a book dropped from JOBS is rebuilt', () => {
  const env = readyEnv();
  const { f, items } = deliverWithPlaces(env, 'محذوف.pdf', 1);
  env.ctx.saveAnswers(f.id, JSON.stringify({ [items.pages[0].items[0].id]: answerFor(items.pages[0].items[0], 'keep') }));
  env.drive.items.get(f.id).trashed = true;
  const n = env.gh.dispatches.length;
  env.ctx.sendReview(f.id);
  assert.strictEqual(env.gh.dispatches.length, n);
  assert.strictEqual(rv(env, f.id).x, 'orig-gone');
  assert.ok(/المراجعة تنتظر الأصل/.test(env.mails[env.mails.length - 1].subject));
  minutes(env, 10);
  tick(env);
  assert.strictEqual(env.gh.dispatches.length, n, 'not again by itself');
  assert.strictEqual(status(env).reviews.length, 0, 'a deleted book is not listed');
  env.drive.items.get(f.id).trashed = false;
  // Long after, the job itself was dropped from JOBS.
  const all = jobs(env);
  delete all[f.id];
  env.props.set('JOBS', JSON.stringify(all));
  assert.strictEqual(JSON.parse(env.ctx.sendReview(f.id)).started, true);
  assert.strictEqual(jobs(env)[f.id].humanRound, true);
  minutes(env, 6);
  writeStatus(env, f.id, 'done', { epub: 'b.epub', pages_unsure: [], review_items: 0, review_kept: 1 });
  writeEpub(env, f.id, 'b.epub', Buffer.from('PK rebuilt'));
  tick(env);
  assert.ok(epubsIn(env, 'ready').concat(epubsIn(env, 'final')).some((x) => x.content.equals(Buffer.from('PK rebuilt'))));
  assert.strictEqual(epubsIn(env, 'ready').concat(epubsIn(env, 'final')).length, 1, 'the old one replaced');
});

/**
 * The page's script in a small DOM: nodes keep their children, click handlers,
 * value/disabled/hidden; google.script.run records each call and answers it when the
 * test says so (calls stay pending until then).
 */
function pageSandbox(env, initial) {
  const byId = {};
  const node = (tag) => {
    const n = {
      tag, kids: [], className: '', style: {}, own: '', disabled: false, hidden: false, value: '', handlers: {},
      set textContent(v) { this.own = String(v); this.kids = []; },
      get textContent() { return [this.own].concat(this.kids.map((k) => k.textContent)).join(' '); },
      appendChild(c) { this.kids.push(c); return c; },
      setAttribute(k, v) { this[k] = String(v); },
      addEventListener(type, fn) { (this.handlers[type] = this.handlers[type] || []).push(fn); }
    };
    return n;
  };
  const calls = [];
  const run = () => {
    const call = { ok: null, fail: null };
    const chain = {
      withSuccessHandler(f) { call.ok = f; return chain; },
      withFailureHandler(f) { call.fail = f; return chain; }
    };
    ['getStatus', 'tick', 'getReview', 'getReviewImage', 'saveAnswers', 'sendReview', 'setHelperBoxes', 'helperRequest'].forEach((name) => {
      chain[name] = (...args) => { call.name = name; call.args = args; calls.push(call); };
    });
    return chain;
  };
  const timers = [];
  const storage = {};
  const sandbox = {
    document: {
      hidden: false,
      createElement: node,
      createTextNode: (t) => ({ textContent: String(t) }),
      getElementById: (id) => byId[id] || (byId[id] = node('div')),
      documentElement: node('html'),
      addEventListener() {}
    },
    google: { script: { get run() { return run(); } } },
    setInterval: () => 0,
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeout: () => {},
    localStorage: { getItem: (k) => (k in storage ? storage[k] : null), setItem: (k, v) => { storage[k] = String(v); } },
    Date, JSON, Math, String, Object, Array
  };
  const html = env.ctx.doGet().getContent().replace(/var INITIAL = .*;\n/, 'var INITIAL = ' + JSON.stringify(initial) + ';\n');
  const script = /<script>([\s\S]*?)<\/script>/.exec(html)[1];
  vm.createContext(sandbox);
  vm.runInContext(script, sandbox);
  const find = (root, pred) => {
    if (pred(root)) return root;
    for (const k of root.kids || []) { const f = find(k, pred); if (f) return f; }
    return null;
  };
  const api = {
    byId, calls, timers, storage, sandbox,
    button(text, root) {
      // The static elements are separate nodes here (no HTML parsing): look in all of them.
      const roots = root ? [root] : Object.values(byId);
      let b = null;
      roots.some((r) => (b = find(r, (x) => x.tag === 'button' && x.textContent.indexOf(text) >= 0)));
      assert.ok(b, 'button ' + text);
      return b;
    },
    click(b) { (b.handlers.click || []).forEach((fn) => fn.call(b)); },
    pending(name) { return calls.filter((c) => c.name === name && !c.done); },
    answer(call, value) { call.done = true; call.ok(typeof value === 'string' ? value : JSON.stringify(value)); },
    fail(call, err) { call.done = true; call.fail(new Error(err)); },
    textarea() { return find(byId['rv-body'], (x) => x.tag === 'textarea'); }
  };
  return api;
}

function reviewForPage() {
  const items = reviewItems(3).pages[0];
  items.items[1] = { id: 'p3-00000000aa', fp: 'missing|سطر', kind: 'missing', n: 2, before: 'آخر السطر', core: '\n\n',
    after: 'أول الفقرة', pos: 50, place: 'after', hint: 'heading', boxes: [[5, 5, 50, 20]], col: [0, 600], located: true,
    options: [{ label: 'other', text: 'السطر الساقط كاملاً' }, { label: 'ocr', text: 'السطر السافط' }] };
  items.items[2].hint = 'likely-ok';
  return { ok: true, fileId: '1Bbook0000000aBcDeF', title: 'كتاب', pages: [items], saved: 0, sent: false, running: false, note: '' };
}

test('the review screen: one place at a time, each button gives the right answer, one save at a time, then send', () => {
  const env = readyEnv();
  const id = '1Bbook0000000aBcDeF';
  const status = JSON.parse(env.ctx.getStatus());
  status.reviews = [{ fileId: id, title: 'كتاب', open: 3, saved: 0, pending: false, note: '' }];
  const P = pageSandbox(env, status);
  assert.strictEqual(P.byId.reviews.hidden, false);
  assert.ok(/مراجعة المشكوك فيه/.test(P.byId.reviews.textContent) && /3 مواضع/.test(P.byId.reviews.textContent));
  P.click(P.button('راجِع', P.byId.reviews));
  assert.strictEqual(P.byId.home.hidden, true);
  assert.strictEqual(P.byId.review.hidden, false);
  const get = P.pending('getReview')[0];
  assert.deepStrictEqual(get.args, [id]);
  P.answer(get, reviewForPage());
  assert.ok(/الموضع 1 من 3 · صفحة 3 من الأصل/.test(P.byId['rv-count'].textContent));
  assert.ok(/كلمة1/.test(P.byId['rv-body'].textContent));
  const img = P.pending('getReviewImage');
  assert.strictEqual(img.length, 1, 'one image for the page, asked once');
  assert.deepStrictEqual(img[0].args, [id, 'p0003-0123456789.jpg']);
  P.answer(img[0], { ok: true, src: 'data:image/jpeg;base64,AAAA' });

  // 1: the reading is right.
  P.click(P.button('القراءة صحيحة'));
  let saves = P.pending('saveAnswers');
  assert.strictEqual(saves.length, 1);
  const first = JSON.parse(saves[0].args[1]);
  assert.deepStrictEqual(first['p3-0000000001'], { page: 3, kind: 'diff', fp: 'diff|كلمه1|كلمة', before: 'قبل ',
    core: 'كلمة1', after: ' بعد', pos: 10, base: PLACE_BASE, place: '', verdict: 'keep' });
  assert.strictEqual(P.byId['rv-send'].disabled, true, 'not while saving');

  // 2: a line seems missing: the editor starts with the other reading's line, as a heading.
  assert.ok(/يبدو أن سطراً سقط هنا/.test(P.byId['rv-body'].textContent));
  P.click(P.button('الصواب هو'));
  assert.strictEqual(P.textarea().value, '## السطر الساقط كاملاً');
  P.click(P.button('عنوان'));
  assert.strictEqual(P.textarea().value, 'السطر الساقط كاملاً');
  P.click(P.button('القراءة الآلية'));
  assert.strictEqual(P.textarea().value, 'السطر السافط');
  P.textarea().value = 'السطر كما في الصورة';
  P.click(P.button('حفظ والتالي'));
  assert.strictEqual(P.pending('saveAnswers').length, 1, 'one save at a time');

  // 3: likely right; "the rest of this page is right".
  assert.ok(/غالباً صحيح/.test(P.byId['rv-body'].textContent));
  P.click(P.button('كل ما بقي في هذه الصفحة صحيح'));
  assert.ok(/انتهت المواضع/.test(P.byId['rv-body'].textContent));

  P.answer(saves[0], { ok: true, saved: 1, pending: 1 });
  saves = P.pending('saveAnswers');
  assert.strictEqual(saves.length, 1, 'the rest goes in one call');
  const second = JSON.parse(saves[0].args[1]);
  assert.deepStrictEqual(second['p3-00000000aa'], { page: 3, kind: 'missing', fp: 'missing|سطر', before: 'آخر السطر',
    core: '\n\n', after: 'أول الفقرة', pos: 50, base: PLACE_BASE, place: 'after', verdict: 'fix', text: 'السطر كما في الصورة' });
  const restId = Object.keys(second).find((k) => /^p3-rest-[0-9a-z]+$/.test(k));
  assert.deepStrictEqual(second[restId], { kind: 'rest', page: 3, verdict: 'keep', ids: ['p3-0000000003'], fps: ['diff|كلمه3|كلمة'] });
  // The server checks them as the page makes them.
  Object.keys(second).forEach((k) => assert.ok(env.ctx.cleanAnswer_(k, second[k]), k));
  assert.ok(env.ctx.cleanAnswer_('p3-0000000001', first['p3-0000000001']));

  // The network drops: tried again after 3 seconds, the copy is kept meanwhile.
  P.fail(saves[0], 'network');
  assert.strictEqual(P.timers[P.timers.length - 1].ms, 3000);
  assert.ok(Object.keys(JSON.parse(P.storage['rv-' + id])).length === 2);
  P.timers.pop().fn();
  saves = P.pending('saveAnswers');
  assert.strictEqual(saves.length, 1);
  P.answer(saves[0], { ok: true, saved: 2, pending: 3 });
  assert.deepStrictEqual(JSON.parse(P.storage['rv-' + id]), {});
  const send = P.byId['rv-send'];
  assert.strictEqual(send.disabled, false);
  assert.ok(/\(3\)/.test(send.own));

  // Send asks once more, then calls sendReview.
  P.click(send);
  assert.strictEqual(P.pending('sendReview').length, 0);
  assert.ok(/اضغط مرة أخرى للتأكيد/.test(P.byId['rv-msg'].textContent));
  P.click(send);
  const sr = P.pending('sendReview');
  assert.deepStrictEqual(sr.map((c) => c.args), [[id]]);
  P.answer(sr[0], { ok: true, started: true });
  assert.strictEqual(P.byId.home.hidden, false);
  assert.ok(/بدأ تحديث الكتاب/.test(P.byId.toasts.textContent));
});

test('a link with ?review= opens the review at once; saves that keep failing ask the user', () => {
  const env = readyEnv();
  const status = JSON.parse(env.ctx.getStatus());
  status.openReview = '1Bbook0000000aBcDeF';
  const P = pageSandbox(env, status);
  assert.strictEqual(P.byId.review.hidden, false);
  P.answer(P.pending('getReview')[0], reviewForPage());
  P.click(P.button('القراءة صحيحة'));
  for (let i = 0; i < 3; i++) {
    P.answer(P.pending('saveAnswers')[0], { ok: false, busy: true });
    P.timers.pop().fn();
  }
  P.answer(P.pending('saveAnswers')[0], { ok: false, busy: true });
  assert.ok(/لم تُحفظ 1 إجابة/.test(P.byId['rv-saving'].textContent), P.byId['rv-saving'].textContent);
  assert.ok(/نافذة خاصة/.test(P.byId['rv-saving'].textContent));
  P.click(P.button('أعد المحاولة', P.byId['rv-saving']));
  assert.strictEqual(P.pending('saveAnswers').length, 1);
  // Going back to the list stops nothing: the answer is still being saved.
  P.click(P.byId['rv-back']);
  assert.strictEqual(P.byId.home.hidden, false);
  P.answer(P.pending('saveAnswers')[0], { ok: true, saved: 1, pending: 1 });
  assert.deepStrictEqual(JSON.parse(P.storage['rv-1Bbook0000000aBcDeF']), {});
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

// ---------------------------------------------------------------------------
// The helper on the user's computer
// ---------------------------------------------------------------------------

function helperEnv() {
  const env = readyEnv();
  env.gh.repoFiles['helper/install.ps1'] = "# install\n$x = 'تحويل الكتب'\n";
  env.gh.repoFiles['helper/launcher.ps1'] = '# launcher\n';
  env.gh.repoFiles['helper/helper.ps1'] = '# helper\n';
  env.gh.files['compare/pages/p011.png'] = Buffer.from([0x89, 0x50, 0x4e, 0x47, 1]);
  env.gh.files['compare/pages/p032.png'] = Buffer.from([0x89, 0x50, 0x4e, 0x47, 2]);
  return env;
}

function helperFolder(env) {
  return env.drive.DriveApp.getFolderById(env.props.get('HELPER_FOLDER'));
}

function liveIn(env, folder) {
  return env.drive.children(folder.getId()).filter((x) => !x.trashed);
}

function sub(env, folder, name) {
  return liveIn(env, folder).find((x) => x instanceof env.drive.MockFolder && x.name === name);
}

function writeHelperStatus(env, extra) {
  const s = Object.assign({ v: 1, at: new Date(env.clock).toISOString(), handled: [], lines: [], actions: [] }, extra || {});
  const old = liveIn(env, helperFolder(env)).find((x) => x.name === 'helper-status.json');
  if (old) old.setContent(JSON.stringify(s));
  else helperFolder(env).createFile('helper-status.json', JSON.stringify(s), 'text/plain');
}

test('helper: the page gives one install line; its file is public, the program and test pages are copied', () => {
  const env = helperEnv();
  const h = status(env).helper;
  assert.strictEqual(h.connected, false);
  assert.strictEqual(h.seen, '');
  const install = env.drive.DriveApp.getFileById(env.props.get('HELPER_INSTALL_ID'));
  assert.strictEqual(install.sharing, 'ANYONE_WITH_LINK/VIEW');
  assert.strictEqual(install.content.toString('utf8'), env.gh.repoFiles['helper/install.ps1']);
  assert.ok(h.line.indexOf("DownloadData('https://drive.usercontent.google.com/download?id=" + install.id +
    "&export=download&confirm=t')") > 0, h.line);
  assert.ok(/UTF8\.GetString/.test(h.line), 'decodes the file as UTF-8');
  const folder = helperFolder(env);
  assert.strictEqual(folder.name, 'بيانات المساعد');
  assert.deepStrictEqual(liveIn(env, sub(env, folder, 'البرنامج')).map((f) => f.name).sort(), ['helper.ps1', 'launcher.ps1']);
  const pages = liveIn(env, sub(env, folder, 'صفحات الاختبار'));
  assert.deepStrictEqual(pages.map((f) => f.name).sort(), ['p011.png', 'p032.png']);
  assert.strictEqual(pages[0].mime, 'image/png');
  assert.ok(pages.find((f) => f.name === 'p032.png').content.equals(env.gh.files['compare/pages/p032.png']));
  const req = JSON.parse(liveIn(env, folder).find((x) => x.name === 'requests.json').content.toString('utf8'));
  assert.deepStrictEqual(req.requests, []);
  // Only the install file is public, and no token is in any Drive file.
  for (const x of env.drive.items.values()) {
    if (x instanceof env.drive.MockFile) {
      if (x.id !== install.id) assert.strictEqual(x.sharing, undefined, x.name + ' is shared');
      const text = x.content.toString('utf8');
      assert.ok(text.indexOf(TOKEN) < 0 && text.indexOf('ya29.') < 0, x.name + ' holds a token');
    }
  }
  assert.deepStrictEqual(env.gh.unknown, []);
});

test('helper: a request from the page reaches requests.json, and leaves once the helper has handled it', () => {
  const env = helperEnv();
  status(env);
  const bad = JSON.parse(env.ctx.helperRequest(JSON.stringify({ action: 'rm -rf', choices: {} })));
  assert.strictEqual(bad.ok, false);
  const r = JSON.parse(env.ctx.helperRequest(JSON.stringify({
    action: 'test', choices: { model: 'grok|grok-4.7', effort: 'low', 'bad key!': 'x' }, label: 'تجربة Grok'
  })));
  assert.strictEqual(r.ok, true);
  assert.strictEqual(env.userLockHeld, false);
  const file = () => JSON.parse(liveIn(env, helperFolder(env)).find((x) => x.name === 'requests.json').content.toString('utf8'));
  assert.deepStrictEqual(file().requests.map((q) => [q.id, q.action, q.choices, q.label]),
    [[r.id, 'test', { model: 'grok|grok-4.7', effort: 'low' }, 'تجربة Grok']]);
  let h = status(env).helper;
  assert.deepStrictEqual(h.pending.map((q) => q.id), [r.id]);

  // The helper is working on it: connected, busy.
  writeHelperStatus(env, { busy: r.id, lines: [{ text: 'يعمل الآن' }] });
  h = status(env).helper;
  assert.strictEqual(h.connected, true);
  assert.strictEqual(h.busy, r.id);
  tick(env);
  assert.strictEqual(file().requests.length, 1);

  // Done: the next check drops it.
  writeHelperStatus(env, { handled: [r.id], lines: [{ text: 'تمت التجربة', path: 'نتائج/تجربة 1', linkText: 'النتائج', kind: 'warn' }] });
  const results = helperFolder(env).createFolder('نتائج').createFolder('تجربة 1');
  h = status(env).helper;
  assert.deepStrictEqual(h.pending, []);
  assert.deepStrictEqual(h.lines, [{ text: 'تمت التجربة', kind: 'warn', link: results.getUrl(), linkText: 'النتائج' }]);
  tick(env);
  assert.deepStrictEqual(file().requests, []);
  assert.deepStrictEqual(JSON.parse(env.props.get('HELPER_REQS')), []);

  // A status from 20 minutes ago: not connected any more.
  minutes(env, 20);
  h = status(env).helper;
  assert.strictEqual(h.connected, false);
  assert.strictEqual(h.minutes, 20);
});

test('helper: files are copied again only when they change on GitHub (each check), the install link stays', () => {
  const env = helperEnv();
  const line = status(env).helper.line;
  const program = sub(env, helperFolder(env), 'البرنامج');
  const before = liveIn(env, program).find((f) => f.name === 'helper.ps1').id;
  env.gh.repoFiles['helper/helper.ps1'] = '# helper v2\n';
  env.gh.repoFiles['helper/install.ps1'] = '# install v2\n';
  tick(env);
  assert.strictEqual(liveIn(env, program).find((f) => f.name === 'helper.ps1').id, before, 'not in the same minute');
  minutes(env, 10);
  tick(env);
  const files = liveIn(env, program);
  assert.strictEqual(files.filter((f) => f.name === 'helper.ps1').length, 1);
  assert.strictEqual(files.find((f) => f.name === 'helper.ps1').content.toString('utf8'), '# helper v2\n');
  assert.strictEqual(files.find((f) => f.name === 'launcher.ps1').created, env.clock - 10 * 60000, 'unchanged file kept');
  assert.strictEqual(env.drive.DriveApp.getFileById(env.props.get('HELPER_INSTALL_ID')).content.toString('utf8'), '# install v2\n');
  assert.strictEqual(status(env).helper.line, line);
});

// ---------------------------------------------------------------------------
// The boxes on the page: the helper's models transcribe and review (docs/helper-boxes-plan.md)
// ---------------------------------------------------------------------------

const GEMINI = 'agy|gemini-3.8-flash';
const GROK = 'grok|grok-4.7';
const MODELS = [{ id: GEMINI, label: 'Gemini 3.8 Flash', tool: 'Antigravity' }, { id: GROK, label: 'Grok 4.7', tool: 'Grok Build' }];

function boxesEnv(boxes) {
  const env = helperEnv();
  status(env);
  const r = JSON.parse(env.ctx.setHelperBoxes(JSON.stringify(boxes)));
  assert.strictEqual(r.ok, true, JSON.stringify(r));
  writeHelperStatus(env, { models: MODELS });
  return env;
}

function requestsFile(env) {
  return JSON.parse(liveIn(env, helperFolder(env)).find((x) => x.name === 'requests.json').content.toString('utf8'));
}

function bookFolderOf(env, fileId) {
  const books = sub(env, helperFolder(env), 'كتب');
  return books ? sub(env, books, fileId) : undefined;
}

test('boxes: the page saves the models of each box; requests.json carries them; unknown ones are refused', () => {
  const env = helperEnv();
  status(env);
  const bad = JSON.parse(env.ctx.setHelperBoxes(JSON.stringify({ transcribe: ['rm|-rf'], review: [] })));
  assert.strictEqual(bad.ok, false);
  const r = JSON.parse(env.ctx.setHelperBoxes(JSON.stringify({ transcribe: [GEMINI, GROK, GEMINI], review: [GROK] })));
  assert.deepStrictEqual(r.boxes, { transcribe: [GEMINI, GROK], review: [GROK], times: {} });
  assert.deepStrictEqual(requestsFile(env).boxes, { transcribe: [GEMINI, GROK], review: [GROK], times: {} });
  assert.strictEqual(env.userLockHeld, false);
  writeHelperStatus(env, { models: MODELS.concat([{ id: 'bad id', label: 'x' }]),
    chips: { [GEMINI]: { state: 'working', text: 'يقرأ ص 3' }, [GROK]: { state: 'odd', text: 'x' } } });
  const h = status(env).helper;
  assert.deepStrictEqual(h.models.map((m) => m.id), [GEMINI, GROK]);
  assert.deepStrictEqual(h.boxes, { transcribe: [GEMINI, GROK], review: [GROK], times: {} });
  assert.deepStrictEqual(h.chips[GEMINI], { state: 'working', text: 'يقرأ ص 3' });
  assert.strictEqual(h.chips[GROK].state, 'idle');
});

test('transcribe: with models in the box the run gets the book folder; the book waits for the helper without holding the slot', () => {
  const env = boxesEnv({ transcribe: [GEMINI], review: [] });
  const a = addBook(env, 'أول.pdf');
  tick(env);
  const folderA = bookFolderOf(env, a.id);
  assert.ok(folderA, 'book folder made under كتب');
  assert.strictEqual(lastDispatch(env).body.inputs.helper, folderA.id + ':transcribe');
  assert.strictEqual(lastDispatch(env).body.inputs.judge, undefined);

  const b = addBook(env, 'ثان.pdf');
  minutes(env, 25);
  writeStatus(env, a.id, 'paused', { waiting_for: 'helper', epub: 'أول.epub', pages_helper: [1, 2, 3], pages_done: 7 });
  writeHelperStatus(env, { models: MODELS });
  tick(env);
  assert.strictEqual(jobs(env)[a.id].stage, 'helper');
  assert.ok(names(env, 'working').indexOf('أول.pdf') >= 0, 'parked book stays in the working folder');
  assert.strictEqual(env.gh.dispatches.length, 2, 'the next book started right away');
  assert.strictEqual(lastDispatch(env).body.inputs.drive_file_id, b.id);
  assert.strictEqual(env.mails.length, 0, 'no mail for waiting on the helper');
  const s = status(env);
  assert.deepStrictEqual(s.working.map((w) => w.title), ['أول'], 'a current book, under «الآن»');
  assert.deepStrictEqual(s.parked, []);
  assert.ok(!s.recent.some((j) => j.title === 'أول'), 'a parked book is not listed as ready');

  // The helper finished the three pages: seen twice, eight minutes apart, then the book runs again.
  minutes(env, 30);
  writeStatus(env, b.id, 'done', { epub: 'ثان.epub' });
  writeEpub(env, b.id, 'ثان.epub');
  const bookAt = new Date(env.clock - 20 * 60000).toISOString();
  writeHelperStatus(env, { models: MODELS, books: { [a.id]: { need: 3, finished: 3, book_at: bookAt } } });
  tick(env);
  assert.strictEqual(jobs(env)[b.id].stage, 'delivered');
  assert.strictEqual(env.gh.dispatches.length, 2, 'not on the first sight');
  minutes(env, 10);
  writeHelperStatus(env, { models: MODELS, books: { [a.id]: { need: 3, finished: 3, book_at: bookAt } } });
  tick(env);
  assert.strictEqual(env.gh.dispatches.length, 3);
  assert.strictEqual(lastDispatch(env).body.inputs.drive_file_id, a.id);
  assert.strictEqual(lastDispatch(env).body.inputs.helper, folderA.id + ':transcribe');

  minutes(env, 10);
  writeStatus(env, a.id, 'done', { epub: 'أول.epub' });
  writeEpub(env, a.id, 'أول.epub');
  tick(env);
  assert.strictEqual(jobs(env)[a.id].stage, 'delivered');
  assert.strictEqual(bookFolderOf(env, a.id), undefined, 'the exchange folder is removed once the book is delivered');
});

test('transcribe: the free models come back to the book while the helper works on it; the page shows it under «الآن» with the whole book', () => {
  const env = boxesEnv({ transcribe: [GEMINI], review: [] });
  const a = addBook(env, 'كبير.pdf');
  tick(env);
  const folderA = bookFolderOf(env, a.id);
  minutes(env, 40);
  const helperPages = Array.from({ length: 869 }, (_, i) => i + 55);
  writeStatus(env, a.id, 'paused', { waiting_for: 'helper', epub: 'كبير.epub', pages_total: 923, pages_done: 54,
    pages_helper: helperPages, free_left: false, free_done: 120 });
  tick(env);
  assert.strictEqual(jobs(env)[a.id].stage, 'helper');
  const bookAt = new Date(env.clock).toISOString();
  writeHelperStatus(env, { models: MODELS, chips: { [GEMINI]: { state: 'working', text: 'يحسم ص 915' } },
    books: { [a.id]: { need: 869, finished: 700, book_at: bookAt } } });

  // The whole book: 54 pages done by the runs and 700 by the helper's models since.
  let s = status(env);
  assert.strictEqual(s.active, null);
  assert.deepStrictEqual(s.parked, []);
  const w = s.working[0];
  assert.deepStrictEqual([w.title, w.total, w.done, w.helperDone], ['كبير', 923, 754, 700]);
  assert.ok(w.freeAt, 'the free models come back once their quota renews');
  const page = pageSandbox(env, s);
  const now = page.byId.now.textContent;
  assert.ok(/«كبير»/.test(now) && /نماذجك تعمل عليه/.test(now) && /754/.test(now) && /من 923 صفحة/.test(now), now);
  assert.ok(!/كبير/.test(page.byId.queue.textContent), 'not in the waiting list');
  assert.ok(/754 من 923/.test(page.byId.figs.textContent), page.byId.figs.textContent);

  // Same day: their quota is used for today, the helper's models go on.
  minutes(env, 10);
  writeHelperStatus(env, { models: MODELS, books: { [a.id]: { need: 869, finished: 700, book_at: bookAt } } });
  tick(env);
  assert.strictEqual(env.gh.dispatches.length, 1);
  // A new Pacific day: the free models take their share again, while the helper's models go on.
  const parkedDay = jobs(env)[a.id].freeDay;
  for (let i = 1; i <= 13 && env.gh.dispatches.length < 2; i++) {
    env.clock += 2 * 3600000;
    writeHelperStatus(env, { models: MODELS, books: { [a.id]: { need: 869, finished: 700 + 10 * i, book_at: bookAt } } });
    tick(env);
  }
  assert.strictEqual(env.gh.dispatches.length, 2);
  assert.ok(formatDate(env.clock, 'America/Los_Angeles', 'yyyy-MM-dd') > parkedDay, 'not before their quota renews');
  assert.strictEqual(lastDispatch(env).body.inputs.helper, folderA.id + ':transcribe');
  // They stopped for time with quota left: the next run starts in the same check.
  minutes(env, 40);
  writeStatus(env, a.id, 'paused', { waiting_for: 'helper', epub: 'كبير.epub', pages_total: 923, pages_done: 400,
    pages_helper: [60, 61], free_left: true });
  tick(env);
  assert.strictEqual(env.gh.dispatches.length, 3);
  assert.strictEqual(lastDispatch(env).body.inputs.helper, folderA.id + ':transcribe');
});

test('transcribe: an empty box, a computer away for an hour, or a helper that stopped moving hands the book to the free model', () => {
  const env = boxesEnv({ transcribe: [GEMINI], review: [] });
  const a = addBook(env, 'أ.pdf');
  tick(env);
  minutes(env, 25);
  writeStatus(env, a.id, 'paused', { waiting_for: 'helper', epub: 'أ.epub', pages_helper: [1] });
  writeHelperStatus(env, { models: MODELS });
  tick(env);
  assert.strictEqual(jobs(env)[a.id].stage, 'helper');
  const folderA = bookFolderOf(env, a.id);

  // The user empties the box: the book goes on with the free model, the folder's work used.
  env.ctx.setHelperBoxes(JSON.stringify({ transcribe: [], review: [] }));
  minutes(env, 10);
  tick(env);
  assert.strictEqual(lastDispatch(env).body.inputs.drive_file_id, a.id);
  assert.strictEqual(lastDispatch(env).body.inputs.helper, folderA.id + ':import');

  // Away for over an hour.
  const env2 = boxesEnv({ transcribe: [GEMINI], review: [] });
  const c = addBook(env2, 'ج.pdf');
  tick(env2);
  minutes(env2, 25);
  writeStatus(env2, c.id, 'paused', { waiting_for: 'helper', epub: 'ج.epub', pages_helper: [1] });
  writeHelperStatus(env2, { models: MODELS });
  tick(env2);
  minutes(env2, 50);
  tick(env2);
  assert.strictEqual(env2.gh.dispatches.length, 1, 'still within the hour');
  minutes(env2, 15);
  tick(env2);
  assert.strictEqual(env2.gh.dispatches.length, 2);
  assert.ok(/:import$/.test(lastDispatch(env2).body.inputs.helper));

  // Connected but not moving for six hours.
  const env3 = boxesEnv({ transcribe: [GEMINI], review: [] });
  const d = addBook(env3, 'د.pdf');
  tick(env3);
  minutes(env3, 25);
  writeStatus(env3, d.id, 'paused', { waiting_for: 'helper', epub: 'د.epub', pages_helper: [1, 2] });
  const parkedAt = env3.clock;
  while (env3.gh.dispatches.length < 2 && env3.clock - parkedAt < 8 * 3600000) {
    writeHelperStatus(env3, { models: MODELS, books: { [d.id]: { need: 2, finished: 1, book_at: new Date(env3.clock).toISOString() } } });
    tick(env3);
    minutes(env3, 10);
  }
  assert.strictEqual(env3.gh.dispatches.length, 2, 'stalled');
  assert.ok(env3.clock - parkedAt > 6 * 3600000, 'not before six hours without progress');
  assert.ok(/:import$/.test(lastDispatch(env3).body.inputs.helper));
});

test('transcribe: no helper, no box or not a PDF: runs as before; a book paused for the quota resumes once the helper read the rest', () => {
  const plain = readyEnv();
  addBook(plain, 'عادي.pdf');
  tick(plain);
  assert.strictEqual(lastDispatch(plain).body.inputs.helper, undefined);

  const env = boxesEnv({ transcribe: [], review: [] });
  addBook(env, 'كتاب.pdf');
  tick(env);
  assert.strictEqual(lastDispatch(env).body.inputs.helper, undefined, 'empty box, no folder');

  const env2 = boxesEnv({ transcribe: [GEMINI], review: [] });
  addBook(env2, 'نص.docx');
  tick(env2);
  assert.strictEqual(lastDispatch(env2).body.inputs.helper, undefined, 'only scanned books');

  const env3 = boxesEnv({ transcribe: [GEMINI, GROK], review: [] });
  const f = addBook(env3, 'كبير.pdf');
  tick(env3);
  minutes(env3, 30);
  writeStatus(env3, f.id, 'paused', { epub: 'كبير.epub', pages_waiting: [5, 6], pages_helper: [3] });
  tick(env3);
  assert.ok(jobs(env3)[f.id].pausedDay, 'paused for the quota');
  minutes(env3, 60);
  const bookAt = new Date(env3.clock - 30 * 60000).toISOString();
  writeHelperStatus(env3, { models: MODELS, books: { [f.id]: { need: 3, finished: 3, book_at: bookAt } } });
  tick(env3);
  minutes(env3, 10);
  writeHelperStatus(env3, { models: MODELS, books: { [f.id]: { need: 3, finished: 3, book_at: bookAt } } });
  tick(env3);
  assert.strictEqual(env3.gh.dispatches.length, 2, 'resumed the same day');
  assert.ok(/:transcribe$/.test(lastDispatch(env3).body.inputs.helper));
});

test('review: with models in the review box the places are copied for the helper, and its finished verdicts are applied in a round', () => {
  const env = boxesEnv({ transcribe: [], review: [GEMINI, GROK] });
  const { f, items: placed } = deliverWithPlaces(env, 'مراجع.pdf', 3);
  env.gh.files['drive-' + f.id + '/review/p0003-0123456789.jpg'] = Buffer.from([0xff, 0xd8, 1]);
  env.gh.files['drive-' + f.id + '/review/answers.json'] = '{}';
  minutes(env, 10);
  tick(env);
  const dir = sub(env, sub(env, helperFolder(env), 'مراجعة'), f.id);
  assert.ok(dir, 'review folder for the book');
  assert.deepStrictEqual(liveIn(env, dir).map((x) => x.name).sort(), ['items.json', 'p0003-0123456789.jpg']);
  const copied = liveIn(env, dir).find((x) => x.name === 'items.json').id;
  minutes(env, 10);
  tick(env);
  assert.strictEqual(liveIn(env, dir).find((x) => x.name === 'items.json').id, copied, 'not copied again unchanged');

  // The helper is still judging: nothing happens.
  const verdicts = helperFolder(env).createFolder('أحكام');
  const items = placed.pages[0].items;
  const answers = {};
  answers[items[0].id] = Object.assign(answerFor(items[0], 'keep'), { by: [GEMINI] });
  const file = verdicts.createFile(f.id + '.json', JSON.stringify({ v: 1, fileId: f.id, answers, done: false }), 'application/json');
  const before = env.gh.dispatches.length;
  minutes(env, 10);
  tick(env);
  assert.strictEqual(env.gh.dispatches.length, before);

  // Done: one review round with the verdicts, then nothing more until there are new ones.
  file.setContent(JSON.stringify({ v: 1, fileId: f.id, answers, done: true }));
  minutes(env, 10);
  tick(env);
  assert.strictEqual(env.gh.dispatches.length, before + 1);
  const d = lastDispatch(env).body.inputs;
  assert.strictEqual(d.judge, file.id);
  assert.ok(/:only$/.test(d.review), d.review);
  minutes(env, 5);
  writeStatus(env, f.id, 'done', { epub: 'مراجع.epub', pages_unsure: [3], review_items: 2, review_kept: 1 });
  writeEpub(env, f.id, 'مراجع.epub');
  tick(env);
  assert.ok(env.mails.some((m) => /طُبّقت أحكام النماذج/.test(m.subject)), env.mails.map((m) => m.subject).join(' | '));
  minutes(env, 10);
  tick(env);
  assert.strictEqual(env.gh.dispatches.length, before + 1, 'the same verdicts are not applied twice');

  // Nothing open any more: the copy for the helper goes.
  writeStatus(env, f.id, 'done', { epub: 'مراجع.epub', pages_unsure: [], review_items: 0 });
  const rvNow = JSON.parse(env.props.get('RV_' + f.id));
  rvNow.o = 0;
  env.props.set('RV_' + f.id, JSON.stringify(rvNow));
  minutes(env, 10);
  tick(env);
  assert.strictEqual(sub(env, sub(env, helperFolder(env), 'مراجعة'), f.id), undefined);
});

test('a book the user deleted: off the page, not reviewed by the models, not improved', () => {
  const env = boxesEnv({ transcribe: [], review: [GEMINI] });
  const { f } = deliverWithPlaces(env, 'محذوف.pdf', 2);
  env.gh.files['drive-' + f.id + '/review/p0003-0123456789.jpg'] = Buffer.from([0xff, 0xd8, 1]);
  minutes(env, 10);
  tick(env);
  assert.ok(sub(env, sub(env, helperFolder(env), 'مراجعة'), f.id), 'copied for the models');
  assert.deepStrictEqual(requestsFile(env).reviews, [f.id], 'listed for the helper');
  assert.ok(status(env).recent.some((j) => j.fileId === f.id));

  env.drive.DriveApp.getFileById(f.id).setTrashed(true);
  minutes(env, 10);
  tick(env);
  assert.strictEqual(sub(env, sub(env, helperFolder(env), 'مراجعة'), f.id), undefined, 'the models stop');
  // The helper stops on the list, even before Drive for desktop removes the folder from the computer.
  assert.deepStrictEqual(requestsFile(env).reviews, [], 'no longer listed');
  const s = status(env);
  assert.ok(!s.recent.some((j) => j.fileId === f.id), 'not in the last books');
  assert.strictEqual(s.reviews.length, 0);
  minutes(env, 24 * 60);
  const before = env.gh.dispatches.length;
  tick(env);
  assert.strictEqual(env.gh.dispatches.length, before, 'no round the next day');
});

test('a deleted book: its review folder goes even while the review box is empty, and before a model is put in it', () => {
  const env = boxesEnv({ transcribe: [], review: [GEMINI] });
  const { f } = deliverWithPlaces(env, 'قديم.pdf', 2);
  env.gh.files['drive-' + f.id + '/review/p0003-0123456789.jpg'] = Buffer.from([0xff, 0xd8, 1]);
  minutes(env, 10);
  tick(env);
  const reviews = () => sub(env, helperFolder(env), 'مراجعة');
  assert.ok(sub(env, reviews(), f.id), 'copied for the models');

  // The box is emptied, then the book is deleted: the next check removes its folder anyway.
  env.ctx.setHelperBoxes(JSON.stringify({ transcribe: [], review: [] }));
  env.drive.DriveApp.getFileById(f.id).setTrashed(true);
  minutes(env, 10);
  tick(env);
  assert.strictEqual(sub(env, reviews(), f.id), undefined, 'gone with the box empty');

  // Deleted between two checks: putting a model in the box removes it at once.
  const { f: g } = deliverWithPlaces(env, 'ثان.pdf', 2);
  env.gh.files['drive-' + g.id + '/review/p0003-0123456789.jpg'] = Buffer.from([0xff, 0xd8, 1]);
  env.ctx.setHelperBoxes(JSON.stringify({ transcribe: [], review: [GEMINI] }));
  minutes(env, 10);
  tick(env);
  assert.ok(sub(env, reviews(), g.id));
  env.ctx.setHelperBoxes(JSON.stringify({ transcribe: [], review: [] }));
  env.drive.DriveApp.getFileById(g.id).setTrashed(true);
  env.ctx.setHelperBoxes(JSON.stringify({ transcribe: [], review: [GEMINI] }));
  assert.strictEqual(sub(env, reviews(), g.id), undefined, 'gone before the model starts');
});

test('a book deleted while it waits for the helper: dropped after two checks, its folder goes, and the helper is told', () => {
  const env = boxesEnv({ transcribe: [GEMINI], review: [] });
  const f = addBook(env, 'محذوف قبل التسليم.pdf');
  tick(env);
  assert.ok(bookFolderOf(env, f.id), 'folder for the helper');
  env.drive.DriveApp.getFileById(f.id).setTrashed(true);
  minutes(env, 10);
  tick(env);
  assert.ok(jobs(env)[f.id], 'one check is not enough');
  minutes(env, 10);
  tick(env);
  assert.strictEqual(jobs(env)[f.id], undefined, 'dropped');
  assert.ok(!bookFolderOf(env, f.id), 'its folder for the helper goes');
  assert.deepStrictEqual(requestsFile(env).gone, [f.id]);
  assert.strictEqual(status(env).active, null);
});

test('the models\' finished verdicts are applied even after the user empties the review box', () => {
  const env = boxesEnv({ transcribe: [], review: [GEMINI] });
  const { f, items: placed } = deliverWithPlaces(env, 'أحكام.pdf', 2);
  const items = placed.pages[0].items;
  const answers = { [items[0].id]: Object.assign(answerFor(items[0], 'keep'), { by: [GEMINI] }) };
  const file = helperFolder(env).createFolder('أحكام').createFile(f.id + '.json',
    JSON.stringify({ v: 1, fileId: f.id, answers, done: true }), 'application/json');
  env.ctx.setHelperBoxes(JSON.stringify({ transcribe: [], review: [] }));
  const before = env.gh.dispatches.length;
  minutes(env, 10);
  tick(env);
  assert.strictEqual(env.gh.dispatches.length, before + 1, 'a round with the verdicts');
  assert.strictEqual(lastDispatch(env).body.inputs.judge, file.id);
});

test('the page: models move between the boxes with a tap (or a drag), are saved, and come back if saving fails', () => {
  const env = boxesEnv({ transcribe: [GEMINI], review: [] });
  writeHelperStatus(env, { models: MODELS.concat([{ id: 'agy|gemini-4-argon', label: 'Gemini 4 Argon', tool: 'Antigravity',
    warn: 'لا للتفريغ: يستنفد الحدود' }]), chips: { [GEMINI]: { state: 'working', text: 'يقرأ ص 12 من «أول»' } } });
  const page = pageSandbox(env, status(env));
  const boxes = page.byId.boxes;
  assert.strictEqual(boxes.hidden, false);
  const sections = boxes.kids.filter((k) => k.className === 'bx');
  assert.strictEqual(sections.length, 2);
  const pair = (sec) => sec.kids.find((k) => k.className === 'pair');
  const chipIn = (box, text) => {
    for (const k of box.kids || []) {
      if (k.tag === 'button' && k.textContent.indexOf(text) >= 0) return k;
      const inner = chipIn(k, text);
      if (inner) return inner;
    }
    return null;
  };
  const [offT, onT] = pair(sections[0]).kids;
  assert.ok(/Gemini 3\.8 Flash/.test(onT.textContent) && /يقرأ ص 12/.test(onT.textContent), onT.textContent);
  assert.ok(/Grok 4\.7/.test(offT.textContent) && /يستنفد الحدود/.test(offT.textContent));
  assert.deepStrictEqual(offT.kids.filter((k) => k.className === 'grp').map((g) => g.kids[0].textContent),
    ['Grok Build', 'Antigravity'], 'in rows under the name of their program');
  assert.ok(/فارغ: المواضع تبقى لك/.test(pair(sections[1]).kids[1].textContent));

  // A tap on Grok in the right box puts it to work.
  page.click(chipIn(offT, 'Grok 4.7'));
  const call = page.pending('setHelperBoxes')[0];
  assert.deepStrictEqual(JSON.parse(call.args[0]), { transcribe: [GEMINI, GROK], review: [], times: {} });
  let now = pair(boxes.kids.filter((k) => k.className === 'bx')[0]).kids[1];
  assert.ok(/Grok 4\.7/.test(now.textContent), 'shown in the left box at once');
  page.answer(call, env.ctx.setHelperBoxes(call.args[0]));
  assert.deepStrictEqual(JSON.parse(env.props.get('HELPER_BOXES')).transcribe, [GEMINI, GROK]);

  // A start time: Grok begins tomorrow at 14:30 (local time). The times open under its card.
  const find = (node, pred) => { if (pred(node)) return node; for (const k of node.kids || []) { const f = find(k, pred); if (f) return f; } return null; };
  const cardOf = (box, text) => box.kids.find((k) => /\bmcard\b/.test(k.className) && k.textContent.indexOf(text) >= 0);
  const buttonIn = (root, text) => find(root, (k) => k.tag === 'button' && k.textContent.trim() === text);
  let onBox = pair(boxes.kids.filter((k) => k.className === 'bx')[0]).kids[1];
  assert.ok(/يبدأ: الآن · يتوقف: لا يتوقف/.test(cardOf(onBox, 'Grok 4.7').textContent));
  page.click(buttonIn(cardOf(onBox, 'Grok 4.7'), 'المواعيد'));
  onBox = pair(boxes.kids.filter((k) => k.className === 'bx')[0]).kids[1];
  const startRow = find(cardOf(onBox, 'Grok 4.7'), (k) => k.className === 'times').kids[0];
  const daySel = find(startRow, (k) => k.tag === 'select');
  const clock = find(startRow, (k) => k.tag === 'input');
  daySel.value = '0';
  clock.value = '00:00';
  page.click(buttonIn(startRow, 'حدّد'));
  assert.strictEqual(page.pending('setHelperBoxes').length, 0, 'a time already past is not saved');
  assert.ok(/مضى/.test(page.byId.toasts.textContent));
  daySel.value = '1';
  clock.value = '14:30';
  page.click(buttonIn(startRow, 'حدّد'));
  const timed = JSON.parse(page.pending('setHelperBoxes')[0].args[0]);
  const expect = new Date();
  expect.setDate(expect.getDate() + 1);
  expect.setHours(14, 30, 0, 0);
  assert.strictEqual(Date.parse(timed.times[GROK].start), expect.getTime(), 'tomorrow at 14:30');
  page.answer(page.pending('setHelperBoxes')[0], env.ctx.setHelperBoxes(JSON.stringify(timed)));
  assert.strictEqual(JSON.parse(env.props.get('HELPER_BOXES')).times[GROK].start, timed.times[GROK].start);

  now = pair(boxes.kids.filter((k) => k.className === 'bx')[0]).kids[1];
  const grok = cardOf(now, 'Grok 4.7').textContent;
  assert.ok(/مجدول/.test(grok) && /يبدأ: [^·]*:30/.test(grok) && !/يبدأ خلال دقيقة/.test(grok), grok);

  // Its stop button sends Gemini back; saving fails: it comes back.
  page.click(buttonIn(cardOf(now, 'Gemini 3.8 Flash'), 'أوقف'));
  assert.deepStrictEqual(JSON.parse(page.pending('setHelperBoxes')[0].args[0]).transcribe, [GROK]);
  page.fail(page.pending('setHelperBoxes')[0], 'offline');
  now = pair(boxes.kids.filter((k) => k.className === 'bx')[0]).kids[1];
  assert.ok(/Gemini 3\.8 Flash/.test(now.textContent), now.textContent);
  assert.ok(/تعذّر الحفظ/.test(page.byId.toasts.textContent));

  // A drop on the left box of the review section.
  const reviewOn = pair(boxes.kids.filter((k) => k.className === 'bx')[1]).kids[1];
  reviewOn.handlers.drop[0]({ preventDefault() {}, dataTransfer: { getData: () => JSON.stringify({ id: GEMINI }) } });
  assert.deepStrictEqual(JSON.parse(page.pending('setHelperBoxes')[0].args[0]).review, [GEMINI]);
});

test('the page: the waiting books by name in their order, and how each finished book ended', () => {
  const env = readyEnv();
  const first = addBook(env, 'أول.pdf', { ageMin: 30 });
  addBook(env, 'ثان.pdf', { ageMin: 20 });
  addBook(env, 'ثالث.docx', { ageMin: 10 });
  tick(env);
  let s = status(env);
  assert.deepStrictEqual(s.queueNames, ['ثان', 'ثالث'], 'without their extension, oldest first');
  const page = pageSandbox(env, s);
  assert.ok(/2\. ?|ثان/.test(page.byId.queue.textContent) && /تبدأ بهذا الترتيب/.test(page.byId.queue.textContent));
  assert.ok(/يُحوَّل الآن/.test(page.byId.now.textContent) && /«أول»/.test(page.byId.now.textContent), page.byId.now.textContent);
  assert.ok(/الكتاب الحالي/.test(page.byId.figs.textContent) && /يُحوَّل الآن/.test(page.byId.figs.textContent));
  minutes(env, 30);
  writeStatus(env, first.id, 'done', { epub: 'أول.epub', pages_unsure: [4, 9] });
  writeEpub(env, first.id, 'أول.epub');
  tick(env);
  s = status(env);
  const done = s.recent.find((j) => j.title === 'أول');
  assert.deepStrictEqual([done.fileId, done.unsure, done.final], [first.id, 2, false]);
  const page2 = pageSandbox(env, s);
  assert.ok(/صفحتان مشكوك فيهما/.test(page2.byId.recent.textContent), page2.byId.recent.textContent);
});

test('review: a book the user deleted leaves the review list', () => {
  const env = readyEnv();
  const { f } = deliverWithPlaces(env, 'محذوف.pdf', 2);
  assert.strictEqual(status(env).reviews.length, 1);
  env.drive.DriveApp.getFileById(f.id).setTrashed(true);
  assert.strictEqual(status(env).reviews.length, 0, 'original in the trash');
  const { f: g } = deliverWithPlaces(env, 'آخر.pdf', 2);
  env.drive.DriveApp.getFileById(rv(env, g.id).e).setTrashed(true);
  assert.strictEqual(status(env).reviews.length, 0, 'EPUB in the trash');
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
