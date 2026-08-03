# -*- coding: utf-8 -*-
"""P1 정제: articles.jsonl → articles_clean.jsonl (룰 기반 노이즈 제거 + removed_spans 기록).

규칙은 골든셋(cleaned_articles_ex.xlsx) 역산 관찰로 도출했다 (52건 중 50건이 선두/말미 노이즈만).
- 선두: '입력 <ts> [업데이트 <ts>] [댓글수]' 앵커 절단 (짧은 헤더·긴 포털 내비 공통)
        + 뉴스레터형('N호 YYYY.MM.DD HH:MM') + 타임스탬프 없는 기사용 섹션+제목 폴백
- 중간: '[칼럼 전문 링크]' 류 리터럴 제거
- 말미: UI 전용 앵커(가장 이른 위치)부터 끝까지 절단
- v2: 말미 잔여 꼬리(해시 없는 태그 키워드·기자명) 휴리스틱

원칙: 패턴이 없으면 그대로 통과(과잉 삭제 금지) · 제거는 삭제가 아니라 removed_spans 기록
      · 정제본+제거분으로 원문 복원 가능해야 함(보존 인바리언트).

산출은 2파일로 분리한다 (실사용 파일의 용량 절감):
  - articles_clean.jsonl        코어 5필드만 (article_id·title·posted_date·url·text) — P2·다운스트림 입력
  - articles_clean_trace.jsonl  감사 사이드카 (article_id·pipeline_version·removed_spans)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PIPELINE_VERSION = "clean_v3"

# ── 선두(프리픽스) 규칙 ──────────────────────────────────────────
# R1: '입력 2025.06.23. 09:03 업데이트 2025.06.23. 17:40 0 ' — 업데이트·댓글수는 옵션
_TS = r"\d{4}\.\s?\d{1,2}\.\s?\d{1,2}\.?\s*\d{1,2}:\d{2}"
RE_HEADER = re.compile(
    rf"입력\s*{_TS}(?:\s*업데이트\s*{_TS})?(?:\s+\d{{1,4}}(?=\s))?\s*"
)
# R2: 뉴스레터형 '604호 2025.09.15 11:00 '
RE_NEWSLETTER = re.compile(rf"\d{{1,4}}호\s+{_TS}\s*")
PREFIX_SEARCH_LIMIT = 1200  # 앵커가 이 위치 안에서 시작해야 헤더로 인정 (긴 포털 내비 ~850자)

# ── 중간(미드) 규칙 ─────────────────────────────────────────────
RE_MID_LITERALS = [re.compile(r"\[\s*칼럼\s*전문\s*링크\s*\]\s*")]

# ── 말미(서픽스) 앵커 — 전부 기사 본문에 나올 수 없는 UI 전용 문구 ──
SUFFIX_ANCHORS = [
    ("english_button", re.compile(r"English\s*기사보기")),
    ("hashtag_block", re.compile(r"#(?!\d+(?:\s|$))[^\s#]{2,}")),
    ("reporter_profile", re.compile(r"[가-힣]{2,4}\s*기자(?:\(조선비즈\))?\s+\d{4}년\s*조선일보에\s*입사")),
    ("staff_writer", re.compile(r"[가-힣]{2,4}\s*기자\s+staff\s+writer")),
    ("comment_widget", re.compile(r"(?:코스피\s+코스닥\s+증권\s+)?100자평\s+도움말\s+삭제기준")),
    ("video_player", re.compile(r"Video\s+Player\s+is\s+loading")),
    ("video_time", re.compile(r"\d{1,2}:\d{2}\s*/\s*(?:Duration\s*)?\d{1,2}:\d{2}")),
    ("ads_close", re.compile(r"close\s+Advertisements")),
    ("hot_news", re.compile(r"오늘의\s*핫뉴스")),
    ("most_viewed", re.compile(r"많이\s*본\s*뉴스")),
    ("recommend", re.compile(r"당신이\s*좋아할\s*만한\s*콘텐츠")),
    ("taboola", re.compile(r"By\s+Taboola")),
    ("newsletter_promo", re.compile(r"매일\s*조선일보에\s*실린\s*칼럼")),
]

# v2 — 말미 잔여 꼬리(태그 키워드·기자명): 마지막 문장 종결 이후의 짧은 무종결 꼬리
RE_SENT_END = re.compile(r"[.!?…”\"』」)]\s")
RE_TRAILING_REPORTER = re.compile(r"(?:[가-힣]{1,6}=)?[가-힣]{2,4}\s*기자\s*$")
TAIL_MAX_LEN = 40


def _collapse(s: str) -> tuple[str, list[int]]:
    """공백 전부 제거 + 원본 인덱스 매핑 (공백 표기 차이를 무시한 비교용)"""
    out, m = [], []
    for i, ch in enumerate(s):
        if not ch.isspace():
            out.append(ch)
            m.append(i)
    return "".join(out), m


def find_prefix_end(text: str, title: str) -> tuple[int, str] | None:
    """선두 노이즈의 끝 위치. (끝 오프셋, 규칙명) 또는 None(없으면 통과)."""
    m = RE_HEADER.search(text)
    if m and m.start() < PREFIX_SEARCH_LIMIT:
        return m.end(), "prefix_header"
    m = RE_NEWSLETTER.search(text)
    if m and m.start() < PREFIX_SEARCH_LIMIT:
        return m.end(), "prefix_newsletter"
    # 폴백: 본문이 (짧은 섹션명 +) 제목 반복으로 시작하면 제목 끝까지 절단.
    # 다중 문장형 제목("헤드라인… 부제")은 첫 세그먼트('…'까지)만 헤드라인 반복으로 본다
    # — 부제는 본문 리드로 남는 경우가 있음 (골든셋 관찰).
    seg = title
    for delim in ("…", "..."):
        if delim in title:
            seg = title.split(delim, 1)[0] + delim
            break
    t_c, _ = _collapse(seg)
    if t_c:
        x_c, x_map = _collapse(text)
        pos = x_c.find(t_c)
        if 0 <= pos <= 20:  # 섹션명 정도만 앞에 허용
            end_c = pos + len(t_c) - 1
            end = x_map[end_c] + 1
            # 제목 뒤 공백까지 포함
            while end < len(text) and text[end].isspace():
                end += 1
            return end, "prefix_title_repeat"
    return None


def find_suffix_start(text: str, from_pos: int) -> tuple[int, str] | None:
    """말미 노이즈의 시작 위치. 앵커 중 가장 이른 매치. 없으면 None."""
    best = None
    for name, pat in SUFFIX_ANCHORS:
        m = pat.search(text, from_pos)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), name)
    return best


def find_tail_residue(text: str, start: int, end: int) -> tuple[int, str] | None:
    """v2: [start,end) 구간 끝의 잔여 꼬리(태그 키워드·기자명) 시작 위치."""
    seg = text[start:end]
    if not seg.strip():
        return None
    # 마지막 문장 종결 위치
    last = None
    for m in RE_SENT_END.finditer(seg):
        last = m.end()
    if last is None:
        # 종결 없음 — 기자명 단독 꼬리만 검사
        m = RE_TRAILING_REPORTER.search(seg)
        if m and m.start() > 0:
            return start + m.start(), "tail_reporter"
        return None
    tail = seg[last:]
    if not tail.strip():
        return None
    if len(tail.strip()) <= TAIL_MAX_LEN and not RE_SENT_END.search(tail + " "):
        return start + last, "tail_keywords"
    m = RE_TRAILING_REPORTER.search(seg)
    if m and m.start() >= last:
        return start + m.start(), "tail_reporter"
    return None


def clean_text(text: str, title: str) -> tuple[str, list[dict]]:
    """본문에서 노이즈를 제거하고 (정제본, removed_spans)를 반환.

    removed_spans: [{start, end, rule, text}] — 원본 오프셋 기준, 겹침 없음, 정렬됨.
    보존 인바리언트: 정제본 + 제거분을 오프셋 순서로 이으면 원문과 동일.
    """
    spans: list[dict] = []
    body_start = 0
    body_end = len(text)

    p = find_prefix_end(text, title)
    if p:
        body_start = p[0]
        spans.append({"start": 0, "end": p[0], "rule": p[1]})

    # 앵커가 본문 시작 지점과 정확히 겹치면 본문이 통째로 UI 블록인 기사([속보] 등) — 빈 본문 허용
    s = find_suffix_start(text, body_start)
    if s and s[0] >= body_start:
        body_end = s[0]
        spans.append({"start": s[0], "end": len(text), "rule": s[1]})

    # 미드 리터럴 (본문 구간 안에서만)
    for pat in RE_MID_LITERALS:
        for m in pat.finditer(text, body_start, body_end):
            spans.append({"start": m.start(), "end": m.end(), "rule": "mid_literal"})

    # v2: 말미 잔여 꼬리 (서픽스 절단 후 남은 본문 구간의 끝)
    mid_spans = sorted([sp for sp in spans if sp["rule"] == "mid_literal"], key=lambda x: x["start"])
    seg_end = body_end
    t = find_tail_residue(text, body_start, seg_end)
    if t and all(not (sp["start"] <= t[0] < sp["end"]) for sp in spans):
        spans.append({"start": t[0], "end": seg_end, "rule": t[1]})
        # 꼬리 구간과 겹치는 미드 스팬 제거(포함됨)
        spans = [sp for sp in spans if not (sp["rule"] == "mid_literal" and sp["start"] >= t[0])]

    spans.sort(key=lambda x: x["start"])
    # 겹침 방지(안전망): 앞 스팬과 겹치면 뒤 스팬을 버림
    dedup: list[dict] = []
    for sp in spans:
        if dedup and sp["start"] < dedup[-1]["end"]:
            continue
        dedup.append(sp)
    spans = dedup

    kept: list[str] = []
    cursor = 0
    for sp in spans:
        kept.append(text[cursor:sp["start"]])
        sp["text"] = text[sp["start"]:sp["end"]]
        cursor = sp["end"]
    kept.append(text[cursor:])
    clean = "".join(kept)

    # 보존 인바리언트: 정제본+제거분 = 원문
    rebuilt, cursor, ki = [], 0, 0
    for sp in spans:
        rebuilt.append(kept[ki]); ki += 1
        rebuilt.append(sp["text"])
    rebuilt.append(kept[ki])
    assert "".join(rebuilt) == text, "보존 인바리언트 위반: 정제본+제거분 ≠ 원문"

    return clean.strip(), spans


def main(argv=None) -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="P1 정제: articles.jsonl → articles_clean.jsonl (+trace 사이드카)")
    ap.add_argument("--input", type=Path, default=Path("data/articles.jsonl"))
    ap.add_argument("--output", type=Path, default=Path("data/articles_clean.jsonl"))
    ap.add_argument("--trace", type=Path, default=Path("data/articles_clean_trace.jsonl"))
    args = ap.parse_args(argv)

    n = {"prefix": 0, "suffix": 0, "mid": 0, "tail": 0, "untouched": 0}
    out_lines = []
    trace_lines = []
    with open(args.input, encoding="utf-8") as f:
        for line in f:
            a = json.loads(line)
            clean, spans = clean_text(a["text"], a["title"])
            rules = {sp["rule"] for sp in spans}
            if any(r.startswith("prefix") for r in rules):
                n["prefix"] += 1
            if any(r in ("english_button", "hashtag_block", "reporter_profile", "staff_writer",
                         "comment_widget", "video_player", "video_time", "ads_close", "hot_news",
                         "most_viewed", "recommend", "taboola", "newsletter_promo") for r in rules):
                n["suffix"] += 1
            if "mid_literal" in rules:
                n["mid"] += 1
            if any(r.startswith("tail") for r in rules):
                n["tail"] += 1
            if not spans:
                n["untouched"] += 1
            out_lines.append(json.dumps({
                "article_id": a["article_id"],
                "title": a["title"],
                "posted_date": a["posted_date"],
                "url": a["url"],
                "text": clean,
            }, ensure_ascii=False))
            trace_lines.append(json.dumps({
                "article_id": a["article_id"],
                "pipeline_version": PIPELINE_VERSION,
                "removed_spans": spans,
            }, ensure_ascii=False))

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    args.trace.parent.mkdir(parents=True, exist_ok=True)
    args.trace.write_text("\n".join(trace_lines) + "\n", encoding="utf-8")
    print(f"{PIPELINE_VERSION}: {len(out_lines)}건 정제 — 선두 {n['prefix']} · 말미 {n['suffix']} · "
          f"중간 {n['mid']} · 꼬리 {n['tail']} · 무변경 {n['untouched']}")
    print(f"본문(코어 5필드): {args.output}")
    print(f"감사 사이드카(removed_spans): {args.trace}")


if __name__ == "__main__":
    main()
