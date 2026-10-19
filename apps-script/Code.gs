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
  REPORT_EMAIL_CHARS: 1500
};

var SUPPORTED_EXT = { pdf: true, docx: true, txt: true };
var MIME_EXT = {
  'application/pdf': 'pdf',
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document': 'docx',
  'text/plain': 'txt'
};
var GH_RAW = 'application/vnd.github.raw';
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
      out.uploading = scan.uploading;
      out.unsupported = scan.unsupported.slice(0, 10);
      out.links = {
        root: folders.root.getUrl(),
        inbox: folders.inbox.getUrl(),
        ready: folders.ready.getUrl(),
        problem: folders.problem.getUrl()
      };
    }

    out.recent = finishedJobs_(jobs).slice(0, 10).map(function (j) {
      return {
        title: j.title,
        stage: j.stage,
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

/** The status web page. */
function doGet() {
  var template = HtmlService.createTemplateFromFile('Page');
  template.initialJson = jsonForScript_(getStatus());
  return template.evaluate()
    .setTitle('تحويل الكتب')
    .addMetaTag('viewport', 'width=device-width, initial-scale=1')
    .setXFrameOptionsMode(HtmlService.XFrameOptionsMode.DEFAULT);
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
    // Parked books go back to the judges first on a new day (their quota is separate
    // and small), then new books, then improvement rounds.
    var judged = judgeNext_(ctx);
    var started = judged || startNext_(ctx);
    notes.push(started);
    // Nothing new to convert: use the day's judge quota on a finished book's unsure pages.
    if (started === 'idle') notes.push(refineNext_(ctx));
  }
  rememberExpiry_();
  return notes.join('; ');
}

function advanceJob_(ctx, job) {
  // "queued": in "2- قيد التحويل" but GitHub has not accepted the dispatch yet.
  if (job.stage === 'queued') {
    return dispatch_(ctx, job, job.pendingCount !== false);
  }

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

  // Waiting only for the judges is written as "paused" for older scripts.
  var state = s.state === 'paused' && s.waiting_for === 'judges' ? 'judging' : s.state;
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
    if (j.stage !== 'delivered' || j.refineDone || !(j.unsure > 0) || j.refineDay === today) return;
    if ((j.refines || 0) >= CONFIG.MAX_REFINES) {
      finalize_(ctx, j, true);
      return;
    }
    if (!pick || String(j.finishedAt) < String(pick.finishedAt)) pick = j;
  });
  if (!pick) return 'idle';
  // Finished jobs are stored without these fields (see pruneJobs_); rebuild them.
  pick.resultDir = 'drive-' + pick.fileId;
  try {
    var again = newJob_(DriveApp.getFileById(pick.fileId));
    pick.name = again.name;
    if (again.driveName) pick.driveName = again.driveName;
  } catch (err) {
    // The original was deleted from "4- الأصل": nothing left to improve.
    pick.refineDone = true;
    saveJobs_(ctx.jobs);
    return 'idle (original gone: ' + pick.fileId + ')';
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
 * The book will not change any more: its EPUB and report move to "6- مكتمل".
 * With mail, the user is told which pages (if any) are still unsure.
 */
function finalize_(ctx, job, mail) {
  job.refineDone = true;
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
  lines.push('يمكنك الآن حذف ملفه الأصلي من «' + CONFIG.FOLDERS.originals + '» إن احتجت المساحة.');
  sendMail_('مكتمل: ' + job.title, lines);
}

/** Dispatches the oldest parked book whose judges' quota has renewed, or returns ''. */
function judgeNext_(ctx) {
  var today = pacificDay_(now_());
  var pick = null;
  Object.keys(ctx.jobs).forEach(function (id) {
    var j = ctx.jobs[id];
    if (j && j.stage === 'judging' && j.judgeDay < today &&
        (!pick || String(j.judgeDay) < String(pick.judgeDay))) pick = j;
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
  var r = gh_(ctx.cfg, 'post', '/repos/' + ctx.cfg.repo + '/actions/workflows/' + CONFIG.WORKFLOW + '/dispatches', body);

  if (r.code === 204 || r.code === 200) {
    job.stage = 'running';
    job.dispatchedAt = at.toISOString();
    if (countAttempt) job.attempts = (job.attempts || 0) + 1;
    job.pausedDay = '';
    job.lastError = '';
    delete job.pendingCount;
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
  var unsure = pageList_(s.pages_unsure);
  var missing = pageList_(s.pages_missing);
  var url = 'https://drive.google.com/file/d/' + job.epubId + '/view';
  job.stage = 'delivered';
  job.lastState = s.state;
  job.lastError = '';
  job.unsure = unsure.length;

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
    if (unsure.length >= before || !unsure.length) {
      job.unsureList = shorten_(compressPages_(unsure), 300);
      finalize_(ctx, job, true);
      return 'refined and final (' + before + ' -> ' + unsure.length + ')';
    }
    saveJobs_(ctx.jobs);
    if (unsure.length < before) {
      sendMail_('تحسّن: ' + job.title, [
        'راجعت النماذج الأقوى صفحات مشكوكاً فيها في «' + job.title + '».',
        'الصفحات المشكوك فيها نزلت من ' + before + ' إلى ' + unsure.length + '.',
        'النسخة الجديدة حلّت محل القديمة: ' + url,
        unsure.length ? 'ستستمر المراجعة في الأيام القادمة إن بقي ما يمكن تحسينه.' : 'لم تبقَ صفحات مشكوك فيها.'
      ]);
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
  // Parked books (judging) still need all their fields and must never be dropped.
  var finished = finishedJobs_(jobs).filter(function (j) { return j.stage !== 'judging'; });
  finished.forEach(function (j) {
    ['name', 'driveName', 'resultDir', 'dispatchedAt', 'createdAt', 'pausedDay', 'pausedAt',
      'pendingCount', 'noteId', 'pauseMailed', 'pagesDone', 'pagesTotal', 'pauses']
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
