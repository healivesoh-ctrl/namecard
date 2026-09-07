"""명령줄 도구.

  python -m alimtalk.cli check                          # 설정·발송사 연결 확인
  python -m alimtalk.cli send --phone 010-1234-5678 --message 접수확인 --var 이름=홍길동
  python -m alimtalk.cli intake --form default --json '{"이름":"홍길동","연락처":"010-1234-5678"}'
  python -m alimtalk.cli import --form default responses.csv    # 구글시트 내려받은 CSV 일괄 접수
  python -m alimtalk.cli worker                         # 예약 발송 워커 (Ctrl+C 종료)
  python -m alimtalk.cli run-due                        # 지금 보낼 것만 처리하고 종료 (cron 용)
  python -m alimtalk.cli jobs [--status pending]
  python -m alimtalk.cli optout add 010-1234-5678
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from .config import BASE_DIR, load_config
from .models import Job
from .phone import format_phone, normalize_phone
from .providers import get_provider
from .scheduler import Worker
from .service import deliver, intake, run_due
from .store import Store


def _fmt(dt: datetime | None, tz: str) -> str:
    return dt.astimezone(ZoneInfo(tz)).strftime("%m-%d %H:%M") if dt else "-"


def cmd_check(args, cfg, store):
    print(f"발송사: {cfg.provider}   발신번호: {cfg.sender or '(미설정)'}   채널: {cfg.kakao_channel or '(미설정)'}")
    print(f"시간대: {cfg.timezone}   야간금지: {cfg.quiet_hours or '없음'}   DB: {store.path}")
    for f in cfg.forms:
        print(f"- 폼 '{f.id}' (동의필수={f.require_consent}, 중복제외={f.dedupe_hours}h)")
        for m in f.messages:
            print(f"    · {m.name}: 템플릿={m.template} when={m.when}")
    try:
        p = get_provider(cfg.provider)
        print(f"발송사 연결 준비 OK ({p.name})")
    except Exception as e:  # noqa: BLE001
        print(f"발송사 설정 오류: {e}")
        return 1
    return 0


def cmd_send(args, cfg, store):
    phone = normalize_phone(args.phone)
    if not phone:
        print("휴대폰 번호 형식이 아닙니다.")
        return 1
    variables = dict(kv.split("=", 1) for kv in args.var or [])
    form = cfg.form(args.form)
    rule = next((m for m in (form.messages if form else []) if m.name == args.message), None)
    if rule is None and not args.template:
        print("--message 이름이 rules.yaml 에 없습니다. --template 으로 직접 지정하세요.")
        return 1
    from .templates import builtin_vars, render, render_variables
    now = datetime.now(ZoneInfo(cfg.timezone))
    answers = dict(variables)
    builtins = builtin_vars(variables.get("이름", variables.get("name", "")), phone, now)
    job = Job(id=None, submission_id=None, form_id=form.id if form else "manual",
              message_name=args.message or "manual", phone=phone,
              template_code=args.template or rule.template,
              variables=render_variables(rule.variables, answers, builtins) if rule else variables,
              text=render(rule.text, answers, builtins) if rule else args.text,
              subject=render(rule.subject, answers, builtins) if rule else None,
              scheduled_at=now)
    store.add_job(job)
    res = deliver(cfg, store, get_provider(cfg.provider), job)
    print(json.dumps(res.to_dict(), ensure_ascii=False, indent=2))
    return 0 if res.ok else 1


def cmd_intake(args, cfg, store):
    answers = json.loads(args.json)
    r = intake(cfg, store, args.form, answers, source="cli")
    print(json.dumps(r.to_dict(), ensure_ascii=False, indent=2))
    return 0 if r.accepted else 1


def cmd_import(args, cfg, store):
    path = Path(args.file)
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp949")
    rows = list(csv.DictReader(text.splitlines()))
    ok = 0
    for i, row in enumerate(rows, 1):
        r = intake(cfg, store, args.form, row, source=f"csv:{path.name}")
        ok += r.accepted
        print(f"{i:>4}. {format_phone(r.phone) if r.phone else '-':<14} {'접수' if r.accepted else '생략'} {r.reason}"
              + (f" → {len(r.jobs)}건 예약" if r.accepted else ""))
    print(f"총 {len(rows)}행 중 {ok}건 접수")
    return 0


def cmd_worker(args, cfg, store):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    w = Worker(cfg, store, get_provider(cfg.provider), interval=args.interval)
    print(f"워커 시작 (발송사={cfg.provider}, {args.interval}s 간격). Ctrl+C 로 종료.")
    try:
        w._loop()
    except KeyboardInterrupt:
        pass
    return 0


def cmd_run_due(args, cfg, store):
    results = run_due(cfg, store, get_provider(cfg.provider))
    for job, res in results:
        print(f"job#{job.id} {job.message_name} {format_phone(job.phone)} → {'OK' if res.ok else 'FAIL ' + str(res.error)}")
    print(f"{len(results)}건 처리")
    return 0


def cmd_jobs(args, cfg, store):
    jobs = store.list_jobs(args.status, args.limit)
    print(f"{'id':>5} {'상태':<9} {'예정':<12} {'메시지':<14} {'수신':<14} 결과")
    for j in jobs:
        err = (j.result or {}).get("error") or (j.result or {}).get("message_id") or ""
        print(f"{j.id:>5} {j.status:<9} {_fmt(j.scheduled_at, cfg.timezone):<12} {j.message_name:<14} "
              f"{format_phone(j.phone):<14} {err}")
    return 0


def cmd_optout(args, cfg, store):
    if args.action == "list":
        for o in store.list_optouts():
            print(f"{format_phone(o['phone'])}  {o['created_at']}  {o.get('reason') or ''}")
        return 0
    phone = normalize_phone(args.phone)
    if not phone:
        print("휴대폰 번호 형식이 아닙니다.")
        return 1
    if args.action == "add":
        store.add_optout(phone, args.reason or "cli")
        print(f"{format_phone(phone)} 수신거부 등록 (대기 중 예약 취소됨)")
    else:
        store.remove_optout(phone)
        print(f"{format_phone(phone)} 수신거부 해제")
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv(BASE_DIR / ".env")
    p = argparse.ArgumentParser(prog="alimtalk", description="설문폼 → 카카오 알림톡 자동 발송")
    p.add_argument("--rules", help="rules.yaml 경로")
    p.add_argument("--db", help="SQLite 경로")
    sp = p.add_subparsers(dest="cmd", required=True)

    sp.add_parser("check").set_defaults(fn=cmd_check)
    s = sp.add_parser("send"); s.set_defaults(fn=cmd_send)
    s.add_argument("--phone", required=True); s.add_argument("--form", default="default")
    s.add_argument("--message", help="rules.yaml 의 메시지 이름"); s.add_argument("--template", help="템플릿 코드 직접 지정")
    s.add_argument("--text", help="완성 본문 (템플릿 직접 지정 시)"); s.add_argument("--var", action="append", help="변수 이름=값")
    s = sp.add_parser("intake"); s.set_defaults(fn=cmd_intake)
    s.add_argument("--form", default="default"); s.add_argument("--json", required=True)
    s = sp.add_parser("import"); s.set_defaults(fn=cmd_import)
    s.add_argument("file"); s.add_argument("--form", default="default")
    s = sp.add_parser("worker"); s.set_defaults(fn=cmd_worker); s.add_argument("--interval", type=float, default=15)
    sp.add_parser("run-due").set_defaults(fn=cmd_run_due)
    s = sp.add_parser("jobs"); s.set_defaults(fn=cmd_jobs)
    s.add_argument("--status"); s.add_argument("--limit", type=int, default=50)
    s = sp.add_parser("optout"); s.set_defaults(fn=cmd_optout)
    s.add_argument("action", choices=["add", "remove", "list"]); s.add_argument("phone", nargs="?"); s.add_argument("--reason")

    args = p.parse_args(argv)
    cfg = load_config(args.rules)
    store = Store(args.db)
    return args.fn(args, cfg, store)


if __name__ == "__main__":
    sys.exit(main())
