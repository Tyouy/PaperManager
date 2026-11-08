"""Own-document arXiv recognition and optional, title-verified Atom metadata."""
from __future__ import annotations

import re
import threading
import time
import unicodedata
import xml.etree.ElementTree as ET
from urllib.parse import urlencode
from urllib.request import Request, build_opener

from .metadata import NoRedirect, title_in_text, titles_agree


_ID = r"(?:\d{2}(?:0[1-9]|1[0-2])\.\d{4,5}|[a-z][a-z.\-]*/\d{7})(?:v[1-9]\d*)?"
_FULL_ID = re.compile(r"^" + _ID + r"$", re.I)
_STAMP = re.compile(r"^\s*arxiv\s*:\s*(?P<id>" + _ID + r")(?P<tail>.*)$", re.I)
_URL = re.compile(r"^\s*(?:https?://)?(?:www\.)?arxiv\.org/(?:abs|pdf|html)/(?P<id>" + _ID + r")(?:\.pdf)?/?(?:[?#][^\s]*)?\s*$", re.I)
_DOI = re.compile(r"^\s*(?:(?:https?://(?:dx\.)?doi\.org/)|(?:doi\s*:\s*))?10\.48550/arxiv\.(?P<id>" + _ID + r")\s*$", re.I)
_DATE = re.compile(r"\b\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{4}\b", re.I)
_REFS = re.compile(r"^\s*(?:\d+[.\s]+)?(?:references|bibliography|参考文献)\s*$", re.I)
_NS = {"atom": "http://www.w3.org/2005/Atom"}
_LOCK = threading.Lock()
_CACHE: dict[str, bytes] = {}
_LAST_REQUEST = 0.0


def _clean_id(value: str) -> str:
    return value.strip().lower() if _FULL_ID.fullmatch(value.strip()) else ""


def detect_arxiv(first_text: str, native_text: str = "") -> str:
    """Recognize an own-paper stamp, not a bibliography mention of another paper.

    Input must contain only the first page. The native stream is optional and can
    recover a rotated stamp when coordinate-sorted extraction interleaves it.
    """
    candidates = []
    for stream in (first_text, native_text):
        in_references = False
        stream = unicodedata.normalize("NFKC", stream).replace("\u200b", "").replace("\u00ad", "")
        # PDF producers frequently split the label, number, category and date into
        # separate spans. Join only syntactically valid pieces, never whole prose.
        stream = re.sub(r"(?im)^(\s*arxiv\s*:)\s*\n\s*(?=" + _ID + r")", r"\1", stream)
        stream = re.sub(r"(?im)^(\s*arxiv\s*:\s*\d{4}\.)\s*\n\s*(?=\d{4,5}(?:v\d+)?(?:\s|$))", r"\1", stream)
        stream = re.sub(r"(?im)^(\s*arxiv\s*:\s*" + _ID + r")\s*\n\s*(?=\[[A-Za-z])", r"\1 ", stream)
        stream = re.sub(r"(?im)^(\s*arxiv\s*:\s*" + _ID + r"\s*\[[A-Za-z.\-]+\])\s*\n\s*(?=\d{1,2}\s+[A-Za-z]+\s+\d{4})", r"\1 ", stream)
        for line in stream.splitlines():
            if _REFS.fullmatch(line):
                in_references = True
                continue
            stamp = _STAMP.fullmatch(line)
            if stamp:
                tail = stamp.group("tail").strip()
                strong = bool(re.search(r"\[[A-Za-z][A-Za-z.\-]*\]", tail) and _DATE.search(tail))
                # Permit a category/date suffix, never arbitrary prose after an ID.
                valid_tail = not tail or bool(re.fullmatch(
                    r"(?:\[[A-Za-z][A-Za-z.\-]*\]\s*)?(?:\d{1,2}\s+[A-Za-z]+\s+\d{4})?", tail))
                if valid_tail and (strong or not in_references):
                    candidates.append((3 if strong else 1, _clean_id(stamp.group("id"))))
                continue
            if not in_references:
                match = _URL.fullmatch(line) or _DOI.fullmatch(line)
                if match:
                    candidates.append((2, _clean_id(match.group("id"))))
    if not candidates:
        return ""
    score = max(candidate[0] for candidate in candidates)
    identities = {identity for rank, identity in candidates if rank == score and identity}
    return next(iter(identities)) if len(identities) == 1 else ""


def filename_arxiv_id(filename: str) -> str:
    """A download-name hint, requiring title or independent research evidence."""
    stem = re.sub(r"\.pdf$", "", filename, flags=re.I)
    stem = re.sub(r"\s*\(\d+\)$", "", stem)
    stem = re.sub(r"^arxiv[-_: ]*", "", stem, flags=re.I)
    return _clean_id(stem)


def _request_feed(arxiv_id: str) -> bytes:
    """Serialize API calls and leave at least three seconds between starts."""
    global _LAST_REQUEST
    with _LOCK:
        if arxiv_id in _CACHE:
            return _CACHE[arxiv_id]
        wait = 3.0 - (time.monotonic() - _LAST_REQUEST)
        if wait > 0:
            time.sleep(wait)
        _LAST_REQUEST = time.monotonic()
        url = "https://export.arxiv.org/api/query?" + urlencode({"id_list": arxiv_id, "max_results": 1})
        request = Request(url, headers={"User-Agent": "LocalPaperRenamer/1.0", "Accept": "application/atom+xml"})
        with build_opener(NoRedirect()).open(request, timeout=12) as response:
            raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ValueError("arXiv 返回内容过大")
        # Cache feed bytes, then verify them separately against each local PDF.
        _CACHE[arxiv_id] = raw
        if len(_CACHE) > 256:
            del _CACHE[next(iter(_CACHE))]
        return raw


def _entry_id(value: str) -> str:
    match = re.fullmatch(r"https?://(?:www\.)?arxiv\.org/abs/(" + _ID + r")", value.strip(), re.I)
    return _clean_id(match.group(1)) if match else ""


def arxiv_metadata(arxiv_id: str, title: str, front: str) -> tuple[dict, list[str]]:
    """Fetch original author order only when the requested ID and PDF title match."""
    identity = _clean_id(arxiv_id)
    if not identity:
        return {}, ["arXiv 编号格式不正确，使用本地识别"]
    try:
        raw = _request_feed(identity)
        # Reject DTD/entity input explicitly; Atom metadata never needs a DTD.
        if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
            raise ValueError("Unexpected XML declaration")
        root = ET.fromstring(raw)
        entries = root.findall("atom:entry", _NS)
        if len(entries) != 1:
            raise ValueError("Expected one arXiv entry")
        entry = entries[0]
        received_id = _entry_id(entry.findtext("atom:id", "", _NS))
        base = lambda value: re.sub(r"v\d+$", "", value)
        if not received_id or base(received_id) != base(identity):
            raise ValueError("arXiv ID mismatch")
        if re.search(r"v\d+$", identity) and received_id != identity:
            raise ValueError("arXiv version mismatch")
        remote_title = " ".join(entry.findtext("atom:title", "", _NS).split())
        if not title_in_text(remote_title, front) or not titles_agree(title, remote_title):
            return {}, ["arXiv 返回的标题与 PDF 首页不符，已保留本地识别"]
        authors = []
        for node in entry.findall("atom:author", _NS):
            name = " ".join(node.findtext("atom:name", "", _NS).split())
            if not name:
                raise ValueError("Incomplete author list")
            authors.append({"full_name": name})
        if not authors:
            raise ValueError("Missing author list")
        record = {"title": [remote_title], "author": authors, "arxiv_id": received_id,
                  "container-title": ["arxiv"], "short-container-title": ["arxiv"]}
        from .dates import normalize_date
        published = normalize_date(entry.findtext("atom:published", "", _NS)[:10])
        if published:
            record["published"] = {"date-parts": [[int(part) for part in published.split("-")]]}
        return record, []
    except Exception:
        return {}, ["arXiv 元数据暂时不可用，已保留本地识别"]
