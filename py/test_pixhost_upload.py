"""Unit tests for PiXhost URL helpers (no network)."""

from __future__ import annotations

import unittest

import pixhost_upload


class PixhostUrlTests(unittest.TestCase):
    def test_direct_from_show_cc(self):
        show = "https://pixhost.cc/show/8582/563_preview.gif"
        self.assertEqual(
            pixhost_upload.direct_url_from_show_url(show),
            "https://img1.pixhost.cc/images/8582/563_preview.gif",
        )

    def test_direct_from_show_to(self):
        show = "https://pixhost.to/show/12/34_image.jpg"
        self.assertEqual(
            pixhost_upload.direct_url_from_show_url(show),
            "https://img1.pixhost.to/images/12/34_image.jpg",
        )

    def test_direct_from_thumb(self):
        thumb = "https://t2.pixhost.cc/thumbs/10035/758104882_125-b4ayzotd.gif"
        self.assertEqual(
            pixhost_upload.direct_url_from_thumb_url(thumb),
            "https://img2.pixhost.cc/images/10035/758104882_125-b4ayzotd.gif",
        )

    def test_direct_from_response_prefers_thumb(self):
        raw = {
            "show_url": "https://pixhost.cc/show/10035/758107332_125.gif",
            "th_url": "https://t2.pixhost.cc/thumbs/10035/758107332_125.gif",
        }
        self.assertEqual(
            pixhost_upload.direct_url_from_response(raw),
            "https://img2.pixhost.cc/images/10035/758107332_125.gif",
        )

    def test_bbcode_strips_md_gif(self):
        raw = {
            "show_url": "https://pixhost.cc/show/1/2_anim.md.gif",
            "th_url": "https://t1.pixhost.cc/thumbs/1/2_anim.md.gif",
        }
        bb = pixhost_upload.bbcode_from_response(raw)
        self.assertEqual(bb, "[IMG]https://img1.pixhost.cc/images/1/2_anim.gif[/IMG]")

    def test_content_type_defaults_nsfw(self):
        self.assertEqual(pixhost_upload._pixhost_content_type(), "1")


if __name__ == "__main__":
    unittest.main()
