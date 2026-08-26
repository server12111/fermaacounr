import math
import unittest
from unittest.mock import patch

from handlers import _validate_telegram_link


class LinkValidationTests(unittest.TestCase):
    def test_accepts_public_post(self):
        self.assertEqual(
            _validate_telegram_link("https://t.me/example/12", post=True),
            "https://t.me/example/12",
        )

    def test_accepts_private_thread_post(self):
        self.assertEqual(
            _validate_telegram_link("https://t.me/c/123/9/55", post=True),
            "https://t.me/c/123/9/55",
        )

    def test_rejects_embedded_fake_domain(self):
        with self.assertRaises(ValueError):
            _validate_telegram_link("https://evil.example/t.me/example/12", post=True)

    def test_rejects_channel_without_post_id(self):
        with self.assertRaises(ValueError):
            _validate_telegram_link("https://t.me/example", post=True)
