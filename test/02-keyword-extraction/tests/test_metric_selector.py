# 원문·정규화 metric 중 하나만 안전하게 선택하는지 검증합니다.
import pytest

from src import metric_selector


def _response(content):
    return {"result": {"message": {"content": content}}}


@pytest.mark.parametrize(
    ("metric", "normalized", "selected_index", "expected"),
    [
        ("수출", "수출액", "[1]", "수출액"),
        ("미국 수출", "대미 수출액", "[1]", "대미 수출액"),
        ("무역수지 흑자", "무역수지", "[0]", "무역수지 흑자"),
    ],
)
def test_select_metric_uses_hcx_selected_candidate(
    metric, normalized, selected_index, expected
):
    calls = []

    def fake_call_hcx(model_name, messages, **kwargs):
        calls.append((model_name, messages, kwargs))
        return _response(selected_index)

    selected = metric_selector.select_metric(
        metric,
        normalized,
        claim="통계 claim 문장",
        hcx_caller=fake_call_hcx,
    )

    assert selected == expected
    assert len(calls) == 1
    assert calls[0][0] == "HCX-007"
    assert calls[0][2]["thinking_effort"] == "none"


def test_select_metric_uses_only_metric_without_hcx_call():
    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("HCX should not be called")

    assert metric_selector.select_metric(
        "수출",
        None,
        hcx_caller=fail_if_called,
    ) == "수출"


def test_select_metric_uses_only_normalized_without_hcx_call():
    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("HCX should not be called")

    assert metric_selector.select_metric(
        " ",
        "수출액",
        hcx_caller=fail_if_called,
    ) == "수출액"


def test_select_metric_falls_back_to_normalized_when_hcx_fails():
    def failing_call(*_args, **_kwargs):
        raise RuntimeError("API failure")

    assert metric_selector.select_metric(
        "수출",
        "수출액",
        hcx_caller=failing_call,
    ) == "수출액"


def test_select_metric_falls_back_when_response_selects_no_input_candidate():
    def invalid_call(*_args, **_kwargs):
        return _response("[2]")

    assert metric_selector.select_metric(
        "수출",
        "수출액",
        hcx_caller=invalid_call,
    ) == "수출액"


def test_select_metric_rejects_when_both_candidates_are_empty():
    with pytest.raises(
        metric_selector.InvalidMetricSelectionInputError,
        match="선택할 metric이 없습니다",
    ):
        metric_selector.select_metric(None, "  ")


def test_metric_selector_prompt_requires_one_input_index_and_preserves_meaning():
    messages = metric_selector.build_metric_selector_messages(
        "무역수지 흑자",
        "무역수지",
        "지난해 무역수지는 흑자를 기록했다.",
    )
    prompt = "\n".join(message["content"] for message in messages)

    assert "입력된 두 후보 중 하나" in prompt
    assert "index" in prompt
    assert "원문 의미" in prompt
    assert "대상" in prompt
    assert "방향성" in prompt
    assert "측정 가능한 통계지표" in prompt
    assert "0: 무역수지 흑자" in prompt
    assert "1: 무역수지" in prompt
