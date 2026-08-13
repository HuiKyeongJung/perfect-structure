# -*- coding: utf-8 -*-
"""P3 Stage C — 룰 검증기 (§5.6): 역검증·metric 실존·교차검증·감사 플래그·패스스루 스모크.

역할 구분:
- 파괴적 검사(destructive_issues): 위반 시 해당 item을 수리/폐기 경로로 보낸다
  (역검증 실패 = 환각 값, metric 창작 어휘, 스키마 형식 위반)
- 감사 플래그(audit_flags): 파괴하지 않고 trace에 남긴다
  (forecast 사전 히트↔N 불일치 — 자동 승격은 골든 역행이라 금지(§5.6),
   value_type·direction 룰 교차검증 불일치)

패스스루 스모크(§5.6 구현 순서 ③): 골든 508 Claim을 이 검증기에 통과시켜
룰이 정답을 파괴하지 않는지 HCX 0콜로 확인 — `python -m src.p3_stage_c --passthrough`.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from src.p3_schemas import ClaimRecord

# 전망 표현 사전(26차 판례 + §5.6) — 감사 플래그 전용. 자동 승격 금지.
# (리뷰 실측: 플래그 25건 중 골든 오류 0 — 형제 Claim 반응·명사 '계획' 오탐이 대부분.
#  '~겠다'류 추가는 소음 증가 트레이드오프라 보류, '게 됐'·'앞두'만 보강)
FORECAST_LEXICON = (
    "전망", "예상", "예측", "추산", "계획", "목표", "할 것", "될 것", "넘어설 것",
    "가능성", "우려", "변수", "유력", "달할 것", "이를 것", "게 됐", "앞두",
)
_INCREASE = ("증가", "늘어", "늘었", "상승", "올랐", "오르", "오른", "올라", "높아",
             "확대", "급증", "뛰", "불어나", "커졌")
_DECREASE = ("감소", "줄어", "줄었", "하락", "떨어", "내렸", "낮아", "축소", "급감",
             "밑돌", "위축")
_DIR_WORDS = "|".join(_INCREASE + _DECREASE)


def _value_pattern(value: str, unit: str) -> re.Pattern:
    """value+unit 실존 매칭 패턴 — 역검증과 원문 위치 탐색이 공유."""
    v = (value or "").replace(" ", "")
    u = (unit or "").replace(" ", "")
    vpat = r"\s*".join(re.escape(ch) for ch in v)
    upat = (r"\s?" + r"\s*".join(re.escape(ch) for ch in u)) if u else ""
    guard_l = r"(?<![\d.,만억조천])"
    guard_r = r"(?![\d만억조천])" if not u else ""   # 문장부호는 정당한 우측 경계
    return re.compile(guard_l + vpat + upat + guard_r)


def value_in_sentence(value: str, unit: str, sentence: str) -> bool:
    """역검증(§4.1) — value+unit 결합(공백 무시)이 문장에 실존해야 한다.

    좌측 숫자 경계 필수: '3%'⊂'8.3%', '300조원'⊂'1300조원' 같은 절단 환각이
    부분 문자열로 통과하면 역검증이 무력화된다(리뷰 실측 반례). unit이 없으면
    우측도 수사 경계('13만'⊂'13만4000', '1300'⊂'1300조원' 차단).
    원문 위에서 매칭한다 — 공백을 지우고 비교하면 나열 쉼표("21.8%, 9.2%")가
    천 단위 쉼표("1,300조")와 구분되지 않아 정당한 값이 차단된다.
    """
    if not value:
        return False
    return bool(_value_pattern(value, unit).search(sentence or ""))


def value_position(value: str, unit: str, sentence: str, start: int = 0) -> int | None:
    """value+unit의 원문 등장 위치(§5.6 출력 순서 계약 검사용). start 이후 첫 매치."""
    if not value:
        return None
    m = _value_pattern(value, unit).search(sentence or "", start)
    return m.start() if m else None


def _word_exists(word: str, text: str) -> bool:
    # 숫자로 시작하는 어휘는 단순 substring이 '12·3등급'⊃'2등급' 오허용을 만들므로
    # 경계 있는 나열 패턴만 사용(판례 4 준용: "2·3·4등급" = 각 등급 실존)
    m = re.fullmatch(r"(\d+)(\D+)", word)
    if m:
        num, suf = m.groups()
        pat = re.compile(
            rf"(?<![\d.])(?:\d+\s*[·,~/／]\s*)*{re.escape(num)}(?:\s*[·,~/／]\s*\d+)*\s*{re.escape(suf)}")
        return bool(pat.search(text))
    return word in text


def metric_missing_words(metric: str, article_text: str) -> list[str]:
    """구성 어휘 실존(§4.4) — metric의 각 어휘가 기사 정제본에 있어야 한다(조합 허용·창작 금지)."""
    words = [w for w in re.split(r"[\s()·]+", metric or "") if w]
    return [w for w in words if not _word_exists(w, article_text or "")]


def destructive_issues(claim: ClaimRecord, sentence: str, article_text: str) -> list[str]:
    """위반 시 수리/폐기 대상이 되는 검사 — 골든 패스스루에서 0건이어야 한다."""
    issues = list(claim.schema_issues())
    if not value_in_sentence(claim.value, claim.unit, sentence):
        issues.append(f"역검증 실패: '{claim.value}{claim.unit}' 문장 미실존")
    missing = metric_missing_words(claim.metric, article_text)
    if missing:
        issues.append(f"metric 창작 어휘: {missing}")
    return issues


# ── 룰 1차 분류기 (교차검증용 — LLM 판정과 대조, 불일치는 플래그) ──────────
def rule_direction(sentence: str) -> str | None:
    """어휘 기반 증감 방향. 양쪽 신호 혼재·무신호면 None(판정 보류)."""
    inc = any(k in sentence for k in _INCREASE)
    dec = any(k in sentence for k in _DECREASE)
    if inc and not dec:
        return "increase"
    if dec and not inc:
        return "decrease"
    return None


def rule_value_type(sentence: str, value: str, unit: str) -> str | None:
    """어휘·unit 패턴 기반 1차 분류. 확신 없으면 None.

    리뷰 계통 오류 반영: ① 증감 창은 소수점('(3.8%) 증가')을 통과하되 문장 경계는 막음
    ② %의 change_rate 판정은 값 '근접' 방향어만(사이에 다른 숫자가 끼면 보류 —
      '45.6%로 1%포인트 하락'의 45.6을 change_rate로 오발하던 결함)
    ③ share_ratio는 값 근접 창으로 축소 ④ '(으)로' 도달 구문은 증감액 배제.
    """
    u = (unit or "").strip()
    v = (value or "").strip()
    if not v:
        return None
    v_esc = re.escape(v)
    win = r"(?:[^.]|\.(?=\d)){0,15}"          # 소수점만 통과하는 창(문장 종결 '.'은 차단)
    if u in ("%", "％"):
        if re.search(rf"(전체|GDP|국내총생산)(의|에서)\s*{v_esc}\s*%", sentence):
            return "share_ratio"
        if re.search(rf"{v_esc}\s*%[^\d.]{{0,10}}(비중|비율|점유율|차지)", sentence) \
                or re.search(rf"(비중|비율|점유율)[^\d.]{{0,10}}{v_esc}\s*%", sentence):
            return "share_ratio"
        if re.search(rf"{v_esc}\s*%[^\d.]{{0,10}}(?:{_DIR_WORDS})", sentence):
            return "change_rate"              # 값 바로 뒤 방향어(사이 숫자 없음)만
        return None
    if u in ("%p", "%P", "%포인트", "포인트") \
            and re.search(rf"{v_esc}\s*{re.escape(u)}{win}(?:{_DIR_WORDS})", sentence):
        return "change_amount"
    if u and rule_direction(sentence):
        if re.search(rf"{v_esc}\s*{re.escape(u)}(?!\s*[으]?로(?![\d]))" + win + rf"(?:{_DIR_WORDS})",
                     sentence):
            return "change_amount"
        return "level"
    return None


def audit_flags(claim: ClaimRecord, sentence: str) -> list[str]:
    """비파괴 감사 — trace 기록용."""
    flags = []
    if (claim.forecast or "N").upper() == "N" and any(k in sentence for k in FORECAST_LEXICON):
        flags.append("forecast_lexicon_hit_but_N")
    rd = rule_direction(sentence)
    if rd and claim.direction and rd != claim.direction:
        flags.append(f"direction_rule_mismatch:rule={rd},llm={claim.direction}")
    rv = rule_value_type(sentence, claim.value, claim.unit)
    if rv and claim.value_type and rv != claim.value_type:
        flags.append(f"value_type_rule_mismatch:rule={rv},llm={claim.value_type}")
    return flags


# ── 골든 패스스루 스모크 (§5.6 구현 순서 ③) ───────────────────────────────
def run_passthrough(golden_path=None, articles_path=None):
    from src import config
    from src.p3_golden import load_golden, GOLDEN_DEFAULT

    articles_path = articles_path or (config.data_dir() / "articles_clean.jsonl")
    gold = load_golden(golden_path or GOLDEN_DEFAULT)
    arts = {}
    with open(articles_path, encoding="utf-8") as f:
        for line in f:
            a = json.loads(line)
            arts[a["article_id"]] = a["text"]

    destroyed: list[tuple[str, list[str]]] = []
    flags_count: dict[str, int] = {}
    dir_agree = dir_total = vt_agree = vt_total = 0
    for c in gold.claims:
        issues = destructive_issues(c, c.claim, arts.get(c.article_id, ""))
        if issues:
            destroyed.append((c.claim_id, issues))
        for fl in audit_flags(c, c.claim):
            flags_count[fl.split(":")[0]] = flags_count.get(fl.split(":")[0], 0) + 1
        rd = rule_direction(c.claim)
        if c.direction and rd:
            dir_total += 1
            dir_agree += (rd == c.direction)
        rv = rule_value_type(c.claim, c.value, c.unit)
        if c.value_type and rv:
            vt_total += 1
            vt_agree += (rv == c.value_type)

    # E 사영까지 — 골든 전건이 계약 위반 없이 7필드로 나가는지 + eligible 총계
    handoffs = [c.to_handoff() for c in gold.claims]
    n_eligible = sum(1 for h in handoffs if h["kosis_eligible"])
    excluded_bad = [e for e in gold.excluded if e.schema_issues()]
    return {
        "claims": len(gold.claims), "excluded": len(gold.excluded),
        "destroyed": destroyed, "excluded_bad": excluded_bad,
        "handoff_ok": len(handoffs), "eligible_true": n_eligible,
        "audit_flags": flags_count,
        "direction_rule_agreement": (dir_agree, dir_total),
        "value_type_rule_agreement": (vt_agree, vt_total),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Stage C 룰 — 골든 패스스루 스모크")
    ap.add_argument("--passthrough", action="store_true")
    ap.add_argument("--golden", type=Path, default=None)
    args = ap.parse_args()
    if not args.passthrough:
        ap.error("--passthrough 를 지정하세요")
    r = run_passthrough(args.golden)
    print(f"골든 Claim {r['claims']} · 제외 {r['excluded']}")
    print(f"파괴된 Claim: {len(r['destroyed'])}건")
    for cid, issues in r["destroyed"][:20]:
        print(f"  ✗ {cid}: {issues}")
    print(f"제외 코드 위반: {len(r['excluded_bad'])}건")
    print(f"7필드 사영: {r['handoff_ok']}건 전부 성공 · eligible TRUE {r['eligible_true']}")
    print(f"감사 플래그 분포: {r['audit_flags']}")
    da, dt_ = r["direction_rule_agreement"]
    va, vt_ = r["value_type_rule_agreement"]
    print(f"direction 룰↔골든 합치: {da}/{dt_} ({da / dt_:.1%})" if dt_ else "direction 표본 없음")
    print(f"value_type 룰↔골든 합치: {va}/{vt_} ({va / vt_:.1%})" if vt_ else "value_type 표본 없음")
    ok = not r["destroyed"] and not r["excluded_bad"]
    print(f"\n패스스루 {'통과' if ok else '실패'}")
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
