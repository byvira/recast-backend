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
from jinja2.sandbox import SandboxedEnvironment

from app.prompts.fence import fence as _fence

_PROMPTS_DIR = Path(__file__).resolve().parent


class _PublishedLoader(FileSystemLoader):
    """Reads a prompt file and puts in the text Ops has published for its editable sections, when a version applies
    (app/prompt_library/live.py). The template is rebuilt when the file or the applied version changes."""

    def get_source(self, environment, template):
        source, filename, uptodate = super().get_source(environment, template)
        name = template[:-6] if template.endswith(".jinja") else template
        try:
            from app.prompt_library import live

            applied = live.compose_live(name, source)
        except Exception:  # noqa: BLE001 - the file is always a safe answer
            applied = None
        if applied is None:
            return source, filename, lambda: uptodate() and _applied_now(name) is None
        text, key = applied
        return text, filename, lambda: uptodate() and _applied_now(name) == key


def _applied_now(name: str) -> str | None:
    try:
        from app.prompt_library import live

        return live.live_key(name)
    except Exception:  # noqa: BLE001
        return None


def _environment(loader, sandboxed: bool = False) -> Environment:
    env = (SandboxedEnvironment if sandboxed else Environment)(
        loader=loader,
        undefined=StrictUndefined,
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    # `{{ pasted_text | untrusted("tag") }}` puts outside text in a labelled block it cannot break out of.
    env.filters["untrusted"] = _fence
    return env


# The prompts as the product uses them (published Ops text applied), and the files exactly as written in code.
# Published text is written by Ops staff, not code, so it is rendered in Jinja's sandbox: it can fill variables but never
# reach Python objects.
_env = _environment(_PublishedLoader(str(_PROMPTS_DIR)), sandboxed=True)
_file_env = _environment(FileSystemLoader(str(_PROMPTS_DIR)))


def _render(name: str, variables: dict) -> str:
    """Renders with the published text when a version applies. If that fails for any reason the file is used, so an Ops
    edit can never break a feature; an error the file itself raises (a missing variable) still reaches the caller."""
    if _applied_now(name) is None:
        return _file_env.get_template(f"{name}.jinja").render(**variables)
    try:
        return _env.get_template(f"{name}.jinja").render(**variables)
    except Exception as exc:  # noqa: BLE001
        try:
            from app.prompt_library import live

            live.record_fallback(name, exc)
        except Exception:  # noqa: BLE001
            pass
        return _file_env.get_template(f"{name}.jinja").render(**variables)


def template_version(name: str) -> str:
    """A version label for a prompt that changes whenever its text changes: the name and a short fingerprint of the file. Logs
    that carry it show exactly which wording produced a result, with nothing to remember to bump."""
    import hashlib

    path = _PROMPTS_DIR / f"{name}.jinja"
    try:
        label = f"{name}@{hashlib.sha1(path.read_bytes()).hexdigest()[:8]}"
    except OSError:
        return f"{name}@unknown"
    applied = _applied_now(name)
    return f"{label}+{applied}" if applied else label


def text_version(name: str, text: str) -> str:
    """The same kind of label for a prompt written inside code."""
    import hashlib

    return f"{name}@{hashlib.sha1(text.encode('utf-8')).hexdigest()[:8]}"


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
    rendered = _render(name, variables)
    # The model call that follows this render is for this prompt: the health log records it with the call.
    try:
        from app.shared.llm_health.context import set_prompt_path

        set_prompt_path(name)
    except Exception:  # noqa: BLE001 - never let logging context break a render
        pass
    return rendered


def render_fragment(name: str, /, **variables) -> str:
    """Render a template that is a part of another prompt (a system line, a suffix, a rule block), on every use.

    The same rendering as `load_prompt`, but it leaves the prompt name the health log records alone, so a call is still logged
    under its main prompt rather than under the last fragment added to it."""
    return _render(name, variables)


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
