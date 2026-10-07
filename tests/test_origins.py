"""Which origins the API answers from a browser."""
from app.core.origins import build_origins

VERCEL = "https://recastbyvira.vercel.app"
NEW = "https://recast.byvirastudio.com"


def test_the_app_domain_is_allowed_in_production_without_any_setting():
    origins = build_origins(production=True, production_domain="https://recast-backend.onrender.com", frontend_url=VERCEL, allowed=[])
    assert NEW in origins and VERCEL in origins


def test_each_origin_is_listed_once_and_trailing_slashes_are_dropped():
    origins = build_origins(production=True, production_domain="", frontend_url=VERCEL + "/", allowed=[VERCEL, NEW + "/", "https://staging.example.com"])
    assert origins == [VERCEL, NEW, "https://staging.example.com"]


def test_without_a_primary_domain_the_frontend_address_is_used():
    assert build_origins(production=True, production_domain="", frontend_url=VERCEL, allowed=[])[0] == VERCEL


def test_outside_production_only_the_allowed_list_is_used():
    origins = build_origins(production=False, production_domain="x", frontend_url=VERCEL, allowed=["http://localhost:3000"])
    assert origins == ["http://localhost:3000"]


def test_blank_entries_are_ignored():
    assert build_origins(production=True, production_domain="", frontend_url="", allowed=["", "  "], app_origins=[]) == []
