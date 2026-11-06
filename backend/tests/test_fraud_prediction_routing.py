from app.agent.service import _extract_claim_keys, is_fraud_prediction_question


def test_single_claim_key():
    q = "Is claim key 5725 fraudulent?"
    assert _extract_claim_keys(q) == ["5725"]
    assert is_fraud_prediction_question(q)


def test_multiple_claim_keys():
    q = "Check whether claims 5725, 77784, and 12345 are fraudulent."
    assert _extract_claim_keys(q) == ["5725", "77784", "12345"]
    assert is_fraud_prediction_question(q)


def test_claim_numbers():
    q = "Check fraud for claim numbers CLM-1001 and CLM-1002"
    assert _extract_claim_keys(q) == ["CLM-1001", "CLM-1002"]
    assert is_fraud_prediction_question(q)


def test_normal_bi_question_not_routed():
    q = "Show total sales by month"
    assert not is_fraud_prediction_question(q)
