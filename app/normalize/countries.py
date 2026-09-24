"""Country canonicalization + ISO-3166 alpha-3 codes (for choropleth rendering).

CT.gov already uses mostly standardized names; the override table covers the ones pycountry
does not resolve by exact name. Unknown values are kept verbatim with ``iso3=None``.
"""

from __future__ import annotations

from functools import lru_cache

import pycountry

_OVERRIDES: dict[str, tuple[str, str | None]] = {
    "south korea": ("South Korea", "KOR"),
    "korea, republic of": ("South Korea", "KOR"),
    "north korea": ("North Korea", "PRK"),
    "korea, democratic people's republic of": ("North Korea", "PRK"),
    "russia": ("Russia", "RUS"),
    "russian federation": ("Russia", "RUS"),
    "turkey": ("Turkey", "TUR"),
    "türkiye": ("Turkey", "TUR"),
    "taiwan": ("Taiwan", "TWN"),
    "iran": ("Iran", "IRN"),
    "iran, islamic republic of": ("Iran", "IRN"),
    "vietnam": ("Vietnam", "VNM"),
    "viet nam": ("Vietnam", "VNM"),
    "czechia": ("Czechia", "CZE"),
    "czech republic": ("Czechia", "CZE"),
    "moldova": ("Moldova", "MDA"),
    "moldova, republic of": ("Moldova", "MDA"),
    "syria": ("Syria", "SYR"),
    "laos": ("Laos", "LAO"),
    "bolivia": ("Bolivia", "BOL"),
    "venezuela": ("Venezuela", "VEN"),
    "tanzania": ("Tanzania", "TZA"),
    "macedonia, the former yugoslav republic of": ("North Macedonia", "MKD"),
    "north macedonia": ("North Macedonia", "MKD"),
    "congo, the democratic republic of the": ("DR Congo", "COD"),
    "the democratic republic of the congo": ("DR Congo", "COD"),
    "côte d'ivoire": ("Côte d'Ivoire", "CIV"),
    "cote d'ivoire": ("Côte d'Ivoire", "CIV"),
    "palestinian territories, occupied": ("Palestine", "PSE"),
    "palestinian territory, occupied": ("Palestine", "PSE"),
    "hong kong": ("Hong Kong", "HKG"),
    "former serbia and montenegro": ("Former Serbia and Montenegro", None),
}


@lru_cache(maxsize=512)
def canonical_country(raw: str) -> tuple[str, str | None]:
    """Return (display name, ISO3 or None)."""
    name = " ".join(raw.split())
    key = name.casefold()
    if key in _OVERRIDES:
        return _OVERRIDES[key]
    try:
        match = pycountry.countries.lookup(name)
    except LookupError:
        return name, None
    return name, match.alpha_3
