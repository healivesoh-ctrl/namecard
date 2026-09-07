/**
 * 구글폼 응답 → 카카오 알림톡 자동 발송 (Google Apps Script)
 * ─────────────────────────────────────────────────────────────
 * 설치 (5분):
 *  1. 구글폼 → [응답] 탭 → 스프레드시트 아이콘으로 "응답 스프레드시트" 만들기
 *  2. 그 스프레드시트에서 [확장 프로그램] → [Apps Script] → 이 파일 내용을 통째로 붙여넣기
 *  3. 아래 CONFIG 수정 (발송사 키 등 비밀값은 [프로젝트 설정] → [스크립트 속성]에 넣는 것을 권장)
 *  4. 상단 함수 선택에서 setup 을 고르고 ▶ 실행 → 권한 승인
 *     → 폼 제출 트리거 + 5분마다 예약 발송 트리거가 만들어지고 "알림톡_대기"/"알림톡_로그" 시트가 생깁니다.
 *  5. testSend 를 실행해 본인 번호로 시험 발송
 *
 * 동작:
 *  - 폼이 제출되면 전화번호·이름 항목을 찾아 MESSAGES 규칙대로 즉시 발송하거나 "알림톡_대기" 시트에 예약
 *  - processQueue 가 5분마다 대기 시트를 확인해 시각이 된 것을 발송
 *  - 모든 결과는 "알림톡_로그" 시트에 남습니다
 *
 * MODE:
 *  - "solapi" / "aligo" : 이 스크립트가 직접 발송사 API 호출 (서버 불필요)
 *  - "webhook"          : 응답을 서버(kakao-alimtalk/server)로 전달, 규칙·예약은 서버의 rules.yaml 이 담당
 */

const CONFIG = {
  MODE: "solapi",                 // "solapi" | "aligo" | "webhook"
  FORM_ID: "default",             // 여러 폼을 쓸 때 구분용 (webhook 모드에서는 서버 rules.yaml 의 forms[].id 와 맞춤)
  SENDER: "0212345678",           // 발송사에 등록한 발신번호 (숫자만)
  TEST_PHONE: "010-0000-0000",    // testSend() 가 보낼 번호

  // 비밀값: 스크립트 속성(SOLAPI_API_KEY 등)에 넣으면 여기 값보다 우선합니다.
  SOLAPI: { apiKey: "", apiSecret: "", pfId: "" },                    // pfId = 카카오 채널 연동 후 발급 (KA01PF...)
  ALIGO:  { apiKey: "", userId: "", senderKey: "", testMode: false }, // senderKey = 발신프로필 키
  WEBHOOK: { url: "https://<서버주소>/webhook/form", secret: "" },

  QUIET_HOURS: ["21:00", "08:00"], // 야간 발송 금지 구간 → 08:00 으로 미룸. 사용 안 하면 null
  REQUIRE_CONSENT: false,          // true 면 수신동의 항목에 동의한 응답만 발송 (광고성 메시지는 반드시 true)
  DEDUPE_HOURS: 24,                // 같은 번호가 N시간 내 재제출하면 발송 생략 (0 = 제한 없음)

  // 폼 질문 제목에서 항목을 찾는 키워드 (부분 일치)
  FIELDS: {
    phone:   ["전화번호", "휴대폰", "휴대전화", "연락처", "핸드폰", "전화", "phone", "mobile"],
    name:    ["이름", "성함", "성명", "name"],
    consent: ["수신동의", "수신 동의", "마케팅", "알림톡 동의", "메시지 수신"],
  },

  // 발송 규칙 (직접 발송 모드). {{질문제목}} 으로 응답값 삽입, {{name}} {{phone}} {{date}} 내장. {{항목|기본값}} 가능
  //   when: "immediate" | "30m" | "2h" | "1d" | {at: "2026-09-20 09:00"} | {at_time: "09:00"}
  //         | {field_at: {field: "수업일", days: -1, time: "18:00"}}   ← 폼의 날짜 답변 기준 D-1 18:00
  //   match: {"질문제목": "답변에 포함될 문구"}  → 조건에 맞을 때만 발송
  //   text : 완성 본문 — 알리고는 필수(승인 템플릿과 동일해야 함), 솔라피는 알림톡 실패 시 문자 대체 발송문
  MESSAGES: [
    {
      name: "접수확인",
      template: "TP_RECEIPT_01",
      when: "immediate",
      variables: { "이름": "{{name|고객}}", "과정명": "{{수강 과정|프로그램}}" },
      text: "{{name|고객}}님, {{수강 과정|프로그램}} 신청이 접수되었습니다.\n담당자가 확인 후 연락드리겠습니다. 감사합니다.",
      subject: "신청 접수 안내",
    },
    {
      name: "하루전리마인드",
      template: "TP_REMIND_01",
      when: { field_at: { field: "수업일", days: -1, time: "18:00" } },
      variables: { "이름": "{{name|고객}}", "일시": "{{수업일}}" },
      text: "{{name|고객}}님, 내일 {{수업일}} 수업이 있습니다. 잊지 말고 참석해 주세요!",
    },
  ],
};

const QUEUE_SHEET = "알림톡_대기";
const LOG_SHEET = "알림톡_로그";
const QUEUE_HEADER = ["예정시각", "상태", "메시지", "수신번호", "이름", "템플릿", "변수(JSON)", "본문", "제목", "등록시각", "결과"];
const LOG_HEADER = ["시각", "구분", "메시지", "수신번호", "이름", "결과", "상세"];

// ═══════════════════════════════ 설치·수동 실행 ═══════════════════════════════

function setup() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  ensureSheet_(ss, QUEUE_SHEET, QUEUE_HEADER);
  ensureSheet_(ss, LOG_SHEET, LOG_HEADER);
  ScriptApp.getProjectTriggers().forEach(t => {
    if (["onFormSubmit", "processQueue"].includes(t.getHandlerFunction())) ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger("onFormSubmit").forSpreadsheet(ss).onFormSubmit().create();
  ScriptApp.newTrigger("processQueue").timeBased().everyMinutes(5).create();
  log_("설치", "-", "-", "-", "OK", "트리거 설치 완료: 폼 제출 + 5분마다 예약 발송 (모드: " + CONFIG.MODE + ")");
  Logger.log("설치 완료. testSend 로 시험 발송해 보세요.");
}

/** 본인 번호로 첫 번째 규칙을 시험 발송 */
function testSend() {
  const answers = { "이름": "테스트", "연락처": CONFIG.TEST_PHONE, "수강 과정": "시험 과정", "수업일": Utilities.formatDate(new Date(Date.now() + 2 * 86400000), tz_(), "yyyy-MM-dd") };
  if (CONFIG.MODE === "webhook") {
    Logger.log(JSON.stringify(forwardToServer_(answers)));
    return;
  }
  const m = CONFIG.MESSAGES[0];
  const phone = normalizePhone(CONFIG.TEST_PHONE);
  const b = builtins_("테스트", phone);
  const res = sendAlimtalk_(phone, m.template, renderVars_(m.variables, answers, b), render_(m.text, answers, b), render_(m.subject, answers, b));
  log_("테스트", m.name, phone, "테스트", res.ok ? "OK" : "FAIL", JSON.stringify(res));
  Logger.log(JSON.stringify(res));
}

/** 이미 시트에 있는 응답(예: 스크립트 설치 전 응답)을 골라 수동 접수: 행 번호 지정 */
function intakeRow(rowNumber) {
  const sh = SpreadsheetApp.getActiveSpreadsheet().getSheets().find(s => s.getLastColumn() > 0 && s.getRange(1, 1).getValue() === "타임스탬프");
  if (!sh) throw new Error("응답 시트를 찾지 못했습니다 (첫 열이 '타임스탬프' 인 시트)");
  const head = sh.getRange(1, 1, 1, sh.getLastColumn()).getValues()[0];
  const row = sh.getRange(rowNumber, 1, 1, sh.getLastColumn()).getValues()[0];
  const answers = {};
  head.forEach((h, i) => { if (h) answers[String(h)] = row[i] instanceof Date ? Utilities.formatDate(row[i], tz_(), "yyyy-MM-dd HH:mm") : String(row[i] ?? ""); });
  handleSubmission_(answers);
}

// ═══════════════════════════════ 트리거 ═══════════════════════════════

function onFormSubmit(e) {
  try {
    const answers = answersFromEvent_(e);
    handleSubmission_(answers);
  } catch (err) {
    log_("오류", "-", "-", "-", "FAIL", String(err && err.stack || err));
  }
}

function processQueue() {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(10000)) return;
  try {
    const sh = ensureSheet_(SpreadsheetApp.getActiveSpreadsheet(), QUEUE_SHEET, QUEUE_HEADER);
    const last = sh.getLastRow();
    if (last < 2) return;
    const rows = sh.getRange(2, 1, last - 1, QUEUE_HEADER.length).getValues();
    const now = new Date();
    rows.forEach((r, i) => {
      const [when, status, name, phone, person, template, varsJson, text, subject] = r;
      if (status !== "대기" || !(when instanceof Date) || when > now) return;
      const res = sendAlimtalk_(String(phone), String(template), JSON.parse(varsJson || "{}"), text || null, subject || null);
      sh.getRange(i + 2, 2).setValue(res.ok ? "발송됨" : "실패");
      sh.getRange(i + 2, 11).setValue((res.ok ? "OK " : "FAIL ") + (res.messageId || res.error || ""));
      log_("예약발송", name, String(phone), String(person), res.ok ? "OK" : "FAIL", JSON.stringify(res));
    });
  } finally {
    lock.releaseLock();
  }
}

// ═══════════════════════════════ 핵심 처리 ═══════════════════════════════

function handleSubmission_(answers) {
  if (CONFIG.MODE === "webhook") {
    const res = forwardToServer_(answers);
    log_("서버전달", "-", res.phone || "-", answers[findField_(answers, CONFIG.FIELDS.name)] || "", res.ok ? "OK" : "SKIP", JSON.stringify(res));
    return;
  }
  const phoneKey = findField_(answers, CONFIG.FIELDS.phone);
  let phone = phoneKey ? normalizePhone(answers[phoneKey]) : null;
  if (!phone) for (const v of Object.values(answers)) { phone = normalizePhone(v); if (phone) break; }
  if (!phone) { log_("접수", "-", "-", "-", "SKIP", "휴대폰 번호를 찾지 못함: " + JSON.stringify(answers)); return; }
  const nameKey = findField_(answers, CONFIG.FIELDS.name);
  const name = nameKey ? String(answers[nameKey]).trim() : "";
  const consentKey = findField_(answers, CONFIG.FIELDS.consent);
  if (consentKey) {
    if (!isAffirmative_(answers[consentKey])) { log_("접수", "-", phone, name, "SKIP", "수신 비동의"); return; }
  } else if (CONFIG.REQUIRE_CONSENT) { log_("접수", "-", phone, name, "SKIP", "수신동의 항목 없음"); return; }
  if (isOptedOut_(phone)) { log_("접수", "-", phone, name, "SKIP", "수신거부 번호"); return; }
  if (CONFIG.DEDUPE_HOURS > 0 && recentlyHandled_(phone, CONFIG.DEDUPE_HOURS)) { log_("접수", "-", phone, name, "SKIP", CONFIG.DEDUPE_HOURS + "시간 내 중복 응답"); return; }

  const now = new Date();
  const b = builtins_(name, phone);
  const queue = ensureSheet_(SpreadsheetApp.getActiveSpreadsheet(), QUEUE_SHEET, QUEUE_HEADER);
  let count = 0;
  CONFIG.MESSAGES.forEach(m => {
    if (!matches_(m.match, answers)) return;
    let at;
    try { at = computeSendTime_(m.when, now, answers); } catch (err) { log_("접수", m.name, phone, name, "SKIP", String(err.message || err)); return; }
    const vars = renderVars_(m.variables || {}, answers, b);
    const text = render_(m.text, answers, b), subject = render_(m.subject, answers, b);
    if (at.getTime() - now.getTime() < 60000) {
      const res = sendAlimtalk_(phone, m.template, vars, text, subject);
      log_("즉시발송", m.name, phone, name, res.ok ? "OK" : "FAIL", JSON.stringify(res));
    } else {
      queue.appendRow([at, "대기", m.name, "'" + phone, name, m.template, JSON.stringify(vars), text || "", subject || "", now, ""]);
      log_("예약등록", m.name, phone, name, "OK", "예정: " + Utilities.formatDate(at, tz_(), "yyyy-MM-dd HH:mm"));
    }
    count++;
  });
  log_("접수", "-", phone, name, "OK", count + "건 처리");
}

function forwardToServer_(answers) {
  const url = CONFIG.WEBHOOK.url + (CONFIG.WEBHOOK.url.includes("?") ? "&" : "?") + "form=" + encodeURIComponent(CONFIG.FORM_ID);
  const secret = prop_("WEBHOOK_SECRET", CONFIG.WEBHOOK.secret);
  const res = UrlFetchApp.fetch(url, {
    method: "post", contentType: "application/json", muteHttpExceptions: true,
    headers: { "X-Webhook-Secret": secret },
    payload: JSON.stringify({ form: CONFIG.FORM_ID, answers: answers, source: "google-form" }),
  });
  try { return Object.assign({ http: res.getResponseCode() }, JSON.parse(res.getContentText())); }
  catch (e) { return { ok: false, http: res.getResponseCode(), error: res.getContentText().slice(0, 300) }; }
}

// ═══════════════════════════════ 발송사 ═══════════════════════════════

function sendAlimtalk_(phone, template, variables, text, subject) {
  try {
    if (CONFIG.MODE === "solapi") return solapiSend_(phone, template, variables, text, subject);
    if (CONFIG.MODE === "aligo") return aligoSend_(phone, template, text, subject);
    return { ok: false, error: "알 수 없는 MODE: " + CONFIG.MODE };
  } catch (err) {
    return { ok: false, error: String(err.message || err) };
  }
}

function solapiSend_(phone, template, variables, text, subject) {
  const apiKey = prop_("SOLAPI_API_KEY", CONFIG.SOLAPI.apiKey), apiSecret = prop_("SOLAPI_API_SECRET", CONFIG.SOLAPI.apiSecret);
  const pfId = prop_("SOLAPI_PF_ID", CONFIG.SOLAPI.pfId);
  if (!apiKey || !apiSecret || !pfId) return { ok: false, error: "SOLAPI 설정(apiKey/apiSecret/pfId)이 비어 있습니다" };
  const date = Utilities.formatDate(new Date(), "UTC", "yyyy-MM-dd'T'HH:mm:ss'Z'");
  const salt = Utilities.getUuid().replace(/-/g, "");
  const sig = Utilities.computeHmacSha256Signature(date + salt, apiSecret).map(b => ("0" + (b & 0xff).toString(16)).slice(-2)).join("");
  const vars = {};
  Object.keys(variables || {}).forEach(k => vars[k.startsWith("#{") ? k : "#{" + k + "}"] = variables[k]);
  const message = { to: phone, from: CONFIG.SENDER, type: "ATA", kakaoOptions: { pfId: pfId, templateId: template, variables: vars, disableSms: false } };
  if (text) message.text = text;
  if (subject) message.subject = subject;
  const res = UrlFetchApp.fetch("https://api.solapi.com/messages/v4/send", {
    method: "post", contentType: "application/json", muteHttpExceptions: true,
    headers: { Authorization: "HMAC-SHA256 apiKey=" + apiKey + ", date=" + date + ", salt=" + salt + ", signature=" + sig },
    payload: JSON.stringify({ message: message }),
  });
  const code = res.getResponseCode();
  let data; try { data = JSON.parse(res.getContentText()); } catch (e) { data = { body: res.getContentText().slice(0, 300) }; }
  if (code !== 200) return { ok: false, provider: "solapi", error: "HTTP " + code + " " + (data.errorMessage || data.errorCode || JSON.stringify(data)) };
  if (String(data.statusCode || "").startsWith("4")) return { ok: false, provider: "solapi", error: data.statusCode + " " + (data.statusMessage || ""), messageId: data.messageId };
  return { ok: true, provider: "solapi", messageId: data.messageId, status: data.statusCode };
}

function aligoSend_(phone, template, text, subject) {
  const apiKey = prop_("ALIGO_API_KEY", CONFIG.ALIGO.apiKey), userId = prop_("ALIGO_USER_ID", CONFIG.ALIGO.userId);
  const senderKey = prop_("ALIGO_SENDER_KEY", CONFIG.ALIGO.senderKey);
  if (!apiKey || !userId || !senderKey) return { ok: false, error: "ALIGO 설정(apiKey/userId/senderKey)이 비어 있습니다" };
  if (!text) return { ok: false, error: "알리고 발송에는 완성 본문(text)이 필요합니다" };
  const tok = JSON.parse(UrlFetchApp.fetch("https://kakaoapi.aligo.in/akv10/token/create/30/s/", {
    method: "post", muteHttpExceptions: true, payload: { apikey: apiKey, userid: userId } }).getContentText());
  if (String(tok.code) !== "0") return { ok: false, provider: "aligo", error: "토큰 발급 실패: " + tok.message };
  const payload = {
    apikey: apiKey, userid: userId, token: tok.token, senderkey: senderKey, tpl_code: template, sender: CONFIG.SENDER,
    receiver_1: phone, subject_1: (subject || template).slice(0, 50), message_1: text,
    failover: "Y", fsubject_1: (subject || "안내").slice(0, 30), fmessage_1: text,
    testMode: CONFIG.ALIGO.testMode ? "Y" : "N",
  };
  const data = JSON.parse(UrlFetchApp.fetch("https://kakaoapi.aligo.in/akv10/alimtalk/send/", { method: "post", muteHttpExceptions: true, payload: payload }).getContentText());
  if (String(data.code) !== "0") return { ok: false, provider: "aligo", error: data.code + " " + data.message };
  return { ok: true, provider: "aligo", messageId: data.info && data.info.mid };
}

// ═══════════════════════════════ 규칙 계산 ═══════════════════════════════

function computeSendTime_(when, now, answers) {
  let t;
  if (!when || when === "immediate" || when === "now" || when === "즉시") t = new Date(now);
  else if (typeof when === "string") t = new Date(now.getTime() + parseDelay_(when));
  else if (when.delay) t = new Date(now.getTime() + parseDelay_(when.delay));
  else if (when.at) t = parseDateTime_(when.at);
  else if (when.at_time) { t = atTime_(now, when.at_time); if (t <= now) t = new Date(t.getTime() + 86400000); }
  else if (when.field_at) {
    const key = findField_(answers, [when.field_at.field]);
    const d = key ? parseDate_(answers[key]) : null;
    if (!d) throw new Error("날짜 항목(" + when.field_at.field + ")을 읽지 못해 예약을 건너뜁니다");
    t = atTime_(new Date(d.getTime() + (Number(when.field_at.days || 0)) * 86400000), when.field_at.time || "09:00");
    if (t <= now) throw new Error("예약 시각이 이미 지났습니다");
  } else throw new Error("알 수 없는 when: " + JSON.stringify(when));
  if (t < now) t = new Date(now);
  return applyQuietHours_(t);
}

function parseDelay_(s) {
  const m = /^\s*(\d+)\s*([mhdw]|분|시간|일|주)\s*$/i.exec(String(s));
  if (!m) throw new Error("지연 시간 형식 오류: " + s);
  const unit = { m: 60000, "분": 60000, h: 3600000, "시간": 3600000, d: 86400000, "일": 86400000, w: 604800000, "주": 604800000 }[m[2].toLowerCase()];
  return Number(m[1]) * unit;
}

function parseDate_(v) {
  if (v instanceof Date) return new Date(v.getFullYear(), v.getMonth(), v.getDate());
  const s = String(v || "").trim();
  let m = /(\d{4})\D+(\d{1,2})\D+(\d{1,2})/.exec(s);
  if (m) return new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  if (/^\d{8}$/.test(s)) return new Date(Number(s.slice(0, 4)), Number(s.slice(4, 6)) - 1, Number(s.slice(6)));
  m = /(\d{1,2})\s*월\s*(\d{1,2})\s*일/.exec(s);
  if (m) return new Date(new Date().getFullYear(), Number(m[1]) - 1, Number(m[2]));
  return null;
}

function parseDateTime_(s) {
  const m = /(\d{4})-(\d{2})-(\d{2})\s+(\d{1,2}):(\d{2})/.exec(String(s));
  if (!m) throw new Error("일시 형식 오류 (YYYY-MM-DD HH:MM): " + s);
  return new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]), Number(m[4]), Number(m[5]));
}

function atTime_(d, hhmm) {
  const [h, mi] = String(hhmm).split(":").map(Number);
  return new Date(d.getFullYear(), d.getMonth(), d.getDate(), h, mi || 0, 0, 0);
}

function applyQuietHours_(t) {
  const q = CONFIG.QUIET_HOURS;
  if (!q) return t;
  const mins = t.getHours() * 60 + t.getMinutes();
  const [sh, sm] = q[0].split(":").map(Number), [eh, em] = q[1].split(":").map(Number);
  const start = sh * 60 + sm, end = eh * 60 + em;
  const inQuiet = start <= end ? (mins >= start && mins < end) : (mins >= start || mins < end);
  if (!inQuiet) return t;
  let c = atTime_(t, q[1]);
  if (c <= t) c = new Date(c.getTime() + 86400000);
  return c;
}

function matches_(cond, answers) {
  if (!cond) return true;
  return Object.keys(cond).every(k => {
    const got = String(lookup_(answers, k)).toLowerCase();
    const wants = Array.isArray(cond[k]) ? cond[k] : [cond[k]];
    return wants.some(w => got.includes(String(w).toLowerCase()));
  });
}

// ═══════════════════════════════ 항목·템플릿 ═══════════════════════════════

function answersFromEvent_(e) {
  const answers = {};
  if (e && e.namedValues) {                       // 스프레드시트 트리거
    Object.keys(e.namedValues).forEach(k => answers[String(k).trim()] = (e.namedValues[k] || []).filter(v => v !== "").join(", "));
  } else if (e && e.response) {                   // 폼 자체 트리거
    e.response.getItemResponses().forEach(ir => {
      const v = ir.getResponse();
      answers[ir.getItem().getTitle().trim()] = Array.isArray(v) ? v.join(", ") : String(v ?? "");
    });
  } else throw new Error("폼 제출 이벤트가 아닙니다");
  return answers;
}

function findField_(answers, keywords) {
  const keys = Object.keys(answers || {});
  const kws = (keywords || []).map(k => String(k).toLowerCase());
  const exact = keys.find(k => kws.includes(k.toLowerCase()));
  if (exact) return exact;
  for (const kw of kws) { const hit = keys.find(k => k.toLowerCase().includes(kw)); if (hit) return hit; }
  return null;
}

function lookup_(answers, key, builtins) {
  if (builtins && key in builtins) return builtins[key];
  if (key in answers) return String(answers[key]);
  const f = findField_(answers, [key]);
  return f ? String(answers[f]) : "";
}

function render_(tpl, answers, builtins) {
  if (tpl == null) return null;
  return String(tpl).replace(/\{\{\s*([^}]+?)\s*\}\}/g, (_, key) => {
    let def = "";
    if (key.includes("|")) { const p = key.split("|"); key = p[0].trim(); def = p.slice(1).join("|").trim(); }
    const v = lookup_(answers, key, builtins);
    return v ? v : def;
  });
}

function renderVars_(spec, answers, builtins) {
  const out = {};
  Object.keys(spec || {}).forEach(k => out[k] = render_(spec[k], answers, builtins) || "");
  return out;
}

function builtins_(name, phone) {
  const now = new Date();
  return { name: name, "이름": name, phone: formatPhone(phone), "전화번호": formatPhone(phone),
           date: Utilities.formatDate(now, tz_(), "yyyy-MM-dd"), "오늘": Utilities.formatDate(now, tz_(), "yyyy-MM-dd"),
           time: Utilities.formatDate(now, tz_(), "HH:mm") };
}

function isAffirmative_(v) {
  const s = String(v || "").trim().toLowerCase();
  if (!s) return false;
  if (["비동의", "미동의", "동의하지", "거부", "no", "아니"].some(n => s.includes(n))) return false;
  return ["동의", "예", "네", "yes", "y", "true", "수신", "받겠", "ok"].some(a => s.includes(a));
}

/** 휴대폰 번호 정규화 → "01012345678" / 아니면 null */
function normalizePhone(raw) {
  if (raw == null) return null;
  let s = String(raw).trim().split(/[\/,;]| 또는 |\s{2,}/)[0];
  let d = s.replace(/\D/g, "");
  if (!d) return null;
  if (d.startsWith("0082")) d = "0" + d.slice(4);
  else if (d.startsWith("82") && d.length >= 11) d = "0" + d.slice(2);
  if (d.length === 10 && /^1[016789]/.test(d)) d = "0" + d;
  if (/^01[016789]\d{7,8}$/.test(d)) return d;
  return null;
}

function formatPhone(d) {
  if (!d) return "";
  return d.length === 11 ? d.slice(0, 3) + "-" + d.slice(3, 7) + "-" + d.slice(7) : d.length === 10 ? d.slice(0, 3) + "-" + d.slice(3, 6) + "-" + d.slice(6) : d;
}

// ═══════════════════════════════ 시트·로그·유틸 ═══════════════════════════════

function ensureSheet_(ss, name, header) {
  let sh = ss.getSheetByName(name);
  if (!sh) {
    sh = ss.insertSheet(name);
    sh.appendRow(header);
    sh.getRange(1, 1, 1, header.length).setFontWeight("bold");
    sh.setFrozenRows(1);
  }
  return sh;
}

function log_(kind, msg, phone, name, result, detail) {
  try {
    ensureSheet_(SpreadsheetApp.getActiveSpreadsheet(), LOG_SHEET, LOG_HEADER)
      .appendRow([new Date(), kind, msg, phone && phone !== "-" ? "'" + phone : phone, name, result, String(detail || "").slice(0, 1000)]);
  } catch (e) { Logger.log(kind + " " + result + " " + detail); }
}

/** 최근 N시간 내 같은 번호로 접수(OK) 로그가 있으면 true */
function recentlyHandled_(phone, hours) {
  const sh = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(LOG_SHEET);
  if (!sh || sh.getLastRow() < 2) return false;
  const since = Date.now() - hours * 3600000;
  const n = Math.min(sh.getLastRow() - 1, 2000);
  const rows = sh.getRange(sh.getLastRow() - n + 1, 1, n, 6).getValues();
  return rows.some(r => r[1] === "접수" && r[5] === "OK" && String(r[3]).replace(/\D/g, "") === phone && r[0] instanceof Date && r[0].getTime() >= since);
}

/** "수신거부" 시트(A열에 번호) 가 있으면 그 번호는 제외 */
function isOptedOut_(phone) {
  const sh = SpreadsheetApp.getActiveSpreadsheet().getSheetByName("수신거부");
  if (!sh || sh.getLastRow() < 1) return false;
  return sh.getRange(1, 1, sh.getLastRow(), 1).getValues().some(r => normalizePhone(r[0]) === phone);
}

function prop_(key, fallback) {
  const v = PropertiesService.getScriptProperties().getProperty(key);
  return v ? v : (fallback || "");
}

function tz_() { return Session.getScriptTimeZone() || "Asia/Seoul"; }
