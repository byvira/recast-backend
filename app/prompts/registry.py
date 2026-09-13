"""Centralized loader for every LLM prompt template in app/prompts/.

Templates are plain Jinja2 files (autoescape is OFF — this renders text sent
to an LLM, not HTML). `StrictUndefined` is used deliberately: a template
referencing a variable the caller forgot to pass must raise immediately,
not silently render blank. A blank-rendered prompt fragment is exactly the
kind of bug that's invisible in a diff and only shows up as degraded model
output much later.
"""

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

_PROMPTS_DIR = Path(__file__).resolve().parent

_env = Environment(
    loader=FileSystemLoader(str(_PROMPTS_DIR)),
    undefined=StrictUndefined,
    autoescape=False,
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=True,
)


def load_prompt(name: str, /, **variables) -> str:
    """Render the template at app/prompts/{name}.jinja with *variables*.

    `name` is positional-only so a template needing a variable literally
    called `name` (there are several — e.g. brand name) doesn't collide
    with this function's own path argument.

    Args:
        name: Slash-separated path relative to app/prompts/, without the
            .jinja extension — e.g. "text/generate/master".
        **variables: Values the template's placeholders are rendered with.

    Returns:
        The rendered prompt text.

    Raises:
        jinja2.exceptions.UndefinedError: The template references a
            variable that wasn't passed in `variables`.
        jinja2.exceptions.TemplateNotFound: No template exists at that path.
    """
    template = _env.get_template(f"{name}.jinja")
    return template.render(**variables)


@lru_cache(maxsize=None)
def load_fixture(name: str) -> Any:
    """Load JSON fixture data (few-shot examples, example outputs) from app/prompts/fixtures/.

    Kept separate from load_prompt because fixtures are data passed *into* a
    template's variables, not templates themselves.

    Args:
        name: Filename under app/prompts/fixtures/, without the .json extension.

    Returns:
        The parsed JSON value (typically a list of dicts).
    """
    path = _PROMPTS_DIR / "fixtures" / f"{name}.json"
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


@lru_cache(maxsize=None)
def load_localized(name: str) -> dict[str, str]:
    """Load English source templates for get_localized_string() from
    app/prompts/localized/.

    These are the "translate this template, cache the result" source strings
    for a persona's fixed-voice messages (Odette's flag templates, Remy's
    signal/align templates, analytics report labels) — distinct from a
    one-off LLM prompt, which is why they live in their own directory and
    format (YAML, not Jinja) rather than going through load_prompt().

    Args:
        name: Filename under app/prompts/localized/, without the .yaml extension.

    Returns:
        Dict mapping template key to its English source template string.
    """
    path = _PROMPTS_DIR / "localized" / f"{name}.yaml"
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)
