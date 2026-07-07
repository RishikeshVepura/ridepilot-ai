"use client";

/**
 * RouteMap — the side-pane map card. Renders nothing until a `route_map` event
 * has arrived. The heavy Leaflet map (RouteMapView) is loaded with a dynamic
 * `ssr: false` import so it only ever runs in the browser, where the DOM/`window`
 * it needs exist.
 */

import dynamic from "next/dynamic";

import type { MapRoute } from "@/lib/types";

const RouteMapView = dynamic(() => import("./RouteMapView"), {
  ssr: false,
  loading: () => <div className="route-map__placeholder">Loading map…</div>,
});

export function RouteMap({ route }: { route: MapRoute | null }) {
  if (!route) {
    return null;
  }

  return (
    <section className="route-map" aria-label="Route map">
      <header className="route-map__header">
        <span className="route-map__legend">
          <span className="route-map__swatch route-map__swatch--pickup" />
          Pickup
        </span>
        <span className="route-map__legend">
          <span className="route-map__swatch route-map__swatch--dropoff" />
          Dropoff
        </span>
      </header>
      <RouteMapView route={route} />
    </section>
  );
}
