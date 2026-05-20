"""Shared multi-provider LLM client — Groq (default), Gemini, Anthropic."""

from enum import Enum
from typing import Any

from anthropic import AsyncAnthropic
from groq import AsyncGroq

from app.core.config import settings


class LLMProvider(str, Enum):
    """Supported LLM provider identifiers."""

    GROQ = "groq"
    GEMINI = "gemini"
    ANTHROPIC = "anthropic"


_groq_client: AsyncGroq | None = None
_anthropic_client: AsyncAnthropic | None = None


def get_llm_client() -> Any:
    """Return the singleton client for the active LLM provider.

    Reads ``LLM_PROVIDER`` from settings and lazily initialises the matching
    client on first call.  Gemini returns ``None`` until the
    ``google-generativeai`` package is added to requirements.

    Returns:
        AsyncGroq, AsyncAnthropic, or None (Gemini placeholder).
    """
    global _groq_client, _anthropic_client

    provider = settings.LLM_PROVIDER

    if provider == LLMProvider.ANTHROPIC:
        if _anthropic_client is None:
            _anthropic_client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
        return _anthropic_client

    if provider == LLMProvider.GEMINI:
        # Placeholder: initialise google.generativeai client once installed
        return None

    # Default: groq
    if _groq_client is None:
        _groq_client = AsyncGroq(api_key=settings.GROQ_API_KEY)
    return _groq_client


async def call_llm(prompt: str, system: str = "") -> str:
    """Route *prompt* to the active LLM provider and return the text response.

    Reads ``LLM_PROVIDER`` from settings at call time so the provider can be
    changed without restarting the process.

    Args:
        prompt: The user message to send to the model.
        system: Optional system prompt to guide model behaviour.

    Returns:
        Text content of the model's response, or empty string (placeholder).
    """
    provider = settings.LLM_PROVIDER

    if provider == LLMProvider.ANTHROPIC:
        return await _call_anthropic(prompt, system)
    if provider == LLMProvider.GEMINI:
        return await _call_gemini(prompt, system)
    return await _call_groq(prompt, system)


async def _call_groq(prompt: str, system: str = "") -> str:
    """Send *prompt* to the Groq API and return the completion text.

    Uses the singleton AsyncGroq client from :func:`get_llm_client` to call
    ``chat.completions.create`` with the configured model.

    Args:
        prompt: User message content.
        system: System prompt; defaults to empty string.

    Returns:
        Completion text from the Groq model (placeholder: empty string).
    """
    # Placeholder: client.chat.completions.create(messages=[...], model="...")
    return ""


async def _call_gemini(prompt: str, system: str = "") -> str:
    """Send *prompt* to the Google Gemini API and return the response text.

    Will use the ``google-generativeai`` client once the package is added to
    requirements.txt and ``GEMINI_API_KEY`` is configured.

    Args:
        prompt: User message content.
        system: System instruction passed to the model.

    Returns:
        Response text from Gemini (placeholder: empty string).
    """
    # Placeholder: genai.GenerativeModel(...).generate_content_async(...)
    return ""


async def _call_anthropic(prompt: str, system: str = "") -> str:
    """Send *prompt* to the Anthropic API and return the completion text.

    Uses the singleton AsyncAnthropic client from :func:`get_llm_client` to
    call ``messages.create`` with the configured Claude model.

    Args:
        prompt: User message content.
        system: System prompt passed as the ``system`` parameter.

    Returns:
        Text of the first content block (placeholder: empty string).
    """
    # Placeholder: client.messages.create(model="...", system=system, messages=[...])
    return ""
