import logging
import time
import os
from dataclasses import dataclass
from dotenv import load_dotenv

import requests

load_dotenv()
DECODO_USERNAME = os.environ.get("DECODO_USERNAME")
DECODO_PASSWORD = os.environ.get("DECODO_PASSWORD")

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Country → locale / accept-language mapping
# ---------------------------------------------------------------------------

COUNTRY_TO_LOCALE = {
    "EG": ("ar-EG", "ar-EG,ar;q=0.9,en;q=0.8"),
    "US": ("en-US", "en-US,en;q=0.9"),
    "GB": ("en-GB", "en-GB,en;q=0.9"),
    "DE": ("de-DE", "de-DE,de;q=0.9,en;q=0.8"),
    "FR": ("fr-FR", "fr-FR,fr;q=0.9,en;q=0.8"),
    "NL": ("nl-NL", "nl-NL,nl;q=0.9,en;q=0.8"),
    "ES": ("es-ES", "es-ES,es;q=0.9,en;q=0.8"),
    "IT": ("it-IT", "it-IT,it;q=0.9,en;q=0.8"),
    "PT": ("pt-PT", "pt-PT,pt;q=0.9,en;q=0.8"),
    "BR": ("pt-BR", "pt-BR,pt;q=0.9,en;q=0.8"),
    "CA": ("en-CA", "en-CA,en;q=0.9,fr;q=0.8"),
    "AU": ("en-AU", "en-AU,en;q=0.9"),
    "IN": ("en-IN", "en-IN,en;q=0.9,hi;q=0.8"),
    "JP": ("ja-JP", "ja-JP,ja;q=0.9,en;q=0.8"),
    "KR": ("ko-KR", "ko-KR,ko;q=0.9,en;q=0.8"),
    "TR": ("tr-TR", "tr-TR,tr;q=0.9,en;q=0.8"),
    "SA": ("ar-SA", "ar-SA,ar;q=0.9,en;q=0.8"),
    "AE": ("ar-AE", "ar-AE,ar;q=0.9,en;q=0.8"),
    "PL": ("pl-PL", "pl-PL,pl;q=0.9,en;q=0.8"),
    "RO": ("ro-RO", "ro-RO,ro;q=0.9,en;q=0.8"),
    "CZ": ("cs-CZ", "cs-CZ,cs;q=0.9,en;q=0.8"),
    "SE": ("sv-SE", "sv-SE,sv;q=0.9,en;q=0.8"),
    "NO": ("nb-NO", "nb-NO,nb;q=0.9,en;q=0.8"),
    "DK": ("da-DK", "da-DK,da;q=0.9,en;q=0.8"),
    "FI": ("fi-FI", "fi-FI,fi;q=0.9,en;q=0.8"),
    "AT": ("de-AT", "de-AT,de;q=0.9,en;q=0.8"),
    "CH": ("de-CH", "de-CH,de;q=0.9,en;q=0.8"),
    "BE": ("nl-BE", "nl-BE,nl;q=0.9,fr;q=0.8,en;q=0.7"),
    "IL": ("he-IL", "he-IL,he;q=0.9,en;q=0.8"),
    "RU": ("ru-RU", "ru-RU,ru;q=0.9,en;q=0.8"),
    "UA": ("uk-UA", "uk-UA,uk;q=0.9,en;q=0.8"),
}

# ---------------------------------------------------------------------------
# Fallback profile (Egypt / Suez Canal area)
# ---------------------------------------------------------------------------

EGYPT_FALLBACK_DATA = {
    "exit_ip": "",
    "country_code": "EG",
    "city": "Cairo",
    "latitude": 30.0444,
    "longitude": 31.2357,
    "timezone_id": "Africa/Cairo",
    "locale": "ar-EG",
    "accept_language": "ar-EG,ar;q=0.9,en;q=0.8",
}


@dataclass
class GeoProfile:
    proxy: dict
    exit_ip: str
    country_code: str
    city: str
    latitude: float
    longitude: float
    timezone_id: str
    locale: str
    accept_language: str


def _country_to_locale(country_code: str) -> tuple[str, str]:
    """Map country code to (locale, accept_language). Falls back to en-{CC}."""
    if country_code in COUNTRY_TO_LOCALE:
        return COUNTRY_TO_LOCALE[country_code]
    return (f"en-{country_code}", f"en-{country_code},en;q=0.9")


def resolve_proxy_geo(proxy: dict, timeout: int = 10) -> GeoProfile:
    """Resolve a proxy's exit IP and geolocation via ip-api.com."""
    proxy_url = proxy["server"]
    proxies_dict = {
        "http": f"http://{proxy['username']}:{proxy['password']}@{proxy_url.replace('http://', '')}",
        "https": f"http://{proxy['username']}:{proxy['password']}@{proxy_url.replace('http://', '')}",
    }

    try:
        resp = requests.get(
            "http://ip-api.com/json/?fields=query,countryCode,city,lat,lon,timezone",
            proxies=proxies_dict,
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()

        if data.get("countryCode"):
            locale, accept_lang = _country_to_locale(data["countryCode"])
            profile = GeoProfile(
                proxy=proxy,
                exit_ip=data.get("query", ""),
                country_code=data["countryCode"],
                city=data.get("city", ""),
                latitude=data.get("lat", 0.0),
                longitude=data.get("lon", 0.0),
                timezone_id=data.get("timezone", "UTC"),
                locale=locale,
                accept_language=accept_lang,
            )
            logger.info(
                "  Proxy %s → %s (%s, %s, tz=%s)",
                proxy_url, profile.exit_ip, profile.country_code,
                profile.city, profile.timezone_id,
            )
            return profile

    except Exception as e:
        logger.warning("  Proxy %s geo resolution failed: %s", proxy_url, e)

    # Fallback to Egypt defaults
    logger.info("  Proxy %s → using Egypt fallback", proxy_url)
    return GeoProfile(proxy=proxy, **EGYPT_FALLBACK_DATA)


def resolve_all_proxies(proxies: list[dict]) -> dict[str, GeoProfile]:
    """Resolve all proxies, returning dict keyed by proxy server URL.
    14 proxies is well within ip-api.com's 45 req/min free-tier limit."""
    profiles = {}
    for proxy in proxies:
        profiles[proxy["server"]] = resolve_proxy_geo(proxy)
    return profiles


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    test_proxies = []
    for port in range(10011, 10025):
        test_proxies.append({
            "server": f"http://isp.decodo.com:{port}",
            "username": DECODO_USERNAME,
            "password": DECODO_PASSWORD,
        })

    print(f"Resolving {len(test_proxies)} proxies...\n")
    results = resolve_all_proxies(test_proxies)
    for server, profile in results.items():
        print(f"  {server}")
        print(f"    IP: {profile.exit_ip}")
        print(f"    Location: {profile.city}, {profile.country_code}")
        print(f"    Coords: {profile.latitude}, {profile.longitude}")
        print(f"    Timezone: {profile.timezone_id}")
        print(f"    Locale: {profile.locale}")
        print(f"    Accept-Language: {profile.accept_language}")
        print()
