"""Bounded author-header parsing on actual in-memory PDF text geometry."""
from __future__ import annotations

import unittest

import pymupdf

from paper_renamer.header_authors import _clean_name, _name, header_authors


TITLE = "Attention Is All You Need"


class HeaderAuthorsTests(unittest.TestCase):
    def page(self, lines, *, abstract=True):
        document = pymupdf.open()
        self.addCleanup(document.close)
        page = document.new_page(width=595, height=842)
        page.insert_text((50, 60), TITLE, fontsize=18)
        for index, text in enumerate(lines):
            page.insert_text((50, 100 + index * 18), text, fontsize=10)
        if abstract:
            page.insert_text((50, 120 + len(lines) * 18), "Abstract", fontsize=12)
            page.insert_text((50, 145 + len(lines) * 18), "Body text mentioning Alice Smith.", fontsize=10)
        return page

    def test_complete_ordered_names_and_affiliations(self):
        lines = ["Ashish Vaswani*", "Google Brain", "avaswani@google.com",
                 "Aidan N. Gomez*", "University of Toronto", "aidan@cs.toronto.edu",
                 "Illia Polosukhin*", "illia.polosukhin@gmail.com"]
        self.assertEqual(header_authors(self.page(lines), TITLE),
                         [{"full_name": "Ashish Vaswani"}, {"full_name": "Aidan N. Gomez"},
                          {"full_name": "Illia Polosukhin"}])

    def test_comma_list_and_particles_preserve_full_names(self):
        self.assertEqual(header_authors(self.page(["Alice Smith1, Bob van der Waals2 and Maria Cruz3"]), TITLE),
                         [{"full_name": "Alice Smith"}, {"full_name": "Bob van der Waals"},
                          {"full_name": "Maria Cruz"}])

    def test_initials_or_truncated_list_does_not_invent_last_author(self):
        for lines in (["Alice Smith", "B. Jones"], ["Alice Smith and Bob Jones et al."]):
            with self.subTest(lines=lines):
                self.assertEqual(header_authors(self.page(lines), TITLE), [])

    def test_missing_anchors_return_no_names(self):
        page = self.page(["Alice Smith"], abstract=False)
        self.assertEqual(header_authors(page, TITLE), [])
        self.assertEqual(header_authors(self.page(["Alice Smith"]), "Unknown article title"), [])

    def test_names_outside_header_are_excluded(self):
        page = self.page(["Alice Smith", "Google Brain", "New York"])
        page.insert_text((50, 25), "Jane Jones", fontsize=10)
        self.assertEqual(header_authors(page, TITLE), [{"full_name": "Alice Smith"}])

    def test_chinese_names_and_unicode_affiliation_markers(self):
        page = self.page(["Alice Smith"])
        page.insert_text((200, 100), "张三1、李四2", fontsize=10, fontname="china-s")
        self.assertEqual(header_authors(page, TITLE), [{"full_name": "Alice Smith"},
                         {"full_name": "张三"}, {"full_name": "李四"}])
        self.assertEqual(_clean_name("Łukasz Kaiser∗†"), "Łukasz Kaiser")
        self.assertTrue(_name("Łukasz Kaiser"))


if __name__ == "__main__":
    unittest.main()
