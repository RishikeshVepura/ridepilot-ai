"""Provider configuration — ride types and base prices for each mock provider."""

# Each provider offers a set of ride types with a base price and base pickup ETA.
# Base price is in USD. Actual quotes fluctuate ±15% around the base on each fetch.

PROVIDERS = {
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

# Price fluctuation range (fraction of base price)
PRICE_FLUCTUATION = 0.15

# ETA fluctuation range (minutes added/subtracted)
ETA_FLUCTUATION_MINUTES = 3


def is_valid_provider(provider: str) -> bool:
    """Check whether a provider name is one we simulate.

    Args:
        provider: The provider key to check (e.g. "uber", "lyft", "waymo").

    Returns:
        True if the provider exists in the PROVIDERS config, False otherwise.
    """
    return provider in PROVIDERS


def get_ride_types(provider: str) -> list[dict]:
    """Return the list of ride type configs for a provider.

    Args:
        provider: The provider key. Assumed valid — call is_valid_provider first.

    Returns:
        A list of ride type dicts, each containing ride_type, base_price,
        base_pickup_eta, and base_trip_duration.
    """
    return PROVIDERS[provider]["ride_types"]
