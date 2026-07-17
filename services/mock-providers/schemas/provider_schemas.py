"""Request and response models for the mock providers API.

These Pydantic models define the shape of data flowing in and out of the mock
provider endpoints. FastAPI uses them for validation and automatic API docs.
"""

from pydantic import BaseModel


class QuoteOption(BaseModel):
    """A single ride option in a quote response.

    Fields:
        ride_type: The provider's ride type name (e.g. "UberX").
        price: Quoted price in the given currency.
        currency: ISO currency code. Defaults to "USD".
        pickup_eta_minutes: Estimated minutes until pickup.
        trip_duration_minutes: Estimated trip length in minutes.
        available: Whether this option is currently bookable.
    """

    ride_type: str
    price: float
    currency: str = "USD"
    pickup_eta_minutes: int
    trip_duration_minutes: int
    available: bool = True


class QuotesResponse(BaseModel):
    """Response body for GET /{provider}/quotes.

    Fields:
        provider: The provider these quotes belong to.
        quotes: The list of available ride options.
    """

    provider: str
    quotes: list[QuoteOption]


class BookingRequest(BaseModel):
    """Request body for POST /{provider}/bookings.

    Fields:
        ride_type: The ride type the user selected.
        pickup_lat: Pickup latitude.
        pickup_lng: Pickup longitude.
        dropoff_lat: Dropoff latitude.
        dropoff_lng: Dropoff longitude.
        idempotency_key: Unique key to dedupe retried confirmations.
    """

    ride_type: str
    pickup_lat: float
    pickup_lng: float
    dropoff_lat: float
    dropoff_lng: float
    idempotency_key: str


class BookingResponse(BaseModel):
    """Response body for POST /{provider}/bookings.

    Fields:
        provider_booking_id: The provider's ID for the new booking.
        provider: The provider that confirmed the booking.
        status: Booking status, "CONFIRMED" on success.
        ride_type: The confirmed ride type.
        final_price: The final confirmed price.
        currency: ISO currency code. Defaults to "USD".
        pickup_eta_minutes: Pickup ETA at confirmation time.
    """

    provider_booking_id: str
    provider: str
    status: str
    ride_type: str
    final_price: float
    currency: str = "USD"
    pickup_eta_minutes: int


class RideStatusResponse(BaseModel):
    """Response body for GET /{provider}/bookings/{id}/status.

    Fields:
        provider_booking_id: The booking being reported on.
        status: Current ride stage (e.g. DRIVER_ASSIGNED, RIDE_COMPLETED).
        elapsed_seconds: Seconds since confirmation. None for cancelled rides.
    """

    provider_booking_id: str
    status: str
    elapsed_seconds: int | None = None


class CancelResponse(BaseModel):
    """Response body for POST /{provider}/bookings/{id}/cancel.

    Fields:
        provider_booking_id: The cancelled booking.
        status: Always "CANCELLED" on success.
    """

    provider_booking_id: str
    status: str
