#!/bin/python3
"""Enhanced one-shot diagnostic: discover how MarineTraffic stores its map instance.

Loads the page once, runs aggressive JS introspection (including constructor
hooks injected before page load), writes comprehensive results to
map_discovery.json, then exits.
"""

import json
import logging
import time
from datetime import datetime, timezone

from patchright.sync_api import sync_playwright

from geo_profile import resolve_proxy_geo
from grid import get_tile_centers

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# --- Minimal config (same as scraper_patchright_pan.py) -----------------------

NORTH_POLYGON = [
    (31.575, 31.91),
    (31.77435, 32.27517),
    (31.31, 32.27036),
    (31.517, 32.5445),
]

proxy = {
    "server": "http://isp.decodo.com:10011",
    "username": "sp9r12fuvq",
    "password": "c8yCmlGlR5Kk2=g4rm",
}

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"

OUTPUT_FILE = "map_discovery.json"

# --- Pre-navigation init script (constructor hooks) ---------------------------

INIT_SCRIPT = """
// Capture Leaflet map instances at construction time
(function() {
    window.__discoveredMaps = [];
    window.__eventListenerLog = [];

    // --- Hook 1: Intercept L.Map constructor ---
    // Leaflet may not be loaded yet, so we use a getter to detect when L appears
    let _origL = undefined;
    const _desc = Object.getOwnPropertyDescriptor(window, 'L');
    if (_desc && _desc.value) {
        _origL = _desc.value;
        hookLeaflet(_origL);
    } else {
        Object.defineProperty(window, 'L', {
            configurable: true,
            enumerable: true,
            get() { return _origL; },
            set(val) {
                _origL = val;
                if (val && typeof val === 'object') {
                    hookLeaflet(val);
                }
            }
        });
    }

    function hookLeaflet(L) {
        if (L._discoveryHooked) return;
        L._discoveryHooked = true;

        // Hook L.Map if it exists
        if (L.Map && L.Map.prototype) {
            const origInit = L.Map.prototype.initialize;
            if (origInit) {
                L.Map.prototype.initialize = function() {
                    origInit.apply(this, arguments);
                    window.__discoveredMaps.push({
                        instance: this,
                        timestamp: Date.now(),
                        container_id: this._container?.id || null,
                        container_class: this._container?.className || null,
                    });
                };
            }
        }

        // Hook L.map factory if it exists
        if (typeof L.map === 'function') {
            const origFactory = L.map;
            L.map = function() {
                const result = origFactory.apply(this, arguments);
                window.__discoveredMaps.push({
                    instance: result,
                    timestamp: Date.now(),
                    source: 'L.map_factory',
                });
                return result;
            };
            // Preserve any properties on the original function
            Object.keys(origFactory).forEach(k => { L.map[k] = origFactory[k]; });
        }
    }

    // --- Hook 2: Log event listeners on map-related elements ---
    const origAddEventListener = EventTarget.prototype.addEventListener;
    EventTarget.prototype.addEventListener = function(type, listener, options) {
        // Only log for elements likely to be the map container
        if (this instanceof HTMLElement) {
            const cl = this.className?.toString() || '';
            const id = this.id || '';
            if (cl.includes('leaflet') || cl.includes('map') || id.includes('map')) {
                try {
                    window.__eventListenerLog.push({
                        tag: this.tagName,
                        id: id,
                        className: cl.slice(0, 100),
                        eventType: type,
                        listenerPreview: listener?.toString?.()?.slice(0, 300) || 'N/A',
                    });
                } catch(e) {}
            }
        }
        return origAddEventListener.call(this, type, listener, options);
    };
})();
"""

# --- Post-load JS diagnostic payload -----------------------------------------

DISCOVERY_JS = """
() => {
    const result = {
        meta: {},
        constructor_hook: {},
        leaflet_global: {},
        map_like_globals: [],
        non_native_globals: [],
        map_name_globals: [],
        symbol_scan: [],
        non_enumerable_scan: [],
        framework_bindings: [],
        event_listeners: [],
        leaflet_dom: [],
        mapbox_dom: [],
        leaflet_container_scan: [],
        dom_subtree: [],
        canvas_scan: [],
        deep_walk_hits: [],
        script_urls: [],
        performance_summary: {},
    };

    const MAP_METHODS = ['setView', 'flyTo', 'panTo', 'jumpTo', 'setCenter',
                         'getCenter', 'getZoom', 'setZoom', 'getBounds',
                         'fitBounds', 'invalidateSize', 'remove',
                         'addLayer', 'removeLayer', 'hasLayer'];

    // Helper: describe an object compactly
    function describeObj(obj, maxKeys) {
        if (!obj || typeof obj !== 'object') return { type: typeof obj };
        maxKeys = maxKeys || 30;
        const info = {
            constructor: obj.constructor?.name || 'Object',
            keys: Object.keys(obj).slice(0, maxKeys),
        };
        const mapMethods = MAP_METHODS.filter(m => typeof obj[m] === 'function');
        if (mapMethods.length > 0) info.map_methods = mapMethods;
        // prototype methods
        try {
            const proto = Object.getPrototypeOf(obj);
            if (proto && proto !== Object.prototype) {
                info.proto_methods = Object.getOwnPropertyNames(proto)
                    .filter(p => typeof obj[p] === 'function' && p !== 'constructor')
                    .slice(0, 50);
            }
        } catch(e) {}
        return info;
    }

    // =========================================================================
    // 0. Page metadata
    // =========================================================================
    result.meta = {
        url: window.location.href,
        title: document.title,
        user_agent: navigator.userAgent,
        timestamp: new Date().toISOString(),
        cookie_names: document.cookie.split(';').map(c => c.trim().split('=')[0]).filter(Boolean),
        ready_state: document.readyState,
    };

    // =========================================================================
    // 1. Constructor hook results (from INIT_SCRIPT)
    // =========================================================================
    const discovered = window.__discoveredMaps || [];
    if (discovered.length > 0) {
        const m = discovered[0].instance;
        result.constructor_hook = {
            map_found: true,
            count: discovered.length,
            container_id: discovered[0].container_id,
            container_class: discovered[0].container_class,
            source: discovered[0].source || 'L.Map.initialize',
            center: m.getCenter ? [m.getCenter().lat, m.getCenter().lng] : null,
            zoom: m.getZoom ? m.getZoom() : null,
            methods: describeObj(m, 20),
            options: m.options ? Object.keys(m.options).slice(0, 40) : [],
            layers_count: m._layers ? Object.keys(m._layers).length : null,
        };
        // Store reference for later use
        window.__mt_map = m;
    } else {
        result.constructor_hook = { map_found: false, count: 0 };
    }

    // =========================================================================
    // 2. Comprehensive L global inspection
    // =========================================================================
    if (typeof L !== 'undefined') {
        const lInfo = {
            exists: true,
            version: L.version || null,
            keys: Object.keys(L).slice(0, 100),
        };
        // L.Map prototype methods
        if (L.Map && L.Map.prototype) {
            lInfo.map_proto_methods = Object.getOwnPropertyNames(L.Map.prototype)
                .filter(p => typeof L.Map.prototype[p] === 'function')
                .slice(0, 80);
        }
        // Look for internal registries
        for (const regName of ['_maps', '_mapInstances', '_instances', '_containers', 'maps']) {
            if (L[regName]) {
                lInfo['registry_' + regName] = {
                    type: typeof L[regName],
                    constructor: L[regName]?.constructor?.name,
                    keys: Object.keys(L[regName]).slice(0, 20),
                };
            }
        }
        // L.Map static properties
        if (L.Map) {
            lInfo.map_static_keys = Object.keys(L.Map).slice(0, 30);
            for (const sk of Object.keys(L.Map)) {
                try {
                    const val = L.Map[sk];
                    if (val && typeof val === 'object' && !Array.isArray(val)) {
                        const methods = MAP_METHODS.filter(m => typeof val[m] === 'function');
                        if (methods.length >= 2) {
                            lInfo.map_static_instance = {
                                key: sk,
                                methods: methods,
                            };
                        }
                    }
                } catch(e) {}
            }
        }
        result.leaflet_global = lInfo;
    } else {
        result.leaflet_global = { exists: false };
    }

    // =========================================================================
    // 3. Scan all window properties for objects with map-like methods
    // =========================================================================
    for (const key of Object.getOwnPropertyNames(window)) {
        try {
            const obj = window[key];
            if (!obj || typeof obj !== 'object') continue;
            const methods = MAP_METHODS.filter(m => typeof obj[m] === 'function');
            if (methods.length >= 2) {
                result.map_like_globals.push({
                    name: key,
                    ...describeObj(obj, 30),
                    methods: methods,
                });
            }
        } catch(e) {}
    }

    // =========================================================================
    // 4. Non-native window globals (filter out browser builtins)
    // =========================================================================
    const NATIVE_PREFIXES = ['webkit', 'chrome', 'CSS', 'SVG', 'HTML', 'DOM',
        'XML', 'URL', 'IDB', 'MIDI', 'RTC', 'Web', 'Audio', 'Canvas',
        'File', 'Form', 'GPU', 'Intersection', 'Mutation', 'Performance',
        'Resize', 'Blob', 'Broadcast', 'Clipboard', 'Credential', 'Crypto',
        'Cache', 'Custom', 'DataTransfer', 'Event', 'Focus', 'Font',
        'Gamepad', 'Headers', 'History', 'Image', 'Input', 'Keyboard',
        'Location', 'Lock', 'Media', 'Message', 'Navigator', 'Notification',
        'Payment', 'Permission', 'Plugin', 'Pointer', 'Popup', 'Presentation',
        'Promise', 'Push', 'Range', 'Report', 'Request', 'Response',
        'Screen', 'Scroll', 'Security', 'Selection', 'Service', 'Shadow',
        'Shared', 'SourceBuffer', 'Speech', 'Storage', 'Style', 'Text',
        'Touch', 'UI', 'Visual', 'Window', 'Worker', 'Writable'];
    const NATIVE_EXACT = new Set([
        'Map', 'Set', 'WeakMap', 'WeakSet', 'WeakRef', 'Proxy', 'Reflect',
        'Symbol', 'BigInt', 'Promise', 'Intl', 'JSON', 'Math', 'Array',
        'ArrayBuffer', 'Float32Array', 'Float64Array', 'Int8Array', 'Int16Array',
        'Int32Array', 'Uint8Array', 'Uint16Array', 'Uint32Array', 'Boolean',
        'Number', 'String', 'RegExp', 'Date', 'Error', 'TypeError',
        'RangeError', 'SyntaxError', 'ReferenceError', 'URIError', 'EvalError',
        'Function', 'Object', 'undefined', 'NaN', 'Infinity',
        'isNaN', 'isFinite', 'parseInt', 'parseFloat',
        'encodeURI', 'decodeURI', 'encodeURIComponent', 'decodeURIComponent',
        'escape', 'unescape', 'eval', 'alert', 'confirm', 'prompt',
        'setTimeout', 'clearTimeout', 'setInterval', 'clearInterval',
        'requestAnimationFrame', 'cancelAnimationFrame', 'requestIdleCallback',
        'cancelIdleCallback', 'fetch', 'atob', 'btoa', 'console', 'document',
        'window', 'self', 'top', 'parent', 'frames', 'frameElement', 'length',
        'name', 'status', 'closed', 'opener', 'location', 'history',
        'navigator', 'screen', 'visualViewport', 'menubar', 'toolbar',
        'locationbar', 'personalbar', 'scrollbars', 'statusbar',
        'origin', 'crossOriginIsolated', 'isSecureContext',
        'performance', 'caches', 'cookieStore', 'scheduler',
        'close', 'stop', 'focus', 'blur', 'open', 'print',
        'postMessage', 'getComputedStyle', 'getSelection', 'matchMedia',
        'moveTo', 'moveBy', 'resizeTo', 'resizeBy', 'scroll', 'scrollTo',
        'scrollBy', 'find', 'structuredClone', 'reportError', 'queueMicrotask',
        'createImageBitmap', 'NamedNodeMap',
    ]);

    function isLikelyNative(name) {
        if (NATIVE_EXACT.has(name)) return true;
        for (const prefix of NATIVE_PREFIXES) {
            if (name.startsWith(prefix)) return true;
        }
        // Skip single lowercase letters except known frameworks
        if (name.length === 1 && name >= 'a' && name <= 'z' && name !== 'L') return true;
        // Skip event handlers
        if (name.startsWith('on')) return true;
        return false;
    }

    for (const key of Object.getOwnPropertyNames(window)) {
        if (isLikelyNative(key)) continue;
        try {
            const obj = window[key];
            const type = obj === null ? 'null' : typeof obj;
            const info = { name: key, type: type };
            if (obj && typeof obj === 'object') {
                info.constructor = obj.constructor?.name;
                info.key_count = Object.keys(obj).length;
                info.keys = Object.keys(obj).slice(0, 25);
                const mapMethods = MAP_METHODS.filter(m => typeof obj[m] === 'function');
                if (mapMethods.length > 0) info.map_methods = mapMethods;
                try {
                    const proto = Object.getPrototypeOf(obj);
                    if (proto && proto !== Object.prototype) {
                        info.proto_name = proto.constructor?.name;
                    }
                } catch(e) {}
            } else if (typeof obj === 'function') {
                info.fn_length = obj.length;
                if (obj.prototype) {
                    info.proto_methods = Object.getOwnPropertyNames(obj.prototype)
                        .filter(p => p !== 'constructor')
                        .slice(0, 15);
                }
            }
            result.non_native_globals.push(info);
        } catch(e) {
            result.non_native_globals.push({ name: key, error: e.message });
        }
    }

    // =========================================================================
    // 5. Globals with "map" in name (kept from original)
    // =========================================================================
    for (const key of Object.getOwnPropertyNames(window)) {
        if (!key.toLowerCase().includes('map')) continue;
        if (key === 'Map') continue;
        try {
            const obj = window[key];
            const type = obj === null ? 'null' : typeof obj;
            const info = { name: key, type: type };
            if (obj && typeof obj === 'object') {
                info.constructor = obj.constructor?.name;
                info.methods = Object.getOwnPropertyNames(Object.getPrototypeOf(obj) || {})
                    .filter(p => typeof obj[p] === 'function')
                    .slice(0, 30);
                info.has_setView = typeof obj.setView === 'function';
                info.has_jumpTo = typeof obj.jumpTo === 'function';
                info.has_panTo = typeof obj.panTo === 'function';
            }
            result.map_name_globals.push(info);
        } catch(e) {
            result.map_name_globals.push({ name: key, error: e.message });
        }
    }

    // =========================================================================
    // 6. Symbol property scan on map-related DOM elements
    // =========================================================================
    const mapEls = document.querySelectorAll(
        '#map_canvas, .leaflet-container, [class*="leaflet"], [class*="map"], [id*="map"]'
    );
    mapEls.forEach(el => {
        const symbols = Object.getOwnPropertySymbols(el);
        if (symbols.length > 0) {
            const symInfo = symbols.map(s => {
                try {
                    const val = el[s];
                    return {
                        description: s.toString(),
                        type: val === null ? 'null' : typeof val,
                        value_preview: typeof val === 'object' && val
                            ? describeObj(val, 15)
                            : String(val).slice(0, 100),
                    };
                } catch(e) {
                    return { description: s.toString(), error: e.message };
                }
            });
            result.symbol_scan.push({
                tag: el.tagName,
                id: el.id || null,
                className: (el.className || '').toString().slice(0, 100),
                symbols: symInfo,
            });
        }
    });

    // =========================================================================
    // 7. Non-enumerable property scan on #map_canvas + ancestors
    // =========================================================================
    const mapCanvas = document.getElementById('map_canvas');
    if (mapCanvas) {
        // Scan the element and its first 3 ancestors
        let el = mapCanvas;
        let depth = 0;
        while (el && depth < 4) {
            const descriptors = Object.getOwnPropertyDescriptors(el);
            const nonEnum = [];
            for (const [key, desc] of Object.entries(descriptors)) {
                if (key.startsWith('on') || key === 'style') continue;
                try {
                    const val = el[key];
                    const entry = {
                        name: key,
                        enumerable: desc.enumerable,
                        configurable: desc.configurable,
                        type: val === null ? 'null' : typeof val,
                    };
                    if (val && typeof val === 'object') {
                        const mapMethods = MAP_METHODS.filter(m => typeof val[m] === 'function');
                        if (mapMethods.length > 0) entry.map_methods = mapMethods;
                        entry.constructor = val.constructor?.name;
                        entry.key_count = Object.keys(val).length;
                    }
                    if (!desc.enumerable || entry.map_methods) {
                        nonEnum.push(entry);
                    }
                } catch(e) {}
            }
            if (nonEnum.length > 0) {
                result.non_enumerable_scan.push({
                    tag: el.tagName,
                    id: el.id || null,
                    className: (el.className || '').toString().slice(0, 100),
                    depth: depth,
                    properties: nonEnum.slice(0, 50),
                });
            }
            el = el.parentElement;
            depth++;
        }

        // Also scan children of #map_canvas (first 20)
        const children = mapCanvas.querySelectorAll('*');
        for (let i = 0; i < Math.min(children.length, 20); i++) {
            const child = children[i];
            const descriptors = Object.getOwnPropertyDescriptors(child);
            const interesting = [];
            for (const [key, desc] of Object.entries(descriptors)) {
                if (key.startsWith('on') || key === 'style') continue;
                try {
                    const val = child[key];
                    if (val && typeof val === 'object') {
                        const mapMethods = MAP_METHODS.filter(m => typeof val[m] === 'function');
                        if (mapMethods.length >= 2) {
                            interesting.push({
                                name: key,
                                map_methods: mapMethods,
                                constructor: val.constructor?.name,
                            });
                        }
                    }
                } catch(e) {}
            }
            if (interesting.length > 0) {
                result.non_enumerable_scan.push({
                    tag: child.tagName,
                    id: child.id || null,
                    className: (child.className || '').toString().slice(0, 100),
                    context: 'map_canvas_child',
                    properties: interesting,
                });
            }
        }
    }

    // =========================================================================
    // 8. Framework binding scan
    // =========================================================================
    const frameworkEls = [
        document.getElementById('map_canvas'),
        document.getElementById('app'),
        document.getElementById('root'),
        document.getElementById('__next'),
        document.getElementById('__nuxt'),
        document.body,
    ].filter(Boolean);

    // Also walk up from #map_canvas
    if (mapCanvas) {
        let p = mapCanvas.parentElement;
        let d = 0;
        while (p && d < 5) {
            frameworkEls.push(p);
            p = p.parentElement;
            d++;
        }
    }

    const seen = new Set();
    for (const el of frameworkEls) {
        if (seen.has(el)) continue;
        seen.add(el);
        const bindings = {};
        const allProps = Object.getOwnPropertyNames(el);
        for (const p of allProps) {
            // Vue
            if (p === '__vue__' || p === '__vue_app__' || p === '$vue') {
                try {
                    const v = el[p];
                    bindings[p] = {
                        type: typeof v,
                        keys: v ? Object.keys(v).slice(0, 30) : [],
                        data_keys: v?.$data ? Object.keys(v.$data).slice(0, 20) : [],
                    };
                } catch(e) { bindings[p] = { error: e.message }; }
            }
            // React
            if (p.startsWith('__reactFiber$') || p.startsWith('__reactInternalInstance$')
                || p === '_reactRootContainer') {
                try {
                    const v = el[p];
                    bindings[p] = {
                        type: typeof v,
                        constructor: v?.constructor?.name,
                        state_type: v?.memoizedState ? typeof v.memoizedState : null,
                        element_type: v?.type?.name || v?.type?.displayName || null,
                    };
                } catch(e) { bindings[p] = { error: e.message }; }
            }
            // Svelte
            if (p.startsWith('__svelte')) {
                try {
                    bindings[p] = { type: typeof el[p] };
                } catch(e) { bindings[p] = { error: e.message }; }
            }
            // Angular
            if (p === '__ngContext__' || p === '_ngContext') {
                try {
                    bindings[p] = { type: typeof el[p], isArray: Array.isArray(el[p]) };
                } catch(e) { bindings[p] = { error: e.message }; }
            }
        }
        if (Object.keys(bindings).length > 0) {
            result.framework_bindings.push({
                tag: el.tagName,
                id: el.id || null,
                className: (el.className || '').toString().slice(0, 100),
                bindings: bindings,
            });
        }
    }

    // =========================================================================
    // 9. Event listeners from hook
    // =========================================================================
    result.event_listeners = (window.__eventListenerLog || []).slice(0, 200);

    // =========================================================================
    // 10. DOM elements with _leaflet* properties (expanded)
    // =========================================================================
    document.querySelectorAll('*').forEach(el => {
        const allProps = Object.getOwnPropertyNames(el);
        const leafletProps = allProps.filter(p =>
            p.startsWith('_leaflet') || p.startsWith('leaflet')
        );
        // Also check symbols
        const leafletSymbols = Object.getOwnPropertySymbols(el).filter(s =>
            s.toString().toLowerCase().includes('leaflet')
        );

        if (leafletProps.length > 0 || leafletSymbols.length > 0) {
            const info = {
                tag: el.tagName,
                id: el.id || null,
                className: (el.className || '').toString().slice(0, 200),
                leaflet_props: {},
            };
            for (const p of leafletProps) {
                try {
                    const val = el[p];
                    const type = val === null ? 'null' : typeof val;
                    if (type === 'object' && val) {
                        info.leaflet_props[p] = describeObj(val, 25);
                    } else {
                        info.leaflet_props[p] = { type: type, value: String(val).slice(0, 100) };
                    }
                } catch(e) {
                    info.leaflet_props[p] = { error: e.message };
                }
            }
            for (const s of leafletSymbols) {
                try {
                    const val = el[s];
                    info.leaflet_props[s.toString()] = describeObj(val, 15);
                } catch(e) {}
            }
            result.leaflet_dom.push(info);
        }
    });

    // =========================================================================
    // 11. Mapbox DOM scan (kept from original)
    // =========================================================================
    document.querySelectorAll('.mapboxgl-map, [class*="mapbox"]').forEach(el => {
        const props = Object.getOwnPropertyNames(el)
            .filter(p => !p.startsWith('on') && p !== 'style');
        result.mapbox_dom.push({
            tag: el.tagName,
            id: el.id || null,
            className: (el.className || '').toString().slice(0, 200),
            custom_props: props.slice(0, 30),
        });
    });

    // =========================================================================
    // 12. Leaflet container scan (expanded)
    // =========================================================================
    document.querySelectorAll('.leaflet-container').forEach(el => {
        const allProps = Object.getOwnPropertyNames(el);
        const symbols = Object.getOwnPropertySymbols(el);
        const info = {
            tag: el.tagName,
            id: el.id || null,
            className: (el.className || '').toString().slice(0, 200),
            all_custom_props: allProps.filter(p =>
                !p.startsWith('on') && p !== 'style' && p !== '__proto__'
            ).slice(0, 50),
            symbol_count: symbols.length,
            symbols: symbols.map(s => s.toString()).slice(0, 20),
            dataset: Object.assign({}, el.dataset),
            computed_styles: {},
        };

        // Grab a few computed style properties
        try {
            const cs = getComputedStyle(el);
            info.computed_styles = {
                position: cs.position,
                width: cs.width,
                height: cs.height,
                overflow: cs.overflow,
            };
        } catch(e) {}

        // Check each prop for map-like objects
        for (const p of allProps) {
            try {
                const val = el[p];
                if (val && typeof val === 'object') {
                    const methods = MAP_METHODS.filter(m => typeof val[m] === 'function');
                    if (methods.length >= 2) {
                        info.map_object_found = {
                            prop_name: p,
                            constructor: val.constructor?.name,
                            methods: methods,
                            keys: Object.keys(val).slice(0, 30),
                        };
                    }
                }
            } catch(e) {}
        }

        // Check symbols for map objects
        for (const s of symbols) {
            try {
                const val = el[s];
                if (val && typeof val === 'object') {
                    const methods = MAP_METHODS.filter(m => typeof val[m] === 'function');
                    if (methods.length >= 2) {
                        info.map_object_via_symbol = {
                            symbol: s.toString(),
                            constructor: val.constructor?.name,
                            methods: methods,
                        };
                    }
                }
            } catch(e) {}
        }

        result.leaflet_container_scan.push(info);
    });

    // =========================================================================
    // 13. DOM subtree of #map_canvas
    // =========================================================================
    if (mapCanvas) {
        const descendants = mapCanvas.querySelectorAll('*');
        for (let i = 0; i < Math.min(descendants.length, 200); i++) {
            const el = descendants[i];
            const entry = {
                tag: el.tagName,
                id: el.id || null,
                className: (el.className || '').toString().slice(0, 150),
            };
            // data-* attributes
            if (Object.keys(el.dataset).length > 0) {
                entry.dataset = Object.assign({}, el.dataset);
            }
            // Custom properties count
            const customProps = Object.getOwnPropertyNames(el)
                .filter(p => !p.startsWith('on') && p !== 'style' && p !== '__proto__');
            if (customProps.length > 0) {
                entry.custom_prop_count = customProps.length;
                entry.custom_props = customProps.slice(0, 15);
            }
            result.dom_subtree.push(entry);
        }
    }

    // =========================================================================
    // 14. Canvas element deep scan
    // =========================================================================
    document.querySelectorAll('canvas').forEach(canvas => {
        const info = {
            id: canvas.id || null,
            className: (canvas.className || '').toString().slice(0, 100),
            width: canvas.width,
            height: canvas.height,
            parentId: canvas.parentElement?.id || null,
            parentClass: (canvas.parentElement?.className || '').toString().slice(0, 100),
        };

        // All properties (own)
        const ownProps = Object.getOwnPropertyNames(canvas)
            .filter(p => !p.startsWith('on') && p !== 'style');
        info.own_props = ownProps.slice(0, 30);

        // Symbol properties
        const symbols = Object.getOwnPropertySymbols(canvas);
        info.symbol_count = symbols.length;
        info.symbols = symbols.map(s => {
            try {
                const val = canvas[s];
                return {
                    desc: s.toString(),
                    type: typeof val,
                    preview: typeof val === 'object' && val
                        ? describeObj(val, 10)
                        : String(val).slice(0, 50),
                };
            } catch(e) { return { desc: s.toString(), error: e.message }; }
        });

        // Check context type
        try {
            const ctx2d = canvas.getContext('2d');
            info.context_2d = !!ctx2d;
        } catch(e) {
            info.context_2d = false;
        }
        try {
            const ctxgl = canvas.getContext('webgl') || canvas.getContext('webgl2');
            info.context_webgl = !!ctxgl;
        } catch(e) {
            info.context_webgl = false;
        }

        // Check if any own property is a map object
        for (const p of ownProps) {
            try {
                const val = canvas[p];
                if (val && typeof val === 'object') {
                    const methods = MAP_METHODS.filter(m => typeof val[m] === 'function');
                    if (methods.length >= 2) {
                        info.map_ref = { prop: p, methods: methods };
                    }
                }
            } catch(e) {}
        }

        result.canvas_scan.push(info);
    });

    // =========================================================================
    // 15. Deep walk (expanded: 4 levels, 2000 props, also walk functions)
    // =========================================================================
    const visited = new WeakSet();
    function walk(obj, path, depth) {
        if (depth > 3 || !obj) return;
        if (typeof obj !== 'object' && typeof obj !== 'function') return;
        try { if (visited.has(obj)) return; visited.add(obj); } catch(e) { return; }

        if (typeof obj === 'object') {
            const methods = MAP_METHODS.filter(m => typeof obj[m] === 'function');
            if (methods.length >= 2) {
                result.deep_walk_hits.push({
                    path: path,
                    constructor: obj.constructor?.name,
                    methods: methods,
                    keys: Object.keys(obj).slice(0, 25),
                });
            }
        }

        if (depth < 3) {
            const props = [];
            try { props.push(...Object.getOwnPropertyNames(obj).slice(0, 200)); } catch(e) {}
            try { props.push(...Object.getOwnPropertySymbols(obj).map(s => s)); } catch(e) {}

            for (const key of props.slice(0, 200)) {
                try {
                    const keyStr = typeof key === 'symbol' ? key.toString() : key;
                    const val = obj[key];
                    if (val && (typeof val === 'object' || typeof val === 'function')
                        && val !== window && val !== document) {
                        walk(val, path + '.' + keyStr, depth + 1);
                    }
                } catch(e) {}
            }
        }
    }

    for (const key of Object.getOwnPropertyNames(window).slice(0, 2000)) {
        try {
            const val = window[key];
            if (val && typeof val === 'object' && val !== window && val !== document) {
                walk(val, 'window.' + key, 0);
            }
        } catch(e) {}
    }

    // =========================================================================
    // 16. Loaded script URLs
    // =========================================================================
    document.querySelectorAll('script[src]').forEach(s => {
        result.script_urls.push(s.src);
    });
    // Also inline scripts (first 100 chars of content for identification)
    document.querySelectorAll('script:not([src])').forEach(s => {
        if (s.textContent && s.textContent.trim().length > 0) {
            result.script_urls.push({
                type: 'inline',
                preview: s.textContent.trim().slice(0, 150),
            });
        }
    });

    // =========================================================================
    // 17. Performance summary
    // =========================================================================
    try {
        const entries = performance.getEntriesByType('resource');
        const byType = {};
        for (const e of entries) {
            const ext = (e.name.split('?')[0].split('.').pop() || 'unknown').toLowerCase();
            if (!byType[ext]) byType[ext] = { count: 0, total_ms: 0 };
            byType[ext].count++;
            byType[ext].total_ms += Math.round(e.duration);
        }
        result.performance_summary = {
            total_resources: entries.length,
            by_type: byType,
            navigation_timing: {
                dom_complete: Math.round(performance.timing?.domComplete - performance.timing?.navigationStart) || null,
                dom_interactive: Math.round(performance.timing?.domInteractive - performance.timing?.navigationStart) || null,
                load_complete: Math.round(performance.timing?.loadEventEnd - performance.timing?.navigationStart) || null,
            },
        };
    } catch(e) {
        result.performance_summary = { error: e.message };
    }

    return result;
}
"""

# --- Tile URL interception (Python-side) --------------------------------------


def setup_tile_intercept(page):
    """Register a route handler to capture tile request URLs."""
    tile_urls = []

    def handle_route(route):
        url = route.request.url
        tile_urls.append(url)
        route.continue_()

    # Common tile URL patterns
    page.route("**/{z}/{x}/{y}*", handle_route)
    page.route("**/*.pbf*", handle_route)
    page.route("**/tile*", handle_route)
    page.route("**/*tiles*", handle_route)

    return tile_urls


# ------------------------------------------------------------------------------


def main():
    logger.info("Resolving proxy geo...")
    geo = resolve_proxy_geo(proxy)
    logger.info("Proxy: %s (%s, tz=%s)", geo.exit_ip, geo.country_code, geo.timezone_id)

    tiles, _ = get_tile_centers(NORTH_POLYGON, 13, 1920, 1080)
    _, _, lat, lon = tiles[0]
    url = f"https://www.marinetraffic.com/en/ais/home/centerx:{lon}/centery:{lat}/zoom:13"
    logger.info("URL: %s", url)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            channel="chrome",
            args=["--headless=new"],
        )

        context = browser.new_context(
            proxy={
                "server": proxy["server"],
                "username": proxy["username"],
                "password": proxy["password"],
            },
            viewport={"width": 1920, "height": 1080},
            timezone_id=geo.timezone_id,
            locale=geo.locale,
            geolocation={"latitude": geo.latitude, "longitude": geo.longitude},
            permissions=["geolocation"],
            extra_http_headers={"Accept-Language": geo.accept_language},
            user_agent=USER_AGENT,
        )

        page = context.new_page()

        # Inject constructor hooks BEFORE page loads
        logger.info("Injecting init script (L.Map hook + event listener logger)...")
        page.add_init_script(INIT_SCRIPT)

        # Set up tile URL interception
        tile_urls = setup_tile_intercept(page)

        logger.info("Navigating...")
        page.goto(url, wait_until="domcontentloaded")

        # Wait for Cloudflare if needed
        try:
            title = page.title().lower()
            if "just a moment" in title or "cloudflare" in title:
                logger.info("Cloudflare challenge, waiting up to 20s...")
                for _ in range(14):
                    time.sleep(1.5)
                    title = page.title().lower()
                    if "just a moment" not in title and "cloudflare" not in title:
                        logger.info("Cloudflare passed")
                        break
        except Exception:
            pass

        # Wait for map to load
        logger.info("Waiting for map tiles...")
        try:
            page.wait_for_selector('canvas', state='attached', timeout=15000)
            deadline = time.time() + 15
            while time.time() < deadline:
                ok = page.evaluate("""
                () => {
                    const c = document.querySelector('canvas');
                    if (!c) return false;
                    try {
                        const ctx = c.getContext('2d');
                        if (!ctx) return false;
                        const w = c.width, h = c.height;
                        const pts = [[w*0.25,h*0.25],[w*0.5,h*0.5],[w*0.75,h*0.75]];
                        let hits = 0;
                        for (const [x,y] of pts) {
                            const p = ctx.getImageData(Math.floor(x),Math.floor(y),1,1).data;
                            if (p[3]>0 && (p[0]<250||p[1]<250||p[2]<250)) hits++;
                        }
                        return hits >= 2;
                    } catch(e) { return false; }
                }
                """)
                if ok:
                    logger.info("Map tiles loaded")
                    break
                time.sleep(0.5)
        except Exception as e:
            logger.warning("Tile wait issue: %s", e)

        # Extra settle time for JS initialization
        time.sleep(3)

        # Run main discovery
        logger.info("Running comprehensive map discovery JS...")
        result = page.evaluate(DISCOVERY_JS)

        # Add tile URLs captured by route interception
        result["tile_urls"] = list(set(tile_urls))[:50]

        context.close()
        browser.close()

    # Write results
    with open(OUTPUT_FILE, "w") as f:
        json.dump(result, f, indent=2)
    logger.info("Results written to %s", OUTPUT_FILE)

    # Print summary
    print("\n" + "=" * 70)
    print("ENHANCED MAP DISCOVERY RESULTS")
    print("=" * 70)

    # Meta
    print(f"\nPage: {result['meta'].get('title', '?')}")
    print(f"URL:  {result['meta'].get('url', '?')}")

    # Constructor hook (most important)
    ch = result.get("constructor_hook", {})
    if ch.get("map_found"):
        print(f"\n*** CONSTRUCTOR HOOK: MAP FOUND! ***")
        print(f"  Source: {ch.get('source')}")
        print(f"  Container: #{ch.get('container_id')} .{ch.get('container_class', '')[:60]}")
        print(f"  Center: {ch.get('center')}, Zoom: {ch.get('zoom')}")
        print(f"  Layers: {ch.get('layers_count')}")
        print(f"  Options: {ch.get('options', [])[:15]}")
    else:
        print("\nConstructor hook: NO MAP CAPTURED (hook may not have fired)")

    # Leaflet global
    lg = result.get("leaflet_global", {})
    if lg.get("exists"):
        print(f"\nLeaflet global (L): v{lg.get('version', '?')}")
        print(f"  Keys: {lg.get('keys', [])[:20]}")
        if lg.get("map_proto_methods"):
            print(f"  L.Map methods ({len(lg['map_proto_methods'])}): {lg['map_proto_methods'][:15]}...")
    else:
        print("\nLeaflet global (L): NOT FOUND")

    # Map-like globals
    if result["map_like_globals"]:
        print(f"\nMap-like globals ({len(result['map_like_globals'])}):")
        for g in result["map_like_globals"]:
            print(f"  window.{g['name']}  ({g.get('constructor', '?')})  methods: {g['methods']}")
    else:
        print("\nNo map-like globals found.")

    # Non-native globals (summary)
    nn = result.get("non_native_globals", [])
    print(f"\nNon-native globals: {len(nn)} found")
    map_hits = [g for g in nn if g.get("map_methods")]
    if map_hits:
        print("  WITH MAP METHODS:")
        for g in map_hits:
            print(f"    window.{g['name']}  methods: {g['map_methods']}")

    # Symbol scan
    if result["symbol_scan"]:
        print(f"\nSymbol properties found ({len(result['symbol_scan'])} elements):")
        for s in result["symbol_scan"]:
            print(f"  <{s['tag']} id={s['id']}> symbols: {[x.get('description') for x in s['symbols']]}")

    # Framework bindings
    if result["framework_bindings"]:
        print(f"\nFramework bindings ({len(result['framework_bindings'])}):")
        for fb in result["framework_bindings"]:
            print(f"  <{fb['tag']} id={fb['id']}> bindings: {list(fb['bindings'].keys())}")

    # Event listeners
    el_count = len(result.get("event_listeners", []))
    if el_count > 0:
        print(f"\nEvent listeners on map elements: {el_count}")
        types = {}
        for ev in result["event_listeners"]:
            t = ev.get("eventType", "?")
            types[t] = types.get(t, 0) + 1
        print(f"  By type: {types}")

    # Leaflet DOM
    if result["leaflet_dom"]:
        print(f"\nDOM elements with leaflet props ({len(result['leaflet_dom'])}):")
        for el in result["leaflet_dom"]:
            print(f"  <{el['tag']} id={el['id']}> props: {list(el['leaflet_props'].keys())}")

    # Container scan
    if result["leaflet_container_scan"]:
        print(f"\n.leaflet-container elements ({len(result['leaflet_container_scan'])}):")
        for el in result["leaflet_container_scan"]:
            print(f"  <{el['tag']} id={el['id']}> props: {el['all_custom_props']}")
            if el.get("map_object_found"):
                mf = el["map_object_found"]
                print(f"    >>> MAP OBJECT at .{mf['prop_name']} ({mf['constructor']}) methods={mf['methods']}")
            if el.get("map_object_via_symbol"):
                ms = el["map_object_via_symbol"]
                print(f"    >>> MAP via SYMBOL {ms['symbol']} ({ms['constructor']}) methods={ms['methods']}")

    # Deep walk
    if result["deep_walk_hits"]:
        print(f"\nDeep walk hits ({len(result['deep_walk_hits'])}):")
        for h in result["deep_walk_hits"]:
            print(f"  {h['path']}  ({h['constructor']})  methods={h['methods']}")

    # DOM subtree summary
    print(f"\nDOM subtree of #map_canvas: {len(result.get('dom_subtree', []))} elements")

    # Canvas scan
    if result["canvas_scan"]:
        print(f"\nCanvas elements ({len(result['canvas_scan'])}):")
        for c in result["canvas_scan"]:
            print(f"  {c.get('width')}x{c.get('height')} 2d={c.get('context_2d')} webgl={c.get('context_webgl')} "
                  f"props={c.get('own_props', [])} symbols={c.get('symbol_count', 0)}")

    # Script URLs
    scripts = [s for s in result.get("script_urls", []) if isinstance(s, str)]
    print(f"\nScript URLs: {len(scripts)}")
    for s in scripts[:10]:
        print(f"  {s[:120]}")

    # Tile URLs
    tiles = result.get("tile_urls", [])
    print(f"\nTile URLs captured: {len(tiles)}")
    for t in tiles[:5]:
        print(f"  {t[:120]}")

    # Performance
    perf = result.get("performance_summary", {})
    if perf.get("navigation_timing"):
        nt = perf["navigation_timing"]
        print(f"\nLoad times: DOM interactive={nt.get('dom_interactive')}ms, "
              f"DOM complete={nt.get('dom_complete')}ms, "
              f"Full load={nt.get('load_complete')}ms")

    print("\n" + "=" * 70)
    print(f"Full results in {OUTPUT_FILE}")
    print("=" * 70)


if __name__ == "__main__":
    main()
