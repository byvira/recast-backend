"""The plain wording for every kind of model failure, in one place so it can be improved without touching
logic. Titles and causes avoid jargon and never show an HTTP code; codes live in the technical details.
Sentence case, no first person."""
from __future__ import annotations

from dataclasses import dataclass

CRITICAL, HIGH, MEDIUM, LOW = "critical", "high", "medium", "low"


@dataclass(frozen=True)
class Kind:
    title: str          # {provider} {model} {feature} are filled in
    cause: str
    action_staff: str   # what a non developer can do
    action_dev: str
    priority: str


KINDS: dict[str, Kind] = {
    "rate_limit_minute": Kind("{provider} is busy right now", "Too many requests or too much text was sent in one minute.", "Nothing needed if it clears by itself.", "Pace or queue calls, or lower concurrency.", LOW),
    "quota_daily": Kind("{provider} ran out of free requests for today", "The daily free limit was used up.", "Wait for the reset, or rely on the fallback.", "Pace calls, move heavy features to another model, or consider a paid plan.", MEDIUM),
    "quota_tokens": Kind("{provider} text allowance reached", "Requests were too large or too many for the text allowance.", "Wait a minute, or for the daily reset.", "Shorten prompts or outputs, split large inputs, or lower concurrency.", MEDIUM),
    "quota_exhausted": Kind("{provider} has used up its allowance", "The plan's allowance (credits, characters or pictures) is used up.", "Wait for the plan to renew, or rely on the fallback.", "Check the provider's dashboard. Reduce use, or move to a paid plan.", MEDIUM),
    "billing_or_access": Kind("{provider} access was refused", "The account, plan or project does not have permission.", "Ask an admin to check the account and plan with the provider.", "Check the provider console for the project or billing status.", HIGH),
    "auth_invalid_key": Kind("{provider} rejected our key", "The API key is missing, wrong or revoked.", "Ask an admin to replace the key in the server settings.", "Replace the key in the environment settings and redeploy.", CRITICAL),
    "model_unavailable": Kind("The model {model} is not available", "The model name changed or was retired.", "Tell the dev team.", "Update the model name in app/shared/llm.py.", HIGH),
    "timeout": Kind("{provider} took too long", "No answer arrived in time.", "Usually temporary. Try again.", "If it repeats, check the timeout and the size of the prompt.", LOW),
    "provider_outage": Kind("{provider} is having problems", "The provider returned server errors.", "Wait. The fallback should cover it.", "Check the provider status page. Confirm the fallback is working.", MEDIUM),
    "network_error": Kind("Couldn't reach {provider}", "The connection failed before a reply.", "Usually temporary.", "If it repeats, check the hosting network.", MEDIUM),
    "context_too_long": Kind("Input was too long for {model}", "The request was larger than the model can read.", "Tell the dev team which feature it was.", "Shorten or split the input for {feature}.", MEDIUM),
    "bad_request": Kind("{provider} rejected the request format", "The request did not match what the provider accepts.", "Tell the dev team.", "Check the request builder for {feature}.", HIGH),
    "content_blocked": Kind("{provider} blocked a response", "The safety filter stopped the reply.", "Review the case. It may be expected.", "Check the prompt if this is common.", LOW),
    "empty_response": Kind("{provider} returned an empty answer", "The reply had no content.", "Try again.", "Check the prompt and settings if it repeats.", LOW),
    "unparseable_response": Kind("A reply couldn't be read", "The answer did not follow the required format.", "Tell the dev team.", "Check the format the prompt asks for in {prompt_path}.", MEDIUM),
    "fallback_failed": Kind("Generation is failing for {feature}", "Every provider that was tried failed.", "Urgent: people are affected. Check both providers.", "Check both providers and the keys.", CRITICAL),
    "recorder_error": Kind("The health log has a problem", "Some records could not be saved.", "Tell the dev team.", "Check database space and the connection.", MEDIUM),
    "unknown": Kind("Unexpected {provider} error", "An error we do not recognise.", "Tell the dev team.", "See the technical details.", MEDIUM),
}


# What to think about beyond the immediate fix, for the kinds where there is one.
LONG_TERM: dict[str, str] = {
    "quota_exhausted": "If this keeps happening, plan for a paid plan or a second provider before the allowance runs out.",
    "quota_daily": "If this keeps happening, spread heavy features across providers, pace background work, or move to a paid plan.",
    "quota_tokens": "If this keeps happening, shorten the prompts and outputs of the features that use the most text.",
    "rate_limit_minute": "If this keeps happening, queue or pace bursts of calls so they do not arrive together.",
    "provider_outage": "If this keeps happening, make sure a second provider can cover every core feature.",
    "fallback_failed": "Make sure a second provider with its own key and allowance covers every core feature.",
    "model_unavailable": "Check the provider's model list before each release so a retired model is caught early.",
    "auth_invalid_key": "Keep a spare key ready, and rotate keys on a schedule.",
}


def kind_for(error_type: str) -> Kind:
    return KINDS.get(error_type, KINDS["unknown"])


def render(text: str, *, provider: str = "The provider", model: str = "the model", feature: str = "this feature", prompt_path: str | None = None) -> str:
    """Fills the placeholders. The provider name is capitalised ("groq" becomes "Groq")."""
    return (
        text.replace("{provider}", provider.capitalize() if provider and provider.islower() else provider)
        .replace("{model}", model)
        .replace("{feature}", feature)
        .replace("{prompt_path}", prompt_path or "the prompt")
    )
