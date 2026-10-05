"""Unit tests for quote-provider response normalization."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


SERVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_ROOT))

from provider.provider import MockProviderAdapter, ProviderError


class MockProviderAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = MockProviderAdapter(provider="uber")

    def test_normalize_quotes_maps_optional_fields_and_defaults(self) -> None:
        quotes = self.adapter._normalize_quotes(
            {
                "quotes": [
                    {
                        "ride_type": "UberX",
                        "price": "24.80",
                        "pickup_eta_minutes": 6,
                        "trip_duration_minutes": 22,
                    }
                ]
            }
        )

        self.assertEqual(len(quotes), 1)
        self.assertEqual(quotes[0].provider, "uber")
        self.assertEqual(quotes[0].ride_type, "UberX")
        self.assertEqual(quotes[0].price, 24.8)
        self.assertEqual(quotes[0].currency, "USD")
        self.assertTrue(quotes[0].available)

    def test_normalize_quotes_rejects_missing_or_malformed_options(self) -> None:
        for body in (
            {},
            {"quotes": "not-a-list"},
            {"quotes": ["not-an-object"]},
            {"quotes": [{"ride_type": "UberX"}]},
        ):
            with self.subTest(body=body), self.assertRaises(ProviderError):
                self.adapter._normalize_quotes(body)


if __name__ == "__main__":
    unittest.main()
