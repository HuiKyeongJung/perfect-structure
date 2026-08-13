# -*- coding: utf-8 -*-
"""P3 Stage A — 숫자 문장 필터 (룰, §5.6).

ver1 범위 = 아라비아 숫자를 포함한 문장만(42차 검증: 한글 수사 전용 문장의 실질 누락 0).
sentences.jsonl ⋈ articles_clean.jsonl(posted_date·본문)을 조인해 후보 문장 목록을 만든다.
Recall 우선 — 여기서 놓친 문장은 뒤에서 되살릴 수 없다(§3 입구 원칙).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from src import config

SENTENCES_DEFAULT = config.data_dir() / "sentences.jsonl"
ARTICLES_DEFAULT = config.data_dir() / "articles_clean.jsonl"

RE_DIGIT = re.compile(r"\d")


def is_numeric_sentence(text: str | None) -> bool:
    return bool(text) and bool(RE_DIGIT.search(text))


@dataclass
class SentenceCandidate:
    """Stage B 입력 단위 — 문장 + 역참조에 필요한 메타."""

    article_id: str
    sent_id: str
    text: str
    posted_date: str
    title: str
    start: int | None = None   # 정제본 문자 오프셋(리니지용, sentences.jsonl 유래)
    end: int | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.article_id, self.sent_id)


def load_articles(path: Path | str = ARTICLES_DEFAULT) -> dict[str, dict]:
    arts: dict[str, dict] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            a = json.loads(line)
            arts[a["article_id"]] = a
    return arts


def collect_candidates(sentences_path: Path | str = SENTENCES_DEFAULT,
                       articles_path: Path | str = ARTICLES_DEFAULT,
                       ) -> tuple[list[SentenceCandidate], list[dict], dict[str, dict]]:
    """(숫자 문장 후보, 비대상 문장 원본, 기사 dict) — 회계는 후보 기준, 비대상은 참고 보존."""
    arts = load_articles(articles_path)
    candidates: list[SentenceCandidate] = []
    non_numeric: list[dict] = []
    seen_keys: set[tuple[str, str]] = set()
    with open(sentences_path, encoding="utf-8") as f:
        for line in f:
            s = json.loads(line)
            art = arts.get(s["article_id"])
            if art is None:
                raise ValueError(f"기사 역참조 실패: {s['article_id']} (전수 회계 훼손)")
            key = (s["article_id"], s["sent_id"])
            if key in seen_keys:   # 집합 회계가 중복에 무감하므로 입구에서 차단(리뷰)
                raise ValueError(f"문장 키 중복: {key} — 전수 회계 훼손 신호")
            seen_keys.add(key)
            if is_numeric_sentence(s.get("text")):
                candidates.append(SentenceCandidate(
                    article_id=s["article_id"], sent_id=s["sent_id"], text=s["text"],
                    posted_date=art["posted_date"], title=art.get("title", ""),
                    start=s.get("start"), end=s.get("end"),
                ))
            else:
                non_numeric.append(s)
    return candidates, non_numeric, arts
