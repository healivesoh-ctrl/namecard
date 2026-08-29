# SNS 자동화 콘텐츠 생성·업로드 시스템

주제 하나를 넣으면 **인스타그램 · 샤오홍슈(小红书) · 네이버블로그** 각각의 문법에 맞는
콘텐츠(캡션/제목/본문/해시태그)와 카드 이미지를 Claude API로 자동 생성하고,
플랫폼별로 업로드까지 수행하는 파이프라인입니다.

```
주제(topic)
   │  Claude API (플랫폼별 스타일 동시 생성)
   ▼
drafts/<날짜>-<주제>/          ← 초안 (검수 가능)
   ├─ post.json                ← 전체 데이터 + 업로드 상태
   ├─ card-01.jpg              ← 자동 생성 카드 이미지 (1080×1080)
   ├─ instagram.txt / xiaohongshu.txt / naver_blog.txt
   ▼
업로드
   ├─ instagram   → 공식 Instagram Graph API
   ├─ xiaohongshu → Playwright 브라우저 자동화 (크리에이터 센터)
   └─ naver_blog  → Playwright 브라우저 자동화 (스마트에디터 ONE)
```

## 1. 설치

```bash
cd social-automation
pip install -r requirements.txt
playwright install chromium        # 샤오홍슈·네이버 업로드용 (Instagram만 쓰면 생략 가능)

cp config.example.yaml config.yaml # 브랜드·주제·플랫폼 설정 수정
cp .env.example .env               # API 키 입력
```

`config.yaml` — 브랜드 소개·톤앤매너·타깃, 주제 큐(topics), 플랫폼별 옵션(해시태그 수,
언어, 최소 글자 수), 카드 이미지 색상을 설정합니다.

`.env` — `ANTHROPIC_API_KEY`(필수), Instagram용 `IG_ACCESS_TOKEN`/`IG_USER_ID`/`IMAGE_BASE_URL`.

## 2. 사용법

```bash
# 콘텐츠 생성 (초안만 만들고 업로드는 하지 않음 → 검수 후 업로드 권장)
python -m sns.cli generate --topic "NFC 명함에 프로필 사진까지 담는 방법"
python -m sns.cli generate --auto            # config topics 큐에서 아직 안 쓴 주제 자동 선택

python -m sns.cli list                       # 초안 목록 + 업로드 상태
python -m sns.cli preview <slug>             # 초안 내용 확인

# 업로드 (--dry-run 으로 먼저 리허설 가능)
python -m sns.cli upload <slug> --platforms instagram,xiaohongshu,naver_blog --dry-run
python -m sns.cli upload <slug> --platforms instagram

# 생성+업로드 한 번에 (cron/CI용)
python -m sns.cli run --auto --platforms instagram
```

## 3. 플랫폼별 준비

### Instagram (공식 Graph API — 완전 무인 자동화 가능)

1. Instagram을 **프로페셔널 계정**(비즈니스/크리에이터)으로 전환하고 Facebook 페이지와 연결
2. [Meta 개발자](https://developers.facebook.com)에서 앱 생성 →
   `instagram_basic`, `instagram_content_publish`, `pages_read_engagement` 권한의
   **장기 액세스 토큰** 발급
3. `.env`에 `IG_ACCESS_TOKEN`, `IG_USER_ID`(Instagram Business Account ID) 입력
4. Graph API는 **공개 이미지 URL**만 받으므로, `drafts/`를 GitHub에 push한 뒤
   `IMAGE_BASE_URL=https://raw.githubusercontent.com/<owner>/<repo>/<branch>/social-automation`
   형태로 설정 (GitHub Actions 워크플로는 이를 자동 처리)

### 샤오홍슈 (브라우저 자동화)

공식 게시 API가 일반 사업자에게 열려 있지 않아, 본인 계정으로 로그인한 브라우저
세션을 재사용합니다.

```bash
python -m sns.uploaders.login xiaohongshu   # 브라우저에서 직접 로그인 → 세션 저장
python -m sns.cli upload <slug> --platforms xiaohongshu
```

- 로그인 1회 후 `sessions/xiaohongshu.json`에 세션이 저장되어 재사용됩니다 (커밋 금지, .gitignore 처리됨)
- **과도한 자동 게시는 계정 제재 위험**이 있으니 하루 1~2건 수준을 권장합니다
- UI 개편으로 실패하면 `sns/uploaders/xiaohongshu.py` 상단의 셀렉터 상수만 수정하면 됩니다
  (실패 시 `drafts/<slug>/error-xiaohongshu.png` 스크린샷 자동 저장)

### 네이버블로그 (브라우저 자동화)

네이버는 블로그 글쓰기 공식 API를 종료했으므로 같은 방식을 사용합니다.

```bash
python -m sns.uploaders.login naver         # 자동입력방지 때문에 로그인은 사람이 직접
python -m sns.cli upload <slug> --platforms naver_blog
```

- 스마트에디터 ONE(iframe) 기준. 셀렉터는 `sns/uploaders/naver_blog.py` 상단 상수 참고
- 현재는 텍스트+태그 게시(이미지 첨부는 에디터의 OS 파일창 때문에 수동 권장)

## 4. 관리 대시보드 (웹 화면)

`dashboard/index.html` — 초안 목록·미리보기·업로드 상태를 보여주는 **정적 웹앱**입니다.
서버 없이 GitHub에서 직접 데이터를 읽으므로(공개 리포) 호스팅만 하면 됩니다.

**GitHub Pages로 호스팅** (URL 만들기):
1. 리포지토리 **Settings → Pages → Source: "Deploy from a branch"** → 브랜치 `main`, 폴더 `/ (root)` 선택 후 저장
2. 1~2분 뒤 접속: `https://healivesoh-ctrl.github.io/namecard/social-automation/dashboard/`
3. (머지 전이라면 브랜치를 `claude/social-media-automation-system-2fq5tu` 로 선택해도 됨)

기능:
- 초안 카드 목록(썸네일·날짜·플랫폼별 상태 ✓/✗)
- 상세 보기: 플랫폼 탭 전환, 제목/본문/태그 **복사 버튼** (샤오홍슈·네이버에 붙여넣기용)
- 우측 상단 **⚙️ 서버 연결**에 아래 웹 서비스 URL + 비밀번호를 넣으면 추가로:
  - ✨ **새 콘텐츠 생성** (주제 입력 → Claude 생성)
  - ✏️ **본문 편집** — 제목/본문/해시태그 직접 수정 후 저장
  - 🪄 **AI 수정** — "더 짧게", "이모지 추가" 같은 지시로 Claude 재작성
  - 🖼 **카드 이미지 편집** — 문구·그라데이션 색상 변경 후 재생성
  - 인스타그램 업로드 버튼

## 5. 웹 서비스 (생성 서버 — Render 배포)

`server/` — FastAPI 서버. 대시보드 서빙 + 콘텐츠 생성 API + 인스타그램 업로드 API.
생성된 초안은 **GitHub 리포에 자동 커밋**되므로 서버가 재시작돼도 데이터가 유지되고,
대시보드·Actions·CLI와 항상 같은 데이터를 봅니다.

**Render 배포** (무료 플랜 가능):
1. [render.com](https://render.com) 가입 → **New + → Blueprint** → 이 GitHub 리포 연결
   (리포 루트의 `render.yaml` 을 자동 인식)
2. 환경변수 입력:
   - `ANTHROPIC_API_KEY` — Claude API 키
   - `GITHUB_TOKEN` — [Fine-grained 토큰](https://github.com/settings/personal-access-tokens) (이 리포의 Contents: Read and write 권한)
   - `ADMIN_PASSWORD` — 대시보드에서 생성/업로드 시 입력할 비밀번호(직접 정하기)
   - `IG_ACCESS_TOKEN`, `IG_USER_ID` — 인스타그램 업로드용(선택)
3. 배포 완료 후 발급되는 URL(예: `https://sns-automation.onrender.com`)이 서비스 주소.
   그 주소 자체가 대시보드이기도 하고, GitHub Pages 대시보드의 ⚙️ 서버 연결에 넣어도 됩니다.

참고:
- 무료 플랜은 15분 무접속 시 잠들었다가 첫 요청 때 깨어납니다(수십 초 지연)
- 로컬 실행: `cd social-automation && pip install -r server/requirements.txt && uvicorn server.app:app`
- 서버 업로드는 인스타그램(공식 API)만 지원. 샤오홍슈·네이버는 로그인 세션이 필요해
  대시보드의 복사 버튼 또는 로컬 CLI(Playwright)로 게시합니다

## 6. 스케줄 자동화

### GitHub Actions (권장)

`.github/workflows/social-automation.yml` 이 포함되어 있습니다.

- 매일 09:00 KST에 자동 실행: 콘텐츠 생성 → 초안 커밋 → Instagram 업로드
- 수동 실행(workflow_dispatch)에서 주제 직접 지정 / dry-run 가능
- 리포지토리 **Settings → Secrets**에 등록: `ANTHROPIC_API_KEY`, `IG_ACCESS_TOKEN`, `IG_USER_ID`
- 샤오홍슈·네이버는 로그인 세션이 필요해 CI에서는 실행하지 않고, 초안을
  Actions 아티팩트로 받아 로컬에서 `upload` 명령으로 게시합니다
- cron은 **기본 브랜치에 머지된 뒤**부터 동작합니다

### 로컬 cron

```cron
0 9 * * * cd /path/to/namecard/social-automation && python -m sns.cli run --auto >> sns.log 2>&1
```

## 7. 주의사항

- 샤오홍슈·네이버 자동화는 각 플랫폼 약관상 제한될 수 있는 영역입니다.
  **본인 계정, 저빈도, 검수 후 게시**를 전제로 사용하세요.
- 생성된 콘텐츠는 업로드 전 `preview` 로 반드시 검수하는 워크플로를 권장합니다
  (generate 와 upload 가 분리되어 있는 이유입니다).
- `.env` 와 `sessions/` 는 절대 커밋하지 마세요 (.gitignore에 등록됨).
