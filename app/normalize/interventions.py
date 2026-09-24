"""Conservative intervention-name normalization.

Registered names vary widely for the same agent ("Pembrolizumab (KEYTRUDA®)", "KEYTRUDA®
(pembrolizumab)", "Pembrolizumab Injection [Keytruda]", "Pembrolizumab 200 mg"). Grouping on raw
names would split one drug into many nodes, so names are resolved with deterministic rules only:

1. drop trademark symbols, collapse whitespace;
2. peel trailing ``(...)`` / ``[...]`` groups (brands, codes, notes) — except amino-acid variant
   markers such as ``(210M)``, which change the agent's identity;
3. drop a trailing dose ("200 mg", "25 MG/ML ...") and formulation/salt words ("injection",
   "hydrochloride");
4. map exact brand names / development codes to the generic name via ``ALIASES``; if what remains
   is only a generic category word ("Immunotherapy (Pembrolizumab)"), use the peeled name instead.

There is no fuzzy matching: two names merge only if they resolve to the same string. Combination
names ("Pembrolizumab/Quavonlimab") are kept whole, as registered. The raw name is always kept for
evidence.
"""

from __future__ import annotations

import re

# generic name -> exact brand names / development codes (case-insensitive).
ALIASES: dict[str, tuple[str, ...]] = {
    "pembrolizumab": ("keytruda", "mk-3475", "mk3475", "lambrolizumab", "sch 900475"),
    "nivolumab": ("opdivo", "bms-936558", "mdx-1106", "ono-4538"),
    "ipilimumab": ("yervoy", "mdx-010", "bms-734016"),
    "atezolizumab": ("tecentriq", "mpdl3280a", "rg7446"),
    "durvalumab": ("imfinzi", "medi4736"),
    "avelumab": ("bavencio", "msb0010718c"),
    "cemiplimab": ("libtayo", "regn2810"),
    "tremelimumab": ("imjudo", "cp-675,206"),
    "relatlimab": ("bms-986016",),
    "trastuzumab": ("herceptin",),
    "pertuzumab": ("perjeta",),
    "bevacizumab": ("avastin",),
    "cetuximab": ("erbitux",),
    "rituximab": ("rituxan", "mabthera"),
    "paclitaxel": ("taxol",),
    "nab-paclitaxel": ("abraxane",),
    "docetaxel": ("taxotere",),
    "carboplatin": ("paraplatin",),
    "cisplatin": ("platinol",),
    "oxaliplatin": ("eloxatin",),
    "pemetrexed": ("alimta",),
    "gemcitabine": ("gemzar",),
    "capecitabine": ("xeloda",),
    "fluorouracil": ("5-fu", "5fu", "5-fluorouracil"),
    "dacarbazine": ("dtic",),
    "temozolomide": ("temodar", "temodal", "tmz"),
    "cyclophosphamide": ("cytoxan",),
    "doxorubicin": ("adriamycin",),
    "lenvatinib": ("lenvima",),
    "olaparib": ("lynparza",),
    "osimertinib": ("tagrisso",),
    "dabrafenib": ("tafinlar",),
    "trametinib": ("mekinist",),
    "vemurafenib": ("zelboraf",),
    "cobimetinib": ("cotellic",),
    "encorafenib": ("braftovi",),
    "binimetinib": ("mektovi",),
    "palbociclib": ("ibrance",),
    "ribociclib": ("kisqali",),
    "abemaciclib": ("verzenio",),
    "letrozole": ("femara",),
    "anastrozole": ("arimidex",),
    "tamoxifen": ("nolvadex",),
    "fulvestrant": ("faslodex",),
    "aldesleukin": ("proleukin",),
    "talimogene laherparepvec": ("imlygic", "t-vec"),
}
_LOOKUP: dict[str, str] = {
    alias: generic for generic, aliases in ALIASES.items() for alias in (generic, *aliases)
}
# Names that say *what kind* of intervention, not *which* one.
_GENERIC_WORDS = frozenset({
    "immunotherapy", "chemotherapy", "drug", "study drug", "treatment", "therapy",
    "investigational product", "monoclonal antibody", "checkpoint inhibitor", "anti-pd-1",
    "anti-pd-1 antibody", "pd-1 inhibitor", "targeted therapy", "biologic",
})

# Study assessments / data collection registered as "interventions" (mostly NCI-standard names).
# They describe how outcomes are measured, not what is given, so networks drop them by default.
# Exact match on the resolved key only; the list was built from the most frequent non-drug
# entries in real melanoma, lung and breast cancer records.
ANCILLARY = frozenset({
    "biospecimen collection", "laboratory biomarker analysis", "pharmacological study",
    "pharmacokinetic study", "computed tomography", "magnetic resonance imaging",
    "positron emission tomography", "fludeoxyglucose f-18", "x-ray imaging", "bone scan",
    "echocardiography", "echocardiography test", "electrocardiography",
    "multigated acquisition scan", "biopsy", "biopsy procedure", "blood sampling", "blood draw",
    "lumbar puncture", "flow cytometry", "immunohistochemistry staining method",
    "questionnaire administration", "questionnaire", "questionnaires", "survey administration",
    "interview", "quality-of-life assessment", "electronic health record review",
    "medical chart review", "data collection", "non-interventional study", "non-interventional",
})

_TRADEMARK_RE = re.compile(r"[®™©]")
_TRAILING_GROUP_RE = re.compile(r"\s*[\(\[]([^()\[\]]*)[\)\]]\s*$")
_VARIANT_RE = re.compile(r"^\d+[A-Za-z]$")  # e.g. gp100:209-217(210M)
_DOSE_RE = re.compile(
    r"\s+\d+(?:[.,]\d+)?\s*(?:mg/m2|mg/kg|mg|mcg|µg|ug|g|ml|iu|units?|mci)\b.*$", re.IGNORECASE
)
_SUFFIX_RE = re.compile(
    r"\s+(?:for injection|injection|infusion|intravenous solution|oral solution|tablets?|"
    r"capsules?|hydrochloride|hcl|mesylate|besylate|tosylate|maleate|dimaleate|malate|citrate|"
    r"sulfate|phosphate|acetate|tartrate|alone)$",
    re.IGNORECASE,
)
_WS_RE = re.compile(r"\s+")
# Comparators rather than active interventions.
_PLACEBO_RE = re.compile(
    r"\b(placebo|sham|vehicle|dummy|standard of care|best supportive care|saline|usual care|"
    r"best practice|no intervention|observation)\b",
    re.IGNORECASE,
)


def _strip_suffixes(name: str) -> str:
    name = _DOSE_RE.sub("", name).strip(" ,;-")
    while (stripped := _SUFFIX_RE.sub("", name)) != name and stripped:
        name = stripped
    return name


def resolve(raw: str) -> tuple[str, str]:
    """Return ``(grouping key, display label)`` for a registered intervention name."""
    name = _WS_RE.sub(" ", _TRADEMARK_RE.sub("", raw)).strip()
    peeled: list[str] = []
    while (m := _TRAILING_GROUP_RE.search(name)) and not _VARIANT_RE.match(m.group(1).strip()):
        base = name[: m.start()].strip()
        if not base:
            break
        peeled.append(m.group(1).strip())
        name = base
    base = _strip_suffixes(name) or name

    generic = _LOOKUP.get(base.casefold())
    if generic is None and base.casefold() in _GENERIC_WORDS:
        for inner in peeled:
            inner = _strip_suffixes(inner)
            if inner:
                generic = _LOOKUP.get(inner.casefold())
                if generic is None:
                    return inner.casefold(), inner
                break
    if generic is not None:
        return generic, generic[:1].upper() + generic[1:]
    return base.casefold(), base


def is_placebo(raw: str) -> bool:
    return bool(_PLACEBO_RE.search(raw))


def is_ancillary(key: str) -> bool:
    return key in ANCILLARY


def split_arm_intervention(raw: str) -> tuple[str | None, str]:
    """Arm groups list interventions as 'Drug: Pembrolizumab'. Returns (type label, name)."""
    prefix, sep, rest = raw.partition(": ")
    if sep and prefix and prefix.replace(" ", "").replace("/", "").isalpha() and len(prefix) < 25:
        return prefix, rest
    return None, raw
