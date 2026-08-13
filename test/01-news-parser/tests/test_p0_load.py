# -*- coding: utf-8 -*-
"""P0 적재 테스트 — 룰은 케이스 테이블로, 실데이터는 통합 테스트로."""
import json
from datetime import date, datetime
from pathlib import Path

import pytest
from openpyxl import Workbook

from src import config
from src.p0_load import (
    default_policy,
    load_source,
    make_article_id,
    normalize_posted_date,
    read_rows,
    standardize_article,
    transform,
    write_outputs,
)

REAL_XLSX = config.part1_dir() / "articles.xlsx"
REAL_CSV = config.part1_dir() / "news.csv"
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
        # 크롤링 표기 — 시각·요일이 붙어도 앞머리 연월일만 취한다
        ("2025.06.23. 14:52", "2025-06-23"),
        ("2025-06-23T09:00:00Z", "2025-06-23"),
        ("2025년 6월 23일", "2025-06-23"),
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


def row(aid=None, title="기사 제목", posted=None, url="https://example.com/a/1",
        text="본문. 취업자가 13만 명 줄었다.", src=10, query="통계", journalist="김기자", tc=2):
    return [aid, title, posted or datetime(2025, 6, 23), url, text, src, query, journalist, tc]


def test_transform_excludes_class4_keeps_invariant(tmp_path):
    rows = read_rows(build_xlsx(tmp_path, [
        row(url="https://example.com/a/1", tc=2),
        row(url="https://example.com/a/2", tc=3, text="사진만 있는 문서"),
        row(url="https://example.com/a/3", tc=4, text="잘린 본문"),
    ]))
    articles, aux, excluded, rejected = transform(rows)

    assert len(articles) == 2 and len(excluded) == 1 and len(aux) == 3 and not rejected
    assert len(articles) + len(excluded) + len(rejected) == len(rows)  # 전수 회계
    assert excluded[0]["reason_code"] == "CRAWL_ERROR_TRUNCATED"
    assert excluded[0]["temp_class"] == 4
    # 분류 3은 적재에 포함(음성 대조군)
    assert any(a["url"].endswith("/a/2") for a in articles)


def test_transform_core_schema_fields(tmp_path):
    rows = read_rows(build_xlsx(tmp_path, [row(url="https://example.com/a/1")]))
    res = transform(rows)
    a = res.articles[0]
    assert set(a.keys()) == {"article_id", "title", "posted_date", "url", "text"}
    assert a["posted_date"] == "2025-06-23"
    assert a["article_id"] == make_article_id("https://example.com/a/1")
    assert res.aux[0]["article_id"] == a["article_id"]  # 사이드카는 url·id로 조인 가능


def test_transform_provided_id_match_ok(tmp_path):
    good = make_article_id("https://example.com/a/1")
    rows = read_rows(build_xlsx(tmp_path, [row(aid=good, url="https://example.com/a/1")]))
    assert transform(rows).articles[0]["article_id"] == good


def test_transform_provided_id_mismatch_raises(tmp_path):
    rows = read_rows(build_xlsx(tmp_path, [row(aid="A00000000", url="https://example.com/a/1")]))
    with pytest.raises(ValueError, match="article ID 불일치"):
        transform(rows)


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
    assert transform(rows).articles[0]["text"] == text  # 원본 보존 — P0는 본문을 건드리지 않는다


def test_write_outputs_roundtrip(tmp_path):
    rows = read_rows(build_xlsx(tmp_path, [
        row(url="https://example.com/a/1", tc=2),
        row(url="https://example.com/a/3", tc=4),
    ]))
    res = transform(rows)
    paths = write_outputs(tmp_path / "out", res.articles, res.aux, res.excluded, res.rejected)

    loaded = [json.loads(line) for line in paths["articles"].read_text(encoding="utf-8").splitlines()]
    assert loaded == res.articles
    exc = [json.loads(line) for line in paths["excluded"].read_text(encoding="utf-8").splitlines()]
    assert exc == res.excluded
    assert "rejected" not in paths            # 거부 0건이면 파일을 만들지 않는다
    csv_head = paths["aux"].read_text(encoding="utf-8-sig").splitlines()[0]
    assert csv_head == "url,article_id,news_source,query,journalist,temp_class,source_label"


# ── bulk 정책: 불량 행 격리 ──────────────────────────────────────

class TestBulkPolicy:
    def test_bad_rows_quarantined_not_fatal(self, tmp_path):
        rows = read_rows(build_xlsx(tmp_path, [
            row(url="https://example.com/ok"),
            row(url="", title="URL 없음"),                       # 불량
            row(url="https://example.com/dup"),
            row(url="https://example.com/dup"),                  # 중복 → 뒤엣것 거부
            row(url="https://example.com/nodate", posted="언젠가"),  # 불량
        ]))
        res = transform(rows, policy="bulk")
        assert len(res.articles) == 2 and len(res.rejected) == 3
        assert len(res.articles) + len(res.excluded) + len(res.rejected) == len(rows)
        assert all(r["reason_code"] == "INVALID_ROW" for r in res.rejected)
        assert any("중복" in " ".join(r["reasons"]) for r in res.rejected)

    def test_rejected_file_written(self, tmp_path):
        rows = read_rows(build_xlsx(tmp_path, [row(url="", title="URL 없음")]))
        res = transform(rows, policy="bulk")
        paths = write_outputs(tmp_path / "out", res.articles, res.aux,
                              res.excluded, res.rejected)
        assert paths["rejected"].exists()

    def test_missing_class_ok_in_bulk(self, tmp_path):
        """크롤링 기사에는 temporary classification이 없다 — bulk에서는 필수가 아니다."""
        rows = read_rows(build_xlsx(tmp_path, [row(url="https://example.com/x", tc=None)]))
        res = transform(rows, policy="bulk")
        assert len(res.articles) == 1 and res.aux[0]["temp_class"] is None

    def test_missing_class_fails_in_strict(self, tmp_path):
        rows = read_rows(build_xlsx(tmp_path, [row(url="https://example.com/x", tc=None)]))
        with pytest.raises(ValueError, match="temporary classification"):
            transform(rows, policy="strict")

    def test_invalid_policy_raises(self):
        with pytest.raises(ValueError, match="strict|bulk"):
            transform([], policy="loose")


# ── 소스 어댑터: csv · json · 별칭 ───────────────────────────────

class TestSourceAdapters:
    def write_csv(self, tmp_path: Path, body: str, name="news_test.csv") -> Path:
        p = tmp_path / name
        p.write_text(body, encoding="utf-8-sig")
        return p

    def test_csv_korean_headers(self, tmp_path):
        """news.csv 실제 헤더(기사제목·작성일·URL·기사 본문 전체·검색 구분 레이블)."""
        p = self.write_csv(tmp_path,
                           "기사제목,작성일,URL,기사 본문 전체,검색 구분 레이블\n"
                           "제목1,2025-06-23,https://example.com/c/1,본문 13만 명.,TRUE\n")
        rows = load_source(p)
        res = transform(rows, policy="bulk")
        assert len(res.articles) == 1
        a = res.articles[0]
        assert a["title"] == "제목1" and a["posted_date"] == "2025-06-23"
        assert res.aux[0]["source_label"] == "TRUE"

    def test_csv_embedded_newline_and_comma(self, tmp_path):
        """본문에 개행·따옴표가 들어 있어도 인용 처리로 한 행을 유지해야 한다."""
        p = self.write_csv(tmp_path,
                           "기사제목,작성일,URL,기사 본문 전체,검색 구분 레이블\n"
                           '제목,2025-06-23,https://example.com/c/2,'
                           '"첫 문장, 쉼표.\n둘째 줄 ""인용"" 포함.",FALSE\n')
        res = transform(load_source(p), policy="bulk")
        assert len(res.articles) == 1
        assert "\n" in res.articles[0]["text"] and '"인용"' in res.articles[0]["text"]

    def test_csv_missing_required_column(self, tmp_path):
        p = self.write_csv(tmp_path, "기사제목,작성일\n제목,2025-06-23\n")
        with pytest.raises(ValueError, match="필수 컬럼 누락"):
            load_source(p)

    def test_json_single_object(self, tmp_path):
        p = tmp_path / "one.json"
        p.write_text(json.dumps({"제목": "크롤링 제목", "작성일": "2025.06.23. 14:52",
                                 "url": "https://example.com/j/1", "본문": "본문 5%."},
                                ensure_ascii=False), encoding="utf-8")
        res = transform(load_source(p), policy="bulk")
        assert len(res.articles) == 1 and res.articles[0]["posted_date"] == "2025-06-23"

    def test_json_with_bom(self, tmp_path):
        """Windows 도구(PowerShell Set-Content -Encoding UTF8)는 BOM을 붙인다 — 회귀."""
        p = tmp_path / "bom.json"
        p.write_text(json.dumps({"기사제목": "제목", "작성일": "2025-06-23",
                                 "URL": "https://example.com/j/bom", "기사 본문 전체": "본문 3%."},
                                ensure_ascii=False), encoding="utf-8-sig")
        assert len(transform(load_source(p), policy="bulk").articles) == 1

    def test_jsonl_multiple(self, tmp_path):
        p = tmp_path / "many.jsonl"
        p.write_text("\n".join(json.dumps(
            {"title": f"t{i}", "posted_date": "2025-06-23",
             "url": f"https://example.com/j/{i}", "text": "본문 1%."}, ensure_ascii=False)
            for i in range(3)), encoding="utf-8")
        assert len(transform(load_source(p), policy="bulk").articles) == 3

    def test_unknown_extension_raises(self, tmp_path):
        p = tmp_path / "x.parquet"
        p.write_text("", encoding="utf-8")
        with pytest.raises(ValueError, match="형식"):
            load_source(p)

    def test_format_override(self, tmp_path):
        p = tmp_path / "no_ext_data"
        p.write_text("title,posted date,url,text\nT,2025-06-23,https://example.com/f/1,본문 1%.\n",
                     encoding="utf-8")
        assert len(load_source(p, fmt="csv")) == 1

    @pytest.mark.parametrize("path,expected", [
        ("a/articles.xlsx", "strict"),
        ("a/news.csv", "bulk"),
        ("a/crawled.json", "bulk"),
    ])
    def test_default_policy(self, path, expected):
        assert default_policy(Path(path)) == expected


class TestStandardizeArticle:
    def test_crawled_dict(self):
        a = standardize_article({
            "기사제목": "크롤링 기사", "작성일": "2025-06-23",
            "URL": "https://example.com/live/1", "기사 본문 전체": "취업자 13만 명 감소.",
        })
        assert set(a) == {"article_id", "title", "posted_date", "url", "text"}
        assert a["article_id"] == make_article_id("https://example.com/live/1")

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            standardize_article({"title": "제목만 있음"})


# ── 통합: 실데이터 (파일이 있을 때만) ─────────────────────────────

@pytest.mark.skipif(not REAL_XLSX.exists(), reason="실데이터 없음")
def test_e2e_real_data(tmp_path):
    rows = read_rows(REAL_XLSX)
    res = transform(rows)

    assert len(rows) == 60
    assert len(res.articles) == 52 and len(res.excluded) == 8  # 60 − 분류4(8)
    assert len(res.articles) + len(res.excluded) + len(res.rejected) == len(rows)

    dist = {}
    for r in res.aux:
        dist[r["temp_class"]] = dist.get(r["temp_class"], 0) + 1
    # 2026-07-31 사용자 재분류 반영: 48행(데이터처장 감사) 3→1 · 55행(폐암 기사, 하단 잘림) 2→4
    assert dist == {1: 22, 2: 27, 3: 3, 4: 8}

    ids = [a["article_id"] for a in res.articles] + [e["article_id"] for e in res.excluded]
    assert len(ids) == len(set(ids))  # ID 중복 없음
    assert all(a["posted_date"].count("-") == 2 for a in res.articles)

    write_outputs(tmp_path / "out", res.articles, res.aux, res.excluded, res.rejected)


@pytest.mark.skipif(not REAL_CSV.exists(), reason="news.csv 없음")
def test_e2e_real_csv_bulk():
    """조선일보 원본 csv — 불량 행이 있어도 대량 적재가 완주해야 한다(전수 회계 유지)."""
    rows = load_source(REAL_CSV)
    assert len(rows) > 2000
    res = transform(rows, policy="bulk")
    assert len(res.articles) + len(res.excluded) + len(res.rejected) == len(rows)
    assert res.articles and res.rejected          # 실측: 중복 URL·결측 행이 존재한다
    assert all(a["posted_date"].count("-") == 2 for a in res.articles)
