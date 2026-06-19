export interface PresetAoi {
  id: string;
  name: string;
  geojson: GeoJSON.Feature<GeoJSON.Polygon>;
}

export const PRESET_AOIS: PresetAoi[] = [
  {
    id: 'nw-coast-egypt',
    name: 'NW Coast Egypt',
    geojson: {
      type: 'Feature',
      properties: { name: 'NW Coast Egypt' },
      geometry: {
        type: 'Polygon',
        coordinates: [
          [
            [27.0, 31.0],
            [28.0, 31.0],
            [28.0, 31.5],
            [27.0, 31.5],
            [27.0, 31.0],
          ],
        ],
      },
    },
  },
  {
    id: 'sahara-western-egypt',
    name: 'Sahara (Western Egypt)',
    geojson: {
      type: 'Feature',
      properties: { name: 'Sahara Western Egypt' },
      geometry: {
        type: 'Polygon',
        coordinates: [
          [
            [26.0, 25.0],
            [27.0, 25.0],
            [27.0, 25.5],
            [26.0, 25.5],
            [26.0, 25.0],
          ],
        ],
      },
    },
  },
];
