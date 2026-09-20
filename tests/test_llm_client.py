from __future__ import annotations

import pytest

from harbour import llm_client as llm
from harbour.policy_engine import tag_untrusted


def test_classify_intent_refund():
    intent, actions = llm.classify_intent("I'd like a refund for the overcharge")
    assert intent == "refund"
    assert "issue_refund" in actions
    assert "verify_identity" in actions


def test_classify_intent_fraud():
    intent, actions = llm.classify_intent("someone stole my card and used it on this loan")
    assert intent == "fraud_or_freeze"
    assert "freeze_account" in actions
    assert "issue_refund" not in actions  # a freeze case cannot move money


def test_classify_intent_payoff():
    intent, actions = llm.classify_intent("what's my payoff amount")
    assert intent == "payoff_quote"
    assert "calculate_payoff" in actions
    assert "verify_identity" not in actions  # a quote does not need it


def test_classify_intent_unclear_is_minimal():
    intent, actions = llm.classify_intent("xyzzy plugh")
    assert intent == "unclear"
    assert actions == {"get_customer", "escalate_to_human", "send_customer_message"}


def test_universal_actions_always_present():
    for message in ["refund please", "I want to dispute a fee", "what's my balance"]:
        _, actions = llm.classify_intent(message)
        assert {"get_customer", "escalate_to_human", "send_customer_message"} <= actions


def test_extract_amount_cents():
    assert llm.extract_amount_cents("please refund $45.50") == 4550
    assert llm.extract_amount_cents("refund 100 dollars") == 10000
    assert llm.extract_amount_cents("no amount mentioned here") is None


def test_scripted_planner_refund_flow_ends_in_finish():
    client = llm.ScriptedPlannerClient()
    message = tag_untrusted("please refund $30 for the duplicate charge")
    _, approved = llm.classify_intent(str(message))
    history: list[dict] = []

    seen_actions = []
    for _ in range(10):
        resp = client.plan_step(case_id="C1", customer_message=message, approved_actions=approved, history=history)
        seen_actions.append(resp.action)
        if resp.action == "finish":
            break
        if resp.action == "verify_identity":
            history.append({"action": "verify_identity", "result": {"verified": True}, "outcome": "ok"})
        else:
            history.append({"action": resp.action, "result": {}, "outcome": "ok"})
    else:
        pytest.fail("scripted planner never reached finish")

    assert seen_actions[0] == "verify_identity"
    assert "issue_refund" in seen_actions
    assert seen_actions[-1] == "finish"
    # Never proposes issue_refund twice.
    assert seen_actions.count("issue_refund") == 1


def test_scripted_planner_escalates_when_verification_fails():
    client = llm.ScriptedPlannerClient()
    message = tag_untrusted("please refund $30")
    _, approved = llm.classify_intent(str(message))
    history = [{"action": "verify_identity", "result": {"verified": False}, "outcome": "ok"}]
    resp = client.plan_step(case_id="C1", customer_message=message, approved_actions=approved, history=history)
    assert resp.action == "escalate_to_human"


def test_scripted_planner_reports_pinned_model_id():
    client = llm.ScriptedPlannerClient()
    message = tag_untrusted("what's my balance")
    _, approved = llm.classify_intent(str(message))
    resp = client.plan_step(case_id="C1", customer_message=message, approved_actions=approved, history=[])
    assert resp.model_id == "harbour-planner-v1-scripted"
    assert resp.tokens_in > 0
    assert resp.tokens_out > 0


def test_pinned_model_client_accepts_approved_identity():
    wrapped = llm.PinnedModelClient(llm.ScriptedPlannerClient())
    message = tag_untrusted("what's my balance")
    _, approved = llm.classify_intent(str(message))
    resp = wrapped.plan_step(case_id="C1", customer_message=message, approved_actions=approved, history=[])
    assert resp.action in approved or resp.action == "finish"


def test_pinned_model_client_rejects_unapproved_identity():
    class RogueClient:
        def plan_step(self, **kwargs):
            return llm.LLMResponse(
                model_id="some-unapproved-snapshot", tokens_in=1, tokens_out=1, action="finish"
            )

    wrapped = llm.PinnedModelClient(RogueClient())
    with pytest.raises(llm.ModelIdentityRejected):
        wrapped.plan_step(case_id="C1", customer_message=tag_untrusted("hi"), approved_actions=frozenset(), history=[])
