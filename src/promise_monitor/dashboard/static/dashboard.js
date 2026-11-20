/* Promise Monitor dashboard: progressive enhancement only.
   Every page works without this file; it adds theme switching, a mobile nav drawer,
   sortable/filterable tables, a live editor (word count + preview), confirmations,
   toasts and keyboard shortcuts. Loaded from 'self' only (strict CSP, no inline JS). */
(function () {
  "use strict";
  var root = document.documentElement;

  /* ---- theme: applied before first paint (this script is in <head>, not deferred) ---- */
  var THEME_KEY = "pm-theme";
  function storedTheme() {
    try { return localStorage.getItem(THEME_KEY); } catch (e) { return null; }
  }
  function applyTheme(t) {
    if (t === "light" || t === "dark") root.setAttribute("data-theme", t);
    else root.removeAttribute("data-theme");
  }
  applyTheme(storedTheme());
  root.classList.add("js");

  function ready(fn) {
    if (document.readyState !== "loading") fn();
    else document.addEventListener("DOMContentLoaded", fn);
  }

  function $(sel, ctx) { return (ctx || document).querySelector(sel); }
  function $$(sel, ctx) { return Array.prototype.slice.call((ctx || document).querySelectorAll(sel)); }

  function effectiveTheme() {
    var t = root.getAttribute("data-theme");
    if (t) return t;
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }

  function initTheme() {
    $$("[data-theme-toggle]").forEach(function (btn) {
      function label() {
        var dark = effectiveTheme() === "dark";
        btn.setAttribute("aria-pressed", dark ? "true" : "false");
        btn.setAttribute("title", dark ? "Switch to light theme" : "Switch to dark theme");
      }
      label();
      btn.hidden = false;
      btn.addEventListener("click", function () {
        var next = effectiveTheme() === "dark" ? "light" : "dark";
        applyTheme(next);
        try { localStorage.setItem(THEME_KEY, next); } catch (e) { /* private mode: session only */ }
        label();
      });
    });
  }

  /* ---- mobile navigation drawer ---- */
  function initNav() {
    var btn = $("[data-nav-toggle]");
    var shell = $(".shell");
    if (!btn || !shell) return;
    btn.hidden = false;
    function set(open) {
      shell.classList.toggle("nav-open", open);
      btn.setAttribute("aria-expanded", open ? "true" : "false");
    }
    btn.addEventListener("click", function () { set(!shell.classList.contains("nav-open")); });
    $$("[data-nav-close]").forEach(function (el) { el.addEventListener("click", function () { set(false); }); });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && shell.classList.contains("nav-open")) { set(false); btn.focus(); }
    });
  }

  /* ---- toasts (server flashes) ---- */
  function initToasts() {
    $$(".toast").forEach(function (t) {
      var close = document.createElement("button");
      close.type = "button";
      close.className = "toast-x";
      close.setAttribute("aria-label", "Dismiss");
      close.textContent = "×";
      close.addEventListener("click", function () { dismiss(t); });
      t.appendChild(close);
      if (t.classList.contains("ok") && !t.querySelector("code")) {
        setTimeout(function () { dismiss(t); }, 7000);
      }
    });
  }
  function dismiss(t) {
    t.classList.add("leaving");
    setTimeout(function () { if (t.parentNode) t.parentNode.removeChild(t); }, 220);
  }

  /* ---- confirmations + busy state on submit ---- */
  function initForms() {
    document.addEventListener("click", function (e) {
      var b = e.target.closest ? e.target.closest("button[data-confirm]") : null;
      if (b && !window.confirm(b.getAttribute("data-confirm"))) { e.preventDefault(); e.stopPropagation(); }
    }, true);
    document.addEventListener("submit", function (e) {
      var form = e.target;
      if (form.method && form.method.toLowerCase() !== "post") return;
      var btn = e.submitter || form.querySelector("button[type=submit]");
      if (btn) {
        // Keep the clicked button's name/value (e.g. action=approve) after disabling it.
        if (btn.name) {
          var h = document.createElement("input");
          h.type = "hidden"; h.name = btn.name; h.value = btn.value;
          form.appendChild(h);
        }
        setTimeout(function () { btn.disabled = true; btn.classList.add("busy"); }, 0);
      }
      form.setAttribute("data-submitted", "1");
    });
  }

  /* ---- tables: sort, filter, mobile labels ---- */
  function cellValue(td) {
    var v = td.getAttribute("data-sort");
    if (v !== null) return v;
    return (td.textContent || "").trim();
  }
  function compare(a, b) {
    var na = parseFloat(a.replace(/[$,%]/g, "")), nb = parseFloat(b.replace(/[$,%]/g, ""));
    var numeric = /^[-$]?[\d.,]+%?$/.test(a) && /^[-$]?[\d.,]+%?$/.test(b);
    if (numeric && !isNaN(na) && !isNaN(nb)) return na - nb;
    var da = Date.parse(a), db = Date.parse(b);
    if (/\d{4}/.test(a) && /\d{4}/.test(b) && !isNaN(da) && !isNaN(db)) return da - db;
    return a.localeCompare(b, undefined, { numeric: true, sensitivity: "base" });
  }
  function enhanceTable(table) {
    var thead = table.tHead, body = table.tBodies[0];
    if (!thead || !body) return;
    var heads = $$("th", thead);
    var rows = $$("tr", body);
    // Mobile card layout reads labels from data-label.
    rows.forEach(function (tr) {
      $$("td", tr).forEach(function (td, i) {
        if (heads[i] && !td.hasAttribute("data-label")) td.setAttribute("data-label", heads[i].textContent.trim());
      });
    });
    table.classList.add("stackable");
    var dataRows = rows.filter(function (tr) { return !tr.querySelector("td[colspan]"); });
    if (dataRows.length < 2) return;

    heads.forEach(function (th, i) {
      if (!th.textContent.trim() || th.hasAttribute("data-nosort")) return;
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "sort";
      btn.innerHTML = "";
      btn.appendChild(document.createTextNode(th.textContent.trim()));
      var arrow = document.createElement("span");
      arrow.className = "arrow"; arrow.setAttribute("aria-hidden", "true");
      btn.appendChild(arrow);
      th.textContent = "";
      th.appendChild(btn);
      btn.addEventListener("click", function () {
        var dir = th.getAttribute("aria-sort") === "ascending" ? "descending" : "ascending";
        heads.forEach(function (h) { h.removeAttribute("aria-sort"); });
        th.setAttribute("aria-sort", dir);
        var sorted = dataRows.slice().sort(function (r1, r2) {
          var c1 = r1.cells[i], c2 = r2.cells[i];
          var res = compare(c1 ? cellValue(c1) : "", c2 ? cellValue(c2) : "");
          return dir === "ascending" ? res : -res;
        });
        sorted.forEach(function (tr) { body.appendChild(tr); });
      });
    });

    if (dataRows.length >= 8 && !table.hasAttribute("data-nofilter")) {
      var wrap = table.closest(".scroll") || table;
      var bar = document.createElement("div");
      bar.className = "table-tools";
      var input = document.createElement("input");
      input.type = "search";
      input.placeholder = "Filter " + dataRows.length + " rows…";
      input.setAttribute("aria-label", "Filter table rows");
      input.setAttribute("data-quickfilter", "");
      var count = document.createElement("span");
      count.className = "muted small"; count.setAttribute("aria-live", "polite");
      bar.appendChild(input); bar.appendChild(count);
      wrap.parentNode.insertBefore(bar, wrap);
      var empty = document.createElement("tr");
      var etd = document.createElement("td");
      etd.colSpan = heads.length; etd.className = "muted empty-cell"; etd.textContent = "No rows match.";
      empty.appendChild(etd); empty.hidden = true; body.appendChild(empty);
      input.addEventListener("input", function () {
        var q = input.value.trim().toLowerCase(), shown = 0;
        dataRows.forEach(function (tr) {
          var hit = !q || tr.textContent.toLowerCase().indexOf(q) !== -1;
          tr.hidden = !hit; if (hit) shown++;
        });
        empty.hidden = shown !== 0;
        count.textContent = q ? shown + " of " + dataRows.length : "";
      });
    }
  }

  /* ---- article editor: live word count (same rule as domain/qa.py) + preview ---- */
  var LINK_RE = /\[([^\]]+)\]\(\s*<?([^)\s>]+)>?\s*\)/g;
  function words(md) {
    return md.replace(LINK_RE, "$1").split(/\s+/).filter(function (t) { return /[A-Za-z0-9]/.test(t); }).length;
  }
  function esc(s) {
    return s.replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function renderMd(md) {
    return md.split(/\n\s*\n/).filter(function (p) { return p.trim(); }).map(function (p) {
      var out = "", last = 0, m;
      LINK_RE.lastIndex = 0;
      while ((m = LINK_RE.exec(p)) !== null) {
        out += esc(p.slice(last, m.index));
        var url = m[2];
        if (/^https?:\/\//i.test(url)) out += '<a href="' + esc(url) + '" rel="noopener noreferrer" target="_blank">' + esc(m[1]) + "</a>";
        else out += '<span class="bad-link">' + esc(m[1]) + "</span>";
        last = m.index + m[0].length;
      }
      out += esc(p.slice(last));
      out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>").replace(/\*([^*]+)\*/g, "<em>$1</em>");
      return "<p>" + out + "</p>";
    }).join("");
  }
  function meter(el, n, min, max) {
    el.textContent = n + (max ? " / " + max : "") + " words";
    el.classList.remove("ok", "warn", "bad");
    if (max && n > max) el.classList.add("bad");
    else if (min && n < min) el.classList.add("warn");
    else el.classList.add("ok");
    var bar = el.parentNode && el.parentNode.querySelector(".wbar > i");
    if (bar && max) {
      bar.style.width = Math.min(100, (n / max) * 100) + "%";
      bar.className = el.classList.contains("bad") ? "bad" : el.classList.contains("warn") ? "warn" : "ok";
    }
  }
  function initEditor() {
    var form = $("form[data-editor]");
    if (!form) return;
    var body = $("textarea[name=body]", form), head = $("input[name=headline]", form);
    var bc = $("[data-count=body]"), hc = $("[data-count=headline]");
    var preview = $("[data-preview]"), phead = $("[data-preview-headline]");
    var min = +form.getAttribute("data-min") || 0, max = +form.getAttribute("data-max") || 0;
    var hmax = +form.getAttribute("data-hmax") || 0;
    var dirty = false;
    function update() {
      if (body && bc) meter(bc, words(body.value), min, max);
      if (head && hc) meter(hc, words(head.value), 0, hmax);
      if (preview && body) preview.innerHTML = renderMd(body.value);
      if (phead && head) phead.textContent = head.value;
    }
    [body, head].forEach(function (el) {
      if (!el) return;
      el.addEventListener("input", function () { dirty = true; update(); });
    });
    update();
    window.addEventListener("beforeunload", function (e) {
      if (dirty && !form.getAttribute("data-submitted")) { e.preventDefault(); e.returnValue = ""; }
    });
    // Ctrl/Cmd+S = Save & re-check.
    document.addEventListener("keydown", function (e) {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
        var save = $("button[value=save]", form);
        if (save) { e.preventDefault(); form.requestSubmit ? form.requestSubmit(save) : save.click(); }
      }
    });
    // Auto-grow the body textarea.
    if (body) {
      var grow = function () { body.style.height = "auto"; body.style.height = Math.max(220, body.scrollHeight + 4) + "px"; };
      body.addEventListener("input", grow); grow();
    }
  }

  /* ---- copy buttons (one-time passwords etc.) ---- */
  function initCopy() {
    $$(".toast code, [data-copy]").forEach(function (el) {
      if (!navigator.clipboard) return;
      var b = document.createElement("button");
      b.type = "button"; b.className = "ghost xs"; b.textContent = "Copy";
      b.addEventListener("click", function () {
        navigator.clipboard.writeText(el.textContent.trim()).then(function () { b.textContent = "Copied"; });
      });
      el.insertAdjacentElement("afterend", b);
    });
  }

  /* ---- keyboard: "/" focuses the first filter, "?" shows shortcuts ---- */
  function initKeys() {
    document.addEventListener("keydown", function (e) {
      var tag = (e.target.tagName || "").toLowerCase();
      if (tag === "input" || tag === "textarea" || tag === "select" || e.ctrlKey || e.metaKey || e.altKey) return;
      if (e.key === "/") {
        var f = $("[data-quickfilter]") || $(".filters select, .filters input");
        if (f) { e.preventDefault(); f.focus(); }
      } else if (e.key === "?") {
        var d = $("#shortcuts");
        if (d && d.showModal) { e.preventDefault(); d.showModal(); }
      }
    });
    $$("[data-close-dialog]").forEach(function (b) {
      b.addEventListener("click", function () { var d = b.closest("dialog"); if (d) d.close(); });
    });
  }

  /* ---- running jobs refresh themselves ---- */
  function typing() {
    var a = document.activeElement, tag = a ? (a.tagName || "").toLowerCase() : "";
    if (tag === "input" || tag === "textarea" || tag === "select") return true;
    return $$("textarea, input[type=text], input[type=search]").some(function (f) { return f.value !== f.defaultValue; });
  }
  function initPoll() {
    var el = $("[data-poll]");
    if (!el) return;
    var secs = +el.getAttribute("data-poll") || 5;
    // Never reload while someone is typing or has unsaved text on the page.
    setTimeout(function () { if (!document.hidden && !typing()) window.location.reload(); else initPoll(); }, secs * 1000);
  }

  /* ---- <details> panels opened from a link or the URL hash (e.g. #write) ---- */
  function initDisclosure() {
    function openFromHash() {
      var id = window.location.hash.slice(1);
      var d = id && document.getElementById(id);
      if (d && d.tagName.toLowerCase() === "details") {
        d.open = true;
        var f = d.querySelector("textarea, input:not([type=hidden])");
        if (f) setTimeout(function () { f.focus(); }, 50);
      }
    }
    $$("[data-open]").forEach(function (a) {
      a.addEventListener("click", function () { setTimeout(openFromHash, 0); });
    });
    window.addEventListener("hashchange", openFromHash);
    openFromHash();
    $$("[data-charcount]").forEach(function (el) {
      var field = document.getElementById(el.getAttribute("data-charcount"));
      var max = +el.getAttribute("data-max") || 0;
      if (!field) return;
      var upd = function () { el.textContent = field.value.length + (max ? " / " + max : "") + " characters"; };
      field.addEventListener("input", upd); upd();
    });
  }

  ready(function () {
    initTheme(); initNav(); initToasts(); initForms(); initCopy(); initKeys(); initPoll(); initDisclosure();
    $$("table").forEach(enhanceTable);
    initEditor();
  });
})();
