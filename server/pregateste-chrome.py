"""Pune in profilul kiosk-ului preferintele fara de care puntea nu arata curat.

De ce exista fisierul asta: `chrome-profil\\` e in `.gitignore` — e cache de
browser, nu identitatea lui JA.S.Mine. Deci orice reglaj facut acolo cu mana
dispare la restaurarea pe masina noua, in tacere. Aici sta scris ce trebuie sa
fie adevarat despre profil, si se aplica la fiecare pornire.

Se ruleaza INAINTE de Chrome, din `reporneste-jasmine.vbs`. Idempotent: daca
totul e deja pus, nu scrie nimic si nu spune nimic.

`reporneste-jasmine.vbs` il ruleaza NECONDITIONAT, inclusiv la intoarcerea „la
cald", cu Chrome viu. Chrome isi tine preferintele in memorie si le scrie el la
iesire, deci o scriere de aici, sub un Chrome viu, se pierde in cel mai bun caz
si ii sterge preferinte adevarate in cel mai rau. De-asta se intreaba intai
daca ruleaza ceva PE PROFILUL ASTA, dupa linia de comanda (`chrome_viu`), si la
cald nu se atinge nimic.

`--disable-features=Translate` NU stinge bula „traduci pagina?" (masurat).
Parghia e preferinta profilului, nu linia de comanda — de-asta reglajul
trebuie sa aiba unde sta.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

PROFIL = Path(__file__).resolve().parent.parent / "chrome-profil"
PREF = PROFIL / "Default" / "Preferences"
CACHE = PROFIL / "Default" / "Cache"


def chrome_viu() -> bool | None:
    """Ruleaza un Chrome PE PROFILUL ASTA? `None` = nu se poate sti.

    Se cauta dupa LINIA DE COMANDA, niciodata dupa numele imaginii: un Chrome
    obisnuit poate fi deschis alaturi, si n-are nicio treaba cu kiosk-ul. Semnatura e
    `--user-data-dir` pe folderul profilului — acelasi tipar cu `SEMNATURA_KIOSK`
    din `server.py`, si acelasi cu `DockerPornit()` din `reporneste-jasmine.vbs`:
    se intreaba, nu se cronometreaza.

    Se numara SI copiii (`--type=`), nu doar procesul-parinte cu fereastra: aici
    nu se cauta o fereastra de minimizat, ci un motiv de a nu scrie. Orice
    proces viu pe profil e destul.

    `None` la orice eroare, si cine intreaba trateaza `None` ca „viu". Asimetria
    e a costurilor: o pornire rece in care nu s-au pus preferintele se repara
    singura la urmatoarea, iar profilul ramane intreg; o scriere sub un Chrome
    viu pierde preferinte fara sa spuna nimeni nimic.
    """
    ps = ("@(Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
          "Where-Object { $_.CommandLine -like '*' + $env:SEMNATURA + '*' "
          "}).Count")
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=15,
            env=dict(os.environ, SEMNATURA=str(PROFIL)),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return int((r.stdout or "").strip().splitlines()[-1]) > 0
    except Exception:
        return None


def goleste_cache() -> int:
    """Sterge cache-ul HTTP al kiosk-ului. Intoarce cate fisiere a sters.

    Serverul trimite `Cache-Control: no-store`, dar antetul apara doar
    raspunsurile VIITOARE. Intrarile deja scrise in `Cache_Data` raman pe disc
    si se pot servi mai departe — s-a intamplat: kiosk-ul arata puntea de acum
    doua versiuni, iar pe server nu se vedea nimic. De-aia se sterg aici,
    inainte de pornire, cat nu le tine niciun Chrome deschis.

    Nu se pierde nimic: puntea e un singur fisier local, de la serverul de
    alaturi.
    """
    if not CACHE.exists():
        return 0
    sterse = 0
    for f in CACHE.rglob("*"):
        if not f.is_file():
            continue
        try:
            f.unlink()
            sterse += 1
        except OSError:
            # Tinut de cineva (un Chrome ramas deschis): se sare peste. Un
            # cache pe jumatate golit e tot mai bun decat unul intreg, si
            # nimic de aici n-are voie sa opreasca pornirea.
            pass
    return sterse


def pregateste() -> list[str]:
    """Intoarce ce a schimbat. Lista goala inseamna „era deja bine"."""
    if not PREF.exists():
        return []                      # profil nou; Chrome il face la prima pornire
    try:
        p = json.loads(PREF.read_text(encoding="utf-8"))
    except Exception as ex:
        print(f"  chrome: nu pot citi Preferences ({type(ex).__name__})")
        return []

    schimbari = []

    # 1. Bula de traducere. Puntea e lang="ro"; daca romana nu e printre limbile
    #    acceptate, Chrome ofera s-o traduca la FIECARE pornire, in dreapta sus.
    if p.get("translate", {}).get("enabled") is not False:
        p.setdefault("translate", {})["enabled"] = False
        schimbari.append("translate.enabled=false")

    acceptate = p.get("intl", {}).get("accept_languages", "en-US,en")
    if "ro" not in [x.strip() for x in acceptate.split(",")]:
        p.setdefault("intl", {})["accept_languages"] = "ro,ro-RO," + acceptate
        schimbari.append("accept_languages+=ro")

    blocate = set(p.get("translate_blocked_languages", []))
    if not {"ro", "en"} <= blocate:
        p["translate_blocked_languages"] = sorted(blocate | {"ro", "en"})
        schimbari.append("translate_blocked_languages+=ro,en")

    # 2. Microfonul pentru punte. Fara el, dictarea cere o permisiune pe care o
    #    fereastra `--app --kiosk` NU are unde s-o arate: nu exista bara de
    #    adresa, deci nu exista bula de intrebare.
    #
    #    NU prin CDP: `Browser.grantPermissions` pare sa mearga, dar
    #    permisiunea tine doar cat tine conexiunea clientului. Cand scriptul se
    #    deconecteaza, ea se retrage, iar fluxul ramane deschis SI MUT — niciun
    #    cod de eroare, numai zerouri. Un flux care tace fara sa se planga
    #    fabrica exact defectul pe care apoi il cauti in alta parte.
    exceptii = (p.setdefault("profile", {}).setdefault("content_settings", {})
                 .setdefault("exceptions", {}).setdefault("media_stream_mic", {}))
    for origine in ("http://localhost:8000,*", "http://127.0.0.1:8000,*"):
        if exceptii.get(origine, {}).get("setting") != 1:
            exceptii[origine] = {"setting": 1}          # 1 = permis
            schimbari.append(f"microfon permis pentru {origine.split(',')[0]}")

    # 3. Marcajul de „a crapat". Il lasa orice oprire fortata a lui Chrome, iar
    #    la pornirea urmatoare sesiunea se restaureaza intr-o fereastra NORMALA,
    #    cu bara de titlu, in loc de kiosk. /api/inchide-puntea inchide politicos
    #    tocmai ca sa nu-l lase — dar daca a ramas de la o oprire brutala
    #    (Stop-Process, pana de curent), se sterge aici.
    #
    #    RANDUL DE MAI JOS E ADEVARAT DOAR PE PROFIL INCHIS, si de-aia apelantul
    #    intreaba intai `chrome_viu()`: `Crashed` e valoarea NORMALA cat timp
    #    Chrome ruleaza — o scrie la pornire si o schimba in `Normal` abia la
    #    iesirea curata. Citit sub un Chrome viu, marcajul nu e cicatrice, e
    #    puls; „reparat" acolo, raportam o reparatie care nu se aplica.
    if p.get("profile", {}).get("exit_type") not in (None, "Normal"):
        p.setdefault("profile", {})["exit_type"] = "Normal"
        schimbari.append("exit_type=Normal")

    if schimbari:
        PREF.write_text(json.dumps(p, ensure_ascii=False), encoding="utf-8")
    return schimbari


if __name__ == "__main__":
    # Nefatal, mereu: o preferinta de browser n-are voie sa opreasca puntea.
    try:
        viu = chrome_viu()
        if viu is not False:
            # SE SPUNE DE CE, si asta nu e politete: pornirea rece si intoarcerea
            # la cald arata identic de aici incolo (aceeasi fereastra, acelasi
            # kiosk). Fara randul asta, singurul semn ca reglajele n-au fost
            # aplicate ar fi lipsa oricarui semn.
            print("  chrome: ruleaza deja pe profilul asta"
                  if viu else "  chrome: nu pot afla daca ruleaza")
            print("  chrome: profilul nu se atinge (intoarcere la cald)")
            sys.exit(0)
        n = goleste_cache()
        if n:
            print(f"  chrome: cache golit ({n} fisiere)")
        s = pregateste()
        if s:
            print("  chrome: " + ", ".join(s))
    except Exception as ex:
        print(f"  chrome: pregatirea a esuat, merge mai departe "
              f"({type(ex).__name__}: {ex})")
    sys.exit(0)
