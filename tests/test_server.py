"""End-to-end loopback HTTP tests with temporary PDFs, never user documents."""
from __future__ import annotations

import http.client
import json
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import pymupdf

import app
from paper_renamer.rename import file_fingerprint
from paper_renamer.library import Library


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        cls.server.daemon_threads = True
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.library_patch = patch.object(app, "LIBRARY", Library(self.root / "catalogue.sqlite3"))
        self.library_patch.start()

    def tearDown(self):
        # Polling completion is deliberately separate from the HTTP worker thread.
        deadline = time.monotonic() + 5
        while app.OPERATION_LOCK.locked() and time.monotonic() < deadline:
            time.sleep(.005)
        self.assertFalse(app.OPERATION_LOCK.locked(), "background operation did not finish")
        with app.JOB_LOCK:
            app.JOBS.clear()
        self.library_patch.stop()
        self.temporary.cleanup()

    def request(self, method, path, payload=None, *, headers=None, custom_header=True):
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request_headers = {"Content-Type": "application/json"}
        if method == "POST" and custom_header:
            request_headers["X-Paper-Renamer"] = "local"
        request_headers.update(headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            content = response.read()
            return response.status, json.loads(content)
        finally:
            connection.close()

    def make_pdf(self, relative="original.pdf", *, title=None, authors=True):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        title = title or "A reproducible method for measuring nanoscale material properties"
        document = pymupdf.open()
        page = document.new_page()
        page.insert_text((50, 40), "Nature Communications", fontsize=10)
        page.insert_textbox((50, 70, 550, 160), title, fontsize=18)
        if authors:
            page.insert_text((50, 180), "Alice Smith and Bob Jones", fontsize=11)
        page.insert_textbox((50, 210, 550, 600), (
            "Abstract\nWe describe a reproducible measurement method for materials.\n"
            "Independent validation demonstrates consistent and robust performance.\n"
            "Introduction\nReliable scientific measurements require accurate calibration.\n"
            "Methods\nWe repeatedly measured calibrated samples under fixed conditions.\n"
            "Results\nThe measurements were consistent with the reference values.\n"
            "Discussion\nThe protocol improves reproducibility across laboratories.\n"
            "References\n1. Prior scientific research on calibrated measurements."
        ), fontsize=10)
        page.insert_text((50, 650), "Corresponding author: Alice Smith", fontsize=9)
        page.insert_text((50, 55), "Published online: 12 March 2024", fontsize=9)
        document.set_metadata({"title": title, "author": "Alice Smith; Bob Jones" if authors else ""})
        document.save(path)
        document.close()
        return path

    def scan(self, *, recursive=True):
        status, response = self.request("POST", "/api/scan", {
            "folder": str(self.root), "recursive": recursive,
            "metadata": False, "ai": {"enabled": False},
        })
        self.assertEqual(status, 200, response)
        job_id = response["job_id"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status, job = self.request("GET", "/api/jobs/" + job_id)
            self.assertEqual(status, 200, job)
            if job["status"] != "running":
                self.assertEqual(job["status"], "done", job)
                self.assertEqual(job["progress"]["done"], job["progress"]["total"])
                return job
            time.sleep(.01)
        self.fail("scan did not complete within five seconds")

    def test_rejects_untrusted_host_on_get_and_post(self):
        for method, path in (("GET", "/api/jobs/unknown"), ("POST", "/api/scan")):
            with self.subTest(method=method):
                status, response = self.request(method, path, {"folder": str(self.root)},
                                                headers={"Host": "attacker.example"})
                self.assertEqual(status, 403, response)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_rejects_foreign_origin_and_cross_site_requests(self):
        for headers in ({"Origin": "https://attacker.example"}, {"Sec-Fetch-Site": "cross-site"}):
            with self.subTest(headers=headers):
                status, response = self.request("POST", "/api/scan", {"folder": str(self.root)},
                                                headers=headers)
                self.assertEqual(status, 403, response)

    def test_post_requires_custom_local_request_header(self):
        status, response = self.request("POST", "/api/scan", {"folder": str(self.root)},
                                        custom_header=False)
        self.assertEqual(status, 403, response)
        with app.JOB_LOCK:
            self.assertEqual(app.JOBS, {})

    def test_same_origin_is_accepted(self):
        origin = "http://127.0.0.1:" + str(self.server.server_port)
        status, response = self.request("GET", "/api/jobs/not-a-job", headers={"Origin": origin})
        self.assertEqual(status, 404, response)

    def test_recursive_scan_finds_uppercase_pdfs_but_excludes_symlinks_and_history(self):
        first = self.make_pdf("original.PDF")
        self.make_pdf("nested/second.PDF")
        (self.root / "notes.txt").write_text("This is not a PDF.", encoding="utf-8")
        (self.root / "linked.pdf").symlink_to(first)
        (self.root / "linked-directory").symlink_to(self.root / "nested", target_is_directory=True)
        self.make_pdf(".paper-renamer-history/ignored.pdf")
        job = self.scan()
        self.assertEqual({row["relative_path"] for row in job["results"]},
                         {"original.PDF", "nested/second.PDF"})
        self.assertEqual(job["progress"]["total"], 2)
        self.assertTrue(all(row["status"] == "ready" for row in job["results"]))
        shallow = self.scan(recursive=False)
        self.assertEqual([row["relative_path"] for row in shallow["results"]], ["original.PDF"])

    def test_scan_preview_rename_and_undo_preserve_pdf_content(self):
        original = self.make_pdf("nested/original.PDF")
        before = original.read_bytes()
        job = self.scan()
        self.assertTrue(original.exists())
        self.assertEqual(original.read_bytes(), before)
        self.assertEqual(sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob("*")
                                if p.is_file()), ["nested/original.PDF"])
        self.assertFalse((self.root / ".paper-renamer-history").exists())
        row = job["results"][0]
        self.assertEqual(row["status"], "ready", row)
        self.assertEqual(row["corresponding_author"], "Bob Jones")
        self.assertTrue(row["proposed_filename"].startswith("2024-Bob Jones-Nat. Commun.-"))
        self.assertIsInstance(row["fingerprint"]["mtime_ns"], str)
        self.assertEqual(int(row["fingerprint"]["mtime_ns"]), original.stat().st_mtime_ns)
        status, renamed = self.request("POST", "/api/rename", {
            "folder": str(self.root), "items": [{
                "source": row["source"], "filename": row["proposed_filename"],
                "fingerprint": row["fingerprint"],
            }],
        })
        self.assertEqual(status, 200, renamed)
        self.assertEqual(renamed["errors"], [])
        self.assertEqual(len(renamed["renamed"]), 1)
        target = Path(renamed["renamed"][0]["target"])
        self.assertEqual(target.parent, original.parent)
        self.assertEqual(target.name, row["proposed_filename"])
        self.assertFalse(original.exists())
        self.assertEqual(target.read_bytes(), before)
        self.assertTrue((self.root / ".paper-renamer-history" / (renamed["log_id"] + ".json")).is_file())
        status, undone = self.request("POST", "/api/undo", {
            "folder": str(self.root), "log_id": renamed["log_id"],
        })
        self.assertEqual(status, 200, undone)
        self.assertEqual(undone["errors"], [])
        self.assertEqual(len(undone["restored"]), 1)
        self.assertTrue(original.exists())
        self.assertEqual(original.read_bytes(), before)
        self.assertFalse(target.exists())

    def test_rename_without_scan_fingerprint_is_rejected_without_changes(self):
        original = self.make_pdf()
        before = original.read_bytes()
        status, response = self.request("POST", "/api/rename", {
            "folder": str(self.root),
            "items": [{"source": str(original), "filename": "Smith-Nature-Example.pdf"}],
        })
        self.assertEqual(status, 400, response)
        self.assertIn("指纹", response["error"])
        self.assertEqual(original.read_bytes(), before)
        self.assertFalse((self.root / "Smith-Nature-Example.pdf").exists())
        self.assertFalse((self.root / ".paper-renamer-history").exists())

    def test_review_row_can_rename_and_undo_without_an_author(self):
        original = self.make_pdf("nested/no-author.pdf", authors=False)
        before = original.read_bytes()
        row = self.scan()["results"][0]
        self.assertEqual(row["status"], "review")
        self.assertEqual(row["corresponding_author"], "")
        self.assertEqual(row["last_author"], "")
        self.assertTrue(row["rename_eligible"])
        self.assertTrue(row["proposed_filename"].startswith("2024-Nat. Commun.-"))
        self.assertNotIn("Alice Smith", row["proposed_filename"])
        status, renamed = self.request("POST", "/api/rename", {"folder": str(self.root), "items": [{
            "source": row["source"], "filename": row["proposed_filename"], "fingerprint": row["fingerprint"]}]})
        self.assertEqual(status, 200, renamed)
        self.assertEqual(renamed["errors"], [])
        self.assertEqual(len(renamed["renamed"]), 1)
        target = Path(renamed["renamed"][0]["target"])
        self.assertEqual(target.read_bytes(), before)
        status, undone = self.request("POST", "/api/undo", {"folder": str(self.root), "log_id": renamed["log_id"]})
        self.assertEqual(status, 200, undone)
        self.assertEqual(undone["errors"], [])
        self.assertEqual(original.read_bytes(), before)
        self.assertFalse(target.exists())

    def test_nonpaper_is_ineligible_even_if_client_forges_paper_flags(self):
        original = self.make_pdf("manual.pdf", title="User manual for laboratory measurement equipment")
        before = original.read_bytes()
        row = self.scan()["results"][0]
        self.assertFalse(row["is_research_paper"], row)
        self.assertEqual(row["status"], "skipped", row)
        status, response = self.request("POST", "/api/rename", {
            "folder": str(self.root), "items": [{
                "source": row["source"], "filename": "Jones-Nature-Forged research paper.pdf",
                "fingerprint": row["fingerprint"], "is_research_paper": True,
                "rename_eligible": True, "status": "ready",
            }],
        })
        self.assertEqual(status, 400, response)
        self.assertIn("非论文", response["error"])
        self.assertEqual(original.read_bytes(), before)
        self.assertFalse((self.root / ".paper-renamer-history").exists())
        self.assertEqual([p.name for p in self.root.iterdir()], ["manual.pdf"])

    def test_unscanned_file_with_valid_fingerprint_cannot_be_renamed(self):
        original = self.make_pdf()
        fingerprint = file_fingerprint(original)
        fingerprint["mtime_ns"] = str(fingerprint["mtime_ns"])
        status, response = self.request("POST", "/api/rename", {
            "folder": str(self.root), "items": [{
                "source": str(original), "filename": "Jones-Nature-Unscanned.pdf",
                "fingerprint": fingerprint, "is_research_paper": True,
            }],
        })
        self.assertEqual(status, 400, response)
        self.assertIn("扫描", response["error"])
        self.assertTrue(original.exists())
        self.assertFalse((self.root / ".paper-renamer-history").exists())

    def test_latest_scan_excludes_files_from_an_earlier_preview(self):
        self.make_pdf()
        nested = self.make_pdf("nested/old-preview.pdf")
        row = next(row for row in self.scan()["results"] if row["source"] == str(nested))
        self.scan(recursive=False)
        status, response = self.request("POST", "/api/rename", {
            "folder": str(self.root), "items": [{
                "source": row["source"], "filename": row["proposed_filename"],
                "fingerprint": row["fingerprint"],
            }],
        })
        self.assertEqual(status, 400, response)
        self.assertIn("最新扫描", response["error"])
        self.assertTrue(nested.exists())
        self.assertFalse((self.root / ".paper-renamer-history").exists())

    def test_updated_client_fingerprint_cannot_bypass_scan_snapshot(self):
        original = self.make_pdf()
        row = self.scan()["results"][0]
        with original.open("ab") as stream:
            stream.write(b"\n% updated after scan\n")
        updated = file_fingerprint(original)
        updated["mtime_ns"] = str(updated["mtime_ns"])
        status, response = self.request("POST", "/api/rename", {
            "folder": str(self.root), "items": [{
                "source": row["source"], "filename": row["proposed_filename"],
                "fingerprint": updated,
            }],
        })
        self.assertEqual(status, 400, response)
        self.assertIn("最新扫描", response["error"])
        self.assertTrue(original.exists())
        self.assertFalse((self.root / ".paper-renamer-history").exists())

    def test_latest_nonpaper_classification_supersedes_old_ready_row(self):
        original = self.make_pdf()
        row = self.scan()["results"][0]
        self.assertEqual(row["status"], "ready")
        original.unlink()
        self.make_pdf(title="User manual for laboratory measurement equipment")
        latest = self.scan()["results"][0]
        self.assertFalse(latest["is_research_paper"])
        status, response = self.request("POST", "/api/rename", {
            "folder": str(self.root), "items": [{
                "source": row["source"], "filename": row["proposed_filename"],
                "fingerprint": latest["fingerprint"], "is_research_paper": True,
            }],
        })
        self.assertEqual(status, 400, response)
        self.assertIn("非论文", response["error"])
        self.assertTrue(original.exists())
        self.assertFalse((self.root / ".paper-renamer-history").exists())

    def library_index(self, **options):
        status, response = self.request("POST", "/api/library/index", {"folder": str(self.root), "metadata": False, **options})
        self.assertEqual(status, 200, response)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status, job = self.request("GET", "/api/jobs/" + response["job_id"])
            self.assertEqual(status, 200)
            if job["status"] != "running":
                self.assertEqual(job["status"], "done", job)
                self.assertNotIn("results", job)  # Polling a large library must stay small.
                return job
            time.sleep(.01)
        self.fail("library index did not complete")

    def test_library_deduplicates_full_content_and_restore_is_available_after_restart(self):
        first = self.make_pdf("article.pdf")
        second = self.make_pdf("duplicate.pdf")
        job = self.library_index()
        self.assertEqual(job["duplicates"]["removed"], 1)
        self.assertTrue(first.exists())
        self.assertFalse(second.exists())
        status, found = self.request("POST", "/api/library/search", {"folder": str(self.root)})
        self.assertEqual(status, 200)
        self.assertEqual(found["total"], 1)
        status, report = self.request("POST", "/api/library/duplicates", {"folder": str(self.root)})
        self.assertEqual(len(report["confirmed"]), 1)
        self.assertTrue(report["undo_batch"])
        # Reloading the index and a fresh Library instance must preserve recovery.
        self.library_index()
        app.LIBRARY = Library(app.LIBRARY.database)
        status, restored = self.request("POST", "/api/library/restore-duplicates", {"folder": str(self.root)})
        self.assertEqual(status, 200, restored)
        self.assertEqual(len(restored["restored"]), 1)
        self.assertTrue(second.exists())
        preview = self.library_index(deduplicate=False)
        self.assertEqual(preview["duplicates"]["removed"], 0)
        self.assertTrue(first.exists() and second.exists())
        status, report = self.request("POST", "/api/library/duplicates", {"folder": str(self.root)})
        self.assertEqual(len(report["confirmed"]), 1)
        self.assertFalse(report["removal"]["moved"])

    def test_library_recursive_index_search_cached_update_and_open(self):
        original = self.make_pdf("nested/article.PDF")
        before = original.read_bytes()
        job = self.library_index()
        self.assertEqual(job["progress"]["total"], 1)
        self.assertEqual(original.read_bytes(), before)
        query = {"folder": str(self.root), "author": "BOB Jones", "title": "NANOSCALE", "date_from": "2024", "date_to": "2024-03"}
        status, found = self.request("POST", "/api/library/search", query)
        self.assertEqual(status, 200, found)
        self.assertEqual(found["total"], 1)
        self.assertEqual(found["results"][0]["publication_date"], "2024-03-12")
        self.assertEqual(found["results"][0]["authors"], ["Alice Smith", "Bob Jones"])
        with patch("app.open_document") as opener:
            status, response = self.request("POST", "/api/library/open", {"folder": str(self.root), "source": str(original)})
            self.assertEqual(status, 200, response)
            opener.assert_called_once_with(original)
        cached = self.library_index()
        self.assertEqual(cached["progress"]["reused"], 1)
        renamed = original.with_name("moved.PDF")
        original.rename(renamed)
        self.library_index()
        status, found = self.request("POST", "/api/library/search", query)
        self.assertEqual(found["results"][0]["source"], str(renamed))
        self.assertEqual(found["total"], 1)

    def test_library_index_does_not_authorize_renames_or_invalidate_rename_preview(self):
        original = self.make_pdf()
        self.library_index()
        fingerprint = file_fingerprint(original)
        fingerprint["mtime_ns"] = str(fingerprint["mtime_ns"])
        status, response = self.request("POST", "/api/rename", {"folder": str(self.root), "items": [{
            "source": str(original), "filename": "test.pdf", "fingerprint": fingerprint}]})
        self.assertEqual(status, 400, response)
        row = self.scan()["results"][0]
        self.library_index()
        status, response = self.request("POST", "/api/rename", {"folder": str(self.root), "items": [{
            "source": row["source"], "filename": row["proposed_filename"], "fingerprint": row["fingerprint"]}]})
        self.assertEqual(status, 200, response)
        self.assertEqual(len(response["renamed"]), 1)

    def test_library_includes_unreadable_pdfs_and_rejects_invalid_filters(self):
        (self.root / "broken.pdf").write_bytes(b"not a pdf")
        self.library_index()
        status, found = self.request("POST", "/api/library/search", {"folder": str(self.root)})
        self.assertEqual(status, 200, found)
        self.assertEqual(found["total"], 1)
        self.assertEqual(found["results"][0]["status"], "error")
        status, response = self.request("POST", "/api/library/search", {"folder": str(self.root), "date_from": "2025-02-30"})
        self.assertEqual(status, 400, response)


if __name__ == "__main__":
    unittest.main()
