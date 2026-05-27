"""Runtime diagnostics for MarineTraffic map/Leaflet availability."""

import json
import re
from datetime import datetime, timezone
from pathlib import Path


def run_map_probe(page):
    """Return a JSON-serializable snapshot of map/DOM capabilities."""
    return page.evaluate(
        """
        () => {
            const methodNames = [
                'setView',
                'getCenter',
                'latLngToContainerPoint',
                'getContainer',
            ];

            const rectOf = (el) => {
                if (!el) return null;
                const r = el.getBoundingClientRect();
                return { x: r.x, y: r.y, w: r.width, h: r.height };
            };

            const mtMap = window.__mtMap || null;
            const mtMapMethods = {};
            for (const name of methodNames) {
                mtMapMethods[name] = !!(
                    mtMap && typeof mtMap[name] === 'function'
                );
            }

            const containers = [
                ...document.querySelectorAll('.leaflet-container'),
            ];
            const mapCanvas = document.getElementById('map_canvas');
            const candidates = new Set();

            const inspectObject = (obj) => {
                if (!obj || typeof obj !== 'object') return;
                if (
                    typeof obj.setView === 'function' &&
                    typeof obj.getCenter === 'function'
                ) {
                    candidates.add(obj);
                    return;
                }

                for (const key of Object.getOwnPropertyNames(obj)) {
                    try {
                        const value = obj[key];
                        if (
                            value &&
                            typeof value === 'object' &&
                            typeof value.setView === 'function' &&
                            typeof value.getCenter === 'function'
                        ) {
                            candidates.add(value);
                        }
                    } catch (e) {}
                }

                for (const sym of Object.getOwnPropertySymbols(obj)) {
                    try {
                        const value = obj[sym];
                        if (
                            value &&
                            typeof value === 'object' &&
                            typeof value.setView === 'function' &&
                            typeof value.getCenter === 'function'
                        ) {
                            candidates.add(value);
                        }
                    } catch (e) {}
                }
            };

            for (const container of containers) {
                inspectObject(container);
                for (const child of container.querySelectorAll('*')) {
                    inspectObject(child);
                }
            }

            return {
                has_window_L: typeof window.L !== 'undefined',
                has_mtMap: !!mtMap,
                mtMap_methods: mtMapMethods,
                leaflet_container_count: containers.length,
                map_canvas_present: !!mapCanvas,
                leaflet_container_rect: rectOf(containers[0] || null),
                map_canvas_rect: rectOf(mapCanvas),
                discovered_candidate_count: candidates.size,
                device_pixel_ratio: window.devicePixelRatio || null,
                user_agent: navigator.userAgent,
            };
        }
        """
    )


FRAME_SCAN_JS = """
() => {
    const methodNames = [
        'setView',
        'getCenter',
        'latLngToContainerPoint',
        'getContainer',
    ];

    const safe = (fn, fallback = null) => {
        try {
            return fn();
        } catch (e) {
            return fallback;
        }
    };

    const rectOf = (el) => {
        if (!el) return null;
        const r = el.getBoundingClientRect();
        return { x: r.x, y: r.y, w: r.width, h: r.height };
    };

    const methodStatus = (obj) => {
        const status = {};
        for (const name of methodNames) {
            try {
                status[name] = !!(obj && typeof obj[name] === 'function');
            } catch (e) {
                status[name] = false;
            }
        }
        return status;
    };

    const candidates = [];
    const seen = new WeakSet();
    const maxCandidates = 20;

    const addCandidate = (obj, source, includePartial = false) => {
        if (!obj || typeof obj !== 'object') return false;
        if (seen.has(obj)) return false;
        const methods = methodStatus(obj);
        const hasAny = Object.values(methods).some(Boolean);
        const hasAll = Object.values(methods).every(Boolean);
        if (!hasAll && !(includePartial && hasAny)) return false;
        seen.add(obj);
        if (candidates.length < maxCandidates) {
            candidates.push({
                source,
                constructor: safe(() => obj.constructor && obj.constructor.name),
                methods,
                has_all_required_methods: hasAll,
            });
        }
        return hasAll;
    };

    const scanOwnValues = (obj, source, maxProps = 300) => {
        if (!obj || typeof obj !== 'object') return false;
        let found = addCandidate(obj, source, true);

        const propNames = safe(
            () => Object.getOwnPropertyNames(obj).slice(0, maxProps),
            [],
        );
        for (const key of propNames) {
            let value;
            try {
                value = obj[key];
            } catch (e) {
                continue;
            }
            if (addCandidate(value, `${source}.${key}`)) found = true;
        }

        const symbols = safe(
            () => Object.getOwnPropertySymbols(obj).slice(0, 80),
            [],
        );
        for (const sym of symbols) {
            let value;
            try {
                value = obj[sym];
            } catch (e) {
                continue;
            }
            if (addCandidate(value, `${source}.${sym.toString()}`)) {
                found = true;
            }
        }
        return found;
    };

    const mapCanvas = document.getElementById('map_canvas');
    const leafletContainers = [
        ...document.querySelectorAll('.leaflet-container'),
    ];
    const roots = new Set();

    if (mapCanvas) roots.add(mapCanvas);
    for (const container of leafletContainers) roots.add(container);

    for (const root of [...roots]) {
        let parent = root.parentElement;
        for (let depth = 0; depth < 6 && parent; depth++) {
            roots.add(parent);
            parent = parent.parentElement;
        }
        for (const child of root.querySelectorAll('*')) {
            roots.add(child);
            if (roots.size > 1500) break;
        }
    }

    const mtMapFromWindow = safe(() => window.__mtMap, null);
    if (mtMapFromWindow) {
        addCandidate(mtMapFromWindow, 'window.__mtMap', true);
    }

    for (const root of roots) {
        scanOwnValues(root, describeElement(root));
    }

    const skipWindowProps = new Set([
        'window',
        'self',
        'top',
        'parent',
        'frames',
        'document',
        'localStorage',
        'sessionStorage',
        'indexedDB',
    ]);
    const windowProps = safe(
        () => Object.getOwnPropertyNames(window).slice(0, 2000),
        [],
    );
    for (const key of windowProps) {
        if (skipWindowProps.has(key)) continue;
        let value;
        try {
            value = window[key];
        } catch (e) {
            continue;
        }
        addCandidate(value, `window.${key}`);
    }

    function describeElement(el) {
        if (!el || !el.tagName) return 'object';
        const id = el.id ? `#${el.id}` : '';
        const className = safe(
            () => String(el.className || '').trim().replace(/\\s+/g, '.'),
            '',
        );
        const classes = className ? `.${className.slice(0, 120)}` : '';
        return `dom:${el.tagName.toLowerCase()}${id}${classes}`;
    }

    const mtMap = mtMapFromWindow || null;
    const windowLType = safe(() => typeof window.L, 'error');
    const mtMapMethods = methodStatus(mtMap);
    const hasMapLikeObject = candidates.some(
        (c) => c.has_all_required_methods,
    );
    const setViewAvailable = candidates.some(
        (c) => c.methods && c.methods.setView,
    );

    return {
        location_href: safe(() => window.location.href),
        origin: safe(() => window.location.origin),
        title: safe(() => document.title),
        ready_state: safe(() => document.readyState),
        typeof_window_L: windowLType,
        has_window_L: windowLType !== 'undefined' && windowLType !== 'error',
        has_mtMap: !!mtMap,
        mtMap_methods: mtMapMethods,
        has_map_like_object: hasMapLikeObject,
        setView_available: setViewAvailable,
        map_like_candidate_count: candidates.filter(
            (c) => c.has_all_required_methods,
        ).length,
        map_like_candidates: candidates,
        leaflet_container_count: leafletContainers.length,
        leaflet_container_rects: leafletContainers.slice(0, 5).map(rectOf),
        map_canvas_present: !!mapCanvas,
        map_canvas_rect: rectOf(mapCanvas),
        iframe_count_in_frame: document.querySelectorAll('iframe').length,
        device_pixel_ratio: window.devicePixelRatio || null,
        user_agent: navigator.userAgent,
    };
}
"""


def _safe_frame_name(frame):
    try:
        name = frame.name
        if callable(name):
            return name()
        return name
    except Exception:
        return None


def run_frame_scan(page, reason=None):
    """Traverse the top frame and iframes and report Leaflet/map diagnostics."""
    frames = list(page.frames)
    frame_indexes = {id(frame): idx for idx, frame in enumerate(frames)}
    results = []

    for idx, frame in enumerate(frames):
        parent = getattr(frame, "parent_frame", None)
        parent_idx = frame_indexes.get(id(parent)) if parent else None
        entry = {
            "index": idx,
            "parent_index": parent_idx,
            "is_top_frame": parent is None,
            "name": _safe_frame_name(frame),
            "playwright_url": getattr(frame, "url", None),
        }
        try:
            entry.update(frame.evaluate(FRAME_SCAN_JS))
        except Exception as exc:
            entry["scan_error"] = repr(exc)
        results.append(entry)

    frames_with_window_l = [
        f["index"] for f in results
        if f.get("typeof_window_L") not in (None, "undefined")
    ]
    frames_with_mt_map = [
        f["index"] for f in results if f.get("has_mtMap")
    ]
    frames_with_map_like = [
        f["index"] for f in results if f.get("has_map_like_object")
    ]
    frames_with_set_view = [
        f["index"] for f in results if f.get("setView_available")
    ]
    frames_with_leaflet_container = [
        f["index"] for f in results
        if (f.get("leaflet_container_count") or 0) > 0
    ]
    frames_with_map_canvas = [
        f["index"] for f in results if f.get("map_canvas_present")
    ]

    return {
        "event": "frame_scan",
        "reason": reason,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "page_url": getattr(page, "url", None),
        "frame_count": len(results),
        "summary": {
            "frames_with_window_L": frames_with_window_l,
            "frames_with_mtMap": frames_with_mt_map,
            "frames_with_map_like_object": frames_with_map_like,
            "frames_with_setView": frames_with_set_view,
            "frames_with_leaflet_container": frames_with_leaflet_container,
            "frames_with_map_canvas": frames_with_map_canvas,
            "leaflet_found": bool(
                frames_with_window_l
                or frames_with_map_like
                or frames_with_leaflet_container
            ),
            "setView_available": bool(frames_with_set_view),
        },
        "frames": results,
    }


def write_frame_scan(page, timestamp_str=None, region_name="unknown",
                     reason=None, output_dir="data/probes"):
    """Write a frame scan JSON dump and return its absolute path."""
    scan = run_frame_scan(page, reason=reason)
    scan["region"] = region_name
    scan["capture_timestamp"] = timestamp_str

    dump_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H-%M-%S-%f")
    safe_region = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(region_name or "unknown"))
    path = Path(output_dir) / f"frame_scan_{dump_ts}_{safe_region}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(scan, f, indent=2, sort_keys=True)
        f.write("\n")
    return str(path.resolve())
