"""The name to show for a brand, for every brand type.

Each type keeps its name under its own key in `identity` (a business under `company_name`, a product under `product_name`, the rest
under `name`), and older records used camelCase keys. Every place that shows or sends a brand's name uses this one function, so a
type can never fall through to "Untitled Brand" because one list of keys missed it."""
from __future__ import annotations

from typing import Any, Optional

# The type's own keys first, then every other key a name has been stored under.
_BY_TYPE: dict[str, tuple[str, ...]] = {
    "Person": ("name", "full_name", "fullName"),
    "Personal Brand": ("name", "brand_name", "brandName"),
    "Business": ("company_name", "companyName", "business_name", "businessName", "name"),
    "Product": ("product_name", "productName", "name"),
    "Shop": ("name", "shop_name", "shopName", "store_name", "storeName"),
    "Entertainment": ("name", "title", "show_name", "showName", "project_name", "projectName"),
}
_ANY = (
    "name", "company_name", "companyName", "business_name", "product_name", "productName", "brand_name", "brandName",
    "shop_name", "store_name", "title", "full_name",
)


def brand_display_name(brand: Optional[dict], fallback: str = "") -> str:
    """The brand's name, or `fallback` when none was ever entered."""
    brand = brand or {}
    identity: dict[str, Any] = brand.get("identity") or {}
    for key in (*_BY_TYPE.get(str(brand.get("brand_type") or ""), ()), *_ANY):
        value = identity.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for key in ("name", "brand_name"):
        value = brand.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return fallback
