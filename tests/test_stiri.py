"""Funcțiile pure din `server/stiri.py`: linkuri, categorii, parsarea fluxurilor.

Rulare, din rădăcina depozitului:  py tests/test_stiri.py
Iese cu 0 dacă trece, cu 1 dacă nu. Fără rețea, fără server.

Ce probează: forma canonică a linkului (urmărire, ancoră, www, bară finală),
categoria economică și excluderea pe cale, curățarea textului HTML, parsarea
RSS (cu știrea fără dată aruncată) și a sitemap-ului de știri (forma CNN).

Ce NU acoperă: aducerea din rețea (`_adu`, `aduna_sursa`), regula „sursa prea
veche”, deduplicarea și ordonarea din `_aduna_tot`, catalogul și „dat deja”
de pe disc, contractul MCP.
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

import stiri  # noqa: E402

picate = []


def egal(primit, asteptat, ce):
    ok = primit == asteptat
    print(("TRECE " if ok else "PICĂ  ") + ce + ("" if ok else f": {primit!r} ≠ {asteptat!r}"))
    if not ok:
        picate.append(ce)


RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Digi24</title>
<item>
  <title>Guvernul &#537;i bugetul</title>
  <link>https://www.digi24.ro/stiri/economie/guvernul-si-bugetul-123</link>
  <pubDate>Wed, 01 Oct 2026 09:30:00 +0300</pubDate>
  <description>&lt;p&gt;Primul &lt;b&gt;paragraf&lt;/b&gt;&lt;/p&gt;</description>
</item>
<item>
  <title>Fara data</title>
  <link>https://www.digi24.ro/x</link>
</item>
</channel></rss>"""

SITEMAP = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
        xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">
<url>
  <loc>https://edition.cnn.com/2026/10/01/business/markets</loc>
  <news:news>
    <news:publication_date>2026-10-01T06:00:00Z</news:publication_date>
    <news:title>Markets rally</news:title>
  </news:news>
</url>
<url><loc>https://edition.cnn.com/fara-news</loc></url>
</urlset>"""


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # consola e cp1252, textele au diacritice
    # canonic
    egal(stiri.canonic("https://www.Digi24.ro/a/b/?utm_source=x#sus"), "https://digi24.ro/a/b",
         "urmărirea, ancora, www și bara finală se taie")
    egal(stiri.canonic("https://digi24.ro/a/b"), stiri.canonic("https://www.digi24.ro/a/b/?x=1"),
         "aceeași știre din două fluxuri → același link canonic")

    # e_economica / e_exclusa
    egal(stiri.e_economica("https://x.ro/orice", "economie", []), True,
         "sursa declarată economică")
    egal(stiri.e_economica("https://edition.cnn.com/2026/business/x", "general", ["business"]),
         True, "calea /business/ ridică știrea")
    egal(stiri.e_economica("https://edition.cnn.com/2026/businessman/x", "general", ["business"]),
         False, "doar segment întreg de cale, nu subșir")
    egal(stiri.e_exclusa("https://digi24.ro/Sport/x", ["/sport/"]), True,
         "excluderea nu ține cont de majuscule")

    # curata_textul
    egal(stiri.curata_textul("<p>Unu\n<b>doi</b></p>\xa0trei&amp;patru"), "Unu doi trei&patru",
         "HTML → o singură linie, entități traduse, \\xa0 normalizat")

    config = {"lungime_extras": 10}

    # _parseaza_rss
    r = stiri._parseaza_rss(RSS, {"nume": "Digi24"}, config)
    egal(len(r), 1, "RSS: știrea fără dată se aruncă")
    egal(r[0]["titlu"], "Guvernul și bugetul", "RSS: titlul cu entitate")
    egal(r[0]["data"], datetime(2026, 10, 1, 6, 30, tzinfo=timezone.utc), "RSS: data în UTC")
    egal(r[0]["extras"], "Primul par", "RSS: extrasul curățat și tăiat la lungime_extras")

    # _parseaza_sitemap
    s = stiri._parseaza_sitemap(SITEMAP, {"nume": "CNN"}, config)
    egal(len(s), 1, "sitemap: intrarea fără news: se sare")
    egal((s[0]["titlu"], s[0]["link"], s[0]["extras"]),
         ("Markets rally", "https://edition.cnn.com/2026/10/01/business/markets", ""),
         "sitemap: titlu, link, fără extras")
    egal(s[0]["data"], datetime(2026, 10, 1, 6, 0, tzinfo=timezone.utc), "sitemap: data Z în UTC")

    print("\n" + ("TOTUL TRECE" if not picate else f"{len(picate)} PICATE"))
    return 1 if picate else 0


if __name__ == "__main__":
    sys.exit(main())
