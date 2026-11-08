"""Dates must reflect publication, with partial precision and safe filename budgets."""
import unittest

from paper_renamer.dates import date_bounds, normalize_date, publication_date
from paper_renamer.rename import build_filename


class PublicationDateTests(unittest.TestCase):
    def test_publication_labels_and_languages(self):
        examples = {"Published online: 12 March 2024": "2024-03-12",
                    "First published online on March 12, 2024": "2024-03-12",
                    "Available online 2024/3/12": "2024-03-12",
                    "发表日期：2024年3月12日": "2024-03-12",
                    "Publication date: March 2024": "2024-03",
                    "Published: 2024": "2024"}
        for text, expected in examples.items():
            with self.subTest(text=text):
                self.assertEqual(publication_date({}, text)[0], expected)

    def test_received_accepted_reference_and_database_dates_are_not_publication(self):
        record = {"created": {"date-parts": [[2026, 1, 1]]}, "indexed": {"date-parts": [[2026]]}}
        front = "Received 2022-01-01\nAccepted 2023-02-03\nReferences\nPublished 2020-01-01"
        self.assertEqual(publication_date(record, front), ("", ""))

    def test_online_date_precedes_print_and_preserves_precision(self):
        record = {"published-online": {"date-parts": [[2023, 12]]},
                  "published-print": {"date-parts": [[2024, 1, 2]]}}
        self.assertEqual(publication_date(record, "Published 2025-01-01"), ("2023-12", "Crossref published-online"))
        self.assertEqual(publication_date({"issued": {"date-parts": [[2024]]}}, "")[0], "2024")

    def test_arxiv_initial_month_not_revision_stamp(self):
        self.assertEqual(publication_date({}, "arXiv:1706.03762v7 2 Aug 2023", "1706.03762v7")[0], "2017-06")
        self.assertEqual(publication_date({}, "", "hep-th/9901001v2")[0], "1999-01")
        self.assertEqual(publication_date({"published": {"date-parts": [[2017, 6, 12]]}}, "", "1706.03762v7")[0], "2017-06-12")

    def test_validity_precision_and_leap_year_bounds(self):
        for value in ("2024-02-30", "2023-02-29", "2024-13", "0000", "2024-00", "2024-2-3"):
            self.assertEqual(normalize_date(value), "", value)
        self.assertEqual(date_bounds("2024-02"), ("2024-02-01", "2024-02-29"))
        self.assertEqual(date_bounds("2023"), ("2023-01-01", "2023-12-31"))

    def test_date_prefix_unknown_and_utf8_length(self):
        self.assertEqual(build_filename("Alice Smith", "Cell", "Example title", "2024-03-12"),
                         "2024-Alice Smith-Cell-Example title.pdf")
        for published in ("2024", "2024-03", "2024-03-12"):
            self.assertEqual(build_filename("Bob Jones", "Cell", "Example title", published),
                             "2024-Bob Jones-Cell-Example title.pdf")
        self.assertTrue(build_filename("王明", "Nature", "标题", "").startswith("时间未知-"))
        filename = build_filename("欧阳明", "Nat. Commun.", "电解质研究" * 100, "2024-03-12")
        self.assertLessEqual(len(filename.encode()), 240)
