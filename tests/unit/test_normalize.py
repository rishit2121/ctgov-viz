"""Normalization of real and synthetic CT.gov records."""

from __future__ import annotations

import pytest

from app.contracts.trial import DateValue
from app.normalize import trial as paths
from app.normalize.countries import canonical_country
from app.normalize.dates import months_between, parse_date_struct
from app.normalize.interventions import is_placebo, resolve, split_arm_intervention
from app.normalize.labels import phase_bucket
from app.normalize.trial import NormalizationError, normalize_study
from app.registry.fields import REGISTRY
from tests.conftest import study, trial

# ------------------------------------------------------------------ intervention names


@pytest.mark.parametrize(
    ("raw", "key"),
    [
        ("Pembrolizumab", "pembrolizumab"),
        ("pembrolizumab", "pembrolizumab"),
        ("Pembrolizumab (KEYTRUDA®)", "pembrolizumab"),
        ("KEYTRUDA® (pembrolizumab)", "pembrolizumab"),
        ("KEYTRUDA ®( Pembrolizumab)", "pembrolizumab"),
        ("Pembrolizumab (MK-3475) (KEYTRUDA)", "pembrolizumab"),
        ("Pembrolizumab Injection [Keytruda]", "pembrolizumab"),
        ("Pembrolizumab 25 MG/1 ML Intravenous Solution [KEYTRUDA]", "pembrolizumab"),
        ("Pembrolizumab 200 mg", "pembrolizumab"),
        ("Immunotherapy (Pembrolizumab)", "pembrolizumab"),
        ("MK-3475", "pembrolizumab"),
        ("Opdivo", "nivolumab"),
        ("5-FU", "fluorouracil"),
        ("Gemcitabine hydrochloride", "gemcitabine"),
        ("Abraxane", "nab-paclitaxel"),  # not merged into paclitaxel
    ],
)
def test_resolve_merges_spellings_of_the_same_agent(raw: str, key: str) -> None:
    assert resolve(raw)[0] == key


@pytest.mark.parametrize(
    ("raw", "key"),
    [
        # amino-acid variant markers change identity
        ("gp100:209-217 (210M)", "gp100:209-217 (210m)"),
        ("MART-1:26-35(27L)", "mart-1:26-35(27l)"),
        # combination products / names stay whole
        ("Pembrolizumab/Quavonlimab", "pembrolizumab/quavonlimab"),
        ("NGM707 plus pembrolizumab (KEYTRUDA®)", "ngm707 plus pembrolizumab"),
        ("Paclitaxel", "paclitaxel"),
    ],
)
def test_resolve_does_not_merge_distinct_agents(raw: str, key: str) -> None:
    assert resolve(raw)[0] == key


def test_resolve_label_uses_generic_name() -> None:
    assert resolve("KEYTRUDA® (pembrolizumab)")[1] == "Pembrolizumab"
    assert resolve("Some Novel Agent 5 mg")[1] == "Some Novel Agent"


def test_placebo_detection() -> None:
    assert is_placebo("Placebo for pembrolizumab")
    assert is_placebo("Normal saline")
    assert not is_placebo("Pembrolizumab")


def test_arm_prefix_split() -> None:
    assert split_arm_intervention("Drug: Pembrolizumab") == ("Drug", "Pembrolizumab")
    assert split_arm_intervention("Dietary Supplement: Vitamin D") == ("Dietary Supplement",
                                                                     "Vitamin D")
    assert split_arm_intervention("Ratio 1:2 dosing") == (None, "Ratio 1:2 dosing")


# ------------------------------------------------------------------ dates, countries, phases


@pytest.mark.parametrize(
    ("raw", "year", "month", "precision"),
    [("2019", 2019, None, "year"), ("2019-05", 2019, 5, "month"),
     ("2019-05-04", 2019, 5, "day"), ("2019-13", None, None, None), ("May 2019", None, None, None)],
)
def test_partial_dates_keep_their_precision(raw: str, year: int | None, month: int | None,
                                            precision: str | None) -> None:
    d = parse_date_struct({"date": raw, "type": "ACTUAL"})
    assert (d.year, d.month, d.precision) == (year, month, precision)
    assert d.raw == raw


def test_missing_date_is_explicit() -> None:
    assert parse_date_struct(None) == DateValue()


def test_months_between_requires_month_precision() -> None:
    start = parse_date_struct({"date": "2019-01"})
    assert months_between(start, parse_date_struct({"date": "2020-07"})) == 18
    assert months_between(start, parse_date_struct({"date": "2020"})) is None


@pytest.mark.parametrize(
    ("raw", "name", "iso3"),
    [("United States", "United States", "USA"), ("Korea, Republic of", "South Korea", "KOR"),
     ("Russian Federation", "Russia", "RUS"), ("  Germany ", "Germany", "DEU"),
     ("Former Serbia and Montenegro", "Former Serbia and Montenegro", None),
     ("Atlantis", "Atlantis", None)],
)
def test_country_canonicalization(raw: str, name: str, iso3: str | None) -> None:
    assert canonical_country(raw) == (name, iso3)


@pytest.mark.parametrize(
    ("phases", "bucket"),
    [((), "Not Reported"), (("PHASE3",), "Phase 3"), (("PHASE1", "PHASE2"), "Phase 1/2"),
     (("PHASE2", "PHASE3"), "Phase 2/3"), (("EARLY_PHASE1",), "Early Phase 1"),
     (("NA",), "Not Applicable")],
)
def test_phase_bucket(phases: tuple[str, ...], bucket: str) -> None:
    assert phase_bucket(phases) == bucket


# ------------------------------------------------------------------ whole records


def test_repeated_sites_give_one_country(real_studies: dict) -> None:
    t = normalize_study(real_studies["repeated_site_country"])
    raw = [loc["country"] for loc in
           real_studies["repeated_site_country"]["protocolSection"]["contactsLocationsModule"]
           ["locations"]]
    assert len(raw) >= 3
    assert t.countries == ("United States",)


def test_multinational_study_lists_each_country_once(real_studies: dict) -> None:
    t = normalize_study(real_studies["multinational"])
    assert len(t.countries) == len(set(t.countries)) >= 4
    assert all(t.country_iso3[c] for c in t.countries)


def test_real_edge_cases(real_studies: dict) -> None:
    n = {k: normalize_study(v) for k, v in real_studies.items()}
    assert n["multi_phase"].phases == ("PHASE1", "PHASE2")
    assert n["no_phase"].phases == ()
    assert n["no_locations"].countries == ()
    assert n["missing_start"].start.year is None
    assert n["month_precision_start"].start.precision == "month"
    assert n["estimated_start"].start.kind == "ESTIMATED"
    assert n["zero_enrollment"].enrollment == 0
    assert n["no_interventions"].interventions == ()
    assert any(i.is_placebo for i in n["placebo_arm"].interventions)
    assert all("®" not in i.label for i in n["trademark_name"].interventions)


def test_every_registry_source_path_is_recorded(real_studies: dict) -> None:
    t = normalize_study(real_studies["combination_arm"])
    for fdef in REGISTRY.values():
        for path in fdef.source_paths:
            assert path in t.source, path


def test_arm_keys_match_intervention_keys() -> None:
    t = trial("NCT00000001",
              interventions=[("Pembrolizumab (KEYTRUDA®)", "DRUG"), ("Placebo", "DRUG")],
              arms=[["Drug: Pembrolizumab (KEYTRUDA®)"], ["Drug: Placebo"]])
    keys = {i.key for i in t.interventions}
    assert set().union(*t.arm_intervention_keys) == keys == {"pembrolizumab", "placebo"}


def test_duplicate_values_within_a_study_are_deduped() -> None:
    t = trial("NCT00000002", conditions=["Melanoma", "Melanoma"], phases=["PHASE3", "PHASE2"],
              interventions=[("Keytruda", "DRUG"), ("Pembrolizumab", "DRUG")],
              countries=["France", "France", "Spain"])
    assert t.conditions == ("Melanoma",)
    assert t.phases == ("PHASE2", "PHASE3")
    assert [i.key for i in t.interventions] == ["pembrolizumab"]
    assert t.countries == ("France", "Spain")
    # evidence keeps every registered spelling
    assert [e["name"] for e in t.source[paths.P_INTERVENTIONS]] == ["Keytruda", "Pembrolizumab"]


def test_record_without_nct_id_is_rejected() -> None:
    raw = study("NCT00000003")
    del raw["protocolSection"]["identificationModule"]["nctId"]
    with pytest.raises(NormalizationError):
        normalize_study(raw)


def test_malformed_nested_values_are_tolerated() -> None:
    raw = study("NCT00000004")
    ps = raw["protocolSection"]
    ps["designModule"]["phases"] = "PHASE3"  # not a list
    ps["conditionsModule"] = {"conditions": [None, "", "  Melanoma "]}
    ps["contactsLocationsModule"] = {"locations": [{"city": "Paris"}, "garbage"]}
    ps["armsInterventionsModule"] = {"interventions": [{"type": "DRUG"}, "x"], "armGroups": [1]}
    t = normalize_study(raw)
    assert t.phases == ()
    assert t.conditions == ("Melanoma",)
    assert t.countries == ()
    assert t.interventions == ()
