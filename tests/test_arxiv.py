"""arXiv source detection and Atom validation without live service requests."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from paper_renamer.arxiv import arxiv_metadata, detect_arxiv


TITLE = "Attention Is All You Need"


def feed(title=TITLE, identity="1706.03762v7", authors=("Ashish Vaswani", "Illia Polosukhin")):
    names = "".join("<author><name>" + name + "</name></author>" for name in authors)
    return ('<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>http://arxiv.org/abs/' +
            identity + '</id><title>' + title + '</title><published>2017-06-12T17:57:34Z</published>'
            '<updated>2023-08-02T00:00:00Z</updated>' + names + '</entry></feed>').encode()


class DetectionTests(unittest.TestCase):
    def test_split_label_id_category_and_date(self):
        for stamp in ("arXiv:\n1706.03762v7\n[cs.CL]\n2 Aug 2023",
                      "arXiv:1706.\n03762v7 [cs.CL] 2 Aug 2023",
                      "arXiv：1706.03762v7 [cs.CL] 2 Aug 2023"):
            with self.subTest(stamp=stamp):
                self.assertEqual(detect_arxiv(TITLE + "\n" + stamp), "1706.03762v7")

    def test_html_and_pdf_url_parameters(self):
        for url in ("https://arxiv.org/html/1706.03762v7", "https://arxiv.org/pdf/1706.03762v7?download=1"):
            self.assertEqual(detect_arxiv(url), "1706.03762v7")

    def test_modern_rotated_margin_stamp(self):
        text = TITLE + "\nAbstract\nThe article body.\narXiv:1706.03762v7 [cs.CL] 2 Aug 2023"
        self.assertEqual(detect_arxiv(text), "1706.03762v7")

    def test_legacy_identifier(self):
        self.assertEqual(detect_arxiv("arXiv:hep-th/9901001v2  12 Jan 1999\nArticle title"), "hep-th/9901001v2")

    def test_arxiv_doi_and_standalone_url(self):
        for text in ("https://doi.org/10.48550/arXiv.1706.03762", "doi: 10.48550/arxiv.1706.03762",
                     "https://arxiv.org/pdf/1706.03762v7.pdf"):
            with self.subTest(text=text):
                self.assertTrue(detect_arxiv(text).startswith("1706.03762"))

    def test_native_stream_can_recover_stamp(self):
        self.assertEqual(detect_arxiv("interleaved text arXiv:1706.03762v7", "arXiv:1706.03762v7 [cs.CL] 2 Aug 2023"), "1706.03762v7")

    def test_citation_or_prose_is_not_own_source(self):
        for text in ("We compare with arXiv:1706.03762.", "[1] Vaswani et al. arXiv:1706.03762",
                     "References\n[1] Attention Is All You Need\narXiv:1706.03762",
                     "References\nhttps://arxiv.org/abs/1706.03762", "arXiv:1706.03762 was used as a baseline"):
            with self.subTest(text=text):
                self.assertEqual(detect_arxiv(text), "")

    def test_conflicting_identifiers_are_not_guessed(self):
        self.assertEqual(detect_arxiv("arXiv:1706.03762\narXiv:2301.12345"), "")


class MetadataTests(unittest.TestCase):
    def test_full_ordered_author_names_returned_after_title_match(self):
        with patch("paper_renamer.arxiv._request_feed", return_value=feed()):
            record, warnings = arxiv_metadata("1706.03762v7", TITLE, TITLE + "\nAbstract")
        self.assertEqual(warnings, [])
        self.assertEqual(record["author"], [{"full_name": "Ashish Vaswani"}, {"full_name": "Illia Polosukhin"}])
        self.assertEqual(record["short-container-title"], ["arxiv"])
        self.assertEqual(record["published"], {"date-parts": [[2017, 6, 12]]})

    def test_wrong_title_id_and_version_are_rejected(self):
        for raw in (feed(title="An unrelated experimental study"), feed(identity="2301.12345v7"),
                    feed(identity="1706.03762v6")):
            with self.subTest(raw=raw):
                with patch("paper_renamer.arxiv._request_feed", return_value=raw):
                    record, warnings = arxiv_metadata("1706.03762v7", TITLE, TITLE + "\nAbstract")
                self.assertEqual(record, {})
                self.assertTrue(warnings)

    def test_service_failure_and_unsafe_xml_fall_back(self):
        with patch("paper_renamer.arxiv._request_feed", side_effect=OSError("offline")):
            self.assertEqual(arxiv_metadata("1706.03762", TITLE, TITLE)[0], {})
        raw = b'<!DOCTYPE feed [<!ENTITY x "unsafe">]><feed/>'
        with patch("paper_renamer.arxiv._request_feed", return_value=raw):
            self.assertEqual(arxiv_metadata("1706.03762", TITLE, TITLE)[0], {})


if __name__ == "__main__":
    unittest.main()
