import ssl
from pathlib import Path

import truststore
from pyproj import Transformer

BASE_DIR = Path(__file__).resolve().parent.parent
ONBOARDING_SPEC_PATH = BASE_DIR / "kart.json"
NANOBANANA_PROMPT = (
    "Create a 3d interior visualization from this architectural floor plan. "
    "Show the apartment interior as an isometric cutaway or top-down 3d view, "
    "with rooms, walls, furniture, finishes, and natural light visible. "
    "Do not create an exterior building facade or outside view."
)
PRICE_METRICS_GUIDE = """
Uwzględnij metryki podobne do hedonicznych modeli wyceny i analiz FPRE:
- Cechy lokalu: powierzchnia użytkowa, cena za m², liczba pokoi, układ, piętro, ekspozycja, balkon/taras/ogród, łazienki, komórka, parking, winda, standard wykończenia.
- Cechy budynku: rok budowy, stan/standard budynku, liczba kondygnacji, garaż, efektywność energetyczna, koszty administracyjne, etap inwestycji i ryzyko deweloperskie.
- Mikrolokalizacja: hałas, zieleń, widok, nasłonecznienie, sąsiedztwo, bezpieczeństwo, usługi pieszo, szkoły/przedszkola, transport publiczny, jakość najbliższego otoczenia.
- Makrolokalizacja: dzielnica/miasto, dostępność do centrum i miejsc pracy, infrastruktura, płynność rynku, potencjał wzrostu wartości i odsprzedaży.
- Dane brakujące: każde pole wpływające na cenę lub ryzyko, którego nie da się zweryfikować z oferty/rzutu, oznacz jako brak danych zamiast zgadywać.
""".strip()
GEOPORTAL_SERVICES = {
    "uug": "https://services.gugik.gov.pl/uug/",
    "uldk": "https://uldk.gugik.gov.pl/",
    "kimp": "https://mapy.geoportal.gov.pl/wss/ext/KrajowaIntegracjaMiejscowychPlanowZagospodarowaniaPrzestrzennego",
    "kiskzp": "https://mapy.geoportal.gov.pl/wss/ext/KrajowaIntegracjaStudiumKierunkowZagospodarowaniaPrzestrzennego",
    "egib": "https://integracja.gugik.gov.pl/cgi-bin/KrajowaIntegracjaEwidencjiGruntow",
    "gesut": "https://integracja.gugik.gov.pl/cgi-bin/KrajowaIntegracjaUzbrojeniaTerenu",
    "bdot": "https://integracja.gugik.gov.pl/cgi-bin/KrajowaIntegracjaBazDanychObiektowTopograficznych",
    "orto": "https://mapy.geoportal.gov.pl/wss/service/PZGIK/ORTO/WMS/StandardResolution",
}
WARSAW_SERVICES = {
    "wms": "https://wms.um.warszawa.pl/serwis",
    "wfs": "https://wfs.um.warszawa.pl/serwis",
    "wfs_public": "https://wms2.um.warszawa.pl/geoserver/wfs/wfs",
    "docs": "https://architektura.um.warszawa.pl/-/dane-przestrzenne-2",
}
WARSAW_WGS84_BBOX = {
    "min_lon": 20.851,
    "max_lon": 21.273,
    "min_lat": 52.097,
    "max_lat": 52.369,
}
WARSAW_WFS_LAYERS = [
    {"theme": "transport", "layer": "wfs:METRO_WEJSCIA", "label": "Wejscia do metra", "radius_m": 900},
    {"theme": "transport", "layer": "wfs:PARKINGI_PARK_AND_RIDE", "label": "Parkingi P+R", "radius_m": 1200},
    {"theme": "mobility", "layer": "wfs:STACJE_ROWEROWE", "label": "Stacje rowerowe", "radius_m": 700},
    {"theme": "mobility", "layer": "wfs:TRASY_ROWEROWE", "label": "Trasy rowerowe", "radius_m": 450},
    {"theme": "greenery", "layer": "ns92528565:PLACE_SKWERY", "label": "Place i skwery", "radius_m": 900},
    {"theme": "greenery", "layer": "ns92528565:ZIELEN_DRZEWA", "label": "Drzewa miejskie", "radius_m": 150},
    {"theme": "education", "layer": "ns92528565:OBWODY_SZKOL_PODSTAWOWYCH", "label": "Obwody szkol podstawowych", "radius_m": 60},
    {"theme": "planning", "layer": "ns92528565:PLANY_ZAKRESY_OBOWIAZUJACE", "label": "Zakresy MPZP obowiazujace", "radius_m": 80},
    {"theme": "planning", "layer": "ns92528565:PLANY_ZAKRESY_SPORZADZANE", "label": "Zakresy MPZP sporzadzane", "radius_m": 80},
    {"theme": "services", "layer": "wfs:APTEKI", "label": "Apteki", "radius_m": 700},
    {"theme": "services", "layer": "wfs:POLICJA", "label": "Policja", "radius_m": 1000},
    {"theme": "services", "layer": "ns92528565:BIBLIOTEKI", "label": "Biblioteki", "radius_m": 900},
]
WARSAW_NOISE_WMS_LAYERS = [
    "HALAS_DROGOWY_LDWN_2022",
    "HALAS_DROGOWY_LN_2022",
    "HALAS_TRAMWAJOWY_LDWN_2022",
    "HALAS_TRAMWAJOWY_LN_2022",
    "HALAS_KOLEJOWY_LDWN_2022",
    "HALAS_KOLEJOWY_LN_2022",
    "HALAS_LOTNICZY_LDWN_2022",
    "HALAS_LOTNICZY_LN_2022",
]
CUBICASA5K_SCHEMA = {
    "detectable": ["walls", "doors", "windows", "room_types", "selected_fixtures"],
    "inferred": ["possible_plumbing_core", "possible_shafts", "possible_structural_constraints"],
    "not_directly_detectable": ["load_bearing_status", "wall_materials", "legal_renovation_feasibility"],
}
EPSG2180_TO_WGS84 = Transformer.from_crs("EPSG:2180", "EPSG:4326", always_xy=True)
WGS84_TO_EPSG2180 = Transformer.from_crs("EPSG:4326", "EPSG:2180", always_xy=True)
HTTPX_VERIFY = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
