"""ADSB.fi public regional v3 endpoint with its own identity and usage terms.

The documented v3 response uses the ADSBexchange v2 wire format. Reuse the
bounded decoder/transport, never the ADSB.lol source identity or license.
"""

from skytrace.models import SourceAttribution, TrackingArea
from skytrace.providers.adsblol import AdsbLolProvider

ADSBFI_SOURCE = SourceAttribution(
    name="adsbfi",
    attribution="Aircraft data: ADSB.fi",
    url="https://adsb.fi/",
    license="Personal, non-commercial use",
    license_url="https://github.com/adsbfi/opendata#terms",
)


class AdsbFiProvider(AdsbLolProvider):
    source = "adsbfi"
    attribution = ADSBFI_SOURCE
    display_name = "ADSB.fi"

    def request_url(self, area: TrackingArea) -> str:
        return (
            f"https://opendata.adsb.fi/api/v3/lat/{area.latitude_deg}/"
            f"lon/{area.longitude_deg}/dist/{area.radius_nm}"
        )
