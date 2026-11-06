from app.llm.client import LLMClient, completion_kwargs


def test_complete_returns_stub_when_api_key_is_missing(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.llm.client.get_settings",
        lambda: type(
            "Settings",
            (),
            {
                "llm_api_key": "",
                "llm_base_url": "https://api.openai.com/v1",
                "llm_model": "gpt-4o-mini",
            },
        )(),
    )

    client = LLMClient()

    result = client.complete("Generate SQL for revenue")

    assert result == "[llm stub] missing LLM_API_KEY (prompt length=24)"


def _patch_openai(monkeypatch, model: str):
    monkeypatch.setattr(
        "app.llm.client.get_settings",
        lambda: type(
            "Settings",
            (),
            {
                "llm_api_key": "test-key",
                "llm_base_url": "https://api.openai.com/v1",
                "llm_model": model,
            },
        )(),
    )

    class FakeMessage:
        content = "SELECT SUM(amount) FROM orders"

    class FakeChoice:
        message = FakeMessage()

    class FakeResponse:
        choices = [FakeChoice()]

    class FakeCompletions:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def create(self, **kwargs):
            self.calls.append(kwargs)
            return FakeResponse()

    class FakeChat:
        def __init__(self) -> None:
            self.completions = FakeCompletions()

    class FakeClient:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs
            self.chat = FakeChat()

    monkeypatch.setattr("app.llm.client.OpenAI", FakeClient)
    return FakeClient


def test_complete_calls_openai_and_returns_output(monkeypatch) -> None:
    _patch_openai(monkeypatch, "gpt-4o-mini")

    client = LLMClient()

    assert client.client.kwargs == {
        "api_key": "test-key",
        "base_url": "https://api.openai.com/v1",
    }

    result = client.complete("Generate SQL for revenue")

    assert result == "SELECT SUM(amount) FROM orders"
    assert client.client.chat.completions.calls == [
        {
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "Generate SQL for revenue"}],
            "max_tokens": 2048,
            "temperature": 0.7,
        }
    ]


def test_complete_uses_max_completion_tokens_for_gpt5(monkeypatch) -> None:
    _patch_openai(monkeypatch, "gpt-5.4-mini")

    client = LLMClient()
    result = client.complete("Generate SQL for revenue")

    assert result == "SELECT SUM(amount) FROM orders"
    assert client.client.chat.completions.calls == [
        {
            "model": "gpt-5.4-mini",
            "messages": [{"role": "user", "content": "Generate SQL for revenue"}],
            "max_completion_tokens": 2048,
        }
    ]


def test_completion_kwargs_strips_provider_prefix() -> None:
    payload = completion_kwargs("openai/gpt-5.4-mini", "hi")
    assert "max_completion_tokens" in payload
    assert "max_tokens" not in payload
    assert "temperature" not in payload
