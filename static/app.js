/* ============================================================
 * 成长证据库 · 前端 SPA（纯原生 JS，无框架）
 * 页面：今天 / 记录 / 日历 / 媒体 / 报告 / 数据 / 成长 / 知识库 / 设置 / 记录详情
 * ============================================================ */
(function () {
  'use strict';

  /* ---------------- 基础工具 ---------------- */
  var $ = function (sel, root) { return (root || document).querySelector(sel); };
  var $$ = function (sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); };

  // 第三方库特性检测（缺失时优雅降级）
  var LIB = {
    echarts: typeof window.echarts !== 'undefined',
    mermaid: typeof window.mermaid !== 'undefined',
    md: typeof window.marked !== 'undefined' && typeof window.DOMPurify !== 'undefined'
  };

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function pad(n) { return String(n).padStart(2, '0'); }

  function localDateStr(d) {
    d = d || new Date();
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
  }

  function parseDate(iso) {
    var d = new Date(iso);
    return isNaN(d.getTime()) ? null : d;
  }

  function absTime(iso) {
    var d = parseDate(iso);
    if (!d) return '';
    return d.toLocaleString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false });
  }

  function absDate(iso) {
    var d = parseDate(iso);
    if (!d) return '';
    return d.toLocaleDateString('zh-CN', { year: 'numeric', month: 'long', day: 'numeric' });
  }

  function hhmm(iso) {
    var d = parseDate(iso);
    return d ? pad(d.getHours()) + ':' + pad(d.getMinutes()) : '';
  }

  // 相对时间：时间线用（如 "3 小时前"）
  function relTime(iso) {
    var d = parseDate(iso);
    if (!d) return '';
    var diff = Date.now() - d.getTime();
    if (diff < 0) return hhmm(iso);
    var m = Math.floor(diff / 60000);
    if (m < 1) return '刚刚';
    if (m < 60) return m + ' 分钟前';
    var h = Math.floor(m / 60);
    if (h < 24) return h + ' 小时前';
    var days = Math.floor(h / 24);
    if (days === 1) return '昨天 ' + hhmm(iso);
    if (days < 7) return days + ' 天前';
    return absDate(iso);
  }

  function toLocalInputValue(iso) {
    var d = iso ? parseDate(iso) : new Date();
    if (!d) d = new Date();
    return localDateStr(d) + 'T' + pad(d.getHours()) + ':' + pad(d.getMinutes());
  }

  function fmtSize(n) {
    if (n == null || isNaN(n)) return '';
    if (n < 1024) return n + ' B';
    if (n < 1048576) return (n / 1024).toFixed(1) + ' KB';
    return (n / 1048576).toFixed(1) + ' MB';
  }

  function fmtWords(n) {
    if (n == null) return '0';
    if (n >= 10000) return (n / 10000).toFixed(1) + ' 万';
    return String(n);
  }

  // 旧 work/life/mixed 键名 → 中文（统计接口 category_counts 的兼容兜底；新体系键就是中文类目名）
  var CATS = { work: '工作', life: '生活', mixed: '混合' };

  /* ---------------- Toast ---------------- */
  function toast(msg, type, ms) {
    var root = $('#toasts');
    if (!root) return;
    var t = document.createElement('div');
    t.className = 'toast toast-' + (type || 'info');
    t.textContent = msg;
    root.appendChild(t);
    requestAnimationFrame(function () { t.classList.add('show'); });
    setTimeout(function () {
      t.classList.remove('show');
      setTimeout(function () { t.remove(); }, 300);
    }, ms || 3200);
  }

  /* ---------------- API 封装 ----------------
   * 统一 JSON；非 2xx 抛出带后端 detail 的 Error；支持超时。 */
  async function api(path, opts) {
    opts = opts || {};
    var ctrl = new AbortController();
    var timer = setTimeout(function () { ctrl.abort(); }, opts.timeout || 30000);
    var init = { method: opts.method || 'GET', signal: ctrl.signal, headers: {} };
    if (opts.body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(opts.body);
    }
    var res;
    try {
      res = await fetch(path, init);
    } catch (e) {
      clearTimeout(timer);
      var netErr = new Error(ctrl.signal.aborted ? '请求超时了，请稍后再试' : '连不上服务，请确认电脑上的后端已启动');
      // 网络层异常（服务不可达 / 离线）打标记，调用方可据此走离线逻辑
      if (!ctrl.signal.aborted) netErr.isNetwork = true;
      throw netErr;
    }
    clearTimeout(timer);
    var text = await res.text();
    var data = null;
    if (text) { try { data = JSON.parse(text); } catch (e) { data = null; } }
    if (!res.ok) {
      var msg = '请求失败（' + res.status + '）';
      if (data && data.detail != null) {
        if (typeof data.detail === 'string') msg = data.detail;
        else if (Array.isArray(data.detail)) msg = data.detail.map(function (d) { return d && d.msg ? d.msg : String(d); }).join('；');
        else msg = String(data.detail);
      }
      // 访问口令锁定：全站锁屏（未设锁时永不触发）
      if (res.status === 401 && msg === 'locked') showLockScreen();
      var err = new Error(msg);
      err.status = res.status;
      throw err;
    }
    return data;
  }

  /* ---- 锁屏（访问口令） ---- */
  var lockShown = false;
  function showLockScreen() {
    if (lockShown) return;
    lockShown = true;
    var wrap = document.createElement('div');
    wrap.className = 'lock-screen';
    wrap.innerHTML =
      '<div class="lock-card">' +
      '<img src="/static/icon.svg" class="lock-logo" alt="">' +
      '<div class="lock-title">小满上了锁</div>' +
      '<p class="hint mt-0 mb-14">输入访问口令继续</p>' +
      '<input class="input" type="password" id="lock-input" placeholder="访问口令" autocomplete="current-password">' +
      '<div class="lock-err" id="lock-err"></div>' +
      '</div>';
    document.body.appendChild(wrap);
    var submit = async function () {
      var inp = $('#lock-input');
      var pw = inp.value;
      if (!pw) return;
      try {
        var res = await fetch('/api/auth', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ password: pw })
        });
        var j = null;
        try { j = await res.json(); } catch (e) { /* 忽略 */ }
        if (res.ok && j && j.ok) { location.reload(); return; }
        var errBox = $('#lock-err');
        errBox.textContent = (j && typeof j.detail === 'string') ? j.detail : '口令不对，再试一次';
        wrap.classList.remove('shake');
        void wrap.offsetWidth; // 重触抖动动画
        wrap.classList.add('shake');
        inp.value = '';
        inp.focus();
      } catch (e) {
        $('#lock-err').textContent = '连不上服务，请确认电脑上的后端已启动';
      }
    };
    $('#lock-input').addEventListener('keydown', function (e) { if (e.key === 'Enter') submit(); });
    setTimeout(function () { var i = $('#lock-input'); if (i) i.focus(); }, 60);
  }

  // 文件下载（用于备份 / 导出，可统一处理中文错误）
  async function downloadFile(path, opts) {
    opts = opts || {};
    var res;
    try {
      res = await fetch(path, { method: opts.method || 'GET' });
    } catch (e) {
      throw new Error('连不上服务，请确认电脑上的后端已启动');
    }
    if (!res.ok) {
      var msg = '下载失败（' + res.status + '）';
      try {
        var j = await res.json();
        if (j && typeof j.detail === 'string') msg = j.detail;
      } catch (e) { /* 忽略 */ }
      throw new Error(msg);
    }
    var blob = await res.blob();
    var fname = opts.filename || 'download';
    var cd = res.headers.get('Content-Disposition') || '';
    var m = cd.match(/filename\*?=(?:UTF-8'')?"?([^";]+)/i);
    if (m) { try { fname = decodeURIComponent(m[1]); } catch (e) { fname = m[1]; } }
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url;
    a.download = fname;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(function () { URL.revokeObjectURL(url); }, 5000);
  }

  // 附件上传（XHR 以获得上传进度）
  function uploadFiles(entryId, files, onProgress) {
    return new Promise(function (resolve, reject) {
      var fd = new FormData();
      files.forEach(function (f) { fd.append('files', f); });
      var xhr = new XMLHttpRequest();
      xhr.open('POST', '/api/entries/' + encodeURIComponent(entryId) + '/attachments');
      xhr.upload.onprogress = function (e) {
        if (e.lengthComputable && onProgress) onProgress(Math.round(e.loaded / e.total * 100));
      };
      xhr.onload = function () {
        var j = null;
        try { j = JSON.parse(xhr.responseText); } catch (e) { /* 忽略 */ }
        if (xhr.status >= 200 && xhr.status < 300) resolve(j);
        else reject(new Error(j && typeof j.detail === 'string' ? j.detail : '上传失败，请重试'));
      };
      xhr.onerror = function () {
        var netErr = new Error('网络错误，上传失败');
        netErr.isNetwork = true;
        reject(netErr);
      };
      xhr.send(fd);
    });
  }

  /* ---------------- 手机离线记录 outbox（IndexedDB） ----------------
   * 局域网 http 无法注册 Service Worker，页面已加载时用 IDB 暂存，
   * 回到 Wi-Fi 后自动 flush 到电脑。 */
  var outbox = (function () {
    var DB_NAME = 'xiaoman_outbox';
    var STORE = 'items';
    var dbPromise = null;

    function open() {
      if (!window.indexedDB) return Promise.reject(new Error('no-idb'));
      if (!dbPromise) {
        dbPromise = new Promise(function (resolve, reject) {
          var req = indexedDB.open(DB_NAME, 1);
          req.onupgradeneeded = function () {
            var db = req.result;
            if (!db.objectStoreNames.contains(STORE)) {
              db.createObjectStore(STORE, { keyPath: 'id', autoIncrement: true });
            }
          };
          req.onsuccess = function () { resolve(req.result); };
          req.onerror = function () { reject(req.error); };
        });
      }
      return dbPromise;
    }
    function tx(mode, fn) {
      return open().then(function (db) {
        return new Promise(function (resolve, reject) {
          var t = db.transaction(STORE, mode);
          var store = t.objectStore(STORE);
          var out = fn(store);
          t.oncomplete = function () { resolve(out && out.result !== undefined ? out.result : out); };
          t.onerror = function () { reject(t.error); };
        });
      });
    }
    return {
      // item: {occurred_at, content, is_work, link, latitude, longitude, files:[{name,type,blob}], created_at}
      add: function (item) {
        return tx('readwrite', function (s) { s.add(item); });
      },
      all: function () {
        return open().then(function (db) {
          return new Promise(function (resolve, reject) {
            var t = db.transaction(STORE, 'readonly');
            var req = t.objectStore(STORE).getAll();
            req.onsuccess = function () { resolve(req.result || []); };
            req.onerror = function () { reject(req.error); };
          });
        });
      },
      remove: function (id) {
        return tx('readwrite', function (s) { s.delete(id); });
      },
      count: function () {
        if (!window.indexedDB) return Promise.resolve(0);
        return open().then(function (db) {
          return new Promise(function (resolve, reject) {
            var t = db.transaction(STORE, 'readonly');
            var req = t.objectStore(STORE).count();
            req.onsuccess = function () { resolve(req.result || 0); };
            req.onerror = function () { reject(req.error); };
          });
        }).catch(function () { return 0; });
      }
    };
  })();

  /* ---------------- Markdown 渲染（防 XSS） ---------------- */
  function mdToHtml(src) {
    src = String(src == null ? '' : src);
    if (LIB.md) {
      try {
        return window.DOMPurify.sanitize(window.marked.parse(src));
      } catch (e) { /* 落入内置渲染器 */ }
    }
    return miniMd(src);
  }

  // 内置极简渲染器：先整体转义，再做有限语法（链接仅允许 http/https）
  function miniMd(src) {
    function inline(s) {
      return s
        .replace(/`([^`\n]+)`/g, '<code>$1</code>')
        .replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
        .replace(/\*([^*\n]+)\*/g, '<em>$1</em>')
        .replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    }
    var lines = esc(src).split(/\r?\n/);
    var html = '', inList = false, para = [];
    function flushPara() {
      if (para.length) { html += '<p>' + para.map(function (l) { return inline(l); }).join('<br>') + '</p>'; para = []; }
    }
    function flushList() { if (inList) { html += '</ul>'; inList = false; } }
    for (var i = 0; i < lines.length; i++) {
      var line = lines[i];
      var t = line.trim();
      var m;
      if ((m = t.match(/^###\s+(.*)$/))) { flushPara(); flushList(); html += '<h4>' + inline(m[1]) + '</h4>'; }
      else if ((m = t.match(/^##\s+(.*)$/))) { flushPara(); flushList(); html += '<h3>' + inline(m[1]) + '</h3>'; }
      else if ((m = t.match(/^#\s+(.*)$/))) { flushPara(); flushList(); html += '<h2>' + inline(m[1]) + '</h2>'; }
      else if ((m = t.match(/^[-*•]\s+(.*)$/))) { flushPara(); if (!inList) { html += '<ul>'; inList = true; } html += '<li>' + inline(m[1]) + '</li>'; }
      else if (t === '') { flushPara(); flushList(); }
      else { flushList(); para.push(line); }
    }
    flushPara(); flushList();
    return html;
  }

  /* ---------------- Mermaid（可选，思维导图，v11） ---------------- */
  var mermaidInited = false;
  async function mermaidRenderSvg(code) {
    if (!LIB.mermaid) return null;
    try {
      if (!mermaidInited) {
        // v11 合法配置：securityLevel 'strict' 与 theme 'neutral' 均保留
        window.mermaid.initialize({ startOnLoad: false, securityLevel: 'strict', theme: 'neutral' });
        mermaidInited = true;
      }
      var id = 'mm-' + Date.now() + '-' + Math.floor(Math.random() * 1000000);
      var out = window.mermaid.render(id, code); // v11 返回 Promise
      if (out && typeof out.then === 'function') out = await out;
      var svg = typeof out === 'string' ? out : (out && out.svg);
      if (!svg) return null;
      if (window.DOMPurify) {
        svg = window.DOMPurify.sanitize(svg, { USE_PROFILES: { svg: true, svgFilters: true } });
      }
      return svg;
    } catch (e) {
      return null; // 任何异常都走 mm-tree 回退，绝不显示 mermaid 报错图
    }
  }

  // 清洗 AI 给的 mindmap 代码：去围栏、统一换行、trim；必须以 mindmap 开头
  function cleanMindmapCode(raw) {
    var code = String(raw == null ? '' : raw).replace(/\r\n?/g, '\n');
    code = code.split('\n').filter(function (l) { return !/^\s*```/.test(l); }).join('\n').trim();
    if (!/^mindmap\b/.test(code)) throw new Error('not a mindmap');
    return code;
  }

  // 取节点文本：root((x)) / id(x) / id[x] 取括号内，其余行取原文
  function mmNodeText(line) {
    var t = line.trim().replace(/::icon\([^)]*\)/g, '').trim();
    var m = t.match(/^(?:root|[\w一-龥-]+)?\s*[(\[{]{1,3}\s*([^()\[\]{}]+?)\s*[)\]}]{1,3}\s*$/);
    if (m) return m[1].trim();
    return t;
  }

  // 回退渲染：按缩进把 mindmap 解析为嵌套 <ul>/<li> 大纲树（纯 HTML，永不报错）
  function mmTreeHtml(code) {
    var lines = String(code || '').replace(/\r\n?/g, '\n').split('\n')
      .filter(function (l) { return l.trim() && !/^\s*```/.test(l) && !/^\s*mindmap\b/i.test(l); });
    var items = lines.map(function (l) {
      var indent = (l.match(/^\s*/) || [''])[0].replace(/\t/g, '  ').length;
      return { indent: indent, text: mmNodeText(l) };
    }).filter(function (it) { return it.text; });
    if (!items.length) return '';
    // 先建树
    var root = { children: [] };
    var stack = [{ indent: -1, node: root }];
    items.forEach(function (it) {
      var node = { text: it.text, children: [] };
      while (stack.length > 1 && it.indent <= stack[stack.length - 1].indent) stack.pop();
      stack[stack.length - 1].node.children.push(node);
      stack.push({ indent: it.indent, node: node });
    });
    function renderNodes(nodes) {
      if (!nodes.length) return '';
      return '<ul>' + nodes.map(function (n) {
        return '<li><span class="mm-node">' + esc(n.text) + '</span>' + renderNodes(n.children) + '</li>';
      }).join('') + '</ul>';
    }
    return '<div class="mm-tree">' + renderNodes(root.children) + '</div>';
  }

  // 思维导图入口：先清洗，优先 mermaid，异常一律回退大纲树
  async function renderMindmap(box, rawCode) {
    var code;
    try {
      code = cleanMindmapCode(rawCode);
    } catch (e) {
      box.innerHTML = mmTreeHtml(rawCode) || '<pre class="code">' + esc(String(rawCode || '')) + '</pre>';
      return;
    }
    if (LIB.mermaid) {
      var svg = await mermaidRenderSvg(code);
      if (svg && document.body.contains(box)) { box.innerHTML = svg; return; }
    }
    if (document.body.contains(box)) {
      box.innerHTML = mmTreeHtml(code) || '<pre class="code">' + esc(code) + '</pre>';
    }
  }

  /* ---------------- 遮罩 / 弹窗 / Lightbox ---------------- */
  function showOverlay(text) {
    $('#overlay-root').innerHTML =
      '<div class="overlay"><div class="overlay-box"><div class="spinner"></div><p>' + esc(text) + '</p></div></div>';
  }
  function hideOverlay() { $('#overlay-root').innerHTML = ''; }

  function closeModal() { $('#modal-root').innerHTML = ''; }

  // 通用弹窗：resolve(输入值 / true / null)
  function inputModal(opts) {
    return new Promise(function (resolve) {
      var root = $('#modal-root');
      root.innerHTML =
        '<div class="modal-wrap"><div class="modal">' +
        '<h3 class="modal-title">' + esc(opts.title || '') + '</h3>' +
        (opts.body || '') +
        (opts.input !== false
          ? '<input class="input" id="modal-input" type="' + esc(opts.type || 'text') + '" placeholder="' + esc(opts.placeholder || '') + '" value="' + esc(opts.value || '') + '">'
          : '') +
        '<div class="modal-btns">' +
        '<button class="btn btn-ghost" id="modal-cancel">' + esc(opts.cancelText || '取消') + '</button>' +
        '<button class="btn" id="modal-ok">' + esc(opts.okText || '确定') + '</button>' +
        '</div></div></div>';
      var done = function (v) { closeModal(); resolve(v); };
      $('#modal-cancel').onclick = function () { done(null); };
      $('.modal-wrap', root).addEventListener('click', function (e) {
        if (e.target.classList.contains('modal-wrap')) done(null);
      });
      $('#modal-ok').onclick = function () {
        done(opts.input === false ? true : $('#modal-input').value.trim());
      };
      var inp = $('#modal-input');
      if (inp) {
        inp.focus();
        inp.addEventListener('keydown', function (e) { if (e.key === 'Enter') $('#modal-ok').click(); });
      }
    });
  }

  function confirmModal(text, okText) {
    return inputModal({
      title: '请确认',
      body: '<p class="modal-text">' + esc(text) + '</p>',
      input: false,
      okText: okText || '确定'
    }).then(function (v) { return v === true; });
  }

  // 多选一弹窗：choices: [{label, sub?, value, primary?}]，resolve(选中 value / null)
  function choiceModal(opts) {
    return new Promise(function (resolve) {
      var root = $('#modal-root');
      var btns = opts.choices.map(function (c, i) {
        return '<button class="btn ' + (c.primary ? '' : 'btn-ghost') + '" data-ch="' + i + '">' + esc(c.label) +
          (c.sub ? '<span class="choice-sub">' + esc(c.sub) + '</span>' : '') + '</button>';
      }).join('');
      root.innerHTML =
        '<div class="modal-wrap"><div class="modal">' +
        '<h3 class="modal-title">' + esc(opts.title || '') + '</h3>' +
        (opts.body || '') +
        '<div class="modal-btns modal-btns-col">' + btns +
        '<button class="btn btn-ghost" data-ch="-1">' + esc(opts.cancelText || '取消') + '</button>' +
        '</div></div></div>';
      var done = function (v) { closeModal(); resolve(v); };
      $$('#modal-root [data-ch]').forEach(function (b) {
        b.onclick = function () {
          var i = Number(b.dataset.ch);
          done(i < 0 ? null : opts.choices[i].value);
        };
      });
      $('.modal-wrap', root).addEventListener('click', function (e) {
        if (e.target.classList.contains('modal-wrap')) done(null);
      });
    });
  }

  /* ---- 报告周期计算（本地自然周/自然月） ---- */
  function mondayOf(d) {
    var x = new Date(d);
    x.setDate(x.getDate() - ((x.getDay() + 6) % 7));
    x.setHours(0, 0, 0, 0);
    return x;
  }
  function addDays(d, n) { var x = new Date(d); x.setDate(x.getDate() + n); return x; }
  function fmtMd(d) { return (d.getMonth() + 1) + '月' + d.getDate() + '日'; }

  function openLightbox(kind, url) {
    var lb = $('#lightbox');
    lb.innerHTML = kind === 'video'
      ? '<video src="' + esc(url) + '" controls autoplay></video>'
      : '<img src="' + esc(url) + '" alt="">';
    lb.classList.remove('hidden');
    lb.onclick = function (e) { if (e.target === lb) closeLightbox(); };
  }
  function closeLightbox() {
    var lb = $('#lightbox');
    lb.classList.add('hidden');
    lb.innerHTML = '';
  }

  var closeQcPop = null; // 当前打开的「+」浮层关闭器（renderToday 赋值）
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') {
      closeLightbox();
      closeModal();
      if (closeQcPop) closeQcPop();
    }
  });

  /* ---------------- 通用小部件 ---------------- */
  function emptyHtml(title, sub) {
    return '<div class="empty">' +
      '<svg class="empty-ic"><use href="#i-sprout"/></svg>' +
      '<p class="empty-title">' + esc(title) + '</p>' +
      (sub ? '<p class="empty-sub">' + esc(sub) + '</p>' : '') +
      '</div>';
  }

  function loadingHtml(text) {
    return '<div class="loading"><div class="spinner"></div><p>' + esc(text || '加载中…') + '</p></div>';
  }

  function errorHtml(msg) {
    return emptyHtml('加载失败', msg || '请稍后刷新重试');
  }

  // 记录条目（今天时间线 / 记录列表 / 日历当日列表共用）
  function entryItemHtml(e, opts) {
    opts = opts || {};
    var thumbs = (e.attachments || []).filter(function (a) {
      return a.kind === 'image' || (a.kind === 'video' && a.thumb_url);
    }).slice(0, 4);
    var title = e.title || (e.content ? String(e.content).split('\n')[0].slice(0, 60) : '') || '（无标题）';
    var excerpt = e.summary || (e.content ? String(e.content).replace(/\s+/g, ' ').slice(0, 90) : '');
    var timeLabel = opts.showDate
      ? absDate(e.occurred_at) + ' ' + hhmm(e.occurred_at)
      : hhmm(e.occurred_at);
    // AI 类目 chips（≤3 个，小字淡色）。spec 字面是「摘要行内」，实测行内会挤压标题，放在摘要下一行，视觉成立（终审裁定保持现状）
    var catsHtml = '';
    if (Array.isArray(e.categories) && e.categories.length) {
      catsHtml = '<div class="ei-cats">' + e.categories.slice(0, 3).map(function (c) {
        return '<span class="ei-cat">' + esc(c.name_zh || c.name || c.slug || c) + '</span>';
      }).join('') + '</div>';
    }
    return '<a class="entry-item" href="#/entry/' + encodeURIComponent(e.id) + '">' +
      '<div class="ei-top">' +
      '<span class="ei-time">' + esc(timeLabel) + '</span>' +
      (e.is_work ? '<span class="ei-work" title="工作">工作</span>' : '') +
      '<span class="ei-title">' + esc(title) + '</span>' +
      (opts.showRel ? '<span class="ei-rel">' + esc(relTime(e.occurred_at)) + '</span>' : '') +
      '</div>' +
      (excerpt && excerpt !== title ? '<div class="ei-excerpt">' + esc(excerpt) + '</div>' : '') +
      catsHtml +
      (e.tags && e.tags.length
        ? '<div class="ei-tags">' + e.tags.map(function (t) { return '<span class="tag">#' + esc(t) + '</span>'; }).join('') + '</div>'
        : '') +
      (thumbs.length
        ? '<div class="ei-thumbs">' + thumbs.map(function (a) { return '<img loading="lazy" src="' + esc(a.thumb_url || a.url) + '" alt="">'; }).join('') + '</div>'
        : '') +
      '</a>';
  }

  // 附件磁贴（媒体页 / 详情页共用）
  function attTileHtml(a, opts) {
    opts = opts || {};
    var inner;
    if (a.kind === 'image') {
      inner = '<img loading="lazy" src="' + esc(a.thumb_url || a.url) + '" alt="' + esc(a.filename || '') + '">';
    } else if (a.kind === 'video') {
      inner = (a.thumb_url
        ? '<img loading="lazy" src="' + esc(a.thumb_url) + '" alt="">'
        : '<span class="att-ic"><svg class="ic"><use href="#i-video"/></svg></span>') + '<span class="play-dot"></span>';
    } else {
      inner = '<span class="att-ic"><svg class="ic"><use href="#i-file"/></svg></span>';
    }
    return '<div class="att-tile" data-kind="' + esc(a.kind) + '" data-url="' + esc(a.url) + '" data-name="' + esc(a.filename || '文件') + '" data-size="' + esc(fmtSize(a.size)) + '">' +
      inner +
      '<div class="att-meta"><span class="att-name">' + esc(a.filename || '文件') + '</span>' +
      '<span class="att-size">' + esc(fmtSize(a.size)) + '</span></div>' +
      '<button class="att-edit" data-edit="' + esc(a.id) + '" title="重命名"><svg class="ic"><use href="#i-edit"/></svg></button>' +
      (opts.deletable ? '<button class="att-del" data-del="' + esc(a.id) + '" title="删除附件">×</button>' : '') +
      (opts.entryId != null ? '<a class="att-entry" href="#/entry/' + encodeURIComponent(opts.entryId) + '">所属记录 →</a>' : '') +
      '</div>';
  }

  // URL 协议白名单：仅允许站内路径与 http(s)，防止 javascript: 等协议
  function safeUrl(url) {
    var u = String(url == null ? '' : url).trim();
    return (/^(https?:\/\/|\/)/i.test(u)) ? u : '';
  }

  // 附件磁贴点击：铅笔改名；图片/视频走原有弹层；文件与音频走应用内预览
  function bindAttTiles(container) {
    container.addEventListener('click', function (e) {
      var editBtn = e.target.closest('.att-edit');
      if (editBtn) { e.stopPropagation(); renameAttachment(editBtn); return; }
      if (e.target.closest('.att-del') || e.target.closest('.att-entry')) return;
      var tile = e.target.closest('.att-tile');
      if (!tile) return;
      var kind = tile.dataset.kind, url = safeUrl(tile.dataset.url);
      if (!url) return;
      if (kind === 'image') openLightbox('image', url);
      else if (kind === 'video') openLightbox('video', url);
      else openFilePreview(kind, url, tile.dataset.name || '文件', tile.dataset.size || '');
    });
  }

  // 附件重命名（只改显示名，扩展名服务端保留）
  async function renameAttachment(btn) {
    var tile = btn.closest('.att-tile');
    var attId = btn.dataset.edit;
    var curName = tile.dataset.name || '';
    var name = await inputModal({
      title: '重命名附件',
      body: '<p class="modal-text">只改显示名，扩展名会自动保留</p>',
      value: curName,
      okText: '保存'
    });
    if (name == null) return;
    name = name.trim();
    if (!name || name === curName) return;
    btn.disabled = true;
    try {
      var att = await api('/api/attachments/' + encodeURIComponent(attId), { method: 'PATCH', body: { filename: name } });
      var newName = (att && att.filename) || name;
      tile.dataset.name = newName;
      var nameEl = tile.querySelector('.att-name');
      if (nameEl) nameEl.textContent = newName;
      if (currentEntry && Array.isArray(currentEntry.attachments)) {
        var hit = currentEntry.attachments.find(function (a) { return String(a.id) === String(attId); });
        if (hit) hit.filename = newName;
      }
      toast('已重命名', 'success');
    } catch (e) {
      toast(e.message, 'error');
    }
    btn.disabled = false;
  }

  /* ---- 附件应用内预览（文本 / PDF / 音频 / 兜底下载） ---- */
  var TEXT_EXTS = {};
  ('txt md markdown json csv log py js ts jsx tsx css html htm xml yml yaml toml ini conf sh bat java c cpp h go rs sql vue')
    .split(' ').forEach(function (x) { TEXT_EXTS[x] = 1; });
  var AUDIO_EXTS = {};
  ('mp3 wav m4a ogg flac aac').split(' ').forEach(function (x) { AUDIO_EXTS[x] = 1; });

  function openFilePreview(kind, url, name, sizeLabel) {
    var lb = $('#lightbox');
    var ext = (name.lastIndexOf('.') >= 0 ? name.split('.').pop() : '').toLowerCase();
    var head =
      '<div class="pv-head">' +
      '<div class="pv-name">' + esc(name) + (sizeLabel ? ' <span class="pv-size">' + esc(sizeLabel) + '</span>' : '') + '</div>' +
      '<a class="pv-open" href="' + esc(url) + '" target="_blank" rel="noopener noreferrer">新窗口打开 ↗</a>' +
      '<button class="pv-close" title="关闭">×</button>' +
      '</div>';
    lb.classList.remove('hidden');
    lb.onclick = function (e) { if (e.target === lb) closeLightbox(); };
    var closeBtn = function () { var b = lb.querySelector('.pv-close'); if (b) b.onclick = closeLightbox; };

    if (kind === 'audio' || AUDIO_EXTS[ext]) {
      lb.innerHTML = '<div class="pv-box pv-box-sm">' + head +
        '<div class="pv-audio"><audio controls src="' + esc(url) + '"></audio></div></div>';
      closeBtn();
    } else if (ext === 'pdf') {
      lb.innerHTML = '<div class="pv-box pv-box-pdf">' + head +
        '<iframe class="pv-frame" src="' + esc(url) + '"></iframe></div>';
      closeBtn();
    } else if (TEXT_EXTS[ext]) {
      lb.innerHTML = '<div class="pv-box">' + head + '<pre class="pv-text">正在读取…</pre></div>';
      closeBtn();
      fetch(url).then(function (res) {
        if (!res.ok) throw new Error('读取失败（' + res.status + '）');
        return res.text();
      }).then(function (text) {
        if (text.length > 204800) text = text.slice(0, 204800) + '\n…（内容过长已截断）';
        var pre = lb.querySelector('.pv-text');
        if (pre) pre.textContent = text;
      }).catch(function (err) {
        var pre = lb.querySelector('.pv-text');
        if (pre) pre.textContent = err.message || '读取失败，可以试试右上角「新窗口打开」';
      });
    } else {
      lb.innerHTML = '<div class="pv-box pv-box-sm">' + head +
        '<div class="pv-fallback"><p>这种格式暂时只能下载查看</p>' +
        '<a class="btn btn-ghost" href="' + esc(url) + '" download>下载文件</a></div></div>';
      closeBtn();
    }
  }

  /* ---------------- ECharts 管理 ---------------- */
  var charts = [];
  function makeChart(domId, option) {
    if (!LIB.echarts) return null;
    var dom = document.getElementById(domId);
    if (!dom) return null;
    disposeChart(domId); // 同 id 旧实例先销毁：页内重建（如圈子「关系」tab 反复进入）不经过 route()，否则会泄漏
    var c = window.echarts.init(dom);
    c.setOption(option);
    charts.push(c);
    return c;
  }
  // 销毁同 id 或容器已脱离文档的实例（只动这两类，不误杀同页其他活图表）
  function disposeChart(domId) {
    charts = charts.filter(function (c) {
      try {
        var d = c.getDom && c.getDom();
        if (!d || d.id === domId || !document.body.contains(d)) { c.dispose(); return false; }
      } catch (e) { return false; /* 已销毁的实例直接从注册表剔除 */ }
      return true;
    });
  }
  function disposeCharts() {
    charts.forEach(function (c) { try { c.dispose(); } catch (e) { /* 忽略 */ } });
    charts = [];
  }
  window.addEventListener('resize', function () {
    charts.forEach(function (c) { try { c.resize(); } catch (e) { /* 忽略 */ } });
  });

  /* 图表配色唯一来源：与设计变量同族（暖杏 / 鼠尾草绿 / 暖黄） */
  var CHART_COLORS = {
    accent: '#e08e4f',
    accentDeep: '#c97b3d',
    green: '#8fb398',
    yellow: '#dbbd7e',
    red: '#cf8d7b',
    brown: '#b3a284',
    palette: ['#e08e4f', '#8fb398', '#dbbd7e', '#cf8d7b', '#b3a284', '#a4c3ad', '#eac9a3', '#9db8a4'],
    heatmap: ['#f8ecdc', '#f3cf9f', '#e08e4f', '#c97b3d'],
    axis: '#d8cbb4',
    axisText: '#a09482',
    labelText: '#463f35', /* 图内标签主色（= --ink） */
    muted: '#a09482',     /* 图内次级文字（= --muted，与 axisText 同值别名） */
    splitLine: '#f0e7d6',
    card: '#fffefb'
  };

  /* ---------------- 路由 ---------------- */
  function currentRoute() {
    var h = location.hash.replace(/^#\/?/, '');
    var parts = h.split('/').filter(Boolean);
    return {
      name: parts[0] || 'today',
      param: parts.length > 1 ? decodeURIComponent(parts.slice(1).join('/')) : null
    };
  }

  async function route() {
    disposeCharts();
    closeLightbox();
    closeModal();
    hideOverlay();
    closeYearReview();
    if (closeQcPop) closeQcPop();
    if (todaySyncTimer) { clearInterval(todaySyncTimer); todaySyncTimer = null; }
    if (backfillTimer) { clearTimeout(backfillTimer); backfillTimer = null; }
    var r = currentRoute();
    var navKey = r.name === 'entry' ? 'entries' : r.name;
    $$('#sideNav a, #tabbar a').forEach(function (a) {
      a.classList.toggle('active', a.dataset.route === navKey);
    });
    window.scrollTo(0, 0);
    var view = $('#view');
    try {
      if (r.name === 'entry' && r.param) await renderEntry(view, r.param);
      else if (r.name === 'reports' && r.param) await renderReportDetail(view, r.param);
      else if (PAGES[r.name]) await PAGES[r.name](view);
      else await renderToday(view);
    } catch (e) {
      view.innerHTML = '<div class="page">' + errorHtml(e.message) +
        '<div class="center"><a class="btn" href="#/today">回到今天</a></div></div>';
    }
  }

  window.addEventListener('hashchange', route);

  /* ================= 今天页 ================= */
  // is_work：null=交给小满判断（默认），true=用户点亮了「工作」
  var qc = { files: [], link: '', is_work: null };

  // 「工作」开关的界面同步（点亮/熄灭 + 旁边小字）
  function syncQcWorkPill() {
    var btn = $('#qc-work');
    if (btn) btn.classList.toggle('active', qc.is_work === true);
    var hint = $('#qc-work-hint');
    if (hint) hint.classList.toggle('hidden', qc.is_work === true);
  }

  /* ---- 日志模板小节：就是正文里的一行【标题】 ---- */
  var LOG_PRESETS = ['工作内容', '挑战', '解决办法', '提升', '手记', '见识', '学习']; // 默认 7 项（回退用）
  var presetsCache = null; // settings.log_presets 缓存
  async function getLogPresets() {
    if (presetsCache) return presetsCache;
    try {
      var st = await api('/api/settings');
      var arr = st && st.log_presets;
      presetsCache = (Array.isArray(arr) && arr.length ? arr : LOG_PRESETS).slice(0, 12);
    } catch (e) {
      presetsCache = LOG_PRESETS.slice();
    }
    return presetsCache;
  }
  // 渲染页面时先给默认 7 项，拿到设置后刷新行内容
  function refreshTplRow(sel) {
    getLogPresets().then(function (list) {
      var el = $(sel);
      if (el) el.innerHTML = tplChipsHtml(list);
    });
  }
  function tplChipsHtml(list) {
    return (list || LOG_PRESETS).map(function (t) {
      return '<button class="tool-btn tpl" data-tpl="' + esc(t) + '">' + esc(t) + '</button>';
    }).join('');
  }
  function insertSection(textarea, title) {
    var cur = textarea.value;
    var add = '【' + title + '】\n';
    textarea.value = cur ? cur + '\n\n' + add : add;
    var pos = textarea.value.length;
    textarea.focus();
    try { textarea.setSelectionRange(pos, pos); } catch (e) { /* 忽略 */ }
    textarea.scrollTop = textarea.scrollHeight;
  }

  /* ---- 快速记录草稿（localStorage，300ms 防抖） ---- */
  var qcDraftTimer = null;
  function saveQcDraft() {
    clearTimeout(qcDraftTimer);
    qcDraftTimer = setTimeout(function () {
      var textEl = $('#qc-text');
      if (!textEl) return;
      var text = textEl.value;
      try {
        if (!text.trim() && !qc.link) {
          localStorage.removeItem('qc_draft');
        } else {
          localStorage.setItem('qc_draft', JSON.stringify({
            text: text, is_work: qc.is_work, link: qc.link, ts: Date.now()
          }));
        }
      } catch (e) { /* 存储失败就算了 */ }
    }, 300);
  }
  function clearQcDraft() {
    try { localStorage.removeItem('qc_draft'); } catch (e) { /* 忽略 */ }
  }
  function restoreQcDraft() {
    var d = null;
    try {
      var raw = localStorage.getItem('qc_draft');
      if (raw) d = JSON.parse(raw);
    } catch (e) { d = null; }
    if (!d || (!d.text && !d.link)) return;
    var textEl = $('#qc-text');
    if (!textEl) return;
    textEl.value = d.text || '';
    qc.link = d.link || '';
    // 旧草稿迁移：category=work → is_work=true，其余 → null（交给小满）
    if (typeof d.is_work === 'boolean') qc.is_work = d.is_work;
    else qc.is_work = d.category === 'work' ? true : null;
    syncQcWorkPill();
    renderQcChips();
    var note = $('#qc-draft-note');
    if (note) note.classList.remove('hidden');
  }

  /* ---- 今日节气（本地近似区间表） ---- */
  var SOLAR_TERMS = [
    [1, 5, '小寒'], [1, 20, '大寒'], [2, 4, '立春'], [2, 19, '雨水'],
    [3, 5, '惊蛰'], [3, 20, '春分'], [4, 5, '清明'], [4, 20, '谷雨'],
    [5, 5, '立夏'], [5, 20, '小满'], [6, 5, '芒种'], [6, 21, '夏至'],
    [7, 7, '小暑'], [7, 23, '大暑'], [8, 7, '立秋'], [8, 23, '处暑'],
    [9, 7, '白露'], [9, 23, '秋分'], [10, 8, '寒露'], [10, 23, '霜降'],
    [11, 7, '立冬'], [11, 22, '小雪'], [12, 7, '大雪'], [12, 22, '冬至']
  ];
  function solarTerm(d) {
    d = d || new Date();
    var key = (d.getMonth() + 1) * 100 + d.getDate();
    var name = SOLAR_TERMS[SOLAR_TERMS.length - 1][2]; // 冬至（跨年）
    for (var i = 0; i < SOLAR_TERMS.length; i++) {
      var k = SOLAR_TERMS[i][0] * 100 + SOLAR_TERMS[i][1];
      if (key >= k) name = SOLAR_TERMS[i][2];
      else break;
    }
    return name;
  }

  async function renderToday(view) {
    var warmLines = [
      '每一步都算数。',
      '认真生活的人，运气都不会太差。',
      '记录，是给未来的自己的礼物。',
      '小进步也值得被看见。',
      '今天的你，也在慢慢变好。',
      '别急，慢慢来，反而更快。'
    ];
    var now = new Date();
    var h = now.getHours();
    var greet = h < 5 ? '夜深了' : h < 12 ? '早上好' : h < 18 ? '下午好' : '晚上好';
    var line = warmLines[now.getDate() % warmLines.length];
    qc = { files: [], link: '', is_work: null };

    var dateLine = now.toLocaleDateString('zh-CN', { month: 'long', day: 'numeric', weekday: 'long' });
    view.innerHTML =
      '<div class="page">' +
      '<header class="hero">' +
      '<h1>' + greet + '</h1>' +
      '<div class="hero-date">' + esc(dateLine) + '<span class="term-tag">· ' + esc(solarTerm(now)) + '</span></div>' +
      '<p class="hero-sub">' + esc(line) + '</p>' +
      '</header>' +
      '<div id="today-intent-slot"></div>' +
      '<div id="circle-hint-slot"></div>' +
      '<div id="notice-slot"></div>' +
      '<div id="capsule-slot"></div>' +
      '<div id="blank-slot"></div>' +
      '<div id="intent-slot"></div>' +
      '<div id="sync-slot"></div>' +
      '<div class="quick-links">' +
      '<a href="#/entries">全部记录</a><a href="#/reports">报告</a><a href="#/calendar">日历</a>' +
      '<a href="#/insights">数据</a><a href="#/growth">成长</a><a href="#/knowledge">知识库</a>' +
      '</div>' +
      '<div id="weather-slot"></div>' +
      '<section class="card quick-card">' +
      '<textarea id="qc-text" class="input qc-text" rows="4" placeholder="此刻想记录什么…（标题、标签都可以之后再说）"></textarea>' +
      '<div class="qc-muse-row"><div class="qc-muse hidden" id="qc-muse"></div>' +
      '<div class="qc-muse-links">' +
      '<button class="link-btn qc-ask" id="qc-capsule">✦ 给未来留句话</button>' +
      '<button class="link-btn qc-ask" id="qc-checkin">✦ 让小满问我两句</button>' +
      '</div></div>' +
      '<div class="qc-chips" id="qc-chips"></div>' +
      '<div class="draft-note hidden" id="qc-draft-note">已恢复未保存的草稿</div>' +
      '<div class="qc-tpl" id="qc-tpl">' + tplChipsHtml() + '</div>' +
      '<div class="qc-row">' +
      '<div class="qc-btns">' +
      '<div class="qc-plus-wrap">' +
      '<button class="qc-plus" id="qc-plus" title="添加照片、视频、文件或链接"><svg class="ic"><use href="#i-plus"/></svg></button>' +
      '<div class="qc-pop hidden" id="qc-pop">' +
      '<button class="qc-pop-item" data-act="camera"><svg class="ic"><use href="#i-cam"/></svg><span>拍照</span></button>' +
      '<button class="qc-pop-item" data-act="image"><svg class="ic"><use href="#i-img"/></svg><span>图片</span></button>' +
      '<button class="qc-pop-item" data-act="video"><svg class="ic"><use href="#i-video"/></svg><span>视频</span></button>' +
      '<button class="qc-pop-item" data-act="file"><svg class="ic"><use href="#i-file"/></svg><span>文件</span></button>' +
      '<button class="qc-pop-item" data-act="link"><svg class="ic"><use href="#i-link"/></svg><span>链接</span></button>' +
      '<button class="qc-pop-item" data-act="scan"><svg class="ic"><use href="#i-doc"/></svg><span>拍照识别</span></button>' +
      '</div></div>' +
      '</div>' +
      '<div class="qc-side">' +
      '<input type="datetime-local" class="input qc-when" id="qc-when" title="记录时间（补写以前的日记就改这里）">' +
      '<div class="qc-cats">' +
      '<button class="pill qc-work-pill" id="qc-work" title="点亮表示这是工作；不点就是交给小满判断"><svg class="ic"><use href="#i-brief"/></svg>工作</button>' +
      '<span class="qc-work-hint" id="qc-work-hint">交给小满</span>' +
      '</div>' +
      '</div>' +
      '</div>' +
      '<div class="qc-progress hidden" id="qc-progress"><div class="bar"><i id="qc-bar"></i></div></div>' +
      '<div class="qc-foot"><span class="qc-hint">要补写以前的日记？改上面的日期就行 · Ctrl + Enter 保存</span>' +
      '<div class="qc-foot-right">' +
      '<button class="btn" id="qc-save">记下这一刻</button>' +
      '</div></div>' +
      '</section>' +
      '<section><h2 class="sec-title">今天的时间线</h2><div id="today-list">' + loadingHtml() + '</div></section>' +
      '<div id="otd-slot"></div>' +
      '</div>';

    initWeather();
    // 「工作」开关：点亮=is_work=true；再点一下熄灭=交给小满（null）
    $('#qc-work').onclick = function () {
      qc.is_work = qc.is_work === true ? null : true;
      syncQcWorkPill();
      saveQcDraft();
    };

    function pickFile(accept, capture) {
      var inp = document.createElement('input');
      inp.type = 'file';
      if (accept) inp.accept = accept;
      if (capture) inp.setAttribute('capture', 'environment');
      else inp.multiple = true;
      inp.onchange = function () {
        var fs = Array.prototype.slice.call(inp.files || []);
        if (fs.length) { qc.files = qc.files.concat(fs); renderQcChips(); }
      };
      inp.click();
    }
    var mediaActions = {
      camera: function () { pickFile('image/*', true); },
      image: function () { pickFile('image/*', false); },
      video: function () { pickFile('video/*', false); },
      file: function () { pickFile('', false); },
      link: async function () {
        var url = await inputModal({ title: '添加链接', placeholder: 'https://…', okText: '添加' });
        if (url == null) return;
        if (!/^https?:\/\//i.test(url)) { toast('链接需要以 http:// 或 https:// 开头', 'error'); return; }
        qc.link = url;
        renderQcChips();
        saveQcDraft();
      },
      scan: scanNote
    };
    // 「+」浮层：点空白处 / Esc / 路由离开均可关闭
    var qcPop = $('#qc-pop');
    closeQcPop = function () {
      qcPop.classList.add('hidden');
      document.removeEventListener('click', onQcDocClick, true);
    };
    var onQcDocClick = function (e) {
      if (!e.target.closest('.qc-plus-wrap')) closeQcPop();
    };
    $('#qc-plus').onclick = function (e) {
      e.stopPropagation();
      var willOpen = qcPop.classList.contains('hidden');
      qcPop.classList.toggle('hidden');
      if (willOpen) document.addEventListener('click', onQcDocClick, true);
      else document.removeEventListener('click', onQcDocClick, true);
    };
    qcPop.addEventListener('click', function (e) {
      var item = e.target.closest('[data-act]');
      if (!item) return;
      closeQcPop();
      mediaActions[item.dataset.act]();
    });
    // 模板小节：往正文追加【标题】，并顺手点亮「工作」（可再点灭）
    $('#qc-tpl').addEventListener('click', function (ev) {
      var btn = ev.target.closest('[data-tpl]');
      if (!btn) return;
      insertSection($('#qc-text'), btn.dataset.tpl);
      qc.is_work = true;
      syncQcWorkPill();
      saveQcDraft();
    });
    // 补记用的时间选择，默认当前时间
    $('#qc-when').value = toLocalInputValue();

    $('#qc-save').onclick = quickSave;
    $('#qc-text').addEventListener('keydown', function (e) {
      if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') quickSave();
    });
    // 输入框随内容长高，接近一屏高才出现内部滚动条
    var qcGrow = function () {
      var el = $('#qc-text');
      if (!el) return;
      el.style.height = 'auto';
      var cap = Math.round(window.innerHeight * 0.7);
      el.style.height = Math.min(el.scrollHeight, cap) + 'px';
      el.style.overflowY = el.scrollHeight > cap ? 'auto' : 'hidden';
    };
    $('#qc-text').addEventListener('input', function () { saveQcDraft(); qcGrow(); });

    restoreQcDraft();
    qcGrow();
    refreshTplRow('#qc-tpl');
    updateSyncChip();
    flushOutboxSafe();
    todaySyncTimer = setInterval(flushOutboxSafe, 60000);

    // 小巧思：muse 一句 / 空白日轻提醒 / N 天前的今天（都安静、失败静默）
    api('/api/muse').then(function (m) {
      var el = $('#qc-muse');
      if (el && m && m.text) {
        el.textContent = '✦ ' + m.text;
        el.classList.remove('hidden');
      }
    }).catch(function () { /* 不显示 */ });
    $('#qc-checkin').onclick = openCheckin;
    $('#qc-capsule').onclick = openCapsuleModal;
    blankHint();
    loadTodayIntent();
    loadCircleHint();
    loadGlobalNotice();
    loadCapsules();
    loadIntents();
    loadOnThisDay();

    loadTodayList();
  }

  /* ---- 今日意图：顶部低调一行，失焦自动存（零按钮） ---- */
  async function loadTodayIntent() {
    var slot = $('#today-intent-slot');
    if (!slot) return;
    var day = localDateStr();
    var text = '';
    try {
      var d = await api('/api/intent?date=' + day);
      text = (d && d.text) || '';
    } catch (e) { return; } // 端点不在就静默不出
    if (!document.body.contains(slot)) return;
    slot.innerHTML =
      '<div class="intent-input-row">' +
      '<input class="input intent-input" id="ti-input" placeholder="今天打算……" maxlength="120" value="' + esc(text) + '">' +
      '<span class="intent-saved hidden" id="ti-saved">已记下</span>' +
      '</div>';
    var inp = $('#ti-input');
    var saved = $('#ti-saved');
    var savedTimer = null;
    var save = async function () {
      var v = inp.value.trim();
      if (v === text) return; // 没改过不发
      try {
        await api('/api/intent', { method: 'PUT', body: { day: day, text: v } });
        text = v;
        saved.classList.remove('hidden');
        clearTimeout(savedTimer);
        savedTimer = setTimeout(function () { saved.classList.add('hidden'); }, 1600);
      } catch (e) { toast(e.message, 'error'); }
    };
    inp.addEventListener('blur', save);
    inp.addEventListener('keydown', function (e) { if (e.key === 'Enter') inp.blur(); });
  }

  // 空白日轻提醒：昨天与前天都无记录时说一句（同一天只提醒一次）
  async function blankHint() {
    var slot = $('#blank-slot');
    if (!slot) return;
    var skey = 'blank_hint_' + localDateStr();
    var shown = false;
    try { shown = !!sessionStorage.getItem(skey); } catch (e) { /* 忽略 */ }
    if (shown) return;
    try {
      var need = [];
      for (var i = 1; i <= 2; i++) {
        var d = new Date();
        d.setDate(d.getDate() - i);
        need.push(d);
      }
      var monthCache = {};
      var counts = {};
      for (var j = 0; j < need.length; j++) {
        var ms = need[j].getFullYear() + '-' + pad(need[j].getMonth() + 1);
        if (!monthCache[ms]) {
          var data = await api('/api/calendar?month=' + ms);
          monthCache[ms] = (data && data.days) || {};
        }
        counts[localDateStr(need[j])] = monthCache[ms][localDateStr(need[j])] || 0;
      }
      if (!document.body.contains(slot)) return;
      if (!counts[localDateStr(need[0])] && !counts[localDateStr(need[1])]) {
        slot.innerHTML = '<div class="blank-hint">前两天留白了呢，今天聊两句？</div>';
        try { sessionStorage.setItem(skey, '1'); } catch (e) { /* 忽略 */ }
      }
    } catch (e) { /* 静默 */ }
  }

  // N 天前的今天（-30 / -60 / -90，取第一个有结果的）
  async function loadOnThisDay() {
    var slot = $('#otd-slot');
    if (!slot) return;
    try {
      for (var i = 1; i <= 3; i++) {
        var n = i * 30;
        var d = new Date();
        d.setDate(d.getDate() - n);
        var data = await api('/api/entries?date=' + localDateStr(d) + '&limit=1');
        var item = data && data.items && data.items[0];
        if (item) {
          if (!document.body.contains(slot)) return;
          var title = item.title || (item.content ? String(item.content).split('\n')[0].slice(0, 40) : '') || '（无标题）';
          slot.innerHTML = '<a class="otd-card" href="#/entry/' + encodeURIComponent(item.id) + '">' +
            n + ' 天前的今天，你记了：《' + esc(title) + '》 →</a>';
          return;
        }
      }
    } catch (e) { /* 静默 */ }
  }

  /* ---- 圈子待审提醒：档案待审 + 合并提案 + 问答 合并计数 ---- */
  async function loadCircleHint() {
    var slot = $('#circle-hint-slot');
    if (!slot) return;
    try {
      var res = await Promise.all([
        api('/api/circle/overview').catch(function () { return null; }),
        api('/api/circle/proposals?status=pending').catch(function () { return null; }),
        api('/api/circle/questions?status=pending').catch(function () { return null; })
      ]);
      // 只统计真正需要你动手的：矛盾待裁定 + 合并提案 + 提问；搁置的草稿不用你管
      var n = (res[0] && res[0].needs_review) || 0;
      var qData = res[2];
      var qCount = 0;
      if (qData) {
        if (Array.isArray(qData.groups)) {
          qCount = qData.groups.reduce(function (s, g) { return s + ((g.questions || []).length); }, 0);
        } else if (Array.isArray(qData.items)) {
          qCount = qData.items.length;
        }
      }
      var m = (((res[1] && res[1].items) || []).length) + qCount;
      if (!n && !m) return;
      if (!document.body.contains(slot)) return;
      var parts = [];
      if (n) parts.push(n + ' 处矛盾等你裁定');
      if (m) parts.push(m + ' 个问题想问你');
      // 有矛盾时直达冲突收件箱；合并提案/问答仍进入默认圈子页。
      var target = n ? '#/circle/review' : '#/circle';
      slot.innerHTML = '<a class="circle-hint" href="' + target + '">圈子有 ' + esc(parts.join(' · ')) + ' →</a>';
    } catch (e) { /* 静默 */ }
  }

  /* ---- 时间胶囊：到期的话 + 给未来留句话 ---- */
  async function loadCapsules() {
    var slot = $('#capsule-slot');
    if (!slot) return;
    try {
      var data = await api('/api/capsules/due');
      var items = (data && data.items) || [];
      if (!items.length || !document.body.contains(slot)) return;
      slot.innerHTML = items.map(function (c) {
        var d = parseDate(c.created_at);
        var when = d ? (d.getMonth() + 1) + '月' + d.getDate() + '日' : '那时';
        return '<div class="capsule-card" data-cid="' + esc(c.id) + '">' +
          '<div class="capsule-when">' + esc(when) + ' 的你留给今天的话：</div>' +
          '<div class="capsule-content">' + esc(c.content) + '</div>' +
          '<button class="btn btn-ghost btn-sm" data-capsule="' + esc(c.id) + '">收下了</button>' +
          '</div>';
      }).join('');
      $$('#capsule-slot [data-capsule]').forEach(function (btn) {
        btn.onclick = async function () {
          btn.disabled = true;
          try {
            await api('/api/capsules/' + encodeURIComponent(btn.dataset.capsule) + '/dismiss', { method: 'POST' });
            var card = btn.closest('.capsule-card');
            card.style.opacity = '0';
            setTimeout(function () { card.remove(); }, 250);
          } catch (e) {
            toast(e.message, 'error');
            btn.disabled = false;
          }
        };
      });
    } catch (e) { /* 静默 */ }
  }

  function openCapsuleModal() {
    var root = $('#modal-root');
    var d90 = new Date();
    d90.setDate(d90.getDate() + 90);
    var d1 = new Date();
    d1.setDate(d1.getDate() + 1);
    root.innerHTML =
      '<div class="modal-wrap"><div class="modal">' +
      '<h3 class="modal-title">给未来留句话</h3>' +
      '<div class="set-row"><label>想对未来的自己说什么</label>' +
      '<textarea class="input" id="cap-text" rows="4" placeholder="写给未来的自己…"></textarea></div>' +
      '<div class="set-row"><label>开启日期</label>' +
      '<input class="input" type="date" id="cap-date" value="' + localDateStr(d90) + '" min="' + localDateStr(d1) + '"></div>' +
      '<div class="modal-btns">' +
      '<button class="btn btn-ghost" id="cap-cancel">取消</button>' +
      '<button class="btn" id="cap-ok">收进胶囊</button>' +
      '</div></div></div>';
    $('.modal-wrap', root).addEventListener('click', function (e) {
      if (e.target.classList.contains('modal-wrap')) closeModal();
    });
    $('#cap-cancel').onclick = closeModal;
    $('#cap-ok').onclick = async function () {
      var content = $('#cap-text').value.trim();
      var date = $('#cap-date').value;
      if (!content) { toast('先写点什么吧', 'error'); return; }
      if (!date) { toast('选一个开启日期', 'error'); return; }
      var btn = this;
      btn.disabled = true;
      try {
        await api('/api/capsules', { method: 'POST', body: { content: content, unlock_date: date } });
        closeModal();
        var d = parseDate(date + 'T00:00:00');
        toast('已经替你收好，' + (d.getMonth() + 1) + '月' + d.getDate() + '日见', 'success', 4500);
      } catch (e) {
        toast(e.message, 'error');
        btn.disabled = false;
      }
    };
    $('#cap-text').focus();
  }

  /* ---- 明日接力：昨天说今天要做的事（当天关闭后不再显示） ---- */
  async function loadIntents() {
    var slot = $('#intent-slot');
    if (!slot) return;
    var skey = 'intents_hide_' + localDateStr();
    try {
      if (sessionStorage.getItem(skey)) return;
      var data = await api('/api/next-day-intents');
      var items = (data && data.items) || [];
      if (!items.length || !document.body.contains(slot)) return;
      slot.innerHTML =
        '<div class="intent-card">' +
        '<button class="pv-close intent-close" id="intent-close" title="知道了">×</button>' +
        '<div class="intent-title">昨天你说今天要：</div>' +
        '<ul class="intent-list">' +
        items.map(function (t) { return '<li>' + esc(t) + '</li>'; }).join('') +
        '</ul></div>';
      $('#intent-close').onclick = function () {
        slot.innerHTML = '';
        try { sessionStorage.setItem(skey, '1'); } catch (e) { /* 忽略 */ }
      };
    } catch (e) { /* 静默 */ }
  }

  /* ---- 对话式记录：让小满问我两句 ---- */
  var ckHistory = [];

  function ckBubble(role, text) {
    var flow = $('#ck-flow');
    if (!flow) return;
    var div = document.createElement('div');
    div.className = 'ck-bubble ' + (role === 'user' ? 'user' : 'ai');
    div.textContent = text;
    flow.appendChild(div);
    flow.scrollTop = flow.scrollHeight;
  }

  async function ckTurn() {
    var flow = $('#ck-flow');
    if (!flow) return;
    var loading = document.createElement('div');
    loading.className = 'ck-bubble ai ck-thinking';
    loading.textContent = '小满正在想…';
    flow.appendChild(loading);
    flow.scrollTop = flow.scrollHeight;
    try {
      var r = await api('/api/checkin', {
        method: 'POST',
        body: { history: ckHistory },
        timeout: 120000
      });
      loading.remove();
      if (r && r.reply) {
        ckHistory.push({ role: 'assistant', content: r.reply });
        ckBubble('assistant', r.reply);
      }
      if (r && r.done && r.entry_content) {
        await api('/api/entries', {
          method: 'POST',
          body: {
            occurred_at: new Date().toISOString(),
            content: r.entry_content,
            latitude: window.__geo ? window.__geo.lat : undefined,
            longitude: window.__geo ? window.__geo.lon : undefined
          }
        });
        toast('已经帮你记下来了', 'success');
        closeModal();
        loadTodayList();
      }
    } catch (e) {
      loading.remove();
      if (e.status === 400) {
        ckBubble('assistant', e.message + '（去「设置 → AI 接口」配好大模型就能聊了）');
      } else {
        toast(e.message, 'error');
      }
    }
  }

  function openCheckin() {
    ckHistory = [];
    var root = $('#modal-root');
    root.innerHTML =
      '<div class="modal-wrap"><div class="modal checkin-modal">' +
      '<h3 class="modal-title">小满想问你两句</h3>' +
      '<div class="checkin-flow" id="ck-flow"></div>' +
      '<div class="checkin-input-row">' +
      '<input class="input" id="ck-input" placeholder="说点什么…">' +
      '<button class="btn btn-sm" id="ck-send">发送</button>' +
      '</div>' +
      '<div class="checkin-foot">' +
      '<button class="link-btn" id="ck-finish">直接保存结束</button>' +
      '<button class="link-btn" id="ck-close">先聊到这</button>' +
      '</div></div></div>';
    $('.modal-wrap', root).addEventListener('click', function (e) {
      if (e.target.classList.contains('modal-wrap')) closeModal();
    });
    $('#ck-close').onclick = closeModal;
    $('#ck-finish').onclick = function () {
      ckHistory.push({ role: 'user', content: '我想先聊到这里，请帮我总结并保存。' });
      ckBubble('user', '我想先聊到这里，请帮我总结并保存。');
      ckTurn();
    };
    var send = function () {
      var inp = $('#ck-input');
      var text = inp.value.trim();
      if (!text) return;
      inp.value = '';
      ckHistory.push({ role: 'user', content: text });
      ckBubble('user', text);
      ckTurn();
    };
    $('#ck-send').onclick = send;
    $('#ck-input').addEventListener('keydown', function (e) { if (e.key === 'Enter') send(); });
    $('#ck-input').focus();
    ckTurn(); // 空 history 先拿开场白
  }

  function renderQcChips() {
    var box = $('#qc-chips');
    if (!box) return;
    var html = qc.files.map(function (f, i) {
      return '<span class="qc-chip"><span>' + esc(f.name) + '</span><button data-rm-file="' + i + '" title="移除">×</button></span>';
    }).join('');
    if (qc.link) {
      html += '<span class="qc-chip"><span>' + esc(qc.link) + '</span><button data-rm-link="1" title="移除">×</button></span>';
    }
    box.innerHTML = html;
    $$('#qc-chips [data-rm-file]').forEach(function (b) {
      b.onclick = function () { qc.files.splice(Number(b.dataset.rmFile), 1); renderQcChips(); };
    });
    var rl = $('#qc-chips [data-rm-link]');
    if (rl) rl.onclick = function () { qc.link = ''; renderQcChips(); };
  }

  async function quickSave() {
    var textEl = $('#qc-text');
    var text = textEl.value.trim();
    if (!text && !qc.files.length) { toast('先写点什么，或添加一个附件吧', 'error'); return; }
    var btn = $('#qc-save');
    btn.disabled = true;
    btn.textContent = '保存中…';
    var created = null; // 记录是否已在服务端建成（避免附件失败时重复入 outbox）
    // 补记：用选择器里的时间作为发生时间（空则现在）
    var whenVal = ($('#qc-when') && $('#qc-when').value) || '';
    var occurred = whenVal ? new Date(whenVal) : new Date();
    if (isNaN(occurred.getTime())) occurred = new Date();
    var pastDate = localDateStr(occurred) !== localDateStr();
    var occurredIso = occurred.toISOString();
    try {
      // is_work 只在用户点过「工作」开关时携带；其余交给小满判断
      var payload = {
        occurred_at: occurredIso, content: text,
        latitude: window.__geo ? window.__geo.lat : undefined,
        longitude: window.__geo ? window.__geo.lon : undefined
      };
      if (qc.is_work === true) payload.is_work = true;
      var entry = await api('/api/entries', { method: 'POST', body: payload });
      created = entry;
      if (qc.files.length) {
        $('#qc-progress').classList.remove('hidden');
        var bar0 = $('#qc-bar');
        if (bar0) bar0.style.width = '0';
        entry = await uploadFiles(entry.id, qc.files, function (pct) {
          var bar = $('#qc-bar');
          if (bar) bar.style.width = pct + '%';
        });
        $('#qc-progress').classList.add('hidden');
      }
      if (qc.link) {
        try {
          entry = await api('/api/entries/' + encodeURIComponent(entry.id) + '/links', {
            method: 'POST', body: { url: qc.link }
          });
        } catch (e) { toast('链接没有保存成功：' + e.message, 'error'); }
      }
      // 里程碑彩蛋：刚跨过节点时换成庆祝文案（仅此一处用 emoji）
      var celebrate = null;
      try {
        var ov = await api('/api/stats/overview');
        if (ov) {
          if ([7, 21, 30, 60, 100].indexOf(ov.streak_days) !== -1) {
            celebrate = '连续记录 ' + ov.streak_days + ' 天啦，小满渐盈 🌱';
          } else if ([10, 50, 100, 200, 365, 500, 1000].indexOf(ov.total_entries) !== -1) {
            celebrate = '第 ' + ov.total_entries + ' 条记录啦，小满渐盈 🌱';
          }
        }
      } catch (e) { /* 统计失败就用普通提示 */ }
      if (celebrate) {
        toast(celebrate, 'success', 5000);
      } else if (pastDate) {
        toast('已补记到 ' + (occurred.getMonth() + 1) + '月' + occurred.getDate() + '日', 'success');
      } else {
        toast('已记下，继续加油', 'success');
      }
      textEl.value = '';
      textEl.style.height = 'auto';
      qc = { files: [], link: '', is_work: null };
      syncQcWorkPill();
      renderQcChips();
      clearQcDraft();
      if ($('#qc-when')) $('#qc-when').value = toLocalInputValue();
      if (!pastDate) prependTodayItem(entry);
      // 标题/摘要为空的，后台小模型会补：轮询两次，好了就更新界面
      if (!entry.title || !entry.summary) pollAutoFill(entry.id, { context: 'today' });
    } catch (e) {
      if (e.isNetwork && !created) {
        // 连不上电脑：先存在这台手机上，回 Wi-Fi 自动同步
        try {
          await outbox.add({
            occurred_at: occurredIso,
            content: text,
            is_work: qc.is_work === true ? true : null,
            link: qc.link || '',
            latitude: window.__geo ? window.__geo.lat : null,
            longitude: window.__geo ? window.__geo.lon : null,
            files: qc.files.map(function (f) { return { name: f.name, type: f.type, blob: f }; }),
            created_at: new Date().toISOString()
          });
          textEl.value = '';
      textEl.style.height = 'auto';
          qc = { files: [], link: '', is_work: null };
          syncQcWorkPill();
          renderQcChips();
          clearQcDraft();
          if ($('#qc-when')) $('#qc-when').value = toLocalInputValue();
          toast('现在没连上电脑，已先保存在这台手机上，连上 Wi-Fi 后会自动同步', 'success', 5000);
          updateSyncChip();
        } catch (idbErr) {
          toast('离线保存也失败了：' + ((idbErr && idbErr.message) || idbErr), 'error');
        }
      } else if (e.isNetwork && created) {
        toast('记录已保存，但附件没传上去，联网后请在详情页重新上传', 'error', 5000);
        textEl.value = '';
      textEl.style.height = 'auto';
        qc = { files: [], link: '', is_work: null };
        syncQcWorkPill();
        renderQcChips();
        clearQcDraft();
        if (!pastDate) prependTodayItem(created);
      } else {
        toast(e.message, 'error');
      }
    } finally {
      btn.disabled = false;
      btn.textContent = '记下这一刻';
      var prog = $('#qc-progress');
      if (prog) prog.classList.add('hidden');
    }
  }

  /* ---- 拍照识别手写笔记（/api/scan-note，支持多页） ---- */
  function scanNote() {
    var inp = document.createElement('input');
    inp.type = 'file';
    inp.accept = 'image/*';
    inp.multiple = true;
    // 不设 capture：手机会弹出"拍照/相册"选择，相册里可多选几页一起识别
    inp.onchange = async function () {
      var files = inp.files ? Array.prototype.slice.call(inp.files) : [];
      if (!files.length) return;
      if (files.length > 9) { toast('一次最多识别 9 页，请分批上传', 'error', 5000); return; }
      showOverlay(files.length > 1
        ? 'AI 正在识别你的 ' + files.length + ' 页笔记，可能需要一两分钟…'
        : 'AI 正在识别你的笔记，可能需要半分钟…');
      var ctrl = new AbortController();
      var timer = setTimeout(function () { ctrl.abort(); }, 600000);
      try {
        var fd = new FormData();
        files.forEach(function (f) { fd.append('files', f); });
        var res = await fetch('/api/scan-note', { method: 'POST', body: fd, signal: ctrl.signal });
        clearTimeout(timer);
        var j = null;
        try { j = await res.json(); } catch (e2) { /* 忽略 */ }
        hideOverlay();
        if (!res.ok) {
          toast(j && typeof j.detail === 'string' ? j.detail : '识别失败（' + res.status + '）', 'error', 6000);
          return;
        }
        toast('识别完成，请检查一下内容', 'success');
        location.hash = '#/entry/' + encodeURIComponent(j.id);
      } catch (e) {
        clearTimeout(timer);
        hideOverlay();
        toast(ctrl.signal.aborted ? '识别超时了，请稍后再试' : '连不上服务，请确认电脑上的后端已启动', 'error', 5000);
      }
    };
    inp.click();
  }

  /* ---- 待同步 chip 与 outbox 冲刷 ---- */
  var todaySyncTimer = null;
  var flushing = false;

  async function updateSyncChip() {
    var slot = $('#sync-slot');
    if (!slot) return;
    var n = await outbox.count();
    if (!document.body.contains(slot)) return;
    if (n > 0) {
      slot.innerHTML = '<button class="sync-chip" id="sync-chip">☁ ' + n + ' 条待同步，点我重试</button>';
      $('#sync-chip').onclick = flushOutboxSafe;
    } else {
      slot.innerHTML = '';
    }
  }

  async function flushOutbox() {
    if (flushing) return;
    flushing = true;
    var done = 0, finished = false;
    try {
      var items = await outbox.all();
      items.sort(function (a, b) { return a.id - b.id; });
      for (var i = 0; i < items.length; i++) {
        var it = items[i];
        try {
          var body = {
            occurred_at: it.occurred_at,
            content: it.content,
            latitude: it.latitude == null ? undefined : it.latitude,
            longitude: it.longitude == null ? undefined : it.longitude
          };
          if (it.is_work === true) body.is_work = true;
          var entry = await api('/api/entries', { method: 'POST', body: body });
          if (it.files && it.files.length) {
            var fs = it.files.map(function (f) {
              return new File([f.blob], f.name, { type: f.type || undefined });
            });
            entry = await uploadFiles(entry.id, fs, null);
          }
          if (it.link) {
            try {
              entry = await api('/api/entries/' + encodeURIComponent(entry.id) + '/links', {
                method: 'POST', body: { url: it.link }
              });
            } catch (linkErr) {
              if (linkErr.isNetwork) throw linkErr;
              toast('有 1 条记录的链接没有同步成功：' + linkErr.message, 'error');
            }
          }
          await outbox.remove(it.id);
          done++;
        } catch (e) {
          if (e.isNetwork) break; // 还是连不上：保留剩余，下轮再试
          toast('同步中断：' + e.message, 'error');
          break;
        }
        finished = (i === items.length - 1);
      }
      if (done > 0) {
        if (finished) toast('已把 ' + done + ' 条手机记录同步到电脑', 'success', 4500);
        loadTodayList();
      }
      updateSyncChip();
    } finally {
      flushing = false;
    }
  }

  function flushOutboxSafe() { flushOutbox().catch(function () { /* IDB 不可用则静默 */ }); }

  window.addEventListener('online', function () { flushOutboxSafe(); });

  async function loadTodayList() {
    var box = $('#today-list');
    if (!box) return;
    try {
      var data = await api('/api/entries?date=' + localDateStr() + '&limit=50&offset=0');
      if (!data.items || !data.items.length) {
        box.innerHTML = emptyHtml('今天还没有记录，写下第一句吧', '哪怕一句话，也是给未来的礼物');
        return;
      }
      box.innerHTML = data.items.map(function (e) { return entryItemHtml(e, { showRel: true }); }).join('');
    } catch (e) {
      box.innerHTML = errorHtml(e.message);
    }
  }

  function prependTodayItem(entry) {
    var box = $('#today-list');
    if (!box) return;
    if (box.querySelector('.empty')) box.innerHTML = '';
    box.insertAdjacentHTML('afterbegin', entryItemHtml(entry, { showRel: true }));
  }

  /* ---- 后台自动补全的小提示：保存后在约 5s/12s 各查一次，标题出现就同步界面 ---- */
  function pollAutoFill(entryId, opts) {
    opts = opts || {};
    var done = false;
    [5000, 12000].forEach(function (ms) {
      setTimeout(async function () {
        if (done) return;
        var e;
        try { e = await api('/api/entries/' + encodeURIComponent(entryId)); } catch (err) { return; }
        if (!e || !e.title) return;
        done = true;
        var touched = false;
        if (opts.context === 'today') {
          var item = document.querySelector('#today-list a.entry-item[href="#/entry/' + encodeURIComponent(entryId) + '"]');
          if (item) {
            item.outerHTML = entryItemHtml(e, { showRel: true });
            touched = true;
          }
        } else if (opts.context === 'entry') {
          if (currentEntry && String(currentEntry.id) === String(entryId) && $('#e-title')) {
            currentEntry = e;
            $('#e-title').value = e.title || '';
            $('#e-summary').value = e.summary || '';
            $('#e-tags').value = (e.tags || []).join(', ');
            touched = true;
          }
        }
        if (touched) toast('小满帮你补好了标题和摘要', 'success');
      }, ms);
    });
  }

  function initWeather() {
    var slot = $('#weather-slot');
    if (!slot || !navigator.geolocation) return;
    var doGeo = function () {
      navigator.geolocation.getCurrentPosition(async function (pos) {
        window.__geo = { lat: pos.coords.latitude, lon: pos.coords.longitude };
        try {
          var d = await api('/api/context/preview?lat=' + pos.coords.latitude + '&lon=' + pos.coords.longitude + '&date=' + localDateStr());
          if (!document.body.contains(slot)) return;
          var w = d.weather;
          var wText = '';
          if (w) {
            wText = esc(w.text || '');
            if (w.temperature_c != null) wText += (wText ? ' · ' : '') + esc(w.temperature_c) + '°C';
            if (w.humidity != null) wText += ' · 湿度 ' + esc(w.humidity) + '%';
          }
          slot.innerHTML = '<div class="card weather-card">' +
            '<svg class="wc-pin"><use href="#i-pin"/></svg>' +
            '<div><div class="wc-loc">' + esc(d.location_name || '当前位置') + '</div>' +
            (wText ? '<div class="wc-w">' + wText + '</div>' : '') +
            '</div></div>';
        } catch (e) { /* 静默隐藏天气卡片 */ }
      }, function () { /* 用户拒绝定位：静默 */ }, { timeout: 8000, maximumAge: 600000 });
    };
    // 天气开关关闭时：不请求定位，卡片保持隐藏（设置读取失败则按原流程静默尝试）
    api('/api/settings').then(function (st) {
      if (st && st.weather_enabled === false) return;
      doGeo();
    }).catch(function () { doGeo(); });
  }

  /* ================= 记录页 ================= */
  var entriesState = null;
  var categoryCache = null; // 动态类目 [{slug, name_zh}]

  // 动态类目下拉（GET /api/categories），失败时只留「全部」
  async function loadCategoryFilter() {
    try {
      var data = await api('/api/categories');
      var items = Array.isArray(data) ? data : (data && data.items) || [];
      categoryCache = items;
      var sel = $('#f-cat');
      if (!sel) return;
      sel.innerHTML = '<option value="">全部分类</option>' + items.map(function (c) {
        return '<option value="' + esc(c.slug) + '">' + esc(c.name_zh || c.slug) + '</option>';
      }).join('');
      if (entriesState && entriesState.category) sel.value = entriesState.category;
    } catch (e) { /* 静默 */ }
  }

  function categoryName(slug) {
    var hit = (categoryCache || []).find(function (c) { return c.slug === slug; });
    return hit ? (hit.name_zh || hit.slug) : (CATS[slug] || slug);
  }

  async function renderEntries(view) {
    entriesState = { q: '', category: '', tag: '', work: false, offset: 0, total: 0, loading: false };
    view.innerHTML =
      '<div class="page">' +
      '<header class="page-head entries-head"><h1>记录</h1>' +
      '<span class="head-links"><a class="link-btn" href="#/river">长河 →</a><a class="link-btn" href="#/media">媒体库 →</a></span>' +
      '<p class="page-sub">搜索、筛选，回顾每一个瞬间</p></header>' +
      '<div class="card filters">' +
      '<div class="search-row">' +
      '<input class="input" id="f-q" placeholder="搜索标题、正文、标签…回车搜索">' +
      '<button class="btn btn-ghost" id="f-search"><svg class="ic"><use href="#i-search"/></svg>搜索</button>' +
      '<button class="btn btn-ghost" id="f-ask" title="向小满提问">✦ 问小满</button>' +
      '<button class="btn btn-ghost" id="f-random" title="随便翻翻"><svg class="ic"><use href="#i-shuffle"/></svg></button>' +
      '</div>' +
      '<div id="ask-slot"></div>' +
      '<div class="filter-row">' +
      '<select class="input" id="f-cat"><option value="">全部分类</option></select>' +
      '<button class="pill" id="f-work" title="只看标为工作的记录">只看工作</button>' +
      '<input class="input" id="f-tag" placeholder="按标签筛选，回车确定">' +
      '<button class="btn btn-ghost hidden" id="f-clear">清除条件</button>' +
      '</div>' +
      '<div class="active-filters" id="f-active"></div>' +
      '</div>' +
      '<div id="entries-list">' + loadingHtml() + '</div>' +
      '<div class="center"><button class="btn btn-ghost hidden" id="entries-more">加载更多</button></div>' +
      '</div>';

    function doSearch() {
      entriesState.q = $('#f-q').value.trim();
      loadEntries(true);
    }
    $('#f-search').onclick = doSearch;
    $('#f-q').addEventListener('keydown', function (e) { if (e.key === 'Enter') doSearch(); });
    // 随便翻翻：随机打开一条记录
    $('#f-random').onclick = async function () {
      try {
        var e = await api('/api/entries/random');
        location.hash = '#/entry/' + encodeURIComponent(e.id);
      } catch (err) {
        if (err.status === 404) toast('还没有记录哦', 'error');
        else toast(err.message, 'error');
      }
    };
    // 问小满：用搜索框里的问题问 AI，回答显示在本页，不换页
    $('#f-ask').onclick = async function () {
      var q = $('#f-q').value.trim();
      if (!q) { toast('先输入一个问题吧', 'error'); return; }
      var slot = $('#ask-slot');
      slot.innerHTML = '<div class="ask-loading">' + loadingHtml('小满正在翻你的记录…') + '</div>';
      try {
        var r = await api('/api/ask', { method: 'POST', body: { question: q }, timeout: 600000 });
        if (!document.body.contains(slot)) return;
        var refs = r.refs || [];
        slot.innerHTML =
          '<div class="ask-card">' +
          '<div class="ask-head"><span class="ask-title">小满的回答</span>' +
          '<button class="pv-close" id="ask-close" title="关闭">×</button></div>' +
          '<div class="md">' + mdToHtml(r.answer || '') + '</div>' +
          (refs.length
            ? '<div class="ask-refs">' + refs.map(function (ref) {
                return '<a class="ask-ref" href="#/entry/' + encodeURIComponent(ref.id) + '">' +
                  esc(absDate(ref.occurred_at)) + ' · ' + esc(ref.title || '（无标题）') + '</a>';
              }).join('') + '</div>'
            : '') +
          '</div>';
        $('#ask-close').onclick = function () { slot.innerHTML = ''; };
      } catch (e) {
        slot.innerHTML = '';
        toast(e.message, 'error', 5000);
      }
    };
    $('#f-cat').onchange = function () { entriesState.category = $('#f-cat').value; loadEntries(true); };
    $('#f-work').onclick = function () {
      entriesState.work = !entriesState.work;
      this.classList.toggle('active', entriesState.work);
      loadEntries(true);
    };
    $('#f-tag').addEventListener('keydown', function (e) { if (e.key === 'Enter') { entriesState.tag = $('#f-tag').value.trim(); loadEntries(true); } });
    $('#f-clear').onclick = function () {
      entriesState.q = ''; entriesState.category = ''; entriesState.tag = ''; entriesState.work = false;
      $('#f-q').value = ''; $('#f-cat').value = ''; $('#f-tag').value = '';
      $('#f-work').classList.remove('active');
      loadEntries(true);
    };
    $('#entries-more').onclick = function () { loadEntries(false); };

    loadCategoryFilter();
    loadEntries(true);
  }

  function renderActiveFilters() {
    var s = entriesState;
    var chips = [];
    if (s.q) chips.push('<span class="badge">搜索：' + esc(s.q) + '</span>');
    if (s.category) chips.push('<span class="badge">分类：' + esc(categoryName(s.category)) + '</span>');
    if (s.work) chips.push('<span class="badge">只看工作</span>');
    if (s.tag) chips.push('<span class="badge">标签：#' + esc(s.tag) + '</span>');
    $('#f-active').innerHTML = chips.join('');
    $('#f-clear').classList.toggle('hidden', !(s.q || s.category || s.work || s.tag));
  }

  async function loadEntries(reset) {
    var s = entriesState;
    if (!s || s.loading) return;
    s.loading = true;
    if (reset) { s.offset = 0; $('#entries-list').innerHTML = loadingHtml(); }
    renderActiveFilters();
    var moreBtn = $('#entries-more');
    moreBtn.classList.add('hidden');
    try {
      var data;
      if (s.q) {
        data = await api('/api/search', { method: 'POST', body: { q: s.q, limit: 100 } });
        s.total = data.items ? data.items.length : 0;
        if (reset) $('#entries-list').innerHTML = '';
        appendEntries(data.items || []);
      } else {
        var qs = '?limit=50&offset=' + s.offset;
        if (s.category) qs += '&category=' + encodeURIComponent(s.category);
        if (s.work) qs += '&work=1';
        if (s.tag) qs += '&tag=' + encodeURIComponent(s.tag);
        data = await api('/api/entries' + qs);
        s.total = data.total || 0;
        if (reset) $('#entries-list').innerHTML = '';
        appendEntries(data.items || []);
        s.offset += (data.items || []).length;
        var shown = $('#entries-list').children.length;
        if (shown < s.total) moreBtn.classList.remove('hidden');
      }
      if (!$('#entries-list').children.length) {
        $('#entries-list').innerHTML = emptyHtml(
          s.q || s.category || s.work || s.tag ? '没有找到符合条件的记录' : '还没有任何记录',
          s.q || s.category || s.work || s.tag ? '换个关键词试试，或者清除条件' : '回到「今天」，写下第一句吧'
        );
      }
    } catch (e) {
      if (reset) $('#entries-list').innerHTML = errorHtml(e.message);
      else toast(e.message, 'error');
    } finally {
      s.loading = false;
    }
  }

  function appendEntries(items) {
    var html = items.map(function (e) { return entryItemHtml(e, { showDate: true }); }).join('');
    $('#entries-list').insertAdjacentHTML('beforeend', html);
  }

  /* ================= 日历页 ================= */
  var calState = null;

  async function renderCalendar(view) {
    var now = new Date();
    calState = { y: now.getFullYear(), m: now.getMonth() + 1, days: {}, selected: null };
    view.innerHTML =
      '<div class="page page-wide">' +
      '<header class="page-head"><h1>日历</h1><p class="page-sub">每一个有记录的日子，都在发光</p></header>' +
      '<div class="card">' +
      '<div class="cal-head">' +
      '<button class="btn btn-ghost btn-sm" id="cal-prev">‹ 上月</button>' +
      '<h2 id="cal-title"></h2>' +
      '<button class="btn btn-ghost btn-sm" id="cal-next">下月 ›</button>' +
      '</div>' +
      '<div class="cal-grid" id="cal-grid">' + loadingHtml() + '</div>' +
      '</div>' +
      '<h2 class="sec-title" id="cal-day-title"></h2>' +
      '<div id="cal-list"></div>' +
      '</div>';

    $('#cal-prev').onclick = function () { shiftMonth(-1); };
    $('#cal-next').onclick = function () { shiftMonth(1); };
    await loadMonth();
    selectDate(localDateStr());
  }

  function shiftMonth(delta) {
    var d = new Date(calState.y, calState.m - 1 + delta, 1);
    calState.y = d.getFullYear();
    calState.m = d.getMonth() + 1;
    calState.selected = null;
    $('#cal-list').innerHTML = '';
    $('#cal-day-title').textContent = '';
    loadMonth();
  }

  async function loadMonth() {
    var grid = $('#cal-grid');
    grid.innerHTML = loadingHtml();
    try {
      var data = await api('/api/calendar?month=' + calState.y + '-' + pad(calState.m));
      calState.days = (data && data.days) || {};
      renderCalGrid();
    } catch (e) {
      grid.innerHTML = errorHtml(e.message);
    }
  }

  function renderCalGrid() {
    var y = calState.y, m = calState.m;
    $('#cal-title').textContent = y + ' 年 ' + m + ' 月';
    var html = ['一', '二', '三', '四', '五', '六', '日'].map(function (w) {
      return '<div class="cal-wd">' + w + '</div>';
    }).join('');
    var firstOffset = (new Date(y, m - 1, 1).getDay() + 6) % 7; // 周一开头
    var daysInMonth = new Date(y, m, 0).getDate();
    var today = localDateStr();
    for (var i = 0; i < firstOffset; i++) html += '<div></div>';
    for (var d = 1; d <= daysInMonth; d++) {
      var ds = y + '-' + pad(m) + '-' + pad(d);
      var count = calState.days[ds] || 0;
      var cls = 'cal-cell' + (count ? ' has' : '') + (ds === today ? ' today' : '') + (ds === calState.selected ? ' sel' : '');
      html += '<div class="' + cls + '" data-date="' + ds + '">' + d +
        (count ? '<span class="cal-badge">' + count + '</span>' : '') + '</div>';
    }
    var grid = $('#cal-grid');
    grid.innerHTML = html;
    $$('.cal-cell.has', grid).forEach(function (cell) {
      cell.addEventListener('click', function () { selectDate(cell.dataset.date); });
    });
  }

  async function selectDate(ds) {
    calState.selected = ds;
    renderCalGrid();
    var d = parseDate(ds + 'T00:00:00');
    $('#cal-day-title').textContent = (d.getMonth() + 1) + ' 月 ' + d.getDate() + ' 日的记录';
    var box = $('#cal-list');
    box.innerHTML = loadingHtml();
    try {
      var data = await api('/api/entries?date=' + ds + '&limit=50&offset=0');
      box.innerHTML = (data.items && data.items.length)
        ? data.items.map(function (e) { return entryItemHtml(e); }).join('')
        : emptyHtml('这一天还没有记录', '去「今天」补上几句吧');
    } catch (e) {
      box.innerHTML = errorHtml(e.message);
    }
  }

  /* ================= 媒体页 ================= */
  var mediaState = null;
  var MEDIA_KINDS = [ ['', '全部'], ['image', '图片'], ['video', '视频'], ['file', '文件'] ];

  async function renderMedia(view) {
    mediaState = { kind: '', offset: 0, reachedEnd: false };
    view.innerHTML =
      '<div class="page page-wide">' +
      '<header class="page-head"><h1>媒体</h1><p class="page-sub">所有照片、视频与文件，都是成长的证据</p></header>' +
      '<div class="tabs" id="media-tabs">' +
      MEDIA_KINDS.map(function (kv, i) {
        return '<button class="pill' + (i === 0 ? ' active' : '') + '" data-kind="' + kv[0] + '">' + kv[1] + '</button>';
      }).join('') +
      '</div>' +
      '<div class="media-grid" id="media-grid"></div>' +
      '<div id="media-empty"></div>' +
      '<div class="center"><button class="btn btn-ghost hidden" id="media-more">加载更多</button></div>' +
      '</div>';

    $('#media-tabs').addEventListener('click', function (e) {
      var btn = e.target.closest('.pill');
      if (!btn) return;
      $$('#media-tabs .pill').forEach(function (p) { p.classList.remove('active'); });
      btn.classList.add('active');
      mediaState.kind = btn.dataset.kind;
      loadMedia(true);
    });
    $('#media-more').onclick = function () { loadMedia(false); };
    bindAttTiles($('#media-grid'));

    loadMedia(true);
  }

  async function loadMedia(reset) {
    var s = mediaState;
    if (!s) return;
    if (reset) {
      s.offset = 0; s.reachedEnd = false;
      $('#media-grid').innerHTML = '';
      $('#media-empty').innerHTML = loadingHtml();
    }
    $('#media-more').classList.add('hidden');
    try {
      var qs = '?limit=100&offset=' + s.offset;
      if (s.kind) qs += '&kind=' + encodeURIComponent(s.kind);
      var data = await api('/api/assets' + qs);
      var items = data.items || [];
      s.offset += items.length;
      if (items.length < 100) s.reachedEnd = true;
      $('#media-empty').innerHTML = '';
      var grid = $('#media-grid');
      grid.insertAdjacentHTML('beforeend', items.map(function (a) {
        return attTileHtml(a, { entryId: a.entry_id });
      }).join(''));
      if (!grid.children.length) {
        $('#media-empty').innerHTML = emptyHtml('这里还空着', '记录时添加照片、视频或文件，就会出现在这里');
      } else if (!s.reachedEnd) {
        $('#media-more').classList.remove('hidden');
      }
    } catch (e) {
      $('#media-empty').innerHTML = errorHtml(e.message);
    }
  }

  /* ================= 报告页 ================= */
  var REP_TYPES = { daily: '日报', weekly: '周报', monthly: '月报' };

  async function renderReports(view) {
    view.innerHTML =
      '<div class="page">' +
      '<header class="page-head"><h1>报告</h1><p class="page-sub">让 AI 帮你读懂自己的成长</p></header>' +
      '<div id="yr-slot"></div>' +
      '<div class="card">' +
      '<h2 class="card-title">生成新报告</h2>' +
      '<div class="rep-gen">' +
      '<input type="date" class="input" id="rep-date" value="' + localDateStr() + '">' +
      '<div class="rep-btns">' +
      '<button class="btn btn-ghost" data-rep="daily">生成日报</button>' +
      '<button class="btn btn-ghost" data-rep="weekly">生成周报</button>' +
      '<button class="btn btn-ghost" data-rep="monthly">生成月报</button>' +
      '</div>' +
      '</div>' +
      '<p class="hint">以所选日期为基准 · 需要一两分钟 · 同周期会覆盖旧报告（已确认的知识不受影响）</p>' +
      '</div>' +
      '<h2 class="sec-title">历史报告</h2>' +
      '<div id="rep-list">' + loadingHtml() + '</div>' +
      '</div>';

    $$('#view [data-rep]').forEach(function (btn) {
      btn.onclick = function () { generateReport(btn.dataset.rep); };
    });
    probeYearReview();

    try {
      var data = await api('/api/reports?limit=50');
      var box = $('#rep-list');
      if (!data.items || !data.items.length) {
        box.innerHTML = emptyHtml('还没有生成过报告', '选一个日期，让 AI 为你总结吧');
        return;
      }
      box.innerHTML = data.items.map(function (r) {
        return '<a class="rep-item" href="#/reports/' + encodeURIComponent(r.id) + '">' +
          '<span class="badge">' + esc(REP_TYPES[r.type] || r.type) + '</span>' +
          '<div class="rep-item-main">' +
          '<div class="rep-item-period">' + esc(r.period_start) + ' ~ ' + esc(r.period_end) + '</div>' +
          '<div class="rep-item-meta">生成于 ' + esc(absTime(r.created_at)) + ' · 覆盖 ' + esc(r.entry_count) + ' 条记录' +
          (r.model ? ' · ' + esc(r.model) : '') + '</div>' +
          '</div>' +
          '<button class="quiet-del" data-del-rep="' + esc(r.id) + '" title="删除报告"><svg class="ic"><use href="#i-close"/></svg></button>' +
          '</a>';
      }).join('');
      $$('#rep-list [data-del-rep]').forEach(function (btn) {
        btn.onclick = async function (ev) {
          ev.preventDefault();
          ev.stopPropagation();
          var ok = await confirmModal('删除后不可恢复，确定删除这份报告吗？', '删除');
          if (!ok) return;
          btn.disabled = true;
          try {
            await api('/api/reports/' + encodeURIComponent(btn.dataset.delRep), { method: 'DELETE' });
            btn.closest('.rep-item').remove();
            toast('报告已删除', 'success');
            if (!$('#rep-list').children.length) {
              $('#rep-list').innerHTML = emptyHtml('还没有生成过报告', '选一个日期，让 AI 为你总结吧');
            }
          } catch (e) {
            toast(e.message, 'error');
            btn.disabled = false;
          }
        };
      });
    } catch (e) {
      $('#rep-list').innerHTML = errorHtml(e.message);
    }
  }

  /* ---- 年度故事卡：报告页低调入口 → 全屏卡片流 ---- */
  var yrCache = { year: 0, cards: null };
  var yrCards = [], yrIndex = 0, yrOverlay = null, yrKeyHandler = null;

  // 进报告页时安静探一次：有数据才显示入口卡（结果缓存，点开不重复请求）
  function probeYearReview() {
    var year = new Date().getFullYear();
    var slot = $('#yr-slot');
    if (!slot) return;
    if (yrCache.year === year && yrCache.cards && yrCache.cards.length) { renderYrEntry(slot, year); return; }
    api('/api/year-review?year=' + year, { timeout: 180000 }).then(function (d) {
      var cards = (d && d.cards) || [];
      yrCache = { year: year, cards: cards };
      if (cards.length && document.body.contains(slot)) renderYrEntry(slot, year);
    }).catch(function () { /* 没有就安静不出 */ });
  }

  function renderYrEntry(slot, year) {
    slot.innerHTML =
      '<div class="card yr-entry" id="yr-entry" role="button" tabindex="0">' +
      '<span>我的 ' + esc(year) + ' 故事</span>' +
      '<span class="yr-entry-arrow">→</span></div>';
    $('#yr-entry').onclick = openYearReview;
    $('#yr-entry').addEventListener('keydown', function (e) { if (e.key === 'Enter') openYearReview(); });
  }

  function yrCardHtml(c) {
    if (c.kind === 'quote') {
      // 金句卡：引号排版（用户写过的原话）
      return '<div class="yr-card yr-quote-card">' +
        '<div class="yr-quote-mark">“</div>' +
        '<div class="yr-quote-text">' + esc(c.text || '') + '</div>' +
        (c.title ? '<div class="yr-quote-title">—— ' + esc(c.title) + '</div>' : '') +
        '</div>';
    }
    return '<div class="yr-card">' +
      (c.big ? '<div class="yr-big">' + esc(c.big) + '</div>' : '') +
      (c.title ? '<div class="yr-card-title">' + esc(c.title) + '</div>' : '') +
      (c.text ? '<div class="yr-card-text">' + esc(c.text) + '</div>' : '') +
      '</div>';
  }

  function yrShell(inner, idx, total) {
    var dots = '';
    if (total > 1) {
      dots = '<div class="yr-dots">' + yrCards.map(function (_, j) {
        return '<button class="yr-dot' + (j === idx ? ' active' : '') + '" data-yrdot="' + j + '" title="第 ' + (j + 1) + ' 张"></button>';
      }).join('') + '</div>';
    }
    return '<button class="pv-close yr-close" title="关闭">×</button>' +
      '<div class="yr-stage">' + inner + '</div>' + dots;
  }

  function yrShow(i) {
    if (!yrOverlay || !yrCards.length || i < 0 || i >= yrCards.length) return;
    yrIndex = i;
    yrOverlay.querySelector('.yr-stage').innerHTML = yrCardHtml(yrCards[i]);
    $$('.yr-dot', yrOverlay).forEach(function (d, j) { d.classList.toggle('active', j === i); });
  }

  function wireYrShell(ov) {
    ov.querySelector('.yr-close').onclick = function (e) { e.stopPropagation(); closeYearReview(); };
    // 点击：右 3/5 下一张，左 2/5 上一张
    ov.onclick = function (e) {
      if (!yrCards.length) return;
      var x = e.clientX / ov.clientWidth;
      if (x > 0.62) yrShow(yrIndex + 1);
      else if (x < 0.38) yrShow(yrIndex - 1);
    };
    $$('.yr-dot', ov).forEach(function (d, j) {
      d.onclick = function (e) { e.stopPropagation(); yrShow(j); };
    });
    // 触摸滑动切换（移动端优先）
    var tx = null;
    ov.addEventListener('touchstart', function (e) { tx = e.touches[0].clientX; }, { passive: true });
    ov.addEventListener('touchend', function (e) {
      if (tx == null) return;
      var dx = e.changedTouches[0].clientX - tx;
      tx = null;
      if (Math.abs(dx) < 40) return;
      yrShow(yrIndex + (dx < 0 ? 1 : -1));
    }, { passive: true });
  }

  function openYearReview() {
    closeYearReview();
    var year = new Date().getFullYear();
    var ov = document.createElement('div');
    ov.className = 'yr-overlay';
    ov.innerHTML = '<div class="yr-loading"><div class="spinner"></div><p>小满正在整理你的一年…</p></div>';
    document.body.appendChild(ov);
    document.body.classList.add('yr-open');
    yrOverlay = ov;
    yrKeyHandler = function (e) {
      if (!yrOverlay) return;
      if (e.key === 'Escape') closeYearReview();
      else if (e.key === 'ArrowRight') yrShow(yrIndex + 1);
      else if (e.key === 'ArrowLeft') yrShow(yrIndex - 1);
    };
    document.addEventListener('keydown', yrKeyHandler);

    var ready = (yrCache.year === year && yrCache.cards)
      ? Promise.resolve(yrCache.cards)
      : api('/api/year-review?year=' + year, { timeout: 180000 }).then(function (d) {
          var cards = (d && d.cards) || [];
          yrCache = { year: year, cards: cards };
          return cards;
        });
    ready.then(function (cards) {
      if (ov !== yrOverlay) return; // 已经关掉了
      yrCards = cards; yrIndex = 0;
      if (!cards.length) {
        ov.innerHTML = yrShell('<div class="yr-empty">今年还没有攒够故事，再记一阵子吧</div>', 0, 0);
      } else {
        ov.innerHTML = yrShell(yrCardHtml(cards[0]), 0, cards.length);
      }
      wireYrShell(ov);
    }).catch(function (e) {
      if (ov !== yrOverlay) return;
      yrCards = [];
      ov.innerHTML = yrShell('<div class="yr-empty">' + esc(e.message || '读取失败，稍后再试') + '</div>', 0, 0);
      wireYrShell(ov);
    });
  }

  function closeYearReview() {
    if (yrOverlay) { yrOverlay.remove(); yrOverlay = null; }
    document.body.classList.remove('yr-open');
    if (yrKeyHandler) { document.removeEventListener('keydown', yrKeyHandler); yrKeyHandler = null; }
    yrCards = []; yrIndex = 0;
  }

  async function generateReport(type) {
    var dateStr = $('#rep-date').value || localDateStr();
    var chosen = new Date(dateStr + 'T12:00:00');
    if (isNaN(chosen.getTime())) { chosen = new Date(); dateStr = localDateStr(); }
    var today = new Date();
    var bodyDate = dateStr;

    // 周期边界处理：周报/月报按自然周/自然月统计，当前周期未结束时先问清楚
    if (type === 'weekly') {
      var weekStart = mondayOf(chosen);
      var thisWeekStart = mondayOf(today);
      if (weekStart.getTime() === thisWeekStart.getTime() && today < addDays(weekStart, 6)) {
        var lastStart = addDays(weekStart, -7), lastEnd = addDays(weekStart, -1);
        var v = await choiceModal({
          title: '本周还没结束',
          body: '<p class="modal-text">周报按自然周（周一至周日）统计，这一周想总结哪一段？</p>',
          choices: [
            { label: '上周完整周报', sub: fmtMd(lastStart) + ' – ' + fmtMd(lastEnd), value: localDateStr(lastStart), primary: true },
            { label: '本周至今', sub: fmtMd(weekStart) + ' – 今天', value: dateStr }
          ]
        });
        if (v == null) return;
        bodyDate = v;
      }
    } else if (type === 'monthly') {
      if (chosen.getFullYear() === today.getFullYear() && chosen.getMonth() === today.getMonth()) {
        var mEnd = new Date(chosen.getFullYear(), chosen.getMonth(), 0); // 上月最后一天
        var mStart = new Date(mEnd.getFullYear(), mEnd.getMonth(), 1);
        var v2 = await choiceModal({
          title: '本月还没结束',
          body: '<p class="modal-text">月报按自然月统计，这一月想总结哪一段？</p>',
          choices: [
            { label: '上月完整月报', sub: fmtMd(mStart) + ' – ' + fmtMd(mEnd), value: localDateStr(mStart), primary: true },
            { label: '本月至今', sub: (chosen.getMonth() + 1) + '月1日 – 今天', value: dateStr }
          ]
        });
        if (v2 == null) return;
        bodyDate = v2;
      }
    }

    showOverlay('AI 正在认真阅读你的记录，可能需要一两分钟…');
    try {
      var rep = await api('/api/reports/generate', {
        method: 'POST',
        body: { type: type, date: bodyDate },
        timeout: 600000
      });
      hideOverlay();
      toast('已生成 ' + rep.period_start + ' ~ ' + rep.period_end + ' 的报告（同周期的旧报告已被覆盖）', 'success', 5000);
      location.hash = '#/reports/' + encodeURIComponent(rep.id);
    } catch (e) {
      hideOverlay();
      toast(e.message, 'error', 6000);
    }
  }

  /* ---------------- 报告详情 ---------------- */
  async function renderReportDetail(view, id) {
    view.innerHTML = '<div class="page">' + loadingHtml('正在打开报告…') + '</div>';
    var rep;
    try {
      rep = await api('/api/reports/' + encodeURIComponent(id));
    } catch (e) {
      view.innerHTML = '<div class="page"><a class="back-link" href="#/reports">‹ 返回报告列表</a>' + errorHtml(e.message) + '</div>';
      return;
    }
    var a = rep.analysis || {};
    var evidences = Array.isArray(a.evidence) ? a.evidence : [];
    var usedEv = {};

    function toText(x) {
      if (typeof x === 'string') return x;
      if (x && typeof x === 'object') return x.text || x.title || JSON.stringify(x);
      return String(x == null ? '' : x);
    }
    // 将 evidence 匹配到条目文本，生成可点击的小链条徽章
    function evBadges(text) {
      var out = '';
      evidences.forEach(function (ev, i) {
        if (usedEv[i] || !ev || ev.entry_id == null) return;
        var claim = toText(ev.claim);
        if (claim && text && (text.indexOf(claim) !== -1 || claim.indexOf(text.slice(0, 20)) !== -1)) {
          usedEv[i] = true;
          out += '<a class="ev-badge" href="#/entry/' + encodeURIComponent(ev.entry_id) + '" title="' + esc(claim) + '">' +
            '<svg class="ic"><use href="#i-link"/></svg>证据</a>';
        }
      });
      return out;
    }
    function secList(title, items) {
      if (!Array.isArray(items) || !items.length) return '';
      return '<section class="card rep-sec"><h2 class="card-title">' + esc(title) + '</h2><ul class="rep-list">' +
        items.map(function (t) {
          var txt = toText(t);
          return '<li>' + esc(txt) + ' ' + evBadges(txt) + '</li>';
        }).join('') + '</ul></section>';
    }

    var restEv = evidences.filter(function (ev, i) { return !usedEv[i] && ev && ev.entry_id != null; });
    var html =
      '<div class="page">' +
      '<div class="rep-detail-bar">' +
      '<a class="back-link" href="#/reports">‹ 返回报告列表</a>' +
      '<button class="quiet-del" id="rep-del" title="删除报告"><svg class="ic"><use href="#i-close"/></svg></button>' +
      '</div>' +
      '<header class="page-head"><h1>' + esc(REP_TYPES[rep.type] || rep.type || '报告') + ' · ' + esc(rep.period_start) + ' ~ ' + esc(rep.period_end) + '</h1>' +
      '<p class="page-sub">生成于 ' + esc(absTime(rep.created_at)) + ' · 覆盖 ' + esc(rep.entry_count) + ' 条记录' +
      (rep.model ? ' · ' + esc(rep.model) : '') + '</p></header>' +
      (a.context_notes
        ? '<section class="card ctx-card"><h2 class="card-title">这段时间的变化</h2><div class="md">' + mdToHtml(a.context_notes) + '</div></section>'
        : '') +
      (a.executive_summary
        ? '<section class="card"><h2 class="card-title">执行摘要</h2><div class="md">' + mdToHtml(a.executive_summary) + '</div></section>'
        : '') +
      (Array.isArray(a.timeline_facts) && a.timeline_facts.length
        ? '<details class="card facts-card"><summary>事实时间线（AI 写报告前先核对的 ' + a.timeline_facts.length + ' 条事实）</summary><ul class="facts-list">' +
          a.timeline_facts.map(function (f) {
            return '<li><span class="fact-date">' + esc(f.date) + '</span><span class="fact-text">' + esc(f.fact) + '</span>' +
              (f.quote ? '<span class="fact-quote">“' + esc(f.quote) + '”</span>' : '') +
              '<a class="ev-badge" href="#/entry/' + encodeURIComponent(f.entry_id) + '"><svg class="ic"><use href="#i-link"/></svg>来源</a></li>';
          }).join('') + '</ul></details>'
        : '') +
      secList('成就', a.accomplishments) +
      secList('挑战', a.challenges) +
      (Array.isArray(a.recurring) && a.recurring.length
        ? '<section class="card rep-sec"><h2 class="card-title">反复出现的挑战</h2><div class="recur-tags">' +
          a.recurring.map(function (t) {
            return '<span class="badge">' + esc(toText(t)) + '</span>';
          }).join('') + '</div></section>'
        : '') +
      secList('经验', a.learnings) +
      secList('建议', a.suggestions);

    if (restEv.length) {
      html += '<section class="card rep-sec"><h2 class="card-title">相关证据</h2><ul class="rep-list">' +
        restEv.map(function (ev) {
          return '<li>' + esc(toText(ev.claim) || '相关记录') + ' ' +
            (ev.quote ? '<span class="fact-quote">“' + esc(ev.quote) + '”</span> ' : '') +
            '<a class="ev-badge" href="#/entry/' + encodeURIComponent(ev.entry_id) + '"><svg class="ic"><use href="#i-link"/></svg>证据</a></li>';
        }).join('') + '</ul></section>';
    }
    if (a.mindmap_mermaid) {
      html += '<section class="card"><h2 class="card-title">思维导图</h2><div id="mindmap-box">' +
        (LIB.mermaid ? loadingHtml('正在绘制思维导图…') : '') + '</div></section>';
    }
    if (rep.knowledge_extracted > 0) {
      html += '<div class="know-link-wrap"><a class="know-link" href="#/knowledge">本次提取 ' + esc(rep.knowledge_extracted) + ' 条知识，去知识库确认 →</a></div>';
    }
    html += '</div>';
    view.innerHTML = html;

    // 删除报告：成功返回列表
    $('#rep-del').onclick = async function () {
      var ok = await confirmModal('删除后不可恢复，确定删除这份报告吗？', '删除');
      if (!ok) return;
      var btn = this;
      btn.disabled = true;
      try {
        await api('/api/reports/' + encodeURIComponent(id), { method: 'DELETE' });
        toast('报告已删除', 'success');
        location.hash = '#/reports';
      } catch (e) {
        toast(e.message, 'error');
        btn.disabled = false;
      }
    };

    if (a.mindmap_mermaid) {
      var box = $('#mindmap-box');
      if (box) await renderMindmap(box, a.mindmap_mermaid);
    }
  }

  /* ================= 长河页 ================= */
  var riverState = null;

  async function renderRiver(view) {
    riverState = { limit: 240 };
    view.innerHTML =
      '<div class="page page-wide">' +
      '<header class="page-head"><h1>长河</h1><p class="page-sub">所有记录，按月缓缓流淌</p></header>' +
      '<div id="river-box">' + loadingHtml() + '</div>' +
      '<div class="center"><button class="btn btn-ghost hidden" id="river-more">加载更多</button></div>' +
      '</div>';
    $('#river-more').onclick = function () {
      riverState.limit += 240;
      loadRiver();
    };
    loadRiver();
  }

  async function loadRiver() {
    var box = $('#river-box');
    if (!box) return;
    try {
      var data = await api('/api/river?limit=' + riverState.limit);
      var items = (data && data.items) || [];
      if (!items.length) {
        box.innerHTML = emptyHtml('长河还空着', '写下第一条记录，河流就开始了');
        return;
      }
      // 按月分组（正序，月份大标题 + 条目小卡）
      var groups = [];
      var cur = null;
      items.forEach(function (e) {
        var d = parseDate(e.occurred_at);
        var label = d ? d.getFullYear() + ' 年 ' + (d.getMonth() + 1) + ' 月' : '未知月份';
        if (!cur || cur.label !== label) { cur = { label: label, items: [] }; groups.push(cur); }
        cur.items.push(e);
      });
      box.innerHTML = groups.map(function (g, gi) {
        return '<details class="river-month-group"' + (gi < 2 ? ' open' : '') + '>' +
          '<summary class="river-month">' + esc(g.label) + '<span class="badge">' + esc(g.items.length) + ' 条</span></summary>' +
          '<div class="river-group">' + g.items.map(riverItemHtml).join('') + '</div></details>';
      }).join('');
      var more = $('#river-more');
      if (more) more.classList.toggle('hidden', items.length < riverState.limit);
    } catch (e) {
      box.innerHTML = errorHtml(e.message);
    }
  }

  function riverItemHtml(e) {
    var d = parseDate(e.occurred_at);
    var day = d ? (d.getMonth() + 1) + '.' + d.getDate() : '';
    var title = e.title || (e.content ? String(e.content).split('\n')[0].slice(0, 50) : '') || '（无标题）';
    var sub = e.summary || (e.content ? String(e.content).replace(/\s+/g, ' ').slice(0, 60) : '');
    // 长河接口只给顶层 thumb_url（首图缩略图），不展开 attachments
    var thumb = e.thumb_url || '';
    return '<a class="river-item" href="#/entry/' + encodeURIComponent(e.id) + '">' +
      '<span class="rv-dot"></span>' +
      '<div class="rv-date">' + esc(day) + '</div>' +
      '<div class="rv-main"><div class="rv-title">' + esc(title) + '</div>' +
      (sub && sub !== title ? '<div class="rv-sub">' + esc(sub) + '</div>' : '') +
      '</div>' +
      (thumb ? '<img class="rv-thumb" loading="lazy" src="' + esc(thumb) + '" alt="">' : '') +
      '</a>';
  }

  /* ================= 数据页 ================= */
  async function renderInsights(view) {
    view.innerHTML = '<div class="page page-wide"><header class="page-head"><h1>数据</h1><p class="page-sub">时间会证明，坚持有迹可循</p></header><div id="ins-body">' + loadingHtml('正在统计数据…') + '</div></div>';
    var s;
    try {
      s = await api('/api/stats/overview');
    } catch (e) {
      $('#ins-body').innerHTML = errorHtml(e.message);
      return;
    }
    var html =
      '<div id="insights-slot"></div>' +
      '<div class="stat-grid">' +
      '<div class="stat-card"><div class="stat-ic"><svg class="ic"><use href="#i-flame"/></svg></div><div><div class="stat-num">' + esc(s.streak_days) + '</div><div class="stat-label">连续记录天数</div></div></div>' +
      '<div class="stat-card"><div class="stat-ic green"><svg class="ic"><use href="#i-list"/></svg></div><div><div class="stat-num">' + esc(s.total_entries) + '</div><div class="stat-label">总记录</div></div></div>' +
      '<div class="stat-card"><div class="stat-ic"><svg class="ic"><use href="#i-doc"/></svg></div><div><div class="stat-num">' + esc(fmtWords(s.total_words)) + '</div><div class="stat-label">总字数</div>' +
      (s.fun_facts && s.fun_facts.novels_eq ? '<div class="stat-sub">' + esc(s.fun_facts.novels_eq) + '</div>' : '') +
      '</div></div>' +
      '<div class="stat-card"><div class="stat-ic green"><svg class="ic"><use href="#i-img"/></svg></div><div><div class="stat-num">' + esc(s.media_count) + '</div><div class="stat-label">媒体数</div></div></div>' +
      '<div class="stat-card"><div class="stat-ic"><svg class="ic"><use href="#i-cal"/></svg></div><div><div class="stat-num">' + esc(s.days_active) + '</div><div class="stat-label">活跃天数</div></div></div>' +
      '</div>';

    // 近 30 天 vs 之前 30 天（滚动窗口对比，安静对比卡，数字加粗暖橙）
    var pc = s.period_compare;
    if (pc && pc.this) {
      var pt = pc.this, pl = pc.last || {};
      var labelT = pt.label || '近 30 天', labelL = pl.label || '之前 30 天';
      var cmpRow = function (label, tv, lv) {
        return '<div class="cmp-row"><span class="cmp-label">' + esc(label) + '</span>' +
          '<span class="cmp-num">' + esc(tv) + '</span>' +
          '<span class="cmp-vs">' + esc(labelL) + ' ' + esc(lv) + '</span></div>';
      };
      var moodT = pt.mood_avg != null ? Math.round(pt.mood_avg * 10) / 10 : '—';
      var moodL = pl.mood_avg != null ? Math.round(pl.mood_avg * 10) / 10 : '—';
      html += '<div class="card mb-20"><h2 class="card-title">' + esc(labelT) + ' vs ' + esc(labelL) + '</h2><div class="cmp-grid">' +
        cmpRow('记录数', pt.entries || 0, (pl.entries != null ? pl.entries : '—') + ' 条') +
        cmpRow('字数', fmtWords(pt.words || 0), fmtWords(pl.words || 0)) +
        cmpRow('心情均分', moodT, moodL) +
        cmpRow('最常标签', pt.top_tag ? '#' + pt.top_tag : '—', pl.top_tag ? '#' + pl.top_tag : '—') +
        '</div></div>';
    }

    // 关键词与人物/事件趋势：用 SQL 统计结果做轻量条带，避免只看一张静态词云。
    if ((s.keyword_trends && s.keyword_trends.length) || (s.entity_trends && s.entity_trends.length)) {
      html += '<div class="trend-grid">';
      if (s.keyword_trends && s.keyword_trends.length) {
        html += '<div class="card trend-card"><h2 class="card-title">关键词变化</h2><p class="hint mt-0">近12周 · 新出现 / 持续 / 上升 / 回落</p>' +
          s.keyword_trends.map(function (it) {
            var max = Math.max.apply(null, it.weekly || [1]) || 1;
            return '<div class="trend-row"><div class="trend-head"><b>#' + esc(it.tag) + '</b><span class="badge">' + esc(it.state) + '</span><span class="muted">' + esc(it.total) + ' 次</span></div>' +
              '<div class="trend-bars">' + (it.weekly || []).map(function (v) {
                return '<i style="height:' + Math.max(3, Math.round(v / max * 24)) + 'px" title="' + esc(v) + ' 次"></i>';
              }).join('') + '</div></div>';
          }).join('') + '</div>';
      }
      if (s.entity_trends && s.entity_trends.length) {
        html += '<div class="card trend-card"><h2 class="card-title">人物 / 事件变化</h2><p class="hint mt-0">按最近提及判断状态，点击可去圈子核对</p>' +
          s.entity_trends.map(function (it) {
            return '<button type="button" class="trend-row trend-entity-row" data-trend-entity="' + esc(it.id) + '"><div class="trend-head"><b>' + esc(it.name) + '</b><span class="badge">' + esc(it.state) + '</span><span class="muted">' + esc(it.total) + ' 次</span></div>' +
              '<div class="trend-entity-bar"><span style="width:' + Math.min(100, Math.max(8, (it.total || 0) * 7)) + '%"></span></div>' +
              '<div class="trend-foot">最近 ' + esc(it.last_seen || '未知') + '</div></button>';
          }).join('') + '</div>';
      }
      html += '</div>';
    }

    // 心情曲线（近 30 天）：数据不足 3 天显示安静空态
    var moods = Array.isArray(s.mood_30) ? s.mood_30 : [];
    if (moods.length >= 3) {
      if (LIB.echarts) {
        html += '<div class="card mb-16"><h2 class="card-title">心情曲线（近 30 天）</h2><div class="chart chart-mood" id="ch-mood"></div></div>';
      } else {
        html += '<div class="card mb-16"><h2 class="card-title">心情曲线（近 30 天）</h2><div class="recur-tags">' +
          moods.slice(-7).map(function (m) {
            return '<span class="tag">' + esc(m.date.slice(5)) + ' · ' + esc(m.label || m.score + ' 分') + '</span>';
          }).join('') + '</div></div>';
      }
    } else {
      html += '<div class="card mb-16"><h2 class="card-title">心情曲线（近 30 天）</h2>' +
        emptyHtml('记录几天后，这里会长出心情曲线') + '</div>';
    }

    if (LIB.echarts) {
      html +=
        '<div class="chart-grid">' +
        '<div class="card chart-card chart-wide"><h2 class="card-title">近 180 天热力图</h2><div class="chart chart-cal" id="ch-heat"></div></div>' +
        '<div class="card chart-card"><h2 class="card-title">近 90 天记录趋势</h2><div class="chart" id="ch-trend"></div></div>' +
        '<div class="card chart-card"><h2 class="card-title">分类分布</h2><div class="chart" id="ch-cat"></div></div>' +
        '<div class="card chart-card"><h2 class="card-title">常用标签 Top 10</h2><div class="chart" id="ch-tags"></div></div>' +
        '<div class="card chart-card"><h2 class="card-title">24 小时记录分布</h2><div class="chart" id="ch-hour"></div></div>' +
        '</div>';
    } else {
      html += '<p class="chart-fallback-note">图表库未能加载，以表格形式展示统计。</p>' +
        '<div class="card"><h2 class="card-title">分类统计</h2><div id="fb-cat"></div></div>' +
        '<div class="card"><h2 class="card-title">常用标签</h2><div id="fb-tags"></div></div>' +
        '<div class="card"><h2 class="card-title">24 小时记录分布</h2><div id="fb-hour"></div></div>';
    }
    $('#ins-body').innerHTML = html;

    $$('[data-trend-entity]', $('#ins-body')).forEach(function (row) {
      row.onclick = function () { openCircleDetail(row.dataset.trendEntity); };
    });
    if (LIB.echarts) initCharts(s);
    else initStatTables(s);
    loadInsights();
  }

  /* ---- 小满发现的规律（数据页顶部；空则整区不渲染） ---- */
  async function loadInsights() {
    var slot = $('#insights-slot');
    if (!slot) return;
    try {
      var data = await api('/api/insights');
      var items = Array.isArray(data) ? data : (data && data.items) || [];
      if (!items.length || !document.body.contains(slot)) return;
      slot.innerHTML = '<div class="circle-sec-title">小满发现的规律</div>' + items.map(function (it) {
        var ev = it.evidence || {};
        if (!it.evidence && it.evidence_json) { // 兼容 evidence_json 字符串/对象两形
          try { ev = typeof it.evidence_json === 'string' ? JSON.parse(it.evidence_json) : it.evidence_json; } catch (e) { ev = {}; }
        }
        var evText = '';
        if (ev.days_with != null && ev.days_without != null) {
          evText = '基于 ' + (ev.days_with + ev.days_without) + ' 天对比';
          if (ev.lag === 1) evText += '（看的是第二天的心情）';
        }
        return '<div class="card ins-card" data-iid="' + esc(it.id) + '">' +
          '<div class="ins-text">' + esc(it.text || '') + '</div>' +
          '<div class="ins-foot">' +
          (evText ? '<span class="ins-ev">' + esc(evText) + '</span>' : '<span></span>') +
          '<button class="link-btn" data-idismiss="' + esc(it.id) + '">不感兴趣</button>' +
          '</div></div>';
      }).join('');
      $$('#insights-slot [data-idismiss]').forEach(function (btn) {
        btn.onclick = async function () {
          btn.disabled = true;
          try {
            await api('/api/insights/' + encodeURIComponent(btn.dataset.idismiss) + '/dismiss', { method: 'POST' });
            var card = btn.closest('.ins-card');
            card.style.opacity = '0';
            setTimeout(function () {
              card.remove();
              if (!slot.querySelector('.ins-card')) slot.innerHTML = '';
            }, 250);
          } catch (e) {
            toast(e.message, 'error');
            btn.disabled = false;
          }
        };
      });
    } catch (e) { /* 静默：端点不在或样本不足都不出 */ }
  }

  function initCharts(s) {
    var axisStyle = {
      axisLine: { lineStyle: { color: CHART_COLORS.axis } },
      axisLabel: { color: CHART_COLORS.axisText },
      splitLine: { lineStyle: { color: CHART_COLORS.splitLine } }
    };
    // 心情曲线（近 30 天）
    if (document.getElementById('ch-mood')) {
      var moodData = Array.isArray(s.mood_30) ? s.mood_30 : [];
      makeChart('ch-mood', {
        grid: { left: 34, right: 16, top: 16, bottom: 26 },
        tooltip: {
          trigger: 'axis',
          formatter: function (ps) {
            var d = moodData[ps[0].dataIndex];
            return d.date + ' · ' + (d.label || '') + ' · ' + d.score + ' 分';
          }
        },
        xAxis: Object.assign({ type: 'category', data: moodData.map(function (m) { return m.date.slice(5); }) }, axisStyle),
        yAxis: Object.assign({ type: 'value', min: 1, max: 5, minInterval: 1 }, axisStyle),
        series: [{
          type: 'line', smooth: true, symbol: 'circle', symbolSize: 6,
          lineStyle: { color: CHART_COLORS.accent, width: 2.5 },
          itemStyle: { color: CHART_COLORS.accent },
          areaStyle: {
            color: new window.echarts.graphic.LinearGradient(0, 0, 0, 1, [
              { offset: 0, color: 'rgba(224,142,79,0.32)' },
              { offset: 1, color: 'rgba(224,142,79,0.03)' }
            ])
          },
          data: moodData.map(function (m) { return m.score; })
        }]
      });
    }
    // 日历热力图（近 180 天）
    var heat = (s.heatmap || []).map(function (d) { return [d.date, d.count]; });
    var maxCount = heat.reduce(function (m, d) { return Math.max(m, d[1]); }, 1);
    var end = new Date();
    var start = new Date();
    start.setDate(start.getDate() - 179);
    makeChart('ch-heat', {
      tooltip: { formatter: function (p) { return p.data[0] + '：' + p.data[1] + ' 条'; } },
      visualMap: {
        min: 0, max: maxCount, type: 'piecewise', orient: 'horizontal', left: 'center', top: 0,
        inRange: { color: CHART_COLORS.heatmap },
        textStyle: { color: CHART_COLORS.axisText }
      },
      calendar: {
        top: 56, left: 40, right: 20, bottom: 10,
        cellSize: ['auto', 14],
        range: [localDateStr(start), localDateStr(end)],
        itemStyle: { borderColor: CHART_COLORS.card, borderWidth: 2 },
        splitLine: { lineStyle: { color: CHART_COLORS.splitLine } },
        dayLabel: { firstDay: 1, nameMap: 'zh', color: CHART_COLORS.axisText },
        monthLabel: { nameMap: 'zh', color: CHART_COLORS.axisText },
        yearLabel: { show: false }
      },
      series: [{ type: 'heatmap', coordinateSystem: 'calendar', data: heat }]
    });
    // 近 90 天趋势
    var trend = s.daily_trend || [];
    makeChart('ch-trend', {
      color: CHART_COLORS.palette,
      tooltip: { trigger: 'axis' },
      legend: { data: ['记录数', '字数'], textStyle: { color: CHART_COLORS.axisText }, top: 0 },
      grid: { left: 44, right: 44, top: 34, bottom: 28 },
      xAxis: Object.assign({ type: 'category', data: trend.map(function (d) { return d.date.slice(5); }) }, axisStyle),
      yAxis: [
        Object.assign({ type: 'value', name: '记录数', minInterval: 1 }, axisStyle),
        Object.assign({ type: 'value', name: '字数' }, axisStyle, { splitLine: { show: false } })
      ],
      series: [
        { name: '记录数', type: 'line', smooth: true, symbol: 'none', areaStyle: { opacity: 0.12 }, data: trend.map(function (d) { return d.count; }) },
        { name: '字数', type: 'line', smooth: true, symbol: 'none', yAxisIndex: 1, data: trend.map(function (d) { return d.words; }) }
      ]
    });
    // 分类饼图（category_counts 为 {名字: 条数} 字典；兼容旧 work/life/mixed 键）
    var cc = s.category_counts || {};
    var ccData = Object.keys(cc).map(function (k) {
      return { name: CATS[k] || k, value: cc[k] };
    }).filter(function (d) { return d.value > 0; })
      .sort(function (a, b) { return b.value - a.value; });
    makeChart('ch-cat', {
      color: CHART_COLORS.palette,
      tooltip: { trigger: 'item', formatter: '{b}：{c} 条（{d}%）' },
      legend: { bottom: 0, textStyle: { color: CHART_COLORS.axisText } },
      series: [{
        type: 'pie', radius: ['38%', '64%'], center: ['50%', '44%'],
        itemStyle: { borderColor: CHART_COLORS.card, borderWidth: 2 },
        label: { color: '#463f35' },
        data: ccData
      }]
    });
    // Top 标签
    var tags = (s.tag_counts || []).slice(0, 10);
    makeChart('ch-tags', {
      tooltip: {},
      grid: { left: 10, right: 30, top: 10, bottom: 10, containLabel: true },
      xAxis: Object.assign({ type: 'value', minInterval: 1 }, axisStyle),
      yAxis: Object.assign({ type: 'category', inverse: true, data: tags.map(function (t) { return t.tag; }) }, axisStyle),
      series: [{
        type: 'bar', barMaxWidth: 18,
        itemStyle: { color: CHART_COLORS.green, borderRadius: [0, 6, 6, 0] },
        data: tags.map(function (t) { return t.count; })
      }]
    });
    // 24 小时分布
    var hourMap = {};
    (s.hourly || []).forEach(function (h) { hourMap[h.hour] = h.count; });
    var hours = [];
    for (var i = 0; i < 24; i++) hours.push(i);
    makeChart('ch-hour', {
      tooltip: {},
      grid: { left: 36, right: 10, top: 16, bottom: 26 },
      xAxis: Object.assign({ type: 'category', data: hours.map(function (h) { return h + '时'; }) }, axisStyle),
      yAxis: Object.assign({ type: 'value', minInterval: 1 }, axisStyle),
      series: [{
        type: 'bar', barMaxWidth: 16,
        itemStyle: { color: CHART_COLORS.accent, borderRadius: [5, 5, 0, 0] },
        data: hours.map(function (h) { return hourMap[h] || 0; })
      }]
    });
  }

  function initStatTables(s) {
    function table(headers, rows) {
      return '<table class="simple-table"><thead><tr>' +
        headers.map(function (h) { return '<th>' + esc(h) + '</th>'; }).join('') +
        '</tr></thead><tbody>' +
        rows.map(function (r) { return '<tr>' + r.map(function (c) { return '<td>' + esc(c) + '</td>'; }).join('') + '</tr>'; }).join('') +
        '</tbody></table>';
    }
    var cc = s.category_counts || {};
    var ccRows = Object.keys(cc).map(function (k) { return [CATS[k] || k, cc[k]]; })
      .sort(function (a, b) { return b[1] - a[1]; });
    $('#fb-cat').innerHTML = ccRows.length ? table(['分类', '条数'], ccRows) : emptyHtml('还没有分类数据');
    var tags = (s.tag_counts || []).slice(0, 10);
    $('#fb-tags').innerHTML = tags.length
      ? table(['标签', '次数'], tags.map(function (t) { return ['#' + t.tag, t.count]; }))
      : emptyHtml('还没有标签数据');
    var hourMap = {};
    (s.hourly || []).forEach(function (h) { hourMap[h.hour] = h.count; });
    var rows = [];
    for (var i = 0; i < 24; i++) { if (hourMap[i]) rows.push([i + ' 时', hourMap[i]]); }
    $('#fb-hour').innerHTML = rows.length ? table(['时段', '条数'], rows) : emptyHtml('还没有时段数据');
  }

  /* ================= 成长页 ================= */
  async function renderGrowth(view) {
    view.innerHTML =
      '<div class="page">' +
      '<header class="page-head"><h1>成长</h1><p class="page-sub">回头看看走了多远，再想想想去哪里</p></header>' +

      '<div class="section-head"><h2>我的成长轨迹</h2>' +
      '<button class="btn btn-ghost btn-sm hidden" id="summary-regen">重新总结</button></div>' +
      '<p class="sec-sub">我已经走过的</p>' +
      '<div id="summary-box">' + loadingHtml() + '</div>' +
      '<div id="starred-wrap"></div>' +

      '<div class="sec-divider"></div>' +
      '<div class="section-head"><h2>我的假设 / 自我实验</h2>' +
      '<button class="btn btn-ghost btn-sm" id="experiment-add"><svg class="ic"><use href="#i-plus"/></svg>添加假设</button></div>' +
      '<p class="sec-sub">把一个想验证的观察写下来，窗口结束后只看记录，不替你宣布因果。</p>' +
      '<div id="experiments-box">' + loadingHtml() + '</div>' +
      '<div class="sec-divider"></div>' +
      '<details class="growth-explore"><summary><span>未来方向、路线与情景推演</span><span class="muted">需要时再展开</span></summary>' +
      '<div class="growth-explore-body">' +
      '<div class="section-head"><h2>未来方向</h2></div>' +
      '<p class="sec-sub">先把方向当成待验证的假设，不当成对你的定论</p>' +
      '<div class="card">' +
      '<div id="dir-box"></div>' +
      '<div class="dir-actions">' +
      '<button class="btn" id="dir-gen">发现我的方向</button>' +
      '<span class="hint" id="dir-current"></span>' +
      '</div>' +
      '</div>' +
      '<div class="section-head"><h3>未来路线</h3></div>' +
      '<div id="roadmap-box">' + loadingHtml() + '</div>' +
      '<div class="section-head"><h3>情景推演</h3><button class="btn btn-ghost btn-sm" id="forecast-gen">生成情景推演</button></div>' +
      '<p class="sec-sub">只推演记录投入的变化，不预测升职、离职或现实结果</p>' +
      '<div id="forecast-box">' + loadingHtml() + '</div>' +
      '</div></details>' +
      '</div>';

    $('#dir-gen').onclick = onDirectionButton;
    $('#forecast-gen').onclick = generateForecast;
    $('#experiment-add').onclick = openExperimentModal;
    $('#summary-regen').onclick = function () { generateSummary(); };

    loadSummary();
    loadStarred();
    loadRoadmap();
    loadForecast();
    loadExperiments();
  }

  /* ---- 高光时刻（星标记录，空则整节不显示） ---- */
  async function loadStarred() {
    var wrap = $('#starred-wrap');
    if (!wrap) return;
    try {
      var data = await api('/api/entries?starred=1&limit=50');
      var items = (data && data.items) || [];
      if (!items.length || !document.body.contains(wrap)) return;
      wrap.innerHTML =
        '<div class="section-head"><h2>高光时刻</h2></div>' +
        items.map(function (e) {
          var title = e.title || (e.content ? String(e.content).split('\n')[0].slice(0, 40) : '') || '（无标题）';
          return '<a class="star-card" href="#/entry/' + encodeURIComponent(e.id) + '">' +
            '<svg class="ic star-ic"><use href="#i-star"/></svg>' +
            '<span class="star-date">' + esc(absDate(e.occurred_at)) + '</span>' +
            '<span class="star-main"><span class="star-title">' + esc(title) + '</span>' +
            (e.summary ? '<span class="star-sub">' + esc(e.summary) + '</span>' : '') +
            '</span></a>';
        }).join('');
    } catch (e) { /* 静默 */ }
  }

  /* ---- 未来方向（AI 从记录里发现方向） ---- */
  var selectedDirection = '';

  async function onDirectionButton() {
    if (selectedDirection) {
      generateRoadmap(selectedDirection);
    } else {
      discoverDirections();
    }
  }

  async function discoverDirections() {
    showOverlay('AI 正在你的记录里寻找方向，可能需要一两分钟…');
    try {
      var data = await api('/api/growth/directions', { method: 'POST', timeout: 180000 });
      hideOverlay();
      renderDirections(data && data.directions ? data.directions : []);
    } catch (e) {
      hideOverlay();
      toast(e.message, 'error', 6000);
    }
  }

  function renderDirections(dirs) {
    var box = $('#dir-box');
    if (!box) return;
    if (!dirs.length) {
      box.innerHTML = '<p class="hint mt-0 mb-12">这次没找到明确的方向，再多记一些吧</p>';
      return;
    }
    box.innerHTML = '<div class="dir-grid">' + dirs.map(function (d) {
      return '<div class="dir-card" data-dir="' + esc(d.title) + '">' +
        '<div class="dir-title">' + esc(d.title) + '</div>' +
        (d.rationale ? '<div class="dir-rationale">' + esc(d.rationale) + '</div>' : '') +
        (d.experiment ? '<div class="dir-experiment"><b>先试 14 天：</b>' + esc(d.experiment) + '</div>' : '') +
        (d.success_signal ? '<div class="dir-signal"><b>有效信号：</b>' + esc(d.success_signal) + '</div>' : '') +
        (d.evidence_quote ? '<div class="dir-evidence">“' + esc(d.evidence_quote) + '”</div>' : '') +
        (Array.isArray(d.evidence_entry_ids) && d.evidence_entry_ids.length
          ? '<div class="dir-source">证据：' + d.evidence_entry_ids.map(function (id) {
              return '<a href="#/entry/' + encodeURIComponent(id) + '">打开记录</a>';
            }).join(' · ') + '</div>' : '') +
        '</div>';
    }).join('') + '</div>';
    $$('#dir-box .dir-card').forEach(function (card) {
      card.onclick = function (e) {
        if (e && e.target && e.target.closest && e.target.closest('a')) return;
        var was = card.classList.contains('sel');
        $$('#dir-box .dir-card').forEach(function (c) { c.classList.remove('sel'); });
        if (was) {
          selectedDirection = '';
          $('#dir-gen').textContent = '发现我的方向';
        } else {
          card.classList.add('sel');
          selectedDirection = card.dataset.dir;
          $('#dir-gen').textContent = '以「' + selectedDirection + '」生成路线';
        }
      };
    });
  }

  function showCurrentDirection(direction) {
    var el = $('#dir-current');
    if (el) el.textContent = direction ? '当前方向：' + direction : '';
  }

  /* ---- 我的成长轨迹（总结现在） ---- */
  async function loadSummary() {
    var box = $('#summary-box');
    if (!box) return;
    box.innerHTML = loadingHtml();
    var summary = null;
    try {
      var data = await api('/api/growth/summary');
      summary = data && data.summary;
    } catch (e) {
      box.innerHTML = errorHtml(e.message);
      return;
    }
    if (summary) { renderSummary(summary); return; }
    // 没有缓存：AI 已配置就自动生成一次，否则温暖引导去配置
    var aiReady = false;
    try {
      var st = await api('/api/settings');
      aiReady = !!(st && st.has_ai_key && st.ai_base_url);
    } catch (e) { aiReady = false; }
    if (aiReady) {
      generateSummary();
    } else {
      box.innerHTML =
        '<div class="card summary-guide">' +
        '<svg class="empty-ic"><use href="#i-sprout"/></svg>' +
        '<p class="empty-title">这里会讲出你一路走来的故事</p>' +
        '<p class="empty-sub">配置好 AI 后，它会读你的记录，为你总结成长轨迹</p>' +
        '<a class="btn btn-ghost" href="#/settings">去设置页配置 AI</a>' +
        '</div>';
    }
  }

  async function generateSummary() {
    var box = $('#summary-box');
    if (!box) return;
    box.innerHTML = '<div class="loading"><div class="spinner"></div><p>AI 正在回顾你的记录…</p></div>';
    var regenBtn = $('#summary-regen');
    if (regenBtn) regenBtn.disabled = true;
    try {
      var data = await api('/api/growth/summary/generate', { method: 'POST', timeout: 600000 });
      renderSummary(data && data.summary);
      toast('成长轨迹已更新', 'success');
    } catch (e) {
      // 422 记录太少 / 502 AI 失败：展示后端 detail
      box.innerHTML = emptyHtml('这次没能生成总结', e.message);
      if (regenBtn) regenBtn.classList.remove('hidden');
    } finally {
      if (regenBtn) regenBtn.disabled = false;
    }
  }

  function renderSummary(s) {
    var box = $('#summary-box');
    if (!box) return;
    var regenBtn = $('#summary-regen');
    if (regenBtn) regenBtn.classList.remove('hidden');
    if (!s) {
      box.innerHTML = emptyHtml('还没有成长总结', '记录多了之后，点右上角「重新总结」试试');
      return;
    }
    var strengths = Array.isArray(s.strengths) ? s.strengths : [];
    var milestones = Array.isArray(s.milestones) ? s.milestones : [];
    var evidenceReady = milestones.some(function (m) {
      return Array.isArray(m.evidence_entry_ids) && m.evidence_entry_ids.length && m.evidence_quote;
    });
    box.innerHTML =
      '<div class="card summary-card">' +
      (!evidenceReady ? '<div class="hint summary-evidence-hint">这份总结来自旧版本，暂未绑定原文证据；点击“重新总结”后会显示可核对的记录。</div>' : '') +
      '<div class="summary-narrative md">' + mdToHtml(s.narrative || '') + '</div>' +
      (strengths.length
        ? '<div class="strength-tags">' + strengths.map(function (t) { return '<span class="tag">' + esc(t) + '</span>'; }).join('') + '</div>'
        : '') +
      '<div class="summary-meta">基于 ' + esc(s.entry_count) + ' 条记录 · 总结于 ' + esc(absTime(s.created_at)) + '</div>' +
      '</div>' +
      '<div class="growth-minis" id="growth-minis"></div>' +
      (milestones.length
        ? '<div class="milestone-tl">' + milestones.map(function (m) {
            return '<div class="ms-item"><span class="ms-dot"></span>' +
              '<div class="ms-when">' + esc(m.when || '') + '</div>' +
              '<div class="ms-title">' + esc(m.title || '') + '</div>' +
              (m.detail ? '<div class="ms-detail">' + esc(m.detail) + '</div>' : '') +
              (m.evidence_quote ? '<div class="ms-evidence">“' + esc(m.evidence_quote) + '”</div>' : '') +
              (Array.isArray(m.evidence_entry_ids) && m.evidence_entry_ids.length
                ? '<div class="ms-source">证据：' + m.evidence_entry_ids.map(function (id) {
                    return '<a href="#/entry/' + encodeURIComponent(id) + '">打开原记录</a>';
                  }).join(' · ') + '</div>' : '') +
              '</div>';
          }).join('') + '</div>'
        : '');
    loadGrowthMinis();
  }

  /* ---- 成长轨迹 mini 数据带（全部来自 SQL 统计，不让 AI 编） ---- */
  async function loadGrowthMinis() {
    var band = $('#growth-minis');
    if (!band) return;
    var s;
    try {
      s = await api('/api/stats/overview');
    } catch (e) { return; } // 静默：锦上添花而已
    if (!document.body.contains(band)) return;
    if (!LIB.echarts) {
      // 降级：一行统计小字
      var w12 = (s.weekly_12 || []).reduce(function (sum, w) { return sum + (w.count || 0); }, 0);
      var kc = s.knowledge_counts || {};
      var kTotal = ['experience', 'pitfall', 'case', 'sop', 'skill'].reduce(function (sum, k) { return sum + (kc[k] || 0); }, 0);
      var cc0 = Object.keys(s.category_counts || {}).map(function (k) { return [CATS[k] || k, s.category_counts[k]]; })
        .sort(function (a, b) { return b[1] - a[1]; }).slice(0, 2);
      var ccText = cc0.length ? ' · ' + cc0.map(function (c) { return c[0] + ' ' + c[1]; }).join(' · ') : '';
      band.innerHTML = '<p class="hint mt-0 mb-14">近 12 周 ' + esc(w12) + ' 条' + esc(ccText) +
        ' · 已入库知识 ' + esc(kTotal) + ' 条 · 连续 ' + esc(s.streak_days) + ' 天</p>';
      return;
    }
    band.innerHTML =
      '<div class="gm-card"><div class="gm-label">近 12 周</div><div class="gm-chart" id="gm-trend"></div></div>' +
      '<div class="gm-card"><div class="gm-label">分类</div><div class="gm-chart" id="gm-cat"></div></div>' +
      '<div class="gm-card"><div class="gm-label">已入库知识</div><div class="gm-chart" id="gm-know"></div></div>' +
      '<div class="gm-card gm-num-card"><div class="gm-num">' + esc(s.streak_days) + '</div><div class="gm-label">连续记录天数</div></div>';
    // 近 12 周趋势 mini 折线（无坐标轴）
    var weekly = s.weekly_12 || [];
    makeChart('gm-trend', {
      grid: { left: 2, right: 2, top: 6, bottom: 2 },
      xAxis: { type: 'category', show: false, data: weekly.map(function (w) { return w.week; }) },
      yAxis: { type: 'value', show: false },
      tooltip: { trigger: 'axis', formatter: function (ps) { var p = ps[0]; return p.name + '：' + p.data + ' 条'; } },
      series: [{
        type: 'line', smooth: true, symbol: 'none',
        lineStyle: { color: CHART_COLORS.accent, width: 2 },
        areaStyle: { color: CHART_COLORS.accent, opacity: 0.15 },
        data: weekly.map(function (w) { return w.count; })
      }]
    });
    // 分类 mini 环形（Top 3 类目）
    var cc = s.category_counts || {};
    var ccMini = Object.keys(cc).map(function (k) { return { name: CATS[k] || k, value: cc[k] }; })
      .filter(function (d) { return d.value > 0; })
      .sort(function (a, b) { return b.value - a.value; }).slice(0, 3);
    makeChart('gm-cat', {
      color: [CHART_COLORS.accent, CHART_COLORS.green, CHART_COLORS.yellow],
      tooltip: { formatter: '{b}：{c} 条' },
      series: [{
        type: 'pie', radius: ['52%', '78%'], center: ['50%', '50%'],
        label: { show: false }, labelLine: { show: false },
        itemStyle: { borderColor: '#fffefb', borderWidth: 2 },
        data: ccMini
      }]
    });
    // 知识类型分布 mini 柱（已入库）
    var kc2 = s.knowledge_counts || {};
    var kOrder = [['experience', '经验', CHART_COLORS.accent], ['pitfall', '踩坑', CHART_COLORS.red], ['case', '案例', CHART_COLORS.yellow], ['sop', 'SOP', CHART_COLORS.green], ['skill', '技能', CHART_COLORS.brown]];
    makeChart('gm-know', {
      grid: { left: 2, right: 2, top: 6, bottom: 18 },
      xAxis: { type: 'category', data: kOrder.map(function (k) { return k[1]; }),
        axisLine: { show: false }, axisTick: { show: false },
        axisLabel: { fontSize: 10, color: '#a09482', interval: 0 } },
      yAxis: { type: 'value', show: false },
      tooltip: { formatter: function (p) { var v = (p.data && p.data.value != null) ? p.data.value : p.data; return p.name + '：' + v + ' 条'; } },
      series: [{
        type: 'bar', barMaxWidth: 14,
        itemStyle: { borderRadius: [4, 4, 0, 0] },
        data: kOrder.map(function (k) {
          return { value: kc2[k[0]] || 0, itemStyle: { color: k[2] } };
        })
      }]
    });
  }

  async function loadRoadmap() {
    var box = $('#roadmap-box');
    if (!box) return;
    box.innerHTML = loadingHtml();
    try {
      var data = await api('/api/growth/roadmap');
      showCurrentDirection(data && data.direction ? data.direction : '');
      renderRoadmap(data.nodes || []);
    } catch (e) {
      box.innerHTML = errorHtml(e.message);
    }
  }

  function renderRoadmap(nodes) {
    var box = $('#roadmap-box');
    if (!nodes.length) {
      box.innerHTML = emptyHtml('还没有未来路线', '点上方「发现我的方向」，让 AI 从你的记录里找方向');
      return;
    }
    var buckets = [
      { key: 30, title: '30 天内', nodes: [] },
      { key: 90, title: '90 天内', nodes: [] },
      { key: 180, title: '180 天内', nodes: [] },
      { key: 365, title: '一年内', nodes: [] }
    ];
    nodes.slice().sort(function (a, b) { return (a.sort_order || 0) - (b.sort_order || 0); }).forEach(function (n) {
      var h = n.horizon_days || 30;
      var b = h <= 30 ? buckets[0] : h <= 90 ? buckets[1] : h <= 180 ? buckets[2] : buckets[3];
      b.nodes.push(n);
    });
    var html = '';
    buckets.forEach(function (b) {
      if (!b.nodes.length) return;
      html += '<div class="node-group-title">' + b.title + '</div>' + b.nodes.map(nodeHtml).join('');
    });
    box.innerHTML = html;
    $$('#roadmap-box [data-act]').forEach(function (btn) {
      btn.onclick = function () {
        var id = btn.closest('.node').dataset.node;
        updateNodeStatus(id, btn.dataset.act, btn);
      };
    });
    $$('#roadmap-box [data-checkin]').forEach(function (btn) {
      btn.onclick = function () {
        var id = btn.closest('.node').dataset.node;
        var current = nodes.filter(function (x) { return x.id === id; })[0];
        if (current) openRoadmapCheckin(current);
      };
    });
  }

  function nodeHtml(n) {
    var statusName = { suggested: '建议中', accepted: '已接受', rejected: '已拒绝', done: '已完成' }[n.status] || n.status;
    var cls = (n.status === 'accepted' || n.status === 'done') ? ' node-active' : '';
    var evidence = Array.isArray(n.evidence) ? n.evidence :
      (Array.isArray(n.evidence_entry_ids) ? n.evidence_entry_ids.map(function (id) { return { id: id }; }) : []);
    var btns = '';
    if (n.status === 'suggested') {
      btns = '<button class="btn btn-ghost btn-sm" data-act="accepted">接受</button>' +
        '<button class="btn btn-sm btn-ghost" data-act="rejected">拒绝</button>';
    } else if (n.status === 'accepted') {
      btns = '<button class="btn btn-ghost btn-sm" data-act="done">完成</button>';
    } else if (n.status === 'rejected') {
      btns = '<button class="btn btn-sm btn-ghost" data-act="accepted">接受</button>';
    }
    if (n.status === 'accepted' || n.status === 'done') {
      btns += '<button class="btn btn-ghost btn-sm" data-checkin="1">更新进度</button>';
    }
    var progress = Math.max(0, Math.min(100, Number(n.progress || 0)));
    return '<div class="node' + cls + '" data-node="' + esc(n.id) + '">' +
      '<div class="node-head"><span class="node-title">' + esc(n.title) + '</span>' +
      '<span class="badge status-' + esc(n.status) + '">' + esc(statusName) + '</span></div>' +
      (n.description ? '<p class="node-desc">' + esc(n.description) + '</p>' : '') +
      (n.first_step ? '<div class="node-meta"><b>先做：</b>' + esc(n.first_step) + '</div>' : '') +
      (n.done_when ? '<div class="node-meta"><b>完成标准：</b>' + esc(n.done_when) + '</div>' : '') +
      ((n.status === 'accepted' || n.status === 'done') ?
        '<div class="node-progress"><div class="node-progress-head"><span>完成度' +
        (n.checkin_count ? ' · 已复盘 ' + esc(n.checkin_count) + ' 次' : '') +
        '</span><b>' + esc(progress) + '%</b></div>' +
        '<div class="node-progress-track"><span style="width:' + progress + '%"></span></div></div>' : '') +
      (n.checkin_note ? '<div class="node-checkin"><b>最近复盘：</b>' + esc(n.checkin_note) +
        (n.last_checkin_at ? '<span class="muted"> · ' + esc(absTime(n.last_checkin_at)) + '</span>' : '') + '</div>' : '') +
      (evidence.length
        ? '<div class="node-evidence">证据：' + evidence.map(function (item) {
            var id = typeof item === 'string' ? item : item.id;
            var label = typeof item === 'string' ? '打开记录' :
              ((item.occurred_at ? fmtMD(item.occurred_at) + ' · ' : '') +
               (item.title || item.excerpt || '打开记录'));
            return '<a href="#/entry/' + encodeURIComponent(id) + '" title="' + esc(item.excerpt || '') + '">' + esc(label) + '</a>';
          }).join(' · ') + '</div>' : '') +
      '<div class="node-btns">' + btns + '</div>' +
      '</div>';
  }

  async function openRoadmapCheckin(node) {
    var result = await roadmapCheckinModal(node);
    if (!result) return;
    try {
      await api('/api/growth/roadmap/nodes/' + encodeURIComponent(node.id), {
        method: 'PATCH', body: { progress: result.progress, checkin_note: result.note }
      });
      toast(result.progress >= 100 ? '这一步完成了，记得回看成果' : '进度已记录', 'success');
      loadRoadmap();
    } catch (e) { toast(e.message, 'error'); }
  }

  function roadmapCheckinModal(node) {
    return new Promise(function (resolve) {
      var root = $('#modal-root');
      root.innerHTML = '<div class="modal-wrap"><div class="modal checkin-modal">' +
        '<h3 class="modal-title">更新路线进度</h3>' +
        '<p class="modal-text">记录这一步现在走到哪里，下次回来就能接着做。</p>' +
        '<div class="set-row"><label for="roadmap-progress">完成度（0-100）</label>' +
        '<input class="input" id="roadmap-progress" type="number" min="0" max="100" step="5" value="' + esc(node.progress || 0) + '"></div>' +
        '<div class="set-row"><label for="roadmap-note">本次复盘</label>' +
        '<textarea class="input" id="roadmap-note" rows="4" placeholder="做了什么、遇到什么、下一步是什么…">' + esc(node.checkin_note || '') + '</textarea></div>' +
        '<div class="modal-btns"><button class="btn btn-ghost" id="roadmap-cancel">取消</button>' +
        '<button class="btn" id="roadmap-save">保存</button></div></div></div>';
      var done = function (value) { closeModal(); resolve(value); };
      $('#roadmap-cancel').onclick = function () { done(null); };
      $('.modal-wrap', root).addEventListener('click', function (e) {
        if (e.target.classList.contains('modal-wrap')) done(null);
      });
      $('#roadmap-save').onclick = function () {
        var progress = Number($('#roadmap-progress').value);
        if (!Number.isFinite(progress) || progress < 0 || progress > 100) {
          toast('完成度请填写 0 到 100', 'error');
          return;
        }
        done({ progress: Math.round(progress), note: $('#roadmap-note').value.trim() });
      };
      $('#roadmap-progress').focus();
    });
  }

  async function updateNodeStatus(id, status, btn) {
    btn.disabled = true;
    try {
      // 注意：路径是 /api/growth/roadmap/nodes/{id}
      await api('/api/growth/roadmap/nodes/' + encodeURIComponent(id), {
        method: 'PATCH', body: { status: status }
      });
      toast(status === 'done' ? '完成一步，真不错' : '已更新', 'success');
      loadRoadmap();
    } catch (e) {
      toast(e.message, 'error');
      btn.disabled = false;
    }
  }

  async function generateRoadmap(direction) {
    showOverlay('AI 正在为你规划成长路线，请稍候…');
    try {
      var body = direction ? { direction: direction } : {};
      await api('/api/growth/roadmap/generate', { method: 'POST', body: body, timeout: 600000 });
      hideOverlay();
      toast('成长路线已生成', 'success');
      if (direction) {
        selectedDirection = '';
        var genBtn = $('#dir-gen');
        if (genBtn) genBtn.textContent = '发现我的方向';
        var box = $('#dir-box');
        if (box) box.innerHTML = '';
      }
      loadRoadmap();
    } catch (e) {
      hideOverlay();
      toast(e.message, 'error', 6000);
    }
  }

  async function loadForecast() {
    var box = $('#forecast-box');
    if (!box) return;
    try {
      var data = await api('/api/growth/forecast/latest');
      renderForecast(data && data.forecast);
    } catch (e) {
      box.innerHTML = errorHtml(e.message);
    }
  }

  function renderForecast(forecast, notice) {
    var box = $('#forecast-box');
    if (!forecast) {
      box.innerHTML = emptyHtml(
        notice || '还没有推演结果',
        notice ? '坚持记录，之后再来看吧' : '记录满 8 周后，就能让 AI 推演未来趋势'
      );
      return;
    }
    var clsMap = { '基准': '', '积极': ' scen-up', '保守': ' scen-down' };
    var proj = forecast.stats && forecast.stats.projection_90d;
    var projectionHtml = proj
      ? '<div class="card forecast-baseline"><b>程序基线（不是承诺）</b><span>未来90天约 ' +
        esc(proj.baseline_entries) + ' 条记录</span><span class="muted">历史波动区间 ' +
        esc(proj.low_entries) + '–' + esc(proj.high_entries) + ' 条 · 样本 ' +
        esc(proj.sample_weeks) + ' 周</span></div>' : '';
    var html = '<p class="hint mb-12">基于 ' + esc(forecast.data_weeks) + ' 周数据 · 生成于 ' + esc(absTime(forecast.generated_at)) + '</p>' +
      projectionHtml +
      '<div class="scen-grid">' +
      (forecast.scenarios || []).map(function (s) {
        // confidence 契约：数字（0–1 或 0–100）或中文档（低/中/高）
        var conf = null;
        if (typeof s.confidence === 'number') conf = Math.round(s.confidence <= 1 ? s.confidence * 100 : s.confidence) + '%';
        else if (s.confidence != null && s.confidence !== '') conf = String(s.confidence);
        return '<div class="card scen' + (clsMap[s.name] != null ? clsMap[s.name] : '') + '">' +
          '<div class="scen-head"><span class="scen-name">' + esc(s.name) + '情景</span>' +
          (conf != null ? '<span class="badge">置信度 ' + esc(conf) + '</span>' : '') +
          '<span class="muted">' + esc(s.horizon_days) + ' 天</span></div>' +
          '<div class="md">' + mdToHtml(s.narrative || '') + '</div>' +
          (Array.isArray(s.assumptions) && s.assumptions.length
            ? '<h4>依据假设</h4><ul>' + s.assumptions.map(function (x) { return '<li>' + esc(x) + '</li>'; }).join('') + '</ul>'
            : '') +
          (Array.isArray(s.invalidators) && s.invalidators.length
            ? '<h4>失效条件</h4><ul>' + s.invalidators.map(function (x) { return '<li>' + esc(x) + '</li>'; }).join('') + '</ul>'
            : '') +
          (Array.isArray(s.next_actions) && s.next_actions.length
            ? '<h4>下一步动作</h4><ul class="scen-actions">' + s.next_actions.map(function (x) { return '<li>' + esc(x) + '</li>'; }).join('') + '</ul>'
            : '') +
          (Array.isArray(s.watch_signals) && s.watch_signals.length
            ? '<h4>每周观察</h4><ul>' + s.watch_signals.map(function (x) { return '<li>' + esc(x) + '</li>'; }).join('') + '</ul>'
            : '') +
          (s.evidence_quote ? '<div class="scen-evidence">“' + esc(s.evidence_quote) + '”</div>' : '') +
          (Array.isArray(s.evidence_entry_ids) && s.evidence_entry_ids.length
            ? '<div class="scen-source">依据记录：' + s.evidence_entry_ids.map(function (id) {
                return '<a href="#/entry/' + encodeURIComponent(id) + '">打开原记录</a>';
              }).join(' · ') + '</div>' : '') +
          (s.review_by ? '<div class="scen-review">建议在 ' + esc(s.review_by) + ' 回看一次：哪些信号真的出现了？</div>' : '') +
          '</div>';
      }).join('') + '</div>';
    box.innerHTML = html;
  }

  async function generateForecast() {
    showOverlay('AI 正在分析你的记录趋势，推演未来情景…');
    try {
      await api('/api/growth/forecast/generate', { method: 'POST', timeout: 600000 });
      hideOverlay();
      toast('情景推演已生成', 'success');
      loadForecast();
    } catch (e) {
      hideOverlay();
      if (e.status === 422) {
        renderForecast(null, e.message);
      } else {
        toast(e.message, 'error', 6000);
      }
    }
  }

  /* ---- 我的假设 / 自我实验（完全基于真实记录，不调用 AI） ---- */
  var experimentFilter = 'active';
  var EXP_CONCLUSIONS = {
    supported: '支持',
    refuted: '反驳',
    insufficient: '证据不足',
    undecided: '未判定'
  };

  function addDaysIso(days) {
    var d = new Date();
    d.setHours(12, 0, 0, 0);
    d.setDate(d.getDate() + days);
    return localDateStr(d);
  }

  function openExperimentModal() {
    var root = $('#modal-root');
    root.innerHTML = '<div class="modal-wrap"><div class="modal experiment-modal">' +
      '<h3 class="modal-title">添加一项自我实验</h3>' +
      '<p class="modal-text">写一个你想观察的假设。小满只会在日期窗口里查找记录，不会自动把它解释成因果。</p>' +
      '<div class="set-row"><label for="exp-title">假设</label><input class="input" id="exp-title" maxlength="120" placeholder="例如：连续早起让我白天更专注"></div>' +
      '<div class="set-row"><label for="exp-metric">观察词 / 判断规则</label><input class="input" id="exp-metric" maxlength="100" placeholder="例如：专注、早起、番茄钟（按字面查找）"></div>' +
      '<div class="set-row exp-date-grid"><div><label for="exp-start">开始日期</label><input class="input" id="exp-start" type="date" value="' + addDaysIso(0) + '"></div>' +
      '<div><label for="exp-end">结束日期</label><input class="input" id="exp-end" type="date" value="' + addDaysIso(14) + '"></div></div>' +
      '<div class="modal-btns"><button class="btn btn-ghost" id="exp-cancel">取消</button><button class="btn" id="exp-save">开始观察</button></div>' +
      '</div></div>';
    var done = function () { closeModal(); };
    $('#exp-cancel').onclick = done;
    $('.modal-wrap', root).addEventListener('click', function (e) { if (e.target.classList.contains('modal-wrap')) done(); });
    $('#exp-save').onclick = async function () {
      var title = $('#exp-title').value.trim();
      var metric = $('#exp-metric').value.trim();
      var start = $('#exp-start').value;
      var end = $('#exp-end').value;
      if (!title) { toast('先写下想验证的假设', 'error'); return; }
      if (!metric) { toast('请填写观察词或判断规则', 'error'); return; }
      if (!start || !end || start > end) { toast('请检查起止日期', 'error'); return; }
      var btn = $('#exp-save'); btn.disabled = true;
      try {
        await api('/api/growth/experiments', { method: 'POST', body: { title: title, metric: metric, start_date: start, end_date: end } });
        done();
        toast('实验已开始，记得在结束时回看证据', 'success');
        loadExperiments();
      } catch (e) { toast(e.message, 'error'); btn.disabled = false; }
    };
    $('#exp-title').focus();
  }

  async function loadExperiments() {
    var box = $('#experiments-box');
    if (!box) return;
    box.innerHTML = loadingHtml();
    try {
      var data = await api('/api/growth/experiments');
      renderExperiments((data && data.items) || []);
    } catch (e) { box.innerHTML = errorHtml(e.message); }
  }

  function renderExperiments(items) {
    var box = $('#experiments-box');
    if (!box) return;
    var active = items.filter(function (x) { return x.status === 'active'; });
    var ended = items.filter(function (x) { return x.status !== 'active'; });
    var shown = experimentFilter === 'ended' ? ended : active;
    var tabs = '<div class="experiment-tabs" role="tablist">' +
      '<button class="status-link' + (experimentFilter === 'active' ? ' active' : '') + '" data-exp-filter="active">进行中 <span>' + active.length + '</span></button>' +
      '<button class="status-link' + (experimentFilter === 'ended' ? ' active' : '') + '" data-exp-filter="ended">已结束 <span>' + ended.length + '</span></button>' +
      '</div>';
    if (!shown.length) {
      box.innerHTML = tabs + emptyHtml(experimentFilter === 'active' ? '还没有进行中的假设' : '还没有结束的实验', experimentFilter === 'active' ? '从一个很小、能被记录验证的观察开始' : '结束一项实验后，这里会留下它的证据');
    } else {
      box.innerHTML = tabs + '<div class="experiment-list">' + shown.map(experimentHtml).join('') + '</div>';
    }
    $$('#experiments-box [data-exp-filter]').forEach(function (btn) {
      btn.onclick = function () { experimentFilter = btn.dataset.expFilter; renderExperiments(items); };
    });
    $$('#experiments-box [data-exp-finish]').forEach(function (btn) {
      btn.onclick = function () { finishExperiment(btn.dataset.expFinish); };
    });
  }

  function experimentHtml(x) {
    var r = x.result || {};
    var isActive = x.status === 'active';
    var status = isActive ? (x.due ? '已到结束日' : '进行中') : ('已结束 · ' + (EXP_CONCLUSIONS[x.conclusion] || x.conclusion || '未判定'));
    var evidence = Array.isArray(r.evidence) ? r.evidence : [];
    var evidenceHtml = evidence.length ? '<div class="experiment-evidence"><span class="muted">匹配记录</span>' + evidence.map(function (e) {
      return '<a href="#/entry/' + encodeURIComponent(e.entry_id) + '">' + esc(e.date || absDate(e.occurred_at)) + ' · ' + esc(e.title || '打开记录') + '</a>';
    }).join('') + (r.matching_entries > evidence.length ? '<span class="muted">另有 ' + esc(r.matching_entries - evidence.length) + ' 条</span>' : '') + '</div>' : '<div class="experiment-no-evidence">窗口内还没有匹配到观察词「' + esc(x.metric) + '」的记录</div>';
    return '<article class="experiment-item ' + (isActive ? 'experiment-active' : 'experiment-ended') + '">' +
      '<div class="experiment-head"><div><h3>' + esc(x.title) + '</h3><p>观察词：<b>' + esc(x.metric) + '</b></p></div><span class="badge experiment-status">' + esc(status) + '</span></div>' +
      '<div class="experiment-meta"><span>' + esc(x.start_date) + ' 至 ' + esc(x.end_date) + '</span><span>窗口内 ' + esc(r.total_entries || 0) + ' 条记录 · ' + esc(r.active_days || 0) + ' 个有记录日</span><span>匹配 ' + esc(r.matching_entries || 0) + ' 条 · ' + esc(r.matching_days || 0) + ' 个匹配日</span></div>' +
      evidenceHtml +
      (r.assessment_basis ? '<p class="experiment-basis">' + esc(r.assessment_basis) + '</p>' : '') +
      '<p class="experiment-note">' + esc(x.causality_notice || '这些记录只能帮助观察，不能证明因果。') + '</p>' +
      (isActive ? '<div class="experiment-actions"><button class="btn btn-ghost btn-sm" data-exp-finish="' + esc(x.id) + '">结束并统计</button></div>' : '') +
      '</article>';
  }

  async function finishExperiment(id) {
    var choice = await choiceModal({
      title: '结束这项自我实验？',
      body: '<p class="modal-text">小满会按真实记录计算数量，并保留可回看的证据链接。选择“支持”或“反驳”只是你的判断，不代表因果已被证明。</p>',
      choices: [
        { value: 'supported', label: '支持', sub: '记录更符合这个假设' },
        { value: 'refuted', label: '反驳', sub: '记录更不符合这个假设' },
        { value: 'insufficient', label: '证据不足', sub: '记录太少或无法判断' },
        { value: 'undecided', label: '暂不判断', sub: '先保存记录，之后再看' }
      ]
    });
    if (!choice) return;
    try {
      await api('/api/growth/experiments/' + encodeURIComponent(id) + '/finish', { method: 'POST', body: { conclusion: choice } });
      toast('实验已结束，证据已经整理好', 'success');
      loadExperiments();
    } catch (e) { toast(e.message, 'error'); }
  }

  /* ================= 知识库页 ================= */
  var KTYPES = {
    experience: ['经验', 'kt-experience'],
    pitfall: ['踩坑', 'kt-pitfall'],
    case: ['案例', 'kt-case'],
    sop: ['SOP', 'kt-sop'],
    skill: ['技能', 'kt-skill']
  };
  var knowState = null;
  var KNOW_TABS = [ ['pending', '待确认'], ['accepted', '已入库'], ['rejected', '已拒绝'] ];

  async function renderKnowledge(view) {
    knowState = { status: 'pending', type: '' };
    view.innerHTML =
      '<div class="page">' +
      '<header class="page-head"><h1>知识库</h1><p class="page-sub">AI 从记录里提炼的经验，确认后就是你的财富</p></header>' +
      '<div class="know-toolbar">' +
      '<div class="tabs mb-0" id="know-tabs">' +
      KNOW_TABS.map(function (kv, i) {
        return '<button class="pill' + (i === 0 ? ' active' : '') + '" data-status="' + kv[0] + '">' + kv[1] + '</button>';
      }).join('') +
      '</div>' +
      '<select class="input" id="know-type">' +
      '<option value="">全部类型</option><option value="experience">经验</option><option value="pitfall">踩坑</option>' +
      '<option value="case">案例</option><option value="sop">SOP</option><option value="skill">技能</option>' +
      '</select>' +
      '<span class="spacer"></span>' +
      '<a class="btn btn-ghost" href="/api/export/knowledge"><svg class="ic"><use href="#i-dl"/></svg>导出已入库 Markdown</a>' +
      '</div>' +
      '<div id="know-list">' + loadingHtml() + '</div>' +
      '</div>';

    $('#know-tabs').addEventListener('click', function (e) {
      var btn = e.target.closest('.pill');
      if (!btn) return;
      $$('#know-tabs .pill').forEach(function (p) { p.classList.remove('active'); });
      btn.classList.add('active');
      knowState.status = btn.dataset.status;
      loadKnowledge();
    });
    $('#know-type').onchange = function () {
      knowState.type = $('#know-type').value;
      loadKnowledge();
    };
    loadKnowledge();
  }

  async function loadKnowledge() {
    var box = $('#know-list');
    if (!box) return;
    box.innerHTML = loadingHtml();
    var qs = '?status=' + encodeURIComponent(knowState.status);
    if (knowState.type) qs += '&type=' + encodeURIComponent(knowState.type);
    try {
      var data = await api('/api/knowledge' + qs);
      var items = data.items || [];
      knowState.items = items;
      if (!items.length) {
        box.innerHTML = emptyHtml(
          knowState.status === 'pending' ? '没有待确认的知识' : knowState.status === 'accepted' ? '知识库还空着' : '没有已忽略的知识',
          knowState.status === 'pending' ? 'AI 生成报告后，提炼的知识会出现在这里' : '每一步都算数，慢慢来'
        );
        return;
      }
      box.innerHTML = items.map(knowCardHtml).join('');
      $$('#know-list [data-know-act]').forEach(function (btn) {
        btn.onclick = function () {
          knowAction(btn.closest('.know-card').dataset.kid, btn.dataset.knowAct, btn);
        };
      });
      $$('#know-list [data-know-edit]').forEach(function (btn) {
        btn.onclick = function () { knowEdit(btn.dataset.knowEdit, btn); };
      });
      $$('#know-list [data-know-del]').forEach(function (btn) {
        btn.onclick = function () { knowDelete(btn.dataset.knowDel, btn); };
      });
    } catch (e) {
      box.innerHTML = errorHtml(e.message);
    }
  }

  function knowCardHtml(k) {
    var kt = KTYPES[k.type] || [k.type || '知识', 'kt-experience'];
    var sourceHtml = '';
    var sources = Array.isArray(k.sources) ? k.sources : [];
    if (sources.length) {
      sourceHtml += sources.slice(0, 3).map(function (s) {
        var label = s.title || s.summary || '打开记录';
        return '<a href="#/entry/' + encodeURIComponent(s.id) + '" title="' + esc(s.quote || label) + '">来源记录 · ' + esc(label.slice(0, 24)) + ' →</a>';
      }).join('');
      if (sources.length > 3) sourceHtml += '<span class="muted">另有 ' + esc(sources.length - 3) + ' 条来源</span>';
    } else if (k.entry_id && k.source_entry_exists !== false) {
      // 兼容旧后端只返回 entry_id 的响应。
      sourceHtml += '<a href="#/entry/' + encodeURIComponent(k.entry_id) + '">来源记录 →</a>';
    }
    if (k.source_report_id && k.source_report_exists !== false) {
      sourceHtml += '<a href="#/reports/' + encodeURIComponent(k.source_report_id) + '">来源报告 →</a>';
    }
    if (!sourceHtml) sourceHtml = '<span class="muted">来源未记录</span>';
    return '<div class="card know-card" data-kid="' + esc(k.id) + '">' +
      '<h2 class="card-title"><span class="kt-badge ' + kt[1] + '">' + esc(kt[0]) + '</span>' + esc(k.title) + '</h2>' +
      '<div class="know-content md">' + mdToHtml(k.content || '') + '</div>' +
      '<div class="know-foot">' +
      sourceHtml +
      '<span class="muted">' + esc(absDate(k.created_at)) + '</span>' +
      '<div class="know-actions">' +
      (knowState.status === 'pending'
        ? '<button class="btn btn-ghost btn-sm" data-know-act="accept">入库</button>' +
          '<button class="btn btn-ghost btn-sm" data-know-act="reject">忽略</button>'
        : '') +
      '<button class="quiet-del" data-know-edit="' + esc(k.id) + '" title="编辑"><svg class="ic"><use href="#i-edit"/></svg></button>' +
      '<button class="quiet-del" data-know-del="' + esc(k.id) + '" title="删除"><svg class="ic"><use href="#i-close"/></svg></button>' +
      '</div>' +
      '</div></div>';
  }

  async function knowAction(id, act, btn) {
    btn.disabled = true;
    try {
      await api('/api/knowledge/' + encodeURIComponent(id) + '/' + act, { method: 'POST' });
      var card = btn.closest('.know-card');
      card.style.opacity = '0.4';
      setTimeout(function () {
        card.remove();
        if (!$('#know-list').children.length) loadKnowledge();
      }, 250);
      toast(act === 'accept' ? '已入库，知识在慢慢积累' : '已忽略', 'success');
    } catch (e) {
      toast(e.message, 'error');
      btn.disabled = false;
    }
  }

  // 编辑知识：标题 + 类型 + 内容
  function knowEditModal(k) {
    return new Promise(function (resolve) {
      var root = $('#modal-root');
      var typeOpts = Object.keys(KTYPES).map(function (t) {
        return '<option value="' + t + '"' + (k.type === t ? ' selected' : '') + '>' + KTYPES[t][0] + '</option>';
      }).join('');
      root.innerHTML =
        '<div class="modal-wrap"><div class="modal">' +
        '<h3 class="modal-title">编辑知识</h3>' +
        '<div class="set-row"><label>标题</label><input class="input" id="ke-title" value="' + esc(k.title || '') + '"></div>' +
        '<div class="set-row"><label>类型</label><select class="input w-full" id="ke-type">' + typeOpts + '</select></div>' +
        '<div class="set-row"><label>内容</label><textarea class="input" id="ke-content" rows="7">' + esc(k.content || '') + '</textarea></div>' +
        '<div class="modal-btns">' +
        '<button class="btn btn-ghost" id="ke-cancel">取消</button>' +
        '<button class="btn" id="ke-ok">保存</button>' +
        '</div></div></div>';
      var done = function (v) { closeModal(); resolve(v); };
      $('#ke-cancel').onclick = function () { done(null); };
      $('.modal-wrap', root).addEventListener('click', function (e) {
        if (e.target.classList.contains('modal-wrap')) done(null);
      });
      $('#ke-ok').onclick = function () {
        done({
          title: $('#ke-title').value.trim(),
          type: $('#ke-type').value,
          content: $('#ke-content').value
        });
      };
      $('#ke-title').focus();
    });
  }

  async function knowEdit(id, btn) {
    var k = (knowState.items || []).find(function (it) { return String(it.id) === String(id); });
    if (!k) return;
    var patch = await knowEditModal(k);
    if (!patch) return;
    if (!patch.title) { toast('标题不能为空', 'error'); return; }
    btn.disabled = true;
    try {
      await api('/api/knowledge/' + encodeURIComponent(id), { method: 'PATCH', body: patch });
      toast('已保存', 'success');
      loadKnowledge(); // 类型可能变化导致不再匹配当前筛选，重载最稳
    } catch (e) {
      toast(e.message, 'error');
      btn.disabled = false;
    }
  }

  async function knowDelete(id, btn) {
    var ok = await confirmModal('删除后不可恢复，确定删除这条知识吗？', '删除');
    if (!ok) return;
    btn.disabled = true;
    try {
      await api('/api/knowledge/' + encodeURIComponent(id), { method: 'DELETE' });
      var card = btn.closest('.know-card');
      card.style.opacity = '0.4';
      setTimeout(function () {
        card.remove();
        if (!$('#know-list').children.length) loadKnowledge();
      }, 250);
      toast('已删除', 'success');
    } catch (e) {
      toast(e.message, 'error');
      btn.disabled = false;
    }
  }

  /* ================= 设置页 ================= */
  /* ---- 复制到剪贴板（带降级） ---- */
  async function copyText(val) {
    try {
      await navigator.clipboard.writeText(val);
      toast('已复制', 'success');
    } catch (e) {
      var ta = document.createElement('textarea');
      ta.value = val;
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand('copy'); toast('已复制', 'success'); }
      catch (e2) { toast('复制失败，请手动选择复制', 'error'); }
      ta.remove();
    }
  }

  /* ---- 生成二维码 SVG（qrcode.js 缺失则返回空串） ---- */
  function makeQrSvg(text) {
    if (typeof window.qrcode !== 'function') return '';
    try {
      var qr = window.qrcode(0, 'M');
      qr.addData(text);
      qr.make();
      var svg = qr.createSvgTag(5, 2);
      if (window.DOMPurify) {
        svg = window.DOMPurify.sanitize(svg, { USE_PROFILES: { svg: true, svgFilters: true } });
      }
      return svg;
    } catch (e) { return ''; }
  }

  async function renderSettings(view) {
    view.innerHTML = '<div class="page"><header class="page-head"><h1>设置</h1><p class="page-sub">把这里收拾好，用起来更顺手</p></header><div id="set-body">' + loadingHtml() + '</div></div>';
    var st, si = null;
    try {
      st = await api('/api/settings');
    } catch (e) {
      $('#set-body').innerHTML = errorHtml(e.message);
      return;
    }
    try { si = await api('/api/server-info'); } catch (e) { si = null; }
    var phoneUrl = si && si.url ? si.url : '';
    var otherIps = si && Array.isArray(si.ips)
      ? si.ips.filter(function (ip) { return phoneUrl.indexOf(ip) === -1; })
      : [];
    var phoneCardInner;
    if (phoneUrl) {
      var qrSvg = makeQrSvg(phoneUrl);
      phoneCardInner =
        '<div class="phone-url-row"><code class="phone-url" id="phone-url" title="点击复制">' + esc(phoneUrl) + '</code>' +
        '<button class="btn btn-ghost btn-sm" id="phone-copy">复制</button></div>' +
        (qrSvg ? '<div class="qr-box">' + qrSvg + '</div>' : '') +
        (otherIps.length
          ? '<p class="hint">其他可用地址：' + otherIps.map(function (ip) {
              return 'http://' + esc(ip) + ':' + esc(si.port);
            }).join('　') + '</p>'
          : '') +
        '<ol class="api-list phone-steps">' +
        '<li>手机与电脑连接同一个 Wi-Fi</li>' +
        '<li>扫码，或在手机浏览器输入上面的地址</li>' +
        '<li>用浏览器「添加到主屏幕」，就像 App 一样打开</li>' +
        '</ol>';
    } else {
      phoneCardInner = '<p class="hint m-0">没检测到局域网地址，请确认电脑已连 Wi-Fi</p>';
    }
    $('#set-body').innerHTML =
      '<div class="card">' +
      '<h2 class="card-title">手机连接</h2>' +
      phoneCardInner +
      '</div>' +

      '<div class="card">' +
      '<h2 class="card-title">AI 接口</h2>' +
      '<div class="provider-toolbar"><div><strong>已保存的供应商</strong><p class="hint m-0">可同时保存多家服务商，激活后用于对应模型槽位</p></div><button class="btn btn-ghost btn-sm" id="s-provider-add">新增供应商</button></div>' +
      '<div class="provider-list" id="s-provider-list"><div class="provider-empty">正在读取供应商…</div></div>' +
      '<div class="provider-form hidden" id="s-provider-form">' +
      '<div class="set-row"><label>供应商名称</label><input class="input" id="s-provider-name" placeholder="例如：DeepSeek 主账号"></div>' +
      '<div class="set-row"><label>用途</label><select class="input" id="s-provider-slot"><option value="strong">大模型（报告、成长分析）</option><option value="fast">小模型（标题、摘要、分类）</option><option value="embed">嵌入模型（语义搜索）</option></select></div>' +
      '<div class="set-row"><label>接口地址（Base URL）</label><input class="input" id="s-provider-base" placeholder="https://api.example.com/v1"></div>' +
      '<div class="set-row"><label>模型名</label><input class="input" id="s-provider-model" placeholder="模型名称"></div>' +
      '<div class="set-row"><label>API Key <span class="field-hint" id="s-provider-key-hint">仅保存后端，不会回显</span></label><input class="input" id="s-provider-key" type="password" placeholder="新增时填写，编辑时留空表示不修改" autocomplete="new-password"></div>' +
      '<div class="set-row" id="s-provider-vision-row"><label class="check-row"><input type="checkbox" id="s-provider-vision"> 支持视觉输入</label></div>' +
      '<div class="set-row" id="s-provider-effort-row"><label>思考深度</label><select class="input" id="s-provider-effort"><option value="auto">自动适配</option><option value="low">轻量</option><option value="medium">标准</option><option value="high">深入</option><option value="xhigh">极深</option><option value="max">最大</option></select></div>' +
      '<div class="set-inline"><button class="btn" id="s-provider-save">保存供应商</button><button class="btn btn-ghost" id="s-provider-test">测试连接</button><button class="btn btn-ghost" id="s-provider-cancel">取消</button><span class="test-inline" id="s-provider-result"></span></div>' +
      '</div>' +
      '<details class="provider-advanced"><summary>兼容配置与维护工具</summary><div class="provider-advanced-body">' +
      '<p class="hint mt-0">模型与思考深度请优先在上方供应商中设置；这里仅用于兼容旧配置和执行维护操作。</p>' +
      '<div class="tabs" id="s-ai-tabs">' +
      '<button class="pill active" data-sec="strong">大模型</button>' +
      '<button class="pill" data-sec="fast">小模型</button>' +
      '<button class="pill" data-sec="embed">嵌入模型</button>' +
      '</div>' +
      '<div id="s-sec-strong">' +
      '<p class="hint mt-0">识图 · 报告 · 成长分析</p>' +
      '<div class="set-row"><label>接口地址（Base URL）</label><input class="input" id="s-base" placeholder="https://api.deepseek.com/v1" value="' + esc(st.ai_base_url || '') + '"></div>' +
      '<div class="set-row"><label>模型名</label><input class="input" id="s-model" placeholder="deepseek-chat" value="' + esc(st.ai_model || '') + '"></div>' +
      '<div class="set-row"><label>API Key' + (st.has_ai_key ? '（当前已配置）' : '（尚未配置）') + '</label>' +
      '<input class="input" id="s-key" type="password" placeholder="留空则不修改" autocomplete="new-password"></div>' +
      '<div class="set-row"><label class="check-row"><input type="checkbox" id="s-vision"' + (st.ai_vision ? ' checked' : '') + '> 模型支持视觉能力（分析照片）</label></div>' +
      '<input type="hidden" id="s-effort" value="' + esc(st.ai_effort || 'auto') + '">' +
      '<div class="set-inline"><button class="btn btn-ghost btn-sm" id="s-test-strong">测试连接</button><span class="test-inline" id="s-strong-result"></span></div>' +
      '</div>' +
      '<div id="s-sec-fast" class="hidden">' +
      '<p class="hint mt-0">自动标题 · 摘要 · 分类（可选，留空则与大模型共用）</p>' +
      '<div class="set-row"><label>接口地址（Base URL）</label><input class="input" id="s-fbase" placeholder="https://api.deepseek.com/v1" value="' + esc(st.ai_fast_base_url || '') + '"></div>' +
      '<div class="set-row"><label>模型名</label><input class="input" id="s-fmodel" placeholder="如 qwen-turbo / glm-air" value="' + esc(st.ai_fast_model || '') + '"></div>' +
      '<div class="set-row"><label>API Key' + (st.has_ai_fast_key ? '（当前已配置）' : '（未配置）') + '</label>' +
      '<input class="input" id="s-fkey" type="password" placeholder="留空则不修改" autocomplete="new-password"></div>' +
      '<input type="hidden" id="s-feffort" value="' + esc(st.ai_fast_effort || 'auto') + '">' +
      '<div class="set-inline"><button class="btn btn-ghost btn-sm" id="s-test-fast">测试连接</button><span class="test-inline" id="s-fast-result"></span></div>' +
      '</div>' +
      '<div id="s-sec-embed" class="hidden">' +
      '<p class="hint mt-0">语义搜索（可选，留空则与大模型共用）</p>' +
      '<div class="set-row"><label>接口地址（Base URL）</label><input class="input" id="s-ebase" placeholder="https://api.deepseek.com/v1" value="' + esc(st.embed_base_url || '') + '"></div>' +
      '<div class="set-row"><label>模型名</label><input class="input" id="s-emodel" placeholder="如 bge-m3 / text-embedding-3-small" value="' + esc(st.embed_model || '') + '"></div>' +
      '<div class="set-row"><label>API Key' + (st.has_embed_key ? '（当前已配置）' : '（未配置）') + '</label>' +
      '<input class="input" id="s-ekey" type="password" placeholder="留空则与大模型共用" autocomplete="new-password"></div>' +
      '<div class="set-inline"><button class="btn btn-ghost btn-sm" id="s-test-embed">测试连接</button><span class="test-inline" id="s-embed-result"></span></div>' +
      '<div class="set-inline"><button class="btn btn-ghost btn-sm" id="s-embed-batch" title="为历史记录生成语义向量，问小满/相关回忆/知识去重才会真正生效">为历史记录建立语义索引</button><span class="test-inline" id="s-embed-batch-result"></span></div>' +
      '</div>' +
      '<div class="set-inline mt-18"><button class="btn btn-ghost" id="s-save-ai">保存 AI 设置</button></div>' +
      '<div class="set-inline mt-12"><button class="btn btn-ghost" id="s-enrich-batch" title="自动为缺标题/摘要的历史记录补全（用小模型，需一两分钟）">批量补全历史记录的标题 / 摘要</button></div>' +
      '</div></details>' +
      '</div>' +

      '<div class="card">' +
      '<h2 class="card-title">圈子</h2>' +
      '<p class="hint mt-0">小满从记录里整理人物、地方和事件</p>' +
      '<div class="set-row"><label class="check-row"><input type="checkbox" id="s-circle-enabled"' + (st.circle_enabled !== false ? ' checked' : '') + '> 启用圈子</label></div>' +
      '<div class="set-row set-switch"><div class="set-switch-text">自动确认' +
      '<p class="hint m-0">确信的档案小满自己确认，拿错了可在圈子页一键撤销</p></div>' +
      '<label class="switch"><input type="checkbox" id="s-circle-auto-confirm"' + (st.circle_auto_confirm !== false ? ' checked' : '') + '><span class="switch-slider"></span></label></div>' +
      '<div class="set-row set-switch"><div class="set-switch-text">关联发现' +
      '<p class="hint m-0">发现记录与工作、事件、目标之间没说出口的关联</p></div>' +
      '<label class="switch"><input type="checkbox" id="s-link-discovery"' + (st.link_discovery !== false ? ' checked' : '') + '><span class="switch-slider"></span></label></div>' +
      '<div class="set-row set-switch mb-0"><div class="set-switch-text">AI 分类' +
      '<p class="hint m-0">保存后小满自动给记录打上类目标签</p></div>' +
      '<label class="switch"><input type="checkbox" id="s-category-ai"' + (st.category_ai !== false ? ' checked' : '') + '><span class="switch-slider"></span></label></div>' +
      '</div>' +

      '<div class="card">' +
      '<h2 class="card-title">日志模板</h2>' +
      '<p class="hint mt-0">今天页与详情页的快捷小节按钮（≤12 个，每个 ≤8 字）</p>' +
      '<div class="preset-chips" id="preset-chips"></div>' +
      '<div class="set-inline">' +
      '<input class="input" id="preset-input" placeholder="加个模板，如：会议纪要" maxlength="8">' +
      '<button class="btn btn-ghost" id="preset-add">添加</button>' +
      '<button class="btn btn-ghost" id="preset-save">保存</button>' +
      '</div>' +
      '</div>' +

      '<div class="card">' +
      '<h2 class="card-title">天气</h2>' +
      '<div class="set-row"><label class="check-row"><input type="checkbox" id="s-weather-enabled"' + (st.weather_enabled !== false ? ' checked' : '') + '> 自动获取天气和位置</label>' +
      '<p class="hint">关闭后不再获取天气和定位，新记录的地点预填为「公司」</p></div>' +
      '<div class="set-row"><label>默认城市</label><input class="input" id="s-city" placeholder="北京" value="' + esc(st.default_city || '') + '"></div>' +
      '<div class="set-row"><label class="check-row"><input type="checkbox" id="s-city-only"' + (st.weather_city_only ? ' checked' : '') + '> 只用默认城市（开代理会把 IP 定位带歪）</label></div>' +
      '<div class="set-row"><label>高德地图 Key' + (st.has_amap_key ? '（当前已配置）' : '（未配置，天气功能需要）') + '</label>' +
      '<input class="input" id="s-amap" type="password" placeholder="留空则不修改" autocomplete="new-password"></div>' +
      '<p class="hint">手机定位开启时会优先使用实时位置</p>' +
      '<div class="set-inline"><button class="btn btn-ghost" id="s-save-weather">保存</button></div>' +
      '</div>' +

      '<div class="card">' +
      '<h2 class="card-title">Agent 令牌</h2>' +
      '<div class="set-row"><label>访问令牌（给其他 AI 工具调用本系统时使用）</label>' +
      '<div class="token-row"><input class="input" id="s-token" readonly value="' + esc(st.agent_token || '') + '">' +
      '<button class="btn btn-ghost" id="s-copy">复制</button></div></div>' +
      '<div class="set-inline"><button class="btn btn-ghost" id="s-regen">重新生成</button></div>' +
      '<ul class="api-list">可调用接口：<li><code>/api/agent/entries</code></li><li><code>/api/agent/digest</code></li><li><code>/api/agent/search</code></li><li><code>/api/agent/reports/latest</code></li><li><code>/api/agent/metrics</code></li></ul>' +
      '</div>' +

      '<div class="card">' +
      '<h2 class="card-title">每日回顾推送</h2>' +
      '<p class="hint mt-0">每晚 9 点，把昨天的记录和一条旧回忆推到微信</p>' +
      '<div class="set-row"><label>推送 Key' + (st.has_push_key ? '（当前已配置）' : '（尚未配置）') + '</label>' +
      '<input class="input" id="s-push-key" type="password" placeholder="粘贴 Server酱 SendKey 或 PushPlus token" autocomplete="new-password"></div>' +
      '<div class="set-row"><label class="check-row"><input type="checkbox" id="s-push-enabled"' + (st.push_enabled ? ' checked' : '') + '> 开启每日推送</label></div>' +
      '<div class="set-inline"><button class="btn btn-ghost" id="s-save-push">保存</button>' +
      '<button class="btn btn-ghost btn-sm" id="s-test-push">发送测试推送</button><span class="test-inline" id="s-push-result"></span></div>' +
      '<p class="hint">没有 Key？去 Server酱 或 PushPlus 官网，微信扫码即得</p>' +
      '</div>' +

      '<div class="card">' +
      '<h2 class="card-title">数据</h2>' +
      '<div class="set-row"><button class="btn btn-ghost" id="s-backup"><svg class="ic"><use href="#i-dl"/></svg>下载完整备份</button></div>' +
      '<div class="set-row"><div class="set-inline"><button class="btn btn-ghost" id="s-verify-backup">验证最近一次备份</button></div>' +
      '<p class="hint hidden" id="s-verify-result"></p></div>' +
      '<div class="set-row"><label>导出 Markdown（选择起止日期）</label>' +
      '<div class="set-inline">' +
      '<input class="input" type="date" id="s-exp-from">' +
      '<input class="input" type="date" id="s-exp-to" value="' + localDateStr() + '">' +
      '<button class="btn btn-ghost" id="s-export-md">导出</button>' +
      '</div></div>' +
      '<h2 class="card-title mt-20">自动备份</h2>' +
      '<div class="set-row"><label>备份目录</label><input class="input" id="s-backup-dir" placeholder="留空则存到应用目录 backups" value="' + esc(st.backup_dir || '') + '"></div>' +
      '<p class="hint">上次自动备份：' + (st.last_backup_at ? esc(absTime(st.last_backup_at)) : '还没有过') + '</p>' +
      '<div class="set-row"><label class="check-row"><input type="checkbox" id="s-auto-report"' + (st.auto_report_enabled ? ' checked' : '') + '> 周日晚自动生成周报、每月1号生成月报</label></div>' +
      '<div class="set-inline"><button class="btn btn-ghost" id="s-save-auto">保存</button></div>' +
      '<h2 class="card-title mt-20">访问口令</h2>' +
      '<div class="set-row"><label>给手机上把锁' + (st.has_access_key ? '（当前已设置）' : '（未设置）') + '</label>' +
      '<input class="input" id="s-access-key" type="password" placeholder="' + (st.has_access_key ? '留空则不修改' : '设置一个口令') + '" autocomplete="new-password"></div>' +
      '<div class="set-inline"><button class="btn btn-ghost" id="s-save-lock">保存</button>' +
      (st.has_access_key
        ? '<button class="btn btn-ghost" id="s-clear-lock">关闭口令</button>' +
          '<button class="btn btn-ghost" id="s-logout">退出锁定</button>'
        : '') +
      '</div>' +
      '<h2 class="card-title mt-20">回收站</h2>' +
      '<div id="trash-box">' + loadingHtml() + '</div>' +
      '</div>' +

      '<div class="card hidden" id="logs-card">' +
      '<h2 class="card-title">运行日志 <button class="link-btn" id="logs-refresh">刷新</button></h2>' +
      '<pre class="logs-view" id="logs-view"></pre>' +
      '</div>' +

      '<div class="card">' +
      '<h2 class="card-title">关于</h2>' +
      '<p class="hint mt-0">小满 · 成长证据库 v1.5 · 数据都保存在这台电脑上</p>' +
      '<p class="hint">手机与电脑连同一 Wi-Fi 后，访问 ' + esc(phoneUrl || ('http://' + location.hostname + ':' + (location.port || '52122'))) + ' 即可使用</p>' +
      '<p class="hint">小得盈满，日进一步。</p>' +
      '<p class="hint">每一步都算数。</p>' +
      '</div>';

    // 设置页内容较多：将每个顶层卡片变成可折叠分组，保留手机连接与 AI 接口展开，
    // 其它分组默认收起，用户仍可像以前一样直接使用卡片内的所有控件。
    var setBody = $('#set-body');
    if (setBody) {
      Array.prototype.slice.call(setBody.children).forEach(function (card, index) {
        if (!card.classList || !card.classList.contains('card') || card.id === 'logs-card') return;
        var title = card.querySelector(':scope > .card-title');
        if (!title) return;
        var details = document.createElement('details');
        details.className = 'card settings-section';
        details.open = index < 2;
        var summary = document.createElement('summary');
        summary.className = 'settings-summary';
        summary.textContent = title.textContent.replace(/\s+/g, ' ').trim();
        details.appendChild(summary);
        title.remove();
        while (card.firstChild) details.appendChild(card.firstChild);
        card.replaceWith(details);
      });
      // 桌面端增加设置目录，移动端自动横向滚动；目录只负责定位，不改变原有表单逻辑
      var sections = Array.prototype.slice.call(setBody.children).filter(function (el) {
        return el.classList && el.classList.contains('settings-section');
      });
      if (sections.length) {
        var nav = document.createElement('nav');
        nav.className = 'settings-nav';
        var content = document.createElement('div');
        content.className = 'settings-content';
        sections.forEach(function (section, i) {
          var title = (section.querySelector('.settings-summary') || {}).textContent || ('设置 ' + (i + 1));
          var id = 'settings-group-' + i;
          section.id = id;
          var btn = document.createElement('button');
          btn.type = 'button'; btn.textContent = title.trim(); btn.dataset.target = id;
          btn.onclick = function () { section.open = true; section.scrollIntoView({ behavior: 'smooth', block: 'start' }); };
          nav.appendChild(btn); content.appendChild(section);
        });
        // 保留未参与折叠的运行日志等辅助节点
        Array.prototype.slice.call(setBody.children).forEach(function (el) { if (el.parentNode === setBody) { el.remove(); content.appendChild(el); } });
        var layout = document.createElement('div'); layout.className = 'settings-layout'; layout.appendChild(nav); layout.appendChild(content); setBody.appendChild(layout);
        if (window.IntersectionObserver) {
          var io = new IntersectionObserver(function (entries) {
            entries.forEach(function (entry) { if (entry.isIntersecting) { $$('.settings-nav button', nav).forEach(function (b) { b.classList.toggle('active', b.dataset.target === entry.target.id); }); } });
          }, { rootMargin: '-12% 0px -70% 0px', threshold: 0 });
          sections.forEach(function (section) { io.observe(section); });
        }
      }
    }

    // 手机连接
    if (phoneUrl) {
      var phoneCopy = function () { copyText(phoneUrl); };
      if ($('#phone-copy')) $('#phone-copy').onclick = phoneCopy;
      if ($('#phone-url')) $('#phone-url').onclick = phoneCopy;
    }
    // AI 接口：三节胶囊切换
    $('#s-ai-tabs').addEventListener('click', function (e) {
      var btn = e.target.closest('.pill');
      if (!btn) return;
      $$('#s-ai-tabs .pill').forEach(function (p) { p.classList.toggle('active', p === btn); });
      ['strong', 'fast', 'embed'].forEach(function (sec) {
        $('#s-sec-' + sec).classList.toggle('hidden', sec !== btn.dataset.sec);
      });
    });
    // 多供应商管理：后端接口存在时启用；旧版本后端静默隐藏，不影响原有配置
    (async function initProviders() {
      var list = $('#s-provider-list'), form = $('#s-provider-form'), editing = null;
      if (!list || !form) return;
      function render(items) {
        var slotNames = { strong: '大模型', fast: '小模型', embed: '嵌入模型' };
        list.innerHTML = items && items.length ? items.map(function (p) {
          return '<div class="provider-item' + (p.is_active ? ' active' : '') + '">' +
            '<div class="provider-main"><div class="provider-name">' + esc(p.name || p.model || '未命名供应商') + (p.is_active ? ' · 当前使用' : '') + '</div>' +
            '<div class="provider-meta">' + esc(slotNames[p.slot] || p.slot || '') + ' · ' + esc(p.model || '') + ' · ' + esc(p.base_url || '') + '</div></div>' +
            '<div class="provider-actions">' + (!p.is_active ? '<button class="btn btn-ghost btn-sm" data-provider-act="' + esc(p.id) + '">启用</button>' : '') +
            '<button class="btn btn-ghost btn-sm" data-provider-test="' + esc(p.id) + '">测试</button><button class="btn btn-ghost btn-sm" data-provider-edit="' + esc(p.id) + '">编辑</button><button class="btn btn-ghost btn-sm" data-provider-del="' + esc(p.id) + '">删除</button></div></div>';
        }).join('') : '<div class="provider-empty">还没有保存的供应商，可继续使用下方快速配置。</div>';
      }
      var items = [];
      try { var data = await api('/api/settings/ai-providers'); items = data.items || []; render(items); } catch (e) { list.innerHTML = '<div class="provider-empty">供应商管理将在后端更新后启用；当前仍可使用下方配置。</div>'; return; }
      function updateProviderFields() {
        var slot = $('#s-provider-slot').value;
        $('#s-provider-vision-row').classList.toggle('hidden', slot !== 'strong');
        $('#s-provider-effort-row').classList.toggle('hidden', slot === 'embed');
      }
      function clearForm() { editing = null; form.classList.add('hidden'); ['name','base','model','key'].forEach(function (x) { $('#s-provider-' + x).value = ''; }); $('#s-provider-slot').value = 'strong'; $('#s-provider-effort').value = 'auto'; $('#s-provider-vision').checked = false; $('#s-provider-result').textContent = ''; updateProviderFields(); }
      $('#s-provider-slot').onchange = updateProviderFields;
      $('#s-provider-add').onclick = function () { editing = null; form.classList.remove('hidden'); updateProviderFields(); $('#s-provider-name').focus(); };
      $('#s-provider-cancel').onclick = clearForm;
      $('#s-provider-test').onclick = async function () {
        var btn = this, result = $('#s-provider-result');
        var body = { name: $('#s-provider-name').value.trim(), slot: $('#s-provider-slot').value, base_url: $('#s-provider-base').value.trim(), model: $('#s-provider-model').value.trim(), vision: $('#s-provider-vision').checked, effort: $('#s-provider-effort').value, key: $('#s-provider-key').value.trim() };
        if (editing && !body.key) body.provider_id = editing.id;
        if (!body.base_url || !body.model || (!body.key && !body.provider_id)) { toast('测试连接前请填写接口地址、模型名和 API Key', 'error'); return; }
        btn.disabled = true; result.className = 'test-inline'; result.textContent = '正在测试…';
        try { var tr = await api('/api/settings/ai-providers/test', { method: 'POST', body: body, timeout: 60000 }); result.className = 'test-inline ' + (tr.ok ? 'ok' : 'fail'); result.textContent = tr.message || (tr.ok ? '连接成功' : '连接失败'); } catch (err) { result.className = 'test-inline fail'; result.textContent = err.message; } finally { btn.disabled = false; }
      };
      list.onclick = async function (e) {
        var id = e.target.dataset.providerAct || e.target.dataset.providerTest || e.target.dataset.providerEdit || e.target.dataset.providerDel; if (!id) return;
        var p = items.find(function (x) { return String(x.id) === String(id); });
        try {
          if (e.target.dataset.providerAct) {
            await api('/api/settings/ai-providers/' + encodeURIComponent(id) + '/activate', { method: 'POST' });
            // 激活会同步旧版 ai_* 设置，立即刷新下方兼容表单，避免用户看到旧模型名
            try {
              var activeSt = await api('/api/settings');
              if (p && p.slot === 'strong') { $('#s-base').value = activeSt.ai_base_url || ''; $('#s-model').value = activeSt.ai_model || ''; $('#s-effort').value = activeSt.ai_effort || 'auto'; }
              if (p && p.slot === 'fast') { $('#s-fbase').value = activeSt.ai_fast_base_url || ''; $('#s-fmodel').value = activeSt.ai_fast_model || ''; $('#s-feffort').value = activeSt.ai_fast_effort || 'auto'; }
              if (p && p.slot === 'embed') { $('#s-ebase').value = activeSt.embed_base_url || ''; $('#s-emodel').value = activeSt.embed_model || ''; }
            } catch (_) { /* 刷新失败不影响已完成的激活 */ }
            toast('已启用供应商', 'success');
          }
          else if (e.target.dataset.providerTest) { var tr = await api('/api/settings/ai-providers/' + encodeURIComponent(id) + '/test', { method: 'POST', timeout: 60000 }); toast(tr.message || (tr.ok ? '连接成功' : '连接失败'), tr.ok ? 'success' : 'error', 5000); return; }
          else if (e.target.dataset.providerDel) { if (!await confirmModal('删除后该供应商配置会移除，确定吗？', '删除')) return; await api('/api/settings/ai-providers/' + encodeURIComponent(id), { method: 'DELETE' }); toast('已删除', 'success'); }
          else if (p) { editing = p; form.classList.remove('hidden'); $('#s-provider-name').value = p.name || ''; $('#s-provider-slot').value = p.slot || 'strong'; $('#s-provider-base').value = p.base_url || ''; $('#s-provider-model').value = p.model || ''; $('#s-provider-key').value = ''; $('#s-provider-vision').checked = !!p.vision; $('#s-provider-effort').value = p.effort || 'auto'; $('#s-provider-result').textContent = ''; updateProviderFields(); return; }
          var fresh = await api('/api/settings/ai-providers'); items = fresh.items || []; render(items);
        } catch (err) { toast(err.message, 'error'); }
      };
      $('#s-provider-save').onclick = async function () {
        var body = { name: $('#s-provider-name').value.trim(), slot: $('#s-provider-slot').value, base_url: $('#s-provider-base').value.trim(), model: $('#s-provider-model').value.trim(), vision: $('#s-provider-vision').checked, effort: $('#s-provider-effort').value };
        var key = $('#s-provider-key').value.trim(); if (key) body.key = key;
        if (!body.name || !body.base_url || !body.model) { toast('请填写名称、接口地址和模型名', 'error'); return; }
        try { await api('/api/settings/ai-providers' + (editing ? '/' + encodeURIComponent(editing.id) : ''), { method: editing ? 'PATCH' : 'POST', body: body }); toast('供应商已保存', 'success'); clearForm(); var fresh = await api('/api/settings/ai-providers'); items = fresh.items || []; render(items); } catch (err) { toast(err.message, 'error'); }
      };
    })();
    // 日志模板编辑
    var presetEdit = (Array.isArray(st.log_presets) && st.log_presets.length)
      ? st.log_presets.slice(0, 12)
      : LOG_PRESETS.slice();
    var renderPresetChips = function () {
      var box = $('#preset-chips');
      box.innerHTML = presetEdit.length
        ? presetEdit.map(function (t, i) {
            return '<span class="qc-chip"><span>' + esc(t) + '</span><button data-preset-rm="' + i + '" title="移除">×</button></span>';
          }).join('')
        : '<span class="hint">还没有模板，加一个吧</span>';
      $$('#preset-chips [data-preset-rm]').forEach(function (b) {
        b.onclick = function () { presetEdit.splice(Number(b.dataset.presetRm), 1); renderPresetChips(); };
      });
    };
    var addPreset = function () {
      var v = $('#preset-input').value.trim();
      if (!v) return;
      if (presetEdit.indexOf(v) !== -1) { toast('已经有这个模板了', 'error'); return; }
      if (presetEdit.length >= 12) { toast('最多 12 个模板', 'error'); return; }
      presetEdit.push(v);
      $('#preset-input').value = '';
      renderPresetChips();
    };
    renderPresetChips();
    $('#preset-add').onclick = addPreset;
    $('#preset-input').addEventListener('keydown', function (e) { if (e.key === 'Enter') addPreset(); });
    $('#preset-save').onclick = async function () {
      try {
        await api('/api/settings', { method: 'PUT', body: { log_presets: presetEdit } });
        presetsCache = presetEdit.slice(); // 模板行立即生效
        toast('模板已保存', 'success');
      } catch (e) { toast(e.message, 'error'); }
    };
    // AI 接口
    $('#s-save-ai').onclick = async function () {
      var body = {
        ai_base_url: $('#s-base').value.trim(),
        ai_model: $('#s-model').value.trim(),
        ai_vision: $('#s-vision').checked,
        ai_effort: $('#s-effort').value,
        ai_fast_base_url: $('#s-fbase').value.trim(),
        ai_fast_model: $('#s-fmodel').value.trim(),
        ai_fast_effort: $('#s-feffort').value,
        embed_base_url: $('#s-ebase').value.trim(),
        embed_model: $('#s-emodel').value.trim()
      };
      var key = $('#s-key').value.trim();
      if (key) body.ai_key = key;
      var fkey = $('#s-fkey').value.trim();
      if (fkey) body.ai_fast_key = fkey;
      var ekey = $('#s-ekey').value.trim();
      if (ekey) body.embed_key = ekey;
      try {
        var saved = await api('/api/settings', { method: 'PUT', body: body });
        toast('AI 设置已保存', 'success');
        if (saved && saved.embed_rebuilt) {
          setTimeout(function () { toast('嵌入模型已更换，语义索引正在后台自动重建', 'success', 5000); }, 600);
        }
        $('#s-key').value = '';
        $('#s-fkey').value = '';
        $('#s-ekey').value = '';
      } catch (e) { toast(e.message, 'error'); }
    };
    // 测试连接（大模型 strong / 小模型 fast），结果显示在各自按钮旁
    async function testAiSlot(slot, btnSel, resSel, fields) {
      var btn = $(btnSel), box = $(resSel);
      btn.disabled = true;
      box.className = 'test-inline';
      box.textContent = '正在测试…';
      try {
        // 直接带表单里填着的内容去测，不用先保存；Key 留空则后端用已保存的
        var body = { slot: slot };
        if (fields) {
          body.base_url = $(fields[0]) ? $(fields[0]).value.trim() : '';
          body.model = $(fields[1]) ? $(fields[1]).value.trim() : '';
          var kv = $(fields[2]) ? $(fields[2]).value.trim() : '';
          if (kv) body.key = kv;
        }
        var r = await api('/api/settings/test-ai', { method: 'POST', body: body, timeout: 60000 });
        box.className = 'test-inline ' + (r.ok ? 'ok' : 'fail');
        box.textContent = r.message || (r.ok ? '连接成功' : '连接失败');
      } catch (e) {
        box.className = 'test-inline fail';
        box.textContent = e.message;
      } finally {
        btn.disabled = false;
      }
    }
    $('#s-test-strong').onclick = function () { testAiSlot('strong', '#s-test-strong', '#s-strong-result', ['#s-base', '#s-model', '#s-key']); };
    $('#s-test-fast').onclick = function () { testAiSlot('fast', '#s-test-fast', '#s-fast-result', ['#s-fbase', '#s-fmodel', '#s-fkey']); };
    $('#s-test-embed').onclick = function () { testAiSlot('embed', '#s-test-embed', '#s-embed-result', ['#s-ebase', '#s-emodel', '#s-ekey']); };
    // 为历史记录建立语义索引
    $('#s-embed-batch').onclick = async function () {
      var btn = this, res = $('#s-embed-batch-result');
      btn.disabled = true;
      btn.textContent = '正在建立索引…';
      res.className = 'test-inline';
      res.textContent = '';
      try {
        var r = await api('/api/ai/embed-batch', { method: 'POST', body: { limit: 500 }, timeout: 600000 });
        res.className = 'test-inline ' + (r.failed ? 'fail' : 'ok');
        res.textContent = '已索引 ' + (r.updated || 0) + ' 条' + (r.remaining ? '，还剩 ' + r.remaining + ' 条（后台会继续补）' : '，全部完成');
      } catch (e) {
        res.className = 'test-inline fail';
        res.textContent = e.message;
      } finally {
        btn.disabled = false;
        btn.textContent = '为历史记录建立语义索引';
      }
    };
    // 批量补全历史记录的标题/摘要
    $('#s-enrich-batch').onclick = async function () {
      var btn = this;
      btn.disabled = true;
      btn.textContent = '正在批量补全…';
      try {
        var r = await api('/api/ai/enrich-batch', { method: 'POST', body: { limit: 20 }, timeout: 600000 });
        toast('检查 ' + (r.checked || 0) + ' 条 · 补全 ' + (r.updated || 0) + ' 条' + (r.failed ? ' · 失败 ' + r.failed + ' 条' : ''), 'success', 5000);
      } catch (e) { toast(e.message, 'error', 6000); }
      btn.disabled = false;
      btn.textContent = '批量补全历史记录的标题 / 摘要';
    };
    // 天气
    $('#s-save-weather').onclick = async function () {
      var body = {
        default_city: $('#s-city').value.trim(),
        weather_enabled: $('#s-weather-enabled').checked,
        weather_city_only: $('#s-city-only').checked
      };
      var amap = $('#s-amap').value.trim();
      if (amap) body.amap_key = amap;
      try {
        await api('/api/settings', { method: 'PUT', body: body });
        toast('天气设置已保存', 'success');
        $('#s-amap').value = '';
      } catch (e) { toast(e.message, 'error'); }
    };
    // Agent
    $('#s-copy').onclick = function () { copyText($('#s-token').value); };
    $('#s-regen').onclick = async function () {
      var ok = await confirmModal('重新生成后，旧令牌将立即失效，确定吗？', '重新生成');
      if (!ok) return;
      try {
        var r = await api('/api/settings', { method: 'PUT', body: { regenerate_agent_token: true } });
        if (r && r.agent_token) $('#s-token').value = r.agent_token;
        else {
          var st2 = await api('/api/settings');
          $('#s-token').value = st2.agent_token || '';
        }
        toast('已生成新令牌', 'success');
      } catch (e) { toast(e.message, 'error'); }
    };
    // 每日回顾推送
    $('#s-save-push').onclick = async function () {
      var body = { push_enabled: $('#s-push-enabled').checked };
      var pk = $('#s-push-key').value.trim();
      if (pk) body.push_key = pk;
      try {
        await api('/api/settings', { method: 'PUT', body: body });
        toast('推送设置已保存', 'success');
        $('#s-push-key').value = '';
      } catch (e) { toast(e.message, 'error'); }
    };
    $('#s-test-push').onclick = async function () {
      var btn = this, box = $('#s-push-result');
      btn.disabled = true;
      box.className = 'test-inline';
      box.textContent = '发送中…';
      try {
        var r = await api('/api/settings/test-push', { method: 'POST', timeout: 60000 });
        box.className = 'test-inline ' + (r.ok ? 'ok' : 'fail');
        box.textContent = r.message || (r.ok ? '已送达' : '发送失败');
      } catch (e) {
        box.className = 'test-inline fail';
        box.textContent = e.message;
      }
      btn.disabled = false;
    };
    // 自动备份 / 自动化
    $('#s-save-auto').onclick = async function () {
      try {
        await api('/api/settings', {
          method: 'PUT',
          body: {
            backup_dir: $('#s-backup-dir').value.trim(),
            auto_report_enabled: $('#s-auto-report').checked
          }
        });
        toast('已保存', 'success');
      } catch (e) { toast(e.message, 'error'); }
    };
    // 访问口令
    $('#s-save-lock').onclick = async function () {
      var v = $('#s-access-key').value.trim();
      if (!v) { toast('先输入一个口令；想取消口令请点「关闭口令」', 'error'); return; }
      try {
        await api('/api/settings', { method: 'PUT', body: { access_key: v } });
        toast('口令已设置，下次访问需要输入', 'success');
        route();
      } catch (e) { toast(e.message, 'error'); }
    };
    var clearBtn = $('#s-clear-lock');
    if (clearBtn) clearBtn.onclick = async function () {
      var ok = await confirmModal('关闭后任何人连上同一 Wi-Fi 都能访问，确定关闭口令吗？', '关闭口令');
      if (!ok) return;
      try {
        await api('/api/settings', { method: 'PUT', body: { access_key: '' } });
        toast('口令已关闭', 'success');
        route();
      } catch (e) { toast(e.message, 'error'); }
    };
    var logoutBtn = $('#s-logout');
    if (logoutBtn) logoutBtn.onclick = async function () {
      try {
        await api('/api/auth/logout', { method: 'POST' });
      } catch (e) { /* 忽略，仍刷新 */ }
      location.reload();
    };
    // 圈子总开关（与圈子页头同步）
    $('#s-circle-enabled').onchange = async function () {
      var on = this.checked;
      try {
        await api('/api/settings', { method: 'PUT', body: { circle_enabled: on } });
        toast(on ? '圈子已开启' : '圈子已暂停', 'success');
      } catch (e) {
        toast(e.message, 'error');
        this.checked = !on;
      }
    };
    // 圈子细分开关：自动确认 / 关联发现 / AI 分类（切换即保存）
    [['s-circle-auto-confirm', 'circle_auto_confirm'],
     ['s-link-discovery', 'link_discovery'],
     ['s-category-ai', 'category_ai']].forEach(function (kv) {
      var el = $('#' + kv[0]);
      if (!el) return;
      el.onchange = async function () {
        var on = this.checked;
        var body = {};
        body[kv[1]] = on;
        try {
          await api('/api/settings', { method: 'PUT', body: body });
          toast('已保存', 'success');
        } catch (e) {
          toast(e.message, 'error');
          this.checked = !on;
        }
      };
    });
    // 运行日志（无此端点则保持隐藏）
    loadLogs();
    var logsBtn = $('#logs-refresh');
    if (logsBtn) logsBtn.onclick = loadLogs;
    // 数据
    $('#s-backup').onclick = async function () {
      var btn = $('#s-backup');
      btn.disabled = true;
      try {
        await downloadFile('/api/backup', { method: 'POST', filename: 'growth-vault-backup.zip' });
        toast('备份已开始下载', 'success');
      } catch (e) { toast(e.message, 'error'); }
      finally { btn.disabled = false; }
    };
    // 验证最近一次备份：一行 hint 展示结果，验证中防连点
    $('#s-verify-backup').onclick = async function () {
      var btn = this;
      var out = $('#s-verify-result');
      btn.disabled = true;
      btn.textContent = '验证中…';
      out.classList.add('hidden');
      try {
        var r = await api('/api/backup/verify', { timeout: 60000 });
        out.textContent = (r && r.ok ? '✓ ' : '✗ ') + ((r && r.detail) || (r && r.ok ? '备份完整可恢复' : '验证没通过，建议重新备份一次'));
        out.classList.remove('hidden');
      } catch (e) {
        out.textContent = '✗ ' + e.message;
        out.classList.remove('hidden');
      }
      btn.disabled = false;
      btn.textContent = '验证最近一次备份';
    };
    $('#s-export-md').onclick = async function () {
      var from = $('#s-exp-from').value, to = $('#s-exp-to').value;
      if (!from || !to) { toast('请先选择起止日期', 'error'); return; }
      if (from > to) { toast('开始日期不能晚于结束日期', 'error'); return; }
      try {
        await downloadFile('/api/export/markdown?from=' + encodeURIComponent(from) + '&to=' + encodeURIComponent(to), { filename: 'entries.zip' });
        toast('导出已开始下载', 'success');
      } catch (e) { toast(e.message, 'error'); }
    };
    loadTrash();
  }

  /* ---- 运行日志（设置页，端点缺失则隐藏） ---- */
  async function loadLogs() {
    try {
      var d = await api('/api/logs/tail?n=50');
      var card = $('#logs-card');
      if (!card || !d || !Array.isArray(d.lines)) return;
      card.classList.remove('hidden');
      $('#logs-view').textContent = d.lines.slice().reverse().join('\n');
    } catch (e) { /* 无此端点：卡片保持隐藏 */ }
  }

  async function loadTrash() {
    var box = $('#trash-box');
    if (!box) return;
    try {
      var data = await api('/api/entries?deleted=1&limit=50&offset=0');
      var items = data.items || [];
      if (!items.length) {
        box.innerHTML = '<p class="hint">回收站是空的，很干净</p>';
        return;
      }
      box.innerHTML = items.map(function (e) {
        var title = e.title || (e.content ? String(e.content).split('\n')[0].slice(0, 40) : '') || '（无标题）';
        return '<div class="trash-item" data-tid="' + esc(e.id) + '">' +
          '<div class="trash-main"><div class="trash-title">' + esc(title) + '</div>' +
          '<div class="trash-date">' + esc(absTime(e.occurred_at)) + '</div></div>' +
          '<button class="btn btn-sm btn-ghost" data-restore>恢复</button>' +
          '</div>';
      }).join('');
      $$('#trash-box [data-restore]').forEach(function (btn) {
        btn.onclick = async function () {
          var row = btn.closest('.trash-item');
          btn.disabled = true;
          try {
            await api('/api/entries/' + encodeURIComponent(row.dataset.tid) + '/restore', { method: 'POST' });
            row.remove();
            toast('已恢复', 'success');
            if (!$('#trash-box').children.length) loadTrash();
          } catch (e) {
            toast(e.message, 'error');
            btn.disabled = false;
          }
        };
      });
    } catch (e) {
      box.innerHTML = '<p class="hint">' + esc(e.message) + '</p>';
    }
  }

  /* ================= 记录详情 / 编辑页 ================= */
  var currentEntry = null;

  async function renderEntry(view, id) {
    view.innerHTML = '<div class="page">' + loadingHtml('正在打开记录…') + '</div>';
    try {
      currentEntry = await api('/api/entries/' + encodeURIComponent(id));
    } catch (e) {
      view.innerHTML = '<div class="page"><a class="back-link" href="#/entries">‹ 返回记录</a>' + errorHtml(e.message) + '</div>';
      return;
    }
    var e = currentEntry;
    view.innerHTML =
      '<div class="page">' +
      '<a class="back-link" href="#/entries">‹ 返回记录</a>' +
      '<div class="card">' +
      '<div class="form-grid">' +
      '<div><label>时间</label><input class="input" type="datetime-local" id="e-time" value="' + esc(toLocalInputValue(e.occurred_at)) + '"></div>' +
      '<div><label>工作</label><div class="qc-cats">' +
      '<button class="pill qc-work-pill' + (e.is_work ? ' active' : '') + '" id="e-work" title="点亮表示这是工作；小满以后以你为准"><svg class="ic"><use href="#i-brief"/></svg>工作</button>' +
      (Array.isArray(e.categories) && e.categories.length
        ? '<span class="e-cats">' + e.categories.slice(0, 3).map(function (c) {
            return '<span class="ei-cat">' + esc(c.name_zh || c.slug || c) + '</span>';
          }).join('') + '</span>'
        : '') +
      '</div></div>' +
      '<div class="full"><label class="e-title-label">标题' +
      '<button class="star-btn' + (e.starred ? ' active' : '') + '" id="e-star" title="标记为高光时刻"><svg class="ic"><use href="#i-star"/></svg></button>' +
      '</label><input class="input" id="e-title" placeholder="给这一刻起个名字（可选）" value="' + esc(e.title || '') + '"></div>' +
      '<div class="full"><label>正文</label>' +
      '<div class="qc-tpl" id="e-tpl">' + tplChipsHtml() + '</div>' +
      '<textarea class="input" id="e-content" rows="9" placeholder="发生了什么，学到了什么…">' + esc(e.content || '') + '</textarea>' +
      '</div>' +
      '</div>' +
      '<details class="more-details" id="e-more">' +
      '<summary><svg class="ic md-arrow"><use href="#i-down"/></svg>更多细节<span class="md-hint">摘要 · 标签 · 地点 · 天气 · AI 分析</span></summary>' +
      '<div class="more-body">' +
      '<div class="full"><label>摘要</label><input class="input" id="e-summary" placeholder="一句话概括（可选）" value="' + esc(e.summary || '') + '"></div>' +
      '<div><label>标签（逗号分隔）</label><input class="input" id="e-tags" placeholder="如：复盘, 前端, 周报" value="' + esc((e.tags || []).join(', ')) + '"></div>' +
      '<div><label>地点</label><input class="input" id="e-loc" placeholder="如：公司 / 家里" value="' + esc(e.location_name || '') + '"></div>' +
      '<div><label>当时天气（可修改）</label><input class="input" id="e-wtext" placeholder="如：晴 / 多云 / 小雨" value="' + esc(e.weather && e.weather.text ? e.weather.text : '') + '"></div>' +
      '<div><label>当时温度（°C，可修改）</label><div class="set-inline">' +
      '<input class="input" id="e-wtemp" type="number" step="0.5" placeholder="如 26" value="' + (e.weather && e.weather.temperature_c != null ? esc(String(e.weather.temperature_c)) : '') + '">' +
      '<button class="btn btn-ghost" id="e-wrefetch" title="按这条记录的时间和地点重新预填">重新获取</button>' +
      '</div></div>' +
      '<div class="full"><label class="check-row"><input type="checkbox" id="e-excl"' + (e.exclude_from_ai ? ' checked' : '') + '> 不让 AI 分析这条</label></div>' +
      '<div class="full"><button class="link-btn" id="e-regen">↻ 用当前正文重新生成标题/摘要/标签</button></div>' +
      '</div>' +
      '</details>' +
      '<div class="detail-actions">' +
      '<button class="btn" id="e-save">保存</button>' +
      '<button class="btn btn-ghost" id="e-export">导出</button>' +
      '<button class="btn btn-danger" id="e-delete">删除</button>' +
      '</div>' +
      '</div>' +

      '<div class="card">' +
      '<h2 class="card-title">附件</h2>' +
      '<div class="att-grid" id="e-atts"></div>' +
      '<div class="qc-progress hidden mt-12" id="e-progress"><div class="bar"><i id="e-bar"></i></div></div>' +
      '<div class="detail-actions"><button class="btn btn-ghost" id="e-add-att"><svg class="ic"><use href="#i-plus"/></svg>添加附件</button></div>' +
      '</div>' +

      '<div class="card">' +
      '<h2 class="card-title">链接</h2>' +
      '<div id="e-links"></div>' +
      '<div class="detail-actions"><button class="btn btn-ghost" id="e-add-link"><svg class="ic"><use href="#i-plus"/></svg>添加链接</button></div>' +
      '</div>' +
      '<div id="e-related"></div>' +
      '<div id="e-linkrel"></div>' +

      '<p class="hint center">创建于 ' + esc(absTime(e.created_at)) + (e.updated_at && e.updated_at !== e.created_at ? ' · 更新于 ' + esc(absTime(e.updated_at)) : '') + '</p>' +
      '</div>';

    // 「工作」开关：切换即 PATCH（后端顺带置 is_work_manual=1），以后这条以你为准
    $('#e-work').onclick = async function () {
      var btn = this;
      var next = !(currentEntry && currentEntry.is_work);
      btn.disabled = true;
      try {
        currentEntry = await api('/api/entries/' + encodeURIComponent(id), { method: 'PATCH', body: { is_work: next } });
        btn.classList.toggle('active', !!currentEntry.is_work);
        toast('已记下，以后这条以你为准', 'success');
      } catch (err) {
        toast(err.message, 'error');
      }
      btn.disabled = false;
    };

    // 星标：高光时刻 toggle
    $('#e-star').onclick = async function () {
      var btn = this;
      var next = !(currentEntry && currentEntry.starred);
      btn.disabled = true;
      try {
        currentEntry = await api('/api/entries/' + encodeURIComponent(id), { method: 'PATCH', body: { starred: next } });
        btn.classList.toggle('active', !!currentEntry.starred);
        toast(currentEntry.starred ? '已标记为高光时刻' : '已取消高光', 'success');
      } catch (err) { toast(err.message, 'error'); }
      btn.disabled = false;
    };
    // 模板小节：往正文追加【标题】
    $('#e-tpl').addEventListener('click', function (ev) {
      var btn = ev.target.closest('[data-tpl]');
      if (!btn) return;
      insertSection($('#e-content'), btn.dataset.tpl);
    });
    refreshTplRow('#e-tpl');

    renderEntryAtts();
    bindAttTiles($('#e-atts'));
    renderEntryLinks();
    loadRelated(id);
    loadEntryLinkProposals(id);

    // 重新获取天气/地点（只是预填，保存前仍可手动改）
    $('#e-wrefetch').onclick = async function () {
      var btn = this;
      btn.disabled = true;
      btn.textContent = '获取中…';
      try {
        var day = ($('#e-time').value || '').slice(0, 10) || localDateStr();
        var lat = currentEntry.latitude, lon = currentEntry.longitude;
        if ((lat == null || lon == null) && navigator.geolocation) {
          try {
            var pos = await new Promise(function (res, rej) {
              navigator.geolocation.getCurrentPosition(res, rej, { timeout: 8000 });
            });
            lat = pos.coords.latitude;
            lon = pos.coords.longitude;
          } catch (geoErr) { /* 定位失败则用默认城市 */ }
        }
        var url = '/api/context/preview?date=' + encodeURIComponent(day);
        if (lat != null && lon != null) url += '&lat=' + lat + '&lon=' + lon;
        var d = await api(url);
        if (d.location_name) $('#e-loc').value = d.location_name;
        if (d.weather) {
          $('#e-wtext').value = d.weather.text || '';
          $('#e-wtemp').value = d.weather.temperature_c != null ? d.weather.temperature_c : '';
          toast('已重新预填，确认无误后点保存', 'success');
        } else {
          toast('没有获取到天气，可能是网络问题，可以手动填写', 'error');
        }
      } catch (err) {
        toast(err.message, 'error');
      }
      btn.disabled = false;
      btn.textContent = '重新获取';
    };

    // 保存
    $('#e-save').onclick = async function () {
      var btn = $('#e-save');
      var timeVal = $('#e-time').value;
      var payload = {
        title: $('#e-title').value.trim(),
        summary: $('#e-summary').value.trim(),
        content: $('#e-content').value,
        tags: $('#e-tags').value.split(/[,，]/).map(function (s) { return s.trim(); }).filter(Boolean),
        location_name: $('#e-loc').value.trim(),
        exclude_from_ai: $('#e-excl').checked
      };
      // 天气/温度是预填值：用户没动就不提交；改过标记为手动填写；清空则删除天气
      var origW = currentEntry.weather || null;
      var wText = $('#e-wtext').value.trim();
      var wTempStr = $('#e-wtemp').value.trim();
      var origText = origW && origW.text ? origW.text : '';
      var origTemp = origW && origW.temperature_c != null ? String(origW.temperature_c) : '';
      if (wText !== origText || wTempStr !== origTemp) {
        if (wText || wTempStr) {
          var wTemp = parseFloat(wTempStr);
          payload.weather = {
            text: wText,
            temperature_c: wTempStr && !isNaN(wTemp) ? wTemp : null,
            humidity: origW && origW.humidity != null ? origW.humidity : null,
            provider: '手动填写'
          };
        } else {
          payload.weather = null;
        }
      }
      if (timeVal) {
        var d = new Date(timeVal);
        if (!isNaN(d.getTime())) payload.occurred_at = d.toISOString();
      }
      btn.disabled = true;
      btn.textContent = '保存中…';
      try {
        currentEntry = await api('/api/entries/' + encodeURIComponent(id), { method: 'PATCH', body: payload });
        // 位置/时间变化时后端会重新预填天气，这里把输入框同步为服务器最新值，避免下次保存用旧值覆盖
        $('#e-loc').value = currentEntry.location_name || '';
        $('#e-wtext').value = currentEntry.weather && currentEntry.weather.text ? currentEntry.weather.text : '';
        $('#e-wtemp').value = currentEntry.weather && currentEntry.weather.temperature_c != null ? currentEntry.weather.temperature_c : '';
        // 标题/摘要为空的，后台小模型会补：轮询两次，好了就同步输入框
        if (!currentEntry.title || !currentEntry.summary) pollAutoFill(id, { context: 'entry' });
        btn.textContent = '已保存 ✓';
        setTimeout(function () { btn.textContent = '保存'; btn.disabled = false; }, 1500);
      } catch (err) {
        toast(err.message, 'error');
        btn.textContent = '保存';
        btn.disabled = false;
      }
    };
    // 导出这条记录：可选独立网页或 Markdown（含附件打包）
    $('#e-export').onclick = async function () {
      var v = await choiceModal({
        title: '导出这条记录',
        choices: [
          { label: '独立网页（HTML）', sub: '暖色排版，图片内嵌，适合分享', value: 'html', primary: true },
          { label: 'Markdown', sub: '纯文本，有附件时打包 zip', value: 'markdown' },
          { label: '写成故事（AI 润色）', sub: '把这一天润色成一篇文章，存为新记录', value: 'story' }
        ]
      });
      if (!v) return;
      if (v === 'story') {
        showOverlay('小满正在把这一天写成故事…');
        try {
          var story = await api('/api/entries/' + encodeURIComponent(id) + '/story', { method: 'POST', timeout: 180000 });
          hideOverlay();
          toast('故事写好了', 'success');
          location.hash = '#/entry/' + encodeURIComponent(story.id);
        } catch (err) {
          hideOverlay();
          toast(err.message, 'error', 6000);
        }
        return;
      }
      window.open('/api/export/entry/' + encodeURIComponent(id) + '/' + v, '_blank', 'noopener');
    };
    // 唯一的再生成入口（更多细节折叠区底部）：确认后用当前正文强制重新生成
    $('#e-regen').onclick = async function () {
      var ok = await confirmModal('会用当前正文重新生成标题、摘要和标签，覆盖现有内容。继续吗？', '重新生成');
      if (!ok) return;
      var btn = this;
      btn.disabled = true;
      btn.textContent = '正在重新生成…';
      try {
        currentEntry = await api('/api/ai/enrich-entry/' + encodeURIComponent(id), { method: 'POST', body: { force: true }, timeout: 300000 });
        $('#e-title').value = currentEntry.title || '';
        $('#e-summary').value = currentEntry.summary || '';
        $('#e-tags').value = (currentEntry.tags || []).join(', ');
        toast('已按最新正文重新生成', 'success');
      } catch (err) {
        toast(err.message, 'error', 5000);
      }
      btn.disabled = false;
      btn.textContent = '↻ 用当前正文重新生成标题/摘要/标签';
    };
    // 删除
    $('#e-delete').onclick = async function () {
      var ok = await confirmModal('删除后会进入回收站，可以在「设置 → 回收站」里恢复。确定删除这条记录吗？', '删除');
      if (!ok) return;
      try {
        await api('/api/entries/' + encodeURIComponent(id), { method: 'DELETE' });
        toast('已删除，需要时可以去回收站恢复', 'success');
        if (history.length > 1) history.back();
        else location.hash = '#/today';
      } catch (err) { toast(err.message, 'error'); }
    };
    // 添加附件
    $('#e-add-att').onclick = function () {
      var inp = document.createElement('input');
      inp.type = 'file';
      inp.multiple = true;
      inp.onchange = async function () {
        var fs = Array.prototype.slice.call(inp.files || []);
        if (!fs.length) return;
        var prog = $('#e-progress');
        prog.classList.remove('hidden');
        try {
          currentEntry = await uploadFiles(id, fs, function (pct) {
            var bar = $('#e-bar');
            if (bar) bar.style.width = pct + '%';
          });
          toast('附件已上传', 'success');
          renderEntryAtts();
        } catch (err) { toast(err.message, 'error'); }
        finally { prog.classList.add('hidden'); var bar = $('#e-bar'); if (bar) bar.style.width = '0'; }
      };
      inp.click();
    };
    // 附件删除（事件委托）
    $('#e-atts').addEventListener('click', async function (ev) {
      var delBtn = ev.target.closest('.att-del');
      if (!delBtn) return;
      ev.stopPropagation();
      var ok = await confirmModal('确定删除这个附件吗？删除后无法恢复。', '删除');
      if (!ok) return;
      try {
        await api('/api/attachments/' + encodeURIComponent(delBtn.dataset.del), { method: 'DELETE' });
        currentEntry.attachments = (currentEntry.attachments || []).filter(function (a) {
          return String(a.id) !== String(delBtn.dataset.del);
        });
        renderEntryAtts();
        toast('附件已删除', 'success');
      } catch (err) { toast(err.message, 'error'); }
    });
    // 添加链接
    $('#e-add-link').onclick = async function () {
      var url = await inputModal({ title: '添加链接', placeholder: 'https://…', okText: '添加' });
      if (url == null) return;
      if (!/^https?:\/\//i.test(url)) { toast('链接需要以 http:// 或 https:// 开头', 'error'); return; }
      try {
        currentEntry = await api('/api/entries/' + encodeURIComponent(id) + '/links', { method: 'POST', body: { url: url } });
        renderEntryLinks();
        toast('链接已添加', 'success');
      } catch (err) { toast(err.message, 'error'); }
    };
  }

  /* ---- 相关的回忆（详情页底部，非空才显示） ---- */
  async function loadRelated(id) {
    var slot = $('#e-related');
    if (!slot) return;
    try {
      var data = await api('/api/entries/' + encodeURIComponent(id) + '/related?limit=3');
      var items = (data && data.items) || [];
      if (!items.length || !document.body.contains(slot)) return;
      slot.innerHTML =
        '<div class="card related-card"><h2 class="card-title">相关的回忆</h2>' +
        items.map(function (e) {
          var title = e.title || e.summary || (e.content ? String(e.content).split('\n')[0].slice(0, 40) : '') || '（无标题）';
          return '<a class="related-item" href="#/entry/' + encodeURIComponent(e.id) + '">' +
            '<span class="related-date">' + esc(absDate(e.occurred_at)) + '</span>' +
            '<span class="related-title">' + esc(title) + '</span></a>';
        }).join('') + '</div>';
    } catch (e) { /* 静默 */ }
  }

  /* ---- 可能相关（关联发现：详情底部折叠区，空则不渲染） ---- */
  var LINK_TARGET_TYPE = { entry: '记录', entity: '圈子', goal: '目标' };
  async function loadEntryLinkProposals(id) {
    var slot = $('#e-linkrel');
    if (!slot) return;
    try {
      var data = await api('/api/entries/' + encodeURIComponent(id) + '/links');
      var items = (data && data.items) || [];
      if (!items.length || !document.body.contains(slot)) return;
      slot.innerHTML =
        '<details class="card lk-card">' +
        '<summary class="lk-summary">可能相关 <span class="badge">' + esc(items.length) + '</span>' +
        '<span class="lk-summary-hint">小满觉得这些和这条有点关系</span></summary>' +
        '<div class="lk-list">' +
        items.map(function (l) {
          var title = l.target_title || l.title || l.target_name || '（无标题）';
          var head = l.target_type === 'entry'
            ? '<a class="lk-title" href="#/entry/' + encodeURIComponent(l.target_id) + '">' + esc(title) + '</a>'
            : '<span class="lk-title">' + esc(title) + '</span>';
          return '<div class="lk-item" data-lid="' + esc(l.id) + '">' +
            '<div class="lk-main">' + head +
            '<span class="lk-type">' + esc(LINK_TARGET_TYPE[l.target_type] || '记录') + '</span>' +
            (l.reason ? '<div class="lk-reason">' + esc(l.reason) + '</div>' : '') +
            '</div>' +
            (l.status === 'pending'
              ? '<div class="lk-acts"><button class="btn btn-ghost btn-sm" data-lok>对</button>' +
                '<button class="btn btn-ghost btn-sm" data-lno>不像</button></div>'
              : '<span class="lk-done">已记下</span>') +
            '</div>';
        }).join('') +
        '</div></details>';
      $$('#e-linkrel [data-lok], #e-linkrel [data-lno]').forEach(function (btn) {
        btn.onclick = function () { entryLinkAct(btn, btn.hasAttribute('data-lok') ? 'confirm' : 'dismiss'); };
      });
    } catch (e) { /* 静默：没有关联发现时不显示 */ }
  }

  async function entryLinkAct(btn, act) {
    var row = btn.closest('.lk-item');
    btn.disabled = true;
    try {
      var r = await api('/api/links/' + encodeURIComponent(row.dataset.lid) + '/' + act, { method: 'POST' });
      row.style.opacity = '0';
      setTimeout(function () {
        row.remove();
        var slot = $('#e-linkrel');
        if (slot && !slot.querySelector('.lk-item')) slot.innerHTML = '';
      }, 250);
      // 后端 confirm 返回 work_related：目标与工作有关时给一句正反馈
      if (act === 'confirm') {
        toast(r && r.work_related ? '已记下，写周报时会参考' : '已记下这层关系', 'success');
      }
    } catch (e) {
      toast(e.message, 'error');
      btn.disabled = false;
    }
  }

  function renderEntryAtts() {
    var box = $('#e-atts');
    if (!box || !currentEntry) return;
    var atts = currentEntry.attachments || [];
    box.innerHTML = atts.length
      ? atts.map(function (a) { return attTileHtml(a, { deletable: true }); }).join('')
      : '<p class="hint att-empty">还没有附件，可以添加照片、视频或文件</p>';
  }

  function renderEntryLinks() {
    var box = $('#e-links');
    if (!box || !currentEntry) return;
    var links = currentEntry.links || [];
    box.innerHTML = links.length
      ? links.map(function (l) {
          var href = safeUrl(l.url) || '#';
          return '<div class="link-item"><svg class="ic"><use href="#i-link"/></svg>' +
            '<div class="link-main">' +
            '<a class="link-title" href="' + esc(href) + '" target="_blank" rel="noopener noreferrer">' + esc(l.title || l.url) + '</a>' +
            (l.description ? '<div class="link-desc">' + esc(l.description) + '</div>' : '') +
            '</div></div>';
        }).join('')
      : '<p class="hint m-0">还没有链接</p>';
  }

  /* ================= 圈子页 ================= */
  var CIRCLE_TYPES = [ ['person', '人物'], ['place', '地点'], ['event', '事件'] ];
  var CIRCLE_TYPE_NAME = { person: '人物', place: '地点', event: '事件' };
  var CIRCLE_STATUS = [ ['active', '待确认'], ['review', '有矛盾'], ['confirmed', '已确认'], ['draft', '已搁置'], ['rejected', '已忽略'] ];
  var CIRCLE_STATUS_NAME = { active: '待确认', confirmed: '已确认', draft: '已搁置', ended: '已结束', rejected: '已忽略' };
  var circleState = null;
  var backfillTimer = null;

  function fmtMD(iso) {
    var d = parseDate(iso);
    return d ? (d.getMonth() + 1) + '月' + d.getDate() + '日' : '';
  }

  async function renderCircle(view) {
    // 首页的矛盾提醒使用 /circle/review，直接打开冲突收件箱，避免默认 active 为空造成误导。
    var initialRoute = currentRoute();
    var reviewInbox = initialRoute.param === 'review';
    circleState = { tab: reviewInbox ? 'all' : 'person', status: reviewInbox ? 'review' : 'active', enabled: true };
    var circleTabs = (reviewInbox ? [['all', '全部']] : []).concat(CIRCLE_TYPES)
      .map(function (kv) {
        return '<button class="pill' + (circleState.tab === kv[0] ? ' active' : '') + '" data-ctab="' + kv[0] + '">' + kv[1] + '</button>';
      }).join('') + '<button class="pill" data-ctab="relations">关系</button>';
    view.innerHTML =
      '<div class="page">' +
      '<header class="page-head circle-head"><h1>圈子</h1>' +
      '<label class="switch circle-toggle" title="关闭后不再提取与整理，已有档案保留"><input type="checkbox" id="circle-enabled" checked><span class="switch-slider"></span></label>' +
      '<p class="page-sub">小满从记录里认识的人、地方和事</p></header>' +
      '<div id="circle-notice"></div>' +
      '<div class="know-toolbar">' +
      '<div class="tabs mb-0" id="circle-tabs">' +
      circleTabs +
      '</div>' +
      '<span class="spacer"></span>' +
      '<button class="btn btn-ghost btn-sm" id="circle-rescan">重新扫描</button>' +
      '</div>' +
      '<div class="circle-status" id="circle-status">' +
      CIRCLE_STATUS.map(function (kv) {
        return '<button class="status-link' + (circleState.status === kv[0] ? ' active' : '') + '" data-cstatus="' + kv[0] + '">' + kv[1] + '</button>';
      }).join('') +
      '</div>' +
      '<div id="circle-progress"></div>' +
      '<div id="circle-proposals"></div>' +
      '<div id="circle-questions"></div>' +
      '<div id="circle-autolog"></div>' +
      '<div id="circle-box">' + loadingHtml() + '</div>' +
      '</div>';

    $('#circle-tabs').addEventListener('click', function (e) {
      var btn = e.target.closest('.pill');
      if (!btn) return;
      $$('#circle-tabs .pill').forEach(function (p) { p.classList.toggle('active', p === btn); });
      circleState.tab = btn.dataset.ctab;
      loadCircle();
    });
    $('#circle-status').addEventListener('click', function (e) {
      var btn = e.target.closest('.status-link');
      if (!btn) return;
      $$('#circle-status .status-link').forEach(function (p) { p.classList.toggle('active', p === btn); });
      circleState.status = btn.dataset.cstatus;
      loadCircle();
    });
    $('#circle-rescan').onclick = startBackfill;
    // 总开关：状态与设置页同步
    $('#circle-enabled').onchange = async function () {
      var on = this.checked;
      try {
        await api('/api/settings', { method: 'PUT', body: { circle_enabled: on } });
        circleState.enabled = on;
        renderCirclePause();
        toast(on ? '圈子已开启' : '圈子已暂停', 'success');
      } catch (e) {
        toast(e.message, 'error');
        this.checked = !on;
      }
    };

    loadCircleNotice();
    initCircleState();
    loadProposals();
    loadQuestions();
    loadAutoLog();
  }

  /* ---- 小满想跟你确认（合并提案，pending 才显示） ---- */
  async function loadProposals() {
    var slot = $('#circle-proposals');
    if (!slot) return;
    try {
      var data = await api('/api/circle/proposals?status=pending');
      var items = (data && data.items) || [];
      if (!items.length || !document.body.contains(slot)) return;
      slot.innerHTML = '<div class="circle-sec-title">小满想跟你确认 <span class="badge">' + esc(items.length) + '</span></div>' + items.map(function (p) {
        return '<div class="card prop-card" data-pid="' + esc(p.id) + '">' +
          '<div class="prop-title">《' + esc(p.from_name) + '》和《' + esc(p.into_name) + '》可能是同一' + esc(CIRCLE_TYPE_NAME[p.from_type] === '人物' ? '个人' : (CIRCLE_TYPE_NAME[p.from_type] === '地点' ? '个地点' : '件事')) + '</div>' +
          (p.reason ? '<div class="prop-reason">' + esc(p.reason) + '</div>' : '') +
          '<div class="prop-cmp">' +
          '<div class="prop-col"><div class="prop-col-name">《' + esc(p.from_name) + '》</div><div class="prop-col-text">' + esc(p.from_profile || '（暂无描述）') + '</div></div>' +
          '<div class="prop-col"><div class="prop-col-name">《' + esc(p.into_name) + '》</div><div class="prop-col-text">' + esc(p.into_profile || '（暂无描述）') + '</div></div>' +
          '</div>' +
          '<div class="prop-actions">' +
          '<button class="btn btn-ghost btn-sm" data-paccept="' + esc(p.id) + '">是同一' + esc(CIRCLE_TYPE_NAME[p.from_type] === '人物' ? '个人' : (CIRCLE_TYPE_NAME[p.from_type] === '地点' ? '个地点' : '件事')) + '</button>' +
          '<button class="btn btn-ghost btn-sm" data-preject="' + esc(p.id) + '">不是</button>' +
          '</div></div>';
      }).join('');
      $$('#circle-proposals [data-paccept]').forEach(function (btn) {
        btn.onclick = function () { proposalAct(btn.dataset.paccept, 'accept', btn); };
      });
      $$('#circle-proposals [data-preject]').forEach(function (btn) {
        btn.onclick = function () { proposalAct(btn.dataset.preject, 'reject', btn); };
      });
      wireCollapse(slot, '.prop-card', 3, '张');
    } catch (e) { /* 静默 */ }
  }

  async function proposalAct(id, act, btn) {
    btn.disabled = true;
    try {
      await api('/api/circle/proposals/' + encodeURIComponent(id) + '/' + act, { method: 'POST' });
      var card = btn.closest('.prop-card');
      card.style.opacity = '0';
      setTimeout(function () {
        card.remove();
        var slot = $('#circle-proposals');
        if (slot && !slot.querySelector('.prop-card')) slot.innerHTML = '';
        else refreshBlockCount(slot, '.prop-card', '.circle-sec-title .badge', '.prop-card');
      }, 250);
      toast(act === 'accept' ? '已合并' : '好，它们不是同一个', 'success');
    } catch (e) {
      toast(e.message, 'error');
      btn.disabled = false;
    }
  }

  /* ---- 小满最近自己拿的主意（近 14 天，默认收起，每条可一键撤销） ---- */
  async function loadAutoLog() {
    var slot = $('#circle-autolog');
    if (!slot) return;
    try {
      var data = await api('/api/circle/auto-log?days=14');
      var items = (data && data.items) || [];
      if (!items.length || !document.body.contains(slot)) return;
      slot.innerHTML =
        '<details class="card lk-card alog-card">' +
        '<summary class="lk-summary">小满最近自己拿的主意 <span class="badge" id="alog-count">' + esc(items.length) + '</span>' +
        '<span class="lk-summary-hint">不对的随手撤销，小满会记住</span></summary>' +
        '<div class="lk-list">' +
        items.map(function (it) {
          return '<div class="lk-item alog-row" data-aid="' + esc(it.id) + '">' +
            '<div class="lk-main"><span class="lk-title alog-text">' + esc(autoLogText(it)) + '</span>' +
            (it.created_at ? '<div class="lk-reason">' + esc(relTime(it.created_at)) + '</div>' : '') +
            '</div>' +
            '<button class="link-btn alog-undo" data-aundo="' + esc(it.id) + '">不对</button>' +
            '</div>';
        }).join('') +
        '</div></details>';
      $$('#circle-autolog [data-aundo]').forEach(function (btn) {
        btn.onclick = function () { autoLogUndo(btn.dataset.aundo, btn); };
      });
    } catch (e) { /* 静默 */ }
  }

  // 动作描述：优先用后端给的文案，缺了再按动作类型拼一句
  function autoLogText(it) {
    if (it.text) return it.text;
    if (it.description) return it.description;
    var d = it.detail || {};
    var name = it.entity_name || d.entity_name || '';
    if (it.action === 'merge') return '小满把《' + (d.from_name || '?') + '》和《' + (d.into_name || '?') + '》认成了同一个';
    if (it.action === 'fact_update') return '小满更新了《' + (name || '?') + '》的事实';
    return '小满自己确认了《' + (name || '?') + '》';
  }

  async function autoLogUndo(aid, btn) {
    btn.disabled = true;
    try {
      await api('/api/circle/auto-log/' + encodeURIComponent(aid) + '/undo', { method: 'POST' });
      var row = btn.closest('.alog-row');
      row.style.opacity = '0';
      setTimeout(function () {
        row.remove();
        var slot = $('#circle-autolog');
        if (!slot) return;
        if (!slot.querySelector('.alog-row')) { slot.innerHTML = ''; return; }
        refreshBlockCount(slot, '.alog-row', '#alog-count');
      }, 250);
      toast('已撤销，小满记住了', 'success');
      loadCircle(); // 撤销已改变实体状态：下方列表/关系图同步刷新
    } catch (e) {
      toast(e.message, 'error');
      btn.disabled = false;
    }
  }

  /* ---- 列表折叠：超过 limit 个收进「展开全部」 ---- */
  function wireCollapse(slot, cardSel, limit, unit) {
    limit = limit || 3;
    unit = unit || '张';
    var cards = $$(cardSel, slot);
    if (cards.length <= limit) return;
    cards.forEach(function (c, i) { if (i >= limit) c.classList.add('hidden', 'coll-extra'); });
    var more = document.createElement('button');
    more.className = 'link-btn more-link';
    // 配置存在按钮上：行/卡淡出后 refreshBlockCount 据此重算文案
    more.dataset.sel = cardSel;
    more.dataset.limit = limit;
    more.dataset.unit = unit;
    more.textContent = '展开全部（还有 ' + (cards.length - limit) + ' ' + unit + '）';
    more.onclick = function () {
      var open = more.dataset.open === '1';
      $$('.coll-extra', slot).forEach(function (c) { c.classList.toggle('hidden', open); });
      more.dataset.open = open ? '' : '1';
      var rest = $$(cardSel, slot).length - limit; // 实时数量，不吃旧文案
      more.textContent = open ? ('展开全部（还有 ' + rest + ' ' + unit + '）') : '收起';
    };
    slot.appendChild(more);
  }

  /* ---- 行/卡淡出后统一刷新：区块徽标数字 + 「展开全部」链接（questions/proposals/auto-log 共用） ---- */
  function refreshBlockCount(slot, itemSel, badgeSel, cardSel) {
    if (!slot || !document.body.contains(slot)) return;
    var badge = badgeSel ? slot.querySelector(badgeSel) : null;
    if (badge) badge.textContent = $$(itemSel, slot).length;
    var more = cardSel ? slot.querySelector('.more-link') : null;
    if (!more) return;
    var cards = $$(more.dataset.sel || cardSel, slot);
    var limit = Number(more.dataset.limit || 3);
    if (cards.length <= limit) {
      // 不够折叠量了：全部显示，撤下链接
      cards.forEach(function (c) { c.classList.remove('hidden', 'coll-extra'); });
      more.remove();
      return;
    }
    if (more.dataset.open !== '1') {
      // 关闭态：重新划分可见/隐藏，避免淡出后可见数变少
      cards.forEach(function (c, i) {
        var hide = i >= limit;
        c.classList.toggle('hidden', hide);
        c.classList.toggle('coll-extra', hide);
      });
    }
    more.textContent = more.dataset.open === '1'
      ? '收起'
      : '展开全部（还有 ' + (cards.length - limit) + ' ' + (more.dataset.unit || '张') + '）';
  }

  /* ---- 小满的问题（按记录分组的批量卡：AI 预选项描边高亮，可逐问点选或「都对」） ---- */
  async function loadQuestions() {
    var slot = $('#circle-questions');
    if (!slot) return;
    try {
      var data = await api('/api/circle/questions?status=pending');
      var groups = (data && data.groups) || [];
      // 兼容旧形状 {items:[...]}：每问自成一组
      if (!groups.length && data && Array.isArray(data.items)) {
        groups = data.items.map(function (q) {
          return { entry_id: q.entry_id || null, day: q.day || '', questions: [q] };
        });
      }
      if (!groups.length || !document.body.contains(slot)) return;
      var total = groups.reduce(function (s, g) { return s + ((g.questions || []).length); }, 0);
      slot.innerHTML = '<div class="circle-sec-title">小满想问你 <span class="badge">' + esc(total) + '</span></div>' + groups.map(function (g) {
        var qs = Array.isArray(g.questions) ? g.questions : [];
        var canBatch = qs.some(function (q) { return q.ai_suggested != null; });
        return '<div class="card qu-card qu-group">' +
          (g.day ? '<div class="qu-day">' + esc(fmtMD(g.day)) + ' 的记录</div>' : '') +
          qs.map(function (q) {
            var opts = Array.isArray(q.options) ? q.options : [];
            return '<div class="qu-one" data-qid="' + esc(q.id) + '"' + (q.ai_suggested != null ? ' data-suggested="' + esc(q.ai_suggested) + '"' : '') + '>' +
              '<button class="pv-close qu-close" data-qdismiss="' + esc(q.id) + '" title="忽略">×</button>' +
              '<div class="qu-title">' + esc(q.question) + '</div>' +
              '<div class="qu-opts">' +
              opts.map(function (o, i) {
                var ai = q.ai_suggested != null && Number(q.ai_suggested) === i;
                return '<button class="pill' + (ai ? ' qu-ai' : '') + '" data-qopt="' + esc(q.id) + ':' + i + '"' + (ai ? ' title="小满觉得是这个"' : '') + '>' + esc(o) + '</button>';
              }).join('') +
              '</div></div>';
          }).join('') +
          (canBatch ? '<div class="qu-batch-foot"><button class="btn btn-ghost btn-sm" data-qbatch>都对</button></div>' : '') +
          '</div>';
      }).join('');
      $$('#circle-questions [data-qopt]').forEach(function (btn) {
        btn.onclick = function () {
          var parts = btn.dataset.qopt.split(':');
          questionAnswer(parts[0], Number(parts[1]), btn);
        };
      });
      $$('#circle-questions [data-qdismiss]').forEach(function (btn) {
        btn.onclick = function () { questionDismiss(btn.dataset.qdismiss, btn); };
      });
      $$('#circle-questions [data-qbatch]').forEach(function (btn) {
        btn.onclick = function () { questionAnswerBatch(btn); };
      });
      wireCollapse(slot, '.qu-card', 3, '张');
    } catch (e) { /* 静默 */ }
  }

  // 答完/忽略一问：行淡出并刷新计数；整组问完则卡片一起淡出
  function fadeQuRow(row) {
    var card = row.closest('.qu-card');
    row.style.opacity = '0';
    setTimeout(function () {
      row.remove();
      refreshBlockCount($('#circle-questions'), '.qu-one', '.circle-sec-title .badge', '.qu-card');
      if (card && !card.querySelector('.qu-one')) {
        card.style.opacity = '0';
        setTimeout(function () {
          card.remove();
          var slot = $('#circle-questions');
          if (slot && !slot.querySelector('.qu-card')) slot.innerHTML = '';
          else refreshBlockCount(slot, '.qu-one', '.circle-sec-title .badge', '.qu-card');
        }, 250);
      }
    }, 250);
  }

  async function questionAnswer(id, choiceIndex, btn) {
    if (btn.disabled) return; // 防抖：双击不重复提交
    var row = btn.closest('.qu-one') || btn.closest('.qu-card');
    btn.disabled = true;
    try {
      await api('/api/circle/questions/' + encodeURIComponent(id) + '/answer', {
        method: 'POST', body: { choice_index: choiceIndex }
      });
      fadeQuRow(row);
      toast('已记下', 'success');
    } catch (e) {
      if (e.status === 400) {
        // 批量作答已答过的行滞留卡内时：单点会 400，视为已答，淡出即可
        fadeQuRow(row);
        toast('这条已经答过了', 'info');
      } else {
        toast(e.message, 'error');
        btn.disabled = false;
      }
    }
  }

  async function questionDismiss(id, btn) {
    if (btn.disabled) return;
    var row = btn.closest('.qu-one') || btn.closest('.qu-card');
    btn.disabled = true;
    try {
      await api('/api/circle/questions/' + encodeURIComponent(id) + '/dismiss', { method: 'POST' });
      fadeQuRow(row);
    } catch (e) {
      toast(e.message, 'error');
      btn.disabled = false;
    }
  }

  // 「都对」：本组里有 AI 预选的按预选一次性回答；无预选的行保留，可继续单选
  async function questionAnswerBatch(btn) {
    var card = btn.closest('.qu-card');
    var rows = $$('.qu-one', card);
    var answers = [];
    var answeredRows = [];
    rows.forEach(function (row) {
      if (row.dataset.suggested != null && row.dataset.suggested !== '') {
        answers.push({ id: row.dataset.qid, choice_index: Number(row.dataset.suggested) });
        answeredRows.push(row);
      }
    });
    if (!answers.length) return;
    btn.disabled = true;
    try {
      var r = await api('/api/circle/questions/answer-batch', { method: 'POST', body: { answers: answers } });
      var failed = (r && r.failed) || 0;
      if (failed > 0) {
        // 有没记上的：一行都不动，留给用户单独点选
        toast('有 ' + failed + ' 条没记上，请单独点选', 'error', 4500);
        btn.disabled = false;
        return;
      }
      answeredRows.forEach(function (row) { fadeQuRow(row); });
      // 剩下的行都没有预选时，「都对」按钮一并撤下
      var leftRows = rows.filter(function (row) { return answeredRows.indexOf(row) === -1; });
      var stillBatchable = leftRows.some(function (row) {
        return row.dataset.suggested != null && row.dataset.suggested !== '';
      });
      if (!stillBatchable) {
        var foot = card.querySelector('.qu-batch-foot');
        if (foot) foot.remove();
      }
      toast('都记下了', 'success');
    } catch (e) {
      toast(e.message, 'error');
      btn.disabled = false;
    }
  }

  async function initCircleState() {
    var enabled = true;
    try {
      var st = await api('/api/settings');
      enabled = st.circle_enabled !== false;
    } catch (e) { /* 默认开启 */ }
    circleState.enabled = enabled;
    var cb = $('#circle-enabled');
    if (cb) cb.checked = enabled;
    renderCirclePause();
    loadCircle();
  }

  // 暂停态：提示卡 + 列表只读（详情不显示操作按钮）
  function renderCirclePause() {
    var prog = $('#circle-progress');
    if (!prog) return;
    prog.innerHTML = circleState.enabled
      ? ''
      : '<div class="card circle-paused">圈子已暂停：不再提取与整理，已有档案保留（当前只读）</div>';
  }

  function loadCircle() {
    var statusRow = $('#circle-status');
    if (circleState.tab === 'relations') {
      if (statusRow) statusRow.style.display = 'none';
      loadRelations();
    } else {
      if (statusRow) statusRow.style.display = '';
      loadEntities();
    }
  }

  /* ---- 小满的通知（历史迁移完成 / 自动合并等，看完即焚） ---- */
  function renderNoticeInto(slot, n) {
    // 契约：{notice:{text,...}, notices:[{text,...}...]}；兼容旧的 {text} 形状
    // 后端读完即清，队列里的每一条都要渲染出来，不能只看最新一条
    var texts = [];
    if (n && Array.isArray(n.notices)) {
      n.notices.forEach(function (it) { if (it && it.text) texts.push(it.text); });
    }
    var single = n && (n.text || (n.notice && n.notice.text));
    if (!texts.length && single) texts.push(single);
    if (!texts.length || !document.body.contains(slot)) return;
    slot.innerHTML = texts.map(function (text) {
      return '<div class="intent-card">' +
        '<button class="pv-close intent-close" data-nclose title="知道了">×</button>' +
        '<div class="intent-title">' + esc(text) + '</div>' +
        (text.indexOf('圈子') !== -1 ? '<a class="link-btn" href="#/circle">去圈子看看 →</a>' : '') +
        '</div>';
    }).join('');
    $$('[data-nclose]', slot).forEach(function (btn) {
      btn.onclick = function () {
        var card = btn.closest('.intent-card');
        if (card) card.remove();
      };
    });
  }

  async function loadCircleNotice() {
    var slot = $('#circle-notice');
    if (!slot) return;
    try {
      var n = await api('/api/circle/notice');
      if (n) renderNoticeInto(slot, n);
    } catch (e) { /* 静默 */ }
  }

  // 今天页接全局通知（/api/notice）：迁移完成、自动合并等，未必人人会打开圈子页
  async function loadGlobalNotice() {
    var slot = $('#notice-slot');
    if (!slot) return;
    try {
      var n = await api('/api/notice');
      if (n) renderNoticeInto(slot, n);
    } catch (e) { /* 静默 */ }
  }

  async function loadEntities() {
    var box = $('#circle-box');
    if (!box) return;
    box.innerHTML = loadingHtml();
    try {
      var qs = circleState.tab === 'all' ? '' : ('?type=' + encodeURIComponent(circleState.tab));
      if (circleState.status === 'review') {
        qs += (qs ? '&' : '?') + 'status=all&needs_review=1';
      } else {
        qs += (qs ? '&' : '?') + 'status=' + encodeURIComponent(circleState.status);
      }
      var data = await api('/api/circle/entities' + qs);
      var items = (data && data.items) || [];
      if (!items.length) {
        if (circleState.tab === 'person' && circleState.status === 'active') {
          box.innerHTML =
            '<div class="card summary-guide">' +
            '<svg class="empty-ic"><use href="#i-users"/></svg>' +
            '<p class="empty-title">当前没有待确认的档案</p>' +
            '<p class="empty-sub">已确认的档案可在「已确认」里查看；重新扫描会继续整理新记录</p>' +
            '<button class="btn btn-ghost" id="circle-backfill">从全部记录建立档案</button>' +
            '</div>';
          $('#circle-backfill').onclick = startBackfill;
        } else {
          box.innerHTML = emptyHtml(
            circleState.status === 'review' ? '没有待裁定的矛盾' : (circleState.status === 'active' ? '没有待确认的档案' : '这里还空着'),
            circleState.status === 'review' ? '新的证据出现时，小满会把冲突放到这里' : (circleState.status === 'active' ? '小满整理出新档案时会放在这里，确认后就正式启用' : '换个状态或类型看看')
          );
        }
        return;
      }
      box.innerHTML = items.map(circleCardHtml).join('');
      $$('#circle-box .circle-card').forEach(function (card) {
        card.onclick = function () { openCircleDetail(card.dataset.cid); };
      });
    } catch (e) {
      box.innerHTML = errorHtml(e.message);
    }
  }

  function circleCardHtml(it) {
    var isDraft = it.status === 'draft';
    return '<div class="card circle-card' + (isDraft ? ' is-draft' : '') + '" data-cid="' + esc(it.id) + '">' +
      '<div class="cc-head"><span class="cc-name">' + esc(it.name) + '</span>' +
      '<span class="kt-badge cc-t-' + esc(it.type) + '">' + esc(CIRCLE_TYPE_NAME[it.type] || it.type) + '</span>' +
      '<span class="badge">' + esc(CIRCLE_STATUS_NAME[it.status] || it.status) + '</span>' +
      (it.needs_review ? '<span class="badge cc-warn">有矛盾待裁定</span>' : '') +
      '</div>' +
      (it.conflict_note ? '<div class="cc-conflict">' + esc(it.conflict_note) + '</div>' : '') +
      (it.relation_to_user ? '<div class="cc-rel">' + esc(it.relation_to_user) + '</div>' : '') +
      (it.profile ? '<div class="cc-profile">' + esc(it.profile) + '</div>' : '') +
      '<div class="cc-foot">提及 ' + esc(it.mention_count || 0) + ' 次' +
      (it.last_seen ? ' · 最近 ' + esc(fmtMD(it.last_seen)) : '') + '</div>' +
      '</div>';
  }

  // 关系页：GET /api/circle/graph 一次取全量节点与边；无 echarts 或无关系降级为列表
  async function loadRelations() {
    var box = $('#circle-box');
    if (!box) return;
    box.innerHTML = loadingHtml();
    try {
      var data = await api('/api/circle/graph');
      var nodes = (data && data.nodes) || [];
      var links = (data && data.links) || [];
      if (!nodes.length) {
        box.innerHTML = emptyHtml('还没有发现关系', '记录多了，人、地方和事之间会慢慢连起来');
        return;
      }
      if (!links.length) {
        // 有档案但零边（全孤岛）：文案与实情对齐，不假装「没有发现」
        box.innerHTML = '<div class="card"><p class="hint center m-0">已经有 ' + esc(nodes.length) + ' 份档案了，但它们之间还没有连上线，再记几天看看</p></div>';
        return;
      }
      // id → 节点：清单与 tooltip 都用它把内部 ID 换成名字
      var byId = {};
      nodes.forEach(function (n) { byId[String(n.id)] = n; });
      // 图上的 links 已按实体对聚合；清单使用后端保留的完整 relations，
      // 这样近义关系不会画成多条线，但来源与历史标签仍可展开查看。
      var rawRels = Array.isArray(data.relations) ? data.relations : links;
      var rels = rawRels.map(function (l) {
        var sn = byId[String(l.source)], tn = byId[String(l.target)];
        return {
          source_id: l.source, target_id: l.target,
          source_name: sn ? sn.name : '', target_name: tn ? tn.name : '',
          label: l.label, status: l.status, entry_id: l.entry_id || null,
          valid_from: l.valid_from || '', valid_to: l.valid_to || '', certainty: l.certainty || ''
        };
      });
      // 视觉图可按实体对合并边，但清单与 tooltip 必须知道是否存在反向关系。
      var pairDirections = {};
      rels.forEach(function (r) {
        var pairKey = [String(r.source_id), String(r.target_id)].sort().join('|');
        var dirKey = String(r.source_id) + '>' + String(r.target_id);
        if (!pairDirections[pairKey]) pairDirections[pairKey] = {};
        pairDirections[pairKey][dirKey] = 1;
      });
      links = links.map(function (l) {
        var pairKey = [String(l.source), String(l.target)].sort().join('|');
        return Object.assign({}, l, {
          bidirectional: Object.keys(pairDirections[pairKey] || {}).length > 1
        });
      });
      if (!LIB.echarts) {
        box.innerHTML = relationsListHtml(rels);
        return;
      }
      // 关系图只画真正有边的节点；孤岛档案仍保留在档案页，避免力导向图被大量空节点拖慢。
      var connected = {};
      links.forEach(function (l) { connected[String(l.source)] = 1; connected[String(l.target)] = 1; });
      var isolatedCount = nodes.filter(function (n) { return !connected[String(n.id)]; }).length;
      var gNodes = nodes.filter(function (n) { return connected[String(n.id)]; });
      var gLinks = links, partial = false;
      // 节点超过 300 个时，默认只画已确认档案及其关系；完整关系仍在下方清单。
      // 这样不会把待确认的猜测混进大图，也避免力导向布局在大数据量下卡顿。
      if (gNodes.length > 300) {
        partial = true;
        var keep = {};
        gNodes.filter(function (n) { return n.status === 'confirmed'; })
          .forEach(function (n) { keep[String(n.id)] = 1; });
        gNodes = gNodes.filter(function (n) { return keep[String(n.id)]; });
        gLinks = links.filter(function (l) { return keep[String(l.source)] && keep[String(l.target)]; });
      }
      if (partial && !gLinks.length) {
        box.innerHTML = '<div class="card"><p class="hint center m-0">档案超过 300 份，图先只保留已确认档案；当前没有可连线的已确认关系，下面按清单看</p></div>' + relationsGroupHtml(rels);
        var rl0 = box.querySelector('.rel-list-card');
        if (rl0) {
          wireRelationListDetails(rl0);
        }
        return;
      }
      box.innerHTML = '<div class="card"><div class="rel-graph" id="rel-graph"></div>' +
         (partial ? '<p class="hint center">档案超过 300 份，图里先看已确认档案及其关系；完整内容仍在下方清单</p>' : '') +
        (isolatedCount ? '<p class="hint center">另有 ' + esc(isolatedCount) + ' 份档案暂未连上线，已在人物 / 地点 / 事件列表中保留</p>' : '') +
        '</div>' + relationsGroupHtml(rels);
      renderRelGraph(gNodes, gLinks);
      // 关系清单超过 6 组时折叠
      var rlCard = box.querySelector('.rel-list-card');
      if (rlCard) {
        wireRelationListDetails(rlCard);
      }
    } catch (e) {
      box.innerHTML = errorHtml(e.message);
    }
  }

  function relationsListHtml(rels) {
    return '<div class="card rel-card">' + rels.map(function (r) {
      var statusName = { active: '当前', draft: '待核实', ended: '已结束', rejected: '已忽略' }[r.status] || r.status;
      var certaintyName = { explicit: '明确', inferred: '推断', ambiguous: '不确定' }[r.certainty] || r.certainty;
      var dates = (r.valid_from || r.valid_to) ? '<span class="rel-dates">' +
        (r.valid_from ? '自 ' + esc(fmtMD(r.valid_from)) : '') +
        (r.valid_to ? ' 至 ' + esc(fmtMD(r.valid_to)) : '') + '</span>' : '';
      return '<div class="rel-row"><span class="rel-name">' + esc(r.source_name) + '</span>' +
        '<span class="rel-label">— ' + esc(r.label || '相关') + ' →</span>' +
        '<span class="rel-name">' + esc(r.target_name) + '</span>' +
        (statusName ? '<span class="badge">' + esc(statusName) + '</span>' : '') +
        (certaintyName ? '<span class="rel-dates">依据 ' + esc(certaintyName) + '</span>' : '') + dates +
        (r.entry_id ? '<a class="link-btn rel-source-link" href="#/entry/' + encodeURIComponent(r.entry_id) + '" title="打开来源记录">来源记录</a>' : '') +
        '</div>';
    }).join('') + '</div>';
  }

  var REL_COLORS = { person: CHART_COLORS.accent, place: CHART_COLORS.green, event: CHART_COLORS.yellow };
  var REL_CATS = [{ name: '人物' }, { name: '地点' }, { name: '事件' }];
  var REL_CAT_IDX = { person: 0, place: 1, event: 2 };
  function renderRelGraph(nodes, links) {
    // id → 节点：tooltip 绝不显示内部 ID
    var byId = {};
    nodes.forEach(function (n) { byId[String(n.id)] = n; });
    // 节点大小 24–44：degree + mention_count 线性映射；draft（已搁置）打 8 折
    var metric = function (n) { return (n.degree || 0) + (n.mention_count || 0); };
    var maxM = nodes.reduce(function (m, n) { return Math.max(m, metric(n)); }, 1);
    var sized = nodes.map(function (n) {
      var size = 24 + Math.round(metric(n) / maxM * 20);
      return { n: n, size: n.status === 'draft' ? Math.max(18, Math.round(size * 0.8)) : size };
    });
    // 标签默认只显 degree Top5 且 size≥30，其余 hover 才出；节点多时避免中心文字互相覆盖。
    var top8 = {};
    sized.slice().filter(function (s) { return s.n.status !== 'draft'; })
      .sort(function (a, b) { return (b.n.degree || 0) - (a.n.degree || 0); })
      .slice(0, 5).forEach(function (s) { top8[String(s.n.id)] = 1; });
    var data = sized.map(function (s) {
      var n = s.n;
      var color = REL_COLORS[n.type] || CHART_COLORS.brown;
      var style = { color: color };
      if (n.status === 'active') { // 待确认：虚线描边
        style.borderType = 'dashed';
        style.borderWidth = 1.5;
        style.borderColor = color;
      } else if (n.status === 'draft') { // 已搁置：整体淡下去
        style.opacity = 0.45;
      }
      return {
        id: String(n.id),
        name: n.name,
        value: n.mention_count || 0,
        category: REL_CAT_IDX[n.type] != null ? REL_CAT_IDX[n.type] : 0,
        symbolSize: s.size,
        itemStyle: style,
        label: { show: !!(top8[String(n.id)] && s.size >= 30), position: 'bottom', distance: 6, fontSize: 11, color: CHART_COLORS.labelText },
        emphasis: { label: { show: true } }
      };
    });
    var relLinks = links.map(function (l) {
      var sn = byId[String(l.source)], tn = byId[String(l.target)];
      var draftEdge = (sn && sn.status === 'draft') || (tn && tn.status === 'draft');
      return {
        source: String(l.source),
        target: String(l.target),
        value: l.label || '相关',
         entry_id: l.entry_id || null,
         valid_from: l.valid_from || '',
         valid_to: l.valid_to || '',
        status: l.status || '',
        certainty: l.certainty || '',
        bidirectional: !!l.bidirectional,
        // 边标签默认不显示，邻接高亮（emphasis）时才出；直线边（curveness 0）避免 roam 缩放时的渲染错位
        label: { show: false, formatter: l.label || '相关', fontSize: 11, color: CHART_COLORS.muted },
        lineStyle: { width: 1, opacity: draftEdge ? 0.18 : 0.35, curveness: 0, color: CHART_COLORS.axis },
        emphasis: { label: { show: true }, lineStyle: { opacity: 0.8 } }
      };
    });
    // repulsion 按节点数 500–1500 线性插值（约 5–300 节点区间）
    var t = Math.min(Math.max((nodes.length - 5) / 295, 0), 1);
    var repulsion = Math.round(500 + t * 1000);
    var c = makeChart('rel-graph', {
      // 顶层调色板与 REL_CATS（人/地/事）同序：图例色块与节点同色
      color: [CHART_COLORS.accent, CHART_COLORS.green, CHART_COLORS.yellow],
      tooltip: {
        formatter: function (p) {
          if (p.dataType === 'edge') {
            var sn = byId[p.data.source], tn = byId[p.data.target];
            var sourceLink = p.data.entry_id
              ? '<br><a class="chart-source-link" href="#/entry/' + encodeURIComponent(p.data.entry_id) + '">来源记录 →</a>'
              : '<br><span class="chart-source-missing">暂无来源记录</span>';
            var statusName = { active: '当前', draft: '待核实', ended: '已结束', rejected: '已忽略' }[p.data.status] || p.data.status;
            var certaintyName = { explicit: '明确', inferred: '推断', ambiguous: '不确定' }[p.data.certainty] || p.data.certainty;
            var dates = p.data.valid_from || p.data.valid_to
              ? '<br><span class="chart-source-missing">' +
                (p.data.valid_from ? '自 ' + esc(fmtMD(p.data.valid_from)) : '') +
                (p.data.valid_to ? ' 至 ' + esc(fmtMD(p.data.valid_to)) : '') + '</span>' : '';
            var meta = (statusName ? '<br><span class="chart-source-missing">状态：' + esc(statusName) + '</span>' : '') +
              (certaintyName ? '<br><span class="chart-source-missing">依据：' + esc(certaintyName) + '</span>' : '');
            var direction = p.data.bidirectional ? ' ↔ ' : ' → ';
            return esc(sn ? sn.name : '') + ' — ' + esc(p.data.value || '相关') + direction + esc(tn ? tn.name : '') +
              (p.data.bidirectional ? '<br><span class="chart-source-missing">存在双向关系，方向详情见下方清单</span>' : '') +
              dates + meta + sourceLink;
          }
          var nd = byId[p.data.id] || {};
          return esc(p.name) + '（' + esc(CIRCLE_TYPE_NAME[nd.type] || '') + ' · 提及 ' + esc(nd.mention_count || 0) + ' 次' +
            (nd.status === 'draft' ? ' · 搁置中' : '') + '）';
        }
      },
      legend: {
        data: REL_CATS.map(function (cat) { return cat.name; }),
        textStyle: { color: CHART_COLORS.muted },
        itemWidth: 14, itemHeight: 9,
        top: 2, right: 6
      },
      series: [{
        type: 'graph',
        layout: 'force',
        roam: true,
        scaleLimit: { min: 0.2, max: 5 },
        draggable: true,
        categories: REL_CATS,
        force: { repulsion: repulsion, gravity: 0.1, edgeLength: [80, 200], friction: 0.2, layoutAnimation: false },
        animationDuration: 400,
        animationEasing: 'cubicOut',
        emphasis: { focus: 'adjacency' },
        label: { show: false, fontSize: 11, color: CHART_COLORS.labelText },
        labelLayout: { hideOverlap: true },
        data: data,
        links: relLinks
      }]
    });
    if (!c) return;
    c.on('click', function (p) {
      if (p.dataType === 'node' && p.data.id) openCircleDetail(p.data.id);
    });
    // 渲染收敛后 zoom-to-fit 让全图可见；用户一旦手动 roam（平移/缩放）就不再自动 fit；双击空白复位
    var fitted = false, userRoamed = false;
    c.on('finished', function () {
      if (fitted) return;
      fitted = true;
      // layoutAnimation:false 时布局仍异步多帧迭代，延迟 ~120ms 等其收敛再算包围盒
      setTimeout(function () { if (!userRoamed) relGraphFit(c); }, 120);
    });
    c.on('roam', function () { userRoamed = true; });
    c.getZr().on('dblclick', function (ev) {
      if (!ev.target) relGraphFit(c);
    });
  }

  // 按节点包围盒设 center/zoom，让全图刚好可见（用到布局内部坐标，失败静默）
  function relGraphFit(c) {
    try {
      var graph = c.getModel().getSeriesByIndex(0).getGraph();
      var minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity, cnt = 0;
      graph.eachNode(function (node) {
        var l = node.getLayout();
        if (!l) return;
        cnt++;
        if (l[0] < minX) minX = l[0];
        if (l[0] > maxX) maxX = l[0];
        if (l[1] < minY) minY = l[1];
        if (l[1] > maxY) maxY = l[1];
      });
      if (!cnt || !isFinite(minX)) return;
      var dom = c.getDom();
      var w = dom.clientWidth || 600, h = dom.clientHeight || 400;
      var bw = Math.max(maxX - minX, 40), bh = Math.max(maxY - minY, 40);
      var zoom = Math.min(w / (bw + 120), h / (bh + 120));
      zoom = Math.max(0.2, Math.min(zoom, 2));
      c.setOption({ series: [{ center: [(minX + maxX) / 2, (minY + maxY) / 2], zoom: zoom }] });
    } catch (e) { /* 布局未就绪就算了 */ }
  }

  // 关系清单：按实体分组（名字 + 关系→对象 chips），与图同数据源
  function relationsGroupHtml(rels) {
    var byEntity = {};
    var statusName = { active: '当前', draft: '待核实', ended: '已结束', rejected: '已忽略' };
    var certaintyName = { explicit: '明确', inferred: '推断', ambiguous: '不确定' };
    rels.forEach(function (r) {
      var sid = String(r.source_id), tid = String(r.target_id);
      if (!byEntity[sid]) byEntity[sid] = { name: r.source_name, chips: [] };
      if (!byEntity[tid]) byEntity[tid] = { name: r.target_name, chips: [] };
      var meta = {
        label: r.label || '相关', status: r.status || '', certainty: r.certainty || '',
        valid_from: r.valid_from || '', valid_to: r.valid_to || '', entry_id: r.entry_id || null
      };
      byEntity[sid].chips.push(Object.assign({ text: (r.label || '相关') + ' → ' + r.target_name,
        direction: 'out', counterpart: r.target_name }, meta));
      byEntity[tid].chips.push(Object.assign({ text: (r.label || '相关') + ' ← ' + r.source_name,
        direction: 'in', counterpart: r.source_name }, meta));
    });
    // 按关联数量排序，先给用户最有用的 Top 12 实体；单个实体只露出 4 条，
    // 其余关系按需展开。Top 12 之外的实体仍保留在 DOM 里，可一键展开，避免信息丢失。
    var rows = Object.keys(byEntity).map(function (id) {
      var row = byEntity[id];
      // 仅合并完全相同方向/状态/时效的重复边；反向关系必须分开显示。
      var grouped = {};
      row.chips.forEach(function (v) {
        var key = [v.direction, v.counterpart, v.label, v.status, v.certainty,
          v.valid_from, v.valid_to].join('|');
        if (!grouped[key]) grouped[key] = Object.assign({}, v, { entry_ids: [] });
        if (v.entry_id && grouped[key].entry_ids.indexOf(v.entry_id) < 0) grouped[key].entry_ids.push(v.entry_id);
      });
      row.chips = Object.keys(grouped).map(function (key) {
        var g = grouped[key];
        return { text: g.text, entry_id: g.entry_ids[0] || null, entry_ids: g.entry_ids,
          status: statusName[g.status] || g.status, certainty: certaintyName[g.certainty] || g.certainty,
          valid_from: g.valid_from, valid_to: g.valid_to };
      });
      row.chips.sort(function (a, b) { return a.text.localeCompare(b.text); });
      row.degree = row.chips.length;
      return row;
    }).sort(function (a, b) { return b.degree - a.degree || a.name.localeCompare(b.name); });
    var topN = 12;
    return '<div class="card rel-list-card"><h2 class="card-title">关系清单 <span class="muted">先看关联最多的 ' + Math.min(rows.length, topN) + ' 组</span></h2>' +
      rows.map(function (row, rowIndex) {
        var visible = row.chips.slice(0, 4);
        var rest = row.chips.slice(4);
        var chipHtml = function (c) {
          var status = c.status ? '<span class="rl-meta">' + esc(c.status) + '</span>' : '';
          var certainty = c.certainty ? '<span class="rl-meta">' + esc(c.certainty) + '</span>' : '';
          var dates = (c.valid_from || c.valid_to) ? '<span class="rl-meta">' +
            (c.valid_from ? '自 ' + fmtMD(c.valid_from) : '') + (c.valid_to ? ' 至 ' + fmtMD(c.valid_to) : '') + '</span>' : '';
          return '<span class="rl-chip">' + esc(c.text) + status + certainty + dates + (c.entry_id
            ? ' <a class="rel-source-link" href="#/entry/' + encodeURIComponent(c.entry_id) + '" title="打开来源记录">来源</a>'
            : '') + '</span>';
        };
        return '<div class="rl-row' + (rowIndex >= topN ? ' rl-group-extra hidden' : '') + '"><span class="rl-name">' + esc(row.name) + '</span>' +
          '<span class="rl-chips">' + visible.map(function (c) {
            return chipHtml(c);
          }).join('') +
          (rest.length
            ? '<span class="rl-extra hidden">' + rest.map(function (c) {
              return chipHtml(c);
            }).join('') + '</span>' +
              '<button class="link-btn rl-more" data-rl-more>展开 ' + rest.length + ' 条</button>'
            : '') +
          '</span></div>';
      }).join('') +
      (rows.length > topN
        ? '<button class="link-btn rl-groups-more" data-rl-groups-more>展开全部（还有 ' + (rows.length - topN) + ' 组）</button>'
        : '') + '</div>';
  }

  function wireRelationListDetails(card) {
    var groupMore = $('[data-rl-groups-more]', card);
    if (groupMore) {
      groupMore.onclick = function () {
        var extras = $$('.rl-group-extra', card);
        var open = groupMore.dataset.open === '1';
        extras.forEach(function (row) { row.classList.toggle('hidden', open); });
        groupMore.dataset.open = open ? '' : '1';
        groupMore.textContent = open ? '展开全部（还有 ' + extras.length + ' 组）' : '收起';
      };
    }
    $$('[data-rl-more]', card).forEach(function (btn) {
      btn.onclick = function () {
        var extra = btn.parentElement.querySelector('.rl-extra');
        if (!extra) return;
        var open = !extra.classList.contains('hidden');
        extra.classList.toggle('hidden', open);
        btn.textContent = open ? '展开 ' + extra.querySelectorAll('.rl-chip').length + ' 条' : '收起';
      };
    });
  }

  /* ---- backfill 建立档案 ---- */
  async function startBackfill() {
    try {
      await api('/api/circle/backfill', { method: 'POST' });
    } catch (e) {
      toast(e.message, 'error');
      return;
    }
    pollBackfill();
  }

  async function pollBackfill() {
    var prog = $('#circle-progress');
    try {
      var st = await api('/api/circle/backfill-status');
      if (st && !st.done) {
        if (prog && document.body.contains(prog)) {
          prog.innerHTML = '<p class="hint">正在翻你的记录 ' + esc(st.processed_days) + '/' + esc(st.total_days) + ' 天…</p>';
          backfillTimer = setTimeout(pollBackfill, 2000);
        }
      } else {
        if (prog && document.body.contains(prog)) prog.innerHTML = '';
        toast('档案整理好了', 'success');
        loadCircle();
      }
    } catch (e) { /* 停止本轮轮询 */ }
  }

  /* ---- 实体详情 modal ---- */
  async function openCircleDetail(id) {
    var root = $('#modal-root');
    root.innerHTML = '<div class="modal-wrap"><div class="modal circle-modal">' + loadingHtml() + '</div></div>';
    $('.modal-wrap', root).addEventListener('click', function (e) {
      if (e.target.classList.contains('modal-wrap')) closeModal();
    });
    var d;
    try {
      d = await api('/api/circle/entities/' + encodeURIComponent(id));
    } catch (e) {
      var m = $('#modal-root .modal');
      if (m) m.innerHTML = errorHtml(e.message);
      return;
    }
    renderCircleDetail(d);
  }

  function renderCircleDetail(d) {
    var modal = $('#modal-root .modal');
    if (!modal) return;
    var mentions = Array.isArray(d.mentions) ? d.mentions : [];
    var relations = Array.isArray(d.relations) ? d.relations : [];
    var aliases = Array.isArray(d.aliases) ? d.aliases : [];
    var facts = Array.isArray(d.facts) ? d.facts : [];
    // 事实时间线：当前的正常显示；valid_to 非空的为既往事实，灰字 + 起止日期
    var factsHtml = '';
    if (facts.length) {
      var factRow = function (f, old) {
        // 既往行：valid_from 缺失时不留前导破折号，只显示截止日
        var oldDates = f.valid_from
          ? esc(fmtMD(f.valid_from)) + ' – ' + esc(fmtMD(f.valid_to))
          : esc(fmtMD(f.valid_to));
        return '<div class="fact-row' + (old ? ' fact-old' : '') + '">' +
          '<span class="fact-text">' + esc(f.predicate || '') + ' · ' + esc(f.object_text || f.object || '') + '</span>' +
          '<span class="fact-dates">' + (old
            ? oldDates
            : (f.valid_from ? '自 ' + esc(fmtMD(f.valid_from)) : '')) + '</span></div>';
      };
      factsHtml = '<div class="cd-sec">事实时间线</div>' +
        facts.filter(function (f) { return !f.valid_to; }).map(function (f) { return factRow(f, false); }).join('') +
        facts.filter(function (f) { return !!f.valid_to; }).map(function (f) { return factRow(f, true); }).join('');
    }
    // 事件：证据区改名「发生记录」，按日期倒序
    var mentionList = mentions;
    if (d.type === 'event') {
      mentionList = mentions.slice().sort(function (a, b) {
        return String(b.occurred_at || b.created_at || '').localeCompare(String(a.occurred_at || a.created_at || ''));
      });
    }

    var acts = '';
    if (d.needs_review) {
      acts += '<button class="btn btn-ghost btn-sm" data-resolve="keep_old">维持原样</button>' +
        '<button class="btn btn-ghost btn-sm" data-resolve="accept_new">按新证据重整</button>';
    }
    if (d.status === 'draft' || d.status === 'active') {
      acts += '<button class="btn btn-ghost btn-sm" data-cact="confirm">确认</button>' +
        '<button class="btn btn-ghost btn-sm" data-cact="reject">不再提起</button>';
    } else if (d.status === 'confirmed') {
      acts += '<button class="btn btn-ghost btn-sm" data-cact="revive">重新启用</button>' +
        '<button class="btn btn-ghost btn-sm" data-cact="reject">不再提起</button>';
    } else if (d.status === 'rejected') {
      acts += '<button class="btn btn-ghost btn-sm" data-cact="revive">重新提起</button>';
    }
    acts += '<button class="btn btn-sm" data-cact="rework">告诉小满真相</button>';
    // 暂停态：只读，不出操作与编辑
    var readonly = circleState && circleState.enabled === false;

    modal.innerHTML =
      '<div class="cc-head"><span class="cc-name">' + esc(d.name) + '</span>' +
      '<span class="kt-badge cc-t-' + esc(d.type) + '">' + esc(CIRCLE_TYPE_NAME[d.type] || d.type) + '</span>' +
      '<span class="badge">' + esc(CIRCLE_STATUS_NAME[d.status] || d.status) + '</span>' +
      (d.needs_review ? '<span class="badge cc-warn">有矛盾待裁定</span>' : '') +
      '</div>' +
      (d.conflict_note ? '<div class="cc-conflict">' + esc(d.conflict_note) + '</div>' : '') +
      (d.relation_to_user ? '<div class="cc-rel">' + esc(d.relation_to_user) + '</div>' : '') +
      (aliases.length
        ? '<div class="cd-aliases">' + aliases.map(function (a) { return '<span class="tag">' + esc(a) + '</span>'; }).join('') + '</div>'
        : '') +
      (d.profile ? '<div class="cd-profile">' + esc(d.profile) + '</div>' : '') +
      (d.user_note ? '<p class="hint">我的备注：' + esc(d.user_note) + '</p>' : '') +
      factsHtml +
      '<div class="cd-meta muted">提及 ' + esc(d.mention_count || 0) + ' 次' + (d.last_seen ? ' · 最近 ' + esc(fmtMD(d.last_seen)) : '') + '</div>' +
      (mentionList.length
        ? '<div class="cd-sec">' + (d.type === 'event' ? '发生记录' : '证据') + '（共 ' + mentionList.length + ' 条）</div>' +
          mentionList.slice(0, 6).map(function (mn) {
            return '<div class="cd-mention" data-entry="' + esc(mn.entry_id) + '">' +
              '<span class="cd-mention-date">' + esc(fmtMD(mn.occurred_at || mn.created_at)) + '</span>' +
              '<span class="cd-mention-text">' + esc(mn.snippet || '') + '</span></div>';
          }).join('') +
          (mentionList.length > 6
            ? '<div class="coll-extra-wrap"><button class="link-btn" id="cd-more-mn">展开全部（还有 ' + (mentionList.length - 6) + ' 条）</button></div>' +
              '<div id="cd-mn-rest" class="hidden">' +
              mentionList.slice(6).map(function (mn) {
                return '<div class="cd-mention" data-entry="' + esc(mn.entry_id) + '">' +
                  '<span class="cd-mention-date">' + esc(fmtMD(mn.occurred_at || mn.created_at)) + '</span>' +
                  '<span class="cd-mention-text">' + esc(mn.snippet || '') + '</span></div>';
              }).join('') + '</div>'
            : '')
        : '') +
      (relations.length
        ? '<div class="cd-sec">关系</div>' + relations.map(function (r) {
            var ended = !!r.valid_to || r.status === 'ended';
            var dateText = ended
              ? ((r.valid_from ? fmtMD(r.valid_from) + ' – ' : '截至 ') + fmtMD(r.valid_to || ''))
              : (r.valid_from ? '自 ' + fmtMD(r.valid_from) : '');
            var arrow = r.direction === 'in' ? ' ←' : ' →';
            return '<div class="rel-row' + (ended ? ' rel-ended' : '') + '"><span class="rel-name">' + esc(d.name) + '</span>' +
              '<span class="rel-label">— ' + esc(r.label || '相关') + esc(arrow) + '</span>' +
              '<span class="rel-name">' + esc(r.target_name) + '</span>' +
              (r.status ? '<span class="badge">' + esc(r.status === 'active' ? '当前' : (r.status === 'draft' ? '待核实' : (CIRCLE_STATUS_NAME[r.status] || r.status))) + '</span>' : '') +
              (dateText ? '<span class="rel-dates">' + esc(dateText) + '</span>' : '') +
              (r.snippet ? '<span class="rel-evidence" title="关系依据">“' + esc(r.snippet) + '”</span>' : '') +
              (r.entry_id ? '<a class="link-btn rel-source-link" href="#/entry/' + encodeURIComponent(r.entry_id) + '" title="打开来源记录">来源记录</a>' : '') +
              '</div>';
          }).join('')
        : '') +
      (readonly
        ? '<div class="cd-links"><button class="link-btn" id="cd-close">关闭</button></div>'
        : '<div class="cd-actions">' + acts + '</div>' +
          '<div class="cd-links">' +
          '<button class="link-btn" id="cd-edit">编辑</button>' +
          '<button class="link-btn" id="cd-merge">并入另一个档案</button>' +
          '<button class="link-btn" id="cd-close">关闭</button>' +
          '</div>');

    $$('#modal-root [data-cact]').forEach(function (btn) {
      btn.onclick = function () { circleAction(d, btn.dataset.cact, btn); };
    });
    $$('#modal-root [data-resolve]').forEach(function (btn) {
      btn.onclick = function () { circleResolve(d, btn.dataset.resolve, btn); };
    });
    $$('#modal-root .cd-mention').forEach(function (row) {
      row.onclick = function () {
        closeModal();
        location.hash = '#/entry/' + encodeURIComponent(row.dataset.entry);
      };
    });
    $('#cd-close').onclick = closeModal;
    var moreMn = $('#cd-more-mn');
    if (moreMn) moreMn.onclick = function () {
      var rest = $('#cd-mn-rest');
      if (rest) rest.classList.remove('hidden');
      moreMn.closest('.coll-extra-wrap').remove();
      $$('#modal-root .cd-mention').forEach(function (row) {
        row.onclick = function () { closeModal(); location.hash = '#/entry/' + encodeURIComponent(row.dataset.entry); };
      });
    };
    var editBtn = $('#cd-edit');
    if (editBtn) editBtn.onclick = function () { renderCircleEdit(d); };
    var mergeBtn = $('#cd-merge');
    if (mergeBtn) mergeBtn.onclick = function () { openMergeModal(d); };
  }

  async function circleAction(d, act, btn) {
    if (act === 'rework') {
      var desc = await inputModal({
        title: '告诉小满真相',
        placeholder: '用大白话告诉小满事实是什么',
        okText: '重新整理'
      });
      if (desc == null) return;
      btn.disabled = true;
      btn.textContent = '整理中…';
      try {
        await api('/api/circle/entities/' + encodeURIComponent(d.id) + '/rework', {
          method: 'POST', body: { note: desc }, timeout: 180000
        });
        toast('已按你的描述重新整理', 'success');
        closeModal();
        loadEntities();  // 列表即时刷新，确认过的从待确认消失
      } catch (e) {
        toast(e.message, 'error', 6000);
        btn.disabled = false;
        btn.textContent = '告诉小满真相';
      }
      return;
    }
    btn.disabled = true;
    try {
      await api('/api/circle/entities/' + encodeURIComponent(d.id) + '/' + act, { method: 'POST' });
      toast(act === 'confirm' ? '已确认，小满会顺带整理相关档案' : act === 'reject' ? '好，以后不再提起' : '已恢复', 'success');
      closeModal();
      loadEntities();
    } catch (e) {
      toast(e.message, 'error');
      btn.disabled = false;
    }
  }

  // 冲突裁定：维持原样 / 按新证据重整
  async function circleResolve(d, action, btn) {
    btn.disabled = true;
    try {
      await api('/api/circle/entities/' + encodeURIComponent(d.id) + '/resolve', {
        method: 'POST', body: { action: action }
      });
      toast(action === 'keep_old' ? '好，维持原样' : '已按新证据重整', 'success');
      closeModal();
      loadEntities();
    } catch (e) {
      toast(e.message, 'error');
      btn.disabled = false;
    }
  }

  /* ---- 编辑（保存即 PATCH） ---- */
  function renderCircleEdit(d) {
    var modal = $('#modal-root .modal');
    if (!modal) return;
    modal.innerHTML =
      '<h3 class="modal-title">编辑档案</h3>' +
      '<div class="set-row"><label>名字</label><input class="input" id="ce-name" value="' + esc(d.name || '') + '"></div>' +
      '<div class="set-row"><label>类型</label><select class="input" id="ce-type">' +
      '<option value="person"' + (d.type === 'person' ? ' selected' : '') + '>人物</option>' +
      '<option value="place"' + (d.type === 'place' ? ' selected' : '') + '>地点</option>' +
      '<option value="event"' + (d.type === 'event' ? ' selected' : '') + '>事件</option></select></div>' +
      '<div class="set-row"><label>与我的关系</label><input class="input" id="ce-rel" placeholder="如：大学室友 / 常去的咖啡店" value="' + esc(d.relation_to_user || '') + '"></div>' +
      '<div class="set-row"><label>描述</label><textarea class="input" id="ce-profile" rows="5">' + esc(d.profile || '') + '</textarea></div>' +
      '<div class="set-row"><label>我的备注</label><input class="input" id="ce-note" placeholder="只有你能看到的备注" value="' + esc(d.user_note || '') + '"></div>' +
      '<div class="modal-btns">' +
      '<button class="btn btn-ghost" id="ce-cancel">取消</button>' +
      '<button class="btn" id="ce-save">保存</button>' +
      '</div>';
    $('#ce-cancel').onclick = function () { renderCircleDetail(d); };
    $('#ce-save').onclick = async function () {
      var btn = this;
      btn.disabled = true;
      try {
        var updated = await api('/api/circle/entities/' + encodeURIComponent(d.id), {
          method: 'PATCH',
          body: {
            name: $('#ce-name').value.trim(),
            type: $('#ce-type').value,
            relation_to_user: $('#ce-rel').value.trim(),
            profile: $('#ce-profile').value,
            user_note: $('#ce-note').value.trim()
          }
        });
        toast('已保存', 'success');
        if (updated && updated.id) renderCircleDetail(updated);
        else { closeModal(); loadCircle(); }
      } catch (e) {
        toast(e.message, 'error');
        btn.disabled = false;
      }
    };
  }

  /* ---- 合并：并入另一个档案 ---- */
  async function openMergeModal(current) {
    var all = [];
    try {
      // 候选 = active / confirmed / draft（已忽略的不给选），按类型×状态拉全
      var reqs = [];
      ['person', 'place', 'event'].forEach(function (t) {
        ['active', 'confirmed', 'draft'].forEach(function (s) {
          reqs.push(api('/api/circle/entities?type=' + t + '&status=' + s));
        });
      });
      var lists = await Promise.all(reqs);
      var seenIds = {};
      all = lists.reduce(function (acc, d) { return acc.concat((d && d.items) || []); }, [])
        .filter(function (it) {
          if (String(it.id) === String(current.id)) return false;
          if (seenIds[String(it.id)]) return false;
          seenIds[String(it.id)] = 1;
          return true;
        });
    } catch (e) {
      toast(e.message, 'error');
      return;
    }
    var root = $('#modal-root');
    root.innerHTML =
      '<div class="modal-wrap"><div class="modal">' +
      '<h3 class="modal-title">把「' + esc(current.name) + '」并入…</h3>' +
      '<input class="input" id="mg-q" placeholder="输入名字搜索">' +
      '<div id="mg-list" class="mg-list"></div>' +
      '<div class="modal-btns"><button class="btn btn-ghost" id="mg-cancel">取消</button></div>' +
      '</div></div>';
    $('.modal-wrap', root).addEventListener('click', function (e) {
      if (e.target.classList.contains('modal-wrap')) closeModal();
    });
    $('#mg-cancel').onclick = function () { renderCircleDetail(current); };
    var renderList = function (q) {
      var kw = (q || '').trim();
      var hits = all.filter(function (it) {
        if (!kw) return true;
        var hay = (it.name || '') + ' ' + ((it.aliases || []).join(' '));
        return hay.indexOf(kw) !== -1;
      }).slice(0, 8);
      $('#mg-list').innerHTML = hits.length
        ? hits.map(function (it) {
            return '<button class="mg-row" data-mid="' + esc(it.id) + '"><span class="rel-name">' + esc(it.name) + '</span>' +
              '<span class="kt-badge cc-t-' + esc(it.type) + '">' + esc(CIRCLE_TYPE_NAME[it.type] || it.type) + '</span>' +
              '<span class="badge">' + esc(CIRCLE_STATUS_NAME[it.status] || it.status) + '</span></button>';
          }).join('')
        : '<p class="hint mt-10">没有匹配的档案</p>';
      $$('#mg-list .mg-row').forEach(function (row) {
        row.onclick = async function () {
          var target = all.find(function (it) { return String(it.id) === String(row.dataset.mid); });
          if (!target) return;
          var ok = await confirmModal('把「' + current.name + '」并入「' + target.name + '」？提及与关系都会归并，不可撤销。', '并入');
          if (!ok) return;
          try {
            await api('/api/circle/entities/merge', {
              method: 'POST',
              body: { from_id: current.id, into_id: target.id }
            });
            closeModal();
            toast('已并入「' + target.name + '」', 'success');
            loadCircle();
          } catch (e) { toast(e.message, 'error'); }
        };
      });
    };
    $('#mg-q').addEventListener('input', function () { renderList(this.value); });
    renderList('');
    $('#mg-q').focus();
  }

  /* ---------------- 页面注册与启动 ---------------- */
  var PAGES = {
    today: renderToday,
    entries: renderEntries,
    calendar: renderCalendar,
    media: renderMedia,
    river: renderRiver,
    circle: renderCircle,
    reports: renderReports,
    insights: renderInsights,
    growth: renderGrowth,
    knowledge: renderKnowledge,
    settings: renderSettings
  };

  if (!location.hash) {
    history.replaceState(null, '', '#/today');
  }
  route();

})();
