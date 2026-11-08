"""Recognize source declarations in front matter, not citations in the body."""
from __future__ import annotations

import re


def detect_preprint(first: str, native_first: str = "") -> str:
    for stream in (first, native_first):
        # Platform watermarks may follow the abstract in extraction order. Require
        # a masthead, URL/DOI declaration or explicit preprint watermark there.
        front = re.split(r"(?im)^\s*(?:\d+[.\s]+)?(?:references|bibliography|参考文献)\s*$", stream)[0]
        header = re.split(r"\babstract\b|摘\s*要", front, maxsplit=1, flags=re.I)[0]
        for name, pattern in (
            ("biorxiv", r"bioRxiv"), ("medrxiv", r"medRxiv"),
            ("chemrxiv", r"ChemRxiv"), ("researchsquare", r"Research\s+Square"),
            ("ssrn", r"SSRN"), ("preprints.org", r"Preprints\.org"),
            ("arxiv", r"arXiv"),
        ):
            if re.search(r"(?im)^\s*" + pattern + r"(?:\s+preprint)?\s*(?:$|doi\s*:|https?://)", front):
                return name
        for name, domain in (("biorxiv", "biorxiv.org"), ("medrxiv", "medrxiv.org"),
                             ("chemrxiv", "chemrxiv.org"), ("researchsquare", "researchsquare.com"),
                             ("preprints.org", "preprints.org"), ("ssrn", "ssrn.com")):
            if re.search(r"(?im)^\s*(?:(?:preprint|available|posted|doi)\s*:?\s*)?https?://(?:www\.)?" + re.escape(domain) + r"/\S+\s*$", header):
                return name
        if re.search(r"(?im)^\s*(?:(?:a|this is a|author|research|submitted)\s+)?pre[ -]?print\b[^\n]{0,150}$", header):
            return "preprint"
        for name, prefix in (("chemrxiv", r"10\.26434/chemrxiv"), ("researchsquare", r"10\.21203/rs\.")):
            if re.search(r"(?im)^\s*(?:doi\s*:\s*)?(?:https?://doi\.org/)?" + prefix + r"\S*\s*$", header):
                return name
        if re.search(r"(?im)^\s*(?:not peer[- ]reviewed|working paper|预印本)\b[^\n]*$", header):
            return "preprint"
    return ""
