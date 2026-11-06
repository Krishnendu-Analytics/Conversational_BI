"""Call the LLM. Change base_url later if you add a model-runner container."""

from openai import OpenAI

from app.config import get_settings
from app.llm.usage import record_llm_usage, usage_tokens

_MAX_OUTPUT_TOKENS = 2048
_DEFAULT_TEMPERATURE = 0.7


def _model_uses_max_completion_tokens(model: str) -> bool:
    name = (model or "").lower()
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
    return name.startswith(("gpt-5", "o1", "o3", "o4"))


def completion_kwargs(
    model: str, prompt: str, max_output_tokens: int | None = None
) -> dict:
    payload: dict = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
    }
    limit = max_output_tokens or _MAX_OUTPUT_TOKENS
    if _model_uses_max_completion_tokens(model):
        payload["max_completion_tokens"] = limit
    else:
        payload["max_tokens"] = limit
        payload["temperature"] = _DEFAULT_TEMPERATURE
    return payload


class LLMClient:
    def __init__(self, model: str | None = None) -> None:
        settings = get_settings()
        self.api_key = settings.llm_api_key
        self.base_url = settings.llm_base_url
        self.model = model or settings.llm_model

        self.client = (
            OpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
            )
            if self.api_key
            else None
        )

    def complete(self, prompt: str, max_output_tokens: int | None = None) -> str:
        """Generate text using the configured LLM.

        ``max_output_tokens`` raises the default cap for long structured
        outputs (reasoning models spend part of it before answering).
        """
        if self.client is None:
            return f"[llm stub] missing LLM_API_KEY (prompt length={len(prompt)})"

        response = self.client.chat.completions.create(
            **completion_kwargs(self.model, prompt, max_output_tokens)
        )
        self._record_usage(response)
        content = response.choices[0].message.content
        return content or ""

    def _record_usage(self, response) -> None:
        prompt_tokens, completion_tokens = usage_tokens(response)
        record_llm_usage(
            getattr(response, "model", None) or self.model,
            prompt_tokens,
            completion_tokens,
        )

    def chat(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        max_output_tokens: int | None = None,
    ):
        """One chat turn with optional tool calling; returns the assistant
        message (``.content``, ``.tool_calls``). Raises without an API key —
        callers fall back to a non-agentic path."""
        if self.client is None:
            raise RuntimeError("Tool calling needs LLM_API_KEY")
        payload = completion_kwargs(self.model, "", max_output_tokens)
        payload["messages"] = messages
        if tools:
            payload["tools"] = tools
        response = self.client.chat.completions.create(**payload)
        self._record_usage(response)
        return response.choices[0].message

    def read_image(
        self,
        image: bytes,
        mime_type: str,
        prompt: str,
        max_output_tokens: int | None = None,
    ) -> str:
        """Ask a vision-capable model about one image (e.g. transcribe it).

        Raises ``RuntimeError`` without an API key: callers must not store a
        stub in place of the image's real content."""
        if self.client is None:
            raise RuntimeError("Reading images needs LLM_API_KEY")
        import base64

        payload = completion_kwargs(self.model, prompt, max_output_tokens)
        data_url = f"data:{mime_type};base64,{base64.b64encode(image).decode('ascii')}"
        payload["messages"] = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ]
        # Transcription should be faithful, not creative.
        if "temperature" in payload:
            payload["temperature"] = 0
        response = self.client.chat.completions.create(**payload)
        self._record_usage(response)
        return response.choices[0].message.content or ""


def get_llm() -> LLMClient:
    return LLMClient()


def fast_llm(llm: LLMClient | None = None) -> LLMClient:
    """The cheaper tier (LLM_FAST_MODEL) for easy jobs: routing, narrating
    already-computed facts, chat memory. SQL/plan writing, repair, dashboard
    planning and the supervisor stay on LLM_MODEL.

    Takes the caller's client so a test's fake passes straight through; with
    no fast model configured this is the same client."""
    base = llm if llm is not None else get_llm()
    fast = getattr(get_settings(), "llm_fast_model", "") or ""
    if not isinstance(base, LLMClient) or not fast or fast == base.model:
        return base
    return LLMClient(model=fast)
