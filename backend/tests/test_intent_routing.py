"""Intent routing: the model's label, with dashboard and dataset state."""

from uuid import uuid4

import app.agent.workflow as workflow_mod
from app.agent.analysis_state import AnalysisState, DashboardFocus


def test_edit_dashboard_intent_when_dashboard_active(monkeypatch) -> None:
    classifier = workflow_mod.IntentClassifier()
    dash_id = uuid4()
    state = {
        "question": "remove the revenue kpi",
        "conversation_id": str(uuid4()),
        "tenant_id": str(uuid4()),
        "connection_id": str(uuid4()),
        "analysis_state": AnalysisState(
            dashboard=DashboardFocus(dashboard_id=dash_id),
        ).model_dump(),
    }

    class FakeLLM:
        def complete(self, prompt: str) -> str:
            return "edit_dashboard"

    monkeypatch.setattr(workflow_mod, "get_llm", lambda: FakeLLM())
    monkeypatch.setattr(
        workflow_mod,
        "_resolve_schema",
        lambda *a, **k: {"db_type": "postgres", "tables": []},
    )

    out = classifier.run(state)
    assert out["intent"] == "edit_dashboard"


def test_dataset_choice_resumes_dashboard_intent(monkeypatch) -> None:
    classifier = workflow_mod.IntentClassifier()
    ds1 = uuid4()
    ds2 = uuid4()
    state = {
        "question": "1 Agent Wise",
        "conversation_id": str(uuid4()),
        "tenant_id": str(uuid4()),
        "connection_id": str(uuid4()),
        "analysis_state": AnalysisState(
            last_user_question="Create a full insight dashboard",
            last_assistant_answer="Which dataset should I use?",
            turn_count=1,
            last_intent="dashboard",
            pending_dataset_clarification={
                "kind": "pick_dataset",
                "message": "Which dataset?",
                "original_question": "Create a full insight dashboard",
                "choices": [
                    {"id": "1", "label": "Agent Wise", "dataset_id": str(ds1)},
                    {"id": "2", "label": "Available Filters", "dataset_id": str(ds2)},
                ],
            },
        ).model_dump(),
    }

    class FakeLLM:
        def complete(self, prompt: str) -> str:
            return "sql"

    monkeypatch.setattr(workflow_mod, "get_llm", lambda: FakeLLM())
    monkeypatch.setattr(
        workflow_mod,
        "_resolve_schema",
        lambda *a, **k: {"db_type": "excel", "tables": []},
    )

    out = classifier.run(state)
    assert out["intent"] == "dashboard"
    assert state["question"] == "Create a full insight dashboard"
