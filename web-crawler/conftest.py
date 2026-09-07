"""repo 루트 conftest — Windows/Codex 제한 환경에서 안정적인 테스트 temp 경로.

기본 OS temp(예: C:\\Users\\...\\AppData\\Local\\Temp\\pytest-of-...)에서 PermissionError가
나는 환경이 있어, 테스트가 시작되기 전에 TEMP/TMP/tempfile.tempdir 을 repo 안 .tmp/ 로 고정한다.
pytest 의 tmp_path 류는 pytest.ini 의 --basetemp=.tmp/pytest 가 담당한다. (.tmp/ 는 .gitignore)
"""
import os
import tempfile
from pathlib import Path

_TMP = Path(__file__).resolve().parent / ".tmp"
_TMP.mkdir(parents=True, exist_ok=True)

for _var in ("TEMP", "TMP", "TMPDIR"):
    os.environ[_var] = str(_TMP)
tempfile.tempdir = str(_TMP)
