# 제휴 가게 연동 · 친정엄마 산후도우미 접수/양방향 인증

제휴 가게(예: **대추밭한의원**) 이용 고객이 **디에이블 다이음**의 *친정엄마 산후도우미* 를
신청하면, 다이음이 그 가게의 제휴 상품을 고객에게 전달하는 흐름을 관리한다.

핵심은 세 가지다.

| 요구사항 | 구현 |
|---|---|
| **그 가게 이용객임을 입증** | 가게별 전용 접수 링크 + 가게마다 다른 본인확인 항목(차트번호·최근 내원일 등) 입력 → **가게 관리자가 실제 기록과 대조해 최종확인** |
| **지인 양도 차단** | 휴대폰 본인인증으로 접수 건을 사람에게 고정 → 접수 지문(fingerprint) 서명 → 60초마다 바뀌는 수령코드 + 휴대폰 뒷 4자리 대조 |
| **양방향 인증** | 가게 관리자 승인과 다이음 관리자 승인이 **둘 다 유효할 때만** 수령코드가 생성됨. 한쪽만 승인하면 코드 자체가 존재하지 않는다 |

가게와 상품은 계속 바뀌므로 **모두 설정(JSON)으로 분리**되어 있다. 코드 수정 없이
새 제휴처·새 상품·새 본인확인 항목을 추가할 수 있다.

---

## 전체 흐름

```
 고객                     가게 관리자              다이음 관리자
  │  ① 제휴 링크(QR/NFC)로 접수
  │     - 서비스 정보 확인, 상품 선택
  │     - 이름/휴대폰 + 가게별 본인확인 항목
  │     - 본인 외 양도 금지 동의
  │
  │  ② 휴대폰 인증번호 확인  ──►  접수 건이 "그 사람"에게 고정됨
  │                              (이후 이름·연락처·본인확인 값이 바뀌면 승인 자동 무효)
  │
  │                          ③ 차트번호·내원일 대조
  │                             "우리 고객 맞음" 승인   ─┐
  │                                                      ├─► 둘 다 유효해야
  │                                        ④ 배정 가능  ─┘    approved
  │                                           여부 승인
  │
  │  ⑤ 수령코드(60초마다 갱신) 발급 ─────────────────►  ⑥ 코드 + 휴대폰
  │                                                        뒷 4자리 대조 후
  │                                                        상품 전달 확정
```

상태: `phone_pending` → `pending` → `approved` → `fulfilled`
(그 외 `rejected` / `cancelled` / `expired`)

---

## 양도·도용을 막는 장치

1. **휴대폰 본인확인** — 접수 즉시 인증번호를 보내고, 인증에 성공한 브라우저에만
   조회용 서명 토큰을 준다. 접수 건의 휴대폰이 바뀌면 기존 토큰은 즉시 무효가 된다.
2. **접수 지문(fingerprint)** — 이름·휴대폰 해시·본인확인 값·상품을 묶은 해시.
   두 관리자의 승인은 *그 시점의 지문에 대한 서명*이라, 접수 내용이 나중에 바뀌면
   승인이 자동으로 무효화되고 상태가 `pending` 으로 되돌아간다(관리자 화면에 "승인 무효" 표시).
3. **회전 수령코드** — 양방향 승인이 모두 유효한 순간에만 씨앗(nonce)이 만들어지고,
   그로부터 60초마다 바뀌는 6자리 코드가 파생된다. 캡처해서 지인에게 보내도 곧 무효.
4. **수령 시 이중 대조** — 코드 + 접수자 휴대폰 뒷 4자리(`pickup_requires_phone_match`).
5. **1인 1회 제한** — 휴대폰 해시 기준(`max_claims_per_person`). 취소·반려 건은 한도에서 제외.
6. **감사 로그** — 접수·인증·승인(확인한 항목 포함)·반려·전달·코드 오입력이 모두
   `audit.jsonl` 에 추가 전용으로 쌓인다.

권한 분리: 가게 관리자는 **자기 가게 접수 건만** 보고 본인확인 입력값(차트번호 등)을 볼 수 있다.
다이음 관리자는 전체 접수를 보되 가게의 진료 정보는 보지 않는다.

---

## 커스터마이징 — 새 가게/상품 추가

`config/partners.json` (없으면 `config/partners.example.json`) 한 곳만 고치면 된다.

```jsonc
{
  "id": "sunny-mom-cafe",          // 접수 링크: /apply?p=sunny-mom-cafe
  "name": "써니맘카페",
  "active": true,
  "services": ["postpartum-helper"],
  "brand": { "accent": "#c2410c", "intro": "써니맘카페 회원님께 드리는 제휴 혜택입니다." },

  "identity_fields": [             // 가게마다 다른 '이용객 입증' 항목
    { "key": "member_no", "label": "회원번호", "required": true,
      "pattern": "^SM[0-9]{5}$", "hint": "멤버십 카드 뒷면",
      "verify_label": "회원번호가 멤버십 대장과 일치" }
  ],

  "products": [                    // 다이음이 대신 전달할 가게 상품
    { "id": "care-box", "name": "산모 케어 박스", "quantity": 1, "unit": "박스",
      "handover": "산후도우미 첫 방문일에 전달" }
  ],

  "rules": {                       // 가게별 정책
    "require_store_approval": true,
    "require_daieum_approval": true,
    "transfer_allowed": false,
    "max_claims_per_person": 1,
    "claim_valid_days": 30,
    "pickup_requires_phone_match": true,
    "release_code_period_seconds": 60
  }
}
```

- 비밀번호: 환경변수 `PARTNER_ADMIN_PASSWORD__SUNNY_MOM_CAFE` (대문자, `-`→`_`)
  또는 설정의 `admin_password_sha256`(환경변수가 우선).
- 서비스(예약 입력 항목 포함)도 `services` 에 정의되어 있어 산후도우미 외 다른 서비스로 확장 가능.
- 저장 후 **다이음 관리자 화면 → "제휴 설정 다시 읽기"** 를 누르면 재시작 없이 반영된다.
- 접수 링크는 가게에 두는 QR 또는 NFC 태그(이 저장소의 `nfc-writer.html`)에 그대로 쓸 수 있다.

---

## 화면

| 경로 | 대상 | 내용 |
|---|---|---|
| `/` | 안내 | 절차 설명, 제휴처별 접수 링크 |
| `/apply?p=<제휴처ID>` | 고객 | 서비스 확인 → 신청 → 휴대폰 인증 → 진행 상황 → 수령코드 |
| `/store` | 가게 관리자 | 접수 목록, 본인확인 항목 체크리스트 대조 후 승인/반려 |
| `/daieum` | 다이음 관리자 | 전체 접수, 승인/반려, 수령코드 대조 후 전달 확정, 설정 리로드 |

## API

```
GET  /api/health
GET  /api/catalog                          제휴처·서비스 목록(공개)
GET  /api/partners/{id}                    접수 폼 정의(본인확인 항목·상품·서비스)

POST /api/claims                           접수 → 인증번호 발송
POST /api/claims/{id}/otp/verify           본인확인 → 조회 토큰 발급
POST /api/claims/{id}/otp/resend
GET  /api/claims/{id}                       진행 상황            (X-Claim-Token)
GET  /api/claims/{id}/release-code          수령코드(양방향 승인 완료 시)
POST /api/claims/{id}/cancel

POST /api/admin/login                       role=store|operator
GET  /api/admin/claims                      역할 범위 내 목록     (X-Admin-Token)
GET  /api/admin/claims/{id}                 상세 + 감사 로그
POST /api/admin/claims/{id}/approve         역할별 승인(가게는 확인 항목 체크 필수)
POST /api/admin/claims/{id}/reject
POST /api/admin/claims/{id}/fulfill         수령코드+뒷4자리 대조 후 전달 확정
GET  /api/admin/claims/{id}/audit
POST /api/admin/reload                      설정 다시 읽기(다이음 관리자)
```

---

## 로컬 실행

```bash
cd partner-care
pip install -r requirements.txt
export SERVER_SECRET=dev-secret
export DAIEUM_ADMIN_PASSWORD=d-pass
export PARTNER_ADMIN_PASSWORD__DAECHUBAT=s-pass
export OTP_DEBUG=1            # 문자 발송 대신 인증번호를 응답에 노출(개발용)
uvicorn app.main:app --port 8100
```

- 고객: http://localhost:8100/apply?p=daechubat
- 가게 관리자: http://localhost:8100/store
- 다이음 관리자: http://localhost:8100/daieum

테스트:

```bash
pip install -r tests/requirements.txt
python -m pytest tests -q
```

## 환경변수

| 이름 | 필수 | 설명 |
|---|---|---|
| `SERVER_SECRET` | 운영 필수 | 모든 서명(토큰·수령코드·전화번호 해시)의 키. 바뀌면 기존 토큰·코드가 무효 |
| `DAIEUM_ADMIN_PASSWORD` | ✔ | 다이음 관리자 비밀번호 |
| `PARTNER_ADMIN_PASSWORD__<제휴처ID>` | ✔ | 가게 관리자 비밀번호 |
| `PARTNER_CONFIG` | | 카탈로그 JSON 경로 (기본 `config/partners.json`) |
| `DATA_DIR` | | 접수 데이터·감사 로그 경로 (기본 `partner-care/data`, Docker 기본 `/data`) |
| `OTP_DEBUG` | | `1` 이면 인증번호를 응답에 포함(개발 전용) |

## 배포

저장소 루트의 `render.yaml` 에 `partner-care` 서비스가 정의되어 있다.
접수 데이터는 `DATA_DIR` 의 JSON 파일에 저장되므로, 운영에서는 **디스크(볼륨)를 붙이거나**
`app/store.py` 를 DB 구현으로 교체한다(함수 시그니처만 맞추면 된다).

## 운영 전 남은 연동

- **SMS 발송**: `app/main.py` 의 `_send_otp()` 가 연동 지점. 현재는 서버 로그에만 출력한다.
- **개인정보**: 전화번호는 원문 대신 마스킹+해시로 보관한다. 이름과 가게별 본인확인 값은
  가게 확인에 필요해 원문으로 남으므로, 보관 기간(예: 전달 완료 후 N개월) 정책과 파기 작업이 필요하다.
- **저장소**: 파일 기반이라 서버 1대 전제. 다중 인스턴스로 가면 DB로 교체.
