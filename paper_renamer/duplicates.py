"""Full-document duplicate verification and recoverable removal of extra copies."""
from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
import uuid
from collections import defaultdict
from pathlib import Path

import pymupdf

from .metadata import normalized
from .rename import _within_root, file_fingerprint

HISTORY = ".paper-duplicates"


def _text_digest(path: Path) -> str:
    digest = hashlib.sha256()
    length = 0
    with pymupdf.open(path) as document:
        if document.needs_pass:
            return ""
        for page in document:
            text = unicodedata.normalize("NFKC", page.get_text("text", sort=True))
            text = re.sub(r"\s+", "", text)
            length += len(text)
            digest.update(text.encode("utf-8"))
    return digest.hexdigest() if length >= 300 else ""


def _visual_digest(path: Path) -> str:
    """Also check figures, equations and annotations before removing a text match."""
    digest = hashlib.sha256()
    with pymupdf.open(path) as document:
        # A sticky-note icon can render identically while its stored note differs.
        # Preserve annotations, forms, bookmarks, attachments and signed copies.
        digest.update(json.dumps(document.get_toc(simple=False), sort_keys=True, default=str).encode())
        for name in sorted(document.embfile_names()):
            digest.update(name.encode("utf-8"))
            digest.update(document.embfile_get(name))
        if document.get_sigflags() > 0:
            digest.update(file_fingerprint(path)["sha256"].encode())
        digest.update(str(document.page_count).encode())
        for page in document:
            for annotation in page.annots() or ():
                digest.update(json.dumps({"type": annotation.type, "info": annotation.info,
                                          "rect": tuple(annotation.rect), "vertices": annotation.vertices},
                                         sort_keys=True, default=str).encode())
            for widget in page.widgets() or ():
                digest.update(json.dumps({"name": widget.field_name, "value": widget.field_value,
                                          "type": widget.field_type, "flags": widget.field_flags},
                                         sort_keys=True, default=str).encode())
            pixmap = page.get_pixmap(matrix=pymupdf.Matrix(1, 1), alpha=False, annots=True)
            digest.update(f"{pixmap.width},{pixmap.height},{pixmap.n}:".encode())
            digest.update(pixmap.samples)
    return digest.hexdigest()


def inspect_duplicates(root: Path, records: list[dict], progress=lambda *_: None) -> dict:
    """Every removal needs byte equality or full text AND page-image equality.

    Identifiers and title/author agreement only nominate candidates: revisions or
    published/preprint versions are never removed on bibliographic evidence alone.
    """
    exact, text_groups, identities = defaultdict(list), defaultdict(list), defaultdict(list)
    checked, failures = {}, []
    for number, record in enumerate(records, 1):
        path = Path(record["source"])
        progress(number - 1, str(path.relative_to(root)))
        if record.get("is_research_paper") is False or record.get("status") == "error":
            continue
        try:
            _within_root(root, str(path), require_file=True)
            before = file_fingerprint(path)
            expected = record.get("fingerprint")
            if expected and (before["sha256"] != expected["sha256"] or before["size"] != expected["size"]):
                raise ValueError("索引后文件已变化，请更新索引")
            checked[str(path)] = {"record": record, "fingerprint": before}
            exact[before["sha256"]].append(str(path))
            # Corrupt files may still be identical byte-for-byte. Do not interpret
            # their text or use failed metadata for bibliographic matching.
            if record.get("status") != "error":
                text = _text_digest(path)
                if file_fingerprint(path) != before:
                    raise ValueError("全文读取期间文件已变化")
                if text:
                    text_groups[text].append(str(path))
                arxiv = re.sub(r"v\d+$", "", record.get("arxiv_id", "").lower())
                if arxiv:
                    identities["arxiv:" + arxiv].append(str(path))
                if record.get("doi"):
                    identities["doi:" + record["doi"].casefold()].append(str(path))
                title = normalized(record.get("title", ""))
                authors = sorted(normalized(author) for author in record.get("authors", []) if author)
                if len(title) >= 20 and authors:
                    identities["title:" + title + ":" + "|".join(authors)].append(str(path))
        except Exception as exc:
            checked.pop(str(path), None)
            failures.append({"source": str(path), "reason": str(exc)[:200]})

    parent = {path: path for path in checked}
    reasons = {}

    def find(path):
        while parent[path] != path:
            parent[path] = parent[parent[path]]
            path = parent[path]
        return path

    def merge(paths, reason):
        paths = sorted(path for path in paths if path in checked)
        if len(paths) > 1:
            for path in paths[1:]:
                parent[find(path)] = find(paths[0])
            reasons[find(paths[0])] = reason

    for paths in exact.values():
        merge(paths, "文件 SHA-256 完全相同")
    for paths in text_groups.values():
        paths = [path for path in paths if path in checked]
        if len({find(path) for path in paths}) < 2:
            continue
        visual = defaultdict(list)
        for path in paths:
            try:
                visual[_visual_digest(Path(path))].append(path)
            except Exception:
                failures.append({"source": path, "reason": "图表或页面核对失败，已保留"})
        for matches in visual.values():
            merge(matches, "全部页面文本及页面图像一致（含图表、公式与批注）")

    groups = defaultdict(list)
    for path in checked:
        groups[find(path)].append(path)
    confirmed, suspected, seen = [], [], set()
    for representative, paths in groups.items():
        if len(paths) < 2:
            continue
        # Deterministic: retain the shallowest path, then the shorter filename.
        paths.sort(key=lambda value: (len(Path(value).relative_to(root).parts), len(Path(value).name), value))
        keep = paths[0]
        for extra in paths[1:]:
            confirmed.append({"keep": keep, "duplicate": extra,
                              "title": checked[keep]["record"].get("title") or Path(keep).stem,
                              "reason": reasons.get(representative, "文件内容一致"),
                              "keep_fingerprint": checked[keep]["fingerprint"],
                              "fingerprint": checked[extra]["fingerprint"]})
    for paths in list(identities.values()) + list(text_groups.values()):
        representatives = sorted({find(path) for path in paths if path in checked})
        key = tuple(representatives)
        if len(representatives) > 1 and key not in seen:
            seen.add(key)
            suspected.append({"sources": [sorted(groups[value])[0] for value in representatives],
                              "reason": "编号或标题作者相同，但全文或页面存在差异，可能为修订版，已保留"})
    progress(len(records), "")
    return {"checked": len(checked), "confirmed": confirmed, "suspected": suspected, "failures": failures}


def _write_log(path: Path, value: dict):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def remove_duplicates(root: Path, report: dict) -> dict:
    """Move verified extra copies out of the library, journaling before each move."""
    result = {"moved": [], "errors": [], "batch_id": ""}
    if not report["confirmed"]:
        return result
    batch = uuid.uuid4().hex
    history = _within_root(root, HISTORY + "/" + batch)
    history.mkdir(parents=True)
    journal = {"batch_id": batch, "entries": []}
    log = history / "manifest.json"
    result["batch_id"] = batch
    for item in report["confirmed"]:
        try:
            source = _within_root(root, item["duplicate"], require_file=True)
            keep = _within_root(root, item["keep"], require_file=True)
            if file_fingerprint(source) != item["fingerprint"] or file_fingerprint(keep) != item["keep_fingerprint"]:
                raise ValueError("核对后文件已变化，已保留，请重新索引")
            target = history / (uuid.uuid4().hex + ".pdf")
            entry = {"source": str(source), "backup": str(target), "keep": str(keep),
                     "fingerprint": item["fingerprint"], "reason": item["reason"], "state": "pending"}
            journal["entries"].append(entry)
            _write_log(log, journal)
            # Exclusive hard-link creation prevents overwrites; backup lives on
            # the same filesystem. Unsupported filesystems leave originals alone.
            os.link(source, target, follow_symlinks=False)
            if file_fingerprint(target) != item["fingerprint"]:
                target.unlink()
                raise ValueError("文件在移除时发生变化，已保留")
            source.unlink()
            entry["state"] = "moved"
            result["moved"].append({"source": str(source), "keep": str(keep), "reason": item["reason"]})
            _write_log(log, journal)
        except (OSError, ValueError) as exc:
            result["errors"].append({"source": item["duplicate"], "reason": str(exc)[:200]})
    return result


def restore_duplicates(root: Path, batch: str) -> dict:
    if not isinstance(batch, str) or not re.fullmatch(r"[0-9a-f]{32}", batch):
        raise ValueError("去重批次编号不正确")
    log = _within_root(root, HISTORY + "/" + batch + "/manifest.json", require_file=True)
    journal = json.loads(log.read_text(encoding="utf-8"))
    restored, errors = [], []
    for entry in journal["entries"]:
        if entry["state"] == "restored":
            continue
        try:
            backup = _within_root(root, entry["backup"], require_file=True)
            backup.relative_to(log.parent)
            source = _within_root(root, entry["source"])
            actual = file_fingerprint(backup)
            if actual != entry["fingerprint"]:
                raise ValueError("备份已修改，已停止恢复此文件")
            source.parent.mkdir(parents=True, exist_ok=True)
            os.link(backup, source, follow_symlinks=False)
            # Keep the backup until the completed restore is durable in the log.
            entry["state"] = "restored"
            _write_log(log, journal)
            restored.append(str(source))
        except (OSError, ValueError) as exc:
            errors.append({"source": entry["source"], "reason": str(exc)[:200]})
    return {"restored": restored, "errors": errors}
