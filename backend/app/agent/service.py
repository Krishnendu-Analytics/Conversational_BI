"""
Route claim-level insurance fraud questions to the standalone prediction API.

Normal BI questions continue through BIWorkflow unchanged.
"""
from __future__ import annotations

import re
from typing import Any

import httpx

from app.agent.types import AgentQuestion, AgentResult
from app.agent.workflow import BIWorkflow
from app.config import get_settings

_FRAUD_TERMS = re.compile(
    r"\b(fraud|fraudulent|fraudster|fraud risk|fraud alert|suspicious claim|"
    r"claim fraud|detect fraud|fraud detection)\b",
    re.IGNORECASE,
)
_CLAIM_WORD = re.compile(r"\bclaims?\b", re.IGNORECASE)
_KEY_TOKEN = re.compile(r"(?<!\w)[A-Za-z0-9][A-Za-z0-9_-]{0,199}(?!\w)")


def _extract_claim_keys(question: str) -> list[str]:
    """
    Extract claim identifiers from common natural-language forms.
    Only called after fraud intent is detected, to avoid stealing normal BI queries.
    """
    # Prefer identifiers following 'claim key(s)' or 'claim number(s)'.
    patterns = [
        r"\bclaim\s+keys?\b\s*(?:are|is|:|#|no\.?)?\s*(.+)",
        r"\bclaim\s+numbers?\b\s*(?:are|is|:|#|no\.?)?\s*(.+)",
        r"\bclaims?\b\s*(?:are|is|:|#|no\.?)?\s*(.+)",
    ]
    tail = None
    for pattern in patterns:
        match = re.search(pattern, question, re.IGNORECASE)
        if match:
            tail = match.group(1)
            break

    if tail is None:
        return []

    # Stop at explanatory clauses, while keeping comma/and-separated keys.
    tail = re.split(
        r"\b(?:fraudulent|fraud|not fraud|or not|is fraud|are fraud|"
        r"for fraud|whether|please|check|predict|prediction|risk|status|"
        r"and tell me|and explain|with probability)\b",
        tail,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    # Remove common natural-language filler that can occur before the IDs.
    tail = re.sub(r"^\s*(?:are|is|:|#|no\.?|numbered)\s*", "", tail, flags=re.I)

    # Split into likely identifier tokens and discard joiner words.
    tokens = _KEY_TOKEN.findall(tail)
    stop_words = {
        "the", "a", "an", "please", "check", "claim", "claims", "key", "keys",
        "number", "numbers", "whether", "is", "are", "and", "or", "for", "of",
        "fraud", "fraudulent", "not", "likely", "to", "be", "predict", "prediction",
        "risk", "status", "tell", "me", "show", "give", "result", "results",
    }
    keys: list[str] = []
    for token in tokens:
        if token.lower() in stop_words:
            continue
        if token not in keys:
            keys.append(token)
    return keys


def is_fraud_prediction_question(question: str) -> bool:
    """True for fraud-related questions that identify one or more claim IDs."""
    return bool(_FRAUD_TERMS.search(question) and _extract_claim_keys(question))


def _api_error_message(exc: Exception) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "The fraud prediction service timed out. Please try again."
    if isinstance(exc, httpx.ConnectError):
        return "The fraud prediction service is unavailable. Please check that it is running."
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        try:
            detail = exc.response.json().get("detail", exc.response.text)
        except Exception:  # noqa: BLE001
            detail = exc.response.text
        if status == 401:
            return "The fraud prediction service rejected its API key. Please check the service configuration."
        if status == 404:
            return f"The fraud prediction service could not find the claim: {detail}"
        return f"The fraud prediction service returned HTTP {status}: {detail}"
    return f"The fraud prediction request failed ({type(exc).__name__})."


def _format_single_result(claim_key: str, result: dict[str, Any]) -> str:
    prediction = result.get("prediction", "Prediction unavailable")
    probability = result.get("fraud_probability_percent")
    if probability is None and result.get("fraud_probability") is not None:
        probability = round(float(result["fraud_probability"]) * 100, 2)
    threshold = result.get("decision_threshold_percent")
    if threshold is None and result.get("decision_threshold") is not None:
        threshold = round(float(result["decision_threshold"]) * 100, 2)

    if result.get("status") == "not_found":
        return f"Claim **{claim_key}**: not found in the prediction service's bundled dataset."

    lines = [f"**Claim {claim_key} — {prediction}**"]
    if probability is not None:
        lines.append(f"- Fraud probability: **{probability}%**")
    if threshold is not None:
        lines.append(f"- Decision threshold: {threshold}%")
    return "\n".join(lines)


def _format_batch_results(
    claim_keys: list[str],
    results_by_key: dict[str, dict[str, Any]],
) -> str:
    """Format multiple predictions as one Markdown table, preserving request order."""
    rows = [
        "| Claim Identifier | Prediction | Fraud Probability | Decision Threshold |",
        "|---|---|---:|---:|",
    ]

    for claim_key in claim_keys:
        result = results_by_key.get(str(claim_key))
        if not isinstance(result, dict) or result.get("status") == "not_found":
            rows.append(f"| {claim_key} | Claim not found | — | — |")
            continue

        prediction = str(result.get("prediction", "Prediction unavailable"))
        prediction = prediction.replace("|", "\\|").replace("\r", " ").replace("\n", " ")

        probability = result.get("fraud_probability_percent")
        if probability is None and result.get("fraud_probability") is not None:
            probability = float(result["fraud_probability"]) * 100

        threshold = result.get("decision_threshold_percent")
        if threshold is None and result.get("decision_threshold") is not None:
            threshold = float(result["decision_threshold"]) * 100

        probability_text = f"{float(probability):.2f}%" if probability is not None else "—"
        threshold_text = f"{float(threshold):.2f}%" if threshold is not None else "—"
        rows.append(
            f"| {claim_key} | {prediction} | {probability_text} | {threshold_text} |"
        )

    return "## Fraud prediction results\n\n" + "\n".join(rows)


def _run_fraud_prediction(payload: AgentQuestion, claim_keys: list[str]) -> AgentResult:
    settings = get_settings()
    base_url = (getattr(settings, "fraud_prediction_api_url", "") or "").strip()
    api_key = (getattr(settings, "fraud_prediction_api_key", "") or "").strip()
    timeout = float(getattr(settings, "fraud_prediction_timeout_seconds", 15))

    if not base_url or not api_key:
        return AgentResult(
            answer="Fraud prediction is not configured. Set FRAUD_PREDICTION_API_URL and FRAUD_PREDICTION_API_KEY in the BI backend environment.",
            error="Fraud prediction configuration missing",
            intent="fraud_prediction",
            steps=["Detected claim-level fraud prediction request", "Checked fraud API configuration"],
            connection_id=str(payload.connection_id) if payload.connection_id else None,
        )

    headers = {"X-API-Key": api_key, "Accept": "application/json"}
    try:
        with httpx.Client(timeout=timeout) as client:
            if len(claim_keys) == 1:
                response = client.post(
                    base_url,
                    headers=headers,
                    json={"claim_key": claim_keys[0]},
                )
                response.raise_for_status()
                data = response.json()
                answer = _format_single_result(claim_keys[0], data)
            else:
                batch_url = re.sub(r"/predict/by-claim-key/?$", "/predict/by-claim-keys", base_url)
                if batch_url == base_url:
                    return AgentResult(
                        answer="The configured fraud API URL must end with /predict/by-claim-key so the chatbot can derive the batch endpoint.",
                        error="Fraud batch endpoint URL cannot be derived",
                        intent="fraud_prediction",
                        steps=["Detected multiple claim keys", "Validated fraud API endpoint configuration"],
                        connection_id=str(payload.connection_id) if payload.connection_id else None,
                    )
                response = client.post(
                    batch_url,
                    headers=headers,
                    json={"claim_keys": claim_keys},
                )
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict):
                    raise ValueError("Batch API response must be a JSON object.")
                results = data.get("results")
                if not isinstance(results, list):
                    raise ValueError("Batch API response did not contain a results list.")

                by_key: dict[str, dict[str, Any]] = {}
                for index, item in enumerate(results):
                    if not isinstance(item, dict):
                        continue
                    identifier = (
                        item.get("claim_identifier")
                        or item.get("claim_key")
                        or item.get("claim_number")
                    )
                    # Some batch services return results in request order without an
                    # identifier field; use the corresponding requested key as fallback.
                    if identifier is None and index < len(claim_keys):
                        identifier = claim_keys[index]
                    if identifier is not None:
                        by_key[str(identifier)] = item

                answer = _format_batch_results(claim_keys, by_key)
    except (httpx.HTTPError, ValueError) as exc:
        message = _api_error_message(exc) if isinstance(exc, httpx.HTTPError) else str(exc)
        return AgentResult(
            answer=message,
            error=message,
            intent="fraud_prediction",
            steps=["Detected claim-level fraud prediction request", "Called fraud prediction API", "Prediction request failed"],
            connection_id=str(payload.connection_id) if payload.connection_id else None,
        )

    answer += (
        "\n\n*This is a model-generated risk assessment, not proof that a claim is fraudulent.*"
    )
    return AgentResult(
        answer=answer,
        intent="fraud_prediction",
        steps=[
            "Detected claim-level fraud prediction request",
            f"Sent {len(claim_keys)} claim key(s) to the fraud prediction API",
            "Received prediction results from the API",
        ],
        connection_id=str(payload.connection_id) if payload.connection_id else None,
        answered_from={"database": False, "documents": []},
    )


def run_question(payload: AgentQuestion) -> AgentResult:
    # Route only fraud-related questions that identify one or more claim keys.
    # All other questions follow the original BI workflow unchanged.
    if is_fraud_prediction_question(payload.question):
        return _run_fraud_prediction(payload, _extract_claim_keys(payload.question))

    workflow = BIWorkflow()
    raw = workflow.run(
        payload.question,
        tenant_id=payload.tenant_id,
        user_id=payload.user_id,
        connection_id=payload.connection_id,
        conversation_id=payload.conversation_id,
        document_ids=payload.document_ids or None,
        source_scope=payload.source_scope,
    )
    return AgentResult(
        answer=str(raw.get("answer", "")),
        sql=raw.get("sql"),
        rows=list(raw.get("rows") or []),
        dashboard=raw.get("dashboard"),
        rag_chunks=list(raw.get("rag_chunks") or []),
        rag_context=raw.get("rag_context"),
        chart_suggestion=raw.get("chart_suggestion"),
        steps=list(raw.get("steps") or []),
        error=raw.get("error"),
        intent=raw.get("intent"),
        run_id=raw.get("run_id"),
        chat_grounding=raw.get("chat_grounding"),
        query_clarification=raw.get("query_clarification"),
        dataset_id=str(raw["dataset_id"]) if raw.get("dataset_id") else None,
        dataset_display_name=raw.get("dataset_display_name"),
        dataset_clarification=raw.get("dataset_clarification"),
        answered_from=raw.get("answered_from"),
        connection_id=raw.get("connection_id") or None,
        documents_added=list(raw.get("attached_documents") or []),
        source_scope=raw.get("source_scope"),
    )
