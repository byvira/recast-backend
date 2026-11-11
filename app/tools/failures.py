"""The failure wording and cool-down live in `app.core.failures`, under the whole product; this keeps the old import path."""

from app.core.failures import *  # noqa: F401,F403
from app.core.failures import _AI_WORDS, _REFUSAL_RETRY  # noqa: F401
