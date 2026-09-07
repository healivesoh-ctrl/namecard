# 설문폼 → 카카오 알림톡 자동 발송

구글폼·Tally·Typeform 등 설문폼으로 **전화번호를 수집하면, 그 번호로 카카오 알림톡을 자동으로
예약·발송**하는 시스템입니다. 접수 즉시 확인 메시지, 하루 뒤 후속 안내, 수업일 전날 리마인드처럼
"언제, 어떤 조건에, 어떤 템플릿을" 보낼지 규칙으로 정해 두면 그대로 동작합니다.

```
설문폼 응답 (전화번호 포함)
   │
   ├─ [A] 구글폼 ──► 응답 스프레드시트 ──► Apps Script (apps-script/Code.gs)
   │                                          │  서버 없이 발송사 API 직접 호출, 예약은 시트에 저장
   │                                          ▼
   └─ [B] Tally / Typeform / Zapier / 구글폼 ─► 웹훅 서버 (server/) ─► rules.yaml 규칙 ─► SQLite 예약 ─► 워커
                                                                                                │
                                                     발송사 API (솔라피 / 알리고) ◄──────────────┘
                                                            │
                                                     카카오 알림톡 ─(실패 시)─► 문자(SMS/LMS) 대체 발송
```

| 방식 | 언제 쓰나 | 필요한 것 |
|---|---|---|
| **A. Apps Script** (권장) | 구글폼만 쓰고, 서버 운영 없이 끝내고 싶을 때 | 구글 계정 + 발송사 계정 |
| **B. 웹훅 서버** | 구글폼 외 설문 서비스, 여러 폼 통합 관리, 대시보드·명단 일괄 발송·수신거부 관리가 필요할 때 | Render 등 호스팅 (이 리포의 `render.yaml` 로 바로 배포) |

두 방식 모두 같은 규칙 문법(즉시 / 지연 / 특정 일시 / 폼 날짜 기준 D-n / 답변 조건 / 야간 발송 금지)을 씁니다.

---

## 0. 먼저 알아둘 것 — 알림톡은 "사전 승인된 템플릿"만 보낼 수 있습니다

카카오 알림톡은 개인 카카오톡이 아니라 **카카오톡 채널(비즈니스)** 에서 나가는 메시지이고,
카카오 공식 대행사(발송사)를 통해서만 보낼 수 있습니다. 준비 순서:

1. **카카오톡 채널 개설** — [카카오 비즈니스](https://business.kakao.com) → 채널 만들기 → 비즈니스 채널 전환(사업자 등록 필요)
2. **발송사 가입** — 아래 둘 중 하나. 둘 다 카카오 공식 딜러사이고 이 시스템이 지원합니다.
   - [솔라피(SOLAPI)](https://solapi.com) — API 가 깔끔하고 템플릿 변수를 서버에서 치환. 이 문서의 기본값
   - [알리고(aligo)](https://smartsms.aligo.in) — 저렴, 국내 소상공인이 많이 씀. 완성된 본문을 그대로 보내는 방식
3. **발송사에서 카카오 채널 연동** → 채널 ID(솔라피 `pfId` / 알리고 `senderkey`) 발급
4. **알림톡 템플릿 등록 → 카카오 검수(영업일 1~3일)** — 승인된 템플릿 코드가 규칙의 `template` 값입니다.
   변수는 `#{이름}` 처럼 적습니다. 예:
   ```
   #{이름}님, #{과정명} 신청이 접수되었습니다.
   담당자가 확인 후 연락드리겠습니다. 감사합니다.
   ```
5. **발신번호 등록** — 알림톡 실패 시 문자로 대체 발송하려면 발송사에 발신번호 인증(통신서비스 이용증명원)
6. API 키 발급 (솔라피: API Key/Secret, 알리고: API Key + 아이디)

> **정보성 vs 광고성** — 접수 확인·예약 안내·일정 리마인드 같은 **정보성 알림톡은 수신 동의 없이** 보낼 수 있습니다
> (다만 개인정보 수집 시 "안내 메시지 발송 목적" 을 개인정보 수집·이용 동의 문구에 넣어 두세요).
> 할인·이벤트 등 **광고성 메시지는 사전 수신 동의가 필수**이고 `(광고)` 표기·수신거부 방법 안내가 있어야 합니다.
> 광고성이면 폼에 "수신 동의" 항목을 넣고 `require_consent: true` 로 두세요.

---

## A. Apps Script 방식 (구글폼 전용, 서버 없음)

### 설치

1. 구글폼 → **응답** 탭 → 스프레드시트 아이콘 → 응답 스프레드시트 만들기
2. 그 스프레드시트에서 **확장 프로그램 → Apps Script** → 기본 `Code.gs` 내용을 지우고
   [`apps-script/Code.gs`](apps-script/Code.gs) 전체를 붙여넣기
3. 상단 `CONFIG` 수정
   - `MODE`: `"solapi"` 또는 `"aligo"` (또는 서버로 넘기는 `"webhook"`)
   - `SENDER`: 발신번호, `TEST_PHONE`: 본인 번호
   - 발송사 키 — 코드에 직접 넣어도 되지만, **프로젝트 설정(⚙) → 스크립트 속성**에
     `SOLAPI_API_KEY`, `SOLAPI_API_SECRET`, `SOLAPI_PF_ID` (알리고: `ALIGO_API_KEY`, `ALIGO_USER_ID`, `ALIGO_SENDER_KEY`)
     로 넣는 것을 권장합니다 (시트 편집 권한자에게 키가 노출되지 않음)
   - `MESSAGES`: 발송 규칙 (아래 [규칙 문법](#규칙-문법) 참고)
4. 함수 선택 드롭다운에서 **`setup`** 선택 → ▶ 실행 → 권한 승인
   → 폼 제출 트리거 + 5분마다 예약 발송 트리거가 만들어지고 `알림톡_대기`, `알림톡_로그` 시트가 생깁니다
5. **`testSend`** 실행 → `TEST_PHONE` 으로 시험 발송 → `알림톡_로그` 시트에서 결과 확인
6. (선택) 프로젝트 설정에서 **시간대를 Asia/Seoul** 로 확인

이후 폼이 제출될 때마다 자동으로 동작합니다.

### 시트 구성

| 시트 | 내용 |
|---|---|
| `알림톡_대기` | 예약된 발송 (예정시각·상태·본문). 행을 지우거나 상태를 `취소` 로 바꾸면 발송되지 않음 |
| `알림톡_로그` | 접수·발송·오류 기록 전체 |
| `수신거부` (직접 생성) | A열에 번호를 적어 두면 그 번호는 제외 |

기존 응답(설치 전 응답)에도 보내려면 Apps Script 에서 `intakeRow(행번호)` 를 실행하거나,
B 방식의 "명단 일괄 접수" 를 쓰면 됩니다.

---

## B. 웹훅 서버 방식 (모든 설문폼 + 대시보드)

### 1) 로컬 실행

```bash
cd kakao-alimtalk
pip install -r requirements.txt
cp .env.example .env            # WEBHOOK_SECRET, ADMIN_PASSWORD, 발송사 키 입력
cp rules.example.yaml rules.yaml  # 발송 규칙 편집

python -m alimtalk.cli check    # 설정 확인
uvicorn server.app:app --reload --port 8000
# → http://localhost:8000  대시보드 (ADMIN_PASSWORD 로 로그인)
```

발송사를 정하기 전에는 `ALIMTALK_PROVIDER=console` (기본값) 로 두면 실제 발송 없이 로그만 남습니다.
발송사 설정 후에도 `ALIMTALK_DRY_RUN=1` 을 켜면 리허설이 가능합니다.

### 2) Render 배포

리포 루트의 `render.yaml` 에 `kakao-alimtalk` 서비스가 정의되어 있습니다.
Render → New → Blueprint → 이 리포 연결 → 환경변수(`sync: false` 항목) 입력.

> **예약 발송을 잃지 않으려면 디스크가 필요합니다.** 무료 플랜은 재배포·재시작 시 파일이 초기화되므로
> 대기 중인 예약(SQLite)이 사라집니다. Render 의 **Disk** 를 `/app/data` 에 붙이거나(유료),
> `DB_PATH` 를 영구 저장소로 지정하세요. 즉시 발송만 쓴다면 무료 플랜으로 충분합니다.
> 예약을 시트에 보관하는 A 방식은 이 문제가 없습니다.

### 3) 설문폼 연결

웹훅 주소: `https://<서버>/webhook/form?form=<폼id>&secret=<WEBHOOK_SECRET>`
(비밀값은 `X-Webhook-Secret` 헤더로 보내도 됩니다)

| 설문 서비스 | 방법 |
|---|---|
| **구글폼** | A 방식의 `Code.gs` 를 `MODE: "webhook"` 으로 설치. 규칙은 서버 `rules.yaml` 이 담당 |
| **Tally** | 폼 → Integrations → Webhooks → 위 주소 등록 (페이로드 형식 자동 인식) |
| **Typeform** | 폼 → Connect → Webhooks → 위 주소 등록 |
| **네이버폼·카카오폼·기타** | 응답을 구글시트로 모은 뒤 Apps Script 로 전달하거나, Zapier/Make 에서 `{"이름":"…","연락처":"…"}` 형태로 POST |
| **직접 호출** | `POST {"form":"default","answers":{"이름":"홍길동","연락처":"010-1234-5678","수업일":"2026-09-20"}}` |

전화번호 항목은 질문 제목에 `전화번호/휴대폰/연락처/핸드폰/phone` 등이 들어 있으면 자동으로 찾고,
못 찾으면 답변 중 휴대폰 번호 형태인 값을 씁니다. `010-1234-5678`, `+82 10-…`, 엑셀이 앞 0 을 지운
`1012345678` 모두 인식합니다.

### 4) 대시보드

- **발송 현황** — 대기/발송됨/실패 목록, 예약 취소·즉시 발송·재시도
- **테스트 발송** — 규칙의 메시지를 골라 본인 번호로 발송
- **명단 일괄 접수** — 구글시트/엑셀 범위를 복사해 붙여넣거나 CSV 업로드 → 각 행이 폼 응답처럼 규칙대로 예약
  (이미 받아 둔 응답에 소급 발송할 때, 또는 설문 도구가 웹훅을 지원하지 않을 때)
- **수신거부** — 번호 등록 시 대기 중 예약 취소 + 이후 접수 제외

### 5) CLI

```bash
python -m alimtalk.cli check                                  # 설정·발송사 확인
python -m alimtalk.cli send --phone 010-1234-5678 --message 접수확인 --var 이름=홍길동
python -m alimtalk.cli intake --json '{"이름":"홍길동","연락처":"010-1234-5678"}'
python -m alimtalk.cli import 응답.csv --form default         # 구글시트 내려받은 CSV 일괄 접수
python -m alimtalk.cli jobs --status pending                  # 예약 목록
python -m alimtalk.cli optout add 010-1234-5678
python -m alimtalk.cli worker                                 # 예약 발송 워커 (서버 대신 PC 에서 돌릴 때)
python -m alimtalk.cli run-due                                # cron 에서 5분마다 실행하는 용도
```

### 6) 테스트

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

---

## 규칙 문법

`rules.yaml`(서버) 과 `CONFIG.MESSAGES`(Apps Script) 는 같은 개념입니다. 전체 예시는
[`rules.example.yaml`](rules.example.yaml) 참고.

```yaml
forms:
  - id: default              # 웹훅 ?form=<id>. 없으면 default
    require_consent: false   # 광고성이면 true (수신동의 항목에 동의한 응답만)
    dedupe_hours: 24         # 같은 번호 24시간 내 재제출 시 생략
    messages:
      - name: 접수확인
        template: TP_RECEIPT_01            # 승인된 템플릿 코드
        when: immediate
        variables: { 이름: "{{name|고객}}", 과정명: "{{수강 과정|프로그램}}" }
        text: "{{name|고객}}님, {{수강 과정|프로그램}} 신청이 접수되었습니다."
      - name: 후속안내
        when: { delay: 1d }                # 30m / 2h / 1d / 1w
      - name: 하루전리마인드
        when: { field_at: { field: 수업일, days: -1, time: "18:00" } }   # 폼의 날짜 답변 기준
      - name: 무통장안내
        when: { delay: 5m }
        match: { "결제 방법": 무통장 }    # 이 답변일 때만
      # when: { at: "2026-09-20 09:00" }  # 특정 일시
      # when: { at_time: "09:00" }        # 다음 09:00
quiet_hours: { start: "21:00", end: "08:00" }   # 야간엔 08:00 으로 미룸
```

- `{{질문 제목}}` — 폼 답변 삽입 (제목 부분 일치, 대소문자 무시). `{{항목|기본값}}` 으로 빈 값 대비
- 내장 변수: `{{name}}` `{{phone}}` `{{date}}`
- `variables` — 솔라피가 서버에서 치환하는 템플릿 변수. 키는 템플릿의 `#{이름}` 과 같아야 함
- `text` — 완성 본문. **알리고는 필수**(승인 템플릿 원문과 변수 외에 동일해야 검수 통과),
  솔라피는 알림톡 실패 시 문자 대체 발송문으로 사용
- 실패 시 2분·10분·30분 뒤 최대 3회 재시도 후 `failed`

## 자주 겪는 문제

- **템플릿 불일치 오류** — 발송 본문/변수가 승인된 템플릿과 다름. 띄어쓰기·줄바꿈까지 같아야 합니다
- **알림톡은 실패하고 문자만 감** — 수신자가 카카오톡 미사용·채널 차단·번호 변경. 정상 동작(대체 발송)
- **발신번호 오류** — 발송사에 등록·인증된 번호를 `ALIMTALK_SENDER`/`SENDER` 에 숫자만 입력
- **밤에 접수했는데 아침에 감** — `quiet_hours` 설정 때문. 즉시 보내려면 해당 설정 제거
- **Apps Script 에서 권한 오류** — `setup` 을 다시 실행해 외부 연결(UrlFetch)·트리거 권한 승인
- **재배포 후 예약이 사라짐** — Render 디스크 미설정. 위 2) 참고

## 구성 파일

| 경로 | 역할 |
|---|---|
| `apps-script/Code.gs` | 구글폼 응답 스프레드시트용 Apps Script (직접 발송 / 서버 전달) |
| `alimtalk/phone.py` | 전화번호 정규화 |
| `alimtalk/rules.py` | 항목 인식·조건·발송 시각 계산 |
| `alimtalk/providers/` | 솔라피·알리고·콘솔(dry-run) 발송사 연동 |
| `alimtalk/service.py` | 접수 → 예약 → 발송·재시도 |
| `alimtalk/store.py` | SQLite (응답·예약·수신거부) |
| `server/app.py` | 웹훅·관리 API·대시보드 서빙 |
| `server/webhooks.py` | Tally·Typeform·범용 페이로드 정규화 |
| `dashboard/index.html` | 관리 대시보드 |
| `rules.example.yaml` | 규칙 예시 |
