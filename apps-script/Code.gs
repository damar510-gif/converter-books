/**
 * تحويل الكتب: a Google Drive inbox for the converter-books GitHub workflow.
 *
 * Contract with the workflow: docs/drive-inbox.md in the converter-books repository.
 *
 *   1. The user drops a book into "تحويل الكتب/1- جديد" (phone or PC).
 *   2. tick() runs every 10 minutes. It moves the oldest book to "2- قيد التحويل" and
 *      starts convert.yml with a short-lived Drive token (about one hour), so GitHub
 *      never holds a long-lived Google credential.
 *   3. The workflow writes <result_dir>/status.json, the EPUB and report.md to the
 *      "results" branch.
 *   4. tick() reads status.json, files the EPUB in "3- جاهز" and the original in
 *      "4- الأصل" (or the original in "5- مشكلة" with a note) and emails the user.
 *   5. On the status page the user checks the places still unsure (review/items.json in
 *      the results branch) and sends their answers; a "review round" applies them to
 *      the saved pages without any model request and replaces the EPUB.
 *
 * One book at a time. All state lives in Script Properties; the job list is the JSON
 * property JOBS, keyed by Drive file id.
 *
 * User-visible text is Arabic. Logger lines are English diagnostics.
 */

var CONFIG = {
  ROOT_NAME: 'تحويل الكتب',
  FOLDERS: {
    inbox: '1- جديد',
    working: '2- قيد التحويل',
    ready: '3- جاهز',
    originals: '4- الأصل',
    problem: '5- مشكلة',
    // Books whose improvement rounds are over: they will not change any more.
    final: '6- مكتمل'
  },
  DEFAULT_REPO: 'damar510-gif/converter-books',
  WORKFLOW: 'convert.yml',
  RESULTS_BRANCH: 'results',
  TICK_MINUTES: 10,
  // A file changed in the last 2 minutes may still be uploading from the phone.
  SETTLE_MS: 2 * 60 * 1000,
  // A dispatch with no run and no status after this long was lost.
  LOST_AFTER_MS: 20 * 60 * 1000,
  // Tolerance between Google's clock and GitHub's when matching a run to a dispatch.
  RUN_SKEW_MS: 2 * 60 * 1000,
  GIVE_UP_MS: 8 * 60 * 60 * 1000,
  MAX_INCOMPLETE_ATTEMPTS: 3,
  MAX_FAILED_ATTEMPTS: 2,
  MAX_LOST_ATTEMPTS: 3,
  // A book that is still "paused" after this many daily restarts is reported as a problem.
  MAX_PAUSES: 30,
  MAX_REFINES: 15,
  MAX_FINISHED_JOBS: 50,
  // Script Properties allow about 9 KB per value. Long Arabic names take 2 bytes per
  // letter, so old finished jobs are dropped until JOBS fits in this many bytes.
  JOBS_MAX_BYTES: 8000,
  MAX_SCAN: 500,
  // Gemini's free daily quota resets at midnight Pacific time, not Riyadh time.
  QUOTA_TZ: 'America/Los_Angeles',
  REPORT_EMAIL_CHARS: 1500,
  // Review by hand: answers live in this Drive folder, one file per book.
  REVIEW_FOLDER: 'بيانات المراجعة',
  REVIEW_MAX_TRIES: 2,
  REVIEW_TITLE_CHARS: 60,
  REVIEW_ANSWER_CHARS: 2000,
  REVIEW_PAGE_CHARS: 20000,
  REVIEW_REST_IDS: 25,
  REVIEW_MAX_ANSWERS: 300,
  // The helper on the user's computer (helper/ in the repository) works through this
  // Drive folder, which Google Drive for desktop keeps in sync there: see docs/helper.md.
  // No token or key ever leaves this script.
  HELPER_FOLDER: 'بيانات المساعد',
  HELPER_PROGRAM: 'البرنامج',
  HELPER_TEST_PAGES: 'صفحات الاختبار',
  HELPER_REQUESTS: 'requests.json',
  HELPER_INSTALL: 'install.ps1',
  HELPER_STATUS: 'helper-status.json',
  // The helper writes its status every minute or so while the computer is on.
  HELPER_STALE_MS: 15 * 60 * 1000,
  HELPER_MAX_REQUESTS: 10,
  // Every check (a helper fix reaches the computer within about ten minutes).
  HELPER_SYNC_MS: 9 * 60 * 1000,
  // The models the user puts to work from the page (docs/helper-boxes-plan.md), and the
  // folders where the helper and the runs exchange files.
  HELPER_MAX_CHIPS: 12,
  HELPER_BOOKS: 'كتب',
  HELPER_REVIEWS: 'مراجعة',
  HELPER_VERDICTS: 'أحكام',
  // A book waiting for the helper goes on with the free model when the computer has not
  // been seen for this long, or when the helper has not moved for this long.
  HELPER_GONE_MS: 60 * 60 * 1000,
  HELPER_STALL_MS: 6 * 60 * 60 * 1000,
  // "Finished" must be seen by two checks this far apart, so its last files reached Drive.
  HELPER_DONE_WAIT_MS: 8 * 60 * 1000,
  // Review files copied for the helper in one check.
  HELPER_REVIEW_FILES: 60
};

var SUPPORTED_EXT = { pdf: true, docx: true, txt: true };
var MIME_EXT = {
  'application/pdf': 'pdf',
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document': 'docx',
  'text/plain': 'txt'
};
var GH_RAW = 'application/vnd.github.raw';
// One Script Property per book under review (Arabic titles take 2 bytes a letter, so a
// single property for all books would overflow): RV_<fileId>, see loadRv_.
var REVIEW_PREFIX = 'RV_';
var FILE_ID_RE = /^[A-Za-z0-9_-]{10,100}$/;
var IMAGE_RE = /^p\d{4}-[0-9a-f]{10}\.jpg$/;
var PLACE_ID_RE = /^p(\d{1,5})-[0-9a-f]{10}$/;
var REST_ID_RE = /^p(\d{1,5})-rest-[0-9a-z]{1,20}$/;
// A model on the user's computer: "<tool>|<model>" (helper/helper.ps1, $Tools).
var CHIP_RE = /^(grok|agy|codex|claude)\|[A-Za-z0-9._-]{1,60}$/;
var INCOMPLETE_SUFFIX = ' (فيه صفحات ناقصة)';

// Set by gh_() from the GitHub-Authentication-Token-Expiration response header.
var SEEN_TOKEN_EXPIRY = '';

// ============================================================================
// Public entry points (functions without a trailing underscore can be run from the
// editor, from a trigger or from the web page).
// ============================================================================

/**
 * Run once from the editor after adding GITHUB_TOKEN. Safe to run again: it reuses
 * the folders and replaces the trigger.
 */
function setup() {
  var props = props_();
  var cfg = config_();
  if (!cfg.token) {
    throw new Error('لم أجد رمز GitHub. افتح «إعدادات المشروع» (⚙) ثم «Script properties» ' +
      'وأضف خاصية اسمها GITHUB_TOKEN وقيمتها الرمز، كما في الخطوة 3 من دليل الإعداد. ' +
      'ثم شغّل setup مرة أخرى.');
  }

  // 1) GitHub first: a wrong token should stop setup with a clear message.
  var repoResp = gh_(cfg, 'get', '/repos/' + cfg.repo);
  if (repoResp.code === 401) {
    throw new Error('رمز GitHub غير صحيح أو انتهت صلاحيته. أنشئ رمزاً جديداً كما في الخطوة 1 ' +
      'من دليل الإعداد، وضعه مكان قيمة GITHUB_TOKEN (الخطوة 3)، ثم شغّل setup مرة أخرى.');
  }
  if (repoResp.code === 403 || repoResp.code === 404) {
    throw new Error('رمز GitHub لا يصل إلى المستودع ' + cfg.repo + '. تأكد في الخطوة 1 أنك اخترت ' +
      '«Only select repositories» ثم converter-books، ثم شغّل setup مرة أخرى.');
  }
  if (repoResp.code !== 200) {
    throw new Error('تعذّر الاتصال بـ GitHub الآن (' + describeResponse_(repoResp) + '). جرّب بعد دقائق.');
  }
  var repoInfo = parseJson_(ghText_(repoResp)) || {};
  var defaultRef = repoInfo.default_branch || 'main';
  props.setProperty('DEFAULT_REF', defaultRef);
  cfg = config_();

  // Reading the workflow needs the Actions permission; dispatching needs Read and write.
  var wfResp = gh_(cfg, 'get', '/repos/' + cfg.repo + '/actions/workflows/' + CONFIG.WORKFLOW);
  var workflowOk = wfResp.code === 200;

  // 2) Folders.
  var folders = ensureFolders_();

  // The workflow downloads each book through the Drive REST API with this script's
  // token. That API must be on for the script's Google Cloud project; check it now
  // rather than have every book fail later.
  var driveApi = checkDriveApi_(folders.root.getId());
  if (driveApi === 'disabled') {
    throw new Error('خدمة Drive API غير مفعّلة لهذا المشروع، وGitHub يحتاجها لتنزيل الكتاب. ' +
      'في المحرر اضغط «+» بجانب «Services» (الخدمات)، واختر «Drive API» ثم «Add»، ثم شغّل setup مرة أخرى.');
  }

  // 3) Exactly one trigger.
  ScriptApp.getProjectTriggers().forEach(function (t) { ScriptApp.deleteTrigger(t); });
  ScriptApp.newTrigger('tick').timeBased().everyMinutes(CONFIG.TICK_MINUTES).create();

  // A fresh setup usually follows a token fix, so old "once a day" warnings may repeat.
  props.getKeys().forEach(function (k) {
    if (k.indexOf('NOTICE_') === 0) props.deleteProperty(k);
  });
  rememberExpiry_();

  var expiry = props.getProperty('TOKEN_EXPIRES') || '';
  var lines = [
    'تم إعداد «تحويل الكتب» بنجاح.',
    'المجلد: ' + CONFIG.ROOT_NAME + ' — ' + folders.root.getUrl(),
    'ضع الكتب (PDF أو DOCX أو TXT) في المجلد «' + CONFIG.FOLDERS.inbox + '».',
    'يُفحص المجلد تلقائياً كل ' + CONFIG.TICK_MINUTES + ' دقائق، وتصلك رسالة عند جاهزية كل كتاب.',
    'المستودع: ' + cfg.repo + ' — الفرع: ' + cfg.ref
  ];
  if (expiry) lines.push('صلاحية رمز GitHub حتى: ' + formatLocal_(new Date(Date.parse(expiry))));
  if (!workflowOk) {
    lines.push('تنبيه: لم أستطع قراءة سير العمل ' + CONFIG.WORKFLOW + ' (' + describeResponse_(wfResp) +
      '). تأكد أن صلاحية Actions في الرمز هي «Read and write» (الخطوة 1).');
  }
  if (driveApi !== 'ok') {
    lines.push('تنبيه: لم أستطع التأكد من عمل Drive API الآن. إذا فشل أول كتاب فراجع الخطوة 7 في الدليل.');
  }
  var summary = lines.join('\n');
  Logger.log(summary);
  sendMail_('تحويل الكتب: تم الإعداد', lines);
  return summary;
}

/**
 * The periodic check. Called by the time trigger and by the "افحص الآن" button.
 * Returns a short English summary (shown nowhere to the user; useful in the log).
 */
function tick() {
  var lock = LockService.getScriptLock();
  if (!lock.tryLock(1000)) {
    Logger.log('tick: another check is running, skipping');
    return 'busy';
  }
  var result;
  try {
    try {
      result = runTick_();
    } catch (err) {
      Logger.log('tick crashed: ' + (err && err.stack ? err.stack : err));
      notifyOnce_('crash', 'تحويل الكتب: حدث خطأ غير متوقع', [
        'حدث خطأ أثناء الفحص الدوري، وسأعيد المحاولة تلقائياً بعد ' + CONFIG.TICK_MINUTES + ' دقائق.',
        'الكتب في أماكنها ولن تضيع.',
        'إذا وصلتك هذه الرسالة عدة أيام متتالية فأرسل النص التالي لمن يساعدك:',
        String(err && err.message ? err.message : err)
      ]);
      result = 'error: ' + (err && err.message ? err.message : err);
    }
    try {
      helperTick_();
    } catch (err) {
      Logger.log('helper bridge failed: ' + (err && err.stack ? err.stack : err));
    }
    try {
      props_().setProperties({ LAST_TICK: now_().toISOString(), LAST_RESULT: String(result).slice(0, 300) });
    } catch (e) {
      Logger.log('could not save LAST_TICK: ' + e);
    }
  } finally {
    lock.releaseLock();
  }
  Logger.log('tick: ' + result);
  return result;
}

/**
 * Run by hand from the editor (choose judgeNow, then Run): the judges see the unsure
 * pages today instead of waiting for the next day, for delivered books and parked
 * ones alike. Useful when some of today's judge quota is still unused.
 */
function judgeNow() {
  var lock = LockService.getScriptLock();
  if (!lock.tryLock(30000)) {
    Logger.log('مشغول بفحص آخر، أعد المحاولة بعد دقيقة.');
    return 'busy';
  }
  var count = 0;
  try {
    var jobs = loadJobs_();
    var ctx = { cfg: config_(), folders: getFolders_(), jobs: jobs, dispatched: 0 };
    Object.keys(jobs).forEach(function (id) {
      var j = jobs[id];
      if (!j) return;
      if (j.stage === 'delivered' && !j.refineDone && j.unsure === undefined) backfill_(ctx, j);
      if (j.stage === 'judging') {
        j.judgeDay = '';
        count++;
      } else if (j.stage === 'delivered' && !j.refineDone && j.unsure > 0) {
        j.refineDay = '';
        count++;
      }
    });
    saveJobs_(jobs);
  } finally {
    lock.releaseLock();
  }
  if (!count) {
    Logger.log('لا يوجد كتاب فيه صفحات مشكوك فيها تنتظر الحكّام.');
    return 'nothing to judge';
  }
  var result = tick();
  Logger.log(/(judging|refining) [\w-]+: dispatched/.test(result)
    ? 'بدأت مراجعة الحكّام الآن. تصلك رسالة حين تنتهي الجولة.'
    : 'لم تبدأ الآن: يوجد كتاب قيد التحويل أو كتاب جديد، فتبدأ بعده تلقائياً. (' + result + ')');
  return result;
}

/** Status for the web page, as a JSON string (google.script.run cannot return Dates). */
function getStatus() {
  try {
    var props = props_();
    var cfg = config_();
    var now = now_();
    var jobs = loadJobs_();
    var out = {
      ok: true,
      now: now.toISOString(),
      setUp: !!props.getProperty('FOLDER_ROOT'),
      hasToken: !!cfg.token,
      repo: cfg.repo,
      lastTick: props.getProperty('LAST_TICK') || '',
      tokenExpires: props.getProperty('TOKEN_EXPIRES') || '',
      active: null,
      queued: 0,
      uploading: 0,
      unsupported: [],
      recent: [],
      links: {}
    };

    var a = activeJob_(jobs);
    if (a) {
      var since = a.dispatchedAt || a.createdAt || '';
      out.active = {
        title: a.title,
        fileId: a.fileId,
        stage: a.stage,
        attempts: a.attempts || 0,
        since: since,
        minutes: since ? Math.max(0, Math.floor((now.getTime() - Date.parse(since)) / 60000)) : 0,
        paused: !!a.pausedDay,
        pausedAt: a.pausedAt || '',
        resumeAt: a.pausedDay ? pacificMidnightAfter_(a.pausedDay).toISOString() : '',
        pagesDone: a.pagesDone || 0,
        pagesTotal: a.pagesTotal || 0,
        lastError: a.lastError || ''
      };
    }

    var folders = out.setUp ? existingFolders_() : null;
    if (folders) {
      var scan = scanInbox_(folders.inbox);
      out.queued = scan.ready.length + scan.uploading;
      // The next books, in the order they start.
      out.queueNames = scan.ready.slice(0, 8).map(function (f) { return newJob_(f).title; });
      out.uploading = scan.uploading;
      out.unsupported = scan.unsupported.slice(0, 10);
      out.links = {
        root: folders.root.getUrl(),
        inbox: folders.inbox.getUrl(),
        ready: folders.ready.getUrl(),
        problem: folders.problem.getUrl()
      };
    }

    out.reviews = reviewSummaries_(jobs);
    if (out.setUp && cfg.token) out.helper = helperInfo_(now);

    // Books waiting in "2- قيد التحويل" for the judges or the helper's models.
    var hs = out.helper && out.helper.progress ? out.helper.progress : {};
    out.parked = Object.keys(jobs).map(function (id) { return jobs[id]; })
      .filter(function (j) { return j && isParked_(j); })
      .map(function (j) {
        var p = hs[j.fileId] || {};
        return { title: j.title, stage: j.stage, need: Number(p.need) || 0, finished: Number(p.finished) || 0 };
      });

    // The last ten, leaving out the books the user deleted.
    var shown = [];
    finishedJobs_(jobs).forEach(function (j) {
      if (shown.length < 10 && !isParked_(j) && !bookGone_(j.fileId, j.epubId)) shown.push(j);
    });
    out.recent = shown.map(function (j) {
      return {
        title: j.title,
        stage: j.stage,
        fileId: j.fileId,
        unsure: Number(j.unsure) || 0,
        final: !!j.final,
        finishedAt: j.finishedAt || '',
        incomplete: j.lastState === 'incomplete',
        url: jobUrl_(j),
        reason: j.stage === 'problem' ? (j.lastError || '') : ''
      };
    });
    return JSON.stringify(out);
  } catch (err) {
    Logger.log('getStatus failed: ' + (err && err.stack ? err.stack : err));
    return JSON.stringify({ ok: false, error: String(err && err.message ? err.message : err) });
  }
}

/** The status web page; ?review=<fileId> opens that book's review. */
function doGet(e) {
  rememberPageUrl_();
  var template = HtmlService.createTemplateFromFile('Page');
  var initial = parseJson_(getStatus()) || { ok: false, error: 'status' };
  var asked = e && e.parameter ? String(e.parameter.review || '') : '';
  initial.openReview = FILE_ID_RE.test(asked) ? asked : '';
  template.initialJson = jsonForScript_(JSON.stringify(initial));
  return template.evaluate()
    .setTitle('تحويل الكتب')
    .addMetaTag('viewport', 'width=device-width, initial-scale=1')
    .setXFrameOptionsMode(HtmlService.XFrameOptionsMode.DEFAULT);
}

/**
 * The places still open for review in one book, as a JSON string for the page:
 * {ok, fileId, title, pages: [{page, img, w, h, base, more, items}], saved, sent, running, note}.
 * A place the user answered after the book's last run is left out until the next run.
 */
function getReview(fileId) {
  try {
    fileId = String(fileId || '');
    if (!FILE_ID_RE.test(fileId)) return JSON.stringify({ ok: false, error: 'bad id' });
    var cfg = config_();
    var rv = loadRv_(fileId) || {};
    var r = gh_(cfg, 'get', contentsPath_(cfg, 'drive-' + fileId + '/review/items.json'), null, GH_RAW);
    if (r.code !== 200 && r.code !== 404) return JSON.stringify({ ok: false, error: describeResponse_(r) });
    var data = r.code === 200 ? (parseJson_(ghText_(r)) || {}) : {};
    var answers = readAnswers_(answersFile_(fileId, rv, false));
    var seen = String(data.answers_seen || '');
    var hidden = {};
    Object.keys(answers).forEach(function (id) {
      var a = answers[id];
      if (!(String(a.at || '') > seen)) return;
      hidden[id] = true;
      if (a.kind === 'rest') (a.ids || []).forEach(function (x) { hidden[x] = true; });
    });
    var pages = (Array.isArray(data.pages) ? data.pages : []).map(function (p) {
      var items = (Array.isArray(p.items) ? p.items : []).filter(function (it) { return !hidden[it.id]; });
      return { page: p.page, img: p.img || '', w: p.w, h: p.h, base: p.base, more: p.more || 0, items: items };
    }).filter(function (p) { return p.items.length; });
    var job = loadJobs_()[fileId];
    return JSON.stringify({
      ok: true,
      fileId: fileId,
      title: rv.t || data.book || (job && job.title) || '',
      pages: pages,
      saved: unsent_(answers, rv),
      sent: reviewPending_(rv),
      running: !!(job && job.humanRound && isActive_(job)),
      note: rv.x || ''
    });
  } catch (err) {
    Logger.log('getReview failed: ' + (err && err.stack ? err.stack : err));
    return JSON.stringify({ ok: false, error: String(err && err.message ? err.message : err) });
  }
}

/** One page image of a book's review, as {ok, src: "data:image/jpeg;base64,…"}. */
function getReviewImage(fileId, name) {
  try {
    fileId = String(fileId || '');
    name = String(name || '');
    if (!FILE_ID_RE.test(fileId) || !IMAGE_RE.test(name)) return JSON.stringify({ ok: false, error: 'bad name' });
    var cfg = config_();
    var r = gh_(cfg, 'get', contentsPath_(cfg, 'drive-' + fileId + '/review/' + name), null, GH_RAW);
    if (r.code !== 200) return JSON.stringify({ ok: false, error: describeResponse_(r) });
    return JSON.stringify({ ok: true, src: 'data:image/jpeg;base64,' + Utilities.base64Encode(r.resp.getContent()) });
  } catch (err) {
    Logger.log('getReviewImage failed: ' + err);
    return JSON.stringify({ ok: false, error: String(err && err.message ? err.message : err) });
  }
}

/**
 * Saves answers from the page: json is {id: answer}. They are merged into the book's
 * answers file in Drive (a later answer to the same place replaces the earlier one).
 * Returns {ok, saved, pending} or {ok: false, busy: true} when another check holds the lock.
 */
function saveAnswers(fileId, json) {
  fileId = String(fileId || '');
  if (!FILE_ID_RE.test(fileId)) return JSON.stringify({ ok: false, error: 'bad id' });
  var incoming = parseJson_(json);
  if (!incoming || typeof incoming !== 'object' || Array.isArray(incoming)) {
    return JSON.stringify({ ok: false, error: 'bad answers' });
  }
  var ids = Object.keys(incoming);
  if (ids.length > CONFIG.REVIEW_MAX_ANSWERS) return JSON.stringify({ ok: false, error: 'too many answers' });
  var clean = {};
  for (var i = 0; i < ids.length; i++) {
    var a = cleanAnswer_(ids[i], incoming[ids[i]]);
    if (!a) return JSON.stringify({ ok: false, error: 'bad answer ' + shorten_(ids[i], 40) });
    clean[ids[i]] = a;
  }
  var lock = LockService.getScriptLock();
  if (!lock.tryLock(20000)) return JSON.stringify({ ok: false, busy: true });
  try {
    var rv = loadRv_(fileId) || { t: titleOf_(fileId) };
    var file = answersFile_(fileId, rv, true);
    var data = parseJson_(file.getBlob().getDataAsString()) || {};
    var answers = data.answers && typeof data.answers === 'object' ? data.answers : {};
    var at = now_().toISOString();
    ids.forEach(function (id) {
      clean[id].at = at;
      answers[id] = clean[id];
    });
    file.setContent(JSON.stringify({ v: 1, fileId: fileId, book: rv.t || data.book || '', answers: answers }));
    rv.a = file.getId();
    rv.n = unsent_(answers, rv);
    saveRv_(fileId, rv);
    return JSON.stringify({ ok: true, saved: ids.length, pending: rv.n });
  } catch (err) {
    Logger.log('saveAnswers failed: ' + (err && err.stack ? err.stack : err));
    return JSON.stringify({ ok: false, error: String(err && err.message ? err.message : err) });
  } finally {
    lock.releaseLock();
  }
}

/**
 * "أرسل التصحيحات": the book's saved answers are applied in a review round, now when
 * nothing else is running, otherwise after the current book. Pressing it twice
 * starts one round.
 */
function sendReview(fileId) {
  fileId = String(fileId || '');
  if (!FILE_ID_RE.test(fileId)) return JSON.stringify({ ok: false, error: 'bad id' });
  var lock = LockService.getScriptLock();
  if (!lock.tryLock(30000)) return JSON.stringify({ ok: false, busy: true });
  try {
    var rv = loadRv_(fileId);
    if (!rv || !rv.a) return JSON.stringify({ ok: false, error: 'nothing to send' });
    var unsent = unsent_(readAnswers_(answersFile_(fileId, rv, false)), rv);
    if (!unsent && !rv.x && !reviewPending_(rv)) return JSON.stringify({ ok: true, started: false, nothing: true });
    if (unsent || rv.x) rv.sent = now_().toISOString();
    if (rv.x === 'orig-gone' || rv.x === 'failed') rv.x = '';
    rv.tries = 0;
    saveRv_(fileId, rv);
  } finally {
    lock.releaseLock();
  }
  var result = tick();
  return JSON.stringify({ ok: true, started: /review [\w-]+: dispatched/.test(result) });
}

/**
 * A request from the status page for the helper on the user's computer: json is
 * {action, choices: {name: value}, label}. It goes into requests.json in the helper's
 * Drive folder, where the helper finds it within a minute or two. Returns {ok, id} or
 * {ok: false, error}.
 */
function helperRequest(json) {
  try {
    var r = parseJson_(json) || {};
    var action = String(r.action || '');
    if (!/^[a-z0-9-]{1,40}$/.test(action)) return JSON.stringify({ ok: false, error: 'طلب غير مفهوم' });
    var choices = {};
    var given = r.choices && typeof r.choices === 'object' ? r.choices : {};
    Object.keys(given).slice(0, 10).forEach(function (k) {
      if (/^[a-z0-9_-]{1,30}$/.test(k)) choices[k] = shorten_(String(given[k]), 200);
    });
    var req = {
      id: 'r' + now_().getTime().toString(36) + Math.floor(Math.random() * 1296).toString(36),
      action: action,
      choices: choices,
      label: shorten_(String(r.label || ''), 120),
      at: now_().toISOString()
    };
    // The user lock, not the script lock: a check can hold that one for minutes.
    var lock = LockService.getUserLock();
    if (!lock.tryLock(20000)) return JSON.stringify({ ok: false, error: 'مشغول، أعد المحاولة بعد دقيقة.' });
    try {
      var reqs = helperRequests_();
      reqs.push(req);
      while (reqs.length > CONFIG.HELPER_MAX_REQUESTS) reqs.shift();
      props_().setProperty('HELPER_REQS', JSON.stringify(reqs));
      writeRequests_();
    } finally {
      lock.releaseLock();
    }
    return JSON.stringify({ ok: true, id: req.id });
  } catch (err) {
    Logger.log('helperRequest failed: ' + (err && err.stack ? err.stack : err));
    return JSON.stringify({ ok: false, error: String(err && err.message ? err.message : err) });
  }
}

/**
 * From the page: the models the user put in the left box of each section, as JSON
 * {transcribe: [ids], review: [ids]}. They go into requests.json, where the helper finds
 * them within a minute or two. Returns {ok, boxes} or {ok: false, error}.
 */
function setHelperBoxes(json) {
  try {
    var r = parseJson_(json);
    if (!r || typeof r !== 'object' || Array.isArray(r)) return JSON.stringify({ ok: false, error: 'طلب غير مفهوم' });
    var boxes = {};
    var bad = false;
    ['transcribe', 'review'].forEach(function (k) {
      var list = Array.isArray(r[k]) ? r[k].map(String) : [];
      if (list.length > CONFIG.HELPER_MAX_CHIPS || list.some(function (id) { return !CHIP_RE.test(id); })) bad = true;
      boxes[k] = list.filter(function (id, i) { return list.indexOf(id) === i; });
    });
    if (bad) return JSON.stringify({ ok: false, error: 'نموذج غير معروف' });
    // When each model may work: {id: {start, stop}} (times; '' = now / never).
    boxes.times = cleanTimes_(r.times, boxes.transcribe.concat(boxes.review));
    var lock = LockService.getUserLock();
    if (!lock.tryLock(20000)) return JSON.stringify({ ok: false, error: 'مشغول، أعد المحاولة بعد دقيقة.' });
    try {
      boxes.at = now_().toISOString();
      props_().setProperty('HELPER_BOXES', JSON.stringify(boxes));
      // A model put in the review box starts within a minute: what it must not review
      // (a book deleted, or closed, since the last check) goes first.
      var reviews = boxes.review.length ? helperSub_(CONFIG.HELPER_REVIEWS, false) : null;
      if (reviews) dropHelperReviews_(reviews);
      writeRequests_();
    } finally {
      lock.releaseLock();
    }
    return JSON.stringify({ ok: true, boxes: helperBoxes_() });
  } catch (err) {
    Logger.log('setHelperBoxes failed: ' + (err && err.stack ? err.stack : err));
    return JSON.stringify({ ok: false, error: String(err && err.message ? err.message : err) });
  }
}

// ============================================================================
// One check
// ============================================================================

function runTick_() {
  if (!props_().getProperty('FOLDER_ROOT')) {
    Logger.log('tick: setup() has not been run yet');
    return 'not-set-up';
  }
  var cfg = config_();
  if (!cfg.token) {
    notifyOnce_('token-missing', 'تحويل الكتب: رمز GitHub غير موجود', [
      'التحويل متوقف لأن خاصية GITHUB_TOKEN غير موجودة في إعدادات المشروع.',
      'الحل: الخطوة 3 من دليل الإعداد (إضافة GITHUB_TOKEN في Script properties).'
    ]);
    return 'no-token';
  }

  var ctx = { cfg: cfg, folders: getFolders_(), jobs: loadJobs_(), dispatched: 0 };
  var notes = [];

  var active = activeJob_(ctx.jobs);
  if (active) {
    notes.push(active.fileId + ': ' + advanceJob_(ctx, active));
  }
  // A finished job frees the slot, so the next book can start in the same check.
  if (!activeJob_(ctx.jobs) && ctx.dispatched === 0) {
    // The user's answers first (a quick round with no model request), then the verdicts
    // of the helper's models (the same kind of round), then books parked for a reader
    // whose quota has renewed (oldest first), then parked books back to the judges on a
    // new day (their quota is separate and small), then books the helper finished, then
    // new books, then improvement rounds.
    var applied = applyNext_(ctx) || helperJudgeNext_(ctx);
    var judged = applied || resumeReaderNext_(ctx) || judgeNext_(ctx) || helperNext_(ctx);
    var started = judged || startNext_(ctx);
    notes.push(started);
    // Nothing new to convert: use the day's judge quota on a finished book's unsure pages.
    if (started === 'idle') {
      var refined = refineNext_(ctx);
      notes.push(refined);
      // Still nothing: prepare the review of a finished book delivered before it existed.
      if (refined === 'idle') notes.push(prepareNext_(ctx));
    }
  }
  rememberExpiry_();
  return notes.join('; ');
}

function advanceJob_(ctx, job) {
  // "queued": in "2- قيد التحويل" but GitHub has not accepted the dispatch yet.
  if (job.stage === 'queued') {
    return dispatch_(ctx, job, job.pendingCount !== false);
  }

  // A review round never pauses, waits for the judges or gives the book up.
  if (job.humanRound) return advanceReview_(ctx, job);

  // Paused for the Gemini quota: the run has finished; wait for a new Pacific day.
  if (job.pausedDay) {
    return resumeIfNewDay_(ctx, job);
  }

  var status = readStatus_(ctx, job);
  if (status === 'error') return 'waiting (could not read status)';
  if (status) return handleStatus_(ctx, job, status);
  return handleNoStatus_(ctx, job);
}

function handleStatus_(ctx, job, s) {
  job.lastState = s.state;
  if (s.pages_total) job.pagesTotal = Number(s.pages_total) || 0;
  if (s.pages_done !== undefined) job.pagesDone = Number(s.pages_done) || 0;

  // Failures count in a row only: any other result starts the count again.
  if (s.state !== 'failed') job.failStreak = 0;
  delete job.helperOff;

  // Waiting only for the judges, the helper or one reader is written as "paused" for older scripts.
  var waiting = s.state === 'paused' ? String(s.waiting_for || '') : '';
  var state = waiting === 'judges' ? 'judging' : waiting === 'helper' ? 'helper'
    : waiting === 'reader' ? 'readerWait' : s.state;
  switch (state) {
    case 'done':
      return deliver_(ctx, job, s, false);

    case 'incomplete':
      if (job.attempts < CONFIG.MAX_INCOMPLETE_ATTEMPTS) {
        saveJobs_(ctx.jobs);
        return 'incomplete, retry: ' + dispatch_(ctx, job, true);
      }
      return deliver_(ctx, job, s, true);

    case 'paused':
      // quota_day is the Pacific date the quota ran out; the next attempt waits for a
      // later Pacific date. A paused restart is not a failed attempt.
      job.pausedDay = isDay_(s.quota_day) ? s.quota_day : pacificDay_(new Date(Date.parse(s.updated_at)));
      job.pausedAt = s.updated_at;
      job.pauses = (job.pauses || 0) + 1;
      if (job.pauses > CONFIG.MAX_PAUSES) {
        return giveUp_(ctx, job, 'توقف الكتاب يومياً لأكثر من ' + CONFIG.MAX_PAUSES + ' يوماً دون أن يكتمل.');
      }
      saveJobs_(ctx.jobs);
      if (!job.pauseMailed) {
        job.pauseMailed = true;
        saveJobs_(ctx.jobs);
        mailPaused_(job);
      }
      return resumeIfNewDay_(ctx, job);

    case 'judging':
      // Read and checked; only the judges' quota is missing. The book stays parked in
      // "2- قيد التحويل" without holding the slot, so the next book can use the readers'
      // quota; judgeNext_ brings it back to the judges on a later Pacific day.
      // A refinement round is different: its book is already delivered, so what the
      // judges did today replaces it now, and the next round continues tomorrow.
      if (job.refining) return deliver_(ctx, job, s, false);
      job.stage = 'judging';
      job.judgeDay = isDay_(s.quota_day) ? s.quota_day : pacificDay_(new Date(Date.parse(s.updated_at)));
      job.judgeRounds = (job.judgeRounds || 0) + 1;
      if (job.judgeRounds > CONFIG.MAX_PAUSES) return deliver_(ctx, job, s, false);
      saveJobs_(ctx.jobs);
      if (!job.judgeMailed) {
        job.judgeMailed = true;
        saveJobs_(ctx.jobs);
        sendMail_('ينتظر الحكّام: ' + job.title, [
          'اكتملت قراءة «' + job.title + '»، وبقيت ' + pageList_(s.pages_judging).length +
            ' صفحة مشكوك فيها تنتظر الحكّام، فقد انتهى رصيدهم اليوم.',
          'يبقى الكتاب في «' + CONFIG.FOLDERS.working + '»، ويبدأ الكتاب التالي الآن.',
          'بعد تجدد رصيد الحكّام تُعرض عليهم صفحاته أولاً، ثم يصلك الكتاب في «' + CONFIG.FOLDERS.ready + '».'
        ]);
      }
      return 'judging (parked)';

    case 'readerWait':
      // One free reader's quota is used up and the other still has some: the pages wait
      // only for second readings from the reader that is out. The book waits without
      // holding the slot, so the next book starts with the reader that is left;
      // resumeReaderNext_ brings it back first on a later Pacific day.
      if (job.refining) return deliver_(ctx, job, s, false);
      job.stage = 'readerWait';
      job.readerDay = isDay_(s.quota_day) ? s.quota_day : pacificDay_(new Date(Date.parse(s.updated_at)));
      job.readerWaits = (job.readerWaits || 0) + 1;
      if (job.readerWaits > CONFIG.MAX_PAUSES) {
        return giveUp_(ctx, job, 'انتظر الكتاب القارئ الثاني أكثر من ' + CONFIG.MAX_PAUSES + ' يوماً دون أن يكتمل.');
      }
      saveJobs_(ctx.jobs);
      if (!job.readerMailed) {
        job.readerMailed = true;
        saveJobs_(ctx.jobs);
        sendMail_('ينتظر القارئ الثاني: ' + job.title, [
          'انتهى اليوم رصيد أحد القارئين المجانيين أثناء «' + job.title + '»، وبقي رصيد للآخر.',
          'تنتظر ' + pageList_(s.pages_waiting).length + ' صفحة قراءتها الثانية من القارئ الذي نفد رصيده.',
          'يبقى الكتاب في «' + CONFIG.FOLDERS.working + '»، ويبدأ الكتاب التالي الآن بالقارئ المتاح.',
          'بعد تجدد الرصيد (نحو الساعة ' + Utilities.formatDate(pacificMidnightAfter_(job.readerDay), scriptTz_(), 'HH:mm') +
            ' بتوقيتك) يكمل هذا الكتاب أولاً، ثم يصلك في «' + CONFIG.FOLDERS.ready + '».'
        ]);
      }
      return 'reader wait (parked)';

    case 'helper':
      // Read once by the free model; the helper's models on the user's computer give the
      // second reading and settle the pages. The book waits without holding the slot, so
      // the free model starts the next book; helperNext_ brings it back.
      job.stage = 'helper';
      job.helperAt = now_().toISOString();
      job.helperMovedAt = job.helperAt;
      job.helperFinished = 0;
      job.helperPages = pageList_(s.pages_helper).length;
      delete job.helperDoneSeen;
      saveJobs_(ctx.jobs);
      return 'helper (parked)';

    case 'failed':
      return failedOrGiveUp_(ctx, job, 'فشل التحويل مرتين متتاليتين.');

    default:
      Logger.log('unknown state "' + s.state + '" for ' + job.fileId + ', treating as no status');
      return handleNoStatus_(ctx, job);
  }
}

function resumeIfNewDay_(ctx, job) {
  var today = pacificDay_(now_());
  if (today > job.pausedDay) {
    return 'quota reset, resume: ' + dispatch_(ctx, job, false);
  }
  // The helper's models read what the free model could not: no need to wait for tomorrow.
  if (helperDoneFor_(ctx, job)) return 'helper finished, resume: ' + dispatch_(ctx, job, false);
  return 'paused until after ' + job.pausedDay + ' (Pacific)';
}

function handleNoStatus_(ctx, job) {
  var age = now_().getTime() - Date.parse(job.dispatchedAt);
  if (age > CONFIG.GIVE_UP_MS) {
    return giveUp_(ctx, job, 'مرّت أكثر من 8 ساعات على بدء التحويل دون أي نتيجة.');
  }
  var running = runInProgress_(ctx, job);
  if (running === null) return 'waiting (could not list runs)';
  if (running) return 'running';
  if (age < CONFIG.LOST_AFTER_MS) return 'waiting for the run to start';

  // No run and no status long after the dispatch: GitHub dropped it, or the run was
  // cancelled before it could write status.json.
  Logger.log('dispatch of ' + job.fileId + ' looks lost (attempt ' + job.attempts + ')');
  if (job.attempts < CONFIG.MAX_LOST_ATTEMPTS) {
    return 'lost, retry: ' + dispatch_(ctx, job, true);
  }
  return giveUp_(ctx, job, 'بدأ التحويل ' + job.attempts + ' مرات ولم يصل أي رد من GitHub.');
}

function failedOrGiveUp_(ctx, job, reason) {
  job.failStreak = (job.failStreak || 0) + 1;
  if (job.failStreak < CONFIG.MAX_FAILED_ATTEMPTS) {
    job.lastState = 'failed';
    saveJobs_(ctx.jobs);
    return 'failed, retry: ' + dispatch_(ctx, job, true);
  }
  return giveUp_(ctx, job, reason);
}

// ============================================================================
// Starting a book
// ============================================================================

function startNext_(ctx) {
  if (ctx.dispatched > 0) return 'wait';

  // A book left in "2- قيد التحويل" without a job (for example after JOBS was lost)
  // is picked up again before new ones.
  var orphan = findOrphan_(ctx);
  var candidates = orphan ? [orphan] : scanInbox_(ctx.folders.inbox).ready;
  if (!candidates.length) return 'idle';

  for (var i = 0; i < candidates.length && i < 3; i++) {
    var file = candidates[i];
    if (!orphan) {
      try {
        file.moveTo(ctx.folders.working);
      } catch (err) {
        Logger.log('could not move ' + file.getId() + ' to the working folder: ' + err);
        continue;
      }
    }
    var job = newJob_(file);
    ctx.jobs[job.fileId] = job;
    saveJobs_(ctx.jobs);
    return 'started ' + job.fileId + ': ' + dispatch_(ctx, job, true);
  }
  return 'idle (files could not be moved)';
}

/**
 * Runs a delivered book again (once per Pacific day) so the stronger "judge" models
 * look at its unsure pages; nothing is read again. Stops for a book when a round
 * no longer lowers its count of unsure pages.
 */
function refineNext_(ctx) {
  var today = pacificDay_(now_());
  var pick = null;
  finishedJobs_(ctx.jobs).forEach(function (j) {
    if (j.stage === 'delivered' && !j.refineDone && j.unsure === undefined) backfill_(ctx, j);
    if (j.stage !== 'delivered' || j.refineDone || !(j.unsure > 0) || j.refineDay === today) return;
    if ((j.refines || 0) >= CONFIG.MAX_REFINES) {
      stopRefining_(ctx, j);
      return;
    }
    if (!pick || String(j.finishedAt) < String(pick.finishedAt)) pick = j;
  });
  if (!pick) return 'idle';
  // Finished jobs are stored without these fields (see pruneJobs_); rebuild them.
  pick.resultDir = 'drive-' + pick.fileId;
  try {
    if (bookGone_(pick.fileId, pick.epubId)) throw new Error('gone');
    var again = newJob_(DriveApp.getFileById(pick.fileId));
    pick.name = again.name;
    if (again.driveName) pick.driveName = again.driveName;
  } catch (err) {
    // The original was deleted from "4- الأصل", or the EPUB: nothing left to improve.
    pick.refineDone = true;
    saveJobs_(ctx.jobs);
    return 'idle (book gone: ' + pick.fileId + ')';
  }
  pick.refineDay = today;
  pick.refines = (pick.refines || 0) + 1;
  pick.refining = true;
  pick.unsureBefore = pick.unsure;
  // The new EPUB and report replace the old ones once the round is delivered.
  pick.oldEpubId = pick.epubId || '';
  pick.oldReportId = pick.reportId || '';
  pick.epubId = '';
  pick.reportId = '';
  // Queued (not delivered) before saving, so pruneJobs_ keeps the fields set above.
  pick.stage = 'queued';
  pick.pendingCount = false;
  saveJobs_(ctx.jobs);
  return 'refining ' + pick.fileId + ': ' + dispatch_(ctx, pick, false);
}

/**
 * Books delivered by an older script have no count of unsure pages (and may have lost
 * their report's id): read them back from the results branch and the ready folder.
 */
function backfill_(ctx, job) {
  var r = gh_(ctx.cfg, 'get', contentsPath_(ctx.cfg, 'drive-' + job.fileId + '/status.json'), null, GH_RAW);
  if (r.code !== 200) return;
  var s = parseJson_(ghText_(r));
  if (!s || typeof s !== 'object') return;
  job.unsure = pageList_(s.pages_unsure).length;
  if (!job.reportId && job.title) {
    var it = ctx.folders.ready.getFilesByName(job.title + ' - تقرير.txt');
    while (it.hasNext()) {
      var f = it.next();
      if (!f.isTrashed()) { job.reportId = f.getId(); break; }
    }
  }
  saveJobs_(ctx.jobs);
}

/**
 * The book will not change any more: its EPUB and report move to "6- مكتمل".
 * With mail, the user is told which pages (if any) are still unsure.
 */
/**
 * The judges cannot improve the book any more, but pages are still unsure: no more
 * improvement rounds, and the book stays in "3- جاهز" until the user's review brings
 * its unsure pages to none (only then it is finished, see finalize_).
 */
function stopRefining_(ctx, job) {
  job.refineDone = true;
  saveJobs_(ctx.jobs);
  var rv = loadRv_(job.fileId);
  sendMail_('انتهت مراجعة النماذج: ' + job.title, [
    'لم تعد النماذج تحسّن «' + job.title + '» أكثر.',
    'بقيت ' + job.unsure + ' صفحة مشكوكاً فيها' + (job.unsureList ? ' (' + job.unsureList + ')' : '') +
      '، فيبقى الكتاب في «' + CONFIG.FOLDERS.ready + '» حتى تراجعها، ثم يُنقل إلى «' + CONFIG.FOLDERS.final + '».'
  ].concat(reviewLines_(job, rv)));
}

/** No page is unsure any more: the book moves to "6- مكتمل" and will not change. */
function finalize_(ctx, job, mail) {
  job.refineDone = true;
  job.final = true;
  [job.epubId, job.reportId].forEach(function (id) {
    if (!id) return;
    try { DriveApp.getFileById(id).moveTo(ctx.folders.final); } catch (err) { Logger.log('final move ' + id + ': ' + err); }
  });
  saveJobs_(ctx.jobs);
  if (!mail) return;
  var lines = [
    'انتهى تحسين «' + job.title + '»، ولن يتغير بعد الآن.',
    'نقلته إلى مجلد «' + CONFIG.ROOT_NAME + ' / ' + CONFIG.FOLDERS.final + '»: https://drive.google.com/file/d/' + job.epubId + '/view'
  ];
  if (job.unsure > 0) {
    lines.push('بقيت ' + job.unsure + ' صفحة لم تقدر النماذج على حسمها، وعليها تنبيه أصفر داخل الكتاب لتطابقها بالأصل' +
      (job.unsureList ? ': ' + job.unsureList : '') + '.');
  } else {
    lines.push('لم تبقَ فيه صفحات مشكوك فيها.');
  }
  var rv = loadRv_(job.fileId);
  if (rv && rv.o > 0) {
    lines = lines.concat(reviewLines_(job, rv));
  } else {
    lines.push('يمكنك الآن حذف ملفه الأصلي من «' + CONFIG.FOLDERS.originals + '» إن احتجت المساحة.');
  }
  sendMail_('مكتمل: ' + job.title, lines);
}

/** Dispatches the oldest book parked for a reader whose quota has renewed, or returns ''. */
function resumeReaderNext_(ctx) {
  var today = pacificDay_(now_());
  var pick = null;
  Object.keys(ctx.jobs).forEach(function (id) {
    var j = ctx.jobs[id];
    if (j && j.stage === 'readerWait' && String(j.readerDay || '') < today &&
        (!pick || String(j.createdAt || '') < String(pick.createdAt || ''))) pick = j;
  });
  if (!pick) return '';
  pick.stage = 'queued';
  pick.pendingCount = false;
  saveJobs_(ctx.jobs);
  return 'reader renewed ' + pick.fileId + ': ' + dispatch_(ctx, pick, false);
}

/** Dispatches the oldest parked book whose judges' quota has renewed, or returns ''. */
function judgeNext_(ctx) {
  var today = pacificDay_(now_());
  var pick = null;
  Object.keys(ctx.jobs).forEach(function (id) {
    var j = ctx.jobs[id];
    if (j && j.stage === 'judging' && String(j.judgeDay || '') < today &&
        (!pick || String(j.judgeDay || '') < String(pick.judgeDay || ''))) pick = j;
  });
  if (!pick) return '';
  pick.stage = 'queued';
  pick.pendingCount = false;
  saveJobs_(ctx.jobs);
  return 'judging ' + pick.fileId + ': ' + dispatch_(ctx, pick, false);
}

function newJob_(file) {
  var id = file.getId();
  var name = String(file.getName() || '').trim();
  var ext = extOf_(name);
  var hasExt = !!SUPPORTED_EXT[ext];
  var title = (hasExt ? name.slice(0, name.length - ext.length - 1) : name).trim() || 'كتاب';
  var job = {
    fileId: id,
    name: name,
    title: title,
    stage: 'queued',
    createdAt: now_().toISOString(),
    dispatchedAt: '',
    attempts: 0,
    pausedDay: '',
    lastState: '',
    resultDir: 'drive-' + id,
    lastError: '',
    pendingCount: true
  };
  // The workflow picks the converter from the extension, so a name without one
  // (possible on Drive) gets the extension of its MIME type.
  if (!hasExt) job.driveName = name + '.' + (MIME_EXT[file.getMimeType()] || 'pdf');
  return job;
}

/**
 * Starts (or restarts) the workflow for a job. countAttempt is false only for the
 * restart after a quota pause.
 */
function dispatch_(ctx, job, countAttempt) {
  if (ctx.dispatched > 0) {
    Logger.log('second dispatch in one check refused for ' + job.fileId);
    return 'deferred';
  }
  ctx.dispatched++;
  // getOAuthToken() returns this execution's token. It carries the manifest scopes,
  // including drive (DriveApp is also used, so the scope is never dropped). It is valid
  // for about an hour, which is why every restart sends a fresh one.
  var token = ScriptApp.getOAuthToken();
  var at = now_();
  var body = {
    ref: ctx.cfg.ref,
    inputs: {
      drive_file_id: job.fileId,
      drive_token: token,
      drive_file_name: job.driveName || job.name,
      result_dir: job.resultDir,
      title: job.title
    }
  };
  // The user's answers go with every run of the book (improvement and judge rounds
  // too), so none is lost; a review round only applies them.
  var rv = loadRv_(job.fileId);
  var review = (rv && rv.a ? rv.a : '') + (job.humanRound ? ':only' : '');
  if (review) body.inputs.review = review;
  // The helper on the user's computer (docs/helper-boxes-plan.md): the book's folder,
  // and the verdicts of its models, which go with every run like the user's answers.
  var helper = helperInput_(ctx, job);
  if (helper) body.inputs.helper = helper;
  var verdicts = verdictsFile_(job.fileId);
  if (verdicts) body.inputs.judge = verdicts.getId();
  var r = gh_(ctx.cfg, 'post', '/repos/' + ctx.cfg.repo + '/actions/workflows/' + CONFIG.WORKFLOW + '/dispatches', body);

  if (r.code === 204 || r.code === 200) {
    job.stage = 'running';
    job.dispatchedAt = at.toISOString();
    if (countAttempt) job.attempts = (job.attempts || 0) + 1;
    job.pausedDay = '';
    job.lastError = '';
    delete job.pendingCount;
    delete job.helperDoneSeen;
    if (rv && rv.a) {
      // Answers saved until now travel with this run; the old mark comes back if a
      // review round fails.
      if (job.rvDonePrev === undefined) job.rvDonePrev = rv.done || '';
      rv.done = at.toISOString();
      rv.n = 0;
      saveRv_(job.fileId, rv);
    }
    saveJobs_(ctx.jobs);
    Logger.log('dispatched ' + job.fileId + ' (attempt ' + job.attempts + ', ref ' + ctx.cfg.ref + ')');
    return 'dispatched';
  }

  // Keep the book in "2- قيد التحويل" and try again at the next check.
  job.stage = 'queued';
  job.pendingCount = !!countAttempt;
  job.pausedDay = '';
  job.lastError = shorten_('dispatch: ' + describeResponse_(r), 200);
  saveJobs_(ctx.jobs);
  reportGitHubProblem_(ctx, r, 'dispatch');
  return 'dispatch failed (' + r.code + ')';
}

// ============================================================================
// Review by hand (docs/human-review-plan.md)
// ============================================================================

/**
 * RV_<fileId>: {t: title, a: answers file id, o: places open after the last run,
 * e: EPUB id, seen: answers_seen of the last run, n: answers saved and not sent yet,
 * sent: when the user last pressed send, done: when the last run carrying the answers
 * started, tries: failed review rounds, x: '' | 'orig-gone' | 'no-data' | 'failed'}.
 * Always written under the script lock.
 */
function loadRv_(fileId) {
  var v = parseJson_(props_().getProperty(REVIEW_PREFIX + fileId));
  return v && typeof v === 'object' && !Array.isArray(v) ? v : null;
}

function saveRv_(fileId, rv) {
  rv.t = shorten_(rv.t || '', CONFIG.REVIEW_TITLE_CHARS);
  props_().setProperty(REVIEW_PREFIX + fileId, JSON.stringify(rv));
}

function reviewIds_() {
  return props_().getKeys().filter(function (k) { return k.indexOf(REVIEW_PREFIX) === 0; })
    .map(function (k) { return k.slice(REVIEW_PREFIX.length); });
}

/** Answers were sent (or a review was asked for) and no run has carried them yet. */
function reviewPending_(rv) {
  return !!(rv && rv.sent && String(rv.sent) > String(rv.done || ''));
}

function titleOf_(fileId) {
  var j = loadJobs_()[fileId];
  return j ? j.title : '';
}

/** The book's answers file in Drive, made on first use when create is true. */
function answersFile_(fileId, rv, create) {
  if (rv && rv.a) {
    try {
      var known = DriveApp.getFileById(rv.a);
      if (!known.isTrashed()) return known;
    } catch (err) { /* deleted: look it up by name */ }
  }
  var root = folderFromProp_('FOLDER_ROOT');
  if (!root) return null;
  var it = root.getFoldersByName(CONFIG.REVIEW_FOLDER);
  var folder = null;
  while (it.hasNext()) {
    var f = it.next();
    if (!f.isTrashed()) { folder = f; break; }
  }
  var name = 'review-' + fileId + '.json';
  if (folder) {
    var files = folder.getFilesByName(name);
    while (files.hasNext()) {
      var x = files.next();
      if (!x.isTrashed()) return x;
    }
  }
  if (!create) return null;
  folder = folder || root.createFolder(CONFIG.REVIEW_FOLDER);
  return folder.createFile(name, JSON.stringify({ v: 1, fileId: fileId, book: (rv && rv.t) || '', answers: {} }),
    'application/json');
}

function readAnswers_(file) {
  if (!file) return {};
  var data = parseJson_(file.getBlob().getDataAsString());
  return data && data.answers && typeof data.answers === 'object' ? data.answers : {};
}

/** How many answers were saved after the last run that carried them. */
function unsent_(answers, rv) {
  var since = String((rv && rv.done) || '');
  return Object.keys(answers).filter(function (k) { return String(answers[k].at || '') > since; }).length;
}

/** An answer from the page with only the expected fields, or null when it is not valid. */
function cleanAnswer_(id, a) {
  if (!a || typeof a !== 'object' || Array.isArray(a)) return null;
  var m = PLACE_ID_RE.exec(id) || REST_ID_RE.exec(id);
  var page = Number(a.page);
  if (!m || Number(m[1]) !== page) return null;
  var str = function (v, max) { return typeof v === 'string' && v.length <= max ? v : null; };
  if (a.kind === 'rest') {
    var ids = Array.isArray(a.ids) ? a.ids : null;
    var fps = Array.isArray(a.fps) ? a.fps : [];
    if (!REST_ID_RE.test(id) || a.verdict !== 'keep' || !ids || ids.length > CONFIG.REVIEW_REST_IDS ||
        fps.length > CONFIG.REVIEW_REST_IDS) return null;
    if (ids.some(function (x) { var k = PLACE_ID_RE.exec(String(x)); return !k || Number(k[1]) !== page; })) return null;
    if (fps.some(function (x) { return str(x, 2000) === null; })) return null;
    return { kind: 'rest', page: page, verdict: 'keep', ids: ids.map(String), fps: fps };
  }
  if (!PLACE_ID_RE.test(id) || ['diff', 'missing', 'page'].indexOf(a.kind) < 0) return null;
  if (a.verdict !== 'keep' && a.verdict !== 'fix') return null;
  var big = a.kind === 'page' ? 60000 : 6000;
  var out = {
    kind: a.kind, page: page, verdict: a.verdict, fp: str(a.fp, 4000), before: str(a.before, 6000),
    core: str(a.core, big), after: str(a.after, 6000), base: str(a.base || '', 40),
    place: str(a.place || '', 10), pos: Number(a.pos)
  };
  if (out.fp === null || out.before === null || out.core === null || out.after === null || out.base === null ||
      out.place === null || !(out.pos >= 0) || Math.floor(out.pos) !== out.pos) return null;
  if (a.verdict === 'fix') {
    out.text = str(a.text, a.kind === 'page' ? CONFIG.REVIEW_PAGE_CHARS : CONFIG.REVIEW_ANSWER_CHARS);
    if (out.text === null) return null;
  }
  return out;
}

/** The status page's list of books with places to review. */
function reviewSummaries_(jobs) {
  return reviewIds_().map(function (id) {
    var rv = loadRv_(id);
    if (!rv) return null;
    var j = jobs[id];
    var pending = reviewPending_(rv) || !!(j && j.humanRound && isActive_(j));
    if (!(rv.o > 0 || rv.n > 0 || pending || rv.x)) return null;
    // A book the user deleted (its original, or its EPUB) is not listed any more.
    if (bookGone_(id, rv.e)) return null;
    return { fileId: id, title: rv.t || (j && j.title) || '', open: rv.o || 0, saved: rv.n || 0,
      pending: pending, note: rv.x || '' };
  }).filter(function (x) { return x; });
}

/** The user deleted the book: its original, or its EPUB, is gone or in the trash. */
function bookGone_(id, epubId) {
  return !fileAlive_(id) || (!!epubId && !fileAlive_(epubId));
}

/** The Drive file exists and is not in the trash. */
function fileAlive_(id) {
  try {
    return !DriveApp.getFileById(id).isTrashed();
  } catch (err) {
    return false;
  }
}

/** After a delivery: what is open for review now (from status.json). */
function updateRv_(job, s) {
  var rv = loadRv_(job.fileId);
  // A workflow older than the review writes no review numbers.
  if (s.review_items === undefined) return rv;
  rv = rv || {};
  rv.t = job.title;
  rv.o = Number(s.review_items) || 0;
  rv.e = job.epubId || '';
  rv.seen = String(s.review_seen || '');
  rv.tries = 0;
  if (rv.x !== 'orig-gone') rv.x = '';
  if (rv.a) rv.n = unsent_(readAnswers_(answersFile_(job.fileId, rv, false)), rv);
  saveRv_(job.fileId, rv);
  return rv;
}

function rememberPageUrl_() {
  try {
    var url = ScriptApp.getService().getUrl();
    // Only the deployed page: the editor's test link (/dev) works for the owner alone.
    if (url && /\/exec$/.test(url) && props_().getProperty('PAGE_URL') !== url) props_().setProperty('PAGE_URL', url);
  } catch (err) { /* not deployed as a web app */ }
}

function reviewUrl_(fileId) {
  var page = props_().getProperty('PAGE_URL');
  return page ? page + '?review=' + fileId : '';
}

/** Lines for a mail about a book with places open for review. */
function reviewLines_(job, rv) {
  if (!rv || !(rv.o > 0)) return [];
  var url = reviewUrl_(job.fileId);
  return [
    'جاهز للمراجعة: ' + rv.o + ' موضعاً مشكوكاً فيه، ترى كل واحد منها على صورة الصفحة' +
      (url ? ': ' + url : ' في صفحة الحالة.'),
    'لا تحذف الملف الأصلي من «' + CONFIG.FOLDERS.originals + '» قبل أن تنهي المراجعة.'
  ];
}

/** The first delivered book whose user sent answers that no run has applied yet. */
function applyNext_(ctx) {
  var pick = null;
  reviewIds_().forEach(function (id) {
    var rv = loadRv_(id);
    if (!rv || !rv.a || rv.x || !reviewPending_(rv)) return;
    var j = ctx.jobs[id];
    if (j && j.stage !== 'delivered') return;
    if (!pick || String(rv.sent) < String(pick.rv.sent)) pick = { id: id, rv: rv, job: j };
  });
  if (!pick) return '';
  var job = pick.job;
  if (!job) {
    // Dropped from JOBS long after it finished: rebuilt from what the review knows.
    job = { fileId: pick.id, title: pick.rv.t || 'كتاب', stage: 'delivered', epubId: pick.rv.e || '',
      refineDone: true, finishedAt: now_().toISOString(), attempts: 0, unsure: pick.rv.o || 0 };
    ctx.jobs[pick.id] = job;
  }
  return startReviewRound_(ctx, job, false);
}

/**
 * Books finished before the review existed have no places yet: one run that only
 * rebuilds the book from its saved pages makes them.
 */
function prepareNext_(ctx) {
  var pick = null;
  finishedJobs_(ctx.jobs).forEach(function (j) {
    if (j.stage !== 'delivered' || !j.refineDone || !(j.unsure > 0) || loadRv_(j.fileId)) return;
    if (!pick || String(j.finishedAt) < String(pick.finishedAt)) pick = j;
  });
  if (!pick) return 'idle';
  return startReviewRound_(ctx, pick, true);
}

function startReviewRound_(ctx, job, prepare) {
  var file = null;
  try {
    file = DriveApp.getFileById(job.fileId);
    if (file.isTrashed()) file = null;
  } catch (err) { /* deleted */ }
  if (!file) {
    // The run needs the original. Said once; a new "send" tries again.
    var rv = loadRv_(job.fileId) || { t: job.title };
    rv.x = 'orig-gone';
    saveRv_(job.fileId, rv);
    saveJobs_(ctx.jobs);
    if (!prepare) {
      sendMail_('المراجعة تنتظر الأصل: ' + job.title, [
        'لم أستطع تطبيق مراجعتك على «' + job.title + '» لأن ملفه الأصلي لم يعد في «' + CONFIG.FOLDERS.originals + '».',
        'أعِد الملف الأصلي إلى «' + CONFIG.FOLDERS.originals + '»، ثم اضغط «أرسل التصحيحات» في صفحة المراجعة مرة أخرى.',
        'إجاباتك محفوظة ولن تضيع.'
      ]);
    }
    return 'review: original gone (' + job.fileId + ')';
  }
  // Finished jobs are stored without these fields (see pruneJobs_); rebuild them.
  var again = newJob_(file);
  job.resultDir = 'drive-' + job.fileId;
  job.name = again.name;
  if (again.driveName) job.driveName = again.driveName;
  job.humanRound = true;
  if (prepare) job.prepareRound = true;
  job.oldEpubId = job.epubId || '';
  job.oldReportId = job.reportId || '';
  job.epubId = '';
  job.reportId = '';
  job.downloadFails = 0;
  job.stage = 'queued';
  job.pendingCount = false;
  saveJobs_(ctx.jobs);
  return (prepare ? 'preparing ' : 'review ') + job.fileId + ': ' + dispatch_(ctx, job, false);
}

function advanceReview_(ctx, job) {
  var s = readStatus_(ctx, job);
  if (s === 'error') return 'waiting (could not read status)';
  if (s) {
    if (s.state === 'done' || s.state === 'incomplete') return deliver_(ctx, job, s, false);
    return reviewFailed_(ctx, job, s.state + (s.waiting_for ? '/' + s.waiting_for : ''));
  }
  var age = now_().getTime() - Date.parse(job.dispatchedAt);
  if (age <= CONFIG.GIVE_UP_MS) {
    var running = runInProgress_(ctx, job);
    if (running === null) return 'waiting (could not list runs)';
    if (running) return 'running';
    if (age < CONFIG.LOST_AFTER_MS) return 'waiting for the run to start';
  }
  return reviewFailed_(ctx, job, 'lost');
}

function clearReviewRound_(job) {
  ['humanRound', 'prepareRound', 'judgeRound', 'humanFolderId', 'oldEpubId', 'oldReportId', 'rvDonePrev', 'downloadFails']
    .forEach(function (k) { delete job[k]; });
}

/**
 * A review round that did not end well leaves the delivered book exactly as it was
 * (never "5- مشكلة"): the answers count as unsent and are tried once more, then the
 * user is told once.
 */
function reviewFailed_(ctx, job, why) {
  Logger.log('review round of ' + job.fileId + ' did not finish: ' + why);
  var prepare = !!job.prepareRound;
  var donePrev = job.rvDonePrev;
  job.stage = 'delivered';
  job.epubId = job.oldEpubId || job.epubId || '';
  job.reportId = job.oldReportId || job.reportId || '';
  job.pausedDay = '';
  clearReviewRound_(job);
  var rv = loadRv_(job.fileId) || { t: job.title };
  var mail = false;
  if (prepare) {
    // Pages saved before the review existed are gone: nothing to review, nothing to say.
    rv.x = 'no-data';
  } else {
    if (donePrev !== undefined) rv.done = donePrev;
    rv.tries = (rv.tries || 0) + 1;
    if (rv.tries >= CONFIG.REVIEW_MAX_TRIES) {
      rv.x = 'failed';
      mail = true;
    }
  }
  saveRv_(job.fileId, rv);
  saveJobs_(ctx.jobs);
  if (mail) {
    sendMail_('تعذّر تطبيق المراجعة: ' + job.title, [
      'حاولت مرتين تطبيق مراجعتك على «' + job.title + '» ولم تنجح المحاولة، فبقي الكتاب كما كان.',
      'إجاباتك محفوظة، وتُطبَّق أيضاً مع أي جولة تحسين قادمة للكتاب.',
      'للمحاولة مرة أخرى: اضغط «أرسل التصحيحات» في صفحة المراجعة.'
    ]);
  }
  return 'review round failed (' + why + ')';
}

/** Delivery of a review round: the book stays in its folder, its old files are replaced. */
function deliverReview_(ctx, job, s) {
  var prepare = !!job.prepareRound;
  // A round that applied the verdicts of the helper's models (helperJudgeNext_).
  var judged = !!job.judgeRound;
  var used = (Number(s.review_applied) || 0) + (Number(s.review_kept) || 0);
  // A preparation round that applied no answer keeps the book the user already has.
  var keepOld = prepare && !used;
  // The new EPUB goes to "3- جاهز"; finalize_ moves it on when nothing is unsure.
  var folder = ctx.folders.ready;
  if (!keepOld && !job.epubId) {
    var epubName = String(s.epub || '').trim();
    if (!epubName) return reviewFailed_(ctx, job, 'no EPUB');
    var r = gh_(ctx.cfg, 'get', contentsPath_(ctx.cfg, job.resultDir + '/' + epubName), null, GH_RAW);
    if (r.code !== 200) {
      job.downloadFails = (job.downloadFails || 0) + 1;
      if (r.code === 404 || job.downloadFails >= 6) return reviewFailed_(ctx, job, 'download ' + r.code);
      saveJobs_(ctx.jobs);
      reportGitHubProblem_(ctx, r, 'download');
      return 'waiting (download failed)';
    }
    var blob = r.resp.getBlob();
    blob.setName(job.title + (s.state === 'incomplete' ? INCOMPLETE_SUFFIX : '') + '.epub');
    blob.setContentType('application/epub+zip');
    job.epubId = folder.createFile(blob).getId();
    saveJobs_(ctx.jobs);
  }
  if (!keepOld && !job.reportId) {
    var rr = gh_(ctx.cfg, 'get', contentsPath_(ctx.cfg, job.resultDir + '/report.md'), null, GH_RAW);
    if (rr.code === 200) {
      try {
        job.reportId = folder.createFile(job.title + ' - تقرير.txt', ghText_(rr), 'text/plain').getId();
      } catch (err) {
        Logger.log('could not save the report of ' + job.fileId + ': ' + err);
      }
    }
  }
  if (keepOld) {
    job.epubId = job.oldEpubId || '';
    job.reportId = job.oldReportId || '';
  } else {
    [job.oldEpubId, job.oldReportId].forEach(function (id) {
      if (!id || id === job.epubId || id === job.reportId) return;
      try { DriveApp.getFileById(id).setTrashed(true); } catch (err) { Logger.log('trash ' + id + ': ' + err); }
    });
  }
  var before = job.unsure || 0;
  var unsure = pageList_(s.pages_unsure);
  job.stage = 'delivered';
  job.lastState = s.state;
  job.lastError = '';
  job.unsure = unsure.length;
  if (unsure.length) job.unsureList = shorten_(compressPages_(unsure), 300);
  clearReviewRound_(job);
  var rv = updateRv_(job, s);
  if (!rv) {
    // The workflow on GitHub does not know the review yet: do not prepare again.
    rv = { t: job.title, x: 'no-data' };
    saveRv_(job.fileId, rv);
  }
  var finalNow = !unsure.length;
  if (finalNow) finalize_(ctx, job, false);
  saveJobs_(ctx.jobs);

  var url = 'https://drive.google.com/file/d/' + job.epubId + '/view';
  if (prepare) {
    if (rv.o > 0) sendMail_('جاهز للمراجعة: ' + job.title, ['في «' + job.title + '» مواضع لم تقدر النماذج على حسمها.'].concat(reviewLines_(job, rv)));
    return 'prepared (' + (rv.o || 0) + ' places)';
  }
  var lines = [
    (judged ? 'طُبّقت أحكام نماذجك على «' : 'طُبّقت مراجعتك على «') + job.title + '»: ' +
      (Number(s.review_applied) || 0) + ' تصحيحاً و' + (Number(s.review_kept) || 0) + ' تأكيداً.',
    'الصفحات المشكوك فيها: ' + before + ' ← ' + unsure.length + '.'
  ];
  if (judged) lines.push('المواضع التي اختلفت فيها النماذج، أو احتاجت تصحيحاً كبيراً لم يتفق عليه نموذجان، بقيت لك.');
  if (Number(s.review_conflicts_new) > 0) {
    lines.push(s.review_conflicts_new + ' إجابة لم تُطبَّق لأن صفحتها تغيّرت منذ أن رأيتها؛ ستجد مواضعها من جديد في صفحة المراجعة.');
  }
  lines.push('النسخة الجديدة حلّت محل القديمة: ' + url);
  if (rv.o > 0) {
    lines = lines.concat(reviewLines_(job, rv));
  } else if (!unsure.length) {
    lines.push('لم تبقَ صفحات مشكوك فيها' + (finalNow ? '، ونقلت الكتاب إلى «' + CONFIG.FOLDERS.final + '».' : '.'));
    lines.push('يمكنك الآن حذف ملفه الأصلي من «' + CONFIG.FOLDERS.originals + '» إن احتجت المساحة.');
  }
  sendMail_((judged ? 'طُبّقت أحكام النماذج: ' : 'طُبّقت مراجعتك: ') + job.title, lines);
  return 'review delivered (' + before + ' -> ' + unsure.length + ')';
}

// ============================================================================
// Reading results
// ============================================================================

/** Returns the fresh status object, null when there is none, or 'error'. */
function readStatus_(ctx, job) {
  var r = gh_(ctx.cfg, 'get', contentsPath_(ctx.cfg, job.resultDir + '/status.json'), null, GH_RAW);
  if (r.code === 404) return null;
  if (r.code !== 200) {
    reportGitHubProblem_(ctx, r, 'status');
    return 'error';
  }
  var s = parseJson_(ghText_(r));
  if (!s || typeof s !== 'object' || !s.state) return null;
  // status.json of an earlier attempt stays in the folder until the new run replaces
  // it. Only a status written after this dispatch describes the current attempt.
  if (!(Date.parse(s.updated_at) >= Date.parse(job.dispatchedAt))) return null;
  return s;
}

/** true: a convert.yml run started after the dispatch is still going; null: unknown. */
function runInProgress_(ctx, job) {
  var r = gh_(ctx.cfg, 'get', '/repos/' + ctx.cfg.repo + '/actions/workflows/' + CONFIG.WORKFLOW + '/runs?per_page=10');
  if (r.code !== 200) {
    reportGitHubProblem_(ctx, r, 'runs');
    return null;
  }
  var data = parseJson_(ghText_(r));
  if (!data || !data.workflow_runs) return null;
  var since = Date.parse(job.dispatchedAt) - CONFIG.RUN_SKEW_MS;
  return data.workflow_runs.some(function (run) {
    return run && run.status !== 'completed' && Date.parse(run.created_at) >= since;
  });
}

function deliver_(ctx, job, s, incomplete) {
  if (job.humanRound) return deliverReview_(ctx, job, s);
  var epubName = String(s.epub || '').trim();
  if (!epubName) return failedOrGiveUp_(ctx, job, 'انتهى التحويل دون أن يُنتج ملف الكتاب.');

  // Each step records what it created, so a check that stops halfway (time limit,
  // Drive hiccup) continues without duplicating files.
  if (!job.epubId) {
    var r = gh_(ctx.cfg, 'get', contentsPath_(ctx.cfg, job.resultDir + '/' + epubName), null, GH_RAW);
    if (r.code === 404) return failedOrGiveUp_(ctx, job, 'انتهى التحويل لكن ملف الكتاب لم يُحفظ في النتائج.');
    if (r.code !== 200) {
      job.lastError = shorten_('download: ' + describeResponse_(r), 200);
      // A file too big for Apps Script (over 50 MB) fails forever: stop after a few tries
      // so the next books are not blocked.
      job.downloadFails = (job.downloadFails || 0) + 1;
      if (job.downloadFails >= 6) {
        return giveUp_(ctx, job, 'تعذّر تنزيل ملف الكتاب من النتائج (قد يكون أكبر من 50 ميجا). الملف موجود في فرع results في GitHub.');
      }
      saveJobs_(ctx.jobs);
      reportGitHubProblem_(ctx, r, 'download');
      return 'waiting (download failed)';
    }
    var blob = r.resp.getBlob();
    blob.setName(job.title + (incomplete ? INCOMPLETE_SUFFIX : '') + '.epub');
    blob.setContentType('application/epub+zip');
    var epub = ctx.folders.ready.createFile(blob);
    job.epubId = epub.getId();
    saveJobs_(ctx.jobs);
  }

  var report = '';
  var rr = gh_(ctx.cfg, 'get', contentsPath_(ctx.cfg, job.resultDir + '/report.md'), null, GH_RAW);
  if (rr.code === 200) report = ghText_(rr);
  if (report && !job.reportId) {
    try {
      var rf = ctx.folders.ready.createFile(job.title + ' - تقرير.txt', report, 'text/plain');
      job.reportId = rf.getId();
      saveJobs_(ctx.jobs);
    } catch (err) {
      Logger.log('could not save the report of ' + job.fileId + ': ' + err);
    }
  }

  moveFile_(job.fileId, ctx.folders.originals);
  // Its pages are all read: the files exchanged with the helper are not needed any more.
  if (!job.refining) dropBookFolder_(job.fileId);
  var unsure = pageList_(s.pages_unsure);
  var missing = pageList_(s.pages_missing);
  var url = 'https://drive.google.com/file/d/' + job.epubId + '/view';
  job.stage = 'delivered';
  job.lastState = s.state;
  job.lastError = '';
  job.unsure = unsure.length;
  delete job.rvDonePrev;
  var rv = updateRv_(job, s);

  if (job.refining) {
    // A refinement round: the new files replace the old ones.
    [job.oldEpubId, job.oldReportId].forEach(function (id) {
      if (!id) return;
      try { DriveApp.getFileById(id).setTrashed(true); } catch (err) { Logger.log('trash ' + id + ': ' + err); }
    });
    var before = job.unsureBefore || 0;
    job.refining = false;
    job.oldEpubId = '';
    job.oldReportId = '';
    // A round cut short by the judges' quota is not a sign the book cannot improve.
    var cutShort = pageList_(s.pages_judging).length > 0;
    job.unsureList = shorten_(compressPages_(unsure), 300);
    if (!unsure.length) {
      finalize_(ctx, job, true);
      return 'refined and final (' + before + ' -> 0)';
    }
    if (unsure.length >= before && !cutShort) {
      stopRefining_(ctx, job);
      return 'refined, judges done (' + before + ' -> ' + unsure.length + ')';
    }
    if (cutShort && unsure.length >= before) {
      // Nothing was judged (no quota left): this round does not count toward MAX_REFINES.
      job.refines = Math.max(0, (job.refines || 0) - 1);
    }
    saveJobs_(ctx.jobs);
    if (cutShort && unsure.length >= before) {
      sendMail_('ينتظر الحكّام: ' + job.title, [
        'لم يبقَ اليوم رصيد للحكّام لمراجعة «' + job.title + '»، ولم يتغير شيء.',
        'تكمل المراجعة تلقائياً في يوم قادم.'
      ]);
    }
    if (unsure.length < before) {
      sendMail_('تحسّن: ' + job.title, [
        'راجعت النماذج الأقوى صفحات مشكوكاً فيها في «' + job.title + '».',
        'الصفحات المشكوك فيها نزلت من ' + before + ' إلى ' + unsure.length + '.',
        'النسخة الجديدة حلّت محل القديمة: ' + url,
        unsure.length ? 'ستستمر المراجعة في الأيام القادمة إن بقي ما يمكن تحسينه.' : 'لم تبقَ صفحات مشكوك فيها.'
      ].concat(reviewLines_(job, rv)));
    }
    return 'refined (' + before + ' -> ' + unsure.length + ')';
  }

  job.finishedAt = now_().toISOString();
  // The judges already saw this book today; improvement rounds start tomorrow.
  job.refineDay = pacificDay_(now_());
  var folderName = CONFIG.FOLDERS.ready;
  if (!unsure.length && !incomplete) {
    finalize_(ctx, job, false);
    folderName = CONFIG.FOLDERS.final;
  }
  saveJobs_(ctx.jobs);
  var lines = [
    incomplete
      ? 'كتابك «' + job.title + '» جاهز، لكن فيه صفحات لم تُقرأ بعد ' + job.attempts + ' محاولات.'
      : 'كتابك «' + job.title + '» صار جاهزاً.',
    'افتحه من هنا: ' + url,
    'تجده أيضاً في مجلد «' + CONFIG.ROOT_NAME + ' / ' + folderName + '»، ومعه ملف التقرير.'
  ];
  if (s.pages_total) lines.push('عدد الصفحات: ' + s.pages_total + '.');
  if (incomplete && missing.length) {
    lines.push('الصفحات الناقصة (عليها علامة داخل الكتاب): ' + compressPages_(missing) + '.');
  }
  if (unsure.length) {
    lines.push('صفحات تستحق المراجعة (اختلفت قراءتان لها، وهي موجودة في الكتاب): ' + compressPages_(unsure) + '.');
  }
  lines = lines.concat(reviewLines_(job, rv));
  lines.push('نقلت الملف الأصلي إلى مجلد «' + CONFIG.FOLDERS.originals + '».');
  var short = shortReport_(report);
  if (short) {
    lines.push('');
    lines.push('التقرير المختصر:');
    lines = lines.concat(short.split('\n'));
  }
  sendMail_((incomplete ? 'جاهز مع صفحات ناقصة: ' : 'جاهز: ') + job.title, lines);
  return incomplete ? 'delivered (incomplete)' : 'delivered';
}

function giveUp_(ctx, job, reason) {
  if (!job.noteId) {
    var note = [
      'الكتاب: ' + job.name,
      'التاريخ: ' + formatLocal_(now_()),
      'السبب: ' + reason,
      '',
      'ماذا أفعل؟',
      '- للمحاولة مرة أخرى: انقل الملف الأصلي من هذا المجلد إلى «' + CONFIG.FOLDERS.inbox + '».',
      '- إذا تكررت المشكلة فقد يكون الملف تالفاً أو محمياً بكلمة مرور. افتحه على جهازك للتأكد.',
      '',
      'تفاصيل تقنية: ' + (job.lastError || job.lastState || '-') + ' / ' + job.fileId
    ].join('\n');
    try {
      var nf = ctx.folders.problem.createFile(job.title + ' - سبب المشكلة.txt', note, 'text/plain');
      job.noteId = nf.getId();
      saveJobs_(ctx.jobs);
    } catch (err) {
      Logger.log('could not write the problem note for ' + job.fileId + ': ' + err);
    }
  }
  moveFile_(job.fileId, ctx.folders.problem);
  dropBookFolder_(job.fileId);
  job.stage = 'problem';
  job.lastError = shorten_(reason, 200);
  job.finishedAt = now_().toISOString();
  saveJobs_(ctx.jobs);
  sendMail_('مشكلة في تحويل: ' + job.title, [
    'لم أستطع تحويل «' + job.title + '».',
    'السبب: ' + reason,
    'نقلت الملف الأصلي إلى مجلد «' + CONFIG.FOLDERS.problem + '»، ومعه ملف يشرح السبب.',
    'للمحاولة مرة أخرى: انقل الملف من «' + CONFIG.FOLDERS.problem + '» إلى «' + CONFIG.FOLDERS.inbox + '».',
    'المجلد: ' + ctx.folders.problem.getUrl()
  ]);
  return 'problem';
}

function mailPaused_(job) {
  var resume = pacificMidnightAfter_(job.pausedDay);
  var progress = job.pagesTotal ? ' (أُنجزت ' + (job.pagesDone || 0) + ' من ' + job.pagesTotal + ' صفحة)' : '';
  sendMail_('سيكمل غداً: ' + job.title, [
    'الكتاب «' + job.title + '» كبير، وانتهت حصة القراءة المجانية لهذا اليوم' + progress + '.',
    'ما أُنجز محفوظ، وسيكمل تلقائياً بعد منتصف الليل بتوقيت أمريكا، أي نحو الساعة ' +
      Utilities.formatDate(resume, scriptTz_(), 'HH:mm') + ' بتوقيتك.',
    'لا تحتاج أن تفعل شيئاً. ستصلك رسالة عندما يجهز.'
  ]);
}

// ============================================================================
// GitHub
// ============================================================================

function gh_(cfg, method, path, payload, accept) {
  var params = {
    method: method,
    muteHttpExceptions: true,
    headers: {
      Authorization: 'Bearer ' + cfg.token,
      Accept: accept || 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28'
    }
  };
  if (payload !== undefined && payload !== null) {
    params.contentType = 'application/json';
    params.payload = JSON.stringify(payload);
  }
  try {
    var resp = UrlFetchApp.fetch('https://api.github.com' + path, params);
    var code = resp.getResponseCode();
    var expiry = header_(resp, 'github-authentication-token-expiration');
    if (expiry) SEEN_TOKEN_EXPIRY = expiry;
    return { code: code, resp: resp };
  } catch (err) {
    // Network errors and timeouts. Never log params: they hold both tokens.
    Logger.log('GitHub ' + method.toUpperCase() + ' ' + path.split('?')[0] + ' failed: ' + err);
    return { code: 0, resp: null, error: String(err) };
  }
}

/** 'ok', 'disabled' (Drive API off for this project) or 'unknown'. */
function checkDriveApi_(fileId) {
  try {
    var resp = UrlFetchApp.fetch('https://www.googleapis.com/drive/v3/files/' + encodeURIComponent(fileId) + '?fields=id', {
      headers: { Authorization: 'Bearer ' + ScriptApp.getOAuthToken() },
      muteHttpExceptions: true
    });
    var code = resp.getResponseCode();
    if (code === 200) return 'ok';
    var text = resp.getContentText();
    Logger.log('Drive API check: HTTP ' + code + ' ' + shorten_(text, 200));
    if (code === 403 && /accessNotConfigured|SERVICE_DISABLED|has not been used|is disabled/i.test(text)) return 'disabled';
  } catch (err) {
    Logger.log('Drive API check failed: ' + err);
  }
  return 'unknown';
}

function contentsPath_(cfg, path) {
  var encoded = path.split('/').map(function (p) { return encodeURIComponent(p); }).join('/');
  return '/repos/' + cfg.repo + '/contents/' + encoded + '?ref=' + encodeURIComponent(CONFIG.RESULTS_BRANCH);
}

function ghText_(r) {
  try {
    return r && r.resp ? r.resp.getContentText('UTF-8') : '';
  } catch (err) {
    return '';
  }
}

function header_(resp, name) {
  try {
    var headers = resp.getHeaders() || {};
    var want = name.toLowerCase();
    for (var k in headers) {
      if (k.toLowerCase() === want) return String(headers[k]);
    }
  } catch (err) { /* headers are optional */ }
  return '';
}

function isAuthProblem_(r) {
  if (r.code === 401) return true;
  // 403 is also used for rate limits, which fix themselves.
  return r.code === 403 && !/rate limit/i.test(ghText_(r));
}

function describeResponse_(r) {
  if (!r.code) return 'no connection: ' + shorten_(r.error || '', 120);
  var msg = '';
  var data = parseJson_(ghText_(r));
  if (data && data.message) msg = ': ' + shorten_(String(data.message), 120);
  return 'HTTP ' + r.code + msg;
}

/** Emails the user (at most once a day per kind) when GitHub needs their attention. */
function reportGitHubProblem_(ctx, r, what) {
  Logger.log('GitHub problem during ' + what + ': ' + describeResponse_(r));
  if (isAuthProblem_(r)) {
    notifyOnce_('github-auth', 'تحويل الكتب: رمز GitHub يحتاج تجديداً', [
      'لم أستطع الوصول إلى GitHub: الرمز انتهت صلاحيته أو لا يملك الصلاحيات اللازمة.',
      'التحويل متوقف مؤقتاً، والكتب في أماكنها ولن تضيع.',
      '',
      'للإصلاح (نحو 5 دقائق):',
      '1. أنشئ رمزاً جديداً كما في الخطوة 1 من دليل الإعداد (أو افتح الرمز القديم واضغط Regenerate token).',
      '2. افتح مشروع «تحويل الكتب» في script.google.com ← إعدادات المشروع (⚙) ← Script properties، ' +
        'وضع الرمز الجديد مكان قيمة GITHUB_TOKEN ثم احفظ.',
      'سيكمل التحويل تلقائياً خلال ' + CONFIG.TICK_MINUTES + ' دقائق.',
      '',
      'تفاصيل تقنية: ' + describeResponse_(r)
    ]);
  } else if (what === 'dispatch' && r.code === 404) {
    notifyOnce_('github-404', 'تحويل الكتب: لم أجد سير عمل التحويل', [
      'لم أستطع بدء التحويل: GitHub يقول إن سير العمل ' + CONFIG.WORKFLOW + ' غير موجود في ' + ctx.cfg.repo +
        '، أو إن الرمز لا يملك صلاحية Actions.',
      'تأكد في الخطوة 1 من دليل الإعداد أن Actions = «Read and write».',
      'الكتاب ينتظر في «' + CONFIG.FOLDERS.working + '» وسأحاول مرة أخرى كل ' + CONFIG.TICK_MINUTES + ' دقائق.'
    ]);
  } else if (what === 'dispatch' && r.code === 422) {
    notifyOnce_('github-422', 'تحويل الكتب: سير العمل لا يقبل الكتب من درايف', [
      'رفض GitHub بدء التحويل. السبب غالباً أن ملف ' + CONFIG.WORKFLOW + ' في الفرع «' + ctx.cfg.ref +
        '» لا يحتوي بعد على مدخلات درايف (drive_file_id وغيرها).',
      'هذه مشكلة في إعداد المستودع وليست منك. الكتاب ينتظر في «' + CONFIG.FOLDERS.working +
        '» وسأحاول مرة أخرى كل ' + CONFIG.TICK_MINUTES + ' دقائق.',
      '',
      'تفاصيل تقنية: ' + describeResponse_(r)
    ]);
  }
}

/** Warns before the GitHub token expires: once two weeks ahead, once three days ahead. */
function rememberExpiry_() {
  if (!SEEN_TOKEN_EXPIRY) return;
  var props = props_();
  // GitHub sends e.g. "2027-10-19 10:00:00 UTC".
  var m = /^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})/.exec(SEEN_TOKEN_EXPIRY);
  if (!m) return;
  var iso = m[1] + 'T' + m[2] + 'Z';
  if (props.getProperty('TOKEN_EXPIRES') !== iso) props.setProperty('TOKEN_EXPIRES', iso);
  var daysLeft = Math.floor((Date.parse(iso) - now_().getTime()) / 86400000);
  if (daysLeft > 14) return;
  var stage = daysLeft <= 3 ? 'final' : 'early';
  var key = iso + '|' + stage;
  if (props.getProperty('NOTICE_EXPIRY') === key) return;
  props.setProperty('NOTICE_EXPIRY', key);
  sendMail_('تحويل الكتب: رمز GitHub تنتهي صلاحيته قريباً', [
    'تنتهي صلاحية رمز GitHub بعد ' + Math.max(0, daysLeft) + ' يوم (' + formatLocal_(new Date(Date.parse(iso))) + ').',
    'بعدها يتوقف التحويل حتى تضع رمزاً جديداً.',
    '',
    'للتجديد (نحو 5 دقائق):',
    '1. github.com ← الصورة الشخصية ← Settings ← Developer settings ← Personal access tokens ← ' +
      'Fine-grained tokens ← اضغط «تحويل الكتب» ← Regenerate token، ثم انسخ الرمز.',
    '2. script.google.com ← مشروع «تحويل الكتب» ← إعدادات المشروع (⚙) ← Script properties، ' +
      'وضع الرمز الجديد مكان قيمة GITHUB_TOKEN ثم احفظ.'
  ]);
}

// ============================================================================
// The helper on the user's computer (docs/helper.md)
// ============================================================================

/**
 * For the page: the install line, whether the helper was seen lately, what it shows
 * (its own lines and buttons, from helper-status.json) and the requests it has not
 * finished. The first call creates the folder and copies the helper's files.
 */
function helperInfo_(now) {
  try {
    var props = props_();
    if (!props.getProperty('HELPER_INSTALL_ID')) {
      var lock = LockService.getUserLock();
      if (!lock.tryLock(20000)) return { error: 'مشغول، حدّث الصفحة بعد دقيقة.' };
      try {
        writeRequests_();
        syncHelperFiles_(true);
      } finally {
        lock.releaseLock();
      }
    }
    var status = readHelperStatus_();
    var at = status ? Date.parse(status.at) : NaN;
    var handled = status && Array.isArray(status.handled) ? status.handled : [];
    var info = {
      line: installLine_(),
      seen: isFinite(at) ? status.at : '',
      minutes: isFinite(at) ? Math.max(0, Math.floor((now.getTime() - at) / 60000)) : 0,
      connected: isFinite(at) && now.getTime() - at < CONFIG.HELPER_STALE_MS,
      busy: status ? String(status.busy || '') : '',
      lines: (status && Array.isArray(status.lines) ? status.lines.slice(0, 20) : []).map(function (l) {
        l = l && typeof l === 'object' ? l : { text: String(l) };
        return { text: shorten_(String(l.text || ''), 500), kind: /^(warn|bad)$/.test(l.kind) ? l.kind : '',
          link: l.path ? helperPathUrl_(String(l.path)) : '', linkText: shorten_(String(l.linkText || ''), 40) };
      }),
      actions: status && Array.isArray(status.actions) ? status.actions.slice(0, 10) : [],
      pending: helperRequests_().filter(function (r) { return handled.indexOf(r.id) < 0; }),
      // The boxes on the page: the models found on the computer, where the user put them,
      // and what each is doing.
      models: (status && Array.isArray(status.models) ? status.models : []).filter(function (m) {
        return m && CHIP_RE.test(String(m.id));
      }).slice(0, 40).map(function (m) {
        return { id: String(m.id), label: shorten_(String(m.label || m.id), 80), tool: shorten_(String(m.tool || ''), 30),
          warn: shorten_(String(m.warn || ''), 120) };
      }),
      boxes: helperBoxes_(),
      chips: {},
      progress: {}
    };
    var chips = status && status.chips && typeof status.chips === 'object' ? status.chips : {};
    Object.keys(chips).slice(0, 40).forEach(function (id) {
      var c = chips[id];
      if (!CHIP_RE.test(id) || !c || typeof c !== 'object') return;
      info.chips[id] = { state: /^(working|idle|exhausted|failing)$/.test(c.state) ? c.state : 'idle',
        text: shorten_(String(c.text || ''), 160) };
    });
    var books = status && status.books && typeof status.books === 'object' ? status.books : {};
    Object.keys(books).slice(0, 50).forEach(function (id) {
      var b = books[id];
      if (FILE_ID_RE.test(id) && b && typeof b === 'object') {
        info.progress[id] = { need: Number(b.need) || 0, finished: Number(b.finished) || 0 };
      }
    });
    if (!info.line) info.error = 'لم أستطع تجهيز ملف التثبيت بعد، أعد فتح الصفحة بعد دقائق.';
    return info;
  } catch (err) {
    Logger.log('helperInfo_ failed: ' + (err && err.stack ? err.stack : err));
    return { error: String(err && err.message ? err.message : err) };
  }
}

/** Every check: drop the requests the helper finished, and refresh its files. */
function helperTick_() {
  var props = props_();
  if (!props.getProperty('HELPER_INSTALL_ID')) return;
  var lock = LockService.getUserLock();
  if (!lock.tryLock(20000)) return;
  try {
    var status = readHelperStatus_();
    var handled = status && Array.isArray(status.handled) ? status.handled : [];
    var reqs = helperRequests_();
    var left = reqs.filter(function (r) { return handled.indexOf(r.id) < 0; });
    if (left.length !== reqs.length) {
      props.setProperty('HELPER_REQS', JSON.stringify(left));
      writeRequests_();
    }
    syncHelperFiles_(false);
    syncHelperReviews_(config_());
  } finally {
    lock.releaseLock();
  }
}

// ----------------------------------------------------------------------------
// The boxes: models the user put to work from the page (docs/helper-boxes-plan.md)
// ----------------------------------------------------------------------------

/** {transcribe: [ids], review: [ids]}: the models in the left box of each section. */
function helperBoxes_() {
  var v = parseJson_(props_().getProperty('HELPER_BOXES')) || {};
  var clean = function (list) {
    list = Array.isArray(list) ? list.map(String) : [];
    return list.filter(function (id, i) { return CHIP_RE.test(id) && list.indexOf(id) === i; })
      .slice(0, CONFIG.HELPER_MAX_CHIPS);
  };
  var out = { transcribe: clean(v.transcribe), review: clean(v.review) };
  out.times = cleanTimes_(v.times, out.transcribe.concat(out.review));
  return out;
}

/** {id: {start, stop}} for the models in the boxes: start = not before, stop = not after ('' = none). */
function cleanTimes_(times, ids) {
  var out = {};
  var iso = function (v) { return typeof v === 'string' && isFinite(Date.parse(v)) ? new Date(Date.parse(v)).toISOString() : ''; };
  if (!times || typeof times !== 'object') return out;
  ids.forEach(function (id) {
    var t = times[id];
    if (!t || typeof t !== 'object') return;
    var w = { start: iso(t.start), stop: iso(t.stop) };
    if (w.start || w.stop) out[id] = w;
  });
  return out;
}

/** The helper's status file, read once per check. */
function helperStatusOf_(ctx) {
  if (ctx.helperStatus === undefined) {
    ctx.helperStatus = props_().getProperty('HELPER_INSTALL_ID') ? readHelperStatus_() : null;
  }
  return ctx.helperStatus;
}

/**
 * True when the helper's models give the second readings: some of the models in the box
 * are on the computer, and the helper was seen within the hour.
 */
function helperTranscribes_(status) {
  var all = helperBoxes_();
  var boxes = all.transcribe;
  var at = status ? Date.parse(status.at) : NaN;
  if (!boxes.length || !isFinite(at) || now_().getTime() - at > CONFIG.HELPER_GONE_MS) return false;
  var found = (Array.isArray(status.models) ? status.models : []).map(function (m) { return m && String(m.id); });
  // A model whose stop time has passed does no more work; one that starts later will.
  return boxes.some(function (id) {
    var stop = Date.parse((all.times[id] || {}).stop || '');
    return found.indexOf(id) >= 0 && !(stop <= now_().getTime());
  });
}

/** The helper's work on a book's pages: {need, finished, bookAt} (bookAt: when the run that listed them wrote book.json). */
function helperProgress_(status, fileId) {
  var b = status && status.books && typeof status.books === 'object' ? status.books[fileId] : null;
  if (!b || typeof b !== 'object') return null;
  return { need: Number(b.need) || 0, finished: Number(b.finished) || 0, bookAt: String(b.book_at || '') };
}

/**
 * The helper has finished every page the book's last run left to it, as seen by two
 * checks HELPER_DONE_WAIT_MS apart (its last files need a moment to reach Drive).
 */
function helperDoneFor_(ctx, job) {
  var p = helperProgress_(helperStatusOf_(ctx), job.fileId);
  var done = !!p && p.need > 0 && p.finished >= p.need && Date.parse(p.bookAt) >= Date.parse(job.dispatchedAt || '');
  if (!done) {
    if (job.helperDoneSeen) {
      delete job.helperDoneSeen;
      saveJobs_(ctx.jobs);
    }
    return false;
  }
  if (!job.helperDoneSeen) {
    job.helperDoneSeen = now_().toISOString();
    saveJobs_(ctx.jobs);
    return false;
  }
  return now_().getTime() - Date.parse(job.helperDoneSeen) >= CONFIG.HELPER_DONE_WAIT_MS;
}

/**
 * A book parked for the helper runs again when the helper has finished its pages; or,
 * with the free model doing the rest, when the helper is off (the box was emptied, or the
 * computer has not been seen for an hour) or has not moved for hours.
 */
function helperNext_(ctx) {
  var parked = Object.keys(ctx.jobs).map(function (id) { return ctx.jobs[id]; })
    .filter(function (j) { return j && j.stage === 'helper'; })
    .sort(function (a, b) { return String(a.helperAt) < String(b.helperAt) ? -1 : 1; });
  if (!parked.length) return '';
  var status = helperStatusOf_(ctx);
  var on = helperTranscribes_(status);
  var now = now_().getTime();
  for (var i = 0; i < parked.length; i++) {
    var j = parked[i];
    var p = helperProgress_(status, j.fileId);
    if (p && Date.parse(p.bookAt) >= Date.parse(j.dispatchedAt || '') && p.finished > (j.helperFinished || 0)) {
      j.helperFinished = p.finished;
      j.helperMovedAt = new Date(now).toISOString();
      saveJobs_(ctx.jobs);
    }
    var why = !on ? 'helper off'
      : helperDoneFor_(ctx, j) ? 'helper done'
      : now - Date.parse(j.helperMovedAt || j.helperAt) > CONFIG.HELPER_STALL_MS ? 'helper stalled' : '';
    if (!why) continue;
    j.stage = 'queued';
    j.pendingCount = false;
    // Off or stalled: the free model does the rest (transcribing would only park it again).
    if (why !== 'helper done') j.helperOff = true;
    saveJobs_(ctx.jobs);
    return 'helper ' + j.fileId + ' (' + why + '): ' + dispatch_(ctx, j, false);
  }
  return '';
}

/**
 * The run's helper input: the book's folder, with ":transcribe" when the helper's models
 * give the second readings (the folder is made then), or ":import" so their work so far
 * is used. Nothing for review and improvement rounds, which read nothing.
 */
function helperInput_(ctx, job) {
  if (job.humanRound || job.refining || !props_().getProperty('HELPER_INSTALL_ID')) return '';
  var on = extOf_(job.driveName || job.name) === 'pdf' && !job.helperOff && helperTranscribes_(helperStatusOf_(ctx));
  var f = bookFolder_(job.fileId, on);
  return f ? f.getId() + (on ? ':transcribe' : ':import') : '';
}

/** A folder in the helper's folder ("كتب", "مراجعة", "أحكام"), made when create is true. */
function helperSub_(name, create) {
  var root = folderFromProp_('HELPER_FOLDER');
  if (!root) return null;
  var it = root.getFoldersByName(name);
  while (it.hasNext()) {
    var f = it.next();
    if (!f.isTrashed()) return f;
  }
  return create ? root.createFolder(name) : null;
}

function liveFolder_(parent, name) {
  var it = parent.getFoldersByName(name);
  while (it.hasNext()) {
    var f = it.next();
    if (!f.isTrashed()) return f;
  }
  return null;
}

/** The book's folder for the helper's readings ("كتب/<fileId>"). */
function bookFolder_(fileId, create) {
  var parent = helperSub_(CONFIG.HELPER_BOOKS, create);
  if (!parent) return null;
  return liveFolder_(parent, fileId) || (create ? parent.createFolder(fileId) : null);
}

function dropBookFolder_(fileId) {
  try {
    var f = bookFolder_(fileId, false);
    if (f) f.setTrashed(true);
  } catch (err) {
    Logger.log('could not remove the helper folder of ' + fileId + ': ' + err);
  }
}

/** The verdicts of the helper's models on a book's unsure places ("أحكام/<fileId>.json"), or null. */
function verdictsFile_(fileId) {
  var dir = helperSub_(CONFIG.HELPER_VERDICTS, false);
  if (!dir) return null;
  return liveFiles_(dir, fileId + '.json').sort(function (a, b) {
    return b.getLastUpdated().getTime() - a.getLastUpdated().getTime();
  })[0] || null;
}

/**
 * A delivered book whose places the helper's models have finished judging, with verdicts
 * no run has applied yet: a review round applies them, with no model request.
 */
function helperJudgeNext_(ctx) {
  if (!helperBoxes_().review.length) return '';
  var pick = null;
  reviewIds_().forEach(function (id) {
    var rv = loadRv_(id);
    var j = ctx.jobs[id];
    if (!rv || rv.x || !j || j.stage !== 'delivered' || bookGone_(id, rv.e)) return;
    var f = verdictsFile_(id);
    var data = f ? parseJson_(f.getBlob().getDataAsString('UTF-8')) : null;
    if (!data || data.done !== true || !data.answers || typeof data.answers !== 'object') return;
    var n = Object.keys(data.answers).length;
    if (n > (rv.jn || 0) && (!pick || String(j.finishedAt) < String(pick.job.finishedAt))) {
      pick = { job: j, rv: rv, n: n };
    }
  });
  if (!pick) return '';
  pick.rv.jn = pick.n;
  saveRv_(pick.job.fileId, pick.rv);
  pick.job.judgeRound = true;
  return startReviewRound_(ctx, pick.job, false);
}

/**
 * With models in the review box: the open places of delivered books (review/ in the
 * results branch) are copied for the helper into "مراجعة/<fileId>", a few files per
 * check. The folders of books with nothing open, or deleted, go at every check, also
 * while the box is empty: a model put in it later must not start on them.
 */
function syncHelperReviews_(cfg) {
  var boxed = helperBoxes_().review.length > 0;
  var parent = helperSub_(CONFIG.HELPER_REVIEWS, boxed);
  if (!parent) return;
  var open = dropHelperReviews_(parent);
  if (!boxed) return;
  var jobs = loadJobs_();
  var budget = CONFIG.HELPER_REVIEW_FILES;
  Object.keys(open).forEach(function (id) {
    var j = jobs[id];
    // Copied from a delivered book only: a running round is about to change its places.
    if (budget > 0 && (!j || j.stage === 'delivered')) budget -= syncReviewFolder_(cfg, id, open[id], parent, budget);
  });
}

/**
 * Removes the helper's folders of books with nothing open, or that the user deleted (a
 * round running on a book keeps its folder, and the verdicts in it). Returns the books
 * open for review: {fileId: rv}.
 */
function dropHelperReviews_(parent) {
  var open = {};
  reviewIds_().forEach(function (id) {
    var rv = loadRv_(id);
    if (rv && rv.o > 0 && !rv.x && !bookGone_(id, rv.e)) open[id] = rv;
  });
  var it = parent.getFolders();
  while (it.hasNext()) {
    var f = it.next();
    if (!f.isTrashed() && !open[f.getName()]) f.setTrashed(true);
  }
  return open;
}

/** One book's items.json and place images; returns how many files it fetched. */
function syncReviewFolder_(cfg, id, rv, parent, budget) {
  var r = gh_(cfg, 'get', '/repos/' + cfg.repo + '/contents/' + encodeURIComponent('drive-' + id) +
    '/review?ref=' + encodeURIComponent(CONFIG.RESULTS_BRANCH));
  var list = r.code === 200 ? parseJson_(ghText_(r)) : null;
  if (!Array.isArray(list)) return 0;
  var folder = liveFolder_(parent, id) || parent.createFolder(id);
  var want = {};
  var used = 0;
  list.forEach(function (item) {
    var name = item && item.type === 'file' ? String(item.name) : '';
    if (name !== 'items.json' && !IMAGE_RE.test(name)) return;
    want[name] = true;
    var have = liveFiles_(folder, name);
    // Image names carry a hash of their content; items.json changes with every run.
    if (have.length && (name !== 'items.json' || rv.hs === item.sha)) return;
    if (used >= budget) return;
    used++;
    var f = gh_(cfg, 'get', contentsPath_(cfg, 'drive-' + id + '/review/' + name), null, GH_RAW);
    if (f.code !== 200) return;
    have.forEach(function (old) { old.setTrashed(true); });
    folder.createFile(Utilities.newBlob(f.resp.getContent(), name === 'items.json' ? 'application/json' : 'image/jpeg', name));
    if (name === 'items.json') {
      rv.hs = item.sha;
      saveRv_(id, rv);
    }
  });
  // Images of places that are gone.
  var files = folder.getFiles();
  while (files.hasNext()) {
    var x = files.next();
    if (!x.isTrashed() && IMAGE_RE.test(x.getName()) && !want[x.getName()]) x.setTrashed(true);
  }
  return used;
}

function helperRequests_() {
  var v = parseJson_(props_().getProperty('HELPER_REQS'));
  return Array.isArray(v) ? v : [];
}

function helperFolder_() {
  var f = folderFromProp_('HELPER_FOLDER');
  if (!f) {
    f = findOrCreateFolder_(getFolders_().root, CONFIG.HELPER_FOLDER);
    props_().setProperty('HELPER_FOLDER', f.getId());
  }
  return f;
}

/** A file stored by id in `prop`; null when missing. */
function fileFromProp_(prop) {
  var id = props_().getProperty(prop);
  if (!id) return null;
  try {
    var f = DriveApp.getFileById(id);
    return f.isTrashed() ? null : f;
  } catch (err) {
    return null;
  }
}

/** The helper's to-do list, with the books it may be asked about. */
function writeRequests_() {
  var jobs = loadJobs_();
  var text = JSON.stringify({
    v: 1,
    at: now_().toISOString(),
    requests: helperRequests_(),
    boxes: helperBoxes_(),
    books: Object.keys(jobs).map(function (id) {
      var j = jobs[id];
      return j ? { id: id, title: j.title || '', stage: j.stage || '', unsure: j.unsure || 0 } : null;
    }).filter(function (b) { return b; }).slice(0, 50)
  });
  var f = fileFromProp_('HELPER_REQUESTS_ID');
  if (f) {
    f.setContent(text);
  } else {
    f = helperFolder_().createFile(CONFIG.HELPER_REQUESTS, text, 'text/plain');
    props_().setProperty('HELPER_REQUESTS_ID', f.getId());
  }
}

/**
 * Copies the helper's program (helper/ in the repository) and the test pages
 * (compare/pages in the results branch) into the helper folder, where Drive for desktop
 * brings them to the computer; only files whose GitHub sha changed are copied.
 * install.ps1 also goes to a file anyone with its link can download: it is the program's
 * first step and holds no secret.
 */
function syncHelperFiles_(force) {
  var props = props_();
  var last = Date.parse(props.getProperty('HELPER_SYNC_AT') || '');
  if (!force && isFinite(last) && now_().getTime() - last < CONFIG.HELPER_SYNC_MS) return;
  var cfg = config_();
  var shas = parseJson_(props.getProperty('HELPER_SHAS')) || {};
  var folder = helperFolder_();
  var ok = syncDir_(cfg, 'helper', cfg.ref, findOrCreateFolder_(folder, CONFIG.HELPER_PROGRAM), shas);
  ok = syncDir_(cfg, 'compare/pages', CONFIG.RESULTS_BRANCH, findOrCreateFolder_(folder, CONFIG.HELPER_TEST_PAGES), shas) && ok;
  props.setProperty('HELPER_SHAS', JSON.stringify(shas));
  if (ok) props.setProperty('HELPER_SYNC_AT', now_().toISOString());
}

function syncDir_(cfg, dir, ref, folder, shas) {
  var r = gh_(cfg, 'get', '/repos/' + cfg.repo + '/contents/' + dir + '?ref=' + encodeURIComponent(ref));
  var list = r.code === 200 ? parseJson_(ghText_(r)) : null;
  if (!Array.isArray(list)) {
    Logger.log('helper files ' + dir + ': ' + describeResponse_(r));
    return false;
  }
  var ok = true;
  list.forEach(function (item) {
    if (!item || item.type !== 'file' || !/^[\w.-]{1,80}$/.test(String(item.name))) return;
    var key = ref + ':' + item.path;
    var install = dir === 'helper' && item.name === CONFIG.HELPER_INSTALL;
    var existing = install ? [fileFromProp_('HELPER_INSTALL_ID')].filter(function (f) { return f; })
      : liveFiles_(folder, item.name);
    if (shas[key] === item.sha && existing.length) return;
    var f = gh_(cfg, 'get', '/repos/' + cfg.repo + '/contents/' + item.path + '?ref=' + encodeURIComponent(ref), null, GH_RAW);
    if (f.code !== 200) {
      Logger.log('helper file ' + item.path + ': ' + describeResponse_(f));
      ok = false;
      return;
    }
    if (install) {
      if (existing.length) {
        existing[0].setContent(ghText_(f));
      } else {
        var pub = helperFolder_().createFile(item.name, ghText_(f), 'text/plain');
        pub.setSharing(DriveApp.Access.ANYONE_WITH_LINK, DriveApp.Permission.VIEW);
        props_().setProperty('HELPER_INSTALL_ID', pub.getId());
      }
    } else {
      existing.forEach(function (old) { old.setTrashed(true); });
      var mime = /\.png$/i.test(item.name) ? 'image/png' : /\.jpe?g$/i.test(item.name) ? 'image/jpeg' : 'text/plain';
      folder.createFile(Utilities.newBlob(f.resp.getContent(), mime, item.name));
    }
    shas[key] = item.sha;
  });
  return ok;
}

function liveFiles_(folder, name) {
  var out = [];
  var it = folder.getFilesByName(name);
  while (it.hasNext()) {
    var f = it.next();
    if (!f.isTrashed()) out.push(f);
  }
  return out;
}

/**
 * The one line the user pastes into PowerShell, or '' before the install file exists.
 * drive.google.com/uc answers a .ps1 file with a "virus scan warning" page; this address
 * with confirm=t gives the file itself.
 */
function installLine_() {
  var id = props_().getProperty('HELPER_INSTALL_ID');
  if (!id) return '';
  return "[Net.ServicePointManager]::SecurityProtocol='Tls12';iex ([Text.Encoding]::UTF8.GetString(" +
    "(New-Object Net.WebClient).DownloadData('https://drive.usercontent.google.com/download?id=" + id +
    "&export=download&confirm=t')))";
}

/** The helper's own status file (the newest, if Drive for desktop left two). */
function readHelperStatus_() {
  var f = folderFromProp_('HELPER_FOLDER');
  if (!f) return null;
  var files = liveFiles_(f, CONFIG.HELPER_STATUS).sort(function (a, b) {
    return b.getLastUpdated().getTime() - a.getLastUpdated().getTime();
  });
  return files.length ? parseJson_(files[0].getBlob().getDataAsString('UTF-8')) : null;
}

/** A link to a folder or file inside the helper folder, from its path ("نتائج/…"). */
function helperPathUrl_(path) {
  try {
    var item = folderFromProp_('HELPER_FOLDER');
    var parts = path.split('/').filter(function (p) { return p; }).slice(0, 5);
    for (var i = 0; item && i < parts.length; i++) {
      var sub = item.getFoldersByName(parts[i]);
      var next = null;
      while (sub.hasNext() && !next) { var x = sub.next(); if (!x.isTrashed()) next = x; }
      if (!next && i === parts.length - 1) next = liveFiles_(item, parts[i])[0] || null;
      item = next;
    }
    return item ? item.getUrl() : '';
  } catch (err) {
    return '';
  }
}

// ============================================================================
// Drive
// ============================================================================

function ensureFolders_() {
  var props = props_();
  var root = folderFromProp_('FOLDER_ROOT') || findOrCreateFolder_(DriveApp.getRootFolder(), CONFIG.ROOT_NAME);
  props.setProperty('FOLDER_ROOT', root.getId());
  var out = { root: root };
  Object.keys(CONFIG.FOLDERS).forEach(function (key) {
    var f = findOrCreateFolder_(root, CONFIG.FOLDERS[key]);
    props.setProperty(folderProp_(key), f.getId());
    out[key] = f;
  });
  return out;
}

/** The folders, recreated if one was deleted. */
function getFolders_() {
  return existingFolders_() || ensureFolders_();
}

/** The folders, or null if one is missing (never creates anything). */
function existingFolders_() {
  var out = { root: folderFromProp_('FOLDER_ROOT') };
  if (!out.root) return null;
  var keys = Object.keys(CONFIG.FOLDERS);
  for (var i = 0; i < keys.length; i++) {
    out[keys[i]] = folderFromProp_(folderProp_(keys[i]));
    if (!out[keys[i]]) return null;
  }
  return out;
}

function folderProp_(key) {
  return 'FOLDER_' + key.toUpperCase();
}

function folderFromProp_(prop) {
  var id = props_().getProperty(prop);
  if (!id) return null;
  try {
    var f = DriveApp.getFolderById(id);
    return f.isTrashed() ? null : f;
  } catch (err) {
    return null;
  }
}

function findOrCreateFolder_(parent, name) {
  var it = parent.getFoldersByName(name);
  while (it.hasNext()) {
    var f = it.next();
    if (!f.isTrashed()) return f;
  }
  return parent.createFolder(name);
}

/** Books in "1- جديد": ready ones (oldest first), ones still uploading, unsupported names. */
function scanInbox_(folder) {
  var res = { ready: [], uploading: 0, unsupported: [] };
  var now = now_().getTime();
  var found = [];
  var it = folder.getFiles();
  var seen = 0;
  while (it.hasNext() && seen < CONFIG.MAX_SCAN) {
    var f = it.next();
    seen++;
    try {
      if (f.isTrashed()) continue;
      if (!isSupported_(f)) {
        res.unsupported.push(f.getName());
        continue;
      }
      if (f.getSize() === 0 || now - f.getLastUpdated().getTime() < CONFIG.SETTLE_MS) {
        res.uploading++;
        continue;
      }
      found.push({ file: f, created: f.getDateCreated().getTime(), name: f.getName() });
    } catch (err) {
      Logger.log('skipping an inbox file: ' + err);
    }
  }
  found.sort(function (a, b) {
    return a.created - b.created || (a.name < b.name ? -1 : a.name > b.name ? 1 : 0);
  });
  res.ready = found.map(function (x) { return x.file; });
  return res;
}

function findOrphan_(ctx) {
  var it = ctx.folders.working.getFiles();
  var seen = 0;
  while (it.hasNext() && seen < 50) {
    var f = it.next();
    seen++;
    try {
      if (!f.isTrashed() && isSupported_(f) && !ctx.jobs[f.getId()]) return f;
    } catch (err) { /* ignore */ }
  }
  return null;
}

function isSupported_(file) {
  var mime = String(file.getMimeType() || '');
  // Google Docs, shortcuts and other Drive-native items have no file content to send.
  if (mime.indexOf('application/vnd.google-apps') === 0) return false;
  return !!SUPPORTED_EXT[extOf_(file.getName())] || !!MIME_EXT[mime];
}

function extOf_(name) {
  var m = /\.([^.\/\\]+)$/.exec(String(name || ''));
  return m ? m[1].toLowerCase() : '';
}

function moveFile_(fileId, folder) {
  try {
    DriveApp.getFileById(fileId).moveTo(folder);
    return true;
  } catch (err) {
    Logger.log('could not move ' + fileId + ': ' + err);
    return false;
  }
}

// ============================================================================
// Jobs (Script Property JOBS)
// ============================================================================

function loadJobs_() {
  var obj = parseJson_(props_().getProperty('JOBS'));
  return obj && typeof obj === 'object' && !Array.isArray(obj) ? obj : {};
}

function saveJobs_(jobs) {
  pruneJobs_(jobs);
  props_().setProperty('JOBS', JSON.stringify(jobs));
}

function isActive_(job) {
  return job.stage === 'queued' || job.stage === 'running';
}

/** Waiting in "2- قيد التحويل" without holding the slot: for the judges, the helper or a reader. */
function isParked_(job) {
  return job.stage === 'judging' || job.stage === 'helper' || job.stage === 'readerWait';
}

function activeJob_(jobs) {
  var best = null;
  Object.keys(jobs).forEach(function (id) {
    var j = jobs[id];
    if (j && isActive_(j) && (!best || String(j.createdAt) < String(best.createdAt))) best = j;
  });
  return best;
}

function finishedJobs_(jobs) {
  return Object.keys(jobs).map(function (id) { return jobs[id]; })
    .filter(function (j) { return j && !isActive_(j); })
    .sort(function (a, b) {
      var x = String(a.finishedAt || ''), y = String(b.finishedAt || '');
      return x === y ? 0 : (x > y ? -1 : 1);   // newest first
    });
}

/** Keeps JOBS small: finished jobs are trimmed to what the page needs, oldest dropped. */
function pruneJobs_(jobs) {
  // Parked books (judging, helper) still need all their fields and must never be dropped.
  var finished = finishedJobs_(jobs).filter(function (j) { return !isParked_(j); });
  finished.forEach(function (j) {
    ['name', 'driveName', 'resultDir', 'dispatchedAt', 'createdAt', 'pausedDay', 'pausedAt',
      'pendingCount', 'noteId', 'pauseMailed', 'pagesDone', 'pagesTotal', 'pauses',
      'judgeDay', 'judgeRounds', 'judgeMailed', 'rvDonePrev', 'downloadFails',
      'helperAt', 'helperMovedAt', 'helperFinished', 'helperPages', 'helperDoneSeen', 'helperOff',
      'readerDay', 'readerWaits', 'readerMailed']
      .forEach(function (k) { delete j[k]; });
    if (j.lastError) j.lastError = shorten_(j.lastError, 120);
  });
  for (var i = finished.length - 1; i >= CONFIG.MAX_FINISHED_JOBS; i--) {
    delete jobs[finished[i].fileId];
    finished.pop();
  }
  while (finished.length && utf8Length_(JSON.stringify(jobs)) > CONFIG.JOBS_MAX_BYTES) {
    delete jobs[finished.pop().fileId];
  }
}

function jobUrl_(job) {
  if (job.stage === 'delivered' && job.epubId) return 'https://drive.google.com/file/d/' + job.epubId + '/view';
  if (job.stage === 'problem') return 'https://drive.google.com/file/d/' + job.fileId + '/view';
  return '';
}

// ============================================================================
// Email
// ============================================================================

function ownerEmail_() {
  var custom = props_().getProperty('NOTIFY_EMAIL');
  if (custom) return custom.trim();
  try {
    return Session.getEffectiveUser().getEmail() || '';
  } catch (err) {
    return '';
  }
}

function sendMail_(subject, lines) {
  var to = ownerEmail_();
  if (!to) {
    Logger.log('no email address for "' + subject + '"');
    return false;
  }
  var html = '<div dir="rtl" style="font-family:Tahoma,Arial,sans-serif;font-size:15px;line-height:1.8">' +
    lines.map(function (l) { return linkify_(escapeHtml_(l)); }).join('<br>') + '</div>';
  try {
    MailApp.sendEmail({ to: to, subject: subject, body: lines.join('\n'), htmlBody: html, name: CONFIG.ROOT_NAME });
    return true;
  } catch (err) {
    Logger.log('sendEmail failed for "' + subject + '": ' + err);
    return false;
  }
}

/** Sends a message at most once per local day for each kind of problem. */
function notifyOnce_(kind, subject, lines) {
  var props = props_();
  var key = 'NOTICE_' + kind;
  var today = Utilities.formatDate(now_(), scriptTz_(), 'yyyy-MM-dd');
  if (props.getProperty(key) === today) return false;
  props.setProperty(key, today);
  return sendMail_(subject, lines);
}

function shortReport_(report) {
  if (!report) return '';
  var text = report.replace(/\r/g, '').split('\n')
    .map(function (l) { return l.replace(/^#+\s*/, '').replace(/\*\*/g, '').trimEnd(); })
    .join('\n').replace(/\n{3,}/g, '\n\n').trim();
  if (text.length > CONFIG.REPORT_EMAIL_CHARS) {
    text = text.slice(0, CONFIG.REPORT_EMAIL_CHARS).replace(/\n[^\n]*$/, '') + '\n… (البقية في ملف التقرير)';
  }
  return text;
}

// ============================================================================
// Small helpers
// ============================================================================

/** Overridden by the tests. */
function now_() {
  return new Date();
}

function props_() {
  return PropertiesService.getScriptProperties();
}

function config_() {
  var p = props_();
  var repo = String(p.getProperty('GITHUB_REPO') || CONFIG.DEFAULT_REPO).trim()
    .replace(/^https?:\/\/github\.com\//, '').replace(/\.git$/, '').replace(/\/+$/, '');
  return {
    token: String(p.getProperty('GITHUB_TOKEN') || '').trim(),
    repo: repo,
    ref: String(p.getProperty('GITHUB_REF') || p.getProperty('DEFAULT_REF') || 'main').trim()
  };
}

function scriptTz_() {
  try {
    return Session.getScriptTimeZone() || 'Asia/Riyadh';
  } catch (err) {
    return 'Asia/Riyadh';
  }
}

function formatLocal_(date) {
  return Utilities.formatDate(date, scriptTz_(), 'yyyy-MM-dd HH:mm');
}

function pacificDay_(date) {
  return Utilities.formatDate(date, CONFIG.QUOTA_TZ, 'yyyy-MM-dd');
}

function isDay_(s) {
  return typeof s === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(s);
}

/** The moment the Pacific day after `day` (yyyy-MM-dd) starts. */
function pacificMidnightAfter_(day) {
  var p = String(day).split('-').map(Number);
  var next = Utilities.formatDate(new Date(Date.UTC(p[0], p[1] - 1, p[2] + 1, 12)), 'UTC', 'yyyy-MM-dd');
  // Pacific midnight is 07:00 UTC in summer (PDT) and 08:00 UTC in winter (PST).
  for (var h = 7; h <= 8; h++) {
    var t = new Date(Date.UTC(p[0], p[1] - 1, p[2] + 1, h));
    if (Utilities.formatDate(t, CONFIG.QUOTA_TZ, 'yyyy-MM-dd HH:mm') === next + ' 00:00') return t;
  }
  return new Date(Date.UTC(p[0], p[1] - 1, p[2] + 1, 8));
}

function parseJson_(text) {
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch (err) {
    return null;
  }
}

function pageList_(v) {
  if (!Array.isArray(v)) return [];
  return v.map(Number).filter(function (n) { return isFinite(n) && n > 0; });
}

/** [1,2,3,7,9,10] → "1-3، 7، 9-10" */
function compressPages_(pages) {
  var nums = pages.slice().sort(function (a, b) { return a - b; })
    .filter(function (n, i, arr) { return i === 0 || n !== arr[i - 1]; });
  var parts = [];
  for (var i = 0; i < nums.length; i++) {
    var start = nums[i];
    while (i + 1 < nums.length && nums[i + 1] === nums[i] + 1) i++;
    parts.push(start === nums[i] ? String(start) : start + '-' + nums[i]);
  }
  return shorten_(parts.join('، '), 600);
}

function shorten_(s, max) {
  s = String(s || '');
  return s.length > max ? s.slice(0, max - 1) + '…' : s;
}

function utf8Length_(s) {
  try {
    return encodeURIComponent(s).replace(/%[0-9A-F]{2}/gi, '_').length;
  } catch (err) {
    return s.length * 3;
  }
}

function escapeHtml_(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function linkify_(escaped) {
  return escaped.replace(/https?:\/\/[^\s<]+/g, function (u) { return '<a href="' + u + '">' + u + '</a>'; });
}

/** JSON text that is safe to place inside a <script> element. */
function jsonForScript_(json) {
  // U+2028/U+2029 end a line inside JavaScript strings in older engines.
  return String(json).replace(/</g, '\\u003c').replace(new RegExp('\\u2028', 'g'), '\\u2028')
    .replace(new RegExp('\\u2029', 'g'), '\\u2029');
}
