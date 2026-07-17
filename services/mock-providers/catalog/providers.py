"""Provider catalog — ride types and base prices for each mock provider.

Wraps the static provider configuration in a :class:`ProviderCatalog` class so
the service layer can look up whether a provider exists, list its ride types, and
resolve a single ride type's config. The price/ETA fluctuation ranges that model
"live" provider pricing are kept here as module-level constants alongside the
catalog they describe.
"""

from __future__ import annotations

# Each provider offers a set of ride types with a base price and base pickup ETA.
# Base price is in USD. Actual quotes fluctuate ±PRICE_FLUCTUATION around the base
# on each fetch.
PROVIDERS: dict[str, dict] = {
    "uber": {
        "display_name": "Uber",
        "ride_types": [
            {"ride_type": "UberX", "base_price": 24.80, "base_pickup_eta": 6, "base_trip_duration": 22},
            {"ride_type": "Uber Comfort", "base_price": 31.50, "base_pickup_eta": 8, "base_trip_duration": 22},
            {"ride_type": "Uber Black", "base_price": 48.00, "base_pickup_eta": 10, "base_trip_duration": 20},
        ],
    },
    "lyft": {
        "display_name": "Lyft",
        "ride_types": [
            {"ride_type": "Lyft Standard", "base_price": 21.90, "base_pickup_eta": 10, "base_trip_duration": 24},
            {"ride_type": "Wait & Save", "base_price": 17.40, "base_pickup_eta": 15, "base_trip_duration": 26},
            {"ride_type": "Lyft XL", "base_price": 34.20, "base_pickup_eta": 9, "base_trip_duration": 23},
        ],
    },
    "waymo": {
        "display_name": "Waymo",
        "ride_types": [
            {"ride_type": "Waymo Robotaxi", "base_price": 28.00, "base_pickup_eta": 8, "base_trip_duration": 25},
        ],
    },
}

# Price fluctuation range (fraction of base price).
PRICE_FLUCTUATION = 0.15

# ETA fluctuation range (minutes added/subtracted).
ETA_FLUCTUATION_MINUTES = 3


class ProviderCatalog:
    """Read-only lookup over the configured mock providers and their ride types."""

    def __init__(self, providers: dict[str, dict] | None = None) -> None:
        """Build the catalog.

        Args:
            providers: Optional provider config override; defaults to PROVIDERS.
        """
        self._providers = providers if providers is not None else PROVIDERS

    def is_valid(self, provider: str) -> bool:
        """Return whether a provider name is one we simulate.

        Args:
            provider: The provider key to check (e.g. "uber", "lyft", "waymo").

        Returns:
            True if the provider exists in the catalog, False otherwise.
        """
        return provider in self._providers

    def get_ride_types(self, provider: str) -> list[dict]:
        """Return the list of ride type configs for a provider.

        Args:
            provider: The provider key. Assumed valid — call is_valid first.

        Returns:
            A list of ride type dicts, each with ride_type, base_price,
            base_pickup_eta, and base_trip_duration.
        """
        return self._providers[provider]["ride_types"]

    def get_ride_type_config(self, provider: str, ride_type: str) -> dict | None:
        """Return one ride type's config for a provider, or None if not offered.

        Args:
            provider: The provider key. Assumed valid — call is_valid first.
            ride_type: The ride type name to resolve (e.g. "UberX").

        Returns:
            The ride type config dict, or None when the provider does not offer it.
        """
        return next(
            (
                rt
                for rt in self.get_ride_types(provider)
                if rt["ride_type"] == ride_type
            ),
            None,
        )
