# 산모·신생아 건강관리 지원사업 제공기관(파견기관) 수집

전국 산모신생아건강관리사 파견기관의 **기관명 · 주소 · 전화 · 팩스 · 이메일 · 시도/시군구 · 사업명** 등을
모아 엑셀로 만드는 작업입니다. 스크립트는 `crawl_script.py` 하나입니다.

## 데이터 출처 (가벼운 순서)

| 트랙 | 출처 | 성격 | 비고 |
|---|---|---|---|
| **A `--mode api` (기본)** | 공공데이터포털 [한국사회보장정보원_사회서비스 제공기관 정보 검색](https://www.data.go.kr/data/15057683/openapi.do) | 공식 Open API (사다리 0단) | 무료 · 활용신청 즉시 자동승인. 서비스키 필요 |
| B `--mode web` | [사회서비스 전자바우처 제공기관 검색](https://www.socialservice.or.kr:444/user/svcsrch/supply/supplyList.do) | 정적 HTML (사다리 1단) | 키 불필요. 상세 페이지까지 들어가 전화·이메일 수집 |
| 참고 | [한국사회보장정보원_산모신생아_제공기관_정보 (파일)](https://www.data.go.kr/data/15142429/fileData.do) | CSV 다운로드 | 시도·시군구·기관명·주소·인력수·대상자수만 있고 **전화번호 없음** |

## 실행 (로컬 PC)

```bash
cd web-crawler
python -m venv .venv && . .venv/bin/activate        # Windows: .\.venv\Scripts\activate
python scripts/bootstrap.py                          # 최초 1회 설치 (이미 했으면 skip)

# Track A — data.go.kr 에서 받은 "일반 인증키(Decoding)" 를 환경변수로
export DATA_GO_KR_SERVICE_KEY="발급받은키"            # Windows PowerShell: $env:DATA_GO_KR_SERVICE_KEY="발급받은키"
python jobs/sanmo_provider/crawl_script.py --test    # 1페이지만 테스트
python jobs/sanmo_provider/crawl_script.py           # 전량

# Track B — 키 없이 누리집 검색
python jobs/sanmo_provider/crawl_script.py --mode web --test   # 1개 시도 2페이지 + 상세 5건
python jobs/sanmo_provider/crawl_script.py --mode web
```

결과는 `web-crawler/output/<도메인>/산모신생아_제공기관_<일시>/` 아래에 생깁니다.

| 파일 | 내용 |
|---|---|
| `crawl_result.xlsx` | 최종 엑셀 (표준 컬럼 + 원시 필드 `raw_*`) |
| `raw_data.json` | 원시 데이터 |
| `progress.json` | 진행 상황 · 에러 로그 |
| `samples/` | 첫 응답 원문 (API 응답 / 검색 폼 / 결과 목록 / 상세 페이지). **매핑 확정용** |

## 이 스크립트가 "정찰 없이" 짜인 이유와 다음 단계

이 작업을 만든 원격 세션은 `data.go.kr` · `socialservice.or.kr` 로 나가는 통신이 조직 egress 정책에 막혀 있어
사이트 정찰도, 실제 수집도 할 수 없었습니다 (크롤러 문서 CLAUDE.md 의 "원격 전용 환경에서는 정찰까지만" 상황).
그래서 스크립트는 필드명·폼 파라미터를 고정하지 않고 **응답에서 스스로 찾도록** 되어 있습니다.

- API 응답 필드는 키 이름 힌트(`FIELD_HINTS`)와 값 패턴(전화·이메일 정규식)으로 표준 컬럼에 맞추고, 못 맞춘 것은 `raw_*` 로 전부 남깁니다.
- 사업구분 필터 파라미터 이름은 후보(`SERVICE_PARAM_CANDIDATES`)를 모두 보내고, 그래도 0건이면 필터 없이 받아 `산모` 글자로 걸러냅니다.
  문서에서 정확한 이름을 확인했다면 `--extra 파라미터=값` 으로 직접 지정하세요.
- 누리집 모드는 검색 폼의 `select` 를 읽어 시도 목록과 "산모" 가 들어간 사업 옵션을 찾아 시도별로 검색하고, 결과 행의 상세 링크(`supplyViewBokji.do?pb=`)를 따라가 `th/td`·`dt/dd` 라벨을 값으로 뽑습니다.

**로컬에서 `--test` 를 한 번 돌린 뒤 `samples/` 폴더와 로그를 다음 세션에 보여 주면** 필드 매핑을 확정하고
`fingerprints/<도메인>/profile.json` 을 정식 저장(Step 5-A)할 수 있습니다. 전량 수집이 성공하면 스크립트가 프로필을 자동 저장하며,
그 뒤 `python scripts/sync_domain_list.py` 를 한 번 실행해 도메인 목록을 재생성하세요.

원격 세션에서 바로 돌리고 싶다면 Claude Code 환경 설정의 네트워크 정책에 `api.socialservice.or.kr`, `www.socialservice.or.kr`,
`www.data.go.kr` 를 허용 목록에 넣으면 됩니다.

## 주의

- 서비스키는 환경변수로만 넘기고 `profile.json` 이나 코드에 적지 마세요 (커밋 대상).
- 수집 결과에는 기관 대표 전화·이메일이 들어갑니다. 기관 정보이지만 담당자 개인 이메일이 섞일 수 있으니 `detect_pii` 경고를 확인하고, 필요 없는 컬럼은 빼고 쓰세요.
- 요청 간격 기본 1초, 총 요청 상한 기본 3000건(`--max-requests`). robots.txt 차단이 확인되면 스크립트가 멈춥니다 (`--ignore-robots` 는 사용자 책임).
