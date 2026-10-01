"""Conversațiile pe disc: nu se pierd, nu se suprascriu, nu costă degeaba.

Rulare, din rădăcina depozitului:  py tests/test_conversatii.py
Iese cu 0 dacă trece, cu 1 dacă nu. Nu cere serverul pornit și nu cheamă
modelul: orice încercare de a-l chema e prinsă și numărată.

Ce probează, pe `chat()` adevărat, printr-un `TestClient` fără lifespan, cu
`CONVERSATII` mutat într-un dosar temporar:
  - un fișier de conversație stricat NU e suprascris: cererea se refuză, octeții
    rămân identici, modelul nu e chemat;
  - un id invalid primește 400, fără apel la model;
  - două fire noi în aceeași secundă primesc id-uri diferite;
  - scrierea e atomică: dacă mutarea finală pică, fișierul vechi rămâne întreg.

Ce NU acoperă: bucla de unelte, salvarea după un răspuns adevărat, ștergerea
(mutarea în `conversatii-inchise`).
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
os.environ.setdefault("OPENWEBUI_URL", "http://127.0.0.1:9")
os.environ.setdefault("OPENWEBUI_KEY", "test")

import server  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

apeluri_model = []


class ClientInterzis:
    """Ține locul lui httpx.AsyncClient: orice apel spre model e o picătură."""
    def __init__(self, *a, **k):
        apeluri_model.append(k)
        raise RuntimeError("modelul n-avea voie să fie chemat")


def main() -> int:
    picate = []

    def verifica(conditie, ce):
        print(("TRECE " if conditie else "PICĂ  ") + ce)
        if not conditie:
            picate.append(ce)

    with tempfile.TemporaryDirectory() as tmp:
        server.CONVERSATII = Path(tmp)
        server.httpx.AsyncClient = ClientInterzis
        client = TestClient(server.app, base_url="http://localhost:8000")
        strat = sorted(server.STRATURI)[0]

        # 1. Fișier stricat
        stricat = Path(tmp) / "20260101-120000.json"
        octeti = b'{"titlu": "jumatate", "mesaje": [{"role": "us'
        stricat.write_bytes(octeti)
        r = client.post("/api/chat", json={"mesaj": "salut", "strat": strat,
                                           "id": "20260101-120000"})
        verifica(r.status_code == 409 and r.json().get("ok") is False,
                 f"fir stricat se refuza (status {r.status_code})")
        verifica(stricat.read_bytes() == octeti, "fir stricat ramane neatins")
        verifica(not apeluri_model, "fir stricat nu cheama modelul")

        # 2. Id invalid
        r = client.post("/api/chat", json={"mesaj": "salut", "strat": strat,
                                           "id": "../../server/.env"})
        verifica(r.status_code == 400, f"id invalid → 400 (status {r.status_code})")
        verifica(not apeluri_model, "id invalid nu cheama modelul")

        # 3. Două fire noi în aceeași secundă
        a = server.id_conv_nou()
        b = server.id_conv_nou()
        verifica(a != b, f"doua fire noi, doua id-uri ({a}, {b})")
        server.ID_REZERVATE.clear()
        (Path(tmp) / f"{a}.json").write_text("{}", encoding="utf-8")
        c = server.id_conv_nou()
        verifica(c != a, f"id existent pe disc nu se refoloseste ({c})")
        server.ID_REZERVATE.clear()

        # 4. Scriere atomică
        server.scrie_conv("atomic", {"titlu": "vechi"})
        vechi = (Path(tmp) / "atomic.json").read_bytes()
        replace_adevarat = server.os.replace

        def replace_care_pica(*a, **k):
            raise OSError("cadere simulata")
        server.os.replace = replace_care_pica
        try:
            server.scrie_conv("atomic", {"titlu": "nou"})
        except OSError:
            pass
        finally:
            server.os.replace = replace_adevarat
        verifica((Path(tmp) / "atomic.json").read_bytes() == vechi,
                 "scriere cazuta lasa fisierul vechi intreg")

    print("\n" + ("TOTUL TRECE" if not picate else f"{len(picate)} PICATE"))
    return 1 if picate else 0


if __name__ == "__main__":
    sys.exit(main())
