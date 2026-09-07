"""requirements.txt 가 '직접 import 하는 것은 직접 선언한다' 원칙을 지키는지 검사."""
from pathlib import Path

import pytest

REQUIREMENTS = Path(__file__).resolve().parents[1] / "requirements.txt"


def _declared():
    """주석·빈 줄을 뺀 '패키지명 -> 원문 줄' 매핑."""
    out = {}
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        stripped = line.split("#", 1)[0].strip()
        if not stripped:
            continue
        name = stripped.split(">=")[0].split("==")[0].split("[")[0].strip().lower()
        out[name] = stripped
    return out


@pytest.mark.parametrize("package", ["scrapling", "playwright", "curl_cffi", "patchright",
                                     "protego", "openpyxl", "pytest"])
def test_direct_imports_are_declared(package):
    assert package in _declared(), (
        f"{package} 를 코드가 직접 쓰는데 requirements.txt 에 선언되지 않았습니다"
    )


def test_scrapling_has_upper_bound():
    """pre-1.0 이고 v0.3 breaking 전력이 있어 상한이 필수다."""
    assert "<0.5" in _declared()["scrapling"], (
        "scrapling 에 상한(<0.5)이 없습니다 — 0.5 가 나오면 생성 스크립트가 일괄로 깨집니다"
    )


def test_protego_comment_is_true():
    """requirements.txt 의 주석이 실제 코드와 일치하는지.

    G3 의 재발 방지 — protego 는 '런타임에서 사용' 이라고 적혀 있으면서 실제로는
    어디서도 import 되지 않은 채 오래 방치됐다. 주석이 이름을 대면 그 이름은 존재해야 한다.
    """
    import utils
    assert hasattr(utils, "check_robots"), (
        "requirements.txt 의 protego 주석이 scripts/utils.py check_robots() 를 가리키는데 "
        "그 함수가 없습니다 — 주석이 사실과 다릅니다"
    )
