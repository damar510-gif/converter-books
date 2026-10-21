// Loads the extension «حفظ صوت Grok» (helper/grok-audio*.{js,json}) in Chromium with a fake
// grok.com page and checks that each way the page may play audio gives a clip to save.
// Run: node tests/grok-audio/run_tests.js (needs Playwright; NODE_PATH may point to it).
'use strict';
const fs = require('fs');
const os = require('os');
const path = require('path');
const { chromium } = require('playwright');

const ROOT = path.join(__dirname, '..', '..');
const MP3 = fs.readFileSync(path.join(__dirname, 'tone.mp3'));

const PAGE = `<!doctype html><meta charset="utf-8"><body>
<div class="msg"><p>هذا رد طويل من Grok عن تحويل فصل الكتاب إلى صوت مسموع بصوت هادئ</p>
<button id="speak">read</button></div>
<script>
const b64 = ${JSON.stringify(MP3.toString('base64'))};
const bytes = () => Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
window.ways = {
  fetch: () => fetch('/rest/tts/1').then((r) => r.arrayBuffer()),
  json: () => fetch('/rest/read-response-audio/2').then((r) => r.json()),
  src: () => { const a = new Audio('/media/3.mp3'); return a.play().catch(() => {}); },
  blob: () => { const u = URL.createObjectURL(new Blob([bytes(), new Uint8Array([4])], { type: 'audio/mpeg' }));
    return new Audio(u).play().catch(() => {}); },
  stream: () => new Promise((done) => {
    const ms = new MediaSource();
    const a = new Audio(URL.createObjectURL(ms));
    ms.addEventListener('sourceopen', () => {
      const sb = ms.addSourceBuffer('audio/mpeg');
      const d = bytes();
      sb.addEventListener('updateend', () => { ms.endOfStream(); done(); }, { once: true });
      sb.appendBuffer(d.slice(0, d.length - 5));
    });
    a.play().catch(() => {});
  }),
  webaudio: () => { const d = bytes(); d[d.length - 1] ^= 1; return new AudioContext().decodeAudioData(d.buffer).catch(() => {}); },
};
</script>`;

function check(cond, msg) {
  if (!cond) throw new Error(msg);
  console.log('ok - ' + msg);
}

(async () => {
  const ext = fs.mkdtempSync(path.join(os.tmpdir(), 'grok-audio-'));
  fs.copyFileSync(path.join(ROOT, 'helper', 'grok-audio-manifest.json'), path.join(ext, 'manifest.json'));
  fs.copyFileSync(path.join(ROOT, 'helper', 'grok-audio.js'), path.join(ext, 'grok-audio.js'));
  const ctx = await chromium.launchPersistentContext('', {
    headless: true,
    channel: 'chromium',
    ignoreDefaultArgs: ['--disable-extensions'],
    acceptDownloads: true,
    args: ['--disable-extensions-except=' + ext, '--load-extension=' + ext, '--autoplay-policy=no-user-gesture-required'],
  });
  let failed = false;
  try {
    await ctx.route('https://grok.com/**', (route) => {
      const u = new URL(route.request().url());
      if (u.pathname === '/') return route.fulfill({ contentType: 'text/html', body: PAGE });
      // Each answer differs by one byte, so the clips are not taken for one another.
      const tag = (n) => Buffer.concat([MP3, Buffer.from([n])]);
      if (u.pathname === '/rest/tts/1') return route.fulfill({ contentType: 'audio/mpeg', body: tag(1) });
      if (u.pathname === '/rest/read-response-audio/2') {
        return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ audio: tag(2).toString('base64') }).replace(/\//g, '\\/') });
      }
      if (u.pathname === '/media/3.mp3') return route.fulfill({ contentType: 'audio/mpeg', body: tag(3) });
      return route.fulfill({ status: 404, body: '' });
    });
    const page = await ctx.newPage();
    await page.goto('https://grok.com/');
    const count = async () => Number(((await page.locator('button.btn').textContent()) || '').replace(/\D/g, ''));
    await page.waitForFunction(() => window.__grokAudio === true);
    await page.locator('button.btn').waitFor();
    check((await count()) === 0, 'panel shows no clip at first');
    await page.click('#speak');
    let n = 0;
    for (const way of ['fetch', 'json', 'src', 'blob', 'stream', 'webaudio']) {
      await page.evaluate((w) => window.ways[w](), way);
      n += 1;
      await page.waitForTimeout(400);
      check((await count()) === n, way + ' gives a clip');
    }
    await page.evaluate(() => window.ways.fetch());
    await page.waitForTimeout(400);
    check((await count()) === n, 'the same audio twice is one clip');

    await page.locator('button.btn').click();
    const [one] = await Promise.all([page.waitForEvent('download'), page.locator('.row button').first().click()]);
    const name = one.suggestedFilename();
    check(/^هذا رد طويل من Grok.*\.mp3$/.test(name), 'a clip saves as mp3 named after the message: ' + name);
    const saved = fs.readFileSync(await one.path());
    check(saved.length === MP3.length + 1 && saved.subarray(0, 64).equals(MP3.subarray(0, 64)), 'the saved file is the audio itself');
  } catch (e) {
    failed = true;
    console.error('FAIL - ' + e.message);
  } finally {
    await ctx.close();
    fs.rmSync(ext, { recursive: true, force: true });
  }
  process.exit(failed ? 1 : 0);
})();
