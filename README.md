# NFC 디지털 명함 솔루션

> 📣 **SNS 자동화**: 인스타그램·샤오홍슈·네이버블로그용 콘텐츠를 자동 생성·업로드하는
> 시스템은 [`social-automation/`](social-automation/README.md) 에 있습니다.
>
> 🤝 **제휴 가게 연동 · 산후도우미 접수/양방향 인증**: 제휴 가게 이용 고객이 다이음
> 친정엄마 산후도우미를 신청하면, 가게 관리자와 다이음 관리자의 양방향 확인을 거쳐
> 가게 상품을 전달하는 시스템은 [`partner-care/`](partner-care/README.md) 에 있습니다.
> (가게·상품·본인확인 항목은 JSON 설정으로 커스터마이징)

폰을 NFC 태그에 대면 명함 정보(사진·소속·성명·직책·연락처·이메일)가 상대방 연락처로 전달되는 솔루션입니다.

## 구성 파일

| 파일 | 역할 |
|---|---|
| `index.html` | 웹 명함 페이지. NFC 태그의 URL로 열리며 "연락처에 저장" 버튼으로 사진 포함 vCard(.vcf)를 전달 |
| `nfc-writer.html` | NFC 태그 기록 도구 (Android Chrome, Web NFC 사용) |

## 동작 원리 (중요)

NFC는 카메라 촬영이 아니라 **태그에 폰을 갖다 대는(태깅)** 방식입니다.
상대방이 별도 앱 없이 폰을 태그에 대기만 하면 됩니다.

- **Android**: NFC 켜져 있으면 탭 즉시 반응
- **iPhone XS 이상**: 백그라운드 NFC 읽기 지원 — 잠금화면에서도 태그를 대면 알림이 뜸

## 두 가지 방식

### 방식 A — URL 방식 (권장, 사진 포함 가능)

1. `index.html` 상단의 `CARD` 객체에 본인 정보 입력, 같은 폴더에 `photo.jpg`(프로필 사진, 200~400px 권장) 배치
2. HTTPS 호스팅에 업로드 — GitHub Pages / Vercel / Netlify 모두 무료로 가능
3. `nfc-writer.html`을 같은 호스팅에 올린 뒤 **Android Chrome**에서 열기
4. "URL 방식" 탭에 명함 페이지 주소 입력 → 기록 버튼 → 태그에 폰 대기
5. 완료. 이후 누구든 태그에 폰을 대면 명함 페이지가 열리고, 버튼 한 번으로 사진 포함 연락처가 저장됨

- 태그: 저렴한 **NTAG213**(144B)으로 충분 (URL만 저장하므로)
- 사람마다 정보가 다르면 URL을 `card.example.com/hong`, `card.example.com/kim`처럼 나누거나, `index.html`을 쿼리스트링(`?name=...`) 기반으로 확장

### 방식 B — vCard 직접 기록 (사진 불가, 탭 즉시 연락처 추가)

`nfc-writer.html`의 "vCard 직접 기록" 탭 사용.
태그를 탭하면 Android에서 바로 연락처 추가 화면이 뜹니다.

- 텍스트만으로 200~300바이트 → **NTAG215(504B) 또는 NTAG216(888B)** 필요
- **사진은 용량상 불가** (사진 base64는 수십 KB)
- iPhone은 vCard NDEF 태그를 OS에서 직접 처리하지 않는 경우가 많음 → 아이폰 대응이 필요하면 방식 A 사용

## PC에서 태그 기록 (선택)

USB NFC 리더(ACR122U 등)가 있다면 Python으로도 기록할 수 있습니다:

```bash
pip install nfcpy ndeflib
```

```python
import nfc, ndef

URL = "https://card.example.com/hong"

def write(tag):
    if tag.ndef:
        tag.ndef.records = [ndef.UriRecord(URL)]
        print("기록 완료:", URL)
    return True

with nfc.ContactlessFrontend("usb") as clf:
    print("태그를 리더에 올려놓으세요…")
    clf.connect(rdwr={"on-connect": write})
```

vCard를 직접 기록하려면:

```python
vcf = (
    "BEGIN:VCARD\r\nVERSION:3.0\r\n"
    "N;CHARSET=UTF-8:홍길동;;;;\r\nFN;CHARSET=UTF-8:홍길동\r\n"
    "ORG;CHARSET=UTF-8:㈜디에이블\r\nTITLE;CHARSET=UTF-8:대표이사\r\n"
    "TEL;TYPE=CELL:010-0000-0000\r\nEMAIL:hello@example.com\r\n"
    "END:VCARD"
)
record = ndef.Record("text/vcard", "", vcf.encode("utf-8"))
# tag.ndef.records = [record]
```

## 태그 구매 시 체크리스트

- 칩: NTAG213 / 215 / 216 (NDEF 표준, iOS·Android 모두 호환)
- 형태: 카드형(명함 크기), 스티커형, 에폭시 키링 등
- 금속 표면에 붙일 경우 "on-metal(안티메탈)" 타입 필요

## 자주 겪는 문제

- **iPhone에서 반응 없음** → iPhone X 이하는 백그라운드 읽기 미지원. URL 방식 + iPhone XS 이상이면 정상 동작
- **Web NFC 버튼이 안 눌림** → iOS/데스크톱 Chrome은 Web NFC 미지원. 반드시 Android Chrome + HTTPS
- **.vcf 다운로드 후 아무 일 없음(Android 일부 기기)** → 알림창의 다운로드 파일을 탭하면 연락처 앱이 열림
- **태그 기록 실패** → 태그가 잠금(read-only) 상태이거나 용량 초과. 새 태그로 시도
