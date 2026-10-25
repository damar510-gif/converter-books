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
    jobs: {},              // run id -> its jobs (with their steps)
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
    const runJobs = new RegExp('^' + base + '/actions/runs/(\\d+)/jobs$').exec(p);
    if (req.method === 'GET' && runJobs) {
      const list = gh.jobs[runJobs[1]] || [];
      return json(200, { total_count: list.length, jobs: list });
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

/**
 * One check; also asserts the global rules: at most one dispatch, one active job (a book
 * paused for its quota, with no run going, may wait beside one review round), lock released.
 */
function tick(env) {
  const before = env.gh.dispatches.length;
  const posts = env.requests.filter((r) => r.method === 'POST').length;
  const result = env.ctx.tick();
  assert.ok(env.gh.dispatches.length - before <= 1, 'more than one dispatch in one check');
  assert.ok(env.requests.filter((r) => r.method === 'POST').length - posts <= 1, 'more than one dispatch request in one check');
  // A book carried by another book's run (runLead) is part of that run, not a second one.
  const active = Object.values(jobs(env)).filter((j) => (j.stage === 'queued' || j.stage === 'running') && !j.runLead);
  const going = active.filter((j) => !(j.pausedDay && !j.humanRound));
  assert.ok(going.length <= 1 && active.length <= 2, 'two active jobs');
  assert.strictEqual(env.lockHeld, false, 'lock released');
  return result;
}

function lastDispatch(env) {
  return env.gh.dispatches[env.gh.dispatches.length - 1];
}

/** The live file of a dispatch (its "live" input is "<live file>:<requests file>"). */
function liveId(d) {
  return String(d.body.inputs.live || '').split(':')[0];
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
      title: 'تاريخ الأندلس',
      // The live view's file (live.py), which says which run is coming until the run writes,
      // and the file of the page's requests to the run («أعد المحاولة الآن»).
      live: env.props.get('LIVE_FILE_ID') + ':' + env.props.get('LIVE_CONTROL_ID'),
      // The free models' boxes as the page shows them (the converter's own until the user
      // changes them): each Lite reader gives the readings the other cannot.
      models: 'read1=gemini-3.5-flash-lite,gemini-3.1-flash-lite;read2=gemini-3.1-flash-lite,gemini-3.5-flash-lite;' +
        'settle=gemini-3.8-flash,gemini-3.7-flash,gemini-3.6-flash,gemini-3-flash,gemini-3.5-flash-lite,gemini-3.1-flash-lite;review='
    }
  });
  const live = JSON.parse(env.drive.DriveApp.getFileById(liveId(d)).getBlob().getDataAsString());
  assert.deepStrictEqual(live, { v: 1, seq: 0, phase: 'queued', file: f.id, book: 'تاريخ الأندلس', dispatched: iso(env.clock), round: '' });
  assert.strictEqual(env.drive.DriveApp.getFileById(liveId(d)).getName(), 'live.json');
  assert.strictEqual(env.drive.DriveApp.getFileById(env.props.get('LIVE_CONTROL_ID')).getName(), 'control.json');
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

test('reader wait: the next book runs at once with the waiting one; the queue waits for the reader, the oldest book holding the turn', () => {
  const env = readyEnv();
  const a = addBook(env, 'أول.pdf');
  tick(env);
  minutes(env, 3);
  const b = addBook(env, 'ثان.pdf');
  minutes(env, 30);
  writeStatus(env, a.id, 'paused', { waiting_for: 'reader', readers_left: ['gemini-3.5-flash-lite'], epub: 'أول.epub',
    pages_waiting: [3, 4], stages: { done: 8, read1: 2 } });
  tick(env);
  // The next book starts at once, and the waiting one goes with it: the oldest is the run's own.
  assert.strictEqual(env.gh.dispatches.length, 2, 'the next book started right away');
  let d = lastDispatch(env).body.inputs;
  assert.strictEqual(d.drive_file_id, a.id);
  let q = JSON.parse(d.queue);
  assert.deepStrictEqual(q.books.map((x) => [x.id, x.dir, x.title]), [[b.id, 'drive-' + b.id, 'ثان']]);
  assert.ok(q.order && q.books[0].order > q.order, 'the books in the order they were put in Drive');
  assert.strictEqual(jobs(env)[b.id].runLead, a.id);
  assert.ok(names(env, 'working').indexOf('ثان.pdf') >= 0);
  assert.ok(/ومعه «ثان»/.test(journal(env).pop().x));

  // Both wait for that reader: the oldest book holds the turn until the quota renews.
  minutes(env, 30);
  writeStatus(env, a.id, 'paused', { waiting_for: 'reader', pages_waiting: [3, 4], stages: { done: 8, read1: 2 },
    requests: { 'gemini-3.5-flash-lite': 40 }, models_out: ['gemini-3.1-flash-lite'] });
  writeStatus(env, b.id, 'paused', { waiting_for: 'reader', pages_waiting: [7], stages: { done: 9, read1: 1 } });
  tick(env);
  assert.strictEqual(jobs(env)[a.id].stage, 'running');
  assert.ok(jobs(env)[a.id].pausedDay, 'the queue waits for the new day: ' + JSON.stringify(jobs(env)[a.id]) + JSON.stringify(jobs(env)[b.id]));
  assert.strictEqual(jobs(env)[b.id].stage, 'waiting');
  assert.strictEqual(jobs(env)[b.id].runLead, undefined);
  minutes(env, 10);
  tick(env);
  assert.strictEqual(env.gh.dispatches.length, 2, 'not before the quota renews');
  const s = status(env);
  assert.deepStrictEqual(s.parked.map((p) => [p.title, p.stage]), [['ثان', 'waiting']]);
  assert.ok(!s.recent.some((j) => j.title === 'أول' || j.title === 'ثان'), 'waiting books are not listed as ready');

  // A new Pacific day: both again in one run; the first is done, the other goes on at once.
  env.clock += 24 * 3600000;
  tick(env);
  assert.strictEqual(env.gh.dispatches.length, 3);
  d = lastDispatch(env).body.inputs;
  assert.deepStrictEqual([d.drive_file_id, JSON.parse(d.queue).books.map((x) => x.id)], [a.id, [b.id]]);
  minutes(env, 15);
  writeStatus(env, a.id, 'done', { epub: 'أول.epub' });
  writeEpub(env, a.id, 'أول.epub');
  writeStatus(env, b.id, 'paused', { waiting_for: 'time', stages: { done: 9, read2: 1 } });
  tick(env);
  assert.strictEqual(jobs(env)[a.id].stage, 'delivered');
  assert.strictEqual(env.gh.dispatches.length, 4);
  d = lastDispatch(env).body.inputs;
  assert.strictEqual(d.drive_file_id, b.id);
  assert.strictEqual(d.queue, undefined, 'a run of one book');
});

test('a run of several books: each is delivered once done, a failed one does not stop the others, one whose EPUB is missing is converted again with the queue', () => {
  const env = readyEnv();
  const a = addBook(env, 'أ.pdf');
  tick(env);
  const b = addBook(env, 'ب.pdf');
  const c = addBook(env, 'ج.pdf');
  minutes(env, 30);
  writeStatus(env, a.id, 'paused', { waiting_for: 'time', stages: { done: 4, unread: 300 } });
  tick(env);
  // The books in «1- جديد» join while the run has room (each box takes a's pages first), even
  // with many pages of a left to read first: one back from «5- مشكلة» may have pages to settle.
  let d = lastDispatch(env).body.inputs;
  assert.deepStrictEqual(JSON.parse(d.queue).books.map((x) => x.id), [b.id, c.id]);
  minutes(env, 30);
  writeStatus(env, a.id, 'done', { epub: 'أ.epub' });   // its EPUB is not in the results: converted again
  writeStatus(env, b.id, 'failed', { reason: 'not a PDF' });
  writeStatus(env, c.id, 'paused', { waiting_for: 'time', stages: { done: 2, unread: 300 } });
  tick(env);
  assert.strictEqual(jobs(env)[a.id].failStreak, 1);
  assert.strictEqual(jobs(env)[b.id].failStreak, 1);
  assert.strictEqual(env.gh.dispatches.length, 3, 'one run for the queue, not one per book');
  d = lastDispatch(env).body.inputs;
  assert.deepStrictEqual([d.drive_file_id, JSON.parse(d.queue).books.map((x) => x.id)], [a.id, [b.id, c.id]]);
  minutes(env, 30);
  writeStatus(env, a.id, 'done', { epub: 'أ.epub' });
  writeEpub(env, a.id, 'أ.epub');
  writeStatus(env, b.id, 'paused', { waiting_for: 'time', stages: { done: 2, read2: 8 } });
  writeStatus(env, c.id, 'paused', { waiting_for: 'time', stages: { done: 2, read2: 8 } });
  tick(env);
  assert.strictEqual(jobs(env)[a.id].stage, 'delivered');
  assert.strictEqual(jobs(env)[b.id].failStreak, 0, 'a failure counts only in a row');
  d = lastDispatch(env).body.inputs;
  assert.deepStrictEqual([d.drive_file_id, JSON.parse(d.queue).books.map((x) => x.id)], [b.id, [c.id]]);
  minutes(env, 30);
  writeStatus(env, b.id, 'failed', { reason: 'not a PDF' });
  writeStatus(env, c.id, 'paused', { waiting_for: 'time', stages: { done: 2, read2: 8 } });
  tick(env);
  minutes(env, 30);
  writeStatus(env, b.id, 'failed', { reason: 'not a PDF' });
  writeStatus(env, c.id, 'done', { epub: 'ج.epub' });
  writeEpub(env, c.id, 'ج.epub');
  tick(env);
  assert.strictEqual(jobs(env)[b.id].stage, 'problem', 'given up after its second failure');
  assert.strictEqual(jobs(env)[c.id].stage, 'delivered');
  assert.deepStrictEqual(names(env, 'problem').filter((n) => /ب/.test(n)).length > 0, true);
});

test('reader wait: with both readers out the book keeps its turn; waiting for a reader for a month gives up', () => {
  const env = readyEnv();
  const a = addBook(env, 'أ.pdf');
  tick(env);
  addBook(env, 'ب.pdf');
  minutes(env, 30);
  writeStatus(env, a.id, 'paused', { epub: 'أ.epub', pages_waiting: [3], models_out: LITES,
    requests: { 'gemini-3.5-flash-lite': 500 } });
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

test('several books in «1- جديد»: one run carries them, the oldest its own; the next run starts when it ends', () => {
  const env = readyEnv();
  const b1 = addBook(env, 'الثاني.pdf', { ageMin: 20 });
  const b0 = addBook(env, 'الأول.pdf', { ageMin: 30 });
  const b2 = addBook(env, 'الثالث.pdf', { ageMin: 10 });
  tick(env);
  assert.strictEqual(env.gh.dispatches.length, 1);
  let d = lastDispatch(env).body.inputs;
  assert.deepStrictEqual([d.drive_file_id, JSON.parse(d.queue).books.map((x) => x.id)], [b0.id, [b1.id, b2.id]], 'oldest first');
  assert.strictEqual(names(env, 'inbox').length, 0);
  env.gh.runs = [{ id: 1, status: 'in_progress', created_at: ghTime(env.clock + 5000) }];
  for (let i = 0; i < 3; i++) { minutes(env, 10); tick(env); }
  assert.strictEqual(env.gh.dispatches.length, 1, 'one run at a time');

  writeStatus(env, b0.id, 'done', { epub: 'الأول.epub' });
  writeEpub(env, b0.id, 'الأول.epub');
  writeStatus(env, b1.id, 'paused', { waiting_for: 'time', stages: { done: 2, read2: 8 } });
  writeStatus(env, b2.id, 'paused', { waiting_for: 'time', stages: { unread: 10 } });
  tick(env);
  assert.strictEqual(env.gh.dispatches.length, 2, 'next run started in the same check');
  d = lastDispatch(env).body.inputs;
  assert.deepStrictEqual([d.drive_file_id, JSON.parse(d.queue).books.map((x) => x.id)], [b1.id, [b2.id]]);
  assert.strictEqual(jobs(env)[b0.id].stage, 'delivered');
});

test('a book left in قيد التحويل without a job is picked up again', () => {
  const env = readyEnv();
  const f = addBook(env, 'يتيم.pdf', { folder: 'working' });
  const g = addBook(env, 'جديد.pdf', { ageMin: 60 });
  tick(env);
  const d = lastDispatch(env).body.inputs;
  assert.deepStrictEqual([d.drive_file_id, JSON.parse(d.queue).books.map((x) => x.id)], [g.id, [f.id]], 'oldest first');
  assert.strictEqual(names(env, 'inbox').length, 0);
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
  const g = addBook(env, 'التالي.pdf', { ageMin: 20 });
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
  assert.strictEqual(s.queued, 0, 'the other book is in the run');
  assert.deepStrictEqual(s.recent, []);
  assert.ok(/^https:\/\/drive\.google\.com\/drive\/folders\//.test(s.links.inbox));
  assert.ok(s.lastTick);
  writeStatus(env, f.id, 'done', { epub: 'الأول.epub' });
  writeEpub(env, f.id, 'الأول.epub');
  writeStatus(env, g.id, 'paused', { waiting_for: 'time', stages: { unread: 10 } });
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
  // "افحص الآن" is one check that says what it did; the live view and "شغّل الآن" are their own calls.
  assert.ok(/google\.script\.run[\s\S]*\.checkNow\(\)/.test(PAGE));
  assert.ok(/\.getStatus\(\)/.test(PAGE));
  assert.ok(/\.getLive\(JSON\.stringify/.test(PAGE));
  assert.ok(/\.runNow\(fileId\)/.test(PAGE));
  // Every function the page calls is a public function of Code.gs (google.script.run cannot call private ones).
  const called = new Set((PAGE.match(/\.(\w+)\((?:[^()]|\([^()]*\))*\);?\s*(?:\n|\})/g) || []).map((x) => /\.(\w+)\(/.exec(x)[1]));
  ['getStatus', 'getLive', 'checkNow', 'runNow', 'getReview', 'getReviewImage', 'saveAnswers', 'sendReview', 'helperRequest', 'setBoxes', 'retryFree']
    .forEach((fn) => {
      assert.ok(new RegExp('\\.' + fn + '\\(').test(PAGE), 'the page calls ' + fn);
      assert.ok(new RegExp('\\nfunction ' + fn + '\\(').test(CODE), fn + ' is public in Code.gs');
    });
  assert.ok(called.size > 0);
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
  assert.ok(text.indexOf('موسوعة كبيرة') >= 0 && /يعمل التشغيل على GitHub منذ ساعة و35 دقيقة/.test(text), text);
  // What the script did, under «الآن».
  assert.ok(/آخر ما حدث/.test(text) && /اكتمل وسُلّم إلى «6- مكتمل»/.test(text), text);
  assert.ok(/منتهٍ/.test(byId.recent.textContent) && /افتح الكتاب/.test(byId.recent.textContent));
  assert.ok(/صورة\.png/.test(byId.queue.textContent));
  assert.ok(/آخر فحص/.test(byId.sub.textContent));

  writeStatus(env, big.id, 'paused', { quota_day: '2026-10-19', pages_total: 800, pages_done: 200 });
  tick(env);
  sandbox.render(JSON.parse(env.ctx.getStatus()));
  text = byId.now.textContent;
  assert.ok(/ينتظر تجدد الرصيد/.test(text) && /متوقف: نفد رصيد النماذج المجانية لهذا اليوم/.test(text) && /200 *من 800/.test(text), text);
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

test('a review sent while another book waits for its quota is applied at once; that book resumes the next day', () => {
  const env = readyEnv({ start: '2026-10-19T15:00:00Z' });   // Pacific 08:00 on the 19th
  const { f, items } = deliverWithPlaces(env, 'أ.pdf', 2);
  const b = addBook(env, 'ب.pdf');
  tick(env);
  assert.strictEqual(lastDispatch(env).body.inputs.drive_file_id, b.id);
  minutes(env, 60);
  writeStatus(env, b.id, 'paused', { quota_day: '2026-10-19', pages_total: 500, pages_done: 100 });
  env.gh.runs = [{ id: 1, status: 'completed', created_at: ghTime(env.clock - 3500000) }];
  tick(env);
  assert.strictEqual(jobs(env)[b.id].pausedDay, '2026-10-19');
  const n = env.gh.dispatches.length;

  // The user finishes the review and sends it: the round starts now, not days later after ب.
  items.pages[0].items.forEach((it) => env.ctx.saveAnswers(f.id, JSON.stringify({ [it.id]: answerFor(it, 'keep') })));
  assert.strictEqual(JSON.parse(env.ctx.sendReview(f.id)).started, true);
  assert.strictEqual(env.gh.dispatches.length, n + 1);
  const d = lastDispatch(env).body.inputs;
  assert.strictEqual(d.drive_file_id, f.id);
  assert.ok(/:only$/.test(d.review), d.review);
  assert.strictEqual(jobs(env)[b.id].pausedDay, '2026-10-19', 'ب still waits for its quota');
  assert.strictEqual(status(env).reviews[0].pending, true);

  // A second check while the round runs starts nothing else.
  minutes(env, 3);
  tick(env);
  assert.strictEqual(env.gh.dispatches.length, n + 1);

  // Nothing unsure left: the book goes to 6- مكتمل, and ب still waits.
  minutes(env, 3);
  putItems(env, f.id, reviewItems(0));
  writeStatus(env, f.id, 'done', { epub: 'b.epub', pages_unsure: [], review_items: 0, review_kept: 2 });
  writeEpub(env, f.id, 'b.epub', Buffer.from('PK all checked'));
  tick(env);
  assert.strictEqual(jobs(env)[f.id].final, true);
  assert.ok(epubsIn(env, 'final').some((x) => x.content.equals(Buffer.from('PK all checked'))));
  assert.strictEqual(env.gh.dispatches.length, n + 1, 'nothing else starts');
  assert.strictEqual(status(env).active.fileId, b.id);
  assert.strictEqual(status(env).active.paused, true);

  // The next Pacific day: ب goes on.
  env.clock = Date.parse('2026-10-20T07:05:00Z');
  tick(env);
  assert.strictEqual(env.gh.dispatches.length, n + 2);
  assert.strictEqual(lastDispatch(env).body.inputs.drive_file_id, b.id);
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
    ['getStatus', 'tick', 'getReview', 'getReviewImage', 'saveAnswers', 'sendReview', 'setBoxes', 'helperRequest',
      'getLive', 'checkNow', 'runNow', 'retryFree'].forEach((name) => {
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

test('the review screen: «the other reading is right» for one place, or for every place left on the page', () => {
  const env = readyEnv();
  const id = '1Bbook0000000aBcDeF';
  const status = JSON.parse(env.ctx.getStatus());
  status.reviews = [{ fileId: id, title: 'كتاب', open: 3, saved: 0, pending: false, note: '' }];
  const P = pageSandbox(env, status);
  P.click(P.button('راجِع', P.byId.reviews));
  P.answer(P.pending('getReview')[0], reviewForPage());
  const saved = {};
  const flush = () => { let s; while ((s = P.pending('saveAnswers')).length) { Object.assign(saved, JSON.parse(s[0].args[1])); P.answer(s[0], { ok: true, saved: 1 }); } };
  P.click(P.button('القراءة الأخرى هي الصحيحة'));
  flush();
  assert.deepStrictEqual([saved['p3-0000000001'].verdict, saved['p3-0000000001'].text], ['fix', 'كلمه']);
  // Place 2 is a line that may be missing: no «other reading» for it, the page-wide button skips it.
  assert.ok(!/القراءة الأخرى هي الصحيحة/.test(P.byId['rv-body'].textContent));
  P.click(P.button('تخطَّ'));
  P.click(P.button('القراءة الأخرى لكل ما بقي في الصفحة'));
  flush();
  assert.strictEqual(saved['p3-00000000aa'], undefined, 'the missing line is left');
  assert.ok(Object.keys(saved).length >= 1);
});

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
  // Every place of the page is listed, so the rest can be checked before «كل ما بقي … صحيح».
  const body = P.byId['rv-body'].textContent;
  assert.ok(/مواضع هذه الصفحة \(3\)/.test(body) && /سطر قد يكون ساقطاً/.test(body) && /كلمة3/.test(body), body);
  // «آخر صفحة» opens the first place of the last page (here the only page).
  P.click(P.button('آخر صفحة'));
  assert.ok(/الموضع 1 من 3/.test(P.byId['rv-count'].textContent));
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

// The board («النماذج», setBoxes) as the two older boards set it: the user's models (their
// reading box gave both readings) and the free models, each leaving the other's alone.
function setHelperBoxes(env, json) {
  const o = JSON.parse(json);
  const b = env.ctx.boxesInfo_();
  const free = (l) => (l || []).filter((id) => id.indexOf('|') < 0);
  const t = o.transcribe || [];
  return env.ctx.setBoxes(JSON.stringify({ read1: free(b.read1).concat(t), read2: free(b.read2).concat(t),
    settle: free(b.settle).concat(o.settle || []), review: free(b.review).concat(o.review || []), times: o.times || {} }));
}

function setFreeBoxes(env, json) {
  const o = JSON.parse(json);
  const b = env.ctx.boxesInfo_();
  const mine = (l) => (l || []).filter((id) => id.indexOf('|') >= 0);
  return env.ctx.setBoxes(JSON.stringify({ read1: (o.read1 || []).concat(mine(b.read1)), read2: (o.read2 || []).concat(mine(b.read2)),
    settle: (o.settle || []).concat(mine(b.settle)), review: (o.review || []).concat(mine(b.review)), times: b.times }));
}

function boxesEnv(boxes) {
  const env = helperEnv();
  status(env);
  const r = JSON.parse(setHelperBoxes(env, JSON.stringify(boxes)));
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

test('boxes: the board saves the models of each box; requests.json carries the user\'s; unknown ones are refused', () => {
  const env = helperEnv();
  status(env);
  const bad = JSON.parse(env.ctx.setBoxes(JSON.stringify({ read1: ['rm|-rf'], read2: [], settle: [], review: [] })));
  assert.strictEqual(bad.ok, false);
  assert.strictEqual(JSON.parse(env.ctx.setBoxes(JSON.stringify({ read1: ['gemini-x;rm'] }))).ok, false);
  const r = JSON.parse(env.ctx.setBoxes(JSON.stringify({ read1: [GEMINI, 'gemini-3.5-flash-lite', GEMINI], read2: [GROK],
    settle: ['gemini-3.8-flash', GEMINI], review: [GROK, 'gemma-4-31b-it'] })));
  assert.strictEqual(r.ok, true, r.error);
  assert.deepStrictEqual([r.boxes.read1, r.boxes.read2, r.boxes.settle, r.boxes.review],
    [[GEMINI, 'gemini-3.5-flash-lite'], [GROK], ['gemini-3.8-flash', GEMINI], [GROK, 'gemma-4-31b-it']]);
  // The helper gets its own models, by box (and "transcribe" for an older helper).
  assert.deepStrictEqual(requestsFile(env).boxes, { transcribe: [GEMINI, GROK], read1: [GEMINI], read2: [GROK], settle: [GEMINI],
    review: [GROK], times: {} });
  // The runs get the free ones.
  assert.strictEqual(env.ctx.freeInput_(env.ctx.freeInfo_()), 'read1=gemini-3.5-flash-lite;read2=;settle=gemini-3.8-flash;review=gemma-4-31b-it');
  assert.strictEqual(env.userLockHeld, false);
  writeHelperStatus(env, { models: MODELS.concat([{ id: 'bad id', label: 'x' }]),
    chips: { [GEMINI]: { state: 'working', text: 'يقرأ ص 3' }, [GROK]: { state: 'odd', text: 'x' } } });
  const s = status(env);
  assert.deepStrictEqual(s.helper.models.map((m) => m.id), [GEMINI, GROK]);
  assert.deepStrictEqual(s.helper.chips[GEMINI], { state: 'working', text: 'يقرأ ص 3' });
  assert.strictEqual(s.helper.chips[GROK].state, 'idle');
  assert.deepStrictEqual(s.boxes.review, [GROK, 'gemma-4-31b-it']);
});

test('transcribe: with models in the box the run gets the book folder; a book with the helper goes with the next book', () => {
  const env = boxesEnv({ transcribe: [GEMINI], review: [] });
  const a = addBook(env, 'أول.pdf');
  tick(env);
  const folderA = bookFolderOf(env, a.id);
  assert.ok(folderA, 'book folder made under كتب');
  assert.strictEqual(lastDispatch(env).body.inputs.helper, folderA.id + ':read1+read2');
  assert.strictEqual(lastDispatch(env).body.inputs.judge, undefined);

  const b = addBook(env, 'ثان.pdf');
  minutes(env, 25);
  writeStatus(env, a.id, 'paused', { waiting_for: 'helper', epub: 'أول.epub', pages_helper: [1, 2, 3], pages_done: 7 });
  writeHelperStatus(env, { models: MODELS });
  tick(env);
  // The next book starts at once, the book with the helper's models in the same run.
  assert.strictEqual(env.gh.dispatches.length, 2, 'the next book started right away');
  const d = lastDispatch(env).body.inputs;
  assert.strictEqual(d.drive_file_id, a.id);
  const q = JSON.parse(d.queue);
  assert.deepStrictEqual(q.books.map((x) => x.id), [b.id]);
  assert.strictEqual(q.books[0].helper, bookFolderOf(env, b.id).id + ':read1+read2', 'each book with its own folder');
  assert.strictEqual(env.mails.length, 0, 'no mail for waiting on the helper');

  // Both books' pages left are with the helper's models: the queue waits for them.
  minutes(env, 30);
  writeStatus(env, a.id, 'paused', { waiting_for: 'helper', pages_helper: [1, 2, 3], pages_done: 7 });
  writeStatus(env, b.id, 'paused', { waiting_for: 'helper', pages_helper: [4], pages_done: 9 });
  tick(env);
  assert.strictEqual(jobs(env)[a.id].waitFor, 'helper');
  assert.strictEqual(env.gh.dispatches.length, 2);
  // The helper finished b's page: seen twice, eight minutes apart, then the books run again.
  const bookAt = new Date(env.clock - 20 * 60000).toISOString();
  writeHelperStatus(env, { models: MODELS, books: { [b.id]: { need: 1, finished: 1, book_at: bookAt } } });
  minutes(env, 1);
  tick(env);
  assert.strictEqual(env.gh.dispatches.length, 2, 'not on the first sight');
  minutes(env, 10);
  writeHelperStatus(env, { models: MODELS, books: { [b.id]: { need: 1, finished: 1, book_at: bookAt } } });
  tick(env);
  assert.strictEqual(env.gh.dispatches.length, 3);
  assert.strictEqual(lastDispatch(env).body.inputs.drive_file_id, a.id);

  minutes(env, 10);
  writeStatus(env, a.id, 'done', { epub: 'أول.epub' });
  writeEpub(env, a.id, 'أول.epub');
  writeStatus(env, b.id, 'done', { epub: 'ثان.epub' });
  writeEpub(env, b.id, 'ثان.epub');
  tick(env);
  assert.strictEqual(jobs(env)[a.id].stage, 'delivered');
  assert.strictEqual(jobs(env)[b.id].stage, 'delivered');
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
  assert.strictEqual(lastDispatch(env).body.inputs.helper, folderA.id + ':read1+read2');
  // They stopped for time with quota left: the next run starts in the same check.
  minutes(env, 40);
  writeStatus(env, a.id, 'paused', { waiting_for: 'helper', epub: 'كبير.epub', pages_total: 923, pages_done: 400,
    pages_helper: [60, 61], free_left: true });
  tick(env);
  assert.strictEqual(env.gh.dispatches.length, 3);
  assert.strictEqual(lastDispatch(env).body.inputs.helper, folderA.id + ':read1+read2');
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
  setHelperBoxes(env, JSON.stringify({ transcribe: [], review: [] }));
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
  assert.ok(/:read1\+read2$/.test(lastDispatch(env3).body.inputs.helper));
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
  assert.deepStrictEqual(liveIn(env, dir).map((x) => x.name).sort(), ['answered.json', 'items.json', 'p0003-0123456789.jpg']);
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

test('review: free models in the review box judge the open places in a round on GitHub, once until something changes', () => {
  const env = readyEnv({ start: '2026-10-23T07:03:00Z' });
  env.ctx.setBoxes(JSON.stringify({ read1: ['gemini-3.5-flash-lite'], read2: ['gemini-3.1-flash-lite'], settle: ['gemini-3.8-flash'],
    review: ['gemma-4-31b-it'] }));
  const f = addBook(env, 'مجاني.pdf');
  tick(env);
  minutes(env, 30);
  putItems(env, f.id, reviewItems(3));
  writeStatus(env, f.id, 'done', { epub: 'b.epub', pages_unsure: [3], review_items: 3, review_seen: '' });
  writeEpub(env, f.id, 'b.epub', Buffer.from('PK first'));
  tick(env);
  // Delivered: in the same check, a review round for the free models.
  const before = env.gh.dispatches.length - 1;
  const d = lastDispatch(env).body.inputs;
  assert.strictEqual(jobs(env)[f.id].stage, 'running');
  const folder = sub(env, sub(env, helperFolder(env), 'مراجعة'), f.id);
  assert.ok(folder, 'the book\'s review folder, where every model\'s verdicts are');
  assert.strictEqual(d.helper, folder.id + ':review');
  assert.ok(/:only$/.test(d.review), 'no conversion: the places only');
  assert.ok(/;review=gemma-4-31b-it/.test(d.models), d.models);
  assert.ok(/مراجعة المشكوك فيه بالنماذج المجانية/.test(journal(env).pop().x));
  // It applied two answers: the book is delivered again, and the next round waits for a change.
  minutes(env, 8);
  writeStatus(env, f.id, 'done', { epub: 'مجاني.epub', pages_unsure: [3], review_items: 1, review_applied: 2,
    free_review: { asked: 2, answers: 2, left: 0 } });
  writeEpub(env, f.id, 'مجاني.epub');
  tick(env);
  assert.strictEqual(jobs(env)[f.id].stage, 'delivered');
  assert.ok(env.mails.some((m) => /طُبّقت أحكام النماذج/.test(m.subject)));
  assert.deepStrictEqual(Object.keys(JSON.parse(env.props.get('RV_' + f.id)).fr).sort(), ['asked', 'at', 'left', 'sig']);
  minutes(env, 60);
  tick(env);
  assert.strictEqual(env.gh.dispatches.length, before + 1, 'nothing changed: no round for nothing');
  // Another free model in the review box: it judges too.
  env.ctx.setBoxes(JSON.stringify(Object.assign(env.ctx.boxesInfo_(), { review: ['gemma-4-31b-it', 'gemini-3.8-flash'] })));
  tick(env);
  assert.strictEqual(env.gh.dispatches.length, before + 2);
  // A round that applied nothing keeps the book as it was and sends no mail.
  const mails = env.mails.length;
  minutes(env, 8);
  writeStatus(env, f.id, 'done', { epub: 'مجاني.epub', pages_unsure: [3], review_items: 1, free_review: { asked: 1, answers: 0, left: 0 } });
  tick(env);
  assert.strictEqual(env.mails.length, mails);
  assert.ok(/لم تُطبَّق إجابة جديدة/.test(journal(env).pop().x));
  // Out of quota today: no round.
  env.ctx.setBoxes(JSON.stringify(Object.assign(env.ctx.boxesInfo_(), { review: ['gemini-2.5-flash'] })));
  const u = { day: '2026-10-23', m: { 'gemini-2.5-flash': { n: 20, out: true } }, runs: [] };
  env.props.set('USAGE', JSON.stringify(u));
  const n = env.gh.dispatches.length;
  minutes(env, 10);
  tick(env);
  assert.strictEqual(env.gh.dispatches.length, n);
});

test('review: a free models\' round that fails leaves the user\'s own review alone', () => {
  const env = readyEnv({ start: '2026-10-23T07:03:00Z' });
  env.ctx.setBoxes(JSON.stringify({ read1: ['gemini-3.5-flash-lite'], read2: [], settle: [], review: ['gemma-4-31b-it'] }));
  const f = addBook(env, 'تعثر.pdf');
  tick(env);
  minutes(env, 30);
  putItems(env, f.id, reviewItems(3));
  writeStatus(env, f.id, 'done', { epub: 'b.epub', pages_unsure: [3], review_items: 3, review_seen: '' });
  writeEpub(env, f.id, 'b.epub', Buffer.from('PK first'));
  tick(env);
  assert.strictEqual(lastDispatch(env).body.inputs.helper.split(':')[1], 'review');
  const rv = JSON.parse(env.props.get('RV_' + f.id));
  minutes(env, 8);
  writeStatus(env, f.id, 'failed', { reason: 'crash' });
  tick(env);
  const after = JSON.parse(env.props.get('RV_' + f.id));
  assert.strictEqual(after.tries, rv.tries);
  assert.ok(!after.x);
  assert.strictEqual(after.fr.failed, true);
  assert.strictEqual(jobs(env)[f.id].stage, 'delivered');
  const n = env.gh.dispatches.length;
  minutes(env, 60);
  tick(env);
  assert.strictEqual(env.gh.dispatches.length, n, 'not tried again until something changes');
});

test('review: the user and the models in the review box never do the same place twice', () => {
  const env = boxesEnv({ transcribe: [], review: [GEMINI] });
  const { f, items: placed } = deliverWithPlaces(env, 'معا.pdf', 4);
  const items = placed.pages[0].items;
  minutes(env, 10);
  tick(env);
  const dir = sub(env, sub(env, helperFolder(env), 'مراجعة'), f.id);
  const answered = () => {
    const x = liveIn(env, dir).filter((y) => y.name === 'answered.json');
    assert.ok(x.length <= 1, 'one answered.json');
    return x.length ? JSON.parse(x[0].content.toString('utf8')).ids : null;
  };
  assert.deepStrictEqual(answered(), [], 'nothing answered yet');

  // The user answers from the end of the book, without sending: the models are told at the next check.
  JSON.parse(env.ctx.saveAnswers(f.id, JSON.stringify({ [items[3].id]: answerFor(items[3], 'keep') })));
  const rest = 'p3-rest-abc';
  JSON.parse(env.ctx.saveAnswers(f.id, JSON.stringify({ [rest]: { kind: 'rest', page: 3, verdict: 'keep',
    ids: [items[2].id], fps: [items[2].fp] } })));
  minutes(env, 10);
  tick(env);
  assert.deepStrictEqual(answered().sort(), [items[2].id, items[3].id].sort());
  const file = liveIn(env, dir).find((y) => y.name === 'answered.json');
  const at = file.updated;
  minutes(env, 10);
  tick(env);
  assert.strictEqual(liveIn(env, dir).find((y) => y.name === 'answered.json').updated, at, 'not written again unchanged');
  assert.strictEqual(env.gh.dispatches.filter((d) => d.body.inputs.review).length, 0, 'and no run for it');

  // The models settled the first place: the user does not see it; the second, which they
  // left to the user, stays with the user's own unanswered places.
  const verdicts = helperFolder(env).createFolder('أحكام');
  verdicts.createFile(f.id + '.json', JSON.stringify({ v: 1, fileId: f.id, done: false, answers: {
    [items[0].id]: Object.assign(answerFor(items[0], 'keep'), { at: iso(env.clock), by: [GEMINI] }) } }), 'application/json');
  let r = JSON.parse(env.ctx.getReview(f.id));
  assert.deepStrictEqual(r.pages[0].items.map((x) => x.id), [items[1].id]);

  // A run carried both (answers_seen is the latest answer it had): a place still open after it
  // (a verdict that could not be applied) is shown again, and the models are told nothing is new.
  const later = reviewItems(4, 3, iso(env.clock));
  putItems(env, f.id, later);
  writeStatus(env, f.id, 'done', { epub: 'b.epub', pages_unsure: [3], review_items: 4, review_seen: iso(env.clock) });
  writeEpub(env, f.id, 'b.epub', Buffer.from('PK again'));
  const rv = JSON.parse(env.props.get('RV_' + f.id));
  rv.seen = iso(env.clock);
  env.props.set('RV_' + f.id, JSON.stringify(rv));
  minutes(env, 10);
  tick(env);
  r = JSON.parse(env.ctx.getReview(f.id));
  assert.deepStrictEqual(r.pages[0].items.map((x) => x.id), items.map((x) => x.id));
  assert.deepStrictEqual(answered(), []);
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
  setHelperBoxes(env, JSON.stringify({ transcribe: [], review: [] }));
  env.drive.DriveApp.getFileById(f.id).setTrashed(true);
  minutes(env, 10);
  tick(env);
  assert.strictEqual(sub(env, reviews(), f.id), undefined, 'gone with the box empty');

  // Deleted between two checks: putting a model in the box removes it at once.
  const { f: g } = deliverWithPlaces(env, 'ثان.pdf', 2);
  env.gh.files['drive-' + g.id + '/review/p0003-0123456789.jpg'] = Buffer.from([0xff, 0xd8, 1]);
  setHelperBoxes(env, JSON.stringify({ transcribe: [], review: [GEMINI] }));
  minutes(env, 10);
  tick(env);
  assert.ok(sub(env, reviews(), g.id));
  setHelperBoxes(env, JSON.stringify({ transcribe: [], review: [] }));
  env.drive.DriveApp.getFileById(g.id).setTrashed(true);
  setHelperBoxes(env, JSON.stringify({ transcribe: [], review: [GEMINI] }));
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
  setHelperBoxes(env, JSON.stringify({ transcribe: [], review: [] }));
  const before = env.gh.dispatches.length;
  minutes(env, 10);
  tick(env);
  assert.strictEqual(env.gh.dispatches.length, before + 1, 'a round with the verdicts');
  assert.strictEqual(lastDispatch(env).body.inputs.judge, file.id);
});

test('the page: one board, any model in any box with a tap (or a drag), saved, and back if saving fails', () => {
  const env = boxesEnv({ transcribe: [GEMINI], review: [] });
  writeHelperStatus(env, { models: MODELS.concat([{ id: 'agy|gemini-4-argon', label: 'Gemini 4 Argon', tool: 'Antigravity',
    warn: 'لا للتفريغ: يستنفد الحدود' }]), chips: { [GEMINI]: { state: 'working', text: 'يقرأ ص 12 من «أول»' } } });
  const page = pageSandbox(env, status(env));
  const boxes = page.byId.boxes;
  assert.strictEqual(boxes.hidden, false);
  const sections = () => boxes.kids.filter((k) => k.className === 'bx');
  assert.deepStrictEqual(sections().map((x) => x.kids[0].kids[0].textContent),
    ['القراءة الأولى', 'القراءة الثانية', 'الحسم', 'مراجعة المشكوك فيه']);
  const pair = (i) => sections()[i].kids.find((k) => k.className === 'pair');
  const find = (node, pred) => { if (pred(node)) return node; for (const k of node.kids || []) { const f = find(k, pred); if (f) return f; } return null; };
  const buttonIn = (root, text) => find(root, (k) => k.tag === 'button' && k.textContent.indexOf(text) >= 0);
  const cardOf = (box, text) => box.kids.find((k) => /\b(mcard|mrow)\b/.test(k.className) && k.textContent.indexOf(text) >= 0);
  // The first reading: Gemini (the user's) and the two Lite readers (free) work there.
  let [off, on] = pair(0).kids;
  assert.ok(/Gemini 3\.8 Flash/.test(on.textContent) && /يقرأ ص 12/.test(on.textContent), on.textContent);
  assert.ok(/Gemini 3\.5 Flash-Lite/.test(on.textContent), 'the free models are on the same board');
  // «المتاحة» is folded under one button while the box has models.
  assert.ok(!/Grok 4\.7/.test(off.textContent), off.textContent);
  page.click(buttonIn(off, 'أضف نموذجاً'));
  off = pair(0).kids[0];
  assert.ok(/Grok 4\.7/.test(off.textContent) && /يستنفد الحدود/.test(off.textContent), off.textContent);
  assert.deepStrictEqual(off.kids.filter((k) => k.className === 'grp').map((g) => g.kids[0].textContent),
    ['مجانية (على GitHub)', 'Grok Build', 'Antigravity'], 'in rows: the free ones, then by program');
  // An empty box says nobody works there, and shows what can.
  assert.ok(/لا أحد يعمل في «مراجعة المشكوك فيه»/.test(pair(3).kids[1].textContent));
  assert.ok(/Gemma 4 31B/.test(pair(3).kids[0].textContent) && /Grok 4\.7/.test(pair(3).kids[0].textContent));

  // A tap on Grok puts it to work in the first reading.
  page.click(buttonIn(off, 'Grok 4.7'));
  let call = page.pending('setBoxes')[0];
  assert.deepStrictEqual(JSON.parse(call.args[0]).read1.slice(-2), [GEMINI, GROK]);
  assert.ok(/Grok 4\.7/.test(pair(0).kids[1].textContent), 'shown in «تعمل الآن» at once');
  page.answer(call, env.ctx.setBoxes(call.args[0]));
  assert.deepStrictEqual(JSON.parse(env.props.get('MODEL_BOXES')).read1.slice(-2), [GEMINI, GROK]);

  // A start time: Grok begins tomorrow at 14:30 (local time). The times open under its card.
  on = pair(0).kids[1];
  assert.ok(/يبدأ: الآن · يتوقف: لا يتوقف/.test(cardOf(on, 'Grok 4.7').textContent));
  page.click(buttonIn(cardOf(on, 'Grok 4.7'), 'المواعيد'));
  const startRow = find(cardOf(pair(0).kids[1], 'Grok 4.7'), (k) => k.className === 'times').kids[0];
  const daySel = find(startRow, (k) => k.tag === 'select');
  const clock = find(startRow, (k) => k.tag === 'input');
  daySel.value = '0';
  clock.value = '00:00';
  page.click(buttonIn(startRow, 'حدّد'));
  assert.strictEqual(page.pending('setBoxes').length, 0, 'a time already past is not saved');
  assert.ok(/مضى/.test(page.byId.toasts.textContent));
  daySel.value = '1';
  clock.value = '14:30';
  page.click(buttonIn(startRow, 'حدّد'));
  const timed = JSON.parse(page.pending('setBoxes')[0].args[0]);
  const expect = new Date();
  expect.setDate(expect.getDate() + 1);
  expect.setHours(14, 30, 0, 0);
  assert.strictEqual(Date.parse(timed.times[GROK].start), expect.getTime(), 'tomorrow at 14:30');
  page.answer(page.pending('setBoxes')[0], env.ctx.setBoxes(JSON.stringify(timed)));
  assert.strictEqual(JSON.parse(env.props.get('MODEL_BOXES')).times[GROK].start, timed.times[GROK].start);
  const grok = cardOf(pair(0).kids[1], 'Grok 4.7').textContent;
  assert.ok(/مجدول/.test(grok) && /يبدأ: [^·]*:30/.test(grok) && !/يبدأ خلال دقيقة/.test(grok), grok);

  // «أخرجه» on Gemini; saving fails: it comes back.
  page.click(buttonIn(cardOf(pair(0).kids[1], 'Gemini 3.8 Flash'), 'أخرجه'));
  assert.ok(JSON.parse(page.pending('setBoxes')[0].args[0]).read1.indexOf(GEMINI) < 0);
  page.fail(page.pending('setBoxes')[0], 'offline');
  assert.ok(/Gemini 3\.8 Flash/.test(pair(0).kids[1].textContent));
  assert.ok(/تعذّر الحفظ/.test(page.byId.toasts.textContent));
  // A drop on «تعمل الآن» of settling, then of the review.
  pair(2).kids[1].handlers.drop[0]({ preventDefault() {}, dataTransfer: { getData: () => JSON.stringify({ id: GEMINI }) } });
  assert.ok(JSON.parse(page.pending('setBoxes')[0].args[0]).settle.indexOf(GEMINI) >= 0);
  page.fail(page.pending('setBoxes')[0], 'offline');
  pair(3).kids[1].handlers.drop[0]({ preventDefault() {}, dataTransfer: { getData: () => JSON.stringify({ id: 'gemma-4-31b-it' }) } });
  assert.deepStrictEqual(JSON.parse(page.pending('setBoxes')[0].args[0]).review, ['gemma-4-31b-it']);
});

test('the page: the waiting books by name in their order, and how each finished book ended', () => {
  const env = readyEnv();
  const first = addBook(env, 'أول.pdf', { ageMin: 30 });
  addBook(env, 'ثان.docx', { ageMin: 20 });
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

// ---------------------------------------------------------------------------
// The live view, the journal and "run now" (live.py, docs/drive-inbox.md)
// ---------------------------------------------------------------------------

function journal(env) {
  return JSON.parse(env.props.get('JOURNAL') || '[]');
}

const LITES = ['gemini-3.5-flash-lite', 'gemini-3.1-flash-lite'];

test('live: a run that stopped for time with quota left goes on at once; a day has at most MAX_TIME_RUNS', () => {
  const env = readyEnv({ start: '2026-10-23T07:10:00Z' });
  const f = addBook(env, 'طويل.pdf');
  tick(env);
  minutes(env, 52);
  writeStatus(env, f.id, 'paused', { waiting_for: 'time', pages_total: 500, pages_done: 120,
    stages: { done: 120, read2: 200, unread: 180, bogus: 3 }, requests: { 'gemini-3.5-flash-lite': 300, 'gemini-3.1-flash-lite': 290 } });
  tick(env);
  assert.strictEqual(env.gh.dispatches.length, 2, 'the next run at once');
  let j = jobs(env)[f.id];
  assert.strictEqual(j.pausedDay, '');
  assert.strictEqual(j.attempts, 1, 'not an attempt');
  assert.deepStrictEqual(j.stages, { done: 120, read2: 200, unread: 180 });
  const lines = journal(env);
  const end = lines.find((e) => e.k === 'end');
  assert.ok(end && /انتهى وقت التشغيل/.test(end.x) && /قُرئت قراءتين وتنتظر الحسم 200/.test(end.x) && /لم تُقرأ 180/.test(end.x), end && end.x);
  assert.strictEqual(lines[lines.length - 1].k, 'start');
  assert.ok(/يكمل ما بقي/.test(lines[lines.length - 1].x), lines[lines.length - 1].x);
  assert.strictEqual(status(env).usage.m['gemini-3.5-flash-lite'].n, 300);
  assert.strictEqual(env.mails.length, 0, 'no "سيكمل غداً" mail');
  const max = env.ctx.CONFIG.MAX_TIME_RUNS;
  for (let i = 0; i < max; i++) {
    minutes(env, 15);
    writeStatus(env, f.id, 'paused', { waiting_for: 'time' });
    tick(env);
  }
  j = jobs(env)[f.id];
  assert.strictEqual(env.gh.dispatches.length, max + 1);
  assert.strictEqual(j.pausedDay, '2026-10-23', 'after MAX_TIME_RUNS the book waits for the next day');
});

test('live: a pause says which free models ran out and where the pages stand; usage counts each run once', () => {
  const env = readyEnv({ start: '2026-10-23T07:03:00Z' });   // Riyadh 10:03, just after the quotas renewed
  const f = addBook(env, 'تراث.pdf');
  tick(env);
  minutes(env, 79);
  const st = writeStatus(env, f.id, 'paused', { quota_day: '2026-10-23', pages_total: 509, pages_done: 54,
    stages: { done: 38, unsure: 16, read2: 441, unread: 14 },
    requests: { 'gemini-3.5-flash-lite': 494, 'gemini-3.1-flash-lite': 497, 'gemini-3.8-flash': 20 },
    models_out: LITES.concat('gemini-3.8-flash') });
  tick(env);
  env.ctx.noteUsage_(JSON.parse(JSON.stringify(st)));   // the same run again: not counted twice
  const s = status(env);
  assert.strictEqual(s.active.paused, true);
  assert.strictEqual(s.active.why, 'نفد رصيد Gemini 3.5 Flash-Lite وGemini 3.1 Flash-Lite لهذا اليوم');
  assert.deepStrictEqual(s.active.stages, { done: 38, unsure: 16, read2: 441, unread: 14 });
  assert.strictEqual(s.active.canRun, true);
  assert.deepStrictEqual(s.usage.m['gemini-3.5-flash-lite'], { n: 494, out: true, at: st.updated_at.replace('Z', '.000Z') });
  assert.strictEqual(s.usage.m['gemini-3.8-flash'].n, 20);
  assert.strictEqual(s.usage.renew, '2026-10-24T07:00:00.000Z');
  const last = s.journal[s.journal.length - 1];
  assert.strictEqual(last.k, 'pause');
  assert.strictEqual(last.b, 'تراث');
  assert.ok(/نفد رصيد Gemini 3.5 Flash-Lite/.test(last.x) && /أُنجزت 54 \(منها 16 مشكوك فيها\)/.test(last.x) &&
    /تنتظر الحسم 441/.test(last.x) && /نحو 10:00 بتوقيتك/.test(last.x), last.x);
  assert.ok(/متوقف حتى يتجدد رصيد النماذج المجانية نحو 10:00/.test(env.ctx.nowLine_(jobs(env))));
  // A new Pacific day starts afresh.
  env.clock = Date.parse('2026-10-24T07:05:00Z');
  assert.deepStrictEqual(status(env).usage.m, {});
});

test('run now: a book waiting for its new day runs at once, as no pause or attempt of its own; refused while it runs', () => {
  const env = readyEnv({ start: '2026-10-23T07:03:00Z' });
  const f = addBook(env, 'تراث.pdf');
  tick(env);
  minutes(env, 79);
  writeStatus(env, f.id, 'paused', { quota_day: '2026-10-23', models_out: LITES });
  tick(env);
  const pauses = jobs(env)[f.id].pauses;
  assert.strictEqual(pauses, 1);
  minutes(env, 60);
  let r = JSON.parse(env.ctx.runNow(f.id));
  assert.deepStrictEqual(r, { ok: true, started: true, error: '' });
  assert.strictEqual(env.gh.dispatches.length, 2);
  assert.ok(lastDispatch(env).body.inputs.live, 'with the live view');
  let j = jobs(env)[f.id];
  assert.strictEqual(j.stage, 'running');
  assert.strictEqual(j.pausedDay, '');
  assert.strictEqual(j.attempts, 1);
  assert.ok(/طلبتَ التشغيل الآن/.test(journal(env).pop().x));
  assert.strictEqual(env.lockHeld, false);
  r = JSON.parse(env.ctx.runNow(f.id));
  assert.strictEqual(r.ok, false);
  assert.ok(/يعمل الآن/.test(r.error), r.error);
  assert.strictEqual(env.gh.dispatches.length, 2);
  // The run finds the quota still out: it waits again, and that is no pause of its own.
  minutes(env, 6);
  writeStatus(env, f.id, 'paused', { quota_day: '2026-10-23', models_out: LITES });
  tick(env);
  j = jobs(env)[f.id];
  assert.strictEqual(j.pauses, pauses);
  assert.strictEqual(j.forced, undefined);
  assert.strictEqual(j.pausedDay, '2026-10-23');
  assert.strictEqual(env.mails.length, 1, 'one "سيكمل غداً" mail only');
  assert.strictEqual(JSON.parse(env.ctx.runNow('x')).ok, false);
  assert.ok(/لم أجد/.test(JSON.parse(env.ctx.runNow('1B99999aBcDeFgHiJkLmNoPqRsTuVwX')).error));
});

test('run now: a parked book runs when no book holds the slot, and waits while one does', () => {
  const env = readyEnv({ start: '2026-10-23T09:00:00Z' });
  const parked = addBook(env, 'مركون.pdf', { folder: 'working' });
  const holder = addBook(env, 'متوقف.pdf', { folder: 'working' });
  const base = (f, title) => ({ fileId: f.id, name: title + '.pdf', title, createdAt: iso(env.clock - 7200000),
    dispatchedAt: iso(env.clock - 3600000), attempts: 1, resultDir: 'drive-' + f.id, lastError: '' });
  const all = {};
  all[parked.id] = Object.assign(base(parked, 'مركون'), { stage: 'helper', helperAt: iso(env.clock - 600000), freeDay: '2026-10-23',
    helperPages: 40 });
  all[holder.id] = Object.assign(base(holder, 'متوقف'), { stage: 'running', pausedDay: '2026-10-23' });
  env.props.set('JOBS', JSON.stringify(all));
  let r = JSON.parse(env.ctx.runNow(parked.id));
  assert.strictEqual(r.ok, false);
  assert.ok(/«متوقف» يشغل الدور الآن/.test(r.error), r.error);
  const s = status(env);
  assert.strictEqual(s.active.canRun, true);
  assert.strictEqual(s.working[0].canRun, false);
  delete all[holder.id];
  env.props.set('JOBS', JSON.stringify(all));
  assert.strictEqual(status(env).working[0].canRun, true);
  r = JSON.parse(env.ctx.runNow(parked.id));
  assert.strictEqual(r.ok, true);
  assert.strictEqual(jobs(env)[parked.id].stage, 'running');
  assert.strictEqual(lastDispatch(env).body.inputs.drive_file_id, parked.id);
});

test('live: the page reads the live file every few seconds, GitHub\'s steps while it is quiet, and is told when the results are in', () => {
  const env = readyEnv({ start: '2026-10-23T07:03:00Z' });
  const f = addBook(env, 'مباشر.pdf');
  tick(env);
  const file = env.drive.DriveApp.getFileById(liveId(lastDispatch(env)));
  let g = JSON.parse(env.ctx.getLive('{}'));
  assert.strictEqual(g.live.phase, 'queued');
  assert.strictEqual(g.live.file, f.id);
  assert.strictEqual(g.job.fileId, f.id);
  assert.strictEqual(g.due, false);
  // Before the run writes: its steps on GitHub (asked at most every 20 seconds).
  env.gh.runs = [{ id: 55, run_number: 43, status: 'in_progress', created_at: ghTime(env.clock + 20000) }];
  env.gh.jobs['55'] = [{ steps: [
    { name: 'Set up job', status: 'completed', conclusion: 'success', started_at: ghTime(env.clock + 25000) },
    { name: 'Install Pandoc and Tesseract', status: 'in_progress', conclusion: null, started_at: ghTime(env.clock + 40000) }] }];
  minutes(env, 1);
  g = JSON.parse(env.ctx.getLive('{}'));
  assert.strictEqual(g.run.id, 55);
  assert.deepStrictEqual(g.run.steps[1], { n: 'Install Pandoc and Tesseract', s: 'in_progress', c: '', at: ghTime(env.clock - 20000) });
  const asked = env.requests.length;
  env.ctx.getLive('{}');
  assert.strictEqual(env.requests.length, asked, 'GitHub is asked at most every 20 seconds');
  // The run writes: only the events the page does not have yet come back.
  const dispatched = JSON.parse(file.content.toString('utf8')).dispatched;
  file.setContent(JSON.stringify({ v: 1, seq: 5, run: '55', file: f.id, dispatched, phase: 'read', at: iso(env.clock),
    events: [{ i: 1, r: 'ok' }, { i: 2, r: 'ok' }, { i: 3, r: 'rest' }] }));
  g = JSON.parse(env.ctx.getLive(JSON.stringify({ run: '55', ev: 2 })));
  assert.deepStrictEqual(g.live.events.map((e) => e.i), [3]);
  assert.strictEqual(g.run, undefined, 'the live file speaks for the run');
  assert.strictEqual(g.due, false);
  // Quiet for more than 90 seconds (its token, say): GitHub's steps again.
  minutes(env, 2);
  assert.strictEqual(JSON.parse(env.ctx.getLive('{}')).run.id, 55);
  // The results are saved: the page asks for a check, which takes them in at once.
  file.setContent(JSON.stringify({ v: 1, seq: 9, run: '55', file: f.id, dispatched, phase: 'saved', at: iso(env.clock) }));
  assert.strictEqual(JSON.parse(env.ctx.getLive('{}')).due, true);
  writeStatus(env, f.id, 'done', { epub: 'م.epub', requests: { 'gemini-3.5-flash-lite': 12 } });
  writeEpub(env, f.id, 'م.epub');
  const c = JSON.parse(env.ctx.checkNow());
  assert.strictEqual(c.ok, true);
  assert.ok(c.journal.some((e) => e.k === 'deliver' && /سُلّم إلى «6- مكتمل»/.test(e.x)), JSON.stringify(c.journal));
  assert.strictEqual(c.line, 'لا شيء يُحوَّل الآن.');
  g = JSON.parse(env.ctx.getLive(JSON.stringify({ after: c.journal[c.journal.length - 1].i })));
  assert.strictEqual(g.due, false);
  assert.deepStrictEqual(g.journal, []);
  assert.strictEqual(g.usage.m['gemini-3.5-flash-lite'].n, 12);
});

test('check now: a check that changes nothing says what the books wait for', () => {
  const env = readyEnv({ start: '2026-10-23T07:03:00Z' });
  const f = addBook(env, 'تراث.pdf');
  tick(env);
  minutes(env, 79);
  writeStatus(env, f.id, 'paused', { quota_day: '2026-10-23', models_out: LITES });
  tick(env);
  minutes(env, 10);
  const c = JSON.parse(env.ctx.checkNow());
  assert.deepStrictEqual(c.journal, []);
  assert.ok(/«تراث» متوقف حتى يتجدد رصيد النماذج المجانية نحو 10:00 بتوقيتك \(نفد رصيد Gemini 3.5 Flash-Lite/.test(c.line), c.line);
  env.lockBusy = true;
  assert.strictEqual(JSON.parse(env.ctx.checkNow()).busy, true);
});

test('a book moved back from مشكلة leaves the problem list at once; its note goes and the journal says so', () => {
  const env = readyEnv();
  const f = addBook(env, 'معطوب.pdf', { ageMin: 40 });
  const other = addBook(env, 'جارٍ.pdf', { ageMin: 30 });
  tick(env);
  for (let i = 0; i < 2; i++) { minutes(env, 10); writeStatus(env, f.id, 'failed'); tick(env); }
  assert.strictEqual(jobs(env)[f.id].stage, 'problem');
  assert.strictEqual(jobs(env)[other.id].stage, 'running', 'the next book took the slot');
  assert.ok(names(env, 'problem').indexOf('معطوب - سبب المشكلة.txt') >= 0);
  assert.ok(status(env).recent.some((j) => j.fileId === f.id && j.stage === 'problem'));
  assert.ok(journal(env).some((e) => e.k === 'problem' && e.b === 'معطوب' && /5- مشكلة/.test(e.x)));
  f.moveTo(folder(env, 'inbox'));
  const s = status(env);
  assert.ok(!s.recent.some((j) => j.fileId === f.id), 'no problem on the page any more');
  assert.deepStrictEqual(s.queueNames, ['معطوب']);
  minutes(env, 10);
  tick(env);
  assert.strictEqual(jobs(env)[f.id], undefined);
  assert.deepStrictEqual(names(env, 'problem'), []);
  const e = journal(env).find((x) => x.k === 'revive');
  assert.ok(e && e.b === 'معطوب' && /إلى «1- جديد»/.test(e.x), JSON.stringify(e));
  // It starts afresh once the slot is free.
  writeStatus(env, other.id, 'done', { epub: 'ج.epub' });
  writeEpub(env, other.id, 'ج.epub');
  minutes(env, 10);
  tick(env);
  assert.strictEqual(jobs(env)[f.id].stage, 'running');
  assert.strictEqual(jobs(env)[f.id].attempts, 1);
});

test('live: the user\'s models\' own live files are read from the helper folder, by id after the first time', () => {
  const env = helperEnv();
  status(env);
  const dir = helperFolder(env);
  dir.createFile('live-agy.json', JSON.stringify({ v: 1, tool: 'agy', at: iso(env.clock),
    chips: { 'agy|gemini-3.8-flash': { state: 'working', page: 12, kind: 'settle' } } }), 'application/json');
  dir.createFile('live-other.json', '{}', 'application/json');
  let g = JSON.parse(env.ctx.getLive(JSON.stringify({ helper: true })));
  assert.deepStrictEqual(Object.keys(g.helper), ['agy']);
  assert.strictEqual(g.helper.agy.chips['agy|gemini-3.8-flash'].page, 12);
  const f = liveIn(env, dir).find((x) => x.name === 'live-agy.json');
  f.setContent(JSON.stringify({ v: 1, tool: 'agy', chips: { 'agy|gemini-3.8-flash': { state: 'idle' } } }));
  g = JSON.parse(env.ctx.getLive(JSON.stringify({ helper: true })));
  assert.strictEqual(g.helper.agy.chips['agy|gemini-3.8-flash'].state, 'idle');
  assert.strictEqual(JSON.parse(env.ctx.getLive('{}')).helper, undefined, 'only when asked');
});

/** The page's script in a minimal DOM (enough for render() and the live view). */
function liveSandbox(env) {
  const byId = {};
  const node = (tag) => ({
    tag, kids: [], className: '', style: {}, own: '', disabled: false, dir: '',
    set textContent(v) { this.own = String(v); this.kids = []; },
    get textContent() { return [this.own].concat(this.kids.map((k) => k.textContent)).join(' '); },
    appendChild(c) { this.kids.push(c); return c; },
    setAttribute() {},
    addEventListener(type, fn) { (this.on = this.on || {})[type] = fn; },
    click() { if (this.on && this.on.click) this.on.click.call(this); }
  });
  const calls = [];
  const chain = {};
  let ok = null;
  chain.withSuccessHandler = (fn) => { ok = fn; return chain; };
  chain.withFailureHandler = () => chain;
  ['getStatus', 'tick', 'getLive', 'checkNow', 'runNow', 'retryFree'].forEach((fn) => {
    chain[fn] = function () { calls.push({ fn, args: Array.from(arguments), ok }); };
  });
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
    setInterval: () => 0,
    setTimeout: () => 0,
    clearTimeout: () => {}
  };
  const html = env.ctx.doGet().getContent();
  vm.createContext(sandbox);
  vm.runInContext(/<script>([\s\S]*?)<\/script>/.exec(html)[1], sandbox);
  const find = (n, test) => test(n) ? n : (n.kids || []).reduce((hit, k) => hit || find(k, test), null);
  return { sandbox, byId, calls, find };
}

test('the page: the free models one by one, the run\'s events as they come, and «شغّل الآن» for a waiting book', () => {
  const env = readyEnv({ start: '2026-10-23T07:03:00Z' });
  const f = addBook(env, 'تراث.pdf');
  tick(env);
  minutes(env, 10);
  const file = env.drive.DriveApp.getFileById(liveId(lastDispatch(env)));
  const t = Math.floor(env.clock / 1000);
  const dispatched = JSON.parse(file.content.toString('utf8')).dispatched;
  file.setContent(JSON.stringify({ v: 1, seq: 12, run: '77', run_n: '43', file: f.id, book: 'تراث', dispatched,
    at: iso(env.clock - 2000), started: iso(env.clock - 500000), phase: 'read', info: { pages: 300 },
    stops: { read: iso(env.clock + 1800000), settle: iso(env.clock + 2500000) },
    pages: { total: 509, done: 40, unsure: 2, read2: 100, read1: 3, unread: 364, failed: 0, helper: 0, now: [233, 234] },
    models: {
      'gemini-3.5-flash-lite': { role: 'read1', queued: 2, ok: 120, err: 1, rl: 2, out: false, out_at: '', missing: false, limit: 500, rest: '',
        busy: [[233, 'a', t - 3]], last: [['ok', 232, 'b', 4.2, t - 6]] },
      'gemini-3.1-flash-lite': { role: 'read2', queued: 0, ok: 118, err: 0, rl: 0, out: false, out_at: '', missing: false, limit: 500,
        rest: iso(env.clock + 40000), busy: [], last: [] },
      'gemini-3.8-flash': { role: 'settle', queued: 0, ok: 20, err: 0, rl: 0, out: true, out_at: iso(env.clock - 60000), missing: false,
        limit: 20, rest: '', busy: [], last: [['out', 0, 'settle', null, t - 60]] }
    },
    events: [
      { i: 1, t: t - 60, m: 'gemini-3.8-flash', p: 0, k: 'settle', r: 'out', n: 20 },
      { i: 2, t: t - 30, m: 'gemini-3.1-flash-lite', p: 231, k: 'b', r: 'rest', s: 40 },
      { i: 3, t: t - 6, m: 'gemini-3.5-flash-lite', p: 232, k: 'b', r: 'ok', s: 4.2, n: 1800 },
      { i: 4, t: t - 5, m: '', p: 0, k: 'page', r: 'wait', n: 441 }
    ] }));
  const pg = liveSandbox(env);
  pg.sandbox.gotLive(JSON.parse(env.ctx.getLive('{}')));
  const firstBox = pg.byId.boxes.kids.filter((k) => k.className === 'bx')[0];
  assert.ok(/القراءة الأولى/.test(firstBox.textContent), firstBox.textContent);
  const working = firstBox.kids.find((k) => k.className === 'pair').kids[1].textContent;
  assert.ok(/Gemini 3\.5 Flash-Lite/.test(working) && /يعمل الآن/.test(working), working);
  // The board's cards are the free models' own; «النماذج المجانية» has the run's news.
  const free = pg.byId.boxes.textContent + pg.byId.free.textContent;
  assert.ok(/الآن:\s+ص 233 \(القراءة الأولى\)/.test(free), free);
  assert.ok(/في الانتظار:\s+طلبان/.test(free), free);
  assert.ok(/اليوم:\s+نحو 120 من 500 طلباً/.test(free), free);
  assert.ok(/Gemini 3\.1 Flash-Lite[\s\S]*يرتاح 40 ث/.test(free), free);
  assert.ok(/Gemini 3\.8 Flash[\s\S]*نفد رصيده اليوم/.test(free), free);
  assert.ok(/قرأ ص 232 \(القراءة الثانية\) في 4\.2 ث/.test(free), free);
  assert.ok(/طلبت Google التمهّل 40 ث قبل ص 231/.test(free), free);
  assert.ok(/441 صفحة تنتظر من يحسمها/.test(free), free);
  // During the run only a model found out of quota has a button: one with no page for it, or
  // waiting the seconds Google asked for, would gain nothing from it.
  const buttons = [];
  pg.find(pg.byId.boxes, (n) => { if (n.tag === 'button' && /المحاولة|شغّله/.test(n.textContent)) buttons.push(n.textContent); return false; });
  assert.ok(buttons.length > 0 && buttons.every((b) => /أعد المحاولة الآن/.test(b)), buttons.join(' | '));
  const now = pg.byId.now.textContent;
  assert.ok(/يقرأ الصفحات/.test(now) && /الآن: ص 233، 234/.test(now) && /التشغيل رقم 43/.test(now), now);
  assert.ok(/42 *من 509 صفحة أُنجزت/.test(now) && /قُرئت قراءتين وتنتظر الحسم *100/.test(now), now);
  assert.ok(/مباشر/.test(pg.byId['live-badge'].textContent), pg.byId['live-badge'].textContent);
  // The next poll brings only the new events, which join the feed.
  const asked = JSON.parse(env.ctx.getLive(JSON.stringify({ run: '77', ev: 4 })));
  assert.deepStrictEqual(asked.live.events, []);
  pg.sandbox.gotLive(asked);
  assert.ok(/441 صفحة تنتظر/.test(pg.byId.free.textContent));

  // The run ends: the quota ran out. The page says why, and offers «شغّل الآن».
  minutes(env, 60);
  writeStatus(env, f.id, 'paused', { quota_day: '2026-10-23', stages: { done: 40, unsure: 2, read2: 441, unread: 26 },
    pages_total: 509, models_out: LITES });
  tick(env);
  pg.sandbox.render(JSON.parse(env.ctx.getStatus()));
  const text = pg.byId.now.textContent;
  assert.ok(/متوقف: نفد رصيد Gemini 3\.5 Flash-Lite وGemini 3\.1 Flash-Lite لهذا اليوم/.test(text), text);
  assert.ok(/قُرئت قراءتين وتنتظر الحسم *441/.test(text), text);
  // Each free model in a box that is out today has «أعد المحاولة الآن».
  pg.sandbox.gotLive(JSON.parse(env.ctx.getLive('{}')));
  const again = pg.find(pg.byId.boxes, (n) => n.tag === 'button' && /أعد المحاولة الآن/.test(n.textContent));
  assert.ok(again, 'a free model\'s «أعد المحاولة الآن»');
  again.click();
  const asks = pg.calls.filter((c) => c.fn === 'retryFree').pop();
  assert.ok(LITES.concat('gemini-3.8-flash').indexOf(asks.args[0]) >= 0, asks.args[0]);
  const run = pg.find(pg.byId.now, (n) => n.tag === 'button' && /شغّل الآن/.test(n.textContent));
  assert.ok(run, 'a «شغّل الآن» button');
  run.click();
  const call = pg.calls.filter((c) => c.fn === 'runNow').pop();
  assert.deepStrictEqual(call.args, [f.id]);
  call.ok(env.ctx.runNow(f.id));
  assert.strictEqual(jobs(env)[f.id].stage, 'running');
  // The sidebar has «النماذج المجانية» right under «المساعد على جهازك».
  const nav = pg.byId['side-nav'].kids.map((k) => k.textContent.replace(/\s+/g, ' ').trim());
  assert.ok(/النماذج المجانية/.test(nav[nav.length - 1]), nav.join(' | '));
});

test('the page: a waiting book says what the user\'s own settling models could do now', () => {
  const env = helperEnv();
  env.clock = Date.parse('2026-10-23T09:00:00Z');
  status(env);
  writeHelperStatus(env, { models: [{ id: 'agy|gemini-3.8-flash', label: 'Gemini 3.8 Flash', tool: 'Antigravity' }] });
  setHelperBoxes(env, JSON.stringify({ transcribe: [], settle: ['agy|gemini-3.8-flash'], review: [] }));
  const f = addBook(env, 'تراث.pdf');
  tick(env);
  minutes(env, 60);
  writeStatus(env, f.id, 'paused', { quota_day: '2026-10-23', stages: { done: 54, read2: 441, unread: 14 }, models_out: LITES });
  tick(env);
  const pg = liveSandbox(env);
  const text = pg.byId.now.textContent;
  assert.ok(/نماذجك في مربع «الحسم» تستطيع حسم 441 صفحة قُرئت قراءتين الآن/.test(text), text);
  // And the run it starts gives them the pages: the settling box reaches the workflow.
  assert.strictEqual(JSON.parse(env.ctx.runNow(f.id)).started, true);
  assert.ok(/:settle$/.test(lastDispatch(env).body.inputs.helper), lastDispatch(env).body.inputs.helper);
});

const ACCOUNT = ['gemini-3.5-flash-lite', 'gemini-3.1-flash-lite', 'gemini-3.8-flash', 'gemini-3-flash-preview', 'gemma-4-31b-it',
  'gemma-4-26b-a4b-it'];

function freeInputOf(env) { return env.ctx.freeInput_(env.ctx.freeInfo_()); }

test('the board\'s free models: the converter\'s own until the user changes them, then sent with every run', () => {
  const env = readyEnv();
  let s = status(env);
  // Each Lite reader gives the readings the other cannot, as the converter does without boxes.
  assert.deepStrictEqual([s.boxes.read1, s.boxes.read2, s.boxes.review],
    [['gemini-3.5-flash-lite', 'gemini-3.1-flash-lite'], ['gemini-3.1-flash-lite', 'gemini-3.5-flash-lite'], []]);
  assert.ok(s.boxes.free.indexOf('gemma-4-31b-it') >= 0, 'Gemma can be picked before any run said so');
  const f = addBook(env, 'أول.pdf');
  tick(env);
  assert.ok(/^read1=gemini-3\.5-flash-lite,gemini-3\.1-flash-lite;/.test(lastDispatch(env).body.inputs.models),
    'the boxes go with every run, the converter\'s own too: only their models work');
  // The run says what the account has, and the converter's own boxes.
  minutes(env, 30);
  writeStatus(env, f.id, 'paused', { free_models: ACCOUNT, free_default: { read1: ['gemini-3.5-flash-lite'],
    read2: ['gemini-3.1-flash-lite'], settle: ['gemini-3.8-flash', 'gemini-3-flash-preview', 'gemini-3.5-flash-lite', 'gemini-3.1-flash-lite'] } });
  tick(env);
  s = status(env);
  assert.deepStrictEqual(s.boxes.free, ACCOUNT);
  assert.deepStrictEqual(s.boxes.settle, ['gemini-3.8-flash', 'gemini-3-flash-preview', 'gemini-3.5-flash-lite', 'gemini-3.1-flash-lite']);
  // An empty box is allowed (nobody does that step); only models' names go in.
  let r = JSON.parse(env.ctx.setBoxes(JSON.stringify({ read1: [], read2: ['gemini-3.1-flash-lite'], settle: [], review: [] })));
  assert.strictEqual(r.ok, true, r.error);
  assert.strictEqual(freeInputOf(env), 'read1=;read2=gemini-3.1-flash-lite;settle=;review=');
  r = JSON.parse(env.ctx.setBoxes(JSON.stringify({ read1: ['gemma-4-31b-it;rm -rf'], read2: ['a'], settle: ['b'] })));
  assert.strictEqual(r.ok, false);
  // Gemma reads first; a model may be in two boxes (each once).
  r = JSON.parse(env.ctx.setBoxes(JSON.stringify({ read1: ['gemma-4-31b-it', 'gemma-4-31b-it'],
    read2: ['gemini-3.5-flash-lite', 'gemini-3.1-flash-lite'], settle: ['gemini-3.8-flash', 'gemini-3.5-flash-lite'], review: ['gemini-3.8-flash'] })));
  assert.strictEqual(r.ok, true);
  assert.deepStrictEqual(r.boxes.read1, ['gemma-4-31b-it']);
  // A model came into a box: the book paused for its quota runs with it at once, in the form
  // the workflow lets through to the converter.
  assert.ok(/بدأ تشغيل بها الآن/.test(r.text), r.text);
  assert.strictEqual(jobs(env)[f.id].stage, 'running');
  const input = lastDispatch(env).body.inputs.models;
  assert.strictEqual(input, 'read1=gemma-4-31b-it;read2=gemini-3.5-flash-lite,gemini-3.1-flash-lite;' +
    'settle=gemini-3.8-flash,gemini-3.5-flash-lite;review=gemini-3.8-flash');
  const yml = fs.readFileSync(path.join(ROOT, '.github', 'workflows', 'convert.yml'), 'utf8');
  assert.ok(/^\s+models:$/m.test(yml), 'the workflow takes the input');
  const re = new RegExp(/BOXES_RE='([^']+)'/.exec(yml)[1]);
  assert.ok(re.test(input), input);
  assert.ok(!re.test('read1=a;rm -rf /') && !re.test('read1=$(id)'));
  assert.ok(re.test('read1=;read2=a;settle=b,c;review=;out=d'), 'empty boxes and the models out today pass');
});

test('the page: the free models go into any box with a tap or a drag, and out again, the last one too', () => {
  const env = readyEnv();
  const f = addBook(env, 'أول.pdf');
  tick(env);
  minutes(env, 30);
  writeStatus(env, f.id, 'paused', { free_models: ACCOUNT });
  tick(env);
  const page = pageSandbox(env, status(env));
  const sections = () => page.byId.boxes.kids.filter((k) => k.className === 'bx');
  const pair = (i) => sections()[i].kids.find((k) => k.className === 'pair').kids;
  const find = (node, pred) => { if (pred(node)) return node; for (const k of node.kids || []) { const x = find(k, pred); if (x) return x; } return null; };
  const buttonIn = (root, text) => find(root, (k) => k.tag === 'button' && k.textContent.indexOf(text) >= 0);
  const cardIn = (box, text) => box.kids.find((k) => /\bmrow\b/.test(k.className) && k.textContent.indexOf(text) >= 0);
  const saved = () => JSON.parse(page.pending('setBoxes')[0].args[0]);
  const answer = () => { const c = page.pending('setBoxes')[0]; page.answer(c, env.ctx.setBoxes(c.args[0])); };
  assert.ok(/Gemini 3\.5 Flash-Lite/.test(pair(0)[1].textContent));
  // Gemma 4 31B joins the first reading: a tap in «المتاحة» (opened with its button).
  page.click(buttonIn(pair(0)[0], 'أضف نموذجاً'));
  page.click(buttonIn(pair(0)[0], 'Gemma 4 31B'));
  assert.deepStrictEqual(saved().read1, ['gemini-3.5-flash-lite', 'gemini-3.1-flash-lite', 'gemma-4-31b-it']);
  assert.ok(cardIn(pair(0)[1], 'Gemma 4 31B'), 'shown in the box at once');
  answer();
  // No order between the models of a box: no «قدّمه».
  assert.ok(!buttonIn(pair(0)[1], 'قدّمه'));
  // «أخرجه» on each: the last one may go too (the user's rule), nobody gives first readings then.
  ['Gemini 3.5 Flash-Lite', 'Gemini 3.1 Flash-Lite', 'Gemma 4 31B'].forEach((name) => {
    page.click(buttonIn(cardIn(pair(0)[1], name), 'أخرجه'));
    answer();
  });
  assert.deepStrictEqual(JSON.parse(env.props.get('MODEL_BOXES')).read1, []);
  assert.ok(/لا أحد يعمل في «القراءة الأولى»/.test(pair(0)[1].textContent), pair(0)[1].textContent);
  assert.ok(/لا أحد يعمل فيه الآن/.test(page.byId.toasts.textContent), page.byId.toasts.textContent);
  // Gemma 4 26B dropped on the settling box joins it.
  pair(2)[1].handlers.drop[0]({ preventDefault() {}, dataTransfer: { getData: () => JSON.stringify({ id: 'gemma-4-26b-a4b-it' }) } });
  assert.ok(saved().settle.indexOf('gemma-4-26b-a4b-it') >= 0);
  // Saving fails: the box is as it was.
  page.fail(page.pending('setBoxes')[0], 'offline');
  assert.ok(!cardIn(pair(2)[1], 'Gemma 4 26B'));
  assert.ok(/تعذّر الحفظ/.test(page.byId.toasts.textContent));
  // There is no «خارج المربعات»: no model works outside the boxes.
  assert.ok(!/خارج المربعات/.test(page.byId.boxes.textContent + page.byId.free.textContent));
});

function controlOf(env) {
  return JSON.parse(env.drive.DriveApp.getFileById(env.props.get('LIVE_CONTROL_ID')).getBlob().getDataAsString());
}

test('«أعد المحاولة الآن» on a free model: its out mark goes, and a waiting book runs with it at once', () => {
  const env = readyEnv({ start: '2026-10-23T07:03:00Z' });
  const f = addBook(env, 'تراث.pdf');
  tick(env);
  minutes(env, 60);
  writeStatus(env, f.id, 'paused', { quota_day: '2026-10-23', models_out: LITES, requests: { 'gemini-3.5-flash-lite': 11 } });
  tick(env);
  // The runs are told which models are out today, so none spends minutes asking them again.
  assert.ok(/;out=gemini-3\.5-flash-lite,gemini-3\.1-flash-lite$/.test(freeInputOf(env)), freeInputOf(env));
  // A model in no box cannot be sent back to work.
  let r = JSON.parse(env.ctx.retryFree('gemma-4-26b-a4b-it'));
  assert.strictEqual(r.ok, false);
  assert.ok(/ضعه في «يعمل هنا»/.test(r.error), r.error);
  assert.strictEqual(JSON.parse(env.ctx.retryFree('rm -rf')).ok, false);
  // No run going: the book paused for the day runs now, and 3.5 Flash-Lite is not out in it.
  r = JSON.parse(env.ctx.retryFree('gemini-3.5-flash-lite'));
  assert.strictEqual(r.ok, true, r.error);
  assert.strictEqual(r.started, true);
  assert.ok(/بدأ تشغيل «تراث»، ويعمل فيه «Gemini 3\.5 Flash-Lite»/.test(r.text), r.text);
  assert.strictEqual(env.gh.dispatches.length, 2);
  assert.ok(/;out=gemini-3\.1-flash-lite$/.test(lastDispatch(env).body.inputs.models), lastDispatch(env).body.inputs.models);
  assert.strictEqual(jobs(env)[f.id].stage, 'running');
  assert.strictEqual(jobs(env)[f.id].pauses, 1, 'asked for by the user: no daily pause of its own');
  assert.ok(/طلبتَ أن يعود «Gemini 3\.5 Flash-Lite» إلى العمل/.test(journal(env).pop().x));
  assert.strictEqual(env.lockHeld, false);
  // A run going: it is told through the requests file, and nothing new starts.
  minutes(env, 2);
  r = JSON.parse(env.ctx.retryFree('gemini-3.1-flash-lite'));
  assert.ok(/في التشغيل الجاري/.test(r.text), r.text);
  assert.strictEqual(env.gh.dispatches.length, 2);
  assert.deepStrictEqual(controlOf(env), { v: 1, retry: { 'gemini-3.1-flash-lite': iso(env.clock) } });
  assert.strictEqual(freeInputOf(env).indexOf('out='), -1, 'neither is out any more');
  // A later run that asked a model without it running out: whatever was said before, it works.
  minutes(env, 30);
  writeStatus(env, f.id, 'paused', { quota_day: '2026-10-23', models_out: ['gemini-3.1-flash-lite'],
    requests: { 'gemini-3.5-flash-lite': 40, 'gemini-3.1-flash-lite': 3 } });
  tick(env);
  const u = status(env).usage.m;
  assert.strictEqual(u['gemini-3.5-flash-lite'].out, false);
  assert.strictEqual(u['gemini-3.1-flash-lite'].out, true);
});

test('empty boxes: nobody works, no run starts for a scanned book, and a book waiting for a model runs once there is one', () => {
  let env = readyEnv({ start: '2026-10-23T07:03:00Z' });
  const none = JSON.stringify({ read1: [], read2: [], settle: [] });
  assert.strictEqual(JSON.parse(setFreeBoxes(env, none)).ok, true);
  // A text file needs no model.
  addBook(env, 'نص.txt');
  tick(env);
  assert.strictEqual(env.gh.dispatches.length, 1);
  env = readyEnv({ start: '2026-10-23T07:03:00Z' });
  setFreeBoxes(env, none);
  const g = addBook(env, 'ممسوح.pdf');
  tick(env);
  assert.strictEqual(env.gh.dispatches.length, 0, 'no run for nothing');
  assert.deepStrictEqual(names(env, 'inbox'), ['ممسوح.pdf']);
  // One model in one box: it starts. Its waiting pages no model in any box can take further:
  // it waits for one, with no daily pause counted, and runs as soon as the boxes change.
  setFreeBoxes(env, JSON.stringify({ read1: ['gemini-3.5-flash-lite'], read2: [], settle: [] }));
  tick(env);
  assert.strictEqual(env.gh.dispatches.length, 1);
  minutes(env, 20);
  writeStatus(env, g.id, 'paused', { waiting_for: 'boxes', stages: { read1: 10 } });
  tick(env);
  const j = jobs(env)[g.id];
  assert.strictEqual(j.stage, 'boxWait');
  assert.ok(!j.pauses);
  assert.ok(/لا نموذج في المربعات يكمل صفحاته/.test(journal(env).pop().x));
  minutes(env, 60 * 30);
  tick(env);
  assert.strictEqual(env.gh.dispatches.length, 1, 'waits for the user, not for a new day');
  assert.strictEqual(status(env).parked[0].stage, 'boxWait');
  setFreeBoxes(env, JSON.stringify({ read1: ['gemini-3.5-flash-lite'], read2: ['gemini-3.1-flash-lite'], settle: ['gemini-3.8-flash'] }));
  tick(env);
  assert.strictEqual(env.gh.dispatches.length, 2);
  assert.strictEqual(jobs(env)[g.id].stage, 'running');
  assert.ok(/تغيّرت المربعات/.test(journal(env).pop().x));
});

test('a paused book does not run on its new day when nobody can work', () => {
  const env = readyEnv({ start: '2026-10-23T07:03:00Z' });
  const f = addBook(env, 'تراث.pdf');
  tick(env);
  minutes(env, 60);
  writeStatus(env, f.id, 'paused', { quota_day: '2026-10-23', models_out: LITES });
  tick(env);
  setFreeBoxes(env, JSON.stringify({ read1: [], read2: [], settle: [] }));
  env.clock = Date.parse('2026-10-24T07:20:00Z');
  tick(env);
  assert.strictEqual(env.gh.dispatches.length, 1);
  setFreeBoxes(env, JSON.stringify({ read1: ['gemini-3.5-flash-lite'], read2: ['gemini-3.1-flash-lite'], settle: ['gemini-3.8-flash'] }));
  tick(env);
  assert.strictEqual(env.gh.dispatches.length, 2);
});

test('a model put in a box: the run going takes it within seconds; with none going, the book waiting runs at once', () => {
  const env = readyEnv({ start: '2026-10-23T07:03:00Z' });
  const set = (b) => JSON.parse(env.ctx.setBoxes(JSON.stringify(Object.assign({ review: [] }, b))));
  set({ read1: ['gemini-3.5-flash-lite'], read2: ['gemini-3.1-flash-lite'], settle: ['gemini-3.8-flash'] });
  const f = addBook(env, 'تراث.pdf');
  tick(env);
  assert.strictEqual(env.gh.dispatches.length, 1);
  // During the run: it is told through the requests file, and nothing else starts.
  minutes(env, 5);
  let r = set({ read1: ['gemini-3.5-flash-lite', 'gemma-4-31b-it'], read2: ['gemini-3.1-flash-lite'], settle: ['gemini-3.8-flash'] });
  assert.ok(/يأخذها التشغيل الجاري خلال ثوانٍ/.test(r.text), r.text);
  const told = controlOf(env);
  assert.deepStrictEqual(told.boxes, { read1: ['gemini-3.5-flash-lite', 'gemma-4-31b-it'], read2: ['gemini-3.1-flash-lite'],
    settle: ['gemini-3.8-flash'] });
  assert.deepStrictEqual(told.helper, { read1: false, read2: false, settle: false });
  assert.strictEqual(told.boxes_at, iso(env.clock));
  assert.strictEqual(env.gh.dispatches.length, 1);
  // The run took them, then the free quota ran out: the book waits for its new day.
  minutes(env, 20);
  writeStatus(env, f.id, 'paused', { quota_day: '2026-10-23', models_out: LITES, boxes_at: told.boxes_at });
  tick(env);
  assert.strictEqual(jobs(env)[f.id].pausedDay, '2026-10-23');
  assert.strictEqual(env.gh.dispatches.length, 1);
  // A model taken out starts nothing; one put in starts the book at once.
  r = set({ read1: ['gemini-3.5-flash-lite'], read2: ['gemini-3.1-flash-lite'], settle: ['gemini-3.8-flash'] });
  assert.strictEqual(r.text, '');
  assert.strictEqual(env.gh.dispatches.length, 1);
  r = set({ read1: ['gemini-3.5-flash-lite'], read2: ['gemini-3.1-flash-lite', 'gemma-4-31b-it'], settle: ['gemini-3.8-flash'] });
  assert.ok(/بدأ تشغيل بها الآن/.test(r.text), r.text);
  assert.strictEqual(env.gh.dispatches.length, 2);
  assert.ok(/وضعتَ نموذجاً في المربعات/.test(journal(env).pop().x));
  // The run had ended when the user put another model in, before the check read its result:
  // the book goes on at once, not tomorrow.
  minutes(env, 10);
  set({ read1: ['gemini-3.5-flash-lite', 'gemma-4-26b-a4b-it'], read2: ['gemini-3.1-flash-lite', 'gemma-4-31b-it'],
    settle: ['gemini-3.8-flash'] });
  assert.strictEqual(env.gh.dispatches.length, 2);
  writeStatus(env, f.id, 'paused', { quota_day: '2026-10-23', models_out: LITES });
  tick(env);
  assert.strictEqual(env.gh.dispatches.length, 3);
  assert.strictEqual(jobs(env)[f.id].stage, 'running');
  assert.ok(/read1=gemini-3\.5-flash-lite,gemma-4-26b-a4b-it;/.test(lastDispatch(env).body.inputs.models));
  // A book parked for a reader runs again once a model comes into a box after its last run.
  minutes(env, 10);
  writeStatus(env, f.id, 'paused', { waiting_for: 'reader', quota_day: '2026-10-23', pages_waiting: [3, 4] });
  tick(env);
  assert.strictEqual(jobs(env)[f.id].stage, 'readerWait');
  const n = env.gh.dispatches.length;
  set({ read1: ['gemini-3.5-flash-lite', 'gemma-4-26b-a4b-it'], read2: ['gemini-3.1-flash-lite', 'gemma-4-31b-it'],
    settle: ['gemini-3.8-flash', 'gemma-4-31b-it'] });
  assert.strictEqual(env.gh.dispatches.length, n + 1);
  assert.strictEqual(jobs(env)[f.id].stage, 'running');
});

test('a free model the key cannot use is offered no more; one in a box stays there until taken out', () => {
  const env = readyEnv({ start: '2026-10-23T07:03:00Z' });
  const all = ACCOUNT.concat('gemini-2.5-flash');
  const f = addBook(env, 'أول.pdf');
  tick(env);
  minutes(env, 30);
  writeStatus(env, f.id, 'paused', { quota_day: '2026-10-23', free_models: all, models_out: LITES });
  tick(env);
  assert.ok(status(env).boxes.free.indexOf('gemini-2.5-flash') >= 0);
  // The user puts it in a box: a run starts with it, and finds Google no longer serves it.
  env.ctx.setBoxes(JSON.stringify({ read1: ['gemini-2.5-flash', 'gemma-4-31b-it'], read2: [], settle: [], review: [] }));
  assert.strictEqual(env.gh.dispatches.length, 2);
  minutes(env, 30);
  writeStatus(env, f.id, 'paused', { quota_day: '2026-10-23', free_models: ACCOUNT, models_missing: ['gemini-2.5-flash'],
    models_out: LITES.concat('gemma-4-31b-it') });
  tick(env);
  assert.ok(status(env).boxes.read1.indexOf('gemini-2.5-flash') >= 0, 'still in its box, to take out');
  env.ctx.setBoxes(JSON.stringify({ read1: ['gemma-4-31b-it'], read2: [], settle: [], review: [] }));
  assert.strictEqual(status(env).boxes.free.indexOf('gemini-2.5-flash'), -1);
  // A later run listing it again (the account's list still names it) does not bring it back.
  minutes(env, 60 * 24);
  tick(env);
  minutes(env, 30);
  writeStatus(env, f.id, 'paused', { quota_day: '2026-10-24', free_models: all, models_out: LITES });
  tick(env);
  assert.strictEqual(status(env).boxes.free.indexOf('gemini-2.5-flash'), -1);
  assert.ok(status(env).boxes.free.indexOf('gemma-4-26b-a4b-it') >= 0);
});

test('the page: the other books of the run under «الآن» with their pages, the queue in the waiting list', () => {
  const env = readyEnv({ start: '2026-10-23T07:03:00Z' });
  const a = addBook(env, 'أول.pdf');
  tick(env);
  minutes(env, 30);
  writeStatus(env, a.id, 'paused', { waiting_for: 'time', stages: { done: 400, read2: 100 }, pages_total: 500 });
  const b = addBook(env, 'ثان.pdf', { ageMin: 12 });
  const c = addBook(env, 'ثالث.docx');
  tick(env);
  const d = lastDispatch(env).body.inputs;
  assert.deepStrictEqual(JSON.parse(d.queue).books.map((x) => x.id), [b.id]);
  // The live file of a run of two books.
  minutes(env, 5);
  const file = env.drive.DriveApp.getFileById(liveId(lastDispatch(env)));
  const t = Math.floor(env.clock / 1000);
  const dispatched = JSON.parse(file.content.toString('utf8')).dispatched;
  file.setContent(JSON.stringify({ v: 1, seq: 3, file: a.id, book: 'أول', dispatched, at: iso(env.clock - 1000),
    started: iso(env.clock - 60000), phase: 'read', info: {},
    pages: { total: 520, done: 400, unsure: 0, read2: 101, read1: 1, unread: 18, failed: 0, helper: 0, now: [3] },
    books: [{ file: '', book: 'أول', pages: { total: 500, done: 400, unsure: 0, read2: 100, read1: 0, unread: 0, failed: 0, helper: 0, now: [] } },
      { file: '', book: 'ثان', pages: { total: 20, done: 0, unsure: 0, read2: 1, read1: 1, unread: 18, failed: 0, helper: 0, now: [3] } }],
    models: { 'gemini-3.5-flash-lite': { role: 'read1', queued: 0, ok: 2, err: 0, rl: 0, out: false, out_at: '', missing: false, limit: 500,
      rest: '', busy: [[3, 'a', t - 20, 1]], last: [] } },
    events: [{ i: 1, t: t - 30, m: 'gemini-3.5-flash-lite', p: 2, k: 'a', r: 'ok', s: 50, b: 1 }] }));
  const pg = liveSandbox(env);
  pg.sandbox.gotLive(JSON.parse(env.ctx.getLive('{}')));
  const now = pg.byId.now.textContent;
  assert.ok(/«أول»/.test(now) && /ومعه في هذا التشغيل/.test(now) && /«ثان»/.test(now) && /الآن: ص 3/.test(now), now);
  assert.ok(/400 *من 500 صفحة أُنجزت/.test(now), 'the first book\'s own pages: ' + now);
  const free = pg.byId.boxes.textContent + pg.byId.free.textContent;
  assert.ok(/ص 3 من «ثان»/.test(free) && /قرأ ص 2 من «ثان»/.test(free), free);
  // c (not a PDF) waits in «1- جديد» for a run of its own.
  assert.ok(names(env, 'inbox').indexOf('ثالث.docx') >= 0);
  // The run is over; b waits in the queue while a holds the turn.
  minutes(env, 40);
  writeStatus(env, a.id, 'paused', { quota_day: '2026-10-23', stages: { done: 450, read2: 50 }, models_out: LITES,
    requests: { 'gemini-3.5-flash-lite': 400 } });
  writeStatus(env, b.id, 'paused', { quota_day: '2026-10-23', stages: { read2: 2, unread: 18 } });
  tick(env);
  const s = status(env);
  assert.deepStrictEqual(s.parked.map((p) => [p.title, p.stage]), [['ثان', 'waiting']]);
  const page = pageSandbox(env, s);
  assert.ok(/«ثان»[\s\S]*في الطابور/.test(page.byId.queue.textContent), page.byId.queue.textContent);
  assert.strictEqual(c.id && jobs(env)[c.id], undefined, 'not taken while a holds the turn');
});

test('each box its own way: the first readers read another book while the oldest waits for its second readers', () => {
  // The user (2026-10-25): every Flash-Lite and Flash out, Gemma in the first reading box, six
  // new books in the queue, and no run started: the oldest book held the turn till the next day.
  const env = readyEnv({ start: '2026-10-23T07:03:00Z' });
  status(env);
  assert.strictEqual(JSON.parse(env.ctx.setBoxes(JSON.stringify({ read1: ['gemma-4-31b-it'], read2: LITES,
    settle: [], review: [] }))).ok, true);
  const a = addBook(env, 'أول.pdf', { ageMin: 30 });
  tick(env);
  minutes(env, 40);
  // Its pages wait for the second readers only (Gemma read them all), who are out today.
  writeStatus(env, a.id, 'paused', { quota_day: '2026-10-23', models_out: LITES, requests: { 'gemini-3.5-flash-lite': 500 },
    stages: { read1: 300 }, needs: { read1: {}, read2: { [LITES.slice().sort().join(',')]: 300 }, settle: {} } });
  tick(env);
  assert.ok(jobs(env)[a.id].pausedDay, 'nothing to do in it today');
  const dispatched = env.gh.dispatches.length;
  minutes(env, 10);
  tick(env);
  assert.strictEqual(env.gh.dispatches.length, dispatched, 'no run for nobody');
  // A new book: Gemma has pages to read first in it, so a run starts with both.
  const b = addBook(env, 'ثان.pdf');
  minutes(env, 10);
  tick(env);
  assert.strictEqual(env.gh.dispatches.length, dispatched + 1, 'the first readers do not wait for the second');
  const d = lastDispatch(env).body.inputs;
  assert.deepStrictEqual([d.drive_file_id, JSON.parse(d.queue).books.map((x) => x.id)], [a.id, [b.id]]);
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
// ONLY=<part of a test name> runs those tests alone.
for (const t of tests.filter((x) => !process.env.ONLY || x.name.indexOf(process.env.ONLY) >= 0)) {
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
