import { useEffect, useRef, useCallback, useState, forwardRef, useImperativeHandle } from 'react';
import maplibregl from 'maplibre-gl';
import 'maplibre-gl/dist/maplibre-gl.css';
import { useAppContext } from '../context/AppContext';
import { SitePopup } from './SitePopup';
import { createRoot } from 'react-dom/client';
import type { SiteFeature, LayerBounds } from '../types/api';
import { getLayerPngUrl, getLayerBounds } from '../api/client';

export interface MapHandle {
  fitAoi: (feature: GeoJSON.Feature<GeoJSON.Polygon>) => void;
  startDraw: () => void;
  stopDraw: () => void;
}

interface MapProps {
  onAoiDrawn: (feature: GeoJSON.Feature<GeoJSON.Polygon>) => void;
}

const SITES_SOURCE = 'sites-source';
const SITES_FILL_LAYER = 'sites-fill';
const SITES_OUTLINE_LAYER = 'sites-outline';
const AOI_SOURCE = 'aoi-source';
const AOI_FILL_LAYER = 'aoi-fill';
const AOI_LINE_LAYER = 'aoi-line';

export const Map = forwardRef<MapHandle, MapProps>(function Map({ onAoiDrawn }, ref) {
  const mapContainer = useRef<HTMLDivElement>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const { state, dispatch } = useAppContext();
  const [isDrawing, setIsDrawing] = useState(false);
  const drawPoints = useRef<[number, number][]>([]);
  const layerBoundsCache = useRef<Record<string, LayerBounds>>({});
  const popupRef = useRef<maplibregl.Popup | null>(null);
  const popupRootRef = useRef<ReturnType<typeof createRoot> | null>(null);

  // Initialize map
  useEffect(() => {
    if (!mapContainer.current || mapRef.current) return;

    const map = new maplibregl.Map({
      container: mapContainer.current,
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
        layers: [
          {
            id: 'osm-layer',
            type: 'raster',
            source: 'osm-tiles',
          },
        ],
      },
      center: [27.5, 28.0],
      zoom: 5,
    });

    map.addControl(new maplibregl.NavigationControl(), 'top-right');
    map.addControl(new maplibregl.ScaleControl(), 'bottom-left');

    map.on('load', () => {
      // Add AOI source/layers
      map.addSource(AOI_SOURCE, {
        type: 'geojson',
        data: { type: 'FeatureCollection', features: [] },
      });
      map.addLayer({
        id: AOI_FILL_LAYER,
        type: 'fill',
        source: AOI_SOURCE,
        paint: { 'fill-color': '#0EA5E9', 'fill-opacity': 0.15 },
      });
      map.addLayer({
        id: AOI_LINE_LAYER,
        type: 'line',
        source: AOI_SOURCE,
        paint: { 'line-color': '#0EA5E9', 'line-width': 2 },
      });

      // Add sites source/layers
      map.addSource(SITES_SOURCE, {
        type: 'geojson',
        data: { type: 'FeatureCollection', features: [] },
      });
      map.addLayer({
        id: SITES_FILL_LAYER,
        type: 'fill',
        source: SITES_SOURCE,
        paint: { 'fill-color': '#F59E0B', 'fill-opacity': 0.4 },
      });
      map.addLayer({
        id: SITES_OUTLINE_LAYER,
        type: 'line',
        source: SITES_SOURCE,
        paint: { 'line-color': '#B45309', 'line-width': 1.5 },
      });

      map.on('click', SITES_FILL_LAYER, (e) => {
        if (!e.features?.length) return;
        const feature = e.features[0] as unknown as SiteFeature;
        dispatch({ type: 'SET_SELECTED_SITE', site: feature });
        showPopup(map, feature, e.lngLat);
      });

      map.on('mouseenter', SITES_FILL_LAYER, () => {
        map.getCanvas().style.cursor = 'pointer';
      });
      map.on('mouseleave', SITES_FILL_LAYER, () => {
        map.getCanvas().style.cursor = '';
      });
    });

    mapRef.current = map;

    return () => {
      if (popupRef.current) {
        popupRef.current.remove();
      }
      map.remove();
      mapRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function showPopup(
    map: maplibregl.Map,
    site: SiteFeature,
    lngLat: maplibregl.LngLat
  ) {
    if (popupRef.current) {
      popupRef.current.remove();
    }
    if (popupRootRef.current) {
      popupRootRef.current.unmount();
    }

    const el = document.createElement('div');
    const root = createRoot(el);
    popupRootRef.current = root;

    const popup = new maplibregl.Popup({ closeButton: false, maxWidth: '300px' })
      .setLngLat(lngLat)
      .setDOMContent(el)
      .addTo(map);

    root.render(
      <SitePopup
        props={site.properties}
        testIdPrefix="map-site"
        onClose={() => {
          popup.remove();
          dispatch({ type: 'SET_SELECTED_SITE', site: null });
        }}
      />
    );

    popupRef.current = popup;
  }

  // Update AOI on map
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !map.isStyleLoaded()) return;
    const src = map.getSource(AOI_SOURCE) as maplibregl.GeoJSONSource | undefined;
    if (!src) return;
    if (state.aoi) {
      src.setData(state.aoi as GeoJSON.Feature);
    } else {
      src.setData({ type: 'FeatureCollection', features: [] });
    }
  }, [state.aoi]);

  // Update sites on map
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !map.isStyleLoaded()) return;
    const src = map.getSource(SITES_SOURCE) as maplibregl.GeoJSONSource | undefined;
    if (!src) return;
    if (state.sites) {
      src.setData(state.sites as GeoJSON.FeatureCollection);
    } else {
      src.setData({ type: 'FeatureCollection', features: [] });
    }
  }, [state.sites]);

  // Manage image overlay layers
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !map.isStyleLoaded()) return;

    async function updateLayers() {
      if (!map) return;
      // Remove layers no longer needed
      const currentImageLayers = map
        .getStyle()
        .layers.filter((l) => l.id.startsWith('img-overlay-'));
      for (const layer of currentImageLayers) {
        const layerName = layer.id.replace('img-overlay-', '');
        if (!state.visibleLayers.has(layerName)) {
          if (map.getLayer(layer.id)) map.removeLayer(layer.id);
          if (map.getSource(`img-src-${layerName}`)) map.removeSource(`img-src-${layerName}`);
        }
      }

      // Add new visible layers
      if (!state.jobId) return;
      for (const layerName of state.visibleLayers) {
        const layerId = `img-overlay-${layerName}`;
        if (map.getLayer(layerId)) continue;

        try {
          let bounds = layerBoundsCache.current[layerName];
          if (!bounds) {
            bounds = await getLayerBounds(state.jobId, layerName);
            layerBoundsCache.current[layerName] = bounds;
          }
          const url = getLayerPngUrl(state.jobId, layerName);
          const sourceId = `img-src-${layerName}`;

          if (!map.getSource(sourceId)) {
            map.addSource(sourceId, {
              type: 'image',
              url,
              coordinates: [
                [bounds.west, bounds.north],
                [bounds.east, bounds.north],
                [bounds.east, bounds.south],
                [bounds.west, bounds.south],
              ],
            });
          }

          // Insert before sites layers so sites appear on top
          map.addLayer(
            {
              id: layerId,
              type: 'raster',
              source: sourceId,
              paint: { 'raster-opacity': 0.75 },
            },
            SITES_FILL_LAYER
          );
        } catch {
          console.warn(`Failed to load layer ${layerName}`);
        }
      }
    }

    void updateLayers();
  }, [state.visibleLayers, state.jobId]);

  // Show popup when selectedSite changes from outside map (e.g. ranking table click)
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !map.isStyleLoaded()) return;
    if (!state.selectedSite) {
      if (popupRef.current) {
        popupRef.current.remove();
        popupRef.current = null;
      }
      return;
    }
    const site = state.selectedSite;
    const lngLat = new maplibregl.LngLat(
      site.properties.centroid_lon,
      site.properties.centroid_lat
    );
    showPopup(map, site, lngLat);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state.selectedSite]);

  // Drawing mode
  const startDraw = useCallback(() => {
    const map = mapRef.current;
    if (!map) return;
    setIsDrawing(true);
    drawPoints.current = [];
    map.getCanvas().style.cursor = 'crosshair';
  }, []);

  const stopDraw = useCallback(() => {
    const map = mapRef.current;
    if (!map) return;
    setIsDrawing(false);
    drawPoints.current = [];
    map.getCanvas().style.cursor = '';
  }, []);

  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;

    function handleClick(e: maplibregl.MapMouseEvent) {
      if (!isDrawing) return;
      drawPoints.current.push([e.lngLat.lng, e.lngLat.lat]);
    }

    function handleDblClick(e: maplibregl.MapMouseEvent) {
      if (!isDrawing) return;
      e.preventDefault();
      if (drawPoints.current.length >= 3) {
        const coords = [
          ...drawPoints.current,
          drawPoints.current[0],
        ] as [number, number][];
        const feature: GeoJSON.Feature<GeoJSON.Polygon> = {
          type: 'Feature',
          properties: {},
          geometry: { type: 'Polygon', coordinates: [coords] },
        };
        onAoiDrawn(feature);
      }
      setIsDrawing(false);
      drawPoints.current = [];
      if (map) map.getCanvas().style.cursor = '';
    }

    map.on('click', handleClick);
    map.on('dblclick', handleDblClick);

    return () => {
      map.off('click', handleClick);
      map.off('dblclick', handleDblClick);
    };
  }, [isDrawing, onAoiDrawn]);

  // Expose imperative handle
  useImperativeHandle(ref, () => ({
    fitAoi(feature: GeoJSON.Feature<GeoJSON.Polygon>) {
      const map = mapRef.current;
      if (!map) return;
      const coords = feature.geometry.coordinates[0];
      const lngs = coords.map((c) => c[0]);
      const lats = coords.map((c) => c[1]);
      map.fitBounds(
        [
          [Math.min(...lngs), Math.min(...lats)],
          [Math.max(...lngs), Math.max(...lats)],
        ],
        { padding: 60, duration: 800 }
      );
    },
    startDraw,
    stopDraw,
  }));

  return (
    <div ref={mapContainer} className="map-container" data-testid="map-container" />
  );
});
