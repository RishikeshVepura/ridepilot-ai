"use client";

/**
 * RouteMapView — the actual Leaflet map, rendered client-only (Leaflet needs the
 * DOM/`window`, so this is loaded via a dynamic `ssr: false` import in
 * RouteMap.tsx). Shows the pickup and dropoff as two markers joined by a dashed
 * line over free OpenStreetMap tiles (no API key required).
 *
 * Until geocoding exists the coordinates are fixed test values, so the same
 * route renders regardless of the typed address — this is intentional for the
 * prototype.
 */

import { useEffect } from "react";
import L from "leaflet";
import {
  MapContainer,
  Marker,
  Polyline,
  Popup,
  TileLayer,
  useMap,
} from "react-leaflet";
import "leaflet/dist/leaflet.css";

import type { MapRoute } from "@/lib/types";

/**
 * Build a small round pin as a div icon. Using a div icon avoids Leaflet's
 * default marker images, which break under bundlers, and lets pickup/dropoff be
 * colour-coded.
 */
function pin(color: string): L.DivIcon {
  return L.divIcon({
    className: "route-map__marker",
    html: `<span class="route-map__pin" style="--pin:${color}"></span>`,
    iconSize: [20, 20],
    iconAnchor: [10, 10],
    popupAnchor: [0, -10],
  });
}

const PICKUP_ICON = pin("#2ddf9b");
const DROPOFF_ICON = pin("#5b8cff");

/** Fit the map to both points whenever the route changes. */
function FitBounds({ route }: { route: MapRoute }) {
  const map = useMap();
  useEffect(() => {
    const bounds = L.latLngBounds(
      [route.pickup.lat, route.pickup.lng],
      [route.dropoff.lat, route.dropoff.lng],
    );
    map.fitBounds(bounds, { padding: [48, 48], maxZoom: 15 });
  }, [map, route.updatedAt]);
  return null;
}

export default function RouteMapView({ route }: { route: MapRoute }) {
  const pickup: [number, number] = [route.pickup.lat, route.pickup.lng];
  const dropoff: [number, number] = [route.dropoff.lat, route.dropoff.lng];
  const center: [number, number] = [
    (pickup[0] + dropoff[0]) / 2,
    (pickup[1] + dropoff[1]) / 2,
  ];

  return (
    <MapContainer
      center={center}
      zoom={13}
      scrollWheelZoom={false}
      className="route-map__canvas"
    >
      <TileLayer
        attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
        url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
      />
      <Polyline
        positions={[pickup, dropoff]}
        pathOptions={{ color: "#5b8cff", weight: 3, dashArray: "6 8" }}
      />
      <Marker position={pickup} icon={PICKUP_ICON}>
        <Popup>{route.pickup.label}</Popup>
      </Marker>
      <Marker position={dropoff} icon={DROPOFF_ICON}>
        <Popup>{route.dropoff.label}</Popup>
      </Marker>
      <FitBounds route={route} />
    </MapContainer>
  );
}
