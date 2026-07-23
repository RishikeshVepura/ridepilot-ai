"""Pure decision logic for background-event notifications (task 6.4).

The AI Service is the single source of truth for what the frontend sees (design
rule 11). Background events arriving on ``POST /internal/events`` — quote deltas
from the Quote Service monitoring worker and ride milestones from the Booking
Service — must be turned into the right server-pushed SSE events, and only the
*meaningful* ones should interrupt the user with a spoken notification
(Requirements 3.3, 3.4, 3.5, 4.3, 6.3).

This module holds that decision logic as small, **pure** functions with no I/O
and no side effects, mirroring the style of the producers (``monitor.compute_changes``
and ``ride_tracker.compute_transition``). The thin I/O wrapper that publishes to
the event bus and persists spoken messages lives in ``events.py``; keeping the
decision pure makes the meaningfulness rules easy to reason about and test in
isolation.

Two decisions are modeled:

  - :func:`decide_quote_notification` — given a QUOTE_DELTA's ``changes`` list
    (and, when known, the user's selected ride), decide whether the change is
    meaningful enough to push an ``ai_notification`` and speak, and compose the
    deterministic, templated spoken message (no LLM round-trip — Requirement
    3.5). The silent ``quote_update`` is always pushed by the caller regardless
    of this decision (Requirement 3.3).

  - :func:`decide_booking_notification` — map a booking lifecycle event to the
    ``booking_update`` status and, for the key ride milestones, the spoken
    ``ride_status`` message (Requirement 6.3).

Meaningfulness for quote deltas (Requirement 3.4) is, by design, derived purely
from the delta payload so the core stays dependency-light. A documented
limitation follows from that choice: the ``changes`` list only contains what
changed, not the full current quote picture, so cheapest/fastest *rank* changes
are detected **among the changed items only**. A price drop on a single option
that silently undercuts an unchanged option cannot be detected without the full
snapshot and is intentionally treated as non-meaningful (silent card update
only). The selected-ride rule (Requirement 4.3) is honored whenever the caller
can supply the selected ride; resolving that selection is the caller's concern.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from schemas.event_schemas import QuoteDeltaChange

# The single quote event type this consumer understands on /internal/events.
QUOTE_DELTA_EVENT_TYPE = "QUOTE_DELTA"

# Maps each Booking Service event type (see booking-service/models.py
# BookingEventType) to the booking *status* the frontend should reflect on a
# ``booking_update`` event. Used when the envelope does not carry an explicit
# status of its own.
_BOOKING_STATUS_BY_EVENT: dict[str, str] = {
    "BOOKING_CREATED": "CREATED",
    "FINAL_PRICE_VERIFIED": "FINAL_PRICE_VERIFIED",
    "BOOKING_CONFIRMED": "CONFIRMED",
    "DRIVER_ASSIGNED": "DRIVER_ASSIGNED",
    "DRIVER_ARRIVING": "DRIVER_ARRIVING",
    "RIDE_STARTED": "RIDE_STARTED",
    "RIDE_COMPLETED": "RIDE_COMPLETED",
    "BOOKING_CANCELLED": "CANCELLED",
    "BOOKING_FAILED": "FAILED",
}

# The set of booking event types this consumer recognizes. Used by the route to
# dispatch booking envelopes (anything else is accepted but ignored).
BOOKING_EVENT_TYPES = frozenset(_BOOKING_STATUS_BY_EVENT)

# The ride milestones the AI speaks aloud (Requirement 6.3). Deterministic,
# templated phrasing — no LLM is involved. Lifecycle steps that are not key
# milestones (e.g. BOOKING_CREATED, FINAL_PRICE_VERIFIED) update the cards via a
# ``booking_update`` but say nothing.
_BOOKING_SPOKEN_MESSAGE: dict[str, str] = {
    "DRIVER_ASSIGNED": "Your driver has been assigned.",
    "DRIVER_ARRIVING": "Your driver is arriving.",
    "RIDE_STARTED": "Your ride has started.",
    "RIDE_COMPLETED": "Your ride is complete.",
    "BOOKING_CANCELLED": "Your ride has been cancelled.",
}


@dataclass(frozen=True)
class SelectedRide:
    """The user's currently selected ride, used to honor Requirement 4.3.

    When the caller can resolve which option the user selected, it passes this so
    :func:`decide_quote_notification` can always treat a change to that option's
    price as meaningful and require re-confirmation. Matched against a change by
    ``(provider, ride_type)``.
    """

    provider: str
    ride_type: str


@dataclass(frozen=True)
class QuoteNotificationDecision:
    """The outcome of evaluating a QUOTE_DELTA for meaningfulness.

    Attributes:
        meaningful: Whether an ``ai_notification`` should be pushed and the
            message spoken (Requirement 3.4). When False the caller pushes only
            the silent ``quote_update`` and does not invoke the LLM (Requirement
            3.5).
        message: The deterministic spoken message to surface, or None when not
            meaningful.
        reason: A short machine-readable tag describing why the decision was
            reached (for logging/telemetry and tests); never user-facing.
    """

    meaningful: bool
    message: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class BookingNotificationDecision:
    """The outcome of mapping a booking event to server-pushed updates.

    Attributes:
        booking_update: Whether a ``booking_update`` event should be pushed.
        status: The booking status to carry on the ``booking_update`` (and, by
            extension, the milestone the frontend should reflect).
        spoken_message: The ``ride_status`` message to push and speak for a key
            milestone (Requirement 6.3), or None for non-milestone steps.
    """

    booking_update: bool
    status: str | None = None
    spoken_message: str | None = None


def _format_money(price: float | int | None) -> str:
    """Format a price as a ``$``-prefixed amount, tolerating odd inputs.

    QUOTE_DELTA changes do not carry a currency, so USD/``$`` is assumed (matching
    the mock providers). Non-numeric input is rendered as-is rather than raising,
    keeping the pure function total.
    """
    try:
        return f"${float(price):.2f}"
    except (TypeError, ValueError):
        return str(price)


def _format_minutes(minutes: int | None) -> str:
    """Format a pickup ETA in minutes with simple pluralization."""
    if minutes is None:
        return "an unknown number of minutes"
    unit = "minute" if minutes == 1 else "minutes"
    return f"{minutes} {unit}"


def _display_name(change: QuoteDeltaChange) -> str:
    """Build a readable option label from a change, avoiding brand repetition.

    Provider keys are lowercase (e.g. ``"uber"``) while ride types are often
    already brand-qualified (e.g. ``"UberX"``, ``"Lyft Standard"``). When the
    ride type already starts with the provider name the provider is dropped to
    avoid awkward output like "Lyft Lyft Standard"; otherwise the title-cased
    provider is prefixed.
    """
    provider = (change.provider or "").strip()
    ride_type = (change.ride_type or "").strip()
    if not provider:
        return ride_type or "your ride"
    if ride_type.lower().startswith(provider.lower()):
        return ride_type or provider.title()
    if not ride_type:
        return provider.title()
    return f"{provider.title()} {ride_type}"


def _price_changed(change: QuoteDeltaChange) -> bool:
    """Return whether a change represents a price movement for an option.

    True when the old and new prices differ, which covers an in-place price
    change as well as an option disappearing (new price None) — both of which
    matter for a *selected* ride (Requirement 4.3).
    """
    return change.old_price != change.new_price


def _new_cheapest_among_changes(
    changes: Sequence[QuoteDeltaChange],
) -> QuoteDeltaChange | None:
    """Return the change that becomes the cheapest, if the cheapest rank moved.

    Considers only the changed items (the delta carries nothing else). The option
    holding the minimum *new* price is compared to the one holding the minimum
    *old* price; when they differ the cheapest rank changed within the changed
    set and the new leader is returned. When no changed item had a prior price
    (e.g. brand-new options appeared) any new cheapest is treated as meaningful.

    Returns:
        The change that is now cheapest when the rank changed, else None.
    """
    priced_after = [c for c in changes if c.new_price is not None]
    if not priced_after:
        return None

    new_leader = min(priced_after, key=lambda c: c.new_price)  # type: ignore[arg-type,return-value]

    priced_before = [c for c in changes if c.old_price is not None]
    if not priced_before:
        # Nothing comparable existed among the changed items before — a newly
        # surfaced cheapest option is worth surfacing.
        return new_leader

    prior_leader = min(priced_before, key=lambda c: c.old_price)  # type: ignore[arg-type,return-value]
    if (new_leader.provider, new_leader.ride_type) != (
        prior_leader.provider,
        prior_leader.ride_type,
    ):
        return new_leader
    return None


def _new_fastest_among_changes(
    changes: Sequence[QuoteDeltaChange],
) -> QuoteDeltaChange | None:
    """Return the change that becomes the fastest pickup, if the rank moved.

    Mirrors :func:`_new_cheapest_among_changes` but ranks by pickup ETA. Only the
    changed items are considered (documented limitation: an unchanged option may
    still be the true fastest).

    Returns:
        The change that now has the fastest pickup when the rank changed, else
        None.
    """
    timed_after = [c for c in changes if c.new_pickup_eta_minutes is not None]
    if not timed_after:
        return None

    new_leader = min(timed_after, key=lambda c: c.new_pickup_eta_minutes)  # type: ignore[arg-type,return-value]

    timed_before = [c for c in changes if c.old_pickup_eta_minutes is not None]
    if not timed_before:
        return new_leader

    prior_leader = min(timed_before, key=lambda c: c.old_pickup_eta_minutes)  # type: ignore[arg-type,return-value]
    if (new_leader.provider, new_leader.ride_type) != (
        prior_leader.provider,
        prior_leader.ride_type,
    ):
        return new_leader
    return None


def decide_quote_notification(
    changes: Sequence[QuoteDeltaChange],
    selected_ride: SelectedRide | None = None,
) -> QuoteNotificationDecision:
    """Decide whether a QUOTE_DELTA is meaningful and compose the spoken message.

    Pure: no I/O, no side effects. The caller always pushes a silent
    ``quote_update`` for card refreshes (Requirement 3.3) independent of this
    result; this function only governs the interrupting ``ai_notification`` and
    spoken message (Requirements 3.4, 3.5).

    A change is meaningful when any of the following holds, evaluated in priority
    order:

      1. The user's *selected* ride's price changed or it became unavailable
         (Requirement 4.3 — always require re-confirmation). Only checked when
         ``selected_ride`` is supplied.
      2. A new cheapest option emerged among the changed items (the cheapest rank
         moved, or a new cheaper option appeared).
      3. A new fastest pickup emerged among the changed items.

    Otherwise the change is a small fluctuation that leaves the ranks intact and
    is **not** meaningful: the caller updates the cards silently and does not call
    the LLM or speak (Requirement 3.5).

    Args:
        changes: The validated QUOTE_DELTA change items.
        selected_ride: The user's currently selected ride, when resolvable, so a
            change to it can always be flagged (Requirement 4.3).

    Returns:
        A :class:`QuoteNotificationDecision`. When ``meaningful`` is False the
        ``message`` is None and the caller must not invoke the LLM.
    """
    if not changes:
        return QuoteNotificationDecision(meaningful=False, reason="no_changes")

    # Rule 1 — the selected ride changed: always meaningful (Requirement 4.3).
    if selected_ride is not None:
        for change in changes:
            if (
                change.provider == selected_ride.provider
                and change.ride_type == selected_ride.ride_type
                and _price_changed(change)
            ):
                if change.new_price is None:
                    message = (
                        f"Your selected {_display_name(change)} is no longer "
                        "available — want me to find another option?"
                    )
                else:
                    message = (
                        f"The price for your selected {_display_name(change)} "
                        f"changed from {_format_money(change.old_price)} to "
                        f"{_format_money(change.new_price)}. Want me to "
                        "re-confirm before booking?"
                    )
                return QuoteNotificationDecision(
                    meaningful=True,
                    message=message,
                    reason="selected_ride_price_changed",
                )

    # Rule 2 — a new cheapest option among the changed items.
    cheapest = _new_cheapest_among_changes(changes)
    if cheapest is not None:
        message = (
            f"There's a new cheapest option: {_display_name(cheapest)} at "
            f"{_format_money(cheapest.new_price)}."
        )
        return QuoteNotificationDecision(
            meaningful=True, message=message, reason="new_cheapest"
        )

    # Rule 3 — a new fastest pickup among the changed items.
    fastest = _new_fastest_among_changes(changes)
    if fastest is not None:
        message = (
            f"{_display_name(fastest)} now has the fastest pickup at "
            f"{_format_minutes(fastest.new_pickup_eta_minutes)}."
        )
        return QuoteNotificationDecision(
            meaningful=True, message=message, reason="new_fastest"
        )

    # Nothing reordered the cheapest/fastest leaders — silent update only.
    return QuoteNotificationDecision(meaningful=False, reason="rank_unchanged")


def decide_booking_notification(
    event_type: str, status: str | None = None
) -> BookingNotificationDecision:
    """Map a booking lifecycle event to the SSE updates it should produce.

    Pure: no I/O, no side effects. Every recognized booking event yields a
    ``booking_update`` carrying the resulting status so the frontend can reflect
    the booking's state; the key ride milestones additionally yield a spoken
    ``ride_status`` message (Requirement 6.3).

    Args:
        event_type: The Booking Service event type (see booking-service
            BookingEventType), e.g. ``"DRIVER_ASSIGNED"``.
        status: An explicit status from the envelope, used as-is when present;
            otherwise the status is derived from ``event_type``.

    Returns:
        A :class:`BookingNotificationDecision`. ``booking_update`` is False (and
        ``status``/``spoken_message`` None) for an unrecognized event type with
        no explicit status.
    """
    resolved_status = status or _BOOKING_STATUS_BY_EVENT.get(event_type)
    spoken_message = _BOOKING_SPOKEN_MESSAGE.get(event_type)
    return BookingNotificationDecision(
        booking_update=resolved_status is not None,
        status=resolved_status,
        spoken_message=spoken_message,
    )
