"""Catalogue search and incremental updates use temporary files only."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from paper_renamer.library import Library


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.folder = self.root / "papers"
        self.folder.mkdir()
        self.library = Library(self.root / "index.sqlite3")
        self.records = {}

    def tearDown(self):
        self.temporary.cleanup()

    def paper(self, name, title, authors, published=""):
        path = self.folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"%PDF-test")
        self.records[path] = {"source": str(path), "filename": path.name,
                              "relative_path": str(path.relative_to(self.folder)), "title": title,
                              "authors": authors, "publication_date": published, "status": "ready",
                              "is_research_paper": True, "corresponding_author": authors[-1]}
        return path

    def index(self, metadata=False):
        with patch("paper_renamer.library.analyze_pdf", side_effect=lambda path, *_: self.records[path]) as analyze:
            reused = self.library.index(self.folder, sorted(self.records), metadata, lambda *_: None)
        return reused, analyze.call_count

    def test_combined_search_all_authors_unicode_partial_dates_and_restart(self):
        self.paper("nested/first.pdf", "Battery interfaces", ["Alice Smith", "欧阳明"], "2024-02")
        self.paper("second.PDF", "Battery models", ["Bob Jones"], "2023")
        self.paper("undated.pdf", "Battery theory", ["Alice Smith"])
        self.index()
        reopened = Library(self.library.database)
        result = reopened.search(self.folder, {"author": "欧阳明", "title": "BATTERY", "date_from": "2024-02-29", "date_to": "2024-02-29"})
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["results"][0]["relative_path"], "nested/first.pdf")
        self.assertEqual(reopened.search(self.folder, {"author": "SMITH Alice"})["total"], 2)
        self.assertEqual(reopened.search(self.folder, {"date_from": "2024-03"})["total"], 0)
        self.assertTrue(result["indexed"])

    def test_sort_pagination_unknown_dates_last_and_root_isolation(self):
        self.paper("a.pdf", "Zebra", ["Bob Jones"], "2020")
        self.paper("b.pdf", "Alpha", ["Alice Smith"], "2024-03-01")
        self.paper("c.pdf", "Beta", ["Zoe Smith"])
        self.index()
        for sort, expected in (("date_asc", ["Zebra", "Alpha", "Beta"]),
                               ("date_desc", ["Alpha", "Zebra", "Beta"]),
                               ("author", ["Alpha", "Zebra", "Beta"]),
                               ("title", ["Alpha", "Beta", "Zebra"])):
            result = self.library.search(self.folder, {"sort": sort})
            self.assertEqual([row["title"] for row in result["results"]], expected)
        result = self.library.search(self.folder, {"page": 2, "page_size": 2, "sort": "title"})
        self.assertEqual(result["total"], 3)
        self.assertEqual([row["title"] for row in result["results"]], ["Zebra"])
        self.assertEqual(self.library.search(self.root, {})["total"], 0)
        self.assertEqual(self.library.search(self.folder, {"title": "' OR 1=1 --"})["total"], 0)

    def test_cache_refreshes_changes_additions_removals_and_online_upgrade(self):
        first = self.paper("first.pdf", "First article", ["Alice Smith"], "2020")
        removed = self.paper("removed.pdf", "Old article", ["Bob Jones"], "2021")
        self.assertEqual(self.index(), (0, 2))
        self.assertEqual(self.index(), (2, 0))
        self.assertEqual(self.index(metadata=True), (0, 2))
        self.assertEqual(self.index(metadata=False), (2, 0))
        first.write_bytes(b"%PDF-changed")
        self.records[first]["title"] = "Updated article"
        removed.unlink()
        del self.records[removed]
        self.paper("new.pdf", "New article", ["Carol White"], "2025")
        self.assertEqual(self.index(), (0, 2))
        result = self.library.search(self.folder, {})
        self.assertEqual(result["total"], 2)
        self.assertEqual({row["title"] for row in result["results"]}, {"Updated article", "New article"})

    def test_bad_filters_are_rejected_and_empty_index_persists(self):
        self.library.index(self.folder, [], False, lambda *_: None)
        self.assertTrue(self.library.search(self.folder, {})["indexed"])
        for query in ({"date_from": "2024-02-30"}, {"date_from": "2025", "date_to": "2020"},
                      {"sort": "random"}, {"page": "no"}, {"author": []}):
            with self.subTest(query=query), self.assertRaises(ValueError):
                self.library.search(self.folder, query)

    def test_temporary_network_failures_retry_on_next_online_index(self):
        path = self.paper("first.pdf", "First article", ["Alice Smith"])
        self.records[path]["warnings"] = ["标题元数据查询暂时不可用，使用本地识别"]
        self.assertEqual(self.index(metadata=True), (0, 1))
        self.assertEqual(self.index(metadata=False), (1, 0))
        self.records[path]["warnings"] = []
        self.assertEqual(self.index(metadata=True), (0, 1))
        self.assertEqual(self.index(metadata=True), (1, 0))

    def test_open_requires_indexed_pdf_inside_selected_folder_without_symlinks(self):
        path = self.paper("article.pdf", "Example", ["Alice Smith"])
        self.index()
        self.assertEqual(self.library.document(self.folder, str(path)), path)
        unindexed = self.folder / "unindexed.pdf"
        unindexed.write_bytes(b"%PDF")
        outside = self.root / "outside.pdf"
        outside.write_bytes(b"%PDF")
        for source in (str(unindexed), str(outside), str(self.folder / ".." / "outside.pdf")):
            with self.assertRaises(ValueError):
                self.library.document(self.folder, source)
        path.unlink()
        path.symlink_to(outside)
        with self.assertRaises(ValueError):
            self.library.document(self.folder, str(path))
