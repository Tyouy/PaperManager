"""Persistent, incremental PDF catalogue with paged metadata search."""
from __future__ import annotations

import json
import sqlite3
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pymupdf

from .dates import date_bounds
from .extract import analyze_pdf, author_full_name
from .header_authors import header_authors

INDEX_VERSION = 4


def search_text(value: str) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).casefold()


class Library:
    def __init__(self, database: Path):
        self.database = database

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.database, timeout=20)
        connection.row_factory = sqlite3.Row
        connection.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS folders (
                root TEXT PRIMARY KEY, updated_at TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS papers (
                root TEXT NOT NULL, source TEXT NOT NULL, size INTEGER NOT NULL,
                mtime_ns TEXT NOT NULL, metadata INTEGER NOT NULL, version INTEGER NOT NULL,
                seen TEXT NOT NULL, date_min TEXT NOT NULL, date_max TEXT NOT NULL,
                author TEXT NOT NULL, title TEXT NOT NULL, record TEXT NOT NULL,
                PRIMARY KEY(root, source)
            );
            CREATE INDEX IF NOT EXISTS papers_date ON papers(root, date_min);
            CREATE TABLE IF NOT EXISTS duplicate_reports (
                root TEXT PRIMARY KEY, report TEXT NOT NULL
            );
        """)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def index(self, root: Path, paths: list[Path], metadata: bool, progress):
        token = uuid.uuid4().hex
        reused = 0
        with self.connect() as connection:
            connection.execute("INSERT OR IGNORE INTO folders(root) VALUES (?)", (str(root),))
            connection.commit()
            for number, path in enumerate(paths, 1):
                progress(number - 1, str(path.relative_to(root)), reused)
                stat = path.stat()
                cached = connection.execute("SELECT * FROM papers WHERE root=? AND source=?",
                                            (str(root), str(path))).fetchone()
                if (cached and cached["size"] == stat.st_size and cached["mtime_ns"] == str(stat.st_mtime_ns)
                        and cached["version"] == INDEX_VERSION and (cached["metadata"] or not metadata)
                        and json.loads(cached["record"]).get("status") != "error"):
                    connection.execute("UPDATE papers SET seen=? WHERE root=? AND source=?",
                                       (token, str(root), str(path)))
                    reused += 1
                else:
                    record = analyze_pdf(path, root, metadata, {})
                    if not record.get("authors") and record.get("status") != "error":
                        # Search covers the full author list even if the PDF has no author metadata.
                        try:
                            with pymupdf.open(path) as document:
                                record["authors"] = [author_full_name(author) or author.get("full_name", "")
                                                     for author in header_authors(document[0], record.get("title", ""))]
                        except (OSError, RuntimeError, ValueError):
                            pass  # Metadata already records unreadable files; keep other results searchable.
                    published = record.get("publication_date", "")
                    date_min, date_max = date_bounds(published) if published else ("", "")
                    author = " ".join(record.get("authors", []) + [record.get("corresponding_author", "")])
                    metadata_ok = metadata and not any(
                        "元数据" in warning and ("不可用" in warning or "未成功" in warning)
                        for warning in record.get("warnings", []))
                    connection.execute("INSERT OR REPLACE INTO papers VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
                        str(root), str(path), stat.st_size, str(stat.st_mtime_ns), int(metadata_ok), INDEX_VERSION,
                        token, date_min, date_max, search_text(author), search_text(record.get("title") or path.stem),
                        json.dumps(record, ensure_ascii=False)))
                # Release write locks frequently; do not hold a transaction during network calls.
                connection.commit()
                progress(number, "", reused)
            connection.execute("DELETE FROM papers WHERE root=? AND seen<>?", (str(root), token))
            connection.execute("UPDATE folders SET updated_at=? WHERE root=?",
                               (datetime.now(timezone.utc).isoformat(), str(root)))
        return reused

    def records(self, root: Path) -> list[dict]:
        with self.connect() as connection:
            return [json.loads(row[0]) for row in connection.execute(
                "SELECT record FROM papers WHERE root=? ORDER BY source", (str(root),))]

    def save_duplicate_report(self, root: Path, report: dict):
        with self.connect() as connection:
            connection.execute("INSERT OR REPLACE INTO duplicate_reports VALUES (?,?)",
                               (str(root), json.dumps(report, ensure_ascii=False)))
            for item in report.get("removal", {}).get("moved", []):
                connection.execute("DELETE FROM papers WHERE source=?", (item["source"],))

    def duplicate_report(self, root: Path) -> dict:
        with self.connect() as connection:
            row = connection.execute("SELECT report FROM duplicate_reports WHERE root=?", (str(root),)).fetchone()
        return json.loads(row[0]) if row else {}

    def search(self, root: Path, query: dict) -> dict:
        clauses, params = ["root=?"], [str(root)]
        for key in ("author", "title"):
            value = query.get(key, "")
            if not isinstance(value, str) or len(value) > 500:
                raise ValueError("检索条件格式不正确（最多 500 字）")
            for term in search_text(value).split():
                clauses.append(f"instr({key}, ?) > 0")
                params.append(term)
        lower, upper = query.get("date_from", ""), query.get("date_to", "")
        lower = date_bounds(lower)[0] if lower else ""
        upper = date_bounds(upper)[1] if upper else ""
        if lower and upper and lower > upper:
            raise ValueError("开始时间不能晚于结束时间")
        if lower:
            clauses.append("date_max >= ? AND date_min <> ''")
            params.append(lower)
        if upper:
            clauses.append("date_min <= ? AND date_min <> ''")
            params.append(upper)
        sort = query.get("sort", "date_desc")
        ordering = {"date_desc": "(date_min=''), date_min DESC, title, source",
                    "date_asc": "(date_min=''), date_min ASC, title, source",
                    "author": "(author=''), author, title, source",
                    "title": "(title=''), title, source"}.get(sort)
        if ordering is None:
            raise ValueError("不支持的排序方式")
        try:
            page = max(1, int(query.get("page", 1)))
            page_size = min(100, max(1, int(query.get("page_size", 50))))
        except (ValueError, TypeError, OverflowError):
            raise ValueError("分页参数格式不正确")
        where = " AND ".join(clauses)
        with self.connect() as connection:
            folder = connection.execute("SELECT updated_at FROM folders WHERE root=?", (str(root),)).fetchone()
            total = connection.execute("SELECT COUNT(*) FROM papers WHERE " + where, params).fetchone()[0]
            page = min(page, max(1, (total + page_size - 1) // page_size))
            records = connection.execute("SELECT record FROM papers WHERE " + where + " ORDER BY " + ordering
                                         + " LIMIT ? OFFSET ?", params + [page_size, (page - 1) * page_size]).fetchall()
            indexed_total = connection.execute("SELECT COUNT(*) FROM papers WHERE root=?", (str(root),)).fetchone()[0]
        results = []
        for row in records:
            record = json.loads(row["record"])
            results.append({key: record.get(key) for key in (
                "source", "relative_path", "filename", "publication_date", "date_source", "authors",
                "corresponding_author", "title", "journal_abbreviation", "status", "reason", "is_research_paper",
                "arxiv_id", "preprint_source")})
        return {"results": results, "total": total, "indexed_total": indexed_total,
                "page": page, "page_size": page_size, "indexed": bool(folder and folder[0]),
                "updated_at": folder[0] if folder else ""}

    def document(self, root: Path, source: str) -> Path:
        if not isinstance(source, str) or not source:
            raise ValueError("请选择文献")
        path = Path(source)
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("文献路径不正确")
        resolved = path.resolve(strict=True)
        try:
            resolved.relative_to(root)
        except ValueError:
            raise ValueError("文献不在所选文件夹内")
        if resolved != path or path.suffix.casefold() != ".pdf" or not path.is_file():
            raise ValueError("文献路径已变化，请更新索引")
        with self.connect() as connection:
            row = connection.execute("SELECT 1 FROM papers WHERE root=? AND source=?",
                                     (str(root), str(path))).fetchone()
        if not row:
            raise ValueError("文献不在当前索引中，请更新索引")
        return path
