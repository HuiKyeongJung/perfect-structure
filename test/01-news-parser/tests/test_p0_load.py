# -*- coding: utf-8 -*-
"""P0 적재 테스트 — 룰은 케이스 테이블로, 실데이터는 통합 테스트로."""
import json
from datetime import date, datetime
from pathlib import Path

import pytest
from openpyxl import Workbook

from src.p0_load import (
    make_article_id,
    normalize_posted_date,
    read_rows,
    transform,
    write_outputs,
)

REAL_XLSX = Path("D:/part1/articles.xlsx")
URL_A = "https://www.chosun.com/national/national_general/2025/04/16/HAYXCMNMPJBY7LZ6ML3JZBRHJM"
URL_B = "https://www.chosun.com/economy/economy_general/2025/06/23/VRSULF3KLRDCXJYUXRMNW45AIY"

HEADER = [
    "article ID", "title", "posted date", "url", "text",
    "news source", "query", "journalist", "temporary classification",
]


# ── 단위: article_id ─────────────────────────────────────────────

class TestMakeArticleId:
    def test_known_hash_stable(self):
        # 실데이터 URL로 고정 — 값이 바뀌면 ID 체계 전체가 흔들린 것
        assert make_article_id(URL_A) == "Ae4300e50"
        assert make_article_id(URL_B) == "Ae21581c3"

    @pytest.mark.parametrize("variant", [
        URL_A,
        URL_A + "/",
        "  " + URL_A + "  ",
        URL_A + "/  ",
    ])
    def test_url_variants_same_id(self, variant):
        assert make_article_id(variant) == "Ae4300e50"

    def test_different_urls_differ(self):
        assert make_article_id(URL_A) != make_article_id(URL_B)

    def test_format(self):
        aid = make_article_id("https://example.com/x")
        assert aid.startswith("A") and len(aid) == 9


# ── 단위: posted_date 정규화 ─────────────────────────────────────

class TestNormalizePostedDate:
    @pytest.mark.parametrize("value,expected", [
        (datetime(2025, 6, 23, 0, 0), "2025-06-23"),
        (datetime(2025, 12, 25, 23, 59), "2025-12-25"),
        (date(2025, 1, 2), "2025-01-02"),
        ("2025-06-23 00:00:00", "2025-06-23"),
        ("2025-06-23", "2025-06-23"),
        ("2025.06.23", "2025-06-23"),
        ("2025/6/3", "2025-06-03"),
    ])
    def test_ok(self, value, expected):
        assert normalize_posted_date(value) == expected

    @pytest.mark.parametrize("bad", [None, "", "어제", "2025년 6월", "13-06-23", "2025-13-01"])
    def test_bad_raises(self, bad):
        with pytest.raises(ValueError):
            normalize_posted_date(bad)


# ── 합성 fixture 기반: transform ─────────────────────────────────

def build_xlsx(tmp_path: Path, rows: list[list]) -> Path:
    wb = Workbook()
    ws = wb.active
    ws.append(HEADER)
    for r in rows:
        ws.append(r)
    path = tmp_path / "articles_test.xlsx"
    wb.save(path)
    return path


def row(title="기사 제목", posted=None, url="https://example.com/a/1",
        text="본문. 취업자가 13만 명 줄었다.", src=10, query="통계", journalist="김기자", tc=2):
    return [None, title, posted or datetime(2025, 6, 23), url, text, src, query, journalist, tc]


def test_transform_excludes_class4_keeps_invariant(tmp_path):
    rows = read_rows(build_xlsx(tmp_path, [
        row(url="https://example.com/a/1", tc=2),
        row(url="https://example.com/a/2", tc=3, text="사진만 있는 문서"),
        row(url="https://example.com/a/3", tc=4, text="잘린 본문"),
    ]))
    articles, aux, excluded = transform(rows)

    assert len(articles) == 2 and len(excluded) == 1 and len(aux) == 3
    assert len(articles) + len(excluded) == len(rows)  # 전수 회계
    assert excluded[0]["reason_code"] == "CRAWL_ERROR_TRUNCATED"
    assert excluded[0]["temp_class"] == 4
    # 분류 3은 적재에 포함(음성 대조군)
    assert any(a["url"].endswith("/a/2") for a in articles)


def test_transform_core_schema_fields(tmp_path):
    rows = read_rows(build_xlsx(tmp_path, [row(url="https://example.com/a/1")]))
    articles, aux, _ = transform(rows)
    a = articles[0]
    assert set(a.keys()) == {"article_id", "title", "posted_date", "url", "text"}
    assert a["posted_date"] == "2025-06-23"
    assert a["article_id"] == make_article_id("https://example.com/a/1")
    assert aux[0]["article_id"] == a["article_id"]  # 사이드카는 url·id로 조인 가능


def test_transform_duplicate_url_raises(tmp_path):
    rows = read_rows(build_xlsx(tmp_path, [
        row(url="https://example.com/dup"),
        row(url="https://example.com/dup"),
    ]))
    with pytest.raises(ValueError, match="중복"):
        transform(rows)


def test_transform_bad_date_raises_with_lineno(tmp_path):
    rows = read_rows(build_xlsx(tmp_path, [row(posted="언젠가", url="https://example.com/a/9")]))
    with pytest.raises(ValueError, match="2행"):
        transform(rows)


def test_transform_text_not_modified(tmp_path):
    text = "  앞뒤 공백과\n줄바꿈이 있는 원문  "
    rows = read_rows(build_xlsx(tmp_path, [row(text=text, url="https://example.com/a/7")]))
    articles, _, _ = transform(rows)
    assert articles[0]["text"] == text  # 원본 보존 — P0는 본문을 건드리지 않는다


def test_write_outputs_roundtrip(tmp_path):
    rows = read_rows(build_xlsx(tmp_path, [
        row(url="https://example.com/a/1", tc=2),
        row(url="https://example.com/a/3", tc=4),
    ]))
    articles, aux, excluded = transform(rows)
    paths = write_outputs(tmp_path / "out", articles, aux, excluded)

    loaded = [json.loads(line) for line in paths["articles"].read_text(encoding="utf-8").splitlines()]
    assert loaded == articles
    exc = [json.loads(line) for line in paths["excluded"].read_text(encoding="utf-8").splitlines()]
    assert exc == excluded
    csv_head = paths["aux"].read_text(encoding="utf-8-sig").splitlines()[0]
    assert csv_head == "url,article_id,news_source,query,journalist,temp_class"


# ── 통합: 실데이터 (파일이 있을 때만) ─────────────────────────────

@pytest.mark.skipif(not REAL_XLSX.exists(), reason="실데이터 없음")
def test_e2e_real_data(tmp_path):
    rows = read_rows(REAL_XLSX)
    articles, aux, excluded = transform(rows)

    assert len(rows) == 60
    assert len(articles) == 52 and len(excluded) == 8  # 60 − 분류4(8)
    assert len(articles) + len(excluded) == len(rows)

    dist = {}
    for r in aux:
        dist[r["temp_class"]] = dist.get(r["temp_class"], 0) + 1
    # 2026-07-31 사용자 재분류 반영: 48행(데이터처장 감사) 3→1 · 55행(폐암 기사, 하단 잘림) 2→4
    assert dist == {1: 22, 2: 27, 3: 3, 4: 8}

    ids = [a["article_id"] for a in articles] + [e["article_id"] for e in excluded]
    assert len(ids) == len(set(ids))  # ID 중복 없음
    assert all(a["posted_date"].count("-") == 2 for a in articles)

    write_outputs(tmp_path / "out", articles, aux, excluded)
