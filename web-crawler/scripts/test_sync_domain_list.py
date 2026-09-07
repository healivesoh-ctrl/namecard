"""scripts/test_sync_domain_list.py — 도메인 목록 생성기 테스트

핵심은 test_docs_are_in_sync: profile.json을 추가하고 문서 갱신을 잊으면
테스트 스위트가 빨갛게 된다. 문서만으로 규율을 유지하려는 시도는 실패했다
(문서엔 11개/12개로 적혀 있는 동안 실제 프로필은 19개였다).
"""
import json

import pytest

import sync_domain_list as sdl


def test_docs_are_in_sync():
    """CLAUDE.md / README.md 의 도메인 목록이 fingerprints/ 와 일치해야 한다.

    실패하면: python scripts/sync_domain_list.py
    """
    assert sdl.build(check_only=True) == 0


def test_repo_has_markers():
    for path, _ in sdl.TARGETS:
        text = path.read_text(encoding="utf-8")
        assert sdl.BEGIN in text, f"{path.name}에 BEGIN 마커 없음"
        assert sdl.END in text, f"{path.name}에 END 마커 없음"


def test_collect_domains_uses_domain_field(tmp_path, monkeypatch):
    monkeypatch.setattr(sdl, "FINGERPRINTS", tmp_path)
    (tmp_path / "example_com").mkdir()
    (tmp_path / "example_com" / "profile.json").write_text(
        json.dumps({"domain": "example.com", "fetcher_type": "Fetcher"}), encoding="utf-8"
    )
    assert sdl.collect_domains() == ["example.com"]


def test_collect_domains_falls_back_to_dirname(tmp_path, monkeypatch):
    """domain 필드가 비어도 조용히 누락되지 않고 디렉터리명으로 나타난다."""
    monkeypatch.setattr(sdl, "FINGERPRINTS", tmp_path)
    (tmp_path / "fallback_site").mkdir()
    (tmp_path / "fallback_site" / "profile.json").write_text(
        json.dumps({"fetcher_type": "Fetcher"}), encoding="utf-8"
    )
    assert sdl.collect_domains() == ["fallback_site"]


def test_collect_domains_skips_broken_json(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sdl, "FINGERPRINTS", tmp_path)
    (tmp_path / "ok_com").mkdir()
    (tmp_path / "ok_com" / "profile.json").write_text(
        json.dumps({"domain": "ok.com", "fetcher_type": "Fetcher"}), encoding="utf-8"
    )
    (tmp_path / "broken_com").mkdir()
    (tmp_path / "broken_com" / "profile.json").write_text("{not json", encoding="utf-8")
    assert sdl.collect_domains() == ["ok.com"]
    assert "읽기 실패" in capsys.readouterr().err


def test_collect_domains_dedupes(tmp_path, monkeypatch):
    """sanitize 규칙상 서로 다른 디렉터리가 같은 domain을 가리킬 수 있다."""
    monkeypatch.setattr(sdl, "FINGERPRINTS", tmp_path)
    for name in ("a_com", "b_com"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "profile.json").write_text(
            json.dumps({"domain": "same.com", "fetcher_type": "Fetcher"}), encoding="utf-8"
        )
    assert sdl.collect_domains() == ["same.com"]


def test_render_includes_count_and_all_domains():
    domains = ["a.com", "b.com", "c.com"]
    for render in (sdl.render_claude, sdl.render_readme):
        out = render(domains)
        assert "3" in out
        for d in domains:
            assert f"`{d}`" in out


def test_replace_block_is_idempotent():
    text = f"머리말\n\n{sdl.BEGIN}\n{sdl.END}\n\n꼬리말\n"
    once = sdl.replace_block(text, "본문", sdl.TARGETS[0][0])
    twice = sdl.replace_block(once, "본문", sdl.TARGETS[0][0])
    assert once == twice
    assert "머리말" in once and "꼬리말" in once


def test_replace_block_requires_markers():
    with pytest.raises(RuntimeError, match="마커가 없습니다"):
        sdl.replace_block("마커 없는 문서", "본문", sdl.TARGETS[0][0])


def test_replace_block_rejects_reversed_markers():
    text = f"{sdl.END}\n{sdl.BEGIN}\n"
    with pytest.raises(RuntimeError, match="뒤집혔"):
        sdl.replace_block(text, "본문", sdl.TARGETS[0][0])
