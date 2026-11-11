"""Tool definitions for editing a video by outcome: trim a clip, reorder clips, add text, set the caption style, choose a transition,
add music, set a clip's look, change the canvas shape, add bleeps, mark stressed words, or go back to an earlier version.

The video editor saves one typed edit list (clips on tracks, captions, effects), checked by one validator. A tool is a small, named
change to that list, so the same checks apply whoever makes it. Every tool carries the version the caller last saw (`base_version`):
a save on top of newer work is refused and not written over. Times are given in seconds; the editor works in frames, and the tool
converts. Making the video (the render) is a separate, priced tool.
"""

from __future__ import annotations

import re
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.tools.contract import Mode, Risk, Scope, ToolSpec
from app.video import edl

_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
MAX_SECONDS = 36_000.0
_FREE = "Free: the edit is saved with no AI call. Making the video is priced separately (render)."
_UNDO = "Every save is a version; go back with video.restore_version."

# the same choices the editor offers; the tests keep these equal to the editor's own lists
Transition = Literal[edl.TRANSITIONS]
Animation = Literal[edl.ANIMATIONS]
Font = Literal[edl.FONT_KEYS]
DuckAmount = Literal[edl.DUCK_AMOUNTS]
CanvasSize = Literal[tuple(edl.CANVAS_SIZES)]


def _hex(value: Optional[str]) -> Optional[str]:
    if value is not None and not _HEX.match(value):
        raise ValueError("A colour is written as # and six letters or digits, such as #ffffff.")
    return value


def _clip_id(value: str) -> str:
    if not _ID.match(value):
        raise ValueError("An id is 1 to 64 letters, numbers, - or _.")
    return value


class VideoRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1, max_length=64)
    #: The version the caller last saw. A save on a newer version is refused with a message to look again.
    base_version: int = Field(ge=1)


class ClipRef(VideoRef):
    clip_id: str

    @field_validator("clip_id")
    @classmethod
    def _clip_id_is_valid(cls, v: str) -> str:
        return _clip_id(v)


class TrimClipIn(ClipRef):
    #: Where in the original file the clip starts, and how long it plays.
    src_in_s: float = Field(ge=0, le=MAX_SECONDS)
    duration_s: float = Field(gt=0, le=MAX_SECONDS)


class ReorderClipsIn(VideoRef):
    """The video clips in their new order, each named once. They are laid end to end, keeping each clip's length and transition."""

    clip_ids: list[str] = Field(min_length=1, max_length=edl.MAX_CLIPS)

    @field_validator("clip_ids")
    @classmethod
    def _each_once(cls, ids: list[str]) -> list[str]:
        for i in ids:
            _clip_id(i)
        if len(set(ids)) != len(ids):
            raise ValueError("Each clip can be named only once.")
        return ids


class AddTextIn(VideoRef):
    text: str = Field(min_length=1, max_length=edl.MAX_TEXT)
    start_s: float = Field(ge=0, le=MAX_SECONDS)
    duration_s: float = Field(gt=0, le=MAX_SECONDS)
    x: float = Field(0.5, ge=0, le=1)
    y: float = Field(0.5, ge=0, le=1)
    size: int = Field(64, ge=8, le=400)
    color: str = "#FFFFFF"
    bold: bool = True
    font: Optional[Font] = None
    anim_in: Animation = "none"
    anim_out: Animation = "none"

    @field_validator("text")
    @classmethod
    def _has_words(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("The text needs some words.")
        return v

    @field_validator("color")
    @classmethod
    def _colour(cls, v: str) -> str:
        return _hex(v) or v


class SetCaptionsStyleIn(VideoRef):
    """Anything left out stays as it is."""

    enabled: Optional[bool] = None
    font: Optional[Font] = None
    size: Optional[float] = Field(None, ge=0.5, le=2.0)
    color: Optional[str] = None
    accent: Optional[str] = None
    outline: Optional[float] = Field(None, ge=0, le=10)
    shadow: Optional[float] = Field(None, ge=0, le=8)
    box: Optional[bool] = None
    y: Optional[float] = Field(None, ge=0.02, le=0.98)
    per_line: Optional[int] = Field(None, ge=0, le=10)
    upper: Optional[bool] = None
    highlight: Optional[bool] = None
    emoji: Optional[Literal["remove", "show"]] = None

    @field_validator("color", "accent")
    @classmethod
    def _colours(cls, v: Optional[str]) -> Optional[str]:
        return _hex(v)

    @model_validator(mode="after")
    def _something_to_change(self):
        if all(getattr(self, name) is None for name in type(self).model_fields if name not in ("project_id", "base_version")):
            raise ValueError("Choose at least one caption setting to change.")
        return self


class SetTransitionIn(ClipRef):
    kind: Transition
    duration_s: float = Field(0.5, gt=0, le=5)


class AddMusicIn(VideoRef):
    media_id: str = Field(min_length=1, max_length=64)
    start_s: float = Field(0, ge=0, le=MAX_SECONDS)
    #: Left out, the music plays to the end of the source file or the video.
    duration_s: Optional[float] = Field(None, gt=0, le=MAX_SECONDS)
    gain_db: float = Field(-12, ge=-60, le=24)
    #: Music dips under speech by this much.
    duck: bool = True
    duck_amount: DuckAmount = "medium"


class SetLookIn(ClipRef):
    brightness: Optional[float] = Field(None, ge=-1.0, le=1.0)
    contrast: Optional[float] = Field(None, ge=0.0, le=2.0)
    saturation: Optional[float] = Field(None, ge=0.0, le=3.0)

    @model_validator(mode="after")
    def _something_to_change(self):
        if self.brightness is None and self.contrast is None and self.saturation is None:
            raise ValueError("Choose brightness, contrast or saturation to change.")
        return self


class SetCanvasIn(VideoRef):
    size: CanvasSize


class Stretch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=1)

    @model_validator(mode="after")
    def _start_first(self):
        if self.end_ms <= self.start_ms:
            raise ValueError("A stretch needs its start before its end.")
        return self


class AddBleepsIn(VideoRef):
    stretches: list[Stretch] = Field(min_length=1, max_length=edl.MAX_BLEEPS)


class SetEmphasisIn(VideoRef):
    """Replaces which stretches of speech are stressed in the captions."""

    stretches: list[Stretch] = Field(max_length=edl.MAX_EMPHASIS)


class RestoreVersionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1, max_length=64)
    version: int = Field(ge=1)
    base_version: int = Field(ge=1)


class VideoOut(BaseModel):
    project_id: str
    #: The project's new version; send it as `base_version` on the next change.
    version: int
    #: Where to open this project in the editor, for fine control by hand.
    href: str


def _edit(name: str, summary: str, input_model: type[BaseModel], source: str, *examples: str) -> ToolSpec:
    return ToolSpec(
        name=name, summary=summary, risk=Risk.WRITE, scope=Scope.CREATE, mode=Mode.INSTANT, input_model=input_model,
        output_model=VideoOut, permission="edit_content", cost_note=_FREE, undo=_UNDO, source=f"route:{source}", examples=examples,
        routes=(source.replace("{id}", "{project_id}"),), coarse=True,
    )


def specs() -> list[ToolSpec]:
    save = "PUT /api/v1/video-projects/{id}"
    return [
        _edit("video.trim_clip", "Set where a clip starts in its file and how long it plays.", TrimClipIn, save, "Trim the intro to 5 seconds"),
        _edit("video.reorder_clips", "Put the video clips in a new order, laid end to end.", ReorderClipsIn, save, "Move the last clip to the start"),
        _edit("video.add_text", "Add a text overlay with its timing, position and style.", AddTextIn, save, "Add the title 'Launch day' for 3 seconds"),
        _edit("video.set_captions_style", "Change how captions look: font, size, colours, outline, position, case, highlight.",
              SetCaptionsStyleIn, save, "Make the captions bigger and yellow"),
        _edit("video.set_transition", "Choose how a clip arrives: dissolve, slide or wipe, and how long it takes.", SetTransitionIn, save,
              "Add a dissolve before the second clip"),
        _edit("video.add_music", "Add a music track from the library, dipping under speech.", AddMusicIn, save, "Put soft music under the video"),
        _edit("video.set_look", "Change a clip's brightness, contrast or saturation.", SetLookIn, save, "Make the first clip warmer and brighter"),
        _edit("video.set_canvas", "Change the video's shape: square, vertical, landscape or portrait, in standard or 4K sizes.",
              SetCanvasIn, save, "Make it vertical for Reels"),
        _edit("video.add_bleeps", "Beep out chosen stretches of speech.", AddBleepsIn, save, "Bleep out 12 to 13 seconds"),
        _edit("video.set_emphasis", "Mark which stretches of speech are stressed in the captions.", SetEmphasisIn, save,
              "Highlight the key phrase at 20 seconds"),
        _edit("video.restore_version", "Bring back an earlier saved version of the project.", RestoreVersionIn,
              "POST /api/v1/video-projects/{id}/versions/{version}/restore", "Go back to the version from this morning"),
    ]
