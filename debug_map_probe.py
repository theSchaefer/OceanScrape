"""Runtime diagnostics for MarineTraffic map/Leaflet availability."""


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
