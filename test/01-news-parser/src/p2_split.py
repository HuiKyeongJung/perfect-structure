# -*- coding: utf-8 -*-
"""P2 문장화(정규식판): articles_clean.jsonl → sentences.jsonl.

설계: "경계 후보를 넓게 찾고 → 보호 필터로 가짜를 걸러내고 → 절단 → 검산".
- 경계 후보 2종
    종결형: [.!?…] + 닫는따옴표·괄호(0~2) + 공백   ← 공백 요구 덕에 소수점("1.0%")은 애초에 후보가 안 됨
    기호형: 공백 뒤의 ◇·☞·▲ 앞                     ← 무종결 소제목·목록의 분리 신호 (골든셋 관찰)
- 보호 필터: 따옴표(“” ‘’ " ')·괄호(() [] <>) 열림 상태 추적 — 열린 구간 안의 후보는 기각
- 오프셋: 정제본 기준 [start, end) — text[start:end] == 문장
- 검산: 전수 보존 인바리언트(정제본의 모든 비공백 문자는 정확히 한 문장에 속함)

알려진 한계(골든 채점으로 물량 관리): 기호 소제목의 '끝' 경계(무종결 → 다음 문장과 병합됨),
표식 없는 소제목, 따옴표로 끝나는 의문 인용의 문장 계속("…하나요?" 같은) 등.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PIPELINE_VERSION = "sent_v3"

# 종결부호에서 '…'와 '...'은 제외 — 칼럼 제목("그만… 판사는") 등 제목 내부의 말줄임을
# 종결로 오인해 과잉 절단됨(실측). 연속 마침표의 마지막 '.'도 lookbehind로 차단. (sent_v3)
RE_TERMINAL = re.compile(r"(?:(?<!\.)\.|[!?])[\"”’'』」)\]]{0,2}(\s+)")
# 소제목·목록 기호: ▷(인사 발령 하위 항목) 추가, 공백 없이 붙는 경우도 절단. (sent_v3)
RE_SYMBOL = re.compile(r"([◇☞▲▷])")

OPEN_PAREN = "([<"
CLOSE_PAREN = ")]>"


def compute_outside(text: str) -> list[bool]:
    """각 위치가 따옴표·괄호 밖인지. 여는 문자 자체는 '안'으로 취급.

    따옴표는 짝 맞춤이 아니라 **토글**로 처리한다 — 크롤링 본문에 닫는 따옴표를
    여는 모양(“)으로 쓴 기사가 실재해서(“…하느냐“고), 짝 맞춤은 '영원히 열림'에
    갇혀 이후 경계를 전부 놓친다. 토글은 방향이 뒤집혀도 안전하다. (sent_v2)
    """
    outside = [True] * len(text)
    paren = 0
    dq = sq = straight_d = straight_s = False
    for i, ch in enumerate(text):
        if ch in "“”":
            dq = not dq
        elif ch in "‘’":
            sq = not sq
        elif ch == '"':
            straight_d = not straight_d
        elif ch == "'":
            straight_s = not straight_s
        elif ch in OPEN_PAREN:
            paren += 1
        elif ch in CLOSE_PAREN:
            paren = max(0, paren - 1)
        outside[i] = not (dq or sq or paren or straight_d or straight_s)
    return outside


def find_boundaries(text: str) -> list[int]:
    """다음 문장이 시작되는 위치 목록 (정렬됨)."""
    outside = compute_outside(text)
    n = len(text)
    bset: set[int] = set()
    # 종결형: 닫는 부호를 소화한 뒤의 공백 위치가 '밖'이어야 진짜 경계
    for m in RE_TERMINAL.finditer(text):
        ws_start = m.start(1)
        if outside[ws_start] and m.end() < n:
            bset.add(m.end())
    # 기호형: 기호 위치가 '밖'이면 그 앞에서 절단 (문서 첫 위치 제외)
    for m in RE_SYMBOL.finditer(text):
        pos = m.start(1)
        if pos > 0 and outside[pos]:
            bset.add(pos)
    return sorted(bset)


def split_spans(text: str) -> list[tuple[int, int]]:
    """문장 스팬 [start, end) 목록 — 양끝 공백 제외, 빈 조각 제거."""
    if not text.strip():
        return []
    cuts = [0] + find_boundaries(text) + [len(text)]
    spans = []
    for a, b in zip(cuts, cuts[1:]):
        seg = text[a:b]
        ls = len(seg) - len(seg.lstrip())
        rs = len(seg.rstrip())
        if rs > ls:
            spans.append((a + ls, a + rs))
    # 전수 보존 인바리언트: 비공백 문자 합이 원문과 동일
    joined = "".join(text[s:e] for s, e in spans)
    assert (
        "".join(c for c in joined if not c.isspace())
        == "".join(c for c in text if not c.isspace())
    ), "전수 보존 인바리언트 위반"
    return spans


def split_text(text: str) -> list[str]:
    """테스트·실험 편의용: 문장 문자열 목록."""
    return [text[s:e] for s, e in split_spans(text)]


def main(argv=None) -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="P2 문장화(정규식): articles_clean.jsonl → sentences.jsonl")
    ap.add_argument("--input", type=Path, default=Path("data/articles_clean.jsonl"))
    ap.add_argument("--output", type=Path, default=Path("data/sentences.jsonl"))
    args = ap.parse_args(argv)

    out_lines = []
    n_articles = n_sents = n_empty = 0
    with open(args.input, encoding="utf-8") as f:
        for line in f:
            a = json.loads(line)
            n_articles += 1
            spans = split_spans(a["text"])
            if not spans:
                n_empty += 1
            for i, (s, e) in enumerate(spans, start=1):
                n_sents += 1
                out_lines.append(json.dumps({
                    "article_id": a["article_id"],
                    "sent_id": f"s{i:03d}",
                    "start": s,
                    "end": e,
                    "text": a["text"][s:e],
                    "para": None,
                    "pipeline_version": PIPELINE_VERSION,
                }, ensure_ascii=False))

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    print(f"{PIPELINE_VERSION}: 기사 {n_articles}건 → 문장 {n_sents}개 (빈 본문 {n_empty}건)")
    print(f"출력: {args.output}")


if __name__ == "__main__":
    main()
