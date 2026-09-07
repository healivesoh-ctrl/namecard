import pytest

from alimtalk.phone import format_phone, mask_phone, normalize_phone


@pytest.mark.parametrize("raw,expected", [
    ("010-1234-5678", "01012345678"),
    ("010 1234 5678", "01012345678"),
    ("01012345678", "01012345678"),
    ("+82 10-1234-5678", "01012345678"),
    ("82-10-1234-5678", "01012345678"),
    ("0082 10 1234 5678", "01012345678"),
    (1012345678, "01012345678"),          # 엑셀이 앞의 0을 지운 경우
    ("010.1234.5678", "01012345678"),
    ("011-123-4567", "0111234567"),
    ("010-1111-2222 / 010-3333-4444", "01011112222"),
    ("02-123-4567", None),                # 유선전화
    ("hello", None),
    ("", None),
    (None, None),
    ("0101234567890", None),              # 자릿수 초과
])
def test_normalize(raw, expected):
    assert normalize_phone(raw) == expected


def test_format_and_mask():
    assert format_phone("01012345678") == "010-1234-5678"
    assert format_phone("0111234567") == "011-123-4567"
    assert mask_phone("01012345678") == "010-****-5678"
