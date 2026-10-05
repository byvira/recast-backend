"""A brand's name is found for every brand type, so no screen shows "Untitled Brand" for a brand that has a name."""
import pytest

from app.shared.activity.runs import brand_label
from app.shared.brand_name import brand_display_name


@pytest.mark.parametrize("brand_type,identity,expected", [
    ("Person", {"name": "Asha Rao"}, "Asha Rao"),
    ("Personal Brand", {"name": "Asha Writes"}, "Asha Writes"),
    ("Business", {"company_name": "Harbor and Pine"}, "Harbor and Pine"),
    ("Business", {"companyName": "Harbor and Pine"}, "Harbor and Pine"),
    ("Product", {"product_name": "Recast", "category": "SaaS"}, "Recast"),
    ("Product", {"productName": "Recast"}, "Recast"),
    ("Shop", {"name": "Corner Books"}, "Corner Books"),
    ("Entertainment", {"name": "Night Shift"}, "Night Shift"),
    ("Entertainment", {"title": "Night Shift"}, "Night Shift"),
])
def test_every_brand_type_shows_its_own_name(brand_type, identity, expected):
    assert brand_display_name({"brand_type": brand_type, "identity": identity}) == expected


def test_the_types_own_key_wins_over_a_stray_one():
    brand = {"brand_type": "Business", "identity": {"name": "Old label", "company_name": "Real Company"}}
    assert brand_display_name(brand) == "Real Company"
    product = {"brand_type": "Product", "identity": {"company_name": "Maker", "product_name": "The Product"}}
    assert brand_display_name(product) == "The Product"


def test_a_name_is_found_even_when_the_type_is_missing_or_unknown():
    assert brand_display_name({"identity": {"product_name": "Recast"}}) == "Recast"
    assert brand_display_name({"brand_type": "Something new", "identity": {"company_name": "Acme"}}) == "Acme"
    assert brand_display_name({"name": "Top level name"}) == "Top level name"


def test_blank_or_missing_names_use_the_fallback():
    assert brand_display_name(None, "Untitled Brand") == "Untitled Brand"
    assert brand_display_name({"identity": {}}, "Untitled Brand") == "Untitled Brand"
    assert brand_display_name({"brand_type": "Product", "identity": {"product_name": "   "}}, "Untitled Brand") == "Untitled Brand"
    assert brand_display_name({"identity": {"name": 42}}) == ""


def test_the_name_is_trimmed():
    assert brand_display_name({"brand_type": "Person", "identity": {"name": "  Asha  "}}) == "Asha"


def test_the_activity_label_uses_the_same_rule():
    assert brand_label({"brand_type": "Product", "identity": {"product_name": "Recast"}}) == "Recast"
    assert brand_label(None) == ""


async def test_the_drafts_list_shows_the_real_brand_name_for_every_type_and_untitled_only_when_there_is_none():
    from uuid import uuid4

    from app.db.mongo import brand_profiles
    from app.pipelines.text.storage import _attach_display_names

    ws_id = f"ws-{uuid4()}"
    made = {}
    for brand_type, identity in (("Product", {"product_name": "Recast"}), ("Business", {"company_name": "Harbor and Pine"}),
                                 ("Person", {"name": "Asha"}), ("Shop", {"name": "Corner Books"}), ("Product", {})):
        brand_id = f"b-{uuid4()}"
        await brand_profiles.insert_one({"id": brand_id, "workspace_id": ws_id, "brand_type": brand_type, "identity": identity})
        made[brand_id] = brand_type
    pieces = [{"brand_id": brand_id, "user_id": "nobody"} for brand_id in made]

    await _attach_display_names(pieces)

    assert [p["brand_name"] for p in pieces] == ["Recast", "Harbor and Pine", "Asha", "Corner Books", "Untitled Brand"]
