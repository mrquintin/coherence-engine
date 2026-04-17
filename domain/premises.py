"""Societal premises database — 47 premises across 10 domains."""

import json
import os

_FALLBACK_DOMAINS = {
    "individual_rights": {
        "name": "Individual Rights",
        "premises": [
            "Every person has the right to make decisions about their own life without coercive interference, provided those decisions do not harm others.",
            "Freedom of speech, thought, and expression are fundamental and inalienable rights.",
            "All individuals are entitled to equal treatment under the law regardless of identity.",
            "Privacy is a fundamental right; surveillance requires justification and oversight.",
        ],
    },
    "market_economics": {
        "name": "Market Economics",
        "premises": [
            "Free markets, through the price mechanism, allocate resources more efficiently than central planning.",
            "Voluntary exchange between consenting parties creates mutual benefit.",
            "Competition drives innovation and improves products and services over time.",
            "Property rights and contract enforcement are prerequisites for economic prosperity.",
            "Excessive regulation distorts markets and creates deadweight loss.",
        ],
    },
    "technological_progress": {
        "name": "Technological Progress",
        "premises": [
            "Technological innovation is the primary driver of long-term economic growth.",
            "The benefits of new technology generally outweigh its risks over time.",
            "Open access to information accelerates scientific and technological progress.",
            "Automation displaces some jobs but creates new ones and increases overall productivity.",
        ],
    },
    "democratic_governance": {
        "name": "Democratic Governance",
        "premises": [
            "Political authority derives from the consent of the governed, expressed through free and fair elections.",
            "Separation of powers prevents the concentration of authority and protects against tyranny.",
            "An independent judiciary is essential for the rule of law.",
            "Transparency and accountability are necessary for legitimate governance.",
        ],
    },
    "social_contract": {
        "name": "Social Contract",
        "premises": [
            "Society has an obligation to ensure the basic well-being of all its members.",
            "Taxation is a legitimate mechanism for funding public goods and redistributing resources.",
            "Education is both a right and a social necessity for informed citizenship.",
            "Healthcare access is a fundamental social right, not merely a market commodity.",
        ],
    },
    "property_rights": {
        "name": "Property Rights",
        "premises": [
            "Private property is a natural right that predates the state.",
            "Intellectual property protections incentivize creation and innovation.",
            "Eminent domain is justified only for genuine public necessity with fair compensation.",
            "Common resources require governance mechanisms to prevent tragedy of the commons.",
        ],
    },
    "meritocracy": {
        "name": "Meritocracy",
        "premises": [
            "Individuals should be rewarded in proportion to their talent, effort, and contribution.",
            "Equal opportunity is more important than equal outcome.",
            "Hierarchies based on competence serve organizational and social efficiency.",
            "Systemic barriers to merit-based advancement should be identified and removed.",
        ],
    },
    "scientific_rationalism": {
        "name": "Scientific Rationalism",
        "premises": [
            "Empirical evidence and the scientific method are the most reliable paths to knowledge.",
            "Claims should be proportioned to evidence; extraordinary claims require extraordinary evidence.",
            "Peer review and replication are essential for validating scientific findings.",
            "Science is self-correcting; errors are identified and eliminated over time.",
        ],
    },
    "secular_ethics": {
        "name": "Secular Ethics",
        "premises": [
            "Moral principles can be derived from reason, empathy, and human flourishing without religious authority.",
            "The reduction of suffering is a universal ethical imperative.",
            "Moral progress is possible and has occurred throughout history.",
            "Ethical obligations extend to future generations and the natural environment.",
        ],
    },
    "national_sovereignty": {
        "name": "National Sovereignty",
        "premises": [
            "Nations have the right to self-determination and control over their borders.",
            "National security may justify temporary restrictions on individual liberties.",
            "Cultural preservation is a legitimate concern in immigration and trade policy.",
            "International cooperation should not supersede democratic national governance.",
            "Military defense is a core function of the state.",
        ],
    },
}

_FALLBACK_TENSIONS = [
    ("individual_rights", "social_contract", "Individual liberty vs. collective welfare obligations"),
    ("market_economics", "social_contract", "Free market allocation vs. redistribution for basic needs"),
    ("individual_rights", "national_sovereignty", "Personal freedom vs. national security restrictions"),
    ("market_economics", "secular_ethics", "Profit maximization vs. environmental/ethical obligations"),
    ("property_rights", "social_contract", "Absolute property rights vs. taxation and common goods"),
    ("meritocracy", "social_contract", "Merit-based rewards vs. guaranteed minimum well-being"),
    ("technological_progress", "national_sovereignty", "Open information flow vs. national security"),
    ("individual_rights", "scientific_rationalism", "Freedom of belief vs. evidence-based policy"),
    ("democratic_governance", "market_economics", "Democratic regulation vs. market freedom"),
    ("national_sovereignty", "secular_ethics", "National self-interest vs. universal ethical obligations"),
    ("property_rights", "technological_progress", "IP protection vs. open access to information"),
    ("meritocracy", "individual_rights", "Hierarchical competence vs. equal treatment"),
]


def _load_from_json():
    """Load premises and tensions from the bundled JSON data file."""
    json_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "data", "societal_premises.json"
    )
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        domains = data.get("domains", {})
        raw_tensions = data.get("tensions", [])
        tensions = [
            (t["domain_a"], t["domain_b"], t["description"]) for t in raw_tensions
        ]
        return domains, tensions
    except (OSError, json.JSONDecodeError, KeyError):
        return None, None


_loaded_domains, _loaded_tensions = _load_from_json()
DOMAINS = _loaded_domains if _loaded_domains is not None else _FALLBACK_DOMAINS
TENSIONS = _loaded_tensions if _loaded_tensions is not None else _FALLBACK_TENSIONS


_DEFAULT_NORMATIVE = (0.33, 0.33, 0.33)

DOMAIN_NORMATIVE = {
    "individual_rights": (0.80, 0.10, 0.10),
    "market_economics": (0.20, 0.70, 0.10),
    "technological_progress": (0.20, 0.70, 0.10),
    "democratic_governance": (0.45, 0.20, 0.35),
    "social_contract": (0.25, 0.35, 0.40),
    "property_rights": (0.55, 0.25, 0.20),
    "meritocracy": (0.35, 0.45, 0.20),
    "scientific_rationalism": (0.20, 0.65, 0.15),
    "secular_ethics": (0.30, 0.30, 0.40),
    "national_sovereignty": (0.20, 0.30, 0.50),
}


def get_domain_normative(domain_key: str) -> tuple:
    """Return declared (rights, utilitarian, deontic) profile for a domain."""
    return DOMAIN_NORMATIVE.get(domain_key, _DEFAULT_NORMATIVE)


def get_domain_premises(domain_key: str) -> list:
    """Get premises for a specific domain."""
    domain = DOMAINS.get(domain_key)
    if domain is None:
        return []
    return domain["premises"]


def get_all_premises() -> list:
    """Get all 47 societal premises as a flat list."""
    all_p = []
    for domain in DOMAINS.values():
        all_p.extend(domain["premises"])
    return all_p


def get_domain_names() -> dict:
    """Get mapping of domain keys to human-readable names."""
    return {k: v["name"] for k, v in DOMAINS.items()}
