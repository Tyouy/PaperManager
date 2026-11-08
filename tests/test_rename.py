import errno
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from paper_renamer.rename import (
    RenameRequest, apply_renames, build_filename, file_fingerprint,
    sanitize_filename_component, undo_renames,
)


class RenameTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.logs = self.root / ".paper-renamer-history"

    def tearDown(self):
        self.temp.cleanup()

    def pdf(self, relative="original.pdf", content=b"%PDF-original"):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def test_unicode_filename_stays_within_utf8_budget(self):
        filename = build_filename("王", "Nature / Materials", "电池研究：电解质/安全? " * 80)
        self.assertLessEqual(len(filename.encode("utf-8")), 240)
        self.assertTrue(filename.startswith("时间未知-王-Nature Materials-"))
        self.assertTrue(filename.endswith(".pdf"))
        self.assertNotIn("/", filename)
        self.assertEqual(sanitize_filename_component("CON"), "_CON")

    def test_empty_fields_rejected(self):
        for journal, title in (("", "Title"), ("Science", "")):
            with self.assertRaises(ValueError):
                build_filename("", journal, title)

    def test_empty_author_is_omitted_without_a_placeholder_or_extra_separator(self):
        self.assertEqual(build_filename("", "Science", "Title", "2024-03-12"), "2024-Science-Title.pdf")
        self.assertEqual(build_filename("  ", "Science", "Title", ""), "时间未知-Science-Title.pdf")

    def test_journal_abbreviation_keeps_final_period_inside_filename(self):
        self.assertEqual(
            build_filename("Alice Smith", "Nat. Commun.", "Some article title"),
            "时间未知-Alice Smith-Nat. Commun.-Some article title.pdf",
        )
        self.assertEqual(sanitize_filename_component(" .hidden. "), "hidden")

    def test_full_author_name_uses_expanded_budget_without_breaking_total_limit(self):
        author = "Alexandria " + "Longmiddlename " * 4 + "Smith"
        self.assertGreater(len(author.encode("utf-8")), 64)
        self.assertLess(len(author.encode("utf-8")), 128)
        filename = build_filename(author, "arxiv", "A detailed research paper title " * 20)
        self.assertTrue(filename.startswith("时间未知-" + author + "-arxiv-"))
        self.assertLessEqual(len(filename.encode("utf-8")), 240)

    def test_unsupported_hardlinks_leave_original_and_explain_external_disk(self):
        original = self.pdf()
        with mock.patch("paper_renamer.rename.os.link", side_effect=OSError(errno.EOPNOTSUPP, "Not supported")):
            result = apply_renames(self.root, [RenameRequest(str(original), "new.pdf")], self.logs)
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("exFAT", result["errors"][0]["error"])
        self.assertIn("原文件未改动", result["errors"][0]["error"])
        self.assertTrue(original.exists())
        self.assertFalse((self.root / "new.pdf").exists())

    def test_nested_pdf_rename_and_undo_preserves_content(self):
        original = self.pdf("nested/original.PDF")
        fingerprint = file_fingerprint(original)
        result = apply_renames(self.root, [RenameRequest(str(original), "Wang-Nat-Test.pdf", fingerprint)], self.logs)
        renamed = original.with_name("Wang-Nat-Test.pdf")
        self.assertEqual(len(result["renamed"]), 1)
        self.assertFalse(original.exists())
        self.assertEqual(file_fingerprint(renamed), fingerprint)
        undone = undo_renames(self.root, result["log_id"], self.logs)
        self.assertEqual(len(undone["restored"]), 1)
        self.assertTrue(original.exists())
        self.assertFalse(renamed.exists())
        repeated = undo_renames(self.root, result["log_id"], self.logs)
        self.assertEqual(repeated["restored"], [])

    def test_existing_and_same_batch_collisions_never_overwrite(self):
        first = self.pdf("a.pdf", b"first")
        second = self.pdf("b.pdf", b"second")
        occupied = self.pdf("Title.pdf", b"existing")
        result = apply_renames(self.root, [RenameRequest(str(first), "Title.pdf"), RenameRequest(str(second), "Title.pdf")], self.logs)
        self.assertFalse(result["errors"])
        self.assertEqual(occupied.read_bytes(), b"existing")
        self.assertEqual((self.root / "Title (2).pdf").read_bytes(), b"first")
        self.assertEqual((self.root / "Title (3).pdf").read_bytes(), b"second")

    def test_stale_preview_rejected(self):
        original = self.pdf()
        fingerprint = file_fingerprint(original)
        original.write_bytes(b"modified")
        result = apply_renames(self.root, [RenameRequest(str(original), "new.pdf", fingerprint)], self.logs)
        self.assertEqual(len(result["errors"]), 1)
        self.assertTrue(original.exists())
        self.assertFalse((self.root / "new.pdf").exists())

    def test_path_traversal_and_non_pdf_rejected(self):
        original = self.pdf()
        text = self.pdf("notes.txt")
        requests = [RenameRequest(str(original), "../outside.pdf"), RenameRequest(str(text), "notes.pdf"), RenameRequest("../outside.pdf", "new.pdf")]
        result = apply_renames(self.root, requests, self.logs)
        self.assertEqual(len(result["errors"]), 3)
        self.assertTrue(original.exists())
        self.assertTrue(text.exists())

    def test_source_and_parent_symlinks_rejected(self):
        original = self.pdf("real/a.pdf")
        (self.root / "link.pdf").symlink_to(original)
        (self.root / "linked-dir").symlink_to(original.parent, target_is_directory=True)
        result = apply_renames(self.root, [RenameRequest("link.pdf", "new.pdf"), RenameRequest("linked-dir/a.pdf", "new.pdf")], self.logs)
        self.assertEqual(len(result["errors"]), 2)
        self.assertTrue(original.exists())

    def test_outside_source_and_log_directory_rejected(self):
        original = self.pdf()
        result = apply_renames(self.root, [RenameRequest("/not/in/root/file.pdf", "new.pdf")], self.logs)
        self.assertEqual(len(result["errors"]), 1)
        with self.assertRaises(ValueError):
            apply_renames(self.root, [RenameRequest(str(original), "new.pdf")], self.root.parent / "logs")

    def test_undo_refuses_changed_pdf_or_occupied_original(self):
        original = self.pdf()
        result = apply_renames(self.root, [RenameRequest(str(original), "new.pdf")], self.logs)
        new = self.root / "new.pdf"
        original.write_bytes(b"other file")
        undone = undo_renames(self.root, result["log_id"], self.logs)
        self.assertEqual(len(undone["errors"]), 1)
        self.assertEqual(original.read_bytes(), b"other file")
        original.unlink()
        new.write_bytes(b"modified after rename")
        undone = undo_renames(self.root, result["log_id"], self.logs)
        self.assertEqual(len(undone["errors"]), 1)
        self.assertFalse(original.exists())

    def test_pending_journal_recovers_move_after_crash(self):
        original = self.pdf()
        result = apply_renames(self.root, [RenameRequest(str(original), "new.pdf")], self.logs)
        log_path = self.logs / (result["log_id"] + ".json")
        journal = json.loads(log_path.read_text())
        journal["operations"][0]["status"] = "pending"
        log_path.write_text(json.dumps(journal))
        undone = undo_renames(self.root, result["log_id"], self.logs)
        self.assertEqual(len(undone["restored"]), 1)
        self.assertTrue(original.exists())

    def test_pending_journal_recovers_interrupted_hardlink(self):
        original = self.pdf()
        fingerprint = file_fingerprint(original)
        result = apply_renames(self.root, [], self.logs)
        target = self.root / "new.pdf"
        os.link(original, target)
        log_path = self.logs / (result["log_id"] + ".json")
        journal = json.loads(log_path.read_text())
        journal["operations"] = [{"source": original.name, "target": target.name, "fingerprint": fingerprint, "status": "pending"}]
        log_path.write_text(json.dumps(journal))
        undone = undo_renames(self.root, result["log_id"], self.logs)
        self.assertEqual(len(undone["restored"]), 1)
        self.assertTrue(original.exists())
        self.assertFalse(target.exists())

    def test_tampered_history_cannot_move_outside_root(self):
        original = self.pdf()
        result = apply_renames(self.root, [RenameRequest(str(original), "new.pdf")], self.logs)
        log_path = self.logs / (result["log_id"] + ".json")
        journal = json.loads(log_path.read_text())
        journal["operations"][0]["source"] = "../escape.pdf"
        log_path.write_text(json.dumps(journal))
        undone = undo_renames(self.root, result["log_id"], self.logs)
        self.assertEqual(len(undone["errors"]), 1)
        self.assertTrue((self.root / "new.pdf").exists())


if __name__ == "__main__":
    unittest.main()
