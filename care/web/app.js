/* 공통 헬퍼 — API 호출, 포맷, 토스트, 접수건 로컬 기억 */
(function (global) {
  "use strict";

  const STORE_KEY = "dayeum.care.apps"; // [{code, token, mother_name, saved_at}]

  async function api(path, options) {
    const opt = Object.assign({ headers: {} }, options || {});
    if (opt.body && !(opt.body instanceof FormData)) {
      opt.headers["Content-Type"] = "application/json";
      opt.body = JSON.stringify(opt.body);
    }
    const res = await fetch(path, opt);
    let data = null;
    try { data = await res.json(); } catch (e) { data = null; }
    if (!res.ok) {
      const msg = (data && data.detail) || "요청을 처리하지 못했습니다. (" + res.status + ")";
      throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
    }
    return data;
  }

  function toast(message, isError) {
    const el = document.createElement("div");
    el.className = "toast" + (isError ? " bad" : "");
    el.textContent = message;
    document.body.appendChild(el);
    setTimeout(function () { el.remove(); }, isError ? 4200 : 2600);
  }

  function won(n) {
    const v = Number(n || 0);
    if (!v) return "-";
    return v.toLocaleString("ko-KR") + "원";
  }

  function fmtDate(iso) {
    if (!iso) return "-";
    const d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    const p = function (x) { return String(x).padStart(2, "0"); };
    return d.getFullYear() + "." + p(d.getMonth() + 1) + "." + p(d.getDate()) + " " + p(d.getHours()) + ":" + p(d.getMinutes());
  }

  function fileSize(bytes) {
    const b = Number(bytes || 0);
    if (b < 1024) return b + "B";
    if (b < 1024 * 1024) return Math.round(b / 1024) + "KB";
    return (b / 1024 / 1024).toFixed(1) + "MB";
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  /* 연락처 입력 도우미 — 숫자만 남겨 하이픈을 붙인다 */
  function bindPhone(input) {
    input.addEventListener("input", function () {
      const d = input.value.replace(/\D/g, "").slice(0, 11);
      let out = d;
      if (d.length > 7) out = d.slice(0, 3) + "-" + d.slice(3, 7) + "-" + d.slice(7);
      else if (d.length > 3) out = d.slice(0, 3) + "-" + d.slice(3);
      input.value = out;
    });
  }

  /* 접수번호·조회코드는 이 브라우저에만 저장해 재방문 시 바로 열 수 있게 한다 */
  function readStore() {
    try { return JSON.parse(localStorage.getItem(STORE_KEY) || "[]"); } catch (e) { return []; }
  }
  function remember(app) {
    if (!app || !app.code || !app.token) return;
    try {
      const list = readStore().filter(function (x) { return x.code !== app.code; });
      list.unshift({ code: app.code, token: app.token, mother_name: app.mother_name || "", saved_at: Date.now() });
      localStorage.setItem(STORE_KEY, JSON.stringify(list.slice(0, 10)));
    } catch (e) { /* 사생활 보호 모드 등 — 저장 실패는 무시 */ }
  }
  function forget(code) {
    try {
      localStorage.setItem(STORE_KEY, JSON.stringify(readStore().filter(function (x) { return x.code !== code; })));
    } catch (e) { /* 무시 */ }
  }

  function statusBadgeClass(status) {
    return { confirmed: "ok", rejected: "bad", reviewing: "warn", received: "info", draft: "" }[status] || "";
  }

  function qs(name) {
    return new URLSearchParams(location.search).get(name) || "";
  }

  global.Care = {
    api: api, toast: toast, won: won, fmtDate: fmtDate, fileSize: fileSize, esc: esc,
    bindPhone: bindPhone, readStore: readStore, remember: remember, forget: forget,
    statusBadgeClass: statusBadgeClass, qs: qs,
    STEPS: [
      { key: "draft", label: "작성중" },
      { key: "received", label: "접수완료" },
      { key: "reviewing", label: "서류검토" },
      { key: "confirmed", label: "최종확인" }
    ]
  };
})(window);
