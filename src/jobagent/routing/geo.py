"""Classify location text: does it include Kenya, Africa or the world, or only other places?

Used only on the employer's own text. Conservative by design: anything not clearly a place
list is treated as unknown, and unknown never causes a SKIP.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

KENYA = re.compile(r"\b(kenya|nairobi|nakuru|mombasa|kisumu|eldoret|thika|kenyan)\b", re.I)
# "Africa" on its own, East Africa, sub-Saharan, pan-African. Not "South Africa" (a country) or
# West/North/Southern/Central Africa (regions without Kenya, listed in OTHER_REGIONS).
AFRICA_WIDE = re.compile(
    r"(?<!south )(?<!north )(?<!west )(?<!central )(?<!southern )(?<!northern )(?<!western )"
    r"\b(africa(?:-wide)?|pan[- ]african|sub[- ]saharan|across africa)\b",
    re.I,
)
WORLDWIDE = re.compile(
    r"\b(world\s?wide|anywhere(?: in the world)?|global(?:ly)?|any country|all countries|"
    r"fully distributed|international(?:ly)?|remote[- ]first,? anywhere)\b",
    re.I,
)
# Regions that contain Kenya but are not "Africa-wide": not an exclusion, but not proof either.
CONTAINS_KENYA = re.compile(
    r"\b(?:(?i:emea|east african time|utc\s?\+\s?3|gmt\s?\+\s?3|commonwealth)|EAT)\b"
)
# Regions that do not contain Kenya.
OTHER_REGIONS = re.compile(
    r"\b(latam|latin america|south america|central america|north america|americas|"
    r"europe(?:an union)?|eu|eea|eastern europe|western europe|nordics?|dach|benelux|"
    r"apac|asia(?:[- ]pacific)?|southeast asia|oceania|anz|middle east|gulf|gcc|mena|"
    r"caribbean|us time ?zones?|(?:west|western|north|northern|southern|central) africa)\b",
    re.I,
)
# Abbreviations only count in capitals, so "contact us" or "eat" are never places.
ABBREVIATIONS = re.compile(
    r"(?<![\w.])(US|USA|U\.S\.A?\.?|UK|UAE|EU|CET|CEST|EST|EDT|PST|PDT|CST|MST)(?![\w])"
)
COUNTRIES = [
    "afghanistan",
    "albania",
    "algeria",
    "andorra",
    "angola",
    "argentina",
    "armenia",
    "australia",
    "austria",
    "azerbaijan",
    "bahamas",
    "bahrain",
    "bangladesh",
    "barbados",
    "belarus",
    "belgium",
    "belize",
    "benin",
    "bhutan",
    "bolivia",
    "bosnia",
    "botswana",
    "brazil",
    "brunei",
    "bulgaria",
    "burkina faso",
    "burundi",
    "cambodia",
    "cameroon",
    "canada",
    "cape verde",
    "chad",
    "chile",
    "china",
    "colombia",
    "comoros",
    "congo",
    "costa rica",
    "croatia",
    "cuba",
    "cyprus",
    "czechia",
    "czech republic",
    "denmark",
    "djibouti",
    "dominica",
    "dominican republic",
    "ecuador",
    "egypt",
    "el salvador",
    "eritrea",
    "estonia",
    "eswatini",
    "ethiopia",
    "fiji",
    "finland",
    "france",
    "gabon",
    "gambia",
    "georgia",
    "germany",
    "ghana",
    "greece",
    "grenada",
    "guatemala",
    "guinea",
    "guyana",
    "haiti",
    "honduras",
    "hong kong",
    "hungary",
    "iceland",
    "india",
    "indonesia",
    "iran",
    "iraq",
    "ireland",
    "israel",
    "italy",
    "ivory coast",
    "jamaica",
    "japan",
    "jordan",
    "kazakhstan",
    "kosovo",
    "kuwait",
    "kyrgyzstan",
    "laos",
    "latvia",
    "lebanon",
    "lesotho",
    "liberia",
    "libya",
    "liechtenstein",
    "lithuania",
    "luxembourg",
    "madagascar",
    "malawi",
    "malaysia",
    "maldives",
    "mali",
    "malta",
    "mauritania",
    "mauritius",
    "mexico",
    "moldova",
    "monaco",
    "mongolia",
    "montenegro",
    "morocco",
    "mozambique",
    "myanmar",
    "namibia",
    "nepal",
    "netherlands",
    "new zealand",
    "nicaragua",
    "niger",
    "nigeria",
    "north macedonia",
    "norway",
    "oman",
    "pakistan",
    "palestine",
    "panama",
    "papua new guinea",
    "paraguay",
    "peru",
    "philippines",
    "poland",
    "portugal",
    "puerto rico",
    "qatar",
    "romania",
    "russia",
    "rwanda",
    "saudi arabia",
    "senegal",
    "serbia",
    "seychelles",
    "sierra leone",
    "singapore",
    "slovakia",
    "slovenia",
    "somalia",
    "south africa",
    "south korea",
    "korea",
    "south sudan",
    "spain",
    "sri lanka",
    "sudan",
    "suriname",
    "sweden",
    "switzerland",
    "syria",
    "taiwan",
    "tajikistan",
    "tanzania",
    "thailand",
    "togo",
    "trinidad",
    "tunisia",
    "turkey",
    "turkiye",
    "turkmenistan",
    "uganda",
    "ukraine",
    "united arab emirates",
    "united kingdom",
    "england",
    "scotland",
    "wales",
    "united states",
    "america",
    "uruguay",
    "uzbekistan",
    "venezuela",
    "vietnam",
    "yemen",
    "zambia",
    "zimbabwe",
]
OTHER_COUNTRY = re.compile(
    r"(?<![\w.])(" + "|".join(sorted(COUNTRIES, key=len, reverse=True)) + r")(?![\w])", re.I
)
# Words that carry no location meaning in a location field.
NEUTRAL = re.compile(
    r"\b(remote|hybrid|on[- ]?site|in[- ]office|office|work from home|wfh|home[- ]based|flexible|"
    r"telecommute|full[- ]time|part[- ]time|contract|and|or|the|of|in|based|n/?a|multiple|"
    r"locations?|time ?zones?|any|region|country|countries|city|state|province|county)\b",
    re.I,
)


@dataclass
class PlaceReading:
    kenya: bool = False
    africa_wide: bool = False
    worldwide: bool = False
    contains_kenya_region: bool = False
    other_places: list[str] = field(default_factory=list)

    @property
    def inclusive(self) -> bool:
        return self.kenya or self.africa_wide or self.worldwide

    @property
    def excludes_kenya(self) -> bool:
        """Only other countries or regions are named, and nothing that covers Kenya."""
        return bool(self.other_places) and not self.inclusive and not self.contains_kenya_region


def read_places(text: str | None) -> PlaceReading:
    if not text:
        return PlaceReading()
    others = (
        [m.group(0) for m in OTHER_COUNTRY.finditer(text)]
        + [m.group(0) for m in OTHER_REGIONS.finditer(text)]
        + [m.group(0) for m in ABBREVIATIONS.finditer(text)]
    )
    # "Pakistan; Pakistan" -> one entry, original casing kept for the quote
    return PlaceReading(
        kenya=bool(KENYA.search(text)),
        africa_wide=bool(AFRICA_WIDE.search(text)),
        worldwide=bool(WORLDWIDE.search(text)),
        contains_kenya_region=bool(CONTAINS_KENYA.search(text)),
        other_places=list(dict.fromkeys(o.strip() for o in others)),
    )


# ---------------------------------------------------------------- employer-text restrictions

_PLACE_LIST = r"(?P<places>[A-Za-z][A-Za-z ,./&()'-]{1,140}?)"
RESTRICTION_PATTERNS = [
    # "US only", "U.S.-only", "UK only role", "LATAM only"
    re.compile(r"\b(?P<places>(?:[A-Z][A-Za-z.]*\.?\s?){1,3})[- ]only\b"),
    # "must be based in / located in / reside in / live in X"
    re.compile(
        r"\bmust\s+(?:currently\s+)?(?:be\s+)?(?:based|located|living|residing|reside|live)\s+in\s+(?:the\s+)?"
        + _PLACE_LIST
        + r"(?=[.;:\n]|$| to | and must| in order)",
        re.I,
    ),
    # "open (only) to candidates/applicants/residents (based/located) in X"
    re.compile(
        r"\b(?:open|available|limited|restricted)\s+(?:only\s+)?to\s+(?:candidates|applicants|residents|"
        r"people|individuals|those)\s+(?:who\s+(?:are|live)\s+)?(?:currently\s+)?(?:based|located|"
        r"residing|living)?\s*(?:in|within)\s+(?:the\s+)?" + _PLACE_LIST + r"(?=[.;:\n]|$)",
        re.I,
    ),
    # "authorized / eligible / legally able to work in the US"
    re.compile(
        r"\b(?:authori[sz]ed|eligible|legally\s+(?:able|permitted|entitled))\s+to\s+work\s+in\s+(?:the\s+)?"
        + _PLACE_LIST
        + r"(?=[.;:,\n]|$| without| and)",
        re.I,
    ),
    # "US citizens / residents only"
    re.compile(r"\b" + _PLACE_LIST + r"\s+(?:citizens?|residents?|nationals?)\s+only\b", re.I),
]
NOT_ABOUT_LOCATION = re.compile(
    r"salary|compensation|pay range|base pay|benefits?|insurance|equity|bonus|401|pension|"
    r"medical|health|holiday|tax",
    re.I,
)


@dataclass
class Restriction:
    quote: str
    places: str
    reading: PlaceReading


def find_restrictions(text: str) -> list[Restriction]:
    """Statements in employer text that limit where applicants may be.

    Sentences about salary or benefits ("the salary range is for US residents only") are
    ignored: they describe pay, not who may apply.
    """
    out: list[Restriction] = []
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
        if NOT_ABOUT_LOCATION.search(sentence):
            continue
        for rx in RESTRICTION_PATTERNS:
            for m in rx.finditer(sentence):
                places = m.group("places")
                reading = read_places(places)
                if reading.other_places or reading.inclusive or reading.contains_kenya_region:
                    out.append(Restriction(m.group(0).strip(), places.strip(), reading))
    return out
