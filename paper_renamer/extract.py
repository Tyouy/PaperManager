"""Conservative PDF classification and bibliographic extraction."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from .arxiv import arxiv_metadata, detect_arxiv, filename_arxiv_id
from .dates import publication_date
from .header_authors import header_authors
from .metadata import ai_metadata, clean_markup, crossref_metadata, normalized, title_in_text
from .rename import build_filename, file_fingerprint
from .preprint import detect_preprint


# Known abbreviations, not an algorithm that invents acronyms for unknown journals.
JOURNALS = {
    "Nature": "Nature", "Science": "Science", "Cell": "Cell",
    "Nature Communications": "Nat. Commun.", "Scientific Reports": "Sci. Rep.",
    "Nature Materials": "Nat. Mater.", "Nature Methods": "Nat. Methods",
    "Nature Chemistry": "Nat. Chem.", "Nature Physics": "Nat. Phys.",
    "Nature Biotechnology": "Nat. Biotechnol.", "Nature Nanotechnology": "Nat. Nanotechnol.",
    "Nature Neuroscience": "Nat. Neurosci.", "Nature Genetics": "Nat. Genet.",
    "Nature Medicine": "Nat. Med.", "Nature Energy": "Nat. Energy",
    "Nature Catalysis": "Nat. Catal.", "Nature Biomedical Engineering": "Nat. Biomed. Eng.",
    "Nature Chemical Engineering": "Nat. Chem. Eng.", "Nature Chemical Biology": "Nat. Chem. Biol.",
    "Science Advances": "Sci. Adv.", "Science Robotics": "Sci. Robot.",
    "Science Translational Medicine": "Sci. Transl. Med.", "Cell Reports": "Cell Rep.",
    "Proceedings of the National Academy of Sciences": "Proc. Natl. Acad. Sci. U.S.A.",
    "Proceedings of the National Academy of Sciences of the United States of America": "Proc. Natl. Acad. Sci. U.S.A.",
    "Journal of the American Chemical Society": "J. Am. Chem. Soc.",
    "Angewandte Chemie International Edition": "Angew. Chem. Int. Ed.",
    "Advanced Materials": "Adv. Mater.", "Advanced Functional Materials": "Adv. Funct. Mater.",
    "Advanced Energy Materials": "Adv. Energy Mater.", "Advanced Science": "Adv. Sci.",
    "ACS Nano": "ACS Nano", "ACS Catalysis": "ACS Catal.", "Nano Letters": "Nano Lett.",
    "ACS Applied Materials & Interfaces": "ACS Appl. Mater. Interfaces",
    "Chemical Reviews": "Chem. Rev.", "Chemical Society Reviews": "Chem. Soc. Rev.",
    "Chemical Science": "Chem. Sci.", "Chemistry of Materials": "Chem. Mater.",
    "Applied Physics Letters": "Appl. Phys. Lett.", "Physical Review Letters": "Phys. Rev. Lett.",
    "Physical Review B": "Phys. Rev. B", "Physical Review A": "Phys. Rev. A",
    "Physical Review E": "Phys. Rev. E", "Physical Review X": "Phys. Rev. X",
    "Journal of Chemical Physics": "J. Chem. Phys.", "The Journal of Chemical Physics": "J. Chem. Phys.",
    "Biomaterials": "Biomaterials", "Acta Biomaterialia": "Acta Biomater.",
    "Nucleic Acids Research": "Nucleic Acids Res.", "Bioinformatics": "Bioinformatics",
    "PLOS ONE": "PLoS One", "PLOS Biology": "PLoS Biol.", "eLife": "eLife",
    "The Lancet": "Lancet", "New England Journal of Medicine": "N. Engl. J. Med.",
    "The New England Journal of Medicine": "N. Engl. J. Med.", "JAMA": "JAMA",
    "IEEE Transactions on Pattern Analysis and Machine Intelligence": "IEEE Trans. Pattern Anal. Mach. Intell.",
    "IEEE Transactions on Medical Imaging": "IEEE Trans. Med. Imaging",
}
CORR = re.compile(r"correspond(?:ing\s+authors?|ence)(?:\s+to)?|通讯作者|通信作者|联系作者", re.I)
DOI = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.I)


@dataclass
class PDFText:
    text: str
    front: str
    first: str
    title: str
    title_confident: bool
    authors: list[dict] = field(default_factory=list)
    pages: int = 0
    native_text: str = ""
    native_front: str = ""
    native_first: str = ""


def useful_title(value: str) -> bool:
    value = value.strip()
    if len(value) < 12 or len(value) > 600 or len(value.split()) > 90:
        return False
    if re.search(r"@|https?://|\bdoi\b|copyright|all rights reserved|accepted manuscript|untitled|microsoft (?:word|powerpoint)", value, re.I):
        return False
    if re.search(r"\barxiv\s*:\s*(?:\d|[a-z.\-]+/)|10\.48550/arxiv", value, re.I):
        return False
    if re.search(r"provided proper attribution|reproduce the tables|solely for use|scholarly works|permission is granted", value, re.I):
        return False
    if re.match(r"^(?:(?:a|this is a|author|research)\s+)?pre[ -]?print\b|^(?:bioRxiv|medRxiv|ChemRxiv|Research Square|SSRN)\s*(?:preprint|doi|$)", value, re.I):
        return False
    return sum(c.isalpha() for c in value) >= 10


def title_from_page(page, metadata_title: str) -> tuple[str, bool]:
    text = page.get_text("text", sort=True)
    verified_metadata = useful_title(metadata_title) and title_in_text(metadata_title, text)
    metadata_title = re.sub(r"\s+", " ", metadata_title).strip()
    lines = []
    for block in page.get_text("dict", flags=pymupdf.TEXTFLAGS_TEXT)["blocks"]:
        for line in block.get("lines", []):
            direction = line.get("dir", (1, 0))
            if direction[0] < .85 or abs(direction[1]) > .15:
                continue
            spans = [s for s in line["spans"] if s["text"].strip()]
            if not spans:
                continue
            value = "".join(s["text"] for s in spans).strip()
            if line["bbox"][1] > page.rect.height * .69:
                continue
            size = max(s["size"] for s in spans)
            bold = any(span.get("flags", 0) & 16 for span in spans)
            lines.append((line["bbox"], size, value, block["number"], bold))
    candidates = []
    # Group neighboring large-font title lines, while excluding page mastheads and author lists.
    for index, (bbox, size, value, block, bold) in enumerate(lines):
        if size < 10.5:
            continue
        parts = [value]
        for next_bbox, next_size, next_value, next_block, next_bold in lines[index + 1:index + 7]:
            if abs(next_size - size) > 1.2 or next_bbox[1] - bbox[3] > size * 1.5:
                break
            if next_block != block or next_bold != bold or "@" in next_value:
                break
            parts.append(next_value)
            bbox = next_bbox
        candidate = re.sub(r"\s+", " ", " ".join(parts)).strip()
        if not useful_title(candidate) or "," in candidate and candidate.count(",") > 3:
            continue
        if normalized(candidate) in {normalized(j) for j in JOURNALS}:
            continue
        if re.match(r"^(research article|review article|article|open access|research|abstract)$", candidate, re.I):
            continue
        score = size * 2.5 + min(len(candidate.split()), 16) * .4
        # Word-authored preprints often use 12pt for both the title and abstract.
        # Prefer the upper, bold title and penalize paragraph-length candidates.
        score += (7 if bold else 0) + max(0, 8 * (1 - lines[index][0][1] / (page.rect.height * .5)))
        score -= max(0, len(candidate.split()) - 28) * .6
        if len(candidate.split()) < 4:
            score -= 6
        candidates.append((score, candidate, size))
    if candidates:
        _, title, size = max(candidates)
        if verified_metadata and not (normalized(title).startswith(normalized(metadata_title))
                                      and len(title.split()) <= len(metadata_title.split()) + 12):
            return metadata_title, True
        return title, size >= 13 and len(title.split()) >= 4
    if verified_metadata:
        return metadata_title, True
    return "", False


def read_pdf(path: Path) -> PDFText:
    with pymupdf.open(path) as doc:
        if doc.needs_pass:
            raise ValueError("PDF 已加密，需要先解除密码保护")
        if not doc.page_count:
            raise ValueError("PDF 中没有页面")
        # Methods/references can sit between the main text and a long supplement.
        # Include every page so unlabeled preprint abstracts remain classifiable.
        indices = range(doc.page_count)
        texts = {i: doc[i].get_text("text", sort=True)[:22000] for i in indices}
        native = {i: doc[i].get_text("text", sort=False)[:22000] for i in indices}
        if sum(c.isalpha() for c in "".join(texts.values())) < 100:
            raise ValueError("PDF 缺少可读文本（可能为扫描件），请先进行 OCR")
        # Recover rotated margin text independently of the main content stream.
        signals = []
        for block in doc[0].get_text("dict", flags=pymupdf.TEXTFLAGS_TEXT)["blocks"]:
            lines = ["".join(span["text"] for span in line.get("spans", [])) for line in block.get("lines", [])]
            signals.append("\n".join(lines))
        # Header links can carry the own-paper URL even when the printed label is
        # merely 'preprint'. Do not promote arXiv links in body citations.
        abstract_y = min((item.y0 for item in doc[0].search_for("Abstract")), default=doc[0].rect.height * .35)
        for link in doc[0].get_links():
            if link.get("uri") and link["from"].y1 <= abstract_y:
                signals.append(link["uri"])
        native[0] += "\n" + "\n".join(signals)
        title, confident = title_from_page(doc[0], doc.metadata.get("title", ""))
        authors = metadata_authors(doc.metadata.get("author", ""), texts[0] + "\n" + native[0])
        printed_authors = prepare_authors(header_authors(doc[0], title), [])
        if printed_authors and len(printed_authors) > len(authors):
            authors = printed_authors
        return PDFText("\n\n".join(texts.values()), "\n".join(texts.get(i, "") for i in range(2)),
                       texts[0], title, confident, authors, doc.page_count,
                       "\n\n".join(native.values()), "\n".join(native.get(i, "") for i in range(2)), native[0])


def classify_paper(pdf: PDFText) -> tuple[bool | None, str]:
    front = pdf.front
    lowered = pdf.text.casefold()
    title_and_header = (pdf.title + "\n" + front[:600]).casefold()
    excluded = r"\b(user manual|user guide|installation guide|curriculum vitae|invoice|doctoral thesis|master'?s thesis|supporting information|supplementary information|supplementary material)\b|用户手册|个人简历|学位论文"
    if re.search(excluded, title_and_header):
        return False, "识别为说明文档、学位论文或独立补充材料"
    if detect_arxiv(pdf.first, pdf.native_first):
        return True, "检测到本篇论文的 arXiv 标记与编号"
    abstract = bool(re.search(r"\babstract\b|摘\s*要", front, re.I))
    if abstract and pdf.title and detect_preprint(pdf.first, pdf.native_first):
        return True, "检测到预印本来源标注、论文标题和摘要"
    references = bool(re.search(r"\breferences\b|\bbibliography\b|参考文献", pdf.text, re.I))
    sections = sum(bool(re.search(pattern, lowered)) for pattern in (
        r"\bintroduction\b|(?m:^\s*main\s*$)|引言", r"\bmethods?\b|实验方法", r"\bresults?\b|实验结果", r"\bdiscussion\b|讨论"))
    if abstract and (references or sections >= 2):
        return True, "检测到摘要及学术论文结构（含学术综述）"
    if references and sections >= 2 and pdf.title:
        return True, "检测到正文研究章节及参考文献"
    if abstract or DOI.search(front) or references:
        return None, "存在学术文献特征，但证据不足，需人工核对"
    return False, "未检测到摘要、参考文献等学术论文特征"


def journal_abbreviation(journal: str, short_title: str = "") -> str:
    if short_title.strip():
        return clean_markup(short_title)
    for full, abbreviated in JOURNALS.items():
        if normalized(full) == normalized(journal):
            return abbreviated
    return ""


def detect_journal(front: str) -> tuple[str, str]:
    # Longest first prevents Nature from matching Nature Communications.
    header = re.split(r"\babstract\b|摘\s*要", front[:5000], maxsplit=1, flags=re.I)[0]
    for full in sorted(JOURNALS, key=len, reverse=True):
        # Generic words such as 'Nature', 'Science' and 'Cell' in prose are not mastheads.
        pattern = (r"(?im)^\s*" + re.escape(full) +
                   r"(?=\s*(?:$|[|•·\d,;]|vol(?:ume)?\b|https?://))")
        if re.search(pattern, header):
            return full, JOURNALS[full]
    return "", ""


def family_from_name(name: str) -> str:
    name = re.sub(r"^(?:Dr\.?|Prof\.?)\s+", "", name.strip(), flags=re.I)
    name = re.sub(r"[\d*†‡]+", "", name).strip(" ,.;:")
    if re.fullmatch(r"[\u4e00-\u9fff]{2,5}", name):
        for compound in ("欧阳", "司马", "上官", "诸葛", "东方", "夏侯", "司徒", "皇甫"):
            if name.startswith(compound):
                return compound
        return name[0]
    words = name.split()
    if len(words) < 2:
        return ""
    start = len(words) - 1
    while start > 0 and words[start - 1].casefold() in {"van", "von", "de", "del", "der", "den", "da", "di", "la", "le"}:
        start -= 1
    return " ".join(words[start:])


def metadata_authors(value: str, front: str) -> list[dict]:
    """Keep a complete verified author sequence, so a partial list cannot change its last author."""
    if not value.strip() or re.search(r"\bet\s+al\b|等作者", value, re.I):
        return []
    names = re.split(r"[;；、]|\band\b", value)
    if len(names) == 1 and re.search(r"[,，]", names[0]):
        comma_names = re.split(r"[,，]", names[0])
        if all(len(name.split()) >= 2 or re.fullmatch(r"[\u4e00-\u9fff]{2,5}", name.strip()) for name in comma_names):
            names = comma_names
    authors = []
    for name in names:
        name = name.strip()
        if not name:
            continue
        if name.count(",") == 1:
            family, given = (part.strip() for part in name.split(","))
            full_name = given + " " + family
        else:
            family = family_from_name(name)
            if re.fullmatch(r"[\u4e00-\u9fff]{2,6}", name) and family:
                given = name[len(family):]
            else:
                given = name[:-len(family)].strip() if family else ""
            full_name = name
        if not family or normalized(full_name) not in normalized(front):
            return []
        authors.append({"given": given, "family": family})
    return authors


def last_author_family(authors: list[dict]) -> str:
    if not authors or not isinstance(authors[-1], dict):
        return ""
    # A consortium/truncated final entry does not justify using an earlier named author.
    family = authors[-1].get("family", "")
    if not isinstance(family, str) or re.search(r"\bet\s+al\b", family, re.I):
        return ""
    return family.strip()


def complete_name(value: str) -> str:
    """Preserve a source's full name; never expand unknown given-name initials."""
    if not isinstance(value, str):
        return ""
    value = re.sub(r"\s+", " ", value).strip(" ,;.")
    value = re.sub(r"^(?:Dr\.?|Prof\.?)\s+", "", value, flags=re.I)
    if not value or len(value) > 300 or re.search(r"@|\d|\bet\s+al\b", value, re.I):
        return ""
    if re.fullmatch(r"[\u4e00-\u9fff]{2,6}", value):
        return value
    words = value.split()
    family = family_from_name(value)
    given_words = value[:-len(family)].split() if family else []
    if len(words) < 2 or not any(len(re.sub(r"[\W\d_]", "", word)) > 1 for word in given_words):
        return ""
    return value


def author_full_name(author: dict) -> str:
    if not isinstance(author, dict):
        return ""
    explicit = complete_name(author.get("full_name", ""))
    if explicit:
        return explicit
    given, family = author.get("given", ""), author.get("family", "")
    if not isinstance(given, str) or not isinstance(family, str) or not given.strip() or not family.strip():
        return ""
    if re.fullmatch(r"[\u4e00-\u9fff]+", family + given):
        return complete_name(family + given)
    return complete_name(given.strip() + " " + family.strip())


def last_author_name(authors: list[dict]) -> str:
    return author_full_name(authors[-1]) if authors else ""


def prepare_authors(authors: list[dict], local_authors: list[dict]) -> list[dict]:
    """Retain primary-source author order and resolve full names only from verified local text."""
    prepared = []
    for author in authors:
        if not isinstance(author, dict):
            prepared.append({})
            continue
        author = dict(author)
        full_name = author_full_name(author)
        if not full_name and author.get("family") and author.get("given"):
            initials = "".join(word[0].casefold() for word in author["given"].split() if word)
            matches = [a for a in local_authors if a.get("family", "").casefold() == author["family"].casefold()
                       and "".join(w[0].casefold() for w in a.get("given", "").split() if w).startswith(initials)
                       and author_full_name(a)]
            if len(matches) == 1:
                full_name = author_full_name(matches[0])
        if full_name:
            author["full_name"] = full_name
            if not author.get("family"):
                family = family_from_name(full_name)
                author["family"] = family
                author["given"] = full_name[len(family):] if re.fullmatch(r"[\u4e00-\u9fff]+", full_name) else full_name[:-len(family)].strip()
        prepared.append(author)
    return prepared


def name_in_text(name: str, text: str) -> bool:
    # Do not accept short surnames inside ordinary words (He in 'The', Li in 'clinical').
    return bool(name and re.search(r"(?<!\w)" + re.escape(name) + r"(?![^\W\d_])", text, re.I))


def correspondence_context(text: str, marker) -> str:
    prefix = text[max(0, marker.start() - 4):marker.start()]
    symbol = re.search(r"([*†‡])[ \t]*$", prefix)
    snippet = (symbol.group(1) + " " if symbol else "") + text[marker.start():marker.end() + 420]
    snippet = re.split(r"\n\s*\n", snippet, maxsplit=1)[0]
    # Author notes can wrap, but the following body paragraph must not supply names.
    snippet = re.split(r"\n\s*(?:Abstract\b|Introduction\b|Methods\b|Results\b|References\b|The\s|We\s|This\s|These\s|Our\s|In this\s)", snippet, maxsplit=1, flags=re.I)[0]
    snippet = re.split(r"(?<=[a-z)])\.\s+(?=(?:The|We|This|These|Our|In this)\s)", snippet, maxsplit=1)[0]
    lines = snippet.splitlines()
    kept = lines[:1]
    for line in lines[1:6]:
        value = line.strip()
        contact_line = re.search(r"@|e[- ]?mail|addressed\s+to|correspond|^to\s+[A-Z]", value, re.I)
        names_only = re.fullmatch(r"(?:[A-ZÀ-ÖØ-Þ][\w’'\-.]*(?:[ \t]+|$)|and[ \t]+|[,&;][ \t]*){1,18}|[\u4e00-\u9fff、，：]{2,20}", value)
        if not value or not (contact_line or names_only):
            break
        kept.append(line)
    return "\n".join(kept)


def correspondence(text: str, front: str, authors: list[dict]) -> tuple[list[str], list[str]]:
    families, evidence = [], []
    for marker in CORR.finditer(text):
        # A footnote usually follows the marker. Do not include the entire author list before it.
        snippet = correspondence_context(text, marker)
        matched = []
        for author in authors:
            family = author.get("family", "").strip()
            given = author.get("given", "").strip()
            if not family:
                continue
            family_pattern = r"(?<!\w)" + re.escape(family) + r"(?![^\W\d_])"
            full = re.search(family_pattern, snippet, re.I)
            initials = "".join(word[0] for word in given.split() if word)
            # Full surname near explicit correspondence wording, or unique author initials.
            same_initials = sum("".join(w[0] for w in a.get("given", "").split()) == initials and
                                a.get("family", "")[:1] == family[:1] for a in authors)
            addressed_initial = re.search(r"addressed to\s+(.{1,90})", snippet, re.I)
            code = initials + family[:1]
            code_match = addressed_initial and same_initials == 1 and re.search(
                r"(?<![A-Za-z])" + r"[.\s]*".join(re.escape(i) for i in code) + r"\.?(?![A-Za-z])",
                addressed_initial.group(1), re.I)
            full_name = author_full_name(author)
            if (full or code_match) and full_name:
                matched.append(full_name)
                continue
            # Asterisk mapping is valid only with explicit '* Corresponding author' footnote.
            star_note = re.match(r"\s*([*†‡])\s*corresponding", snippet, re.I)
            if full_name and star_note and re.search(family_pattern + r"[ \t,\dᵃᵇᶜ]*" + re.escape(star_note.group(1)), front[:6000], re.I):
                matched.append(full_name)
        if not matched:
            # Direct full names after a correspondence label; no inference from bare emails.
            direct = re.search(r"(?:corresponding\s+authors?|correspondence(?:\s+to)?|通讯作者|通信作者|联系作者)\s*[:：,]\s*([^\n;()@]{2,100})", snippet, re.I)
            if not direct:
                direct = re.search(r"correspondence\s+to\s+([^\n;()@]{3,80})", snippet, re.I)
            if direct:
                raw_name = direct.group(1).strip(" .,")
                for name in re.split(r"\s+and\s+|\s*&\s*|[、,]", raw_name):
                    if re.search(r"\b(university|department|email|address|should|please|author|requests|institute)\b", name, re.I):
                        continue
                    if re.fullmatch(r"(?:[A-ZÀ-ÖØ-Þ][\w’'\-.]*\s+){1,4}[A-ZÀ-ÖØ-Þ][\w’'\-]*|[\u4e00-\u9fff]{2,5}", name):
                        full_name = complete_name(name)
                        if full_name:
                            matched.append(full_name)
            if not matched and snippet.count("@") == 1:
                email_name = re.search(r"e[- ]?mail\s+address\s*:\s*[^()]*?@[^()]*?\(([^)\n]+)\)", snippet, re.I)
                if email_name:
                    name = re.sub(r"([A-Z]\.)\s*(?=[A-Z])", r"\1 ", email_name.group(1).strip())
                    if re.fullmatch(r"(?:[A-ZÀ-ÖØ-Þ][\w’'\-.]*\s+){1,4}[A-ZÀ-ÖØ-Þ][\w’'\-]*", name):
                        full_name = complete_name(name)
                        if full_name:
                            matched.append(full_name)
        if matched:
            families.extend(matched)
            evidence.append(snippet.strip())
    return list(dict.fromkeys(families)), list(dict.fromkeys(evidence))


def analyze_pdf(path: Path, root: Path, use_metadata: bool = True, ai: dict | None = None) -> dict:
    result = {"id": str(path.relative_to(root)), "source": str(path), "relative_path": str(path.relative_to(root)),
              "filename": path.name, "status": "review", "is_research_paper": None,
              "confidence": "low", "corresponding_author": "", "journal_abbreviation": "",
              "title": "", "proposed_filename": "", "reason": "", "evidence": [], "warnings": [],
              "fingerprint": None, "journal": "", "doi": "", "rename_eligible": False,
              "author_source": "", "last_author": "", "detected_corresponding_author": "", "arxiv_id": "",
              "publication_date": "", "date_source": "", "authors": [], "preprint_source": ""}
    try:
        result["fingerprint"] = file_fingerprint(path)
        pdf = read_pdf(path)
        arxiv_id = detect_arxiv(pdf.first, pdf.native_first)
        source = "arxiv" if arxiv_id else detect_preprint(pdf.first, pdf.native_first)
        hinted_record, hinted_warnings = {}, []
        hint = filename_arxiv_id(path.name) if not arxiv_id and use_metadata else ""
        if hint:
            hinted_record, hinted_warnings = arxiv_metadata(hint, pdf.title, pdf.front)
            if hinted_record:
                arxiv_id, source = hint, "arxiv"
                result["evidence"].append("下载文件名中的 arXiv 编号已与首页标题联网核对")
        result["arxiv_id"] = arxiv_id
        result["preprint_source"] = source
        result["publication_date"], result["date_source"] = publication_date({}, pdf.front, arxiv_id)
        result["authors"] = [author_full_name(author) or author.get("family", "") for author in pdf.authors]
        is_paper, reason = classify_paper(pdf)
        # An exact arXiv download name is useful even when the author-uploaded
        # manuscript has no arXiv stamp. Require independent research structure.
        local_hint = filename_arxiv_id(path.name)
        if not arxiv_id and local_hint and is_paper is True and pdf.title and not detect_journal(pdf.first)[0]:
            arxiv_id, source = local_hint, "arxiv"
            result.update(arxiv_id=arxiv_id, preprint_source=source)
            result["evidence"].append("arXiv 下载文件名编号与全文论文结构相互印证：" + arxiv_id)
        if hinted_record and is_paper is not False:
            is_paper, reason = True, "arXiv 题录与本篇 PDF 首页标题匹配"
        result.update(is_research_paper=is_paper, reason=reason, title=pdf.title, page_count=pdf.pages)
        if is_paper is False:
            result["status"] = "skipped"
            return result
        result["rename_eligible"] = True
        dois = list(dict.fromkeys(m.group(0).rstrip(".,;:)") for m in DOI.finditer(pdf.first)))
        if arxiv_id:
            record, warnings = (hinted_record, hinted_warnings) if hint == arxiv_id else (arxiv_metadata(arxiv_id, pdf.title, pdf.front) if use_metadata else ({}, []))
            result["evidence"].append("检测到本篇 arXiv 编号：" + arxiv_id + "；期刊字段固定为 arxiv")
        else:
            record, warnings = crossref_metadata(dois, pdf.title, pdf.front) if use_metadata else ({}, [])
        result["warnings"].extend(warnings)
        result["warnings"].extend(hinted_warnings if hint != arxiv_id else [])
        if not source and record.get("type") == "posted-content":
            source = result["preprint_source"] = "preprint"
        if source:
            result["evidence"].append("本篇预印本来源：" + source)
        result["publication_date"], result["date_source"] = publication_date(record, pdf.front, arxiv_id)
        if result["date_source"]:
            result["evidence"].append("发表时间来源：" + result["date_source"])
        title_confirmed = pdf.title_confident
        if record:
            result["title"] = clean_markup((record.get("title") or [pdf.title])[0])
            title_confirmed = True
            result["doi"] = record.get("DOI", "")
            result["evidence"].append("arXiv 编号与 PDF 标题匹配" if arxiv_id else "Crossref DOI 与 PDF 标题匹配：" + result["doi"])
        journal, abbreviation = detect_journal(pdf.first)
        if record.get("container-title"):
            journal = clean_markup(record["container-title"][0])
            abbreviation = journal_abbreviation(journal, (record.get("short-container-title") or [""])[0])
        if source:
            journal = abbreviation = source
        result.update(journal=journal, journal_abbreviation=abbreviation)
        authors = prepare_authors(record.get("author") or pdf.authors, pdf.authors)
        result["authors"] = [author_full_name(author) or author.get("full_name") or
                             " ".join(filter(None, (author.get("given"), author.get("family")))) for author in authors]
        result["last_author"] = last_author_name(authors)
        # Native content order often keeps sidebars intact when coordinate sorting mixes columns.
        families, evidence = correspondence(pdf.text + "\n\n" + pdf.native_text,
                                             pdf.front + "\n\n" + pdf.native_front, authors)
        result["evidence"].extend(evidence)
        if ai and ai.get("enabled"):
            try:
                extra = ai_metadata(pdf.text, ai)
                if extra.get("is_research_paper") is False:
                    result.update(status="skipped", is_research_paper=False, rename_eligible=False,
                                  reason="AI 识别为非论文，已取消重命名资格")
                    return result
                elif extra.get("is_research_paper") is True and is_paper is None:
                    is_paper = True
                    result["warnings"].append("论文类型由 AI 辅助判断，需核对")
                if extra.get("title") and title_in_text(extra["title"], pdf.front):
                    result["title"] = extra["title"].strip()
                if not abbreviation:
                    ai_journal = extra.get("journal", "")
                    if ai_journal and normalized(ai_journal) in normalized(pdf.front):
                        result["journal"] = ai_journal
                        result["journal_abbreviation"] = journal_abbreviation(ai_journal)
                        if not result["journal_abbreviation"] and extra.get("journal_abbreviation"):
                            result["journal_abbreviation"] = extra["journal_abbreviation"].strip()
                            result["warnings"].append("期刊简称来自 AI，尚未核实是否为标准简称")
                for author in extra.get("corresponding_authors", []):
                    family, quote = author.get("family", "").strip(), author.get("evidence", "").strip()
                    full_name = author.get("full_name", "").strip()
                    if family and quote and normalized(quote) in normalized(pdf.text) and CORR.search(quote):
                        # Names must appear in the quoted evidence; otherwise require manual checking.
                        chinese_name = bool(re.fullmatch(r"[\u4e00-\u9fff]{2,5}", full_name) and full_name.startswith(family) and full_name in quote)
                        if ((name_in_text(family, quote) and full_name and name_in_text(full_name, quote)) or chinese_name) and complete_name(full_name):
                            families.append(complete_name(full_name))
                            result["evidence"].append(quote)
                        elif name_in_text(family, pdf.front):
                            result["warnings"].append("AI 发现通讯作者符号映射，请核对：" + family)
                result["warnings"].append("已使用 AI 辅助识别，请核对结果及证据")
            except Exception:
                # Do not echo provider messages, which may include submitted text or credentials.
                result["warnings"].append("AI 请求未成功或返回格式不合要求，已保留本地识别结果；请检查地址、模型和 API Key")
        families = list(dict.fromkeys(families))
        # Keep correspondence evidence for review; filename author always follows author order.
        result["detected_corresponding_author"] = "+".join(families)
        result["corresponding_author"] = result["last_author"]
        result["author_source"] = "last_author" if result["last_author"] else ""
        result["is_research_paper"] = is_paper
        if result["last_author"]:
            result["evidence"].append("命名使用作者列表中的末位作者全名：" + result["last_author"])
        else:
            result["warnings"].append("未能可靠识别末位作者全名，作者已留空；可稍后核对，也可不填写作者直接勾选重命名")
        if not result["journal_abbreviation"]:
            result["warnings"].append("期刊简称缺失或无法确认（预印本也可能没有期刊），请手动填写")
        if not title_confirmed:
            result["warnings"].append("论文标题由版面推测，需核对")
        if all(result[key] for key in ("journal_abbreviation", "title")):
            result["proposed_filename"] = build_filename(result["corresponding_author"], result["journal_abbreviation"], result["title"], result["publication_date"])
            if is_paper is True and result["last_author"]:
                result.update(status="ready", confidence="high" if title_confirmed and not result["warnings"] else "medium")
            else:
                result["confidence"] = "medium"
        if not result["publication_date"]:
            result["warnings"].append("无法确认发表时间，文件名前缀标为「时间未知」，可手动填写；不使用文件创建或修改时间")
        return result
    except Exception as exc:
        result.update(status="error", rename_eligible=False,
                      reason=str(exc)[:400] if isinstance(exc, ValueError) else "PDF 读取失败，文件可能已损坏或无读取权限")
        return result
