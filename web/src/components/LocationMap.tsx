import { useEffect, useRef, useCallback, forwardRef, useImperativeHandle } from 'react';
import maplibregl from 'maplibre-gl';
import 'maplibre-gl/dist/maplibre-gl.css';
import { polygonAreaM2 } from '../util/geo';

export interface LocationMapHandle {
  flyTo: (lat: number, lon: number, zoom?: number) => void;
  /** Close the in-progress roof polygon (>=3 vertices) and emit its area. */
  finishRoof: () => void;
}

interface LocationMapProps {
  lat: number | null;
  lon: number | null;
  /** When true, map clicks add roof-polygon vertices instead of moving the pin. */
  drawingRoof: boolean;
  /** Clicking the map (not in roof mode) drops/moves the location pin. */
  onPick: (lat: number, lon: number) => void;
  /** Emitted when a roof polygon is finished: planar area (m²) + the ring. */
  onRoofDrawn: (areaM2: number, ring: [number, number][]) => void;
}

const ROOF_SOURCE = 'roof-src';
const ROOF_FILL = 'roof-fill';
const ROOF_LINE = 'roof-line';
const ROOF_POINT = 'roof-point';

export const LocationMap = forwardRef<LocationMapHandle, LocationMapProps>(function LocationMap(
  { lat, lon, drawingRoof, onPick, onRoofDrawn },
  ref
) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const markerRef = useRef<maplibregl.Marker | null>(null);
  const roofPts = useRef<[number, number][]>([]);

  // ---- init map (once) ----
  useEffect(() => {
    if (!containerRef.current || mapRef.current) return;
    const map = new maplibregl.Map({
      container: containerRef.current,
      style: {
        version: 8,
        sources: {
          'osm-tiles': {
            type: 'raster',
            tiles: ['https://tile.openstreetmap.org/{z}/{x}/{y}.png'],
            tileSize: 256,
            attribution: '&copy; OpenStreetMap contributors',
          },
        },
        layers: [{ id: 'osm-layer', type: 'raster', source: 'osm-tiles' }],
      },
      center: [lon ?? 20, lat ?? 25],
      zoom: lat != null && lon != null ? 13 : 2,
    });
    map.addControl(new maplibregl.NavigationControl(), 'top-right');

    map.on('load', () => {
      map.addSource(ROOF_SOURCE, { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
      map.addLayer({ id: ROOF_FILL, type: 'fill', source: ROOF_SOURCE, filter: ['==', '$type', 'Polygon'], paint: { 'fill-color': '#F59E0B', 'fill-opacity': 0.35 } });
      map.addLayer({ id: ROOF_LINE, type: 'line', source: ROOF_SOURCE, paint: { 'line-color': '#B45309', 'line-width': 2 } });
      map.addLayer({ id: ROOF_POINT, type: 'circle', source: ROOF_SOURCE, filter: ['==', '$type', 'Point'], paint: { 'circle-radius': 4, 'circle-color': '#B45309' } });
    });

    mapRef.current = map;
    return () => {
      markerRef.current?.remove();
      markerRef.current = null;
      map.remove();
      mapRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ---- keep the location pin in sync with props ----
  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    if (lat == null || lon == null) {
      markerRef.current?.remove();
      markerRef.current = null;
      return;
    }
    if (!markerRef.current) {
      markerRef.current = new maplibregl.Marker({ color: '#2563EB' }).setLngLat([lon, lat]).addTo(map);
    } else {
      markerRef.current.setLngLat([lon, lat]);
    }
  }, [lat, lon]);

  // ---- roof-polygon preview ----
  const renderRoof = useCallback(() => {
    const src = mapRef.current?.getSource(ROOF_SOURCE) as maplibregl.GeoJSONSource | undefined;
    if (!src) return;
    const pts = roofPts.current;
    const features: GeoJSON.Feature[] = pts.map((p) => ({
      type: 'Feature',
      properties: {},
      geometry: { type: 'Point', coordinates: p },
    }));
    if (pts.length >= 3) {
      features.push({
        type: 'Feature',
        properties: {},
        geometry: { type: 'Polygon', coordinates: [[...pts, pts[0]]] },
      });
    } else if (pts.length === 2) {
      features.push({
        type: 'Feature',
        properties: {},
        geometry: { type: 'LineString', coordinates: pts },
      });
    }
    src.setData({ type: 'FeatureCollection', features });
  }, []);

  const finishRoof = useCallback(() => {
    const pts = roofPts.current;
    if (pts.length >= 3) {
      const ring = [...pts, pts[0]] as [number, number][];
      onRoofDrawn(polygonAreaM2(ring), ring);
    }
    roofPts.current = [];
    renderRoof();
    const map = mapRef.current;
    if (map) map.getCanvas().style.cursor = '';
  }, [onRoofDrawn, renderRoof]);

  // ---- reset roof state + cursor when draw mode toggles ----
  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    roofPts.current = [];
    renderRoof();
    try {
      map.getCanvas().style.cursor = drawingRoof ? 'crosshair' : '';
    } catch {
      // canvas not ready yet
    }
  }, [drawingRoof, renderRoof]);

  // ---- click: roof vertex (draw mode) or drop pin (normal) ----
  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;

    function handleClick(e: maplibregl.MapMouseEvent) {
      if (drawingRoof) {
        roofPts.current.push([e.lngLat.lng, e.lngLat.lat]);
        renderRoof();
      } else {
        onPick(e.lngLat.lat, e.lngLat.lng);
      }
    }
    function handleDbl(e: maplibregl.MapMouseEvent) {
      if (!drawingRoof) return;
      e.preventDefault();
      finishRoof();
    }

    map.on('click', handleClick);
    map.on('dblclick', handleDbl);
    return () => {
      map.off('click', handleClick);
      map.off('dblclick', handleDbl);
    };
  }, [drawingRoof, onPick, renderRoof, finishRoof]);

  useImperativeHandle(ref, () => ({
    flyTo(targetLat: number, targetLon: number, zoom = 14) {
      mapRef.current?.flyTo({ center: [targetLon, targetLat], zoom, duration: 800 });
    },
    finishRoof,
  }));

  return <div ref={containerRef} className="cv-map" data-testid="cv-map" />;
});
