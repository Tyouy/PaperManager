"""Conservative arXiv author names from the bounded title-to-abstract region."""
from __future__ import annotations

import re
import unicodedata

import pymupdf


_AFFILIATION = re.compile(
    r"\b(?:university|universities|institute|institution|laboratory|laboratories|lab|labs|research|"
    r"department|school|college|faculty|center|centre|google|brain|openai|deepmind|microsoft|"
    r"meta|facebook|amazon|nvidia|inc|corp|corporation|llc|foundation|academy|hospital|"
    r"engineering|technology|technologies|science|sciences|computing|computer|physics|"
    r"mathematics|toronto|stanford|berkeley|cambridge|oxford|usa|uk|china|canada|germany|"
    r"france|switzerland|australia|singapore)\b|大学|学院|研究|实验室|中心|公司|医院|学部", re.I)
_PARTICLES = {"van", "von", "de", "del", "der", "den", "da", "di", "la", "le", "bin", "al", "el", "dos", "das", "du"}
_CITIES = {"new york", "san francisco", "mountain view", "long beach", "united states", "united kingdom", "hong kong", "los angeles"}
_MARKERS = "∗*†‡§¶#♠♣♥♦⁎✉✝"


def _segments(line: dict) -> list[str]:
    """Preserve PDF text-object order, separating wide author-column gaps."""
    groups = []
    text, previous = "", None
    for span in line.get("spans", []):
        value = span.get("text", "")
        if not value:
            continue
        gap = span["bbox"][0] - previous["bbox"][2] if previous else 0
        if previous and gap > max(25, span.get("size", 10) * 3):
            if text.strip():
                groups.append(text.strip())
            text = ""
        elif previous and gap > span.get("size", 10) * .2 and text and text[-1].isalpha() and value[0].isalpha():
            text += " "
        text += value
        previous = span
    if text.strip():
        groups.append(text.strip())
    return groups


def _clean_name(value: str) -> str:
    # NFKC turns superscript affiliation numbers into digits for predictable stripping.
    value = unicodedata.normalize("NFKC", value)
    value = re.sub("[" + re.escape(_MARKERS) + "]", "", value)
    value = re.sub(r"^[\d\s]+|[\d\s]+$", "", value)
    value = re.sub(r"\s+", " ", value).strip(" .,")
    return value


def _name(value: str) -> bool:
    if re.fullmatch(r"[\u4e00-\u9fff]{2,5}", value):
        return True
    words = value.split()
    if len(words) < 2 or len(words) > 6:
        return False
    # A first name reduced to initials cannot be expanded to a full name locally.
    if len(re.sub(r"[^\w]", "", words[0])) < 2 or "." in words[0]:
        return False
    if not any(c.isupper() for c in words[0]):
        return False
    for word in words:
        if word.casefold() in _PARTICLES:
            continue
        if not all(char.isalpha() or char in "-'’." for char in word):
            return False
        if not any(char.isupper() for char in word):
            return False
    return len(re.sub(r"[^\w]", "", words[-1])) >= 2


def header_authors(page, title: str) -> list[dict]:
    """Return complete full-name candidates only between actual title and Abstract.

    This local fallback is intended for arXiv pages lacking author metadata. It
    deliberately returns no names when title/abstract anchors or a complete name
    sequence cannot be established.
    """
    if not title.strip():
        return []
    title_rects = page.search_for(title)
    if not title_rects:
        return []
    lines = [line for block in page.get_text("dict", flags=pymupdf.TEXTFLAGS_TEXT, sort=False)["blocks"]
             for line in block.get("lines", [])
             if abs(line.get("dir", (1, 0))[0] - 1) < .05 and abs(line.get("dir", (1, 0))[1]) < .05]
    title_top = min(rect.y0 for rect in title_rects)
    headings = [line for line in lines if re.fullmatch(r"\s*(?:Abstract|摘要)\s*[:：]?\s*",
                "".join(span.get("text", "") for span in line.get("spans", [])), re.I)
                and line["bbox"][1] > title_top]
    if not headings:
        # Unlabeled abstracts are common in author manuscripts. An explicit
        # affiliation boundary still bounds the contiguous author area safely.
        headings = [line for line in lines if line["bbox"][1] > title_top
                    and _AFFILIATION.search("".join(span.get("text", "") for span in line.get("spans", [])))]
        if not headings:
            return []
        abstract_top = min(line["bbox"][1] for line in headings)
    else:
        abstract_top = min(line["bbox"][1] for line in headings)
    # Search is explicit so malformed heading text cannot supply a guessed bound.
    abstract_rects = page.search_for("Abstract") + page.search_for("摘要")
    if abstract_rects and not any(abs(rect.y0 - abstract_top) < 8 for rect in abstract_rects):
        return []
    relevant_title = sorted((rect for rect in title_rects if rect.y0 < abstract_top), key=lambda rect: rect.y0)
    title_bottom = relevant_title[0].y1
    for rect in relevant_title[1:]:
        if rect.y0 > title_bottom + max(15, rect.height * 1.5):
            break
        title_bottom = max(title_bottom, rect.y1)
    if title_bottom >= abstract_top:
        return []
    names = []
    bounded_segments = []
    for line in lines:
        if line["bbox"][1] < title_bottom - 1 or line["bbox"][3] > abstract_top + 1:
            continue
        bounded_segments.extend(_segments(line))
    # A comma-separated list can wrap in the middle of a name ("Kenji\nWatanabe").
    # Join only an unfinished final token, never two complete name lists.
    joined = []
    for segment in bounded_segments:
        if joined and re.search(r"[,;]\s*[A-Z][^\W\d_]+\s*$", joined[-1]):
            joined[-1] += " " + segment
        else:
            joined.append(segment)
    for segment in joined:
        if re.search(r"\bet\s+al\b|等作者", segment, re.I):
            return []
        if "@" in segment or re.search(r"https?://|www\.", segment, re.I) or _AFFILIATION.search(segment):
            continue
        # Drop affiliation superscripts, including ranges such as 1-3,6†.
        segment = re.sub(r"\d+(?:[-–,]\d+)*[" + re.escape(_MARKERS) + r"]*", "", segment)
        cleaned = _clean_name(segment)
        if not cleaned or cleaned.casefold() in _CITIES:
            continue
        pieces = re.split(r"\s+and\s+|\s*&\s*|[,，;；、]", cleaned)
        candidate_names = [_clean_name(piece) for piece in pieces if piece.strip()]
        if not candidate_names:
            continue
        if not all(_name(name) for name in candidate_names):
            # Initials or an ambiguous partially readable name invalidate a
            # previously recognized list rather than selecting an earlier last author.
            if re.search(r"^[A-ZŁÀ-ÖØ-Þ]\.\s+[^\W\d_]", cleaned) or len(pieces) > 1:
                return []
            continue
        names.extend(candidate_names)
    unique = list(dict.fromkeys(names))
    return [{"full_name": name} for name in unique]
