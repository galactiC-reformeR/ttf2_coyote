/* ===========================================================
   ttf2_coyote — 站点交互
   1) 导航吸顶 / 移动端展开
   2) 滚动出现
   3) 终端演练动画（剧本 S1，加速版）
   4) 复制路径
   5) 折叠面板单开
   =========================================================== */
(function () {
  "use strict";

  var reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* ---------------- 1. 导航 ---------------- */
  var nav = document.getElementById("nav");
  var navLinks = document.getElementById("navLinks");
  var navToggle = document.getElementById("navToggle");

  function onScroll() {
    if (!nav) return;
    nav.classList.toggle("is-stuck", window.scrollY > 12);
  }
  onScroll();
  window.addEventListener("scroll", onScroll, { passive: true });

  if (navToggle && navLinks) {
    navToggle.addEventListener("click", function () {
      var open = navLinks.classList.toggle("is-open");
      navToggle.setAttribute("aria-expanded", open ? "true" : "false");
    });
    navLinks.addEventListener("click", function (e) {
      if (e.target.tagName === "A") {
        navLinks.classList.remove("is-open");
        navToggle.setAttribute("aria-expanded", "false");
      }
    });
  }

  /* ---------------- 2. 滚动出现 ----------------
     用几何判定而不是 IntersectionObserver：哈希跳转、直接截长图、打印时
     没有滚动事件，观察者可能一次都不回调，内容就会永远停在 opacity:0。 */
  var revealQueue = Array.prototype.slice.call(document.querySelectorAll(".reveal"));

  function revealPass() {
    if (!revealQueue.length) return;
    var limit = window.innerHeight * 1.02;
    var rest = [];
    revealQueue.forEach(function (el) {
      var top = el.getBoundingClientRect().top;
      if (top > limit) {
        rest.push(el);
        return;
      }
      var siblings = el.parentElement ? Array.prototype.slice.call(el.parentElement.children) : [];
      var idx = Math.max(0, siblings.indexOf(el));
      el.style.transitionDelay = Math.min(idx * 55, 330) + "ms";
      el.classList.add("is-in");
    });
    revealQueue = rest;
  }

  if (reduceMotion) {
    revealQueue.forEach(function (el) { el.classList.add("is-in"); });
    revealQueue = [];
  } else {
    var frame = 0;
    var schedule = function () {
      if (frame) return;
      frame = window.requestAnimationFrame(function () {
        frame = 0;
        revealPass();
      });
    };
    revealPass();
    window.addEventListener("scroll", schedule, { passive: true });
    window.addEventListener("resize", schedule);
    window.addEventListener("hashchange", schedule);
    window.addEventListener("load", schedule);
    [180, 520, 1200].forEach(function (ms) { setTimeout(revealPass, ms); });
  }

  /* ---------------- 3. 终端演练 ---------------- */
  var termEl = document.getElementById("term");
  var term = {
    hpFill: document.getElementById("hpFill"),
    hpNum: document.getElementById("hpNum"),
    meterA: document.getElementById("meterA"),
    meterB: document.getElementById("meterB"),
    slotA: document.getElementById("slotA"),
    valA: document.getElementById("valA"),
    slotB: document.getElementById("slotB"),
    valB: document.getElementById("valB"),
    log: document.getElementById("log"),
    chanA: document.querySelector('.chan[data-ch="a"]'),
    chanB: document.querySelector('.chan[data-ch="b"]'),
    replay: document.getElementById("replayBtn"),
    scenario: document.getElementById("scenarioName")
  };

  var MAX_HP = 5000;
  var PLUGIN_CAP = 80;
  var MAX_LINES = 5;

  /* 剧本 S1：68% → 受击 → 48% → 受击 → 29% → 濒死 → 死亡界面 */
  var SCRIPT = [
    { wait: 620, hp: 5000, doomed: 0, ch: "-", slot: "A_NONE", cfg: 0, reason: "idle" },
    { wait: 900, hp: 3400, doomed: 0, ch: "a", slot: "A_70", cfg: 20, reason: "tier-enter" },
    { wait: 900, hp: 3000, doomed: 0, ch: "b", slot: "B_HIT", cfg: 25, reason: "hit" },
    { wait: 460, hp: 3000, doomed: 0, ch: "b", slot: "B_NONE", cfg: 0, reason: "pulse-end n=1 dt=300" },
    { wait: 700, hp: 2400, doomed: 0, ch: "a", slot: "A_50", cfg: 30, reason: "tier-enter" },
    { wait: 820, hp: 2300, doomed: 0, ch: "b", slot: "B_HIT", cfg: 25, reason: "hit" },
    { wait: 460, hp: 2300, doomed: 0, ch: "b", slot: "B_NONE", cfg: 0, reason: "pulse-end n=1 dt=300" },
    { wait: 700, hp: 1450, doomed: 0, ch: "a", slot: "A_30", cfg: 40, reason: "tier-enter" },
    { wait: 820, hp: 0, doomed: 1, ch: "a", slot: "A_DOOM", cfg: 60, reason: "tier-enter" },
    { wait: 1000, hp: 0, doomed: 1, ch: "b", slot: "E_DEATH", cfg: 45, reason: "death-screen" },
    { wait: 1500, hp: 0, doomed: 1, ch: "b", slot: "B_NONE", cfg: 0, reason: "pulse-end dur=5000" }
  ];

  var runToken = 0;
  var clock = 0;

  function sleep(ms) {
    return new Promise(function (resolve) {
      setTimeout(resolve, reduceMotion ? Math.min(ms, 150) : ms);
    });
  }

  function setWidth(el, pct) {
    if (el) el.style.width = Math.max(0, Math.min(100, pct)) + "%";
  }

  function paintFrame(step, ts) {
    setWidth(term.hpFill, (step.hp / MAX_HP) * 100);
    if (term.hpNum) term.hpNum.textContent = step.hp + " / " + MAX_HP;

    if (step.ch === "a") {
      setWidth(term.meterA, (step.cfg / PLUGIN_CAP) * 100);
      if (term.slotA) term.slotA.textContent = step.slot;
      if (term.valA) term.valA.textContent = String(step.cfg);
    }
    if (step.ch === "b") {
      setWidth(term.meterB, (step.cfg / PLUGIN_CAP) * 100);
      if (term.slotB) term.slotB.textContent = step.slot;
      if (term.valB) term.valB.textContent = String(step.cfg);
    }

    if (term.chanA) term.chanA.classList.toggle("is-on", step.ch === "a" && step.cfg > 0);
    if (term.chanB) term.chanB.classList.toggle("is-on", step.ch === "b" && step.cfg > 0);

    if (!term.log) return;

    var previous = term.log.querySelectorAll(".l-new");
    Array.prototype.forEach.call(previous, function (node) { node.classList.remove("l-new"); });

    var line = document.createElement("div");
    line.className = "l-new";
    line.textContent = "[" + ts + "] hp=" + step.hp + "/" + MAX_HP +
      " doomed=" + step.doomed + " titan=1 alive=1 | ch=" + step.ch +
      " slot=" + step.slot + " cfg=" + step.cfg + " eff=" + step.cfg +
      " clamp=- reason=" + step.reason;
    term.log.appendChild(line);

    while (term.log.children.length > MAX_LINES) {
      term.log.removeChild(term.log.firstChild);
    }
  }

  function resetFrame() {
    clock = 0;
    setWidth(term.hpFill, 100);
    if (term.hpNum) term.hpNum.textContent = MAX_HP + " / " + MAX_HP;
    setWidth(term.meterA, 0);
    setWidth(term.meterB, 0);
    if (term.slotA) term.slotA.textContent = "A_NONE";
    if (term.valA) term.valA.textContent = "0";
    if (term.slotB) term.slotB.textContent = "B_NONE";
    if (term.valB) term.valB.textContent = "0";
    if (term.chanA) term.chanA.classList.remove("is-on");
    if (term.chanB) term.chanB.classList.remove("is-on");
    if (term.log) term.log.innerHTML = "";
  }

  async function runTerminal() {
    if (!term.log) return;
    var token = ++runToken;
    resetFrame();
    if (term.scenario) {
      term.scenario.textContent = "剧本 S1：档位递进 + 受击 + 濒死（演示已加速）";
    }

    while (token === runToken) {
      for (var i = 0; i < SCRIPT.length; i++) {
        var step = SCRIPT[i];
        await sleep(step.wait);
        if (token !== runToken) return;
        clock += step.wait / 1000;
        paintFrame(step, Math.round(clock * 10) / 10);
      }
      await sleep(2000);
      if (token !== runToken) return;
      resetFrame();
    }
  }

  if (term.log) {
    runTerminal();

    if (term.replay) {
      term.replay.addEventListener("click", function () { runTerminal(); });
    }

    /* 终端离开视口时停掉节奏，回到视口再从头上演一遍 */
    if ("IntersectionObserver" in window && termEl) {
      var termIo = new IntersectionObserver(function (entries) {
        entries.forEach(function (entry) {
          if (entry.isIntersecting) {
            runTerminal();
          } else {
            runToken++;
          }
        });
      }, { threshold: 0.05 });
      termIo.observe(termEl);
    }
  }

  /* ---------------- 4. 复制 ---------------- */
  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text);
    }
    return new Promise(function (resolve, reject) {
      var area = document.createElement("textarea");
      area.value = text;
      area.setAttribute("readonly", "");
      area.style.position = "fixed";
      area.style.left = "-9999px";
      document.body.appendChild(area);
      area.select();
      var ok = false;
      try {
        ok = document.execCommand("copy");
      } finally {
        document.body.removeChild(area);
      }
      ok ? resolve() : reject(new Error("copy failed"));
    });
  }

  Array.prototype.forEach.call(document.querySelectorAll("[data-copy]"), function (btn) {
    btn.addEventListener("click", function () {
      var label = btn.getAttribute("data-label") || btn.textContent;
      copyText(btn.getAttribute("data-copy")).then(function () {
        btn.textContent = "已复制";
        btn.setAttribute("data-done", "1");
      }).catch(function () {
        btn.textContent = "请手动选择复制";
      }).then(function () {
        setTimeout(function () {
          btn.textContent = label;
          btn.removeAttribute("data-done");
        }, 1600);
      });
    });
  });

  /* ---------------- 5. 折叠面板单开 ---------------- */
  Array.prototype.forEach.call(document.querySelectorAll(".acc"), function (acc) {
    var items = Array.prototype.slice.call(acc.querySelectorAll("details"));
    items.forEach(function (item) {
      item.addEventListener("toggle", function () {
        if (!item.open) return;
        items.forEach(function (other) {
          if (other !== item) other.open = false;
        });
      });
    });
  });
})();
