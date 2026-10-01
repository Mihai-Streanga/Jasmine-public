"""Serverul răspunde numai de acasă: `Host` și `Origin`.

Rulare, din rădăcina depozitului:  py tests/test_securitate.py
Iese cu 0 dacă trece, cu 1 dacă nu. Nu cere serverul pornit.

Ce probează: un `Host` străin primește 403 pe orice rută; un `Origin` străin
primește 403 pe POST, înainte de validarea corpului (403, nu 422); cererile de
acasă și cele fără `Origin` (scurtătura, dictarea) trec mai departe.

Ce NU acoperă: filtrul `linkSigur` din punte (e JavaScript) și browserele
care ar trimite `Origin: null`.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

import server  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def main() -> int:
    picate = []

    def verifica(r, asteptat, ce):
        ok = r.status_code == asteptat
        print(("TRECE " if ok else "PICĂ  ") + f"{ce}: {r.status_code}")
        if not ok:
            picate.append(ce)

    acasa = TestClient(server.app, base_url="http://127.0.0.1:8000")
    strain = TestClient(server.app, base_url="http://evil.example")

    verifica(strain.get("/api/viu"), 403, "Host strain pe GET")
    verifica(strain.post("/api/jurnal", json={}), 403, "Host strain pe POST")
    verifica(acasa.get("/api/viu"), 200, "Host de acasa pe GET")
    verifica(acasa.post("/api/jurnal", json={"x": 1},
                        headers={"Origin": "http://evil.example"}),
             403, "Origin strain pe POST")
    verifica(acasa.delete("/api/conversatii/x",
                          headers={"Origin": "http://evil.example"}),
             403, "Origin strain pe DELETE")
    verifica(acasa.post("/api/jurnal", json={"x": 1},
                        headers={"Origin": "http://localhost:8000"}),
             422, "Origin de acasa trece la validare")
    verifica(acasa.post("/api/jurnal", json={"x": 1}), 422,
             "fara Origin trece la validare")

    print("\n" + ("TOTUL TRECE" if not picate else f"{len(picate)} PICATE"))
    return 1 if picate else 0


if __name__ == "__main__":
    sys.exit(main())
