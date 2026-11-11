"""Tool definitions for editing a picture by outcome: change the headline, the colours, the look, the logo spot, add a big number or a
chart, reshape for platforms, reset, or go back to an earlier version.

The picture editor is made of dozens of small moves (dragging, resizing, one layer at a time). A tool offers the finished outcome
instead, and a person who wants fine control opens the picture in the editor. Every tool carries the version the caller last saw
(`base_version`), so a save made on top of someone else's newer work is refused and not quietly written over it, and every change is
a new version that can be restored.
"""

from __future__ import annotations

import re
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.image_asset import MAX_CHART_BARS, LayoutPreset
from app.pipelines.media.logo_placement import BACKINGS, POSITIONS
from app.tools.contract import Mode, Risk, Scope, ToolSpec

MAX_RESHAPE_LAYOUTS = 6
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
_FREE = "Free: the picture is redrawn without an AI call."
_UNDO = "Every change is a new version; restore an earlier one with image.restore_version."

# the same choices the editor offers; the tests keep these equal to the editor's own lists
LogoSpot = Literal[POSITIONS]
LogoBacking = Literal[BACKINGS]


def _hex(value: Optional[str]) -> Optional[str]:
    if value is not None and not _HEX.match(value):
        raise ValueError("A colour is written as # and six letters or digits, such as #1f6feb.")
    return value


class PictureRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    image_asset_id: str = Field(min_length=1, max_length=64)
    slide_number: int = Field(1, ge=1, le=50)
    #: The version the caller last saw. A save on a newer version is refused with a message to look again.
    base_version: int = Field(ge=1)


class SetHeadlineIn(PictureRef):
    text: str = Field(min_length=1, max_length=200)
    accent_word: Optional[str] = Field(None, max_length=40)

    @field_validator("text")
    @classmethod
    def _text_has_words(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("The headline needs some words.")
        return v.strip()


class SetColorsIn(PictureRef):
    text_color: Optional[str] = None
    accent_color: Optional[str] = None
    band_color: Optional[str] = None

    @field_validator("text_color", "accent_color", "band_color")
    @classmethod
    def _colors_are_hex(cls, v: Optional[str]) -> Optional[str]:
        return _hex(v)

    @model_validator(mode="after")
    def _something_to_change(self):
        if not (self.text_color or self.accent_color or self.band_color):
            raise ValueError("Choose at least one colour to change.")
        return self


class ApplyLookIn(PictureRef):
    """A look left out stays as it is. Switching on remove_background costs 1 credit (the 'remove_background' price)."""

    glow: Optional[bool] = None
    dual_tone: Optional[bool] = None
    grid: Optional[bool] = None
    remove_background: Optional[bool] = None
    corner_radius: Optional[float] = Field(None, ge=0.0, le=0.25)

    @model_validator(mode="after")
    def _something_to_change(self):
        if all(getattr(self, f) is None for f in ("glow", "dual_tone", "grid", "remove_background", "corner_radius")):
            raise ValueError("Choose at least one look to change.")
        return self


class SetLogoSpotIn(PictureRef):
    position: LogoSpot
    backing: Optional[LogoBacking] = None


class AddBigNumberIn(PictureRef):
    figure: str = Field(min_length=1, max_length=12)
    caption: str = Field("", max_length=80)


class ChartBarIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(min_length=1, max_length=24)
    value: float = Field(ge=0)


class AddChartIn(PictureRef):
    bars: list[ChartBarIn] = Field(min_length=1, max_length=MAX_CHART_BARS)
    title: Optional[str] = Field(None, max_length=80)


class ReshapeIn(PictureRef):
    layouts: list[LayoutPreset] = Field(min_length=1, max_length=MAX_RESHAPE_LAYOUTS)
    #: Where the main subject is, as fractions of the picture. Left out, it is found from the picture.
    focus_x: Optional[float] = Field(None, ge=0.0, le=1.0)
    focus_y: Optional[float] = Field(None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _focus_is_a_pair(self):
        if (self.focus_x is None) != (self.focus_y is None):
            raise ValueError("Give both focus_x and focus_y, or neither.")
        return self


class ResetIn(PictureRef):
    pass


class RestoreVersionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    image_asset_id: str = Field(min_length=1, max_length=64)
    version_number: int = Field(ge=1)
    base_version: int = Field(ge=1)


class PictureOut(BaseModel):
    image_asset_id: str
    slide_number: int
    #: The picture's new version; send it as `base_version` on the next change.
    version: int
    #: Where to open this picture in the editor, for fine control by hand.
    href: str


def _declared(source: str) -> str:
    """The route as the app declares it: the path names here are short, the app's are the full parameter names."""
    return (source.replace("{id}", "{image_asset_id}").replace("{n}", "{slide_number}").replace("{version}", "{version_number}"))


def _edit(name: str, summary: str, input_model: type[BaseModel], source: str, *examples: str, note: str = _FREE,
          limits: tuple[str, ...] = ()) -> ToolSpec:
    return ToolSpec(
        name=name, summary=summary, risk=Risk.WRITE, scope=Scope.CREATE, mode=Mode.INSTANT, input_model=input_model,
        output_model=PictureOut, permission="create_content", cost_note=note, limit_keys=limits, undo=_UNDO,
        source=f"route:{source}", examples=examples, routes=(_declared(source),), coarse=True,
    )


def specs() -> list[ToolSpec]:
    layers = "/api/v1/image-assets/{id}/slides/{n}/layers"
    return [
        _edit("image.set_headline", "Change the headline words on a picture, and the one word to highlight.", SetHeadlineIn, f"PUT {layers}",
              "Change the headline to 'Clarity beats scale'"),
        _edit("image.set_colors", "Change a picture's text, highlight or text-band colours.", SetColorsIn, f"PUT {layers}",
              "Make the highlight colour orange"),
        _edit("image.apply_look", "Switch a picture's looks on or off: glow, two-tone, grid, rounded corners, remove background.",
              ApplyLookIn, "POST /api/v1/image-assets/{id}/slides/{n}/looks", "Give it a soft glow",
              note="Free, except switching remove background on costs 1 credit (the 'remove_background' price).", limits=("pictures_day",)),
        _edit("image.set_logo_spot", "Put the brand logo in one of five spots, with an optional light or dark plate behind it.",
              SetLogoSpotIn, f"PUT {layers}", "Move the logo to the bottom right"),
        _edit("image.add_big_number", "Add one large figure with a short line under it, such as '40%' and 'faster setup'.", AddBigNumberIn,
              f"PUT {layers}", "Add a big 40% to this picture"),
        _edit("image.add_chart", "Add a bar chart of up to six labelled values drawn in the brand colours.", AddChartIn, f"PUT {layers}",
              "Add a chart of sales by month"),
        _edit("image.reshape", "Make the same picture in other platform shapes (square, story, LinkedIn, X and more), up to six at once.",
              ReshapeIn, "POST /api/v1/image-assets/{id}/slides/{n}/reshape", "Make a story version of this picture"),
        _edit("image.reset", "Put a picture back to its starting design.", ResetIn, f"POST {layers}/reset",
              "Undo all my edits on this picture"),
        _edit("image.restore_version", "Bring back an earlier version of a picture as a new version.", RestoreVersionIn,
              "POST /api/v1/image-assets/{id}/restore/{version}", "Go back to version 2 of this picture"),
    ]
