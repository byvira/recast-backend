"""Brand profile page rules: identity fields per type, cleaning, completeness, word lists. Pure."""

from app.pipelines.brand import profile_sections as ps


def keys(brand_type, group=None):
    return [f["key"] for f in ps.identity_fields(brand_type) if group in (None, f["group"])]


def test_each_type_has_its_real_stored_fields_first():
    assert keys("Person", "core") == ["name", "profession", "bio", "achievements"]
    assert keys("Business", "core") == ["company_name", "description", "industry", "offerings"]
    assert keys("Product", "core") == ["product_name", "description", "category", "use_cases"]
    assert keys("Personal Brand", "core") == ["name", "tagline", "mission", "industry"]


def test_shop_and_entertainment_get_the_basic_details_card():
    assert keys("Shop") == keys("Entertainment") == ["name", "tagline", "description"]
    assert keys("Something new") == ["name", "tagline", "description"]


def test_every_type_has_exactly_one_required_name_field():
    for t in ps.IDENTITY_FIELDS:
        required = [f["key"] for f in ps.identity_fields(t) if f["required"]]
        assert len(required) == 1 and required[0] in ("name", "company_name", "product_name")


def test_identity_is_cleaned_to_each_fields_kind():
    out, errors = ps.sanitize_identity("Person", {
        "name": "  Asha   Rao ", "bio": "x" * 2000, "achievements": ["A", "a", " B ", ""] + [f"n{i}" for i in range(20)],
        "website": "asha.dev", "headline": "y" * 500,
    })
    assert errors == {}
    assert out["name"] == "Asha Rao" and len(out["bio"]) == ps.LONG_MAX and len(out["headline"]) == ps.SHORT_MAX
    assert out["achievements"][:2] == ["A", "B"] and len(out["achievements"]) == ps.LIST_MAX_ITEMS
    assert out["website"] == "https://asha.dev"


def test_a_bad_address_a_bad_option_and_a_missing_name_are_reported_not_saved():
    _, errors = ps.sanitize_identity("Person", {"name": "", "website": "not a url"})
    assert set(errors) == {"name", "website"}
    _, errors = ps.sanitize_identity("Business", {"company_name": "Acme", "company_size": "huge"})
    assert "company_size" in errors


def test_values_stored_elsewhere_are_kept_when_a_subset_is_saved():
    existing = {"name": "Asha", "bio": "keep me", "legacy_field": "from onboarding"}
    out, errors = ps.sanitize_identity("Person", {"name": "Asha R"}, existing)
    assert errors == {} and out["name"] == "Asha R" and out["bio"] == "keep me" and out["legacy_field"] == "from onboarding"


def test_addresses():
    assert ps.normalise_url("https://a.com/x") == "https://a.com/x"
    assert ps.normalise_url("") == ""
    assert ps.normalise_url("localhost") is None and ps.normalise_url("a b.com") is None


def test_audience_keeps_known_levels_and_reports_unknown_ones():
    out, errors = ps.sanitize_audience({"reading_level": "Expert", "knowledge_base": "Beginner", "primary_pain_point": "  Too   busy "})
    assert errors == {} and out["primary_pain_point"] == "Too busy"
    _, errors = ps.sanitize_audience({"reading_level": "Genius"})
    assert "reading_level" in errors


def test_platforms_are_cleaned_and_limited():
    assert ps.sanitize_platforms(["linkedin", "linkedin", "x"]) == (["linkedin", "x"], {})
    assert ps.sanitize_platforms("linkedin")[1]
    assert ps.sanitize_platforms(["bad;drop"])[1]
    assert ps.sanitize_platforms([f"p{i}" for i in range(20)])[1]


def test_word_lists_drop_repeats_and_refuse_a_banned_replacement():
    out, errors = ps.validate_words({
        "banned_words": ["Synergy", "synergy", "leverage"], "openers": ["Hi", "hi"],
        "preferred_synonyms": [{"original": "use", "replacement": "utilise"}, {"original": "USE", "replacement": "x"}],
    })
    assert out["banned_words"] == ["Synergy", "leverage"] and out["openers"] == ["Hi"]
    assert len(out["preferred_synonyms"]) == 1 and errors == {}
    _, errors = ps.validate_words({"banned_words": ["leverage"], "preferred_synonyms": [{"original": "use", "replacement": "Leverage"}]})
    assert "preferred_synonyms" in errors


def test_an_empty_profile_is_zero_percent_and_points_at_the_first_section():
    c = ps.compute_completeness({"brand_type": "Person"})
    assert c["percent"] == 0 and c["first_incomplete"] == "identity" and c["total"] == 8


def test_a_full_profile_is_one_hundred_percent():
    c = ps.compute_completeness({
        "brand_type": "Person", "identity": {"name": "A", "profession": "Coach", "bio": "Bio", "achievements": ["x"]},
        "audience": {"primary_pain_point": "Busy"}, "voice_tone": {"tones": ["warm"]},
        "manual_data": {"banned_words": ["x"]}, "training_samples": [{"id": "1"}],
        "visual_identity": {"colors": {"primary": "#fff"}}, "is_complete": True, "platforms": ["linkedin"],
    })
    assert c["percent"] == 100 and c["first_incomplete"] is None


def test_identity_needs_its_name_and_a_few_fields_to_count():
    thin = ps.compute_completeness({"brand_type": "Person", "identity": {"name": "A"}})
    assert thin["sections"]["identity"] is False
    ok = ps.compute_completeness({"brand_type": "Person", "identity": {"name": "A", "bio": "b", "profession": "c"}})
    assert ok["sections"]["identity"] is True


def test_onboarding_answers_live_in_the_field_for_the_brand_type():
    assert ps.onboarding_field_for("Business") == "icp_data"
    assert ps.onboarding_field_for("Person") is None


def test_the_default_platform_is_always_one_of_the_brands_platforms():
    assert ps.resolve_default_platform(["LinkedIn", "X"], "X", "LinkedIn") == "X"
    assert ps.resolve_default_platform(["LinkedIn", "X"], "Nope", "X") == "X"
    assert ps.resolve_default_platform(["LinkedIn", "X"], None, "Gone") == "LinkedIn"
    assert ps.resolve_default_platform([], "X", "X") is None
