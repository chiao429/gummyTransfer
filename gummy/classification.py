"""Clearly labelled, provisional classifications derived from product titles."""

import re


def title_measurement_candidates(title: str) -> list[str]:
    """Return only explicit measurement keywords; never infer SAE from 'inch'."""
    candidates = []
    if re.search(r"\bmetric\b", title, re.IGNORECASE) and not re.search(
        r"\bnon[- ]metric\b", title, re.IGNORECASE
    ):
        candidates.append("metric")
    if re.search(r"\bsae\b", title, re.IGNORECASE):
        candidates.append("sae")
    return candidates
