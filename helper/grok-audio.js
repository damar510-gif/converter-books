// «حفظ صوت Grok»: a Chrome extension (grok-audio-manifest.json; the helper copies both files
// to %LOCALAPPDATA%\GrokAudio, docs/helper.md). It runs in the page itself on grok.com and
// catches the audio of Grok's read-aloud button however the page gets it: a fetch or XHR
// answering audio, base64 audio inside a JSON answer, an <audio> source, a MediaSource
// stream or Web Audio decoding. Each clip is listed in a small panel to save, one by one or
// merged into one file. «تشخيص» copies the page's recent requests, to fix the catching if
// Grok changes how it plays.
(() => {
  'use strict';
  if (window.__grokAudio) return;
  window.__grokAudio = true;

  const AUTO_KEY = 'grokAudio.auto';
  // Requests whose text answer may carry base64 audio.
  const AUDIO_URL = /audio|tts|speech|voice|speak|read/i;
  const clips = [];
  const seen = new Set();
  const recent = [];
  const blobOf = new Map(); // blob: URL -> Blob
  let lastLabel = '';
  let ui = null;

  const origFetch = window.fetch;

  // ---- what a clip is

  function typeOf(bytes, given) {
    const b = bytes;
    const s = (i, n) => String.fromCharCode.apply(null, b.subarray(i, i + n));
    if (b.length > 3 && (s(0, 3) === 'ID3' || (b[0] === 0xff && (b[1] & 0xe0) === 0xe0 && (b[1] & 0x06)))) return 'audio/mpeg';
    if (b.length > 3 && b[0] === 0xff && (b[1] & 0xf6) === 0xf0) return 'audio/aac';
    if (b.length > 4 && s(0, 4) === 'OggS') return 'audio/ogg';
    if (b.length > 12 && s(0, 4) === 'RIFF' && s(8, 4) === 'WAVE') return 'audio/wav';
    if (b.length > 4 && b[0] === 0x1a && b[1] === 0x45 && b[2] === 0xdf && b[3] === 0xa3) return 'audio/webm';
    if (b.length > 8 && s(4, 4) === 'ftyp') return 'audio/mp4';
    if (b.length > 4 && s(0, 4) === 'fLaC') return 'audio/flac';
    return /^audio\//.test(given || '') ? given.split(';')[0] : '';
  }

  function extOf(type) {
    return { 'audio/mpeg': 'mp3', 'audio/aac': 'aac', 'audio/ogg': 'ogg', 'audio/wav': 'wav',
      'audio/webm': 'webm', 'audio/mp4': 'm4a', 'audio/flac': 'flac' }[type] || 'mp3';
  }

  // The text of the message whose button the user pressed last, for the file's name.
  document.addEventListener('click', (e) => {
    let el = e.target instanceof Element ? e.target : null;
    if (!el || (ui && ui.host.contains(el))) return;
    for (let i = 0; el && i < 12; i++, el = el.parentElement) {
      const t = (el.innerText || '').trim();
      if (t.length > 40) {
        lastLabel = t.split('\n').find((l) => l.trim().length > 3) || t;
        return;
      }
    }
  }, true);

  function cleanName(text) {
    const name = String(text || '').replace(/[\\/:*?"<>|\u0000-\u001f]+/g, ' ').replace(/\s+/g, ' ').trim();
    return name.slice(0, 60).trim() || 'Grok';
  }

  async function addClip(blob, givenType, source) {
    try {
      const bytes = new Uint8Array(await blob.arrayBuffer());
      if (bytes.length < 1000) return;
      const type = typeOf(bytes, givenType || blob.type);
      if (!type) return;
      const head = Array.prototype.join.call(bytes.subarray(0, 32), ',');
      const tail = Array.prototype.join.call(bytes.subarray(bytes.length - 32), ',');
      const key = bytes.length + ':' + head + ':' + tail;
      if (seen.has(key)) return;
      seen.add(key);
      const clip = { blob: new Blob([bytes], { type }), type, label: cleanName(lastLabel), source, at: new Date() };
      clips.push(clip);
      render();
      if (autoSave()) save([clip], clip.label);
    } catch (e) {
      note('clip failed: ' + e);
    }
  }

  function note(text) {
    recent.push(new Date().toISOString().slice(11, 19) + ' ' + text);
    if (recent.length > 80) recent.shift();
  }

  // ---- base64 audio inside a text answer

  // ID3, an MP3 frame (FF Ex..FF Fx), Ogg, WAV, WebM.
  const B64_AUDIO = /(?:SUQz|\/\/[g-z]|T2dnUw|UklGR|GkXfo)[A-Za-z0-9+/=_-]{800,}/g;

  function fromBase64(s) {
    s = s.replace(/-/g, '+').replace(/_/g, '/');
    while (s.length % 4) s += '=';
    const bin = atob(s);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  }

  function audioInText(text) {
    const parts = [];
    for (const m of String(text).replace(/\\\//g, '/').matchAll(B64_AUDIO)) {
      try { parts.push(fromBase64(m[0])); } catch (e) { /* not base64 after all */ }
    }
    return parts.length ? new Blob(parts) : null;
  }

  // ---- network

  function urlOf(input) {
    try { return String(input instanceof Request ? input.url : input); } catch (e) { return ''; }
  }

  function shortUrl(url) {
    return String(url).split('?')[0].slice(0, 160);
  }

  async function inspect(res, url) {
    const type = (res.headers.get('content-type') || '').toLowerCase();
    const size = res.headers.get('content-length') || '?';
    note(res.status + ' ' + (type || '-') + ' ' + size + ' ' + shortUrl(url));
    if (/^audio\/|octet-stream|^video\/webm/.test(type)) {
      addClip(await res.blob(), type, 'fetch');
    } else if (AUDIO_URL.test(url) && /json|text|event-stream|ndjson/.test(type)) {
      const blob = audioInText(await res.text());
      if (blob) addClip(blob, '', 'json');
    }
  }

  window.fetch = async function (...args) {
    const res = await origFetch.apply(this, args);
    try { inspect(res.clone(), urlOf(args[0])).catch((e) => note('inspect: ' + e)); } catch (e) { /* body used */ }
    return res;
  };

  const xhrOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (method, url, ...rest) {
    this.__grokAudioUrl = String(url);
    return xhrOpen.call(this, method, url, ...rest);
  };
  const xhrSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.send = function (...args) {
    this.addEventListener('load', () => {
      try {
        const type = (this.getResponseHeader('content-type') || '').toLowerCase();
        const url = this.__grokAudioUrl || '';
        note('xhr ' + this.status + ' ' + (type || '-') + ' ' + shortUrl(url));
        const r = this.response;
        if (r instanceof Blob) addClip(r, type, 'xhr');
        else if (r instanceof ArrayBuffer) addClip(new Blob([r]), type, 'xhr');
        else if (typeof r === 'string' && AUDIO_URL.test(url)) {
          const blob = audioInText(r);
          if (blob) addClip(blob, '', 'xhr-json');
        }
      } catch (e) { note('xhr: ' + e); }
    });
    return xhrSend.apply(this, args);
  };

  const OrigWebSocket = window.WebSocket;
  if (OrigWebSocket) {
    window.WebSocket = function (url, protocols) {
      note('websocket ' + shortUrl(url));
      return protocols === undefined ? new OrigWebSocket(url) : new OrigWebSocket(url, protocols);
    };
    window.WebSocket.prototype = OrigWebSocket.prototype;
    Object.setPrototypeOf(window.WebSocket, OrigWebSocket);
  }

  // ---- blob URLs, <audio>, MediaSource, Web Audio

  const createObjectURL = URL.createObjectURL;
  URL.createObjectURL = function (obj) {
    const url = createObjectURL.call(URL, obj);
    if (obj instanceof Blob) blobOf.set(url, obj);
    return url;
  };

  const play = HTMLMediaElement.prototype.play;
  HTMLMediaElement.prototype.play = function (...args) {
    try { fromMedia(this); } catch (e) { note('play: ' + e); }
    return play.apply(this, args);
  };

  function fromMedia(el) {
    const src = el.currentSrc || el.src;
    if (!src) return;
    note('play ' + shortUrl(src));
    if (src.startsWith('blob:')) {
      const blob = blobOf.get(src);
      if (blob) addClip(blob, blob.type, 'audio');
    } else if (/^(https?|data):/.test(src)) {
      origFetch(src, { credentials: 'include' }).then((r) => r.blob())
        .then((b) => addClip(b, b.type, 'audio')).catch((e) => note('src: ' + e));
    }
  }

  if (window.MediaSource && window.SourceBuffer) {
    const addSourceBuffer = MediaSource.prototype.addSourceBuffer;
    MediaSource.prototype.addSourceBuffer = function (mime) {
      const sb = addSourceBuffer.call(this, mime);
      sb.__grokAudio = { mime: String(mime), parts: [] };
      (this.__grokAudio = this.__grokAudio || []).push(sb);
      return sb;
    };
    const appendBuffer = SourceBuffer.prototype.appendBuffer;
    SourceBuffer.prototype.appendBuffer = function (data) {
      try {
        if (this.__grokAudio) {
          const view = data instanceof ArrayBuffer ? new Uint8Array(data)
            : new Uint8Array(data.buffer, data.byteOffset, data.byteLength);
          this.__grokAudio.parts.push(view.slice());
        }
      } catch (e) { note('append: ' + e); }
      return appendBuffer.call(this, data);
    };
    const endOfStream = MediaSource.prototype.endOfStream;
    MediaSource.prototype.endOfStream = function (...args) {
      try {
        for (const sb of this.__grokAudio || []) {
          if (/audio|mpeg/.test(sb.__grokAudio.mime) && sb.__grokAudio.parts.length) {
            addClip(new Blob(sb.__grokAudio.parts), sb.__grokAudio.mime, 'stream');
          }
        }
      } catch (e) { note('end: ' + e); }
      return endOfStream.apply(this, args);
    };
  }

  const AC = window.AudioContext || window.webkitAudioContext;
  if (AC && AC.prototype.decodeAudioData) {
    const decode = AC.prototype.decodeAudioData;
    AC.prototype.decodeAudioData = function (buf, ...rest) {
      try { if (buf && buf.byteLength) addClip(new Blob([buf.slice(0)]), '', 'webaudio'); } catch (e) { note('decode: ' + e); }
      return decode.call(this, buf, ...rest);
    };
  }

  // ---- saving

  function autoSave() {
    try { return localStorage.getItem(AUTO_KEY) === '1'; } catch (e) { return false; }
  }

  function stamp(d) {
    const p = (n) => String(n).padStart(2, '0');
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' + p(d.getHours()) + p(d.getMinutes()) + p(d.getSeconds());
  }

  function save(list, label) {
    const type = list[0].type;
    const blob = new Blob(list.map((c) => c.blob), { type });
    const a = document.createElement('a');
    a.href = createObjectURL.call(URL, blob);
    a.download = cleanName(label) + ' ' + stamp(list[0].at) + '.' + extOf(type);
    document.documentElement.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 60000);
  }

  // One file from several clips: MP3, AAC (ADTS) and Ogg pages join end to end; others
  // are saved one by one.
  function saveAll() {
    if (!clips.length) return;
    const joinable = /^audio\/(mpeg|aac|ogg)$/.test(clips[0].type) && clips.every((c) => c.type === clips[0].type);
    if (joinable) save(clips, clips[0].label);
    else clips.forEach((c, i) => setTimeout(() => save([c], c.label), i * 400));
  }

  // ---- panel (shadow DOM; no innerHTML, for pages with Trusted Types)

  function el(tag, props, kids) {
    const e = document.createElement(tag);
    Object.assign(e, props || {});
    (kids || []).forEach((k) => e.append(k));
    return e;
  }

  const CSS = `
    :host { all: initial; }
    .btn { position: fixed; left: 16px; bottom: 16px; z-index: 2147483647; font: 600 14px system-ui, sans-serif;
      background: #111; color: #e8c766; border: 1px solid #e8c766; border-radius: 999px; padding: 8px 14px; cursor: pointer; }
    .panel { position: fixed; left: 16px; bottom: 60px; z-index: 2147483647; width: min(340px, calc(100vw - 32px));
      max-height: 60vh; overflow: auto; direction: rtl; font: 14px system-ui, sans-serif; background: #111; color: #f4efe2;
      border: 1px solid #e8c766; border-radius: 12px; padding: 12px; box-shadow: 0 8px 24px rgba(0,0,0,.4); }
    .panel[hidden] { display: none; }
    .row { display: flex; gap: 8px; align-items: center; padding: 6px 0; border-bottom: 1px solid #333; }
    .row span { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    button.small { font: 13px system-ui, sans-serif; background: #e8c766; color: #111; border: 0; border-radius: 6px; padding: 4px 10px; cursor: pointer; }
    button.plain { background: transparent; color: #e8c766; border: 1px solid #e8c766; }
    .bar { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
    .hint { color: #b9b2a0; font-size: 13px; margin: 4px 0 8px; }
    label { display: flex; gap: 6px; align-items: center; margin-top: 10px; }
  `;

  function mount() {
    if (ui || !document.body) return;
    const host = el('div');
    const root = host.attachShadow({ mode: 'open' });
    const btn = el('button', { className: 'btn', title: 'حفظ صوت Grok' });
    const panel = el('div', { className: 'panel', hidden: true });
    const list = el('div');
    const auto = el('input', { type: 'checkbox', checked: autoSave() });
    auto.addEventListener('change', () => { try { localStorage.setItem(AUTO_KEY, auto.checked ? '1' : '0'); } catch (e) { /* private window */ } });
    const all = el('button', { className: 'small', textContent: 'نزّل الكل في ملف واحد' });
    all.addEventListener('click', saveAll);
    const clear = el('button', { className: 'small plain', textContent: 'امسح القائمة' });
    clear.addEventListener('click', () => { clips.length = 0; render(); });
    const diag = el('button', { className: 'small plain', textContent: 'تشخيص' });
    diag.addEventListener('click', () => {
      const text = 'grok-audio ' + location.pathname + '\n' + recent.join('\n');
      navigator.clipboard.writeText(text).then(() => { diag.textContent = 'نُسخ، الصقه لـClaude'; },
        () => { window.prompt('انسخ هذا النص:', text); });
    });
    panel.append(
      el('div', { className: 'hint', textContent: 'اضغط زر القراءة الصوتية تحت أي رد في Grok، فيظهر صوته هنا.' }),
      list,
      el('div', { className: 'bar' }, [all, clear, diag]),
      el('label', {}, [auto, 'احفظ كل صوت تلقائياً']));
    btn.addEventListener('click', () => { panel.hidden = !panel.hidden; });
    root.append(el('style', { textContent: CSS }), panel, btn);
    document.body.appendChild(host);
    ui = { host, btn, list };
    render();
  }

  function render() {
    if (!ui) return;
    ui.btn.textContent = '🔊 ' + clips.length;
    ui.list.replaceChildren(...clips.map((c, i) => {
      const dl = el('button', { className: 'small', textContent: 'نزّل' });
      dl.addEventListener('click', () => save([c], c.label));
      const kb = Math.round(c.blob.size / 1024);
      return el('div', { className: 'row', title: c.label }, [
        el('span', { textContent: (i + 1) + '. ' + c.label + ' (' + kb + ' ك.ب)' }), dl]);
    }));
  }

  if (document.body) mount();
  else document.addEventListener('DOMContentLoaded', mount);
  // Grok's page may replace the body; put the panel back.
  setInterval(() => { if (ui && !ui.host.isConnected && document.body) document.body.appendChild(ui.host); else if (!ui) mount(); }, 2000);
})();
