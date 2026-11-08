"""Duplicate cleanup tests operate exclusively on temporary, generated PDFs."""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

import pymupdf

from paper_renamer.duplicates import inspect_duplicates, remove_duplicates, restore_duplicates
from paper_renamer.rename import file_fingerprint
from app import list_pdfs

TITLE = "A reproducible method for measuring nanoscale material properties"


class DuplicateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()

    def tearDown(self):
        self.temporary.cleanup()

    def paper(self, name, *, last_page="Original conclusions", figure=False, stamp="", metadata=""):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with pymupdf.open() as document:
            page = document.new_page()
            page.insert_text((50, 70), TITLE, fontsize=12)
            page.insert_textbox((50, 120, 540, 600), "Abstract\n" + "Research text with scientific detail. " * 35)
            if figure:
                page.draw_rect(pymupdf.Rect(50, 650, 130, 700), color=(1, 0, 0), fill=(1, 0, 0))
            for number in range(10):
                page = document.new_page()
                page.insert_text((50, 100), f"Page of results {number}" if number != 6 else last_page)
            document.set_metadata({"subject": metadata, "title": TITLE})
            document.save(path)
        return self.record(path, stamp)

    def record(self, path, stamp=""):
        return {"source": str(path), "title": TITLE, "authors": ["Alice Smith", "Bob Jones"],
                "arxiv_id": stamp, "status": "ready", "is_research_paper": True,
                "fingerprint": file_fingerprint(path)}

    def test_binary_copies_are_removed_with_audit_and_restored_without_overwrite(self):
        first = self.paper("article.pdf")
        copied = self.root / "nested" / "copy.pdf"
        copied.parent.mkdir()
        shutil.copyfile(first["source"], copied)
        report = inspect_duplicates(self.root, [first, self.record(copied)])
        self.assertEqual(len(report["confirmed"]), 1)
        self.assertIn("SHA-256", report["confirmed"][0]["reason"])
        original_bytes = copied.read_bytes()
        removal = remove_duplicates(self.root, report)
        self.assertEqual(len(removal["moved"]), 1)
        self.assertFalse(copied.exists())
        self.assertEqual(list_pdfs(self.root, True), [Path(first["source"])])
        self.assertTrue((self.root / ".paper-duplicates" / removal["batch_id"] / "manifest.json").exists())
        copied.write_bytes(b"new occupant")
        self.assertTrue(restore_duplicates(self.root, removal["batch_id"])["errors"])
        self.assertEqual(copied.read_bytes(), b"new occupant")
        copied.unlink()
        restored = restore_duplicates(self.root, removal["batch_id"])
        self.assertEqual(restored["restored"], [str(copied)])
        self.assertEqual(copied.read_bytes(), original_bytes)

    def test_identical_text_and_figures_in_different_pdf_containers(self):
        records = [self.paper("article.pdf", metadata="one"), self.paper("copy.pdf", metadata="two")]
        self.assertNotEqual(records[0]["fingerprint"]["sha256"], records[1]["fingerprint"]["sha256"])
        report = inspect_duplicates(self.root, records)
        self.assertEqual(len(report["confirmed"]), 1)
        self.assertIn("页面图像", report["confirmed"][0]["reason"])

    def test_middle_pages_and_changed_figures_prevent_removal_even_with_same_arxiv_id(self):
        first = self.paper("article.pdf", stamp="1706.03762v1")
        for record in (self.paper("revision.pdf", last_page="Changed scientific conclusions", stamp="1706.03762v2"),
                       self.paper("figure.pdf", figure=True, stamp="1706.03762v1")):
            report = inspect_duplicates(self.root, [first, record])
            self.assertEqual(report["confirmed"], [])
            self.assertEqual(len(report["suspected"]), 1)
            self.assertEqual(remove_duplicates(self.root, report)["moved"], [])

    def test_annotations_prevent_automatic_removal(self):
        first = self.paper("article.pdf")
        copied = self.root / "annotated.pdf"
        shutil.copyfile(first["source"], copied)
        with pymupdf.open(copied) as document:
            document[0].add_text_annot((200, 200), "Important personal note")
            document.saveIncr()
        report = inspect_duplicates(self.root, [first, self.record(copied)])
        self.assertEqual(report["confirmed"], [])
        self.assertTrue(report["suspected"])

    def test_different_note_text_is_preserved_even_when_note_icons_render_the_same(self):
        records = [self.paper("first.pdf"), self.paper("second.pdf")]
        for number, record in enumerate(records):
            with pymupdf.open(record["source"]) as document:
                document[0].add_text_annot((200, 200), f"Personal note {number}")
                document.saveIncr()
            record["fingerprint"] = file_fingerprint(Path(record["source"]))
        self.assertFalse(inspect_duplicates(self.root, records)["confirmed"])

    def test_changed_files_and_symlinks_are_not_removed(self):
        first = self.paper("article.pdf")
        second = self.paper("copy.pdf")
        report = inspect_duplicates(self.root, [first, second])
        Path(report["confirmed"][0]["keep"]).write_bytes(b"changed")
        result = remove_duplicates(self.root, report)
        self.assertFalse(result["moved"])
        self.assertTrue(result["errors"])
        self.assertTrue(Path(report["confirmed"][0]["duplicate"]).exists())


if __name__ == "__main__":
    unittest.main()
