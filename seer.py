#!/bin/python3
"""OpenCV-based ship detection for MarineTraffic screenshots.

Detects two marker shapes on the map:
  - Circles (dots)    → stationary ships
  - Triangles (arrows) → moving ships

Both shapes use the same color coding:
  - Red   → tankers
  - Green → cargo ships

Detection pipeline: HSV color masking → contour finding → shape classification
via cv2.approxPolyDP (vertex count) and circularity ratio.
"""

import math
import sys
import cv2
import numpy as np

########
# Define color ranges
# Red (in HSV, red wraps around 0 and 180)
lower_red1 = np.array([0, 120, 100])
upper_red1 = np.array([10, 255, 255])
lower_red2 = np.array([160, 120, 100])
upper_red2 = np.array([179, 255, 255])

# Green — S≥55 catches pale/washed-out markers at low zoom while hue range [40-90]
# prevents teal/olive false positives
lower_green = np.array([40, 55, 80])
upper_green = np.array([90, 255, 255])

# Contour area bounds (in pixels²) — 10 catches markers down to ~4px diameter, 10000 filters UI blobs
MIN_MARKER_AREA = 10
MAX_MARKER_AREA = 10000

# Shape classification thresholds
CIRCULARITY_THRESHOLD = 0.55   # 4π·area/perimeter² above this → circle
POLY_EPSILON_FACTOR = 0.04     # approxPolyDP epsilon as fraction of arc length

# Morphological kernel for noise removal (opening = erosion → dilation)
# 2×2 preserves markers down to ~4px while removing single-pixel noise/JPEG debris
_MORPH_KERNEL = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2, 2))


########
# Shape-based marker detection (replaces HoughCircles)

def detect_markers(mask, original_img=None, color_name=""):
    """Detect circles (stationary) and triangles (moving) on a binary color mask.

    Uses contour detection + polygon approximation:
      - 3 vertices  → triangle (arrow marker) → moving ship
      - High circularity → circle (dot marker) → stationary ship

    Returns (stationary_count, moving_count).
    """
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    stationary = 0
    moving = 0

    for cnt in contours:
        area = cv2.contourArea(cnt)
        if area < MIN_MARKER_AREA or area > MAX_MARKER_AREA:
            continue
        perimeter = cv2.arcLength(cnt, True)
        if perimeter == 0:
            continue

        approx = cv2.approxPolyDP(cnt, POLY_EPSILON_FACTOR * perimeter, True)
        n_vertices = len(approx)
        circularity = 4 * math.pi * area / (perimeter * perimeter)

        if circularity < CIRCULARITY_THRESHOLD:
            # Low circularity → arrow/triangle → moving ship
            moving += 1
            if original_img is not None:
                cv2.drawContours(original_img, [cnt], -1, (0, 255, 255), 2)
                M = cv2.moments(cnt)
                if M["m00"] > 0:
                    cx = int(M["m10"] / M["m00"])
                    cy = int(M["m01"] / M["m00"])
                    cv2.putText(original_img, f"{color_name}(mov)",
                                (cx - 20, cy - 10),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 255, 255), 1)
        else:
            # High circularity → circle/dot → stationary ship
            stationary += 1
            if original_img is not None:
                M = cv2.moments(cnt)
                if M["m00"] > 0:
                    cx = int(M["m10"] / M["m00"])
                    cy = int(M["m01"] / M["m00"])
                    cv2.circle(original_img, (cx, cy), 8, (0, 255, 0), 2)
                    cv2.putText(original_img, color_name,
                                (cx - 10, cy - 10),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 255, 0), 1)

    return (stationary, moving)


########
# Convenience wrappers

def _build_masks(image_hsv):
    """Build red (tanker) and green (cargo) binary masks from an HSV image.

    Applies morphological opening (erosion → dilation) to each mask to
    remove small noise blobs, JPEG artifacts, and anti-aliasing debris
    while preserving the shape of real ship markers.
    """
    mask_red1 = cv2.inRange(image_hsv, lower_red1, upper_red1)
    mask_red2 = cv2.inRange(image_hsv, lower_red2, upper_red2)
    mask_red = cv2.bitwise_or(mask_red1, mask_red2)
    mask_red = cv2.morphologyEx(mask_red, cv2.MORPH_OPEN, _MORPH_KERNEL)

    mask_green = cv2.inRange(image_hsv, lower_green, upper_green)
    mask_green = cv2.morphologyEx(mask_green, cv2.MORPH_OPEN, _MORPH_KERNEL)

    return mask_red, mask_green


def show(image):
    cv2.imshow("Ships", image)
    while True:
        key = cv2.waitKey(0)
        if key == ord('q'):
            break
    cv2.destroyAllWindows()


def count_ships(image):
    """Detect ships in a BGR image.

    Returns dict:
        {
            "stationary_tankers": int,
            "moving_tankers": int,
            "stationary_cargos": int,
            "moving_cargos": int,
        }
    """
    image_hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    mask_red, mask_green = _build_masks(image_hsv)

    stat_red, mov_red = detect_markers(mask_red, image, "Red")
    stat_green, mov_green = detect_markers(mask_green, image, "Green")

    return {
        "stationary_tankers": stat_red,
        "moving_tankers": mov_red,
        "stationary_cargos": stat_green,
        "moving_cargos": mov_green,
    }

def count_ships_from_bytes(img_bytes):
    """Detect ships from raw image bytes (PNG or JPEG) in memory.

    Returns dict with stationary/moving breakdown per ship type.
    Used by the scraper for inline detection so images don't need to be saved.
    """
    arr = np.frombuffer(img_bytes, dtype=np.uint8)
    image = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if image is None:
        return {
            "stationary_tankers": 0, "moving_tankers": 0,
            "stationary_cargos": 0, "moving_cargos": 0,
        }
    return count_ships(image)


def extract_marker_coords(img_bytes, center_lat, center_lon, zoom,
                          viewport_w, viewport_h):
    """Detect ships and return their approximate lat/lon positions.

    Each marker is converted from pixel (x, y) → geographic coordinates
    using the tile's center and the Mercator projection scale at the
    given zoom level.

    Returns list of dicts:
        [{"lat": ..., "lon": ..., "type": "tanker"|"cargo",
          "motion": "stationary"|"moving"}]
    """
    arr = np.frombuffer(img_bytes, dtype=np.uint8)
    image = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if image is None:
        return []

    image_hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    h, w = image.shape[:2]

    total_px = 256 * (2 ** zoom)

    # Center pixel position in the global Mercator grid
    cx_px = (center_lon + 180) / 360.0 * total_px
    lat_rad = math.radians(center_lat)
    cy_px = (1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * total_px

    def _pixel_to_latlon(px_x, px_y):
        """Convert image pixel coords to lat/lon via global Mercator."""
        gx = cx_px + (px_x - w / 2)
        gy = cy_px + (px_y - h / 2)
        lon = gx / total_px * 360.0 - 180.0
        merc_y = gy / total_px
        lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * merc_y))))
        return round(lat, 5), round(lon, 5)

    def _find_markers(mask, ship_type):
        """Find contours on mask, classify as stationary/moving, geolocate."""
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL,
                                       cv2.CHAIN_APPROX_SIMPLE)
        results = []
        for cnt in contours:
            area = cv2.contourArea(cnt)
            if area < MIN_MARKER_AREA or area > MAX_MARKER_AREA:
                continue
            perimeter = cv2.arcLength(cnt, True)
            if perimeter == 0:
                continue

            approx = cv2.approxPolyDP(cnt, POLY_EPSILON_FACTOR * perimeter, True)
            circularity = 4 * math.pi * area / (perimeter * perimeter)

            if circularity < CIRCULARITY_THRESHOLD:
                motion = "moving"
            else:
                motion = "stationary"

            M = cv2.moments(cnt)
            if M["m00"] == 0:
                continue
            mx = M["m10"] / M["m00"]
            my = M["m01"] / M["m00"]
            lat, lon = _pixel_to_latlon(mx, my)
            results.append({
                "lat": lat,
                "lon": lon,
                "type": ship_type,
                "motion": motion,
            })
        return results

    mask_red, mask_green = _build_masks(image_hsv)
    markers = _find_markers(mask_red, "tanker") + _find_markers(mask_green, "cargo")
    return markers


# --- Unified detection (single-pass) ------------------------------------------

def detect_ships_from_bytes(img_bytes, center_lat, center_lon, zoom,
                            viewport_w, viewport_h, center_offset=None):
    """Detect ships in one pass: counts + geo-coordinates.

    Combines the work of count_ships_from_bytes() and extract_marker_coords()
    into a single decode → HSV → mask → contour pipeline, avoiding duplicate
    processing.

    ``center_offset`` (optional dict with ``center_x``, ``center_y``, ``dpr``)
    corrects for UI chrome offsets and device-pixel-ratio differences between
    the CSS layout and the actual screenshot resolution.

    Returns (counts_dict, markers_list, image_shape):
      - counts: {"stationary_tankers", "moving_tankers",
                 "stationary_cargos", "moving_cargos"}
      - markers: [{"lat", "lon", "type", "motion"}, ...]
      - image_shape: (height, width) of the decoded screenshot, or (0, 0)
                     if decoding failed.
    """
    empty_counts = {
        "stationary_tankers": 0, "moving_tankers": 0,
        "stationary_cargos": 0, "moving_cargos": 0,
    }

    arr = np.frombuffer(img_bytes, dtype=np.uint8)
    image = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if image is None:
        return empty_counts, [], (0, 0)

    image_hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    h, w = image.shape[:2]
    mask_red, mask_green = _build_masks(image_hsv)

    # Mercator projection setup
    total_px = 256 * (2 ** zoom)
    cx_px = (center_lon + 180) / 360.0 * total_px
    lat_rad = math.radians(center_lat)
    cy_px = (1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad))
             / math.pi) / 2.0 * total_px

    # Determine map-centre pixel in the *image* coordinate system.
    # center_offset (from Leaflet) gives the CSS-pixel position of the map
    # centre within #map_canvas; multiply by DPR to get image pixels.
    if center_offset and center_offset.get("dpr"):
        dpr = center_offset["dpr"]
        img_center_x = center_offset["center_x"] * dpr
        img_center_y = center_offset["center_y"] * dpr
    else:
        dpr = 1
        img_center_x = w / 2
        img_center_y = h / 2

    def _pixel_to_latlon(px_x, px_y):
        # px_x/px_y are image pixels (cv2 output); cx_px/cy_px are Mercator
        # pixels (== CSS pixels at the displayed zoom). Divide the offset by
        # dpr so the units line up — otherwise markers are over-shifted by
        # exactly the device-pixel-ratio.
        gx = cx_px + (px_x - img_center_x) / dpr
        gy = cy_px + (px_y - img_center_y) / dpr
        lon = gx / total_px * 360.0 - 180.0
        merc_y = gy / total_px
        lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * merc_y))))
        return round(lat, 5), round(lon, 5)

    def _process_mask(mask, ship_type):
        """Count + geolocate markers on one color mask in a single pass."""
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL,
                                       cv2.CHAIN_APPROX_SIMPLE)
        stationary = 0
        moving = 0
        markers = []
        for cnt in contours:
            area = cv2.contourArea(cnt)
            if area < MIN_MARKER_AREA or area > MAX_MARKER_AREA:
                continue
            perimeter = cv2.arcLength(cnt, True)
            if perimeter == 0:
                continue

            circularity = 4 * math.pi * area / (perimeter * perimeter)

            if circularity < CIRCULARITY_THRESHOLD:
                motion = "moving"
                moving += 1
            else:
                motion = "stationary"
                stationary += 1

            M = cv2.moments(cnt)
            if M["m00"] == 0:
                continue
            mx = M["m10"] / M["m00"]
            my = M["m01"] / M["m00"]
            lat, lon = _pixel_to_latlon(mx, my)
            markers.append({
                "lat": lat, "lon": lon,
                "type": ship_type, "motion": motion,
            })
        return stationary, moving, markers

    stat_red, mov_red, markers_red = _process_mask(mask_red, "tanker")
    stat_green, mov_green, markers_green = _process_mask(mask_green, "cargo")

    counts = {
        "stationary_tankers": stat_red,
        "moving_tankers": mov_red,
        "stationary_cargos": stat_green,
        "moving_cargos": mov_green,
    }
    return counts, markers_red + markers_green, (h, w)


def _debug_center_check(req_lat, req_lon, center_offset, row, col, logger):
    """Log whether Leaflet's actual map center matches the requested pan target.

    Compares the lat/lon returned by ``map.getCenter()`` (carried in
    ``center_offset["map_lat"]/["map_lng"]``) against the lat/lon that was
    passed to ``setView()``. Any non-trivial delta means a pan was silently
    rounded or rejected — which would bias every marker on this tile.
    """
    act_lat = center_offset.get("map_lat")
    act_lng = center_offset.get("map_lng")
    if act_lat is None or act_lng is None:
        return

    dlat = abs(act_lat - req_lat)
    dlon = abs(act_lng - req_lon)
    if dlat > 1e-4 or dlon > 1e-4:
        logger.warning("  Tile (%d,%d): setView drift! "
                       "requested (%.6f, %.6f), actual (%.6f, %.6f), "
                       "delta (%.6f, %.6f)",
                       row, col, req_lat, req_lon, act_lat, act_lng,
                       dlat, dlon)
    else:
        logger.debug("  Tile (%d,%d): setView OK — actual (%.6f, %.6f)",
                     row, col, act_lat, act_lng)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("SEER -- detect stationary and moving tankers/cargo ships on a picture")
        print("Stationary ships: circles/dots  |  Moving ships: triangles/arrows")
        print("Tankers = red markers  |  Cargo = green markers")
        print("Usage: ./seer.py image1 image2 ... imageN")
        exit(0)

    for arg in sys.argv[1:]:
        image = cv2.imread(arg)

        is_north = arg[0] == 'N'
        date_time = arg.split('.')[0][1:]

        if image is None:
            print(f"Image {arg} doesn't exit")
            exit(1)

        result = count_ships(image)
        st = result["stationary_tankers"]
        mt = result["moving_tankers"]
        sc = result["stationary_cargos"]
        mc = result["moving_cargos"]

        # Machine-digestible format (backward-compatible totals + new breakdown)
        total_tankers = st + mt
        total_cargo = sc + mc
        print(f"{arg}:{is_north}:{date_time}:{total_tankers}:{total_cargo}"
              f":{st}:{mt}:{sc}:{mc}")
