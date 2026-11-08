"""Bibliographic extraction tests, using real temporary PDF files and mocked HTTP."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pymupdf

from paper_renamer.extract import (
    analyze_pdf, correspondence, detect_journal, family_from_name, journal_abbreviation,
    author_full_name, last_author_family, last_author_name, metadata_authors, read_pdf,
)
from paper_renamer.metadata import crossref_metadata


TITLE = "A reproducible method for measuring nanoscale material properties"
AUTHORS = [
    {"given": "Alice", "family": "Smith"},
    {"given": "Bob", "family": "Jones"},
]


class ExtractionTests(unittest.TestCase):
    def test_unstamped_arxiv_word_manuscript_with_unlabeled_abstract_and_middle_references(self):
        path = self.root / "2610.39978v1.pdf"
        title = "Electronic liquid crystals and superconductivity from doping a moire Mott insulator"
        with pymupdf.open() as document:
            page = document.new_page()
            page.insert_text((40, 70), title, fontsize=12, fontname="hebo")
            page.insert_text((40, 100), "Alice Smith, Bob Jones", fontsize=12)
            page.insert_text((40, 130), "Department of Physics, Cornell University", fontsize=12)
            page.insert_textbox((40, 160, 550, 400), "We describe electronic liquid crystals. " * 18, fontsize=12)
            page.insert_text((40, 450), "Main", fontsize=12)
            for number in range(12):
                page = document.new_page()
                page.insert_text((40, 70), "Methods\nReferences" if number == 6 else "Additional scientific results.", fontsize=12)
            document.set_metadata({"author": "Alice Smith"})
            document.save(path)
        result = analyze_pdf(path, self.root, use_metadata=False)
        self.assertEqual(result["title"], title)
        self.assertEqual(result["arxiv_id"], "2610.39978v1")
        self.assertTrue(result["is_research_paper"])
        self.assertEqual(result["last_author"], "Bob Jones")

    def test_short_metadata_title_does_not_truncate_printed_title(self):
        path = self.make_pdf(title=TITLE)
        with pymupdf.open(path) as document:
            document.set_metadata({"title": "A reproducible method"})
            document.saveIncr()
        self.assertEqual(read_pdf(path).title, TITLE)

    def test_preprints_with_only_title_authors_and_abstract_are_papers(self):
        for label, source in (("A PREPRINT", "preprint"), ("bioRxiv preprint doi: 10.1101/2024.01.01.123456", "biorxiv"),
                              ("medRxiv preprint", "medrxiv"), ("ChemRxiv", "chemrxiv"),
                              ("Research Square", "researchsquare"), ("arXiv", "arxiv")):
            path = self.make_pdf(body="Abstract\n" + "We investigate reproducible materials measurement. " * 10)
            with pymupdf.open(path) as document:
                document[0].insert_text((50, 20), label, fontsize=10)
                document.saveIncr()
            result = analyze_pdf(path, self.root, use_metadata=False)
            with self.subTest(source=source):
                self.assertTrue(result["is_research_paper"], result)
                self.assertEqual(result["preprint_source"], source)
                self.assertEqual(result["journal_abbreviation"], source)
                self.assertEqual(result["status"], "ready")

    def test_filename_arxiv_hint_requires_remote_title_verification(self):
        path = self.make_pdf()
        renamed = path.with_name("1706.03762v7.pdf")
        path.rename(renamed)
        with patch("paper_renamer.extract.arxiv_metadata", return_value=({}, ["不匹配"])), \
                patch("paper_renamer.extract.crossref_metadata", return_value=({}, [])):
            self.assertEqual(analyze_pdf(renamed, self.root)["arxiv_id"], "")
        record = {"title": [TITLE], "author": AUTHORS}
        with patch("paper_renamer.extract.arxiv_metadata", return_value=(record, [])), \
                patch("paper_renamer.extract.crossref_metadata") as crossref:
            result = analyze_pdf(renamed, self.root)
        self.assertEqual(result["arxiv_id"], "1706.03762v7")
        self.assertEqual(result["journal_abbreviation"], "arxiv")
        crossref.assert_not_called()

    def test_preprint_mention_in_body_and_references_does_not_change_source(self):
        path = self.make_pdf(body="Abstract\nWe compare bioRxiv preprints and arXiv results.\nIntroduction\nMethods\nReferences\nbioRxiv preprint doi: 10.1101/12345")
        result = analyze_pdf(path, self.root, use_metadata=False)
        self.assertEqual(result["preprint_source"], "")
        self.assertEqual(result["journal_abbreviation"], "Nat. Commun.")

    def test_short_large_title_beats_smaller_legal_notice(self):
        path = self.root / "arxiv.pdf"
        document = pymupdf.open()
        page = document.new_page()
        page.insert_text((40, 25), "Provided proper attribution is provided, permission to", fontsize=12)
        page.insert_text((40, 42), "reproduce the tables and figures in this paper solely for use in journalistic or scholarly works.", fontsize=12)
        page.insert_text((90, 105), "Attention Is All You Need", fontsize=17)
        page.insert_text((70, 150), "Ashish Vaswani", fontsize=11)
        page.insert_text((70, 180), "Illia Polosukhin", fontsize=11)
        page.insert_text((70, 225), "Abstract", fontsize=12)
        page.insert_textbox((70, 250, 530, 400), "We introduce a neural architecture based on attention mechanisms. " * 8, fontsize=10)
        page.insert_text((20, 700), "arXiv:1706.03762v7 [cs.CL] 2 Aug 2023", fontsize=20, rotate=90)
        document.save(path)
        document.close()
        result = analyze_pdf(path, self.root, use_metadata=False)
        self.assertEqual(result["title"], "Attention Is All You Need")
        self.assertEqual(result["last_author"], "Illia Polosukhin")
        self.assertEqual(result["proposed_filename"], "2017-Illia Polosukhin-arxiv-Attention Is All You Need.pdf")

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def make_pdf(self, note="Corresponding author: Alice Smith", *, authors=True, title=TITLE,
                 body=None, metadata_title=True, printed_authors=True):
        """Create a typeset article fixture; no network or permanent fixture files."""
        path = self.root / "original.pdf"
        document = pymupdf.open()
        page = document.new_page(width=595, height=842)
        page.insert_text((50, 35), "Nature Communications", fontsize=10)
        page.insert_textbox((50, 65, 550, 160), title, fontsize=18)
        if printed_authors:
            page.insert_text((50, 180), "Alice Smith and Bob Jones", fontsize=11)
        text = body or (
            "Abstract\nWe present a reproducible measurement method for nanoscale materials.\n"
            "The method improves calibration and supports independent validation.\n"
            "Introduction\nAccurate measurement is essential to scientific discovery.\n"
            "Methods\nWe measured samples using repeated calibrated observations.\n"
            "Results\nIndependent measurements agreed within the reported uncertainty.\n"
            "Discussion\nThe experiments show a robust improvement over prior methods.\n"
            "References\n1. A previous study of calibrated materials measurement."
        )
        page.insert_textbox((50, 210, 550, 610), text, fontsize=10)
        if note:
            page.insert_textbox((50, 650, 550, 770), note, fontsize=9)
        metadata = {"title": title if metadata_title else ""}
        if authors:
            metadata["author"] = "Alice Smith; Bob Jones"
        document.set_metadata(metadata)
        document.save(path)
        document.close()
        return path

    def make_arxiv_pdf(self, note="", *, authors=True, title=TITLE):
        path = self.make_pdf(note, authors=authors, title=title)
        with pymupdf.open(path) as document:
            document[0].insert_text((50, 800), "arXiv:1706.03762v7 [cs.CL] 2 Aug 2023", fontsize=9)
            document.saveIncr()
        return path

    def test_offline_arxiv_uses_full_last_author_title_and_fixed_source(self):
        path = self.make_arxiv_pdf()
        with patch("paper_renamer.extract.arxiv_metadata") as arxiv_request, \
                patch("paper_renamer.extract.crossref_metadata") as crossref_request:
            result = analyze_pdf(path, self.root, use_metadata=False)
        arxiv_request.assert_not_called()
        crossref_request.assert_not_called()
        self.assertEqual(result["arxiv_id"], "1706.03762v7")
        self.assertEqual(result["journal"], "arxiv")
        self.assertEqual(result["journal_abbreviation"], "arxiv")
        self.assertEqual(result["title"], TITLE)
        self.assertEqual(result["corresponding_author"], "Bob Jones")
        self.assertEqual(result["author_source"], "last_author")
        self.assertEqual(result["proposed_filename"], "2017-Bob Jones-arxiv-" + TITLE + ".pdf")
        self.assertEqual(result["status"], "ready")
        self.assertTrue(result["rename_eligible"])

    def test_large_rotated_arxiv_stamp_is_not_used_as_the_title(self):
        path = self.make_pdf("", metadata_title=False)
        with pymupdf.open(path) as document:
            document[0].insert_text((20, 700), "arXiv:1706.03762v7 [cs.CL] 2 Aug 2023",
                                    fontsize=20, rotate=90)
            document.saveIncr()
        self.assertEqual(read_pdf(path).title, TITLE)
        result = analyze_pdf(path, self.root, use_metadata=False)
        self.assertEqual(result["arxiv_id"], "1706.03762v7")
        self.assertEqual(result["title"], TITLE)
        self.assertEqual(result["proposed_filename"], "2017-Bob Jones-arxiv-" + TITLE + ".pdf")

    def test_online_arxiv_uses_ordered_full_names_without_crossref(self):
        path = self.make_arxiv_pdf(authors=False)
        record = {"title": [TITLE], "author": [{"full_name": "Alice Smith"},
                                                {"full_name": "Robert Arthur Jones"}],
                  "container-title": ["arxiv"], "short-container-title": ["arxiv"]}
        with patch("paper_renamer.extract.arxiv_metadata", return_value=(record, [])) as arxiv_request, \
                patch("paper_renamer.extract.crossref_metadata") as crossref_request:
            result = analyze_pdf(path, self.root, use_metadata=True)
        arxiv_request.assert_called_once()
        self.assertEqual(arxiv_request.call_args.args[0], "1706.03762v7")
        crossref_request.assert_not_called()
        self.assertEqual(result["last_author"], "Robert Arthur Jones")
        self.assertEqual(result["corresponding_author"], "Robert Arthur Jones")
        self.assertEqual(result["proposed_filename"], "2017-Robert Arthur Jones-arxiv-" + TITLE + ".pdf")
        self.assertEqual(result["status"], "ready")

    def test_arxiv_stamp_takes_precedence_over_published_journal_metadata(self):
        path = self.make_arxiv_pdf()
        record = {"title": [TITLE], "author": AUTHORS,
                  "container-title": ["Nature Communications"], "short-container-title": ["Nat. Commun."]}
        with patch("paper_renamer.extract.arxiv_metadata", return_value=(record, [])):
            result = analyze_pdf(path, self.root)
        self.assertEqual(result["journal"], "arxiv")
        self.assertEqual(result["journal_abbreviation"], "arxiv")
        self.assertEqual(result["proposed_filename"], "2017-Bob Jones-arxiv-" + TITLE + ".pdf")

    def test_arxiv_marker_does_not_restore_nonpaper_rename_eligibility(self):
        path = self.make_arxiv_pdf(title="User manual for laboratory measurement equipment")
        with patch("paper_renamer.extract.arxiv_metadata") as arxiv_request:
            result = analyze_pdf(path, self.root)
        arxiv_request.assert_not_called()
        self.assertFalse(result["is_research_paper"])
        self.assertFalse(result["rename_eligible"])
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["proposed_filename"], "")

    def test_cited_arxiv_paper_does_not_override_own_journal(self):
        body = ("Abstract\nWe compare our reproducible measurements with prior work.\n"
                "Introduction\nA prior arXiv:1706.03762v7 report provides background.\n"
                "Methods\nWe measured calibrated samples repeatedly.\n"
                "Results\nThe measurements agreed with reference values.\n"
                "References\n1. Prior work, arXiv:1706.03762v7.")
        path = self.make_pdf(body=body)
        result = analyze_pdf(path, self.root, use_metadata=False)
        self.assertEqual(result["arxiv_id"], "")
        self.assertEqual(result["journal_abbreviation"], "Nat. Commun.")
        self.assertEqual(result["proposed_filename"], "时间未知-Bob Jones-Nat. Commun.-" + TITLE + ".pdf")

    def test_reads_real_pdf_metadata_title_and_author_names(self):
        parsed = read_pdf(self.make_pdf())
        self.assertEqual(parsed.title, TITLE)
        self.assertTrue(parsed.title_confident)
        self.assertEqual(parsed.authors, AUTHORS)
        self.assertEqual(parsed.pages, 1)
        self.assertIn("Abstract", parsed.front)

    def test_large_font_title_is_detected_without_title_metadata(self):
        parsed = read_pdf(self.make_pdf(metadata_title=False))
        self.assertEqual(parsed.title, TITLE)
        self.assertTrue(parsed.title_confident)

    def test_comma_separated_full_author_names_are_read(self):
        path = self.make_pdf()
        with pymupdf.open(path) as document:
            metadata = document.metadata
            metadata["author"] = "Alice Smith, Bob Jones"
            document.set_metadata(metadata)
            document.saveIncr()
        self.assertEqual(read_pdf(path).authors, AUTHORS)

    def test_comma_introduced_correspondence_name_is_detected(self):
        text = "The corresponding author, Daniel Acuna (email: acuna@example.org)."
        families, _ = correspondence(text, text, [])
        self.assertEqual(families, ["Daniel Acuna"])

    def test_last_author_is_used_even_when_a_different_correspondent_is_known(self):
        result = analyze_pdf(self.make_pdf(), self.root, use_metadata=False)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["corresponding_author"], "Bob Jones")
        self.assertEqual(result["journal_abbreviation"], "Nat. Commun.")
        self.assertEqual(result["proposed_filename"], "时间未知-Bob Jones-Nat. Commun.-" + TITLE + ".pdf")
        self.assertTrue(result["fingerprint"]["sha256"])
        self.assertTrue(result["evidence"])

    def test_crossref_author_names_can_resolve_correspondence(self):
        path = self.make_pdf(authors=False)
        record = {
            "DOI": "10.1234/example", "title": [TITLE], "author": AUTHORS,
            "container-title": ["Nature Communications"], "short-container-title": [],
        }
        with patch("paper_renamer.extract.crossref_metadata", return_value=(record, [])):
            result = analyze_pdf(path, self.root)
        self.assertEqual(result["corresponding_author"], "Bob Jones")
        self.assertEqual(result["status"], "ready")

    def test_direct_correspondence_name_without_author_metadata(self):
        result = analyze_pdf(self.make_pdf(authors=False), self.root, use_metadata=False)
        self.assertEqual(result["corresponding_author"], "Bob Jones")
        self.assertEqual(result["status"], "ready")

    def test_multiple_correspondents_do_not_change_last_author_naming(self):
        path = self.make_pdf("Corresponding authors: Alice Smith and Bob Jones")
        result = analyze_pdf(path, self.root, use_metadata=False)
        self.assertEqual(result["detected_corresponding_author"], "Alice Smith+Bob Jones")
        self.assertEqual(result["corresponding_author"], "Bob Jones")
        self.assertEqual(result["author_source"], "last_author")
        self.assertEqual(result["status"], "ready")
        self.assertIn("Bob Jones-Nat. Commun.-", result["proposed_filename"])

    def test_equal_contribution_asterisk_is_not_correspondence(self):
        note = "* These authors contributed equally.\nAlice Smith* and Bob Jones*"
        result = analyze_pdf(self.make_pdf(note), self.root, use_metadata=False)
        self.assertEqual(result["detected_corresponding_author"], "")
        self.assertEqual(result["corresponding_author"], "Bob Jones")
        self.assertEqual(result["author_source"], "last_author")
        self.assertEqual(result["status"], "ready")

    def test_no_correspondence_uses_last_author_with_explicit_label(self):
        note = "Contact details: bob.jones@example.org\nAlice Smith and Bob Jones"
        result = analyze_pdf(self.make_pdf(note), self.root, use_metadata=False)
        self.assertEqual(result["corresponding_author"], "Bob Jones")
        self.assertEqual(result["last_author"], "Bob Jones")
        self.assertEqual(result["detected_corresponding_author"], "")
        self.assertTrue(result["proposed_filename"].startswith("时间未知-Bob Jones-Nat. Commun.-"))
        self.assertEqual(result["status"], "ready")
        self.assertTrue(result["rename_eligible"])
        self.assertTrue(any("末位作者全名" in item for item in result["evidence"]))

    def test_unknown_last_author_is_not_invented(self):
        result = analyze_pdf(self.make_pdf("", authors=False, printed_authors=False), self.root, use_metadata=False)
        self.assertEqual(result["status"], "review")
        self.assertEqual(result["corresponding_author"], "")
        self.assertEqual(result["author_source"], "")
        self.assertEqual(result["proposed_filename"], "时间未知-Nat. Commun.-" + TITLE + ".pdf")
        self.assertTrue(result["rename_eligible"])
        self.assertTrue(any("作者已留空" in warning for warning in result["warnings"]))

    def test_correspondence_does_not_fill_unknown_last_author(self):
        path = self.make_pdf(authors=False, printed_authors=False)
        result = analyze_pdf(path, self.root, use_metadata=False)
        self.assertEqual(result["detected_corresponding_author"], "Alice Smith")
        self.assertEqual(result["last_author"], "")
        self.assertEqual(result["corresponding_author"], "")
        self.assertEqual(result["status"], "review")
        self.assertEqual(result["proposed_filename"], "时间未知-Nat. Commun.-" + TITLE + ".pdf")

    def test_unresolved_last_author_initials_do_not_use_an_earlier_author(self):
        path = self.make_pdf(authors=False, printed_authors=False)
        record = {"title": [TITLE], "author": [AUTHORS[0], {"given": "B.", "family": "Jones"}],
                  "container-title": ["Nature Communications"]}
        with patch("paper_renamer.extract.crossref_metadata", return_value=(record, [])):
            result = analyze_pdf(path, self.root)
        self.assertEqual(result["last_author"], "")
        self.assertEqual(result["corresponding_author"], "")
        self.assertEqual(result["status"], "review")
        self.assertEqual(result["proposed_filename"], "时间未知-Nat. Commun.-" + TITLE + ".pdf")

    def test_crossref_author_sequence_supplies_last_author(self):
        path = self.make_pdf("", authors=False)
        record = {"DOI": "10.1234/example", "title": [TITLE], "author": AUTHORS,
                  "container-title": ["Nature Communications"]}
        with patch("paper_renamer.extract.crossref_metadata", return_value=(record, [])):
            result = analyze_pdf(path, self.root)
        self.assertEqual(result["corresponding_author"], "Bob Jones")
        self.assertEqual(result["author_source"], "last_author")
        self.assertEqual(result["status"], "ready")

    def test_network_warning_does_not_change_last_author_naming(self):
        path = self.make_pdf()
        with patch("paper_renamer.extract.crossref_metadata", return_value=({}, ["联网查询失败"])):
            result = analyze_pdf(path, self.root)
        self.assertEqual(result["detected_corresponding_author"], "Alice Smith")
        self.assertEqual(result["corresponding_author"], "Bob Jones")
        self.assertEqual(result["status"], "ready")

    def test_last_author_does_not_fill_missing_journal(self):
        path = self.make_pdf("")
        with patch("paper_renamer.extract.detect_journal", return_value=("", "")):
            result = analyze_pdf(path, self.root, use_metadata=False)
        self.assertEqual(result["corresponding_author"], "Bob Jones")
        self.assertEqual(result["status"], "review")
        self.assertEqual(result["proposed_filename"], "")

    def test_partial_metadata_list_is_not_used_to_invent_last_author(self):
        self.assertEqual(metadata_authors("Alice Smith; Bob Jones", "Alice Smith"), [])
        self.assertEqual(last_author_family([AUTHORS[0], {"name": "Research Consortium"}]), "")

    def test_reversed_metadata_names_and_surname_particles(self):
        self.assertEqual(metadata_authors("Smith, Alice; van der Waals, Bob", "Alice Smith and Bob van der Waals"),
                         [{"given": "Alice", "family": "Smith"}, {"given": "Bob", "family": "van der Waals"}])

    def test_explicit_star_footnote_maps_only_starred_author(self):
        front = "Alice Smith1,* and Bob Jones2\n* Corresponding author"
        families, evidence = correspondence(front, front, AUTHORS)
        self.assertEqual(families, ["Alice Smith"])
        self.assertTrue(evidence)

    def test_unrelated_author_after_correspondence_is_not_added(self):
        text = (
            "Corresponding author: Alice Smith\n"
            "The experiments were performed by Bob Jones."
        )
        families, _ = correspondence(text, text, AUTHORS)
        self.assertEqual(families, ["Alice Smith"])

    def test_author_contribution_line_is_not_part_of_correspondence_note(self):
        text = "Corresponding author: Alice Smith\nBob Jones designed the experiments."
        families, _ = correspondence(text, text, AUTHORS)
        self.assertEqual(families, ["Alice Smith"])

    def test_singular_email_with_only_initial_does_not_invent_full_name(self):
        text = "* Corresponding author.\nE-mail address: alice@example.org (A. Smith)."
        families, _ = correspondence(text, text, [])
        self.assertEqual(families, [])

    def test_initials_explicitly_addressed_to_a_known_author(self):
        text = "Correspondence and requests for materials should be addressed to A. S."
        families, _ = correspondence(text, text, AUTHORS)
        self.assertEqual(families, ["Alice Smith"])

    def test_user_manual_is_skipped(self):
        path = self.make_pdf(title="User manual for calibrated laboratory measurements")
        result = analyze_pdf(path, self.root, use_metadata=False)
        self.assertEqual(result["status"], "skipped")
        self.assertFalse(result["is_research_paper"])
        self.assertFalse(result["rename_eligible"])
        self.assertEqual(result["proposed_filename"], "")

    def test_ai_nonpaper_result_is_skipped_without_last_author_fallback(self):
        path = self.make_pdf()
        with patch("paper_renamer.extract.ai_metadata", return_value={"is_research_paper": False}):
            result = analyze_pdf(path, self.root, use_metadata=False, ai={"enabled": True})
        self.assertEqual(result["status"], "skipped")
        self.assertFalse(result["is_research_paper"])
        self.assertFalse(result["rename_eligible"])
        self.assertEqual(result["proposed_filename"], "")

    def test_image_only_pdf_is_reported_for_ocr(self):
        path = self.root / "scanned.pdf"
        document = pymupdf.open()
        document.new_page()
        document.save(path)
        document.close()
        result = analyze_pdf(path, self.root, use_metadata=False)
        self.assertEqual(result["status"], "error")
        self.assertIn("OCR", result["reason"])

    def test_unverified_ai_surname_substring_is_not_accepted(self):
        note = "Corresponding author: Alice Smith. The laboratory supports replication."
        path = self.make_pdf(note)
        extra = {
            "is_research_paper": True,
            "corresponding_authors": [{"family": "He", "full_name": "Wei He", "evidence": note}],
        }
        with patch("paper_renamer.extract.ai_metadata", return_value=extra):
            result = analyze_pdf(path, self.root, use_metadata=False, ai={"enabled": True})
        self.assertEqual(result["detected_corresponding_author"], "Alice Smith")
        self.assertEqual(result["corresponding_author"], "Bob Jones")
        self.assertNotIn("He", result["corresponding_author"])

    def test_surname_particles_and_chinese_compound_surnames(self):
        self.assertEqual(family_from_name("Alice van der Waals"), "van der Waals")
        self.assertEqual(family_from_name("欧阳明"), "欧阳")
        self.assertEqual(family_from_name("李明"), "李")

    def test_full_name_helpers_preserve_particles_and_chinese_order(self):
        self.assertEqual(author_full_name({"given": "Alice", "family": "van der Waals"}),
                         "Alice van der Waals")
        self.assertEqual(author_full_name({"given": "小明", "family": "王"}), "王小明")
        self.assertEqual(last_author_name([AUTHORS[0], {"given": "明", "family": "欧阳"}]), "欧阳明")
        self.assertEqual(last_author_name(AUTHORS), "Bob Jones")

    def test_initials_only_metadata_does_not_invent_full_name(self):
        self.assertEqual(author_full_name({"given": "A.", "family": "Smith"}), "")
        self.assertEqual(author_full_name({"given": "A.", "family": "van der Waals"}), "")
        self.assertEqual(last_author_name([AUTHORS[0], {"given": "B.", "family": "Jones"}]), "")

    def test_direct_chinese_correspondence_preserves_entire_name(self):
        text = "通讯作者：欧阳明"
        names, _ = correspondence(text, text, [])
        self.assertEqual(names, ["欧阳明"])

    def test_parenthesized_initial_name_resolves_to_known_full_author(self):
        text = "* Corresponding author.\nE-mail address: alice@example.org (A. Smith)."
        names, _ = correspondence(text, text, AUTHORS)
        self.assertEqual(names, ["Alice Smith"])

    def test_unknown_journal_does_not_invent_an_abbreviation(self):
        self.assertEqual(journal_abbreviation("Imaginary Journal of Unknown Research"), "")
        self.assertEqual(journal_abbreviation("Unknown Journal", "U. J."), "U. J.")

    def test_generic_science_words_in_title_or_abstract_are_not_journals(self):
        text = "The nature of cell science in materials research\nAbstract\nPublished in Nature Communications."
        self.assertEqual(detect_journal(text), ("", ""))
        self.assertEqual(detect_journal("Nature Communications | (2025)\nArticle title\nAbstract"),
                         ("Nature Communications", "Nat. Commun."))


class MetadataMatchingTests(unittest.TestCase):
    def test_doi_with_different_title_is_rejected(self):
        wrong = {"message": {"DOI": "10.1234/wrong", "title": ["An entirely unrelated medical trial"]}}
        with patch("paper_renamer.metadata.request_json", return_value=wrong) as request:
            record, warnings = crossref_metadata(["10.1234/wrong"], TITLE, TITLE + "\nAbstract")
        self.assertEqual(record, {})
        self.assertTrue(warnings)
        self.assertEqual(request.call_count, 1)

    def test_doi_with_matching_markup_title_is_accepted(self):
        expected = {"DOI": "10.1234/right", "title": ["<i>" + TITLE + "</i>"]}
        with patch("paper_renamer.metadata.request_json", return_value={"message": expected}):
            record, warnings = crossref_metadata(["10.1234/right"], TITLE, TITLE + "\nAbstract")
        self.assertEqual(record, expected)
        self.assertEqual(warnings, [])

    def test_cited_doi_title_on_front_page_is_not_this_article_title(self):
        cited_title = "An entirely unrelated medical trial"
        wrong = {"message": {"DOI": "10.1234/cited", "title": [cited_title]}}
        front = TITLE + "\nAbstract\nWe compare with the prior article " + cited_title
        with patch("paper_renamer.metadata.request_json", return_value=wrong):
            record, warnings = crossref_metadata(["10.1234/cited"], TITLE, front)
        self.assertEqual(record, {})
        self.assertTrue(warnings)

    def test_title_search_does_not_blindly_accept_first_result(self):
        wrong = {"DOI": "10.1234/wrong", "title": ["An entirely unrelated medical trial"]}
        right = {"DOI": "10.1234/right", "title": [TITLE]}
        payload = {"message": {"items": [wrong, right]}}
        with patch("paper_renamer.metadata.request_json", return_value=payload):
            record, warnings = crossref_metadata([], TITLE, TITLE + "\nAbstract")
        self.assertEqual(record, right)
        self.assertEqual(warnings, [])


if __name__ == "__main__":
    unittest.main()
