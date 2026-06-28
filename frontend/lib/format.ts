/**
 * Small presentational formatters shared by the ride-card components. Kept
 * dependency-free and defensive so a missing/odd value never throws while
 * rendering a live-updating card.
 */

/** Format a price + currency, e.g. 24.8 + "USD" => "$24.80". */
export function formatPrice(
  price: number | null | undefined,
  currency: string | null | undefined,
): string {
  if (price == null || Number.isNaN(price)) {
    return "—";
  }
  const code = (currency ?? "USD").toUpperCase();
  try {
    return new Intl.NumberFormat(undefined, {
      style: "currency",
      currency: code,
    }).format(price);
  } catch {
    // Unknown currency code — fall back to a plain amount + code.
    return `${price.toFixed(2)} ${code}`;
  }
}

/** Format a minute count, e.g. 6 => "6 min", null => "—". */
export function formatMinutes(minutes: number | null | undefined): string {
  if (minutes == null || Number.isNaN(minutes)) {
    return "—";
  }
  return `${minutes} min`;
}

/** Human-friendly provider label, e.g. "uber" => "Uber". */
export function providerLabel(provider: string): string {
  if (!provider) {
    return "Provider";
  }
  return provider.charAt(0).toUpperCase() + provider.slice(1);
}

/** Human-friendly booking status, e.g. "DRIVER_ARRIVING" => "Driver arriving". */
export function formatStatus(status: string): string {
  if (!status) {
    return "";
  }
  const lower = status.replace(/_/g, " ").toLowerCase();
  return lower.charAt(0).toUpperCase() + lower.slice(1);
}
