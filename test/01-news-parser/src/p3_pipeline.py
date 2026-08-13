# -*- coding: utf-8 -*-
"""P3 파이프라인 오케스트레이터 — A(필터) → B(추출기 주입) → C(룰 검증·해소) → E(산출).

Stage B는 `extractor` 콜러블로 주입한다:
    extractor(cand: SentenceCandidate) -> list[dict]   # 문장당 raw item 목록
raw item 필드: kind(claim|excluded) · exclusion_code · forecast(Y/N — claim 필수) · metric ·
value · unit · period(표면형 period_expr 또는 이미 해소된 표기) · value_type · direction ·
comparison_basis · note · metric_normalized(선택)

라우팅 규약(리뷰 반영):
- kind ∉ {claim, excluded} → errors (미지 kind가 eligible=true Claim으로 폴스루 금지)
- kind=claim인데 exclusion_code가 제외 코드 → errors (kind×code 모순 = 저신뢰 신호, 수리 대상)
- kind=claim인데 forecast ∉ {Y,N} → errors (§5.6 필수 2값 — 기본값 부여는 위험 방향)
- kind=excluded + PARTIAL_PERIOD → errors (§4.8: 제외 코드 아님 — 5단계 수리 규칙 1순위 예약)
- PARTIAL_PERIOD 마킹은 LLM 값을 쓰지 않고 resolver의 partial에서 재계산(골든 전건 일치 실증)
- 앵커 시프트("전년동기")는 원문 순서상 직전의 해소된 형제 period(없으면 첫 후행값) 기준,
  partial(부분기간) 앵커 허용 — "올 들어 20일까지 … 전년 동기 대비"가 골든 실사례(s010)
- claim_id 일련번호는 claim-kind item 슬롯이 소모한다 — 한 item이 검증 실패로 빠져도
  뒤 Claim들의 id가 밀리지 않는다(재실행 안정성, §4.1 조인 키 보호)
- 서킷브레이커: 오류 문장이 배치의 3% 초과 → 파이프라인 실패(§5.6 — 계통 결함 신호)

실 HCX 백엔드·수리 루프·record-replay 캐시 래퍼는 5단계에서 extractor 바깥에 씌운다.
지금은 stub(골든 라벨 반환)으로 A→E 전 구간을 검증한다(§5.6 무HCX 스모크 ③).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

from src.p3_schemas import ClaimRecord, ExcludedRecord, PIPELINE_VERSION, \
    CONTRACT_EXCLUSION_CODES, CLAIM_ALLOWED_CODES, is_std_period
from src.p3_stage_a import SentenceCandidate, collect_candidates
from src.p3_period import resolve_period, ANCHOR_SHIFT_EXPRS, SAME_PERIOD_EXPRS
from src.p3_stage_c import destructive_issues, audit_flags, value_position, rule_direction
from src.p3_emit import emit_all, AccountingError

Extractor = Callable[[SentenceCandidate], list[dict]]
CIRCUIT_BREAKER_RATE = 0.03
CARRY_MAX_DISTANCE = 3     # 문장 간 시점 상속 상한 — 먼 앞 문장 오상속 방지(실측 18문장 사례)


def _carry_value(carry: dict, article_id: str, sent_idx: dict) -> str | None:
    """거리 상한 안에서만 직전 문장의 해소 시점을 물려준다."""
    hit = carry.get(article_id)
    if not hit:
        return None
    period, idx = hit
    return period if sent_idx.get(article_id, 0) - idx < CARRY_MAX_DISTANCE else None


def _get(item: dict, key: str) -> str:
    v = item.get(key, "")
    return str(v).strip() if v is not None else ""


_APPROX_SUFFIX = re.compile(r"\s*(이상|이하|이내|넘게|초과|미만|가량|안팎|남짓|가까이)$")
# 구간 표기 '~원대/조원대' — 수사·화폐 뒤의 '대'만 제거(자동차 '3대'의 단위 '대'는 보존)
_RANGE_DAE = re.compile(r"(?<=[원조억만천])대$")
_UNIT_TAIL = re.compile(r"(%포인트|%p|%|달러|원|명|톤|건|㏊|ha)$")


def normalize_value_unit(value: str, unit: str) -> tuple[str, str]:
    """LLM 출력 관행의 결정적 정규화(§5.1 — 룰이 할 수 있는 일). dev 실측 오류의 최빈 3종:

    ① value에 단위 중복('441만4000명'+unit '명' → 역검증 '명명' 실패)
    ② value에 경계·근사어('8% 이상' — 29차 사전상 value 미포함, 정보는 claim 원문 보존)
    ③ unit 내부 공백('% p') · unit 누락 시 value 꼬리의 단위 분리('3만원' → '3만'+'원')
    정규화 후에도 역검증(value+unit 문장 실존)은 그대로 돌므로 환각 차단은 약화되지 않는다.
    """
    u = re.sub(r"\s+", "", unit or "")
    v = (value or "").strip()
    v = re.sub(r"^약\s*", "", v)
    v = _APPROX_SUFFIX.sub("", v).strip()
    v = _RANGE_DAE.sub("", v).strip()      # '1000조원대' → '1000조원'(구간 표기 — 29차 사전)
    if u and v.endswith(u):
        v = v[: -len(u)].strip()
        v = _APPROX_SUFFIX.sub("", v).strip()
    m = _UNIT_TAIL.search(v)
    if m and len(v) > len(m.group(1)):
        # value 꼬리에 단위 표기가 남아 있으면 분리 — value 쪽 표기가 기사 verbatim일
        # 개연성이 높으므로 unit이 있어도 교체(dev 실측: v='7%포인트'+u='%p' → '7%포인트%p')
        u = m.group(1)
        v = v[: -len(u)].strip()
    return v, u


def restore_unit_notation(value: str, unit: str, sentence: str) -> str:
    """단위 축약의 verbatim 복원 — LLM이 '%포인트'를 '%p'로 줄이면 기사 표기로 되돌린다.

    §4.1: 단위는 기사 표기 그대로. 복원 후보가 문장에 실존할 때만 교체(결정적·안전).
    """
    if unit in ("%p", "%P") and value:
        for cand_u in ("%포인트", "%p"):
            if (value + cand_u).replace(" ", "") in re.sub(r"\s", "", sentence):
                return cand_u
    return unit


def _err(cand: SentenceCandidate, stage: str, reason: str, item, item_index=None) -> dict:
    return {"article_id": cand.article_id, "sent_id": cand.sent_id, "sentence": cand.text,
            "stage": stage, "reason": reason, "item": item, "item_index": item_index}


def process_sentence(cand: SentenceCandidate, items: list[dict], article_text: str,
                     carry_anchor: str | None = None, article_anchor: str | None = None
                     ) -> tuple[list[ClaimRecord], list[ExcludedRecord], list[dict], list[dict]]:
    """한 문장의 raw item들 → (claims, excluded, errors, traces).

    claims와 traces는 같은 순서·같은 길이(쌍). trace에는 item_index(안정 키)가 실린다.
    carry_anchor: 같은 기사 직전 문장의 해소 period — "이 기간"·"전년 동기"가 문장 경계를
    넘어 앞 문장을 가리키는 경우를 위한 상속 씨앗(dev 실측 최빈 오류).
    """
    claims: list[ClaimRecord] = []
    excluded: list[ExcludedRecord] = []
    errors: list[dict] = []
    traces: list[dict] = []
    ANCHORED = ANCHOR_SHIFT_EXPRS | SAME_PERIOD_EXPRS

    # period 해소 2패스 — ①비앵커 먼저, ②앵커 표현은 원문 순서상 직전 형제
    #   (문장 내 형제가 없으면 직전 문장의 carry_anchor로 폴백)
    resolved: list = [None] * len(items)
    for i, it in enumerate(items):
        expr = _get(it, "period")
        if expr not in ANCHORED:
            resolved[i] = resolve_period(expr, cand.posted_date)
    # pass1 결과 스냅샷 — 앵커는 반드시 '비앵커 항목의 해소값'에서만 온다.
    # 스냅샷 없이 resolved를 그대로 읽으면 앵커 항목이 직전 앵커 항목의 *이미 시프트된*
    # 값을 다시 앵커로 잡아 -1년이 누적된다(실측: 한 문장의 3분기 3개가
    # 2024-Q3·2023-Q3·2022-Q3로 흩어짐). 스냅샷으로 1회 시프트를 보장한다.
    base = list(resolved)
    for i, it in enumerate(items):
        if resolved[i] is not None:
            continue
        anchor = None
        for j in range(i - 1, -1, -1):     # 직전 형제 우선(리뷰: 첫 값 고정은 오앵커)
            if base[j] is not None and base[j].period:
                anchor = base[j].period
                break
        if anchor is None:
            for j in range(i + 1, len(items)):
                if base[j] is not None and base[j].period:
                    anchor = base[j].period
                    break
        if anchor is None:
            anchor = carry_anchor          # 문장 간 상속(거리 제한)
        resolved[i] = resolve_period(_get(it, "period"), cand.posted_date, anchor=anchor)

    # pass3 — 기간 길이 표현("지난 5년")의 종점 상속.
    # 통계 기사는 리드에서 대상 시점을 확립하고 이후 문장이 그것을 공유하므로,
    # 문장 내 형제 → 기사 기준 시점(거리 무제한) 순으로 종점을 찾는다(실측 18건).
    sibling = next((r.period for r in resolved if r and r.period and not r.partial), None)
    for i, it in enumerate(items):
        r = resolved[i]
        if r is None or r.period is not None or r.method != "duration_no_anchor":
            continue
        for anc in (sibling, carry_anchor, article_anchor):
            if not anc:
                continue
            retry = resolve_period(_get(it, "period"), cand.posted_date, anchor=anc)
            if retry.period:
                resolved[i] = retry
                break

    prev_pos = -1
    order_violated_idx: set[int] = set()
    for idx, (it, r) in enumerate(zip(items, resolved)):
        kind = _get(it, "kind")
        if kind == "excluded":
            code = _get(it, "exclusion_code")
            if code not in CONTRACT_EXCLUSION_CODES:
                # PARTIAL_PERIOD 포함(§4.8: 제외 코드 아님) — 수리 대상으로 격리
                errors.append(_err(cand, "C", f"excluded인데 계약 밖 코드: {code!r}", it, idx))
                continue
            excluded.append(ExcludedRecord(article_id=cand.article_id, sent_id=cand.sent_id,
                                           sentence=cand.text, exclusion_code=code,
                                           note=_get(it, "note")))
            continue
        if kind != "claim":
            errors.append(_err(cand, "C", f"계약 밖 kind: {kind!r}", it, idx))
            continue

        raw_code = _get(it, "exclusion_code")
        if raw_code not in CLAIM_ALLOWED_CODES:
            errors.append(_err(cand, "C", f"kind=claim인데 제외 코드 {raw_code!r} — kind×code 모순", it, idx))
            continue
        forecast = _get(it, "forecast").upper()
        if forecast not in ("Y", "N"):
            errors.append(_err(cand, "C", f"forecast 누락/이탈: {forecast!r} (Y/N 필수)", it, idx))
            continue

        nv, nu = normalize_value_unit(_get(it, "value"), _get(it, "unit"))
        nu = restore_unit_notation(nv, nu, cand.text)
        # direction은 룰 우선(§5.6) — 단, **증감형(change_rate·change_amount)에서만** 채운다.
        # 골든 실측: direction이 있는 265건은 전부 증감형이고 비증감형은 100% 공백이며,
        # 증감형에서 양쪽 값이 있을 때 룰↔골든 209/209 일치(불일치 0). 조건 없이 룰을 적용하면
        # 문장에 증감 어휘가 있다는 이유로 수준값(level)에까지 방향이 붙어 골든을 파괴한다.
        vt = _get(it, "value_type")
        direction = _get(it, "direction")
        if vt in ("change_rate", "change_amount"):
            direction = rule_direction(cand.text) or direction
        else:
            direction = ""
        c = ClaimRecord(
            claim_id=f"{cand.article_id}-C000",  # 임시 — run()에서 슬롯 기반 정식 부여
            article_id=cand.article_id, sent_id=cand.sent_id,
            posted_date=cand.posted_date, claim=cand.text,
            metric=_get(it, "metric"), metric_normalized=_get(it, "metric_normalized"),
            value=nv, unit=nu,
            value_type=vt, direction=direction,
            period=r.period or "", comparison_basis=_get(it, "comparison_basis"),
            forecast=forecast,
            exclusion_code="PARTIAL_PERIOD" if r.partial else "",
            note=_get(it, "note"),
        ).finalize()

        issues = destructive_issues(c, cand.text, article_text)
        if issues:
            errors.append(_err(cand, "C", "; ".join(issues), it, idx))
            continue

        # §5.6 출력 순서 계약 검사(비파괴 — 감사 플래그)
        pos = value_position(c.value, c.unit, cand.text, start=prev_pos + 1)
        if pos is None and prev_pos >= 0:
            order_violated_idx.add(idx)
        elif pos is not None:
            prev_pos = pos

        flags = audit_flags(c, cand.text)
        if idx in order_violated_idx:
            flags.append("item_order_violation")
        claims.append(c)
        traces.append({
            "article_id": cand.article_id, "sent_id": cand.sent_id, "item_index": idx,
            "offsets": [cand.start, cand.end],
            "period_expr": _get(it, "period"), "period_resolved": r.period,
            "period_method": r.method, "partial": r.partial,
            "audit_flags": flags,
            "pipeline_version": PIPELINE_VERSION,
        })
    return claims, excluded, errors, traces


def _backfill_duration(claims: list[ClaimRecord], traces: list[dict],
                       article_base: dict[str, str]) -> int:
    """기간 길이 표현의 종점을 기사 기준 시점으로 후보정. 반환: 보정 건수.

    순차 처리 중에는 기사 기준 시점이 아직 확립되지 않아 **첫 문장의 duration이 항상
    미해소**로 남는다(실측: "5년간 먹거리 물가가 20% 넘게 상승" — 기준 시점은 다음 문장의
    '지난달'). 전 문장 처리가 끝난 뒤 한 번 더 해소해 이 순서 의존성을 없앤다.
    미해소로 남는 경우는 그대로 둔다(§8-6 억지 추정 금지).
    """
    fixed = 0
    for c, t in zip(claims, traces):
        if c.period or t.get("period_method") != "duration_no_anchor":
            continue
        base = article_base.get(c.article_id)
        if not base:
            continue
        r = resolve_period(t.get("period_expr", ""), c.posted_date, anchor=base)
        if not r.period:
            continue
        c.period = r.period
        c.exclusion_code = "PARTIAL_PERIOD" if r.partial else ""
        c.kosis_eligible = None          # period가 바뀌었으므로 재파생
        c.finalize()
        t.update(period_resolved=r.period, period_method=r.method, partial=r.partial)
        fixed += 1
    return fixed


def run(extractor: Extractor, outdir: Path | str,
        sentences_path=None, articles_path=None, normalizer=None,
        article_filter: set | None = None,
        breaker_rate: float = CIRCUIT_BREAKER_RATE) -> dict:
    """A→B(주입)→C→D(사전)→E 전 구간 실행. 반환: emit 요약(회계 수치·경로).

    normalizer(선택): src.p3_stage_d.MetricNormalizer — metric_normalized가 빈 Claim을
    사전으로 채운다(v0.4 계약 필드). 추출기가 이미 채운 값은 우선.
    article_filter(선택): 기사 ID 집합 — dev 8 부분 실행 등. 회계도 그 부분집합 기준.
    """
    kw = {}
    if sentences_path:
        kw["sentences_path"] = sentences_path
    if articles_path:
        kw["articles_path"] = articles_path
    candidates, _non_numeric, arts = collect_candidates(**kw)
    if article_filter:
        candidates = [c for c in candidates if c.article_id in article_filter]

    all_claims: list[ClaimRecord] = []
    all_excluded: list[ExcludedRecord] = []
    all_errors: list[dict] = []
    all_traces: list[dict] = []
    seq: dict[str, int] = {}          # 기사별 claim_id 슬롯 카운터
    error_sentences: set = set()
    carry: dict[str, tuple[str, int]] = {}   # 기사별 (해소 period, 문장 인덱스)
    sent_idx: dict[str, int] = {}            # 기사별 처리 문장 순번(거리 계산용)
    article_base: dict[str, str] = {}        # 기사에서 처음 확립된 표준형 시점(거리 무제한)

    for cand in candidates:
        sent_idx.setdefault(cand.article_id, 0)
        try:
            items = extractor(cand)
        except Exception as exc:      # LLM 호출 실패 등 — 한 콜이 전체를 죽이지 않게
            all_errors.append(_err(cand, "B", f"EXTRACTION_ERROR: {exc}", None))
            error_sentences.add(cand.key)
            continue
        if not items:
            all_errors.append(_err(cand, "B", "EXTRACTION_ERROR: 추출 결과 없음", None))
            error_sentences.add(cand.key)
            continue

        cl, ex, er, tr = process_sentence(cand, items, arts[cand.article_id]["text"],
                                          carry_anchor=_carry_value(carry, cand.article_id,
                                                                     sent_idx),
                                          article_anchor=article_base.get(cand.article_id))

        # Stage C 실패 → 수리 루프 1회(§5.6): 실패 사유를 피드백으로 재추출.
        # 채택 기준: 파괴적 실패가 엄격히 줄었을 때만 전량 교체(개선 없으면 원본 유지).
        # ※ §5.6의 partial-accept(통과 item 동결)와 달리 문장 전량 재처리 — 슬롯 매칭
        #   모호성을 피하는 단순화이며, 채택 가드가 퇴행을 막는다.
        repair_fn = getattr(extractor, "repair", None)
        c_fails = [e for e in er if e["stage"] == "C" and e.get("item") is not None]
        if c_fails and repair_fn is not None:
            feedback = " | ".join(e["reason"][:120] for e in c_fails[:5])
            try:
                items2 = repair_fn(cand, feedback)
            except Exception:
                items2 = None
            if items2:
                cl2, ex2, er2, tr2 = process_sentence(
                    cand, items2, arts[cand.article_id]["text"],
                    carry_anchor=_carry_value(carry, cand.article_id, sent_idx),
                    article_anchor=article_base.get(cand.article_id))
                fails2 = [e for e in er2 if e["stage"] == "C" and e.get("item") is not None]
                if len(fails2) < len(c_fails):
                    cl, ex, er, tr = cl2, ex2, er2, tr2

        # 다음 문장으로 넘길 앵커 — 이 문장에서 해소된 마지막 period + 문장 인덱스.
        # 거리 제한(CARRY_MAX_DISTANCE)이 없으면 18문장 떨어진 값까지 상속돼
        # "당시"가 엉뚱한 연도를 물려받는다(실측). 인덱스를 함께 저장해 상한을 건다.
        sent_idx[cand.article_id] = sent_idx.get(cand.article_id, 0) + 1
        for c in cl:
            if c.period:
                carry[cand.article_id] = (c.period, sent_idx[cand.article_id])
                # 기사 기준 시점 — 처음 확립된 표준 4형식 하나만 고정(덮어쓰지 않는다)
                if cand.article_id not in article_base and is_std_period(c.period):
                    article_base[cand.article_id] = c.period

        # claim_id 부여 — claim-kind item 슬롯이 번호를 소모(실패 item도 소모 → 재실행 안정)
        aid = cand.article_id
        ok_by_idx = {t["item_index"]: c for c, t in zip(cl, tr)}
        claim_slots = sorted(set(ok_by_idx) |
                             {e["item_index"] for e in er
                              if e["item_index"] is not None
                              and _get(e["item"] or {}, "kind") not in ("excluded",)})
        for slot in claim_slots:
            seq[aid] = seq.get(aid, 0) + 1
            if slot in ok_by_idx:
                ok_by_idx[slot].claim_id = f"{aid}-C{seq[aid]:03d}"
        for c, t in zip(cl, tr):
            t["claim_id"] = c.claim_id

        all_claims += cl
        all_excluded += ex
        all_errors += er
        all_traces += tr
        if er:
            error_sentences.add(cand.key)

    _backfill_duration(all_claims, all_traces, article_base)

    # 서킷브레이커(§5.6): 오류 문장 비율이 임계 초과 = 개별 문제가 아니라 계통 결함.
    # 기본 3%(전량 실행). dev 튜닝 런은 완주해 성적표를 내는 것이 목적이고 dev 8에
    # 최고난도 음성 기사가 의도적으로 포함돼 있어(정당한 거부가 오류로 계상) 상향 허용.
    if candidates and len(error_sentences) / len(candidates) > breaker_rate:
        # 진단 가시성: 중단하더라도 오류 목록은 남긴다 — 무엇이 계통 결함인지 봐야 고친다
        dump = Path(outdir) / "errors_breaker.jsonl"
        dump.parent.mkdir(parents=True, exist_ok=True)
        with open(dump, "w", encoding="utf-8") as f:
            import json as _json
            for e in all_errors:
                f.write(_json.dumps(e, ensure_ascii=False) + "\n")
        raise AccountingError(
            f"서킷브레이커 발동 — 오류 문장 {len(error_sentences)}/{len(candidates)}"
            f" ({len(error_sentences) / len(candidates):.1%}) > {breaker_rate:.0%}"
            f" : 프롬프트/파서 계통 결함으로 간주(§5.6). 오류 목록: {dump}")

    # Stage D — 표준명 사전 적용(v0.4: metric_normalized는 계약 필드 = 크리티컬 패스).
    # 기본값(None)이면 사전 파일에서 자동 로드 — 배선 누락으로 신규 계약 필드가
    # 전건 null인 파일이 무경고 산출되는 함정 차단(리뷰 high). 명시적 False로만 생략.
    # ※ 사전 파일이 **없어도** normalizer를 만든다 — 55차 정책상 기본 동작은
    #   "verbatim metric 복사"이고 그건 빈 사전으로도 성립한다. 파일 존재를 조건으로
    #   걸면 새로 클론한 환경(data/는 저장소 미포함)에서 Stage D가 통째로 건너뛰어져
    #   계약 필드 metric_normalized가 전건 null로 나간다(실측 경로).
    dictionary_version = None
    if normalizer is None:
        from src.p3_stage_d import DICTIONARY_DEFAULT, load_dictionary, MetricNormalizer
        normalizer = MetricNormalizer(load_dictionary())
        if Path(DICTIONARY_DEFAULT).exists():
            import hashlib
            dictionary_version = hashlib.sha1(Path(DICTIONARY_DEFAULT).read_bytes()).hexdigest()[:8]
    stage_d_stats = None
    if normalizer:
        stage_d_stats = normalizer.apply(all_claims, all_traces)

    summary = emit_all(outdir, candidates, all_claims, all_excluded, all_errors, all_traces)
    summary["dictionary_version"] = dictionary_version   # 재현 추적(리니지 4튜플)
    summary["stage_d"] = stage_d_stats or "skipped"
    return summary


def make_golden_stub_extractor(golden_path=None) -> Extractor:
    """골든 라벨을 그대로 돌려주는 stub Stage B — 무HCX E2E 스모크(§5.6 ③)용."""
    from src.p3_golden import load_golden, GOLDEN_DEFAULT

    gold = load_golden(golden_path or GOLDEN_DEFAULT)
    by_sentence: dict[tuple[str, str], list[dict]] = {}
    for c in gold.claims:
        by_sentence.setdefault((c.article_id, c.sent_id), []).append({
            "kind": "claim", "forecast": c.forecast, "metric": c.metric,
            "metric_normalized": c.metric_normalized, "value": c.value, "unit": c.unit,
            "value_type": c.value_type, "direction": c.direction, "period": c.period,
            "comparison_basis": c.comparison_basis, "note": c.note,
        })
    for e in gold.excluded:
        by_sentence.setdefault((e.article_id, e.sent_id), []).append({
            "kind": "excluded", "exclusion_code": e.exclusion_code, "note": e.note,
        })

    def extractor(cand: SentenceCandidate) -> list[dict]:
        return by_sentence.get(cand.key, [])

    return extractor
