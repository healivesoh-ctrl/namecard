"""jobs/sanmo_provider/crawl_script.py — 전국 산모·신생아 건강관리 지원사업 제공기관(파견기관) 수집

수집 항목: 시도 · 시군구 · 기관명 · 주소 · 전화 · 팩스 · 이메일 · 대표자 · 사업(서비스)명 등
          API/페이지가 주는 원시 필드는 전부 뒤에 그대로 붙인다 (raw_*).

두 가지 경로 (가벼운 것부터):

  Track A  --mode api  (기본)  사다리 0단 — 공식 Open API (data.go.kr, 제공자가 열어 둔 경로)
      데이터셋 : 한국사회보장정보원_사회서비스 제공기관 정보 검색  (data.go.kr/data/15057683/openapi.do)
      목록 API : https://api.socialservice.or.kr:444/api/service/provider/providerList
      코드 API : https://api.socialservice.or.kr:444/api/service/common/serviceType
      서비스키 : 환경변수 DATA_GO_KR_SERVICE_KEY  (data.go.kr 로그인 → 활용신청 → 자동승인, 일반 인증키(Decoding))

  Track B  --mode web          사다리 1단 — 전자바우처 누리집 제공기관 검색 (정적 HTML, 위장 없음)
      목록     : https://www.socialservice.or.kr:444/user/svcsrch/supply/supplyList.do
      상세     : https://www.socialservice.or.kr:444/user/svcsrch/supply/bokji/supplyViewBokji.do?pb=<id>&v=m

이 스크립트는 **원격 샌드박스(대상 사이트 egress 차단)에서 정찰 없이 작성**됐다. 그래서 필드명·폼 파라미터를
고정하지 않고 응답에서 스스로 찾아내도록 짜여 있고, 첫 응답 원문을 samples/ 에 남긴다 — 로컬에서 한 번
돌려 보고 samples/ 와 로그를 보면 다음 실행에서 매핑을 확정할 수 있다.

사용 예:
  python jobs/sanmo_provider/crawl_script.py --test                # API 1페이지만 (테스트)
  python jobs/sanmo_provider/crawl_script.py                       # API 전량
  python jobs/sanmo_provider/crawl_script.py --mode web --test     # 누리집 검색 1개 시도 2페이지 + 상세 5건
  python jobs/sanmo_provider/crawl_script.py --mode api --extra srvcCd=XXXX   # 문서를 보고 필터 파라미터를 직접 지정
  python jobs/sanmo_provider/crawl_script.py --selftest            # 네트워크 없이 파서만 검증
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urljoin

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

from utils import (  # noqa: E402
    BudgetExceeded, RateLimiter, check_robots, detect_pii, detect_softblock,
    plain_get, plain_session, sanitize_filename, setup_logger, validate_values,
)
from export_excel import export_to_excel  # noqa: E402
from progress import ProgressTracker  # noqa: E402
from domain_profile import DomainProfile  # noqa: E402

logger = setup_logger("sanmo_provider")

# ───────────────────────── 설정 ─────────────────────────
KEYWORD = "산모"                     # 사업명/서비스명에서 이 글자가 들어간 것만 남긴다
API_DOMAIN = "api.socialservice.or.kr"
API_LIST = f"https://{API_DOMAIN}:444/api/service/provider/providerList"
API_CODES = f"https://{API_DOMAIN}:444/api/service/common/serviceType"
WEB_DOMAIN = "www.socialservice.or.kr"
WEB_LIST = f"https://{WEB_DOMAIN}:444/user/svcsrch/supply/supplyList.do"
WEB_DETAIL = f"https://{WEB_DOMAIN}:444/user/svcsrch/supply/bokji/supplyViewBokji.do"
USER_AGENT = "namecard-sanmo-provider-crawler/1.0 (+https://github.com/healivesoh-ctrl/namecard; contact: repo owner)"

# 사업구분 필터 파라미터 이름은 문서를 못 봐서 모른다. 후보를 전부 보내면 서버는 모르는 것을 무시하고
# 맞는 것 하나만 먹는다. 그래도 0건이면 필터 없이 받아서 클라이언트에서 걸러낸다.
SERVICE_PARAM_CANDIDATES = ("srvcCd", "svcCd", "svcTypeCd", "bizCd", "serviceTypeCd", "srvcTypeCd")

# 출력 컬럼 ← 원시 키 힌트 (소문자 비교). 앞에 있는 힌트가 우선.
FIELD_HINTS: dict[str, tuple[str, ...]] = {
    "기관명":   ("provnm", "orgnm", "instnm", "agcnm", "agencynm", "faclnm", "fcltnm", "entnm", "기관명", "제공기관명"),
    "시도":     ("sidonm", "ctpvnm", "sido", "시도"),
    "시군구":   ("sggnm", "signgunm", "sigungunm", "sigungu", "sgg", "시군구"),
    "주소":     ("addr", "adres", "주소"),
    "상세주소": ("detailaddr", "dtladdr", "addrdtl", "상세주소"),
    "우편번호": ("zip", "postno", "우편번호"),
    "전화":     ("telno", "tel", "phone", "hp", "전화"),
    "팩스":     ("fax", "팩스"),
    "이메일":   ("email", "emailaddr", "mail", "이메일"),
    "대표자":   ("rprsntv", "ceo", "repr", "대표"),
    "홈페이지": ("homepage", "hmpg", "url", "홈페이지"),
    "사업명":   ("srvcnm", "svcnm", "biznm", "servicenm", "bsnsnm", "사업명", "서비스명"),
    "사업코드": ("srvccd", "svccd", "bizcd", "servicecd", "사업코드"),
    "제공인력수": ("mnpwr", "manpower", "인력"),
    "이용자수": ("user", "대상자수", "이용자"),
}
_PHONE_RE = re.compile(r"0\d{1,2}-?\d{3,4}-?\d{4}")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


# ───────────────────────── 공통 유틸 ─────────────────────────
def _norm_key(k: str) -> str:
    return re.sub(r"[^0-9a-z가-힣]", "", str(k).lower())


def normalize_record(raw: dict) -> dict:
    """원시 dict → 표준 컬럼 + raw_* 전부."""
    out: dict = {}
    nk = {_norm_key(k): k for k in raw}
    used: set[str] = set()
    for col, hints in FIELD_HINTS.items():
        val = ""
        for h in hints:
            h2 = _norm_key(h)
            # 정확 일치 우선, 그다음 부분 일치
            cand = [k for k in nk if k == h2] or [k for k in nk if h2 in k and k not in used]
            if cand:
                v = raw.get(nk[cand[0]])
                if v not in (None, ""):
                    val = str(v).strip()
                    used.add(cand[0])
                    break
        out[col] = val
    # 값 패턴으로 보강 — 키 이름이 특이해도 전화/이메일은 값에서 알 수 있다
    if not out["전화"] or not out["이메일"]:
        for k, v in raw.items():
            if not isinstance(v, str):
                continue
            if not out["전화"] and _PHONE_RE.fullmatch(v.strip()) and "fax" not in k.lower():
                out["전화"] = v.strip()
            if not out["이메일"] and _EMAIL_RE.fullmatch(v.strip()):
                out["이메일"] = v.strip()
    for k, v in raw.items():
        out[f"raw_{k}"] = v
    return out


def record_matches_keyword(raw: dict, keyword: str) -> bool:
    return any(isinstance(v, str) and keyword in v for v in raw.values())


def find_dict_lists(obj, path="") -> list[tuple[str, list]]:
    """JSON 안에서 'dict 의 리스트' 를 전부 찾는다 (경로, 리스트). 가장 긴 것이 보통 items."""
    found = []
    if isinstance(obj, list):
        if obj and all(isinstance(x, dict) for x in obj):
            found.append((path, obj))
        for i, x in enumerate(obj):
            found += find_dict_lists(x, f"{path}[{i}]")
    elif isinstance(obj, dict):
        for k, v in obj.items():
            found += find_dict_lists(v, f"{path}.{k}" if path else k)
    return found


def find_key(obj, name_norm: str):
    """중첩 JSON 에서 키 이름(정규화)이 일치하는 첫 값을 찾는다."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if _norm_key(k) == name_norm:
                return v
        for v in obj.values():
            r = find_key(v, name_norm)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_key(v, name_norm)
            if r is not None:
                return r
    return None


def parse_api_body(text: str) -> tuple[list[dict], int | None, str]:
    """data.go.kr 계열 응답(JSON 또는 XML) → (items, totalCount, resultMsg)."""
    text = text.strip()
    if text.startswith("{") or text.startswith("["):
        data = json.loads(text)
        lists = find_dict_lists(data)
        items = max(lists, key=lambda t: len(t[1]))[1] if lists else []
        # item 이 하나면 dict 로 오는 API 도 있다
        if not items:
            single = find_key(data, "item")
            if isinstance(single, dict):
                items = [single]
        total = find_key(data, "totalcount")
        msg = find_key(data, "resultmsg") or find_key(data, "resultcode") or ""
        return items, (int(total) if str(total).isdigit() else None), str(msg)
    # XML
    root = ET.fromstring(text)
    items = []
    for it in root.iter():
        if it.tag.lower().endswith("item") and len(it):
            items.append({c.tag: (c.text or "").strip() for c in it})
    total_el = next((e for e in root.iter() if e.tag.lower().endswith("totalcount")), None)
    msg_el = next((e for e in root.iter() if e.tag.lower().endswith("resultmsg")), None)
    total = int(total_el.text) if total_el is not None and (total_el.text or "").strip().isdigit() else None
    return items, total, (msg_el.text or "") if msg_el is not None else ""


def save_json(path: Path, data):
    """0건이면 기존 산출물을 덮지 않는다. 있으면 .bak 로 물린다."""
    if not data:
        return
    if path.exists():
        shutil.copy(path, path.with_suffix(path.suffix + ".bak"))
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def make_outdir(domain: str) -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    d = REPO / "output" / domain / f"산모신생아_제공기관_{ts}"
    (d / "samples").mkdir(parents=True, exist_ok=True)
    return d


def robots_gate(url: str, limiter_delay: float) -> float:
    v = check_robots(url, user_agent="*")
    if v["error"]:
        logger.warning("robots.txt 확인 못 함 (허용됨과 다르다): %s", v["error"])
    elif not v["allowed"]:
        logger.error("robots.txt 가 %s 를 차단한다 — 사용자 확인 없이 진행하지 않는다. --ignore-robots 로 명시해야 진행", url)
        raise SystemExit(2)
    if v["crawl_delay"]:
        return max(float(v["crawl_delay"]), limiter_delay)
    return limiter_delay


# ───────────────────────── Track A: 공식 Open API ─────────────────────────
def crawl_api(args, outdir: Path) -> list[dict]:
    key = os.environ.get("DATA_GO_KR_SERVICE_KEY", "").strip()
    if not key:
        logger.error("DATA_GO_KR_SERVICE_KEY 환경변수가 비어 있다. data.go.kr 에서 활용신청 후 일반 인증키(Decoding)를 넣어라")
        raise SystemExit(2)

    limiter = RateLimiter(delay=args.delay, max_requests=args.max_requests)
    progress = ProgressTracker(str(outdir / "progress.json"))
    extra = dict(kv.split("=", 1) for kv in args.extra)
    base = {"serviceKey": key, "_type": "json", "type": "json", **extra}
    results: list[dict] = []
    seen: set[str] = set()

    with plain_session(headers={"User-Agent": USER_AGENT}) as session:
        # 1) 공통코드 — 산모신생아 사업코드 후보를 찾는다 (실패해도 계속)
        service_code = None
        try:
            limiter.wait()
            r = session.get(API_CODES, params=base, timeout=30)
            (outdir / "samples" / "codes_response.txt").write_text(r.text[:200_000], encoding="utf-8")
            items, _, msg = parse_api_body(r.text)
            logger.info("공통코드 API: status=%s 항목=%d msg=%s", r.status, len(items), msg)
            for it in items:
                if record_matches_keyword(it, KEYWORD):
                    logger.info("  산모 관련 코드 행: %s", it)
                    for k, v in it.items():
                        if "cd" in k.lower() or "code" in k.lower():
                            service_code = service_code or str(v)
            if service_code:
                logger.info("사업코드 후보: %s", service_code)
        except Exception as e:  # noqa: BLE001
            logger.warning("공통코드 API 실패(계속 진행): %s", e)

        # 2) 목록 — 필터 후보를 넣어 보고, 0건이면 필터 없이
        def fetch_page(page_no: int, use_filter: bool):
            params = {**base, "pageNo": page_no, "numOfRows": args.rows}
            if use_filter and service_code:
                for p in SERVICE_PARAM_CANDIDATES:
                    params[p] = service_code
            limiter.wait()
            r = session.get(API_LIST, params=params, timeout=60)
            if r.status == 429 or r.status == 503:
                limiter.backoff()
                raise RuntimeError(f"rate limited: {r.status}")
            if r.status != 200:
                raise RuntimeError(f"HTTP {r.status}: {r.text[:200]}")
            limiter.reset_errors()
            return r

        use_filter = bool(service_code) and not extra
        try:
            first = fetch_page(1, use_filter)
        except Exception as e:  # noqa: BLE001
            logger.error("목록 API 첫 요청 실패 — 네트워크/키/차단 여부를 확인하라: %s", e)
            progress.fail(f"first request failed: {e}")
            return []
        (outdir / "samples" / "list_page1_response.txt").write_text(first.text[:500_000], encoding="utf-8")
        items, total, msg = parse_api_body(first.text)
        logger.info("목록 1페이지: status=%s items=%d totalCount=%s msg=%s filter=%s", first.status, len(items), total, msg, use_filter)
        if not items and use_filter:
            logger.info("필터를 넣으니 0건 — 필터 없이 다시 받아서 클라이언트에서 거른다")
            use_filter = False
            first = fetch_page(1, False)
            items, total, msg = parse_api_body(first.text)
            logger.info("목록 1페이지(무필터): items=%d totalCount=%s msg=%s", len(items), total, msg)
        if not items:
            logger.error("수집 데이터 0건 — 즉시 중단. samples/list_page1_response.txt 를 확인하라 (키 오류·파라미터 오류 메시지가 들어 있을 수 있다)")
            progress.fail("0 items on page 1")
            return []
        logger.info("원시 필드: %s", list(items[0].keys()))

        # 무필터로 받는 경우 응답에 산모 글자가 아예 없으면(코드만 오는 API) 거르지 않고 전부 남긴다
        keyword_present = any(record_matches_keyword(it, KEYWORD) for it in items)
        client_filter = (not use_filter) and keyword_present and not args.no_filter
        if not use_filter and not keyword_present:
            logger.warning("응답 어디에도 '%s' 가 없다 — 사업명이 코드로만 오는 것 같다. 전부 저장하니 사업코드 컬럼으로 직접 걸러라", KEYWORD)

        def absorb(page_items):
            n = 0
            for it in page_items:
                if client_filter and not record_matches_keyword(it, KEYWORD):
                    continue
                fp = json.dumps(it, ensure_ascii=False, sort_keys=True)
                if fp in seen:
                    continue
                seen.add(fp)
                results.append(normalize_record(it))
                n += 1
            return n

        absorb(items)
        pages = 1
        if total:
            pages = -(-total // args.rows)
        if args.test:
            pages = 1
        logger.info("총 %s건 → %d페이지 (rows=%d)", total, pages, args.rows)

        consecutive_errors = 0
        for page_no in range(2, pages + 1):
            try:
                r = fetch_page(page_no, use_filter)
                page_items, _, _ = parse_api_body(r.text)
                if not page_items:
                    logger.info("page %d 빈 응답 — 끝으로 간주", page_no)
                    break
                absorb(page_items)
                consecutive_errors = 0
                progress.update(collected=len(results), current_page=page_no)
                if len(results) % 100 < args.rows:
                    save_json(outdir / "raw_data.json", results)
            except BudgetExceeded as e:
                logger.error("%s", e)
                break
            except Exception as e:  # noqa: BLE001
                consecutive_errors += 1
                progress.add_error(f"page {page_no}: {e}")
                logger.warning("page %d 실패(%d/5): %s", page_no, consecutive_errors, e)
                if consecutive_errors >= 5:
                    logger.error("5회 연속 실패 — 중단")
                    break
                continue

    progress.update(collected=len(results))
    progress.complete()
    return results


# ───────────────────────── Track B: 누리집 검색 (정적 HTML) ─────────────────────────
def _options(sel) -> list[tuple[str, str]]:
    return [(o.attrib.get("value", ""), o.get_all_text(strip=True)) for o in sel.css("option")]


def discover_form(page):
    """검색 폼에서 action/method/hidden/시도 select/사업 select 를 찾는다."""
    forms = page.css("form")
    best = None
    for f in forms:
        selects = f.css("select")
        if not selects:
            continue
        info = {"action": f.attrib.get("action") or "", "method": (f.attrib.get("method") or "get").lower(),
                "hidden": {}, "sido": None, "service": None, "selects": {}}
        for h in f.css("input[type=hidden]"):
            if h.attrib.get("name"):
                info["hidden"][h.attrib["name"]] = h.attrib.get("value", "")
        for s in selects:
            name = s.attrib.get("name") or s.attrib.get("id") or ""
            opts = _options(s)
            info["selects"][name] = opts
            texts = " ".join(t for _, t in opts)
            if info["service"] is None and KEYWORD in texts:
                info["service"] = (name, [v for v, t in opts if KEYWORD in t and v])
            if info["sido"] is None and ("서울" in texts and ("부산" in texts or "경기" in texts)):
                info["sido"] = (name, [(v, t) for v, t in opts if v])
        if info["service"] or info["sido"]:
            best = info
            break
    return best


def parse_rows(page, base_url: str) -> list[dict]:
    rows = []
    for tr in page.css("table tbody tr, table tr"):
        tds = tr.css("td")
        if not tds:
            continue
        cells = [td.get_all_text(strip=True) for td in tds]
        rec = {f"col{i+1}": c for i, c in enumerate(cells)}
        link = ""
        for a in tr.css("a"):
            href = a.attrib.get("href", "") or ""
            onclick = a.attrib.get("onclick", "") or ""
            if "supplyView" in href:
                link = urljoin(base_url, href)
                break
            m = re.search(r"(\d{6,})", onclick + href)
            if m and ("View" in onclick or "view" in onclick or "detail" in onclick.lower()):
                link = f"{WEB_DETAIL}?pb={m.group(1)}&v=m"
                break
        rec["detail_url"] = link
        rows.append(rec)
    return rows


def parse_detail(page) -> dict:
    """th/td · dt/dd 쌍을 라벨→값 dict 로."""
    out = {}
    for th in page.css("th"):
        td = th.xpath("following-sibling::td[1]")
        if td:
            out[th.get_all_text(strip=True)] = td[0].get_all_text(strip=True)
    for dt in page.css("dt"):
        dd = dt.xpath("following-sibling::dd[1]")
        if dd:
            out[dt.get_all_text(strip=True)] = dd[0].get_all_text(strip=True)
    text = page.get_all_text(separator="\n", strip=True) if hasattr(page, "get_all_text") else ""
    if text:
        m = _EMAIL_RE.search(text)
        if m and not any("메일" in k or "mail" in k.lower() for k in out):
            out["이메일(본문추출)"] = m.group(0)
    return out


def crawl_web(args, outdir: Path) -> list[dict]:
    delay = robots_gate(WEB_LIST, args.delay) if not args.ignore_robots else args.delay
    limiter = RateLimiter(delay=delay, max_requests=args.max_requests)
    progress = ProgressTracker(str(outdir / "progress.json"))
    results: list[dict] = []

    with plain_session(headers={"User-Agent": USER_AGENT}) as session:
        try:
            limiter.wait()
            first = session.get(WEB_LIST, timeout=60)
        except Exception as e:  # noqa: BLE001
            logger.error("검색 페이지 첫 요청 실패 — 네트워크/차단 여부를 확인하라: %s", e)
            progress.fail(f"first request failed: {e}")
            return []
        (outdir / "samples" / "list_form_page.html").write_text(first.text, encoding="utf-8")
        verdict = detect_softblock(first.text, status=first.status, selector_hit=bool(first.css("form")))
        if verdict["blocked"]:
            logger.error("소프트블록 감지 — %s: %s. 사다리 B 진입은 통지가 필요하므로 여기서 멈춘다", verdict["verdict"], verdict["signals"])
            return []
        form = discover_form(first)
        if not form:
            logger.error("검색 폼을 찾지 못했다 — samples/list_form_page.html 을 확인하라 (JS 렌더링이면 plain_dynamic 으로 에스컬레이션 필요)")
            return []
        logger.info("폼: action=%s method=%s hidden=%s", form["action"], form["method"], list(form["hidden"]))
        logger.info("select 목록: %s", {k: len(v) for k, v in form["selects"].items()})
        logger.info("사업 select: %s / 시도 select: %s", form["service"], (form["sido"][0] if form["sido"] else None))
        (outdir / "samples" / "form_discovered.json").write_text(json.dumps(form, ensure_ascii=False, indent=2), encoding="utf-8")

        action = urljoin(WEB_LIST, form["action"]) if form["action"] else WEB_LIST
        sidos = form["sido"][1] if form["sido"] else [("", "전체")]
        if args.test:
            sidos = sidos[:1]
        seen_links: set[str] = set()
        list_rows: list[dict] = []
        consecutive_errors = 0

        for sido_val, sido_nm in sidos:
            for page_no in range(1, (2 if args.test else 200) + 1):
                params = dict(form["hidden"])
                if form["sido"]:
                    params[form["sido"][0]] = sido_val
                if form["service"]:
                    params[form["service"][0]] = form["service"][1][0]
                for pn in ("pageIndex", "pageNo", "page", "currentPage"):
                    params[pn] = page_no
                try:
                    limiter.wait()
                    if form["method"] == "post":
                        r = session.post(action, data=params, timeout=60)
                    else:
                        r = session.get(action, params=params, timeout=60)
                    if r.status in (429, 503):
                        limiter.backoff()
                        raise RuntimeError(f"rate limited {r.status}")
                    if r.status != 200:
                        raise RuntimeError(f"HTTP {r.status}")
                    limiter.reset_errors()
                    if page_no == 1 and sido_val == sidos[0][0]:
                        (outdir / "samples" / "list_result_page1.html").write_text(r.text, encoding="utf-8")
                    rows = parse_rows(r, action)
                    new = [x for x in rows if x["detail_url"] and x["detail_url"] not in seen_links]
                    if not rows or (not new and page_no > 1):
                        break
                    for x in new:
                        x["시도(검색조건)"] = sido_nm
                        seen_links.add(x["detail_url"])
                        list_rows.append(x)
                    consecutive_errors = 0
                    logger.info("[%s] page %d: rows=%d new=%d 누적=%d", sido_nm, page_no, len(rows), len(new), len(list_rows))
                except BudgetExceeded as e:
                    logger.error("%s", e)
                    break
                except Exception as e:  # noqa: BLE001
                    consecutive_errors += 1
                    progress.add_error(f"{sido_nm} p{page_no}: {e}")
                    logger.warning("[%s] page %d 실패(%d/5): %s", sido_nm, page_no, consecutive_errors, e)
                    if consecutive_errors >= 5:
                        break
                    continue

        if not list_rows:
            logger.error("목록 0건 — 즉시 중단. samples/list_result_page1.html 을 확인하라")
            return []

        # 상세 페이지 (전화/이메일/팩스)
        targets = list_rows[:5] if args.test else list_rows
        for i, row in enumerate(targets, 1):
            raw = dict(row)
            try:
                limiter.wait()
                d = session.get(row["detail_url"], timeout=60)
                if i == 1:
                    (outdir / "samples" / "detail_page1.html").write_text(d.text, encoding="utf-8")
                if d.status == 200:
                    raw.update(parse_detail(d))
                else:
                    progress.add_error(f"detail {row['detail_url']}: HTTP {d.status}")
            except BudgetExceeded as e:
                logger.error("%s", e)
                results.append(normalize_record(raw))
                break
            except Exception as e:  # noqa: BLE001
                progress.add_error(f"detail {row['detail_url']}: {e}")
                logger.warning("상세 실패: %s", e)
            results.append(normalize_record(raw))
            progress.update(collected=len(results), current_page=i)
            if i % 100 == 0:
                save_json(outdir / "raw_data.json", results)

    progress.complete()
    return results


# ───────────────────────── 검증 · 저장 · 프로필 ─────────────────────────
def finish(args, outdir: Path, results: list[dict], started: float):
    if not results:
        logger.error("결과 0건 — 엑셀/프로필을 만들지 않는다")
        return 1
    save_json(outdir / "raw_data.json", results)

    for w in detect_pii(results):
        logger.warning("PII: %s", w)
    issues = validate_values(results, {
        "기관명": {"type": "str", "required": True, "max_empty_ratio": 0.1},
        "주소":   {"type": "str", "required": False, "max_empty_ratio": 0.3},
        "사업명": {"type": "str", "required": False, "allow_uniform": True},
    })
    for it in issues:
        logger.warning("값 검증: %s", it)

    cols = [c for c in FIELD_HINTS]
    fill = {c: round(100 * sum(1 for r in results if r.get(c)) / len(results)) for c in cols}
    logger.info("필드 채움률(%%): %s", fill)
    xlsx = outdir / "crawl_result.xlsx"
    export_to_excel(results, str(xlsx))
    logger.info("엑셀: %s (%d건, %.0f초)", xlsx, len(results), time.time() - started)

    # Step 5-A — 성공했을 때만 프로필 저장
    if args.test:
        logger.info("--test 실행이라 profile.json 은 갱신하지 않는다 (전량 수집 성공 후 저장)")
        return 0
    domain = API_DOMAIN if args.mode == "api" else WEB_DOMAIN
    profile = {
        "domain": domain,
        "capability": "api" if args.mode == "api" else "static",
        "fetcher_type": "FetcherSession" if args.mode == "api" else "Fetcher",
        "antibot_type": "none",
        "antibot_strategy": "none",
        "site_type": "api" if args.mode == "api" else "static",
        "selectors": {} if args.mode == "api" else {"list_rows": "table tbody tr", "detail_pairs": "th+td / dt+dd", "detail_link": "a[href*='supplyView']"},
        "pagination": {"type": "query_param", "param": "pageNo" if args.mode == "api" else "pageIndex", "per_page": args.rows if args.mode == "api" else None},
        "api_endpoints": ([{"url": API_LIST, "method": "GET", "params": {"serviceKey": "<env DATA_GO_KR_SERVICE_KEY>", "pageNo": 1, "numOfRows": args.rows, "_type": "json"}, "field_mapping": FIELD_HINTS},
                           {"url": API_CODES, "method": "GET", "params": {"serviceKey": "<env>"}, "field_mapping": {}}]
                          if args.mode == "api" else []),
        "notes": ("data.go.kr 15057683 공식 Open API(사다리 0단). serviceKey 는 env 로만, 프로필에 박지 말 것. "
                  "사업구분 필터 파라미터명은 jobs/sanmo_provider/crawl_script.py SERVICE_PARAM_CANDIDATES 참고 — 응답 필드명은 output/.../samples/list_page1_response.txt 에서 확정."
                  if args.mode == "api" else
                  "전자바우처 누리집 제공기관 검색(supplyList.do) 정적 폼 → 상세(supplyViewBokji.do?pb=). 폼 파라미터는 output/.../samples/form_discovered.json 참고."),
        "last_used": str(date.today()),
    }
    try:
        DomainProfile(base_dir=str(REPO / "fingerprints")).save(domain, profile)
        logger.info("profile.json 저장: fingerprints/%s/profile.json — 이어서 `python scripts/sync_domain_list.py` 실행", sanitize_filename(domain))
    except Exception as e:  # noqa: BLE001
        logger.error("profile.json 저장 실패 — 파이프라인 미완료: %s", e)
        return 1
    return 0


# ───────────────────────── 네트워크 없는 자가 검증 ─────────────────────────
def selftest() -> int:
    json_body = json.dumps({"response": {"header": {"resultCode": "00", "resultMsg": "NORMAL SERVICE."},
                                         "body": {"items": {"item": [
                                             {"provNm": "행복산후도우미", "sidoNm": "서울특별시", "sggNm": "강남구", "addr": "서울 강남구 테헤란로 1",
                                              "telNo": "02-123-4567", "faxNo": "02-123-4568", "email": "info@example.com", "srvcNm": "산모신생아건강관리지원사업"},
                                             {"provNm": "노인돌봄센터", "sidoNm": "부산광역시", "sggNm": "해운대구", "addr": "부산 해운대구 1",
                                              "telNo": "051-111-2222", "faxNo": "", "email": "", "srvcNm": "노인맞춤돌봄"}]},
                                                  "numOfRows": 100, "pageNo": 1, "totalCount": 2}}}, ensure_ascii=False)
    items, total, msg = parse_api_body(json_body)
    assert len(items) == 2 and total == 2, (items, total)
    rec = normalize_record(items[0])
    assert rec["기관명"] == "행복산후도우미" and rec["전화"] == "02-123-4567" and rec["팩스"] == "02-123-4568" and rec["이메일"] == "info@example.com", rec
    assert rec["시도"] == "서울특별시" and rec["시군구"] == "강남구" and rec["사업명"].startswith("산모"), rec
    assert record_matches_keyword(items[0], KEYWORD) and not record_matches_keyword(items[1], KEYWORD)
    xml_body = """<response><header><resultCode>00</resultCode><resultMsg>OK</resultMsg></header><body>
      <items><item><provNm>가나산후조리</provNm><addr>경기 성남시</addr><telNo>031-222-3333</telNo></item></items>
      <totalCount>1</totalCount></body></response>"""
    items, total, _ = parse_api_body(xml_body)
    assert total == 1 and normalize_record(items[0])["전화"] == "031-222-3333", items
    # 키 이름이 낯설어도 값 패턴으로 전화/이메일을 찾는다
    rec = normalize_record({"x1": "홍길동산후", "x2": "02-999-8888", "x3": "a@b.co.kr"})
    assert rec["전화"] == "02-999-8888" and rec["이메일"] == "a@b.co.kr", rec
    # HTML 폼/행 파서
    from scrapling.parser import Selector
    html = """<form action="/user/svcsrch/supply/supplyList.do" method="post"><input type="hidden" name="menuId" value="M1">
      <select name="sidoCd"><option value="">전체</option><option value="11">서울특별시</option><option value="26">부산광역시</option></select>
      <select name="svcCd"><option value="">사업선택</option><option value="S02">산모신생아 건강관리 지원사업</option></select></form>
      <table><tbody><tr><td>1</td><td><a href="/user/svcsrch/supply/bokji/supplyViewBokji.do?pb=123456789&v=m">행복산후</a></td><td>02-1-2</td></tr></tbody></table>"""
    page = Selector(html)
    form = discover_form(page)
    assert form and form["method"] == "post" and form["sido"][0] == "sidoCd" and form["service"] == ("svcCd", ["S02"]), form
    rows = parse_rows(page, WEB_LIST)
    assert rows and rows[0]["detail_url"].endswith("pb=123456789&v=m"), rows
    det = parse_detail(Selector("<table><tr><th>전화번호</th><td>02-333-4444</td></tr><tr><th>이메일</th><td>x@y.kr</td></tr></table>"))
    assert det.get("전화번호") == "02-333-4444" and det.get("이메일") == "x@y.kr", det
    print("selftest OK")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=["api", "web"], default="api")
    ap.add_argument("--test", action="store_true", help="API 1페이지 / 누리집 1개 시도 2페이지+상세 5건")
    ap.add_argument("--rows", type=int, default=100, help="API numOfRows")
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--max-requests", type=int, default=3000)
    ap.add_argument("--extra", action="append", default=[], help="API 추가 파라미터 key=value (여러 번 가능)")
    ap.add_argument("--no-filter", action="store_true", help="'산모' 키워드 클라이언트 필터 끄기")
    ap.add_argument("--ignore-robots", action="store_true", help="robots.txt 차단을 사용자 책임으로 넘어감")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        raise SystemExit(selftest())

    started = time.time()
    outdir = make_outdir(API_DOMAIN if args.mode == "api" else WEB_DOMAIN)
    logger.info("출력 폴더: %s", outdir)
    results = crawl_api(args, outdir) if args.mode == "api" else crawl_web(args, outdir)
    raise SystemExit(finish(args, outdir, results, started))


if __name__ == "__main__":
    main()
