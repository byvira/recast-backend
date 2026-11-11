"""The typed tool contract and registry: the rules every tool must follow, and coverage of the existing job actions."""

import json

import pytest
from pydantic import BaseModel, ValidationError

from app.billing import plans as plan_table
from app.billing.costs import DEFAULT_COSTS
from app.core.rbac import ROLE_PERMISSIONS
from app.shared import jobs
from app.tools import actions, changes, compose, catalog_account, catalog_audio, catalog_campaigns, catalog_images, catalog_library, catalog_video, catalog_publish, catalog_text, pictures, reads, videos

_MODULES = (reads, pictures, videos, actions, catalog_library, catalog_text, catalog_publish, catalog_audio, catalog_images, catalog_campaigns, catalog_video, catalog_account, changes, compose)
from app.tools.contract import Mode, Risk, Scope, ToolError, ToolSpec
from app.tools.registry import ToolRegistry, get_registry


class _In(BaseModel):
    pass


class _Confirmed(BaseModel):
    confirm: bool


def _spec(**over):
    base = dict(name="demo.tool", summary="Do a thing.", risk=Risk.WRITE, scope=Scope.CREATE, mode=Mode.RUN, input_model=_In,
                cost_note="Free.")
    base.update(over)
    return ToolSpec(**base)


def test_the_registry_holds_every_job_action_and_the_read_tools():
    registry = get_registry()
    names = set(registry.names())
    if not jobs.ACTIONS:
        from app.shared.job_actions import register_all

        register_all()
    # a job action with no tool definition fails here, so a new action cannot be added without deciding how it is offered
    assert set(jobs.ACTIONS) <= names, f"actions with no tool: {sorted(set(jobs.ACTIONS) - names)}"
    assert {t.name for t in reads.specs()} <= names
    assert {t.name for t in pictures.specs()} <= names
    assert len(registry) == sum(len(m.specs()) for m in _MODULES)


def test_every_tool_follows_the_contract_and_has_json_schemas():
    for tool in get_registry().all():
        assert json.dumps(tool.input_schema()) and json.dumps(tool.catalog_entry()), tool.name
        if tool.risk is Risk.READ:
            assert tool.output_schema(), f"{tool.name}: a read declares its output"
        if tool.cost_key:
            assert DEFAULT_COSTS[tool.cost_key]["credits"] >= 0
        assert all(key in plan_table.LIMITS for key in tool.limit_keys), tool.name
        assert tool.permission is None or tool.permission in set().union(*ROLE_PERMISSIONS.values())


def test_a_tool_that_breaks_the_contract_is_refused():
    with pytest.raises(ToolError):
        _spec(name="NotValid")
    with pytest.raises(ToolError):
        _spec(summary="")
    with pytest.raises(ToolError):
        _spec(risk=Risk.READ, scope=Scope.READ, cost_key="text_post")        # a read costs nothing
    with pytest.raises(ToolError):
        _spec(cost_key="no_such_price_row")
    with pytest.raises(ToolError):
        _spec(cost_note="")                                                  # no price row and no explanation
    with pytest.raises(ToolError):
        _spec(permission="no_such_permission")
    with pytest.raises(ToolError):
        _spec(risk=Risk.WRITE, scope=Scope.PUBLISH)                          # scope must match the risk
    with pytest.raises(ToolError):
        _spec(risk=Risk.DESTRUCTIVE, scope=Scope.CREATE, input_model=_In)    # destructive needs an explicit confirm
    with pytest.raises(ToolError):
        _spec(risk=Risk.PUBLISH, scope=Scope.PUBLISH)                        # anything that publishes is held for approval
    assert _spec(risk=Risk.DESTRUCTIVE, input_model=_Confirmed)
    assert _spec(risk=Risk.PUBLISH, scope=Scope.PUBLISH, approval="person")


def test_the_same_name_cannot_be_registered_twice():
    registry = ToolRegistry()
    registry.register(_spec())
    with pytest.raises(ToolError):
        registry.register(_spec())


def test_everything_that_publishes_waits_for_a_person_and_everything_destructive_needs_confirm():
    for tool in get_registry().all():
        if tool.risk is Risk.PUBLISH:
            assert tool.approval == "person" and tool.scope is Scope.PUBLISH, tool.name
        if tool.risk is Risk.DESTRUCTIVE:
            assert "confirm" in tool.input_model.model_fields, tool.name


def test_deleting_posts_through_a_tool_needs_an_explicit_confirm():
    tool = get_registry().get("content.bulk_delete")
    with pytest.raises(ValidationError):
        tool.input_model(piece_ids=["a"])                      # no confirm
    with pytest.raises(ValidationError):
        tool.input_model(piece_ids=["a"], confirm=False)       # a "no" is not a confirm
    assert tool.input_model(piece_ids=["a"], confirm=True).confirm is True
    assert tool.undo == "Only support can restore deleted posts."   # no promise of a self-service undo


def test_what_a_person_can_see_follows_the_key_scope_and_their_role():
    registry = get_registry()
    editor = ROLE_PERMISSIONS["editor"]
    reviewer = ROLE_PERMISSIONS["viewer"]

    read_only = {t.name for t in registry.visible_to({Scope.READ}, editor)}
    assert read_only and all(registry.get(n).risk is Risk.READ for n in read_only)

    make = {t.name for t in registry.visible_to({Scope.READ, Scope.CREATE}, editor)}
    assert "text.generate" in make and "publish.now" not in make          # the key was not given publish

    full = {t.name for t in registry.visible_to({Scope.READ, Scope.CREATE, Scope.PUBLISH}, editor)}
    assert {"text.generate", "publish.now"} <= full

    # a Reviewer's role holds only approve_content, so a key made by one can read but never create or publish
    as_reviewer = {t.name for t in registry.visible_to({Scope.READ, Scope.CREATE, Scope.PUBLISH}, reviewer)}
    # only reads, and never more than an editor can read (a read that needs create or edit rights is hidden from them)
    assert as_reviewer and as_reviewer <= read_only
    assert all(registry.get(n).risk is Risk.READ for n in as_reviewer)


def test_a_tool_switched_off_by_ops_is_not_offered():
    registry = ToolRegistry()
    registry.register(_spec(name="demo.on"))
    registry.register(_spec(name="demo.off", enabled=False))
    assert [t.name for t in registry.visible_to({Scope.CREATE}, set())] == ["demo.on"]


def test_the_read_tools_limit_what_can_be_asked_for():
    page = reads.ListPostsIn
    assert page().limit == 20
    for bad in (0, 51, -1):
        with pytest.raises(ValidationError):
            page(limit=bad)
    with pytest.raises(ValidationError):
        page(cursor="x" * 201)
    with pytest.raises(ValidationError):
        page(stage="not_a_stage")
    with pytest.raises(ValidationError):
        page(unknown_field=1)                                  # extra fields are refused, so a typo is not ignored
    with pytest.raises(ValidationError):
        reads.NoInput(anything=1)


def test_prices_and_scopes_are_stated_for_the_tools_people_pay_for():
    registry = get_registry()
    assert registry.get("text.generate").cost_key == "text_post"
    assert registry.get("campaign.regenerate_media").cost_key == "picture"
    assert registry.get("audio.video").cost_key == "render" and "render_minutes_month" in registry.get("audio.video").limit_keys
    assert registry.get("publish.now").scope is Scope.PUBLISH
    assert registry.get("credits.summary").cost_key is None and registry.get("credits.summary").risk is Risk.READ


def test_the_calendar_and_run_reads_refuse_nonsense_ranges_and_statuses():
    from datetime import date

    assert reads.CalendarIn(start=date(2026, 12, 1), end=date(2026, 12, 7))
    with pytest.raises(ValidationError):
        reads.CalendarIn(start=date(2026, 12, 7), end=date(2026, 12, 1))        # the end before the start
    with pytest.raises(ValidationError):
        reads.CalendarIn(start=date(2026, 1, 1), end=date(2028, 1, 1))          # more than a year at once
    with pytest.raises(ValidationError):
        reads.CalendarIn(start="not a date")
    with pytest.raises(ValidationError):
        reads.RunsIn(status="finished")
    with pytest.raises(ValidationError):
        reads.PostIn(id="")
    with pytest.raises(ValidationError):
        reads.TopAssetsIn(kind="text")                                           # only pictures, recordings and videos are ranked
    assert reads.TopAssetsIn(kind="image").limit == 10


def test_the_read_tools_cover_the_questions_people_ask_without_opening_pages():
    registry = get_registry()
    reads_offered = {t.name for t in registry.all() if t.risk is Risk.READ}
    assert {"credits.summary", "plan.limits", "library.posts", "library.post", "approvals.waiting", "calendar.upcoming", "brands.list",
            "campaigns.list", "runs.list", "analytics.top_assets", "me.whoami", "credits.prices"} <= reads_offered
    # every read has a plain example question, which is what the assistant uses to choose it
    assert all(registry.get(n).examples for n in reads_offered)


def test_every_picture_edit_carries_the_version_it_was_made_on_and_can_be_undone():
    for tool in pictures.specs():
        assert "base_version" in tool.input_model.model_fields, tool.name
        assert tool.undo, tool.name
        assert tool.permission == "create_content" and tool.scope is Scope.CREATE
    with pytest.raises(ValidationError):
        pictures.SetHeadlineIn(image_asset_id="a", text="Hello")                 # no base_version: refused, not written over
    with pytest.raises(ValidationError):
        pictures.SetHeadlineIn(image_asset_id="a", text="Hello", base_version=0)
    assert pictures.SetHeadlineIn(image_asset_id="a", text="  Hello  ", base_version=3).text == "Hello"


def test_the_picture_tools_reject_what_the_editor_would_reject():
    ok = dict(image_asset_id="a", slide_number=1, base_version=2)
    with pytest.raises(ValidationError):
        pictures.SetHeadlineIn(**ok, text="   ")
    with pytest.raises(ValidationError):
        pictures.SetColorsIn(**ok)                                               # nothing to change
    with pytest.raises(ValidationError):
        pictures.SetColorsIn(**ok, text_color="red")                             # not a hex colour
    assert pictures.SetColorsIn(**ok, accent_color="#1F6feb").accent_color == "#1F6feb"
    with pytest.raises(ValidationError):
        pictures.ApplyLookIn(**ok)
    with pytest.raises(ValidationError):
        pictures.ApplyLookIn(**ok, corner_radius=0.5)
    assert pictures.ApplyLookIn(**ok, glow=False).glow is False                  # switching a look off is a change too
    with pytest.raises(ValidationError):
        pictures.SetLogoSpotIn(**ok, position="middle_left")
    with pytest.raises(ValidationError):
        pictures.AddBigNumberIn(**ok, figure="1,234,567,890,123")                # too long to read on a picture
    with pytest.raises(ValidationError):
        pictures.AddChartIn(**ok, bars=[])
    with pytest.raises(ValidationError):
        pictures.AddChartIn(**ok, bars=[{"label": "x", "value": 1}] * 7)         # at most six bars
    with pytest.raises(ValidationError):
        pictures.AddChartIn(**ok, bars=[{"label": "x", "value": -1}])
    with pytest.raises(ValidationError):
        pictures.ReshapeIn(**ok, layouts=["quote_1_1"], focus_x=0.5)             # a focus is both numbers or neither
    with pytest.raises(ValidationError):
        pictures.ReshapeIn(**ok, layouts=["not_a_layout"])
    with pytest.raises(ValidationError):
        pictures.ReshapeIn(**ok, layouts=["quote_1_1"] * 7)


def test_the_picture_tools_stay_in_step_with_the_editors_own_limits():
    from typing import get_args

    from app.models.image_asset import MAX_CHART_BARS, LayoutPreset
    from app.pipelines.media.logo_placement import BACKINGS, POSITIONS

    assert pictures.AddChartIn.model_fields["bars"].metadata[0].min_length == 1
    assert max(m.max_length for m in pictures.AddChartIn.model_fields["bars"].metadata if hasattr(m, "max_length") and m.max_length) == MAX_CHART_BARS
    assert set(get_args(pictures.SetLogoSpotIn.model_fields["position"].annotation)) == set(POSITIONS)
    assert set(get_args(pictures.SetLogoSpotIn.model_fields["backing"].annotation.__args__[0])) == set(BACKINGS)
    assert {p.value for p in LayoutPreset} >= {"quote_1_1", "story_9_16", "linkedin_post"}


def test_restoring_a_version_and_resetting_are_offered_as_the_way_back():
    registry = get_registry()
    assert registry.get("image.restore_version") and registry.get("image.reset")
    assert "image.restore_version" in registry.get("image.set_headline").undo
    assert registry.get("image.apply_look").cost_key is None and "remove_background" in registry.get("image.apply_look").cost_note


def test_every_video_edit_carries_the_version_it_was_made_on_and_can_be_undone():
    for tool in videos.specs():
        assert "base_version" in tool.input_model.model_fields, tool.name
        assert tool.undo and tool.permission == "edit_content" and tool.scope is Scope.CREATE, tool.name
    ok = dict(project_id="p1", base_version=4)
    with pytest.raises(ValidationError):
        videos.TrimClipIn(project_id="p1", clip_id="c1", src_in_s=0, duration_s=5)          # no base_version
    assert videos.TrimClipIn(**ok, clip_id="c1", src_in_s=1.5, duration_s=5).duration_s == 5


def test_the_video_tools_reject_what_the_editor_would_reject():
    ok = dict(project_id="p1", base_version=2)
    with pytest.raises(ValidationError):
        videos.TrimClipIn(**ok, clip_id="bad id!", src_in_s=0, duration_s=5)               # an id is letters, numbers, - and _
    with pytest.raises(ValidationError):
        videos.TrimClipIn(**ok, clip_id="c1", src_in_s=0, duration_s=0)                    # a clip must play for some time
    with pytest.raises(ValidationError):
        videos.ReorderClipsIn(**ok, clip_ids=["a", "b", "a"])                              # each clip named once
    with pytest.raises(ValidationError):
        videos.ReorderClipsIn(**ok, clip_ids=[])
    with pytest.raises(ValidationError):
        videos.AddTextIn(**ok, text="   ", start_s=0, duration_s=2)
    with pytest.raises(ValidationError):
        videos.AddTextIn(**ok, text="Hi", start_s=0, duration_s=2, color="white")
    with pytest.raises(ValidationError):
        videos.AddTextIn(**ok, text="Hi", start_s=0, duration_s=2, size=7)                 # the smallest text is 8
    with pytest.raises(ValidationError):
        videos.AddTextIn(**ok, text="Hi", start_s=0, duration_s=2, font="comic sans")
    with pytest.raises(ValidationError):
        videos.SetCaptionsStyleIn(**ok)                                                    # nothing to change
    with pytest.raises(ValidationError):
        videos.SetCaptionsStyleIn(**ok, size=3)
    assert videos.SetCaptionsStyleIn(**ok, upper=False).upper is False                     # switching a setting off is a change
    with pytest.raises(ValidationError):
        videos.SetTransitionIn(**ok, clip_id="c1", kind="spin")
    assert videos.SetTransitionIn(**ok, clip_id="c1", kind="dissolve").duration_s == 0.5
    with pytest.raises(ValidationError):
        videos.AddMusicIn(**ok, media_id="m1", gain_db=40)
    with pytest.raises(ValidationError):
        videos.AddMusicIn(**ok, media_id="m1", duck_amount="extreme")
    with pytest.raises(ValidationError):
        videos.SetLookIn(**ok, clip_id="c1")
    with pytest.raises(ValidationError):
        videos.SetLookIn(**ok, clip_id="c1", contrast=3)
    with pytest.raises(ValidationError):
        videos.SetCanvasIn(**ok, size="cinema")
    with pytest.raises(ValidationError):
        videos.AddBleepsIn(**ok, stretches=[{"start_ms": 500, "end_ms": 400}])            # the start comes first
    with pytest.raises(ValidationError):
        videos.AddBleepsIn(**ok, stretches=[])
    assert videos.SetEmphasisIn(**ok, stretches=[]).stretches == []                        # clearing the stressed words is allowed


def test_the_video_tools_stay_in_step_with_the_editors_own_lists():
    from typing import get_args

    from app.video import edl

    assert set(get_args(videos.SetTransitionIn.model_fields["kind"].annotation)) == set(edl.TRANSITIONS)
    assert set(get_args(videos.SetCanvasIn.model_fields["size"].annotation)) == set(edl.CANVAS_SIZES)
    assert set(get_args(videos.AddMusicIn.model_fields["duck_amount"].annotation)) == set(edl.DUCK_AMOUNTS)
    longest = [m.max_length for m in videos.AddTextIn.model_fields["text"].metadata if getattr(m, "max_length", None)]
    assert longest == [edl.MAX_TEXT]


def test_the_video_edits_cost_nothing_and_say_that_the_render_is_priced_separately():
    for tool in videos.specs():
        assert tool.cost_key is None and "render" in tool.cost_note.lower(), tool.name
    registry = get_registry()
    assert registry.get("video.restore_version") and registry.get("video.set_captions_style")
    assert len(registry) == sum(len(m.specs()) for m in _MODULES)


def test_the_library_tools_are_typed_from_their_own_routes_and_follow_the_decisions():
    registry = get_registry()
    library = [t for t in catalog_library.specs()]
    assert len(library) >= 30
    # decisions (approve, send back) are a person's by default and use the approve scope
    decisions = [t for t in library if t.risk is Risk.DECISION]
    assert {t.name for t in decisions} == {"library.approve_post", "library.reject_post", "library.approve_many", "library.approve_waiting",
                                           "library.approve_run"}
    assert all(t.scope is Scope.APPROVE and t.human_only for t in decisions)
    # a delete needs an explicit confirm
    for name in ("library.delete_post", "library.delete_run"):
        tool = registry.get(name)
        with pytest.raises(ValidationError):
            tool.input_model(piece_id="p1") if name == "library.delete_post" else tool.input_model(session_id="s1")
    deleting = registry.get("library.delete_post").input_model
    assert deleting(piece_id="p1", confirm=True).confirm is True
    with pytest.raises(ValidationError):
        deleting(piece_id="p1", confirm=False)
    # scheduling is held for a person, and only a person may override a held post
    schedule = registry.get("library.schedule_post")
    assert schedule.scope is Scope.PUBLISH and schedule.approval == "person" and schedule.permission == "publish_content"
    assert set(schedule.human_only_fields) == {"confirm_publish_anyway", "confirm_send"}
    # permissions come from the routes themselves
    assert registry.get("library.edit_post").permission == "edit_content"
    assert registry.get("library.approve_post").permission == "approve_content"
    assert registry.get("library.regenerate_picture").cost_key == "picture"


_CATALOGS = (catalog_text, catalog_publish, catalog_audio, catalog_images, catalog_campaigns, catalog_video, catalog_account)


def test_every_catalog_tool_follows_the_rules_for_its_kind():
    for module in _CATALOGS:
        for tool in module.specs():
            if tool.risk is Risk.READ:
                assert tool.scope is Scope.READ and tool.examples, tool.name
            if tool.risk is Risk.DESTRUCTIVE:
                assert "confirm" in tool.input_model.model_fields and tool.undo, tool.name
            if tool.risk is Risk.DECISION:
                assert tool.human_only and tool.scope is Scope.APPROVE, tool.name
            if tool.risk is Risk.PUBLISH:
                assert tool.approval == "person" and tool.scope is Scope.PUBLISH, tool.name
            if tool.risk is not Risk.READ:
                assert tool.permission is not None, tool.name      # a key made by a viewer can never write


def test_decisions_are_never_open_to_a_key_by_default():
    registry = get_registry()
    for name in ("audio.approve", "audio.sign_off", "image.approve", "video.approve", "videogov.add_consent", "activity.decide",
                 "activity.set_autonomy_threshold"):
        assert registry.get(name).human_only, name
    assert not [t.name for t in registry.visible_to({Scope.READ, Scope.CREATE, Scope.PUBLISH}, ROLE_PERMISSIONS["owner"]) if t.human_only]


def test_deleting_through_a_tool_needs_an_explicit_confirm_in_every_catalog():
    for name in ("audio.delete", "image.delete", "video.delete_project", "videolib.delete", "campaigns.delete"):
        model = get_registry().get(name).input_model
        with pytest.raises(ValidationError):
            model.model_validate({})
        assert model.model_fields["confirm"].annotation is not None


def test_text_written_elsewhere_can_be_saved_as_a_draft_for_free():
    tool = get_registry().get("library.create_draft")
    assert tool.risk is Risk.WRITE and tool.cost_note == "Free: no AI call."


def test_the_whole_catalog_is_large_and_every_tool_name_is_unique():
    specs = [t for m in _MODULES for t in m.specs()]
    assert len(specs) == len({t.name for t in specs}) == len(get_registry().all()) >= 390


def test_every_override_or_confirm_a_person_gives_is_a_person_only_field():
    for tool in get_registry().all():
        if tool.risk is Risk.DESTRUCTIVE:
            continue                                   # a destructive tool's own `confirm` is the caller's explicit yes, not an override
        overrides = {f for f in tool.input_schema().get("properties", {}) if f in ("confirm_publish_anyway", "confirm_send")}
        assert overrides <= set(tool.human_only_fields), (tool.name, overrides - set(tool.human_only_fields))
