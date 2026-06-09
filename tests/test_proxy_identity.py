"""Tests for coherent browser and proxy identity configuration."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from geo_profile import EGYPT_FALLBACK_DATA, GeoProfile
import scraper_global as s


def _proxy():
    return {
        "server": "http://isp.decodo.com:10001",
        "username": "user",
        "password": "pass",
    }


def test_user_agent_tracks_installed_chrome_major():
    user_agent = s._chrome_user_agent("146.0.7680.80")
    assert "Chrome/146.0.0.0" in user_agent
    assert "HeadlessChrome" not in user_agent


def test_invalid_chrome_version_is_rejected():
    try:
        s._chrome_user_agent("unknown")
        assert False, "expected invalid browser version to be rejected"
    except ValueError:
        pass


def test_missing_geo_profile_is_rejected():
    old_proxies = s.proxies
    old_profiles = s.geo_profiles
    try:
        s.proxies = [_proxy()]
        s.geo_profiles = {}
        try:
            s._pick_batch_proxy()
            assert False, "expected unresolved proxy identity to be rejected"
        except RuntimeError as exc:
            assert "refusing to use a mismatched fallback identity" in str(exc)
    finally:
        s.proxies = old_proxies
        s.geo_profiles = old_profiles


def test_fallback_geo_profile_is_rejected():
    old_proxies = s.proxies
    old_profiles = s.geo_profiles
    proxy = _proxy()
    try:
        s.proxies = [proxy]
        s.geo_profiles = {
            proxy["server"]: GeoProfile(proxy=proxy, **EGYPT_FALLBACK_DATA)
        }
        try:
            s._pick_batch_proxy()
            assert False, "expected fallback proxy identity to be rejected"
        except RuntimeError as exc:
            assert "refusing to use a mismatched fallback identity" in str(exc)
    finally:
        s.proxies = old_proxies
        s.geo_profiles = old_profiles


def test_resolved_geo_profile_is_accepted():
    old_proxies = s.proxies
    old_profiles = s.geo_profiles
    proxy = _proxy()
    profile = GeoProfile(
        proxy=proxy,
        exit_ip="192.0.2.10",
        country_code="BE",
        city="Brussels",
        latitude=50.85,
        longitude=4.35,
        timezone_id="Europe/Brussels",
        locale="nl-BE",
        accept_language="nl-BE,nl;q=0.9,fr;q=0.8,en;q=0.7",
    )
    try:
        s.proxies = [proxy]
        s.geo_profiles = {proxy["server"]: profile}
        selected_proxy, selected_profile = s._pick_batch_proxy()
        assert selected_proxy is proxy
        assert selected_profile is profile
    finally:
        s.proxies = old_proxies
        s.geo_profiles = old_profiles


if __name__ == "__main__":
    failures = 0
    tests = [
        value for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except Exception as exc:
            failures += 1
            print(f"FAIL {test.__name__}: {exc!r}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    raise SystemExit(1 if failures else 0)
