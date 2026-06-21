/** Planar area in m² of a small lon/lat polygon ring.
 *
 *  For roof-scale polygons (tens of m²) a spherical-excess formula loses
 *  precision, so we project to a local equirectangular plane centred on the
 *  polygon's mean latitude (x scaled by cos(lat)) and apply the shoelace
 *  formula. Accurate to well under 1% for areas up to a few km².
 *  `coords` is an array of [lon, lat] pairs (open or closed ring). */
export function polygonAreaM2(coords: [number, number][]): number {
  const ring = coords.length > 1 &&
    coords[0][0] === coords[coords.length - 1][0] &&
    coords[0][1] === coords[coords.length - 1][1]
    ? coords.slice(0, -1)
    : coords;
  if (ring.length < 3) return 0;
  const R = 6378137; // WGS84 equatorial radius (m)
  const lat0 = ((ring.reduce((s, c) => s + c[1], 0) / ring.length) * Math.PI) / 180;
  const cosLat0 = Math.cos(lat0);
  const pts = ring.map(([lon, lat]) => [
    ((lon * Math.PI) / 180) * R * cosLat0,
    ((lat * Math.PI) / 180) * R,
  ]);
  let area = 0;
  for (let i = 0; i < pts.length; i++) {
    const j = (i + 1) % pts.length;
    area += pts[i][0] * pts[j][1] - pts[j][0] * pts[i][1];
  }
  return Math.abs(area / 2);
}
