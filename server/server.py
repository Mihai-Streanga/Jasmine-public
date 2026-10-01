"""
JA.S.Mine — serverul local.

Ce face:
  - servește puntea la http://localhost:8000, numai pentru localhost
  - jurnalul de bord (SHIP'S LOG), în date/jurnal.jsonl
  - proiectele (ENGINEERING), din date/proiecte.json
  - RECON: client MCP către Agentul 051, pe 8051
  - COMMS: raportul de mail, prin mail.py (doar citire)
  - SENSORS: raportul de știri, prin stiri.py
  - MUNTELE: clipurile camerei lui Ramana, de pe disc
  - chat pe straturi (Sky, Socrate, Ramana), cu buclă de unelte
  - conversațiile pe disc, în date/conversatii/ — discul e sursa istoricului
  - dictare locală prin /api/transcrie: OpenVINO pe iGPU, încărcat în fundal
    după pornire; faster-whisper ca rezervă, încărcată la prima cădere
  - sincronizarea persona: promptul din persona\\*.md ajunge în Open WebUI,
    la pornire și prin /api/persona/sincronizeaza
  - ieșirea: stinge fereastra, Docker și, ultimul, serverul

Ce NU face: streaming (răspunsul vine întreg, nu pe bucăți).

Pornire:  wscript server\\reporneste-jasmine.vbs  (sau `py server.py`
          din folderul server, fără fereastră)
Oprire:   × de pe hartă, sau Ctrl+C în fereastra în care rulează.

Nu pornește fără persona/straturi.json — vezi incarca_straturi().
"""

from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from urllib.parse import urlparse
import asyncio
import httpx
import io
import json
import mail
import os
import re
import stiri
import subprocess
import sys
import threading
import time
import uvicorn
import wave

# ── Ieșirea se scrie UTF-8, oriunde ar duce ──────────────────────────
# Cu stdout pe conductă, Python cade pe `cp1252`, iar primul `print` cu
# diacritice („pornește") ridică `UnicodeEncodeError` înainte ca serverul să
# asculte. Pe consolă merge, deci defectul apare exact când cineva vrea să
# citească ce spune serverul.
#
# `errors="replace"`: o diacritică pierdută într-un jurnal e o zgârietură, o
# pornire ratată e o pană.
for _canal in (sys.stdout, sys.stderr):
    try:
        _canal.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass          # pythonw n-are canale; lipsa lor nu opreste serverul

# ── Căile ────────────────────────────────────────────────────────────
# Scriptul stă în server/ → rădăcina lui JA.S.Mine e folderul părinte.
# Nimic hardcodat: dacă muți depozitul pe alt disc sau pe altă mașină, merge la fel.

RADACINA = Path(__file__).resolve().parent.parent
PUNTE = RADACINA / "punte.html"
FONTURI = RADACINA / "fonturi"
# Cache-ul de compilare al OpenVINO, în AFARA depozitului, ca modelele:
# ~800 MB derivați din model și din driverul plăcii, deci nu se salvează și se
# rescrie singur când se schimbă oricare din ele.
#
# Compilarea durează ~4 s fără cache și ~0,9 s cu el, iar cât compilează
# conducta serverul nu răspunde la nimic (vezi `punte()`). Cifrele sunt
# măsurate pe mașina autorului.
CACHE_OV = Path.home() / ".cache" / "openvino-jasmine"
DATE = RADACINA / "date"
JURNAL = DATE / "jurnal.jsonl"
# Unde ajung intrările scoase din jurnal cu `×`. NU se aruncă: puntea spune, în
# manual, că „un jurnal care pierde în tăcere e mai rău decât unul care lipsește",
# iar gestul care curăță lista n-are voie să contrazică fix propoziția aia. Nu se
# arată nicăieri pe ecran — e sertarul de dedesubt, nu a doua listă de citit.
JURNAL_INCHIS = DATE / "jurnal-inchis.jsonl"
# Adăugarea e o singură linie scrisă la coadă, dar ȘTERGEREA rescrie fișierul
# întreg. Fără zăvor, un `jurnal_scrie` venit de la Sky exact atunci s-ar scrie
# în fișierul vechi și ar dispărea odată cu rescrierea — o intrare pierdută
# tăcut, adică exact ce nu vrem. Un singur zăvor pentru amândouă ușile.
JURNAL_LACAT = threading.Lock()
# Jurnalul probelor de trezire. Urmă lăsată de folosire, nu configurare — deci
# în `date\`, unde `.gitignore` îl ține afară din depozit.
TREZIRE_JURNAL = DATE / "trezire.jsonl"
# Un singur fir scrie o dată în el — vezi trezire_proba().
TREZIRE_LACAT = threading.Lock()
# Jurnalul ieșirilor din JA.S.Mine. Consola serverului moare odată cu ieșirea,
# deci ce a pățit o ieșire (ferestre oprite, forțate) se scrie aici, ca să se
# poată reciti.
IESIRE_JURNAL = DATE / "iesire.jsonl"

DATE.mkdir(exist_ok=True)          # creează date/ dacă nu există
JURNAL.touch(exist_ok=True)        # creează fișierul gol la prima pornire

CONVERSATII = DATE / "conversatii"
CONVERSATII.mkdir(exist_ok=True)
# Conversațiile scoase din listă cu `×`. Nu se aruncă: se mută aici, ca rândurile
# din `jurnal-inchis.jsonl`. Folderul e frate cu `conversatii/`, deci pe același
# volum — de-asta mutarea poate fi o singură operație atomică.
CONVERSATII_INCHISE = DATE / "conversatii-inchise"
CONVERSATII_INCHISE.mkdir(exist_ok=True)

PROIECTE = DATE / "proiecte.json"
STRATURI_FISIER = RADACINA / "persona" / "straturi.json"
DOSAR_FISIER = RADACINA / "persona" / "dosar.md"

# Configurarea (adrese, token) stă în server/.env, lângă acest fișier.
# Calea e explicită, nu relativă la folderul din care s-a pornit serverul.
load_dotenv(Path(__file__).resolve().parent / ".env")

# Numele serverului ăstuia, scris o dată. Apare peste tot unde un eșec trebuie să
# spună CINE l-a produs — vezi executa_unealta() și raportul de sincronizare a
# persona.
SERVER_LOCAL = "serverul JA.S.Mine"


# ── Straturile (straturi.json) ───────────────────────────────────────
# Tabelul strat → model. Cheia e STRATUL („sky", „socrate"), nu id-ul modelului
# din Open WebUI. Puntea trimite straturi; id-urile de model trăiesc doar aici.
#
# Stă în persona/, lângă prompturile pe care le descrie, fiindcă e configurare
# scrisă de om — nu date acumulate de sistem.
#
# Câmpul `prompt` e calea fișierului de persona, relativă la rădăcina depozitului, citită
# de sincronizeaza_persona(). Un strat fără câmpul ăsta e legitim și e sărit.
# JSON n-are comentarii, de-asta explicația e aici.

MESAJ_FARA_STRATURI = (
    "Tabelul de straturi nu se poate citi. Serverul NU porneste fara el.\n"
    "\n"
    f"  Fisierul:  {STRATURI_FISIER}\n"
    "  Forma:     {\"sky\": {\"model\": \"sky\", \"eticheta\": \"Sky\",\n"
    "                       \"asteptare\": \"Sky verifica…\", \"prompt\": \"persona/sky.md\"}}\n"
    "\n"
    "  Cheia e STRATUL, valoarea `model` e id-ul din Open WebUI (Workspace →\n"
    "  Models). Fara tabel, puntea n-ar avea cum sa ceara un strat, iar serverul\n"
    "  ar trebui sa ghiceasca un model implicit — exact ce am scos."
)


def incarca_straturi() -> dict:
    """Citește tabelul o dată, la pornire. Lipsa lui oprește serverul.

    Tabelul se schimbă rar, dar greșit sau lipsă face inutilă orice cerere de
    chat; mai bine se află la pornire decât la prima întrebare. Fără configurarea
    esențială, serverul nu pornește pe jumătate: spune ce lipsește și se oprește.
    """
    # utf-8-sig, nu utf-8: fișierele astea sunt editate de mână, iar unele
    # editoare și PowerShell 5.1 scriu UTF-8 cu BOM. Cu utf-8 curat, un fișier
    # care arată perfect în editor crapă cu „JSON stricat la linia 1, coloana 1".
    try:
        date = json.loads(STRATURI_FISIER.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise RuntimeError(MESAJ_FARA_STRATURI + "\n\n  Motiv: fisierul nu exista.")
    except json.JSONDecodeError as ex:
        raise RuntimeError(
            MESAJ_FARA_STRATURI
            + f"\n\n  Motiv: JSON stricat la linia {ex.lineno}, coloana {ex.colno}.")
    except Exception as ex:
        raise RuntimeError(MESAJ_FARA_STRATURI + f"\n\n  Motiv: {type(ex).__name__}: {ex}")

    if not isinstance(date, dict) or not date:
        raise RuntimeError(MESAJ_FARA_STRATURI
                           + "\n\n  Motiv: fisierul nu contine niciun strat.")
    for strat, d in date.items():
        if not isinstance(d, dict) or not str(d.get("model") or "").strip():
            raise RuntimeError(
                MESAJ_FARA_STRATURI
                + f"\n\n  Motiv: stratul {strat!r} nu are un `model` nevid.")
    return date


try:
    STRATURI = incarca_straturi()
except RuntimeError as ex:
    # Mesaj curat, fără traceback: cine pornește serverul are nevoie să știe ce
    # lipsește, nu prin care linie de cod s-a aflat.
    print()
    print("  JA.S.Mine - REFUZ SA PORNESC")
    print()
    for rand in str(ex).splitlines():
        print(("  " + rand) if rand else "")
    print()
    raise SystemExit(1)


# ── Sincronizarea persona (fișier .md → Open WebUI) ──────────────────
# FIȘIERUL E SURSA: e în git și e scris de om. Open WebUI primește. Ce s-ar
# scrie de mână în interfața lui se pierde la următoarea sincronizare — dar nu în
# tăcere: copia veche ajunge pe disc înainte de fiecare suprascriere.
#
# De ce nu trimite serverul promptul la fiecare cerere (sursă unică adevărată,
# același cost): ar lăsa fără persona conversațiile pornite direct din interfața
# Open WebUI.

PERSONA_SUPRASCRIS = DATE / "persona-suprascris"

# Starea ultimei sincronizări, ca DICTARE: se citește din /api/persona/stare.
# Fără ea, o sincronizare picată la pornire ar fi invizibilă până când cineva ar
# observa că Socrate răspunde ca acum trei versiuni.
PERSONA = {"gata": False, "motiv": "nesincronizat", "cand": None, "straturi": {},
           "incercari": 0}

# Pauzele dintre încercările paznicului de la pornire, în secunde, plus plafonul
# pentru restul. Eșecurile nu costă la fel: Open WebUI care încă pornește refuză
# conexiunea în milisecunde, unul pornit pe jumătate poate ține apelul până la
# plafonul lui. La pauză fixă mică, al doilea ar ține un fir ocupat toată ziua.
#
# Plafonul de 300 s e pentru „Docker nu pornește deloc": motorul poate fi pornit
# cu mâna peste o oră, iar atunci sincronizarea trebuie să vină singură.
PAUZE_SINCRONIZARE = (5, 15, 30, 60)
PAUZA_SINCRONIZARE_MAXIMA = 300


MARCAJ_DOSAR = re.compile(r"^[ \t]*\{\{dosar:([a-z0-9-]+)\}\}[ \t]*$", re.MULTILINE)


def citeste_capitolele_dosarului() -> dict[str, str]:
    """Capitolele din persona\\dosar.md, tăiate pe titlurile de nivel 2.

    Rândurile care încep cu „>" se ARUNCĂ: acolo stau dovada, sursa și motivul,
    care nu intră în niciun prompt. Tăietura o ține codul, nu disciplina cuiva.
    Un capitol scris întreg ca bloc de citat nu dă nimic în prompt.

    Un titlu care nu e nume de capitol (cu spații sau majuscule) e sărit: e
    documentație, nu capitol.
    """
    # utf-8-sig din același motiv ca la straturi.json și la fișierele de persona.
    linii = DOSAR_FISIER.read_text(encoding="utf-8-sig").splitlines()

    capitole: dict[str, list[str]] = {}
    curent = None
    for linie in linii:
        if linie.startswith("## "):
            nume = linie[3:].strip()
            # Ancora e chiar sluggul: capitolele poartă numele cu care sunt
            # chemate. Un titlu cu spații sau majuscule nu e capitol.
            curent = nume if MARCAJ_DOSAR.fullmatch("{{dosar:%s}}" % nume) else None
            if curent:
                capitole[curent] = []
            continue
        # „---" separă capitolele în fișier. Fără rândul ăsta ar ajunge o linie
        # orizontală la coada fiecărui capitol expandat, în mijlocul promptului.
        if curent is not None and linie.strip() != "---" and not linie.lstrip().startswith(">"):
            capitole[curent].append(linie)

    return {k: "\n".join(v).strip() for k, v in capitole.items()}


def expandeaza_dosar(text: str, cale: Path) -> str:
    """Înlocuiește fiecare {{dosar:capitol}} cu corpul capitolului.

    `cale` e fișierul de persona din care vine textul, și intră în orice eroare:
    fără ea, mesajul spune că lipsește un capitol, dar nu și cine îl cerea — iar
    numele care lipsește dintr-o eroare îl inventează modelul.

    Pică zgomotos, ca restul citirii de prompt. Un marcaj lăsat neexpandat ar
    ajunge așa cum e în Open WebUI, iar Sky ar primi drept instrucțiune un text
    care arată a variabilă neînlocuită.
    """
    if not MARCAJ_DOSAR.search(text):
        return text

    if not DOSAR_FISIER.exists():
        raise ValueError(
            f"{cale.name}: cere din dosar, dar {DOSAR_FISIER.name} nu exista"
        )

    capitole = citeste_capitolele_dosarului()

    def inlocuieste(potrivire: re.Match) -> str:
        nume = potrivire.group(1)
        if nume not in capitole:
            raise ValueError(
                f"{cale.name}: cere capitolul '{nume}', care nu e in "
                f"{DOSAR_FISIER.name} (are: {', '.join(sorted(capitole)) or 'niciunul'})"
            )
        corp = capitole[nume]
        if not corp:
            raise ValueError(
                f"{cale.name}: capitolul '{nume}' din {DOSAR_FISIER.name} e gol "
                f"dupa ce s-au scos randurile de dovada"
            )
        return corp

    return MARCAJ_DOSAR.sub(inlocuieste, text)


def citeste_prompt_din_md(cale: Path) -> str:
    """Promptul dintr-un fișier de persona: blocul ``` de după titlul cu „prompt".

    NU „primul bloc din fișier": s-ar rupe tăcut în ziua în care cineva pune un
    exemplu de cod mai sus. Ancora e titlul: primul care conține
    „prompt", de exemplu „## Promptul".

    Orice nepotrivire ridică excepție cu numele fișierului. Un prompt ghicit pe
    jumătate n-ar fi o eroare, ar fi o persona schimbată fără ca nimeni s-o ceară.
    """
    # utf-8-sig din același motiv ca la straturi.json: fișierele sunt scrise de
    # mână, iar un BOM ar intra în prima linie a promptului.
    linii = cale.read_text(encoding="utf-8-sig").splitlines()

    titlu = None
    for i, linie in enumerate(linii):
        if linie.startswith("#") and "prompt" in linie.lower():
            titlu = i
            break
    if titlu is None:
        raise ValueError(f"{cale.name}: niciun titlu care sa contina 'prompt'")

    deschis = None
    for i in range(titlu + 1, len(linii)):
        if linii[i].rstrip() != "```":
            continue
        if deschis is None:
            deschis = i
            continue
        text = "\n".join(linii[deschis + 1:i]).strip()
        if not text:
            raise ValueError(f"{cale.name}: blocul de prompt e gol")
        # Expandarea se face AICI, nu la trimitere: sincronizarea compară cu ce e
        # în Open WebUI, iar dacă ar compara textul cu marcaje ar raporta o
        # divergență la fiecare pornire, la infinit. Tot de-aici o vede și
        # /api/persona/stare, fără să știe că dosarul există.
        return expandeaza_dosar(text, cale)

    # Cele două cazuri se numesc separat: „neinchis" pus pe un fișier care n-are
    # niciun bloc ar trimite omul să caute o ghilimea lipsă acolo unde lipsește
    # blocul întreg.
    if deschis is None:
        raise ValueError(f"{cale.name}: niciun bloc ``` dupa titlul de prompt")
    raise ValueError(f"{cale.name}: bloc ``` neinchis dupa titlul de prompt")


async def sincronizeaza_persona(scrie: bool = True) -> dict:
    """Duce promptul din .md în Open WebUI, pentru fiecare strat care are unul.

    `scrie=False` doar compară: aceleași citiri, zero efecte. E ușa deterministă
    a funcției — se poate întreba „ce e nesincronizat?" fără să se schimbe
    nimic în motor.

    Scrierea nu se crede pe cuvânt: după POST se citește înapoi și se compară.

    Întoarce un raport pe straturi. Nu ridică excepție pentru un strat stricat:
    un fișier prost n-are voie să oprească sincronizarea celorlalte.

    FIECARE EȘEC ÎȘI NUMEȘTE SURSA (`sursa`), fiindcă cele trei se repară în trei
    locuri: în `persona\\*.md`, în Open WebUI (Docker, cheie, model lipsă), sau pe
    discul de aici. Un model formulează ce a primit: dacă textul n-are nume,
    numele îl pune el.
    """
    baza = os.environ["OPENWEBUI_URL"].rstrip("/")
    antet = {"Authorization": f"Bearer {os.environ['OPENWEBUI_KEY']}"}
    raport: dict = {}

    async with httpx.AsyncClient(timeout=30) as client:
        for strat, descriere in STRATURI.items():
            cale_relativa = str(descriere.get("prompt") or "").strip()
            if not cale_relativa:
                continue
            model = descriere["model"]
            rand = {"model": model, "fisier": cale_relativa, "stare": "eroare"}
            raport[strat] = rand

            try:
                dorit = citeste_prompt_din_md(RADACINA / cale_relativa)
            except Exception as ex:
                rand["sursa"] = "fisierul de persona"
                rand["motiv"] = f"{type(ex).__name__}: {ex}"
                continue

            # Obiectul întreg, nu doar promptul: ModelForm cere id, name, meta și
            # params, iar `extra='ignore'` înseamnă că un corp parțial ar șterge
            # tăcut descrierea și capabilitățile modelului. Se trimite înapoi
            # exact ce s-a citit, cu un singur câmp schimbat.
            try:
                r = await client.get(f"{baza}/api/v1/models/model",
                                     params={"id": model}, headers=antet)
                r.raise_for_status()
                obiect = r.json()
            except Exception as ex:
                rand["sursa"] = "Open WebUI"
                rand["motiv"] = f"nu pot citi modelul: {type(ex).__name__}: {ex}"
                continue

            actual = str((obiect.get("params") or {}).get("system") or "")
            rand["caractere_fisier"] = len(dorit)
            rand["caractere_motor"] = len(actual)

            # Comparație pe text normalizat: Open WebUI păstrează un \n final pe
            # care fișierul nu-l are. Fără strip, Sky ar fi fost rescris la
            # fiecare pornire pentru un caracter invizibil.
            if actual.strip() == dorit:
                rand["stare"] = "la zi"
                continue

            if not scrie:
                rand["stare"] = "in urma"
                continue

            # Copia veche ÎNAINTE de scriere, de fiecare dată când conținutul
            # diferă. Costă câțiva kilobytes și e singura plasă sub „fișierul e
            # sursa": dacă cineva chiar scrisese ceva în interfață, se poate lua
            # de aici. date\ e deja ignorat de git, cu excepții enumerate.
            try:
                PERSONA_SUPRASCRIS.mkdir(parents=True, exist_ok=True)
                nume = re.sub(r"[^A-Za-z0-9_.-]", "_", model)
                cand = datetime.now().strftime("%Y%m%d-%H%M%S")
                (PERSONA_SUPRASCRIS / f"{nume}-{cand}.txt").write_text(
                    actual, encoding="utf-8")
            except Exception as ex:
                # Discul de aici, nu motorul: copia veche se scrie în date\.
                rand["sursa"] = SERVER_LOCAL
                rand["motiv"] = f"nu pot salva copia veche, nu scriu: {type(ex).__name__}: {ex}"
                continue

            obiect.setdefault("params", {})["system"] = dorit
            try:
                r = await client.post(f"{baza}/api/v1/models/model/update",
                                      params={"id": model}, headers=antet,
                                      json=obiect)
                r.raise_for_status()
            except Exception as ex:
                rand["sursa"] = "Open WebUI"
                rand["motiv"] = f"scrierea a esuat: {type(ex).__name__}: {ex}"
                continue

            try:
                r = await client.get(f"{baza}/api/v1/models/model",
                                     params={"id": model}, headers=antet)
                r.raise_for_status()
                confirmat = str((r.json().get("params") or {}).get("system") or "")
            except Exception as ex:
                rand["sursa"] = "Open WebUI"
                rand["motiv"] = f"scris, dar nu pot verifica: {type(ex).__name__}: {ex}"
                continue

            if confirmat.strip() != dorit:
                rand["sursa"] = "Open WebUI"
                rand["motiv"] = (f"scris, dar motorul are {len(confirmat)} caractere "
                                 f"in loc de {len(dorit)}")
                continue

            rand["stare"] = "adus la zi"
            rand["caractere_motor"] = len(confirmat)

    return raport


async def sincronizeaza_si_noteaza() -> tuple[dict, dict]:
    """Sincronizează ȘI scrie starea în PERSONA. Întoarce (raport, straturi rele).

    Notarea stă aici, nu în fiecare ușă: o ușă care ar uita-o ar face o
    sincronizare pe care /api/persona/stare n-ar vedea-o, iar indicatorul ar
    liniști și ar minți.

    Excepțiile trec mai departe: cine cheamă hotărăște ce face cu ele (endpoint-ul
    întoarce 502, paznicul de la pornire mai încearcă).
    """
    raport = await sincronizeaza_persona()
    rele = {s: r for s, r in raport.items() if r["stare"] == "eroare"}
    PERSONA["straturi"] = raport
    PERSONA["cand"] = datetime.now().isoformat(timespec="seconds")
    PERSONA["gata"] = not rele
    PERSONA["motiv"] = "" if not rele else "; ".join(
        f"{s}: {r.get('motiv', '?')}" for s, r in rele.items())
    return raport, rele


async def sincronizeaza_la_pornire() -> None:
    """PAZNIC: încearcă până reușește, cu pauze care cresc. Se oprește la primul succes.

    Open WebUI are nevoie de mai mult timp decât serverul ca să pornească (~19 s
    când pornesc împreună, mai mult la boot). Un număr fix de încercări ar cădea
    înainte de momentul în care ar fi mers; „mai târziu la boot" n-are o cifră.
    Pauzele cresc, ca un motor care nu vine deloc să nu țină un fir ocupat toată
    ziua.

    În fundal, nu blocant: un motor lent la pornire n-are voie să țină puntea
    închisă. Eșecul nu oprește nimic — se reține motivul, plus a câta încercare
    e, și se citesc din /api/persona/stare.
    """
    esecuri = 0
    while True:
        # Altcineva a reușit între timp: ușa manuală. Munca
        # paznicului era exact starea asta, deci n-o mai face a doua oară.
        if PERSONA["gata"]:
            return

        reusit = False
        try:
            raport, rele = await sincronizeaza_si_noteaza()
            reusit = not rele
        except Exception as ex:
            PERSONA["motiv"] = f"{type(ex).__name__}: {ex}"

        if reusit:
            for strat, r in raport.items():
                if r["stare"] == "adus la zi":
                    print(f"  persona: {strat} adus la zi "
                          f"({r['caractere_fisier']} caractere)")
            if esecuri:
                print(f"  persona: sincronizata dupa {esecuri} incercare(ri) "
                      f"picate")
            PERSONA["incercari"] = esecuri
            return

        esecuri += 1
        PERSONA["incercari"] = esecuri
        # Se spune O DATĂ, la prima ratare, nu la fiecare: un paznic care scrie
        # la infinit în consolă îneacă exact rândurile pentru care e citită.
        if esecuri == 1:
            print(f"  persona: NESINCRONIZATA - {PERSONA['motiv']}; "
                  f"paznicul incearca mai departe")
        pauza = (PAUZE_SINCRONIZARE[esecuri - 1]
                 if esecuri <= len(PAUZE_SINCRONIZARE)
                 else PAUZA_SINCRONIZARE_MAXIMA)
        await asyncio.sleep(pauza)


# ── Dictarea (Whisper local, pe iGPU) ────────────────────────────────
# Motorul e OpenVINO pe placa grafică integrată, local, int8. NU consumă tokeni.
# int8 și fp16 dau text identic, iar int8 e mai rapid și cere mai puțin commit
# (măsurat pe mașina autorului).
#
# **Un singur motor viu.** `faster-whisper` rămâne rezervă, încărcată LENEȘ, la
# prima cădere a iGPU-ului: în repaus nu costă nimic, iar în ziua în care iGPU-ul
# cedează, textul vine mai încet în loc de deloc.
#
# Audio-ul NU atinge discul. Vine ca octeți din browser, se transcrie din
# memorie și dispare cu cererea.
#
# Spre deosebire de straturi.json, lipsa dictării NU oprește serverul.
# Fără tabelul de straturi nu funcționează nimic; fără Whisper, RECON,
# jurnalul și chatul merg întregi. Un microfon n-are voie să omoare biroul.

DICTARE_FISIER = Path(__file__).resolve().parent / "dictare.json"

# Starea, singurul loc unde se știe dacă dictarea e vie. `motiv` e nevid doar
# când `gata` e False — și e ce vede omul în /api/stare.
#
# `reglaje` e INSTANTANEUL de la pornire: cu ce s-a construit modelul. Nu se
# rescrie la recitirile calde, fiindcă e singura probă a ceea ce rulează de fapt.
# `cald_motiv` e nevid doar dacă ultima recitire a picat.
#
# `se_incarca` e a treia stare: fără ea, `gata: False` ar însemna și „a picat",
# și „mai așteaptă câteva secunde", care se repară altfel.
DICTARE = {"conducta": None, "gata": False, "motiv": "neincarcat",
           "se_incarca": False, "reglaje": {}, "cald_motiv": "",
           "lacat": threading.Lock()}

# Zăvorul conductei: O SINGURĂ inferență deodată pe iGPU.
#
# `WhisperPipeline` e o legătură C++ cu stare proprie, iar transcrierile rulează
# în THREADPOOL — deci două cereri suprapuse (o frază din ENGAGE și o probă de
# trezire) ar chema `generate()` pe același obiect în paralel. Serializarea nu
# costă practic nimic și înlocuiește un defect care ar arăta ca „serverul moare
# uneori".

# Rezerva: `faster-whisper`, încărcat LENEȘ, o singură dată, la prima cădere a
# iGPU-ului. Stările sunt patru fiindcă se repară diferit: `neincarcat` (n-a
# fost nevoie de el niciodată — cazul obișnuit) · `se_incarca` (cineva așteaptă
# chiar acum cele 10–17 s) · `gata` · `a_picat`. Starea intră în /api/stare:
# altfel singurul semn că rezerva s-a aprins ar fi lipsa oricărui semn.
#
# Zăvorul e al încărcării, nu al transcrierii: două fraze care pică deodată n-au
# voie să pornească două `WhisperModel`; al doilea ar cere încă ~1,5 GB de commit.
REZERVA = {"model": None, "stare": "neincarcat", "motiv": "",
           "lacat": threading.Lock()}

# Lista cheilor citite din dictare.json. O cheie care nu mai configurează nimic
# nu stă aici: e mai rea decât una lipsă, fiindcă o citești și te încrezi.
CHEI_DICTARE = ("model_igpu", "dispozitiv_igpu", "granita_igpu", "hotwords",
                "model", "device", "compute_type", "cpu_threads", "limba",
                "prag_tacere_secunde", "secunde_razgandire", "prag_volum",
                "secunde_minim_vorbire", "secunde_incredere_vorbire",
                "beam_size")

# ── Cele două regimuri ale reglajelor ────────────────────────────────
# CALD = se aplică la fiecare transcriere, deci se poate citi la fiecare
# transcriere. Nimic din ce e aici nu ține de un obiect deja construit.
#
# `limba`, `prompt_initial` și `hotwords` sunt calde pe OpenVINO fiindcă
# `get_generation_config()` întoarce o COPIE proaspătă, iar o configurare
# completă costă 0,007 ms. Se construiește una nouă la fiecare frază, în loc să
# se mute un obiect comun sub două transcrieri paralele.
#
# `beam_size` e cald doar pentru REZERVĂ: beam search n-are implementare în
# OpenVINO 2026.3, deci pe iGPU e mereu greedy.
#
# `granita_igpu` e cald ca să se poată STINGE ruta fără repornire. Aprinderea
# înapoi de la 0 cere repornire: la 0 nu s-a construit nicio conductă la
# pornire. Starea aia își spune numele în /api/stare.
CHEI_LA_CALD = ("limba", "beam_size", "prompt_initial", "hotwords",
                "granita_igpu")

# RECE = se aplică la CONSTRUCȚIE și nu se pot schimba pe un obiect viu.
#
# `model_igpu` și `dispozitiv_igpu` construiesc conducta OpenVINO; compilarea ei
# se face o dată.
#
# `cpu_threads` e rece fiindcă în ctranslate2 numărul de fire e `intra_threads`,
# argument DOAR de constructor. Singura cale de a-l schimba ar fi un
# `WhisperModel` nou, adică încă ~1,5 GB de commit.
#
# `model`, `device`, `compute_type` și `cpu_threads` sunt ale REZERVEI. Rămân
# reci chiar dacă rezerva se încarcă târziu: se citesc în clipa încărcării, iar
# după aceea obiectul e construit. Schimbate pe disc înainte de prima cădere,
# se aplică singure; după, își spun numele.
CHEI_LA_RECE = ("model_igpu", "dispozitiv_igpu",
                "model", "device", "compute_type", "cpu_threads")

# Cuvântul de trezire. TOATE OPȚIONALE: lipsa lor nu e o eroare, e o treaptă mai
# jos. Fără ele urechea nu mai stă deschisă, iar dictarea merge întreagă pe
# Alt+A. Un cuvânt de trezire n-are voie să omoare dictarea.
#
# „Opțional" NU înseamnă implicite ascunse în cod: dacă lipsesc, funcția e
# stinsă, nu pornită cu alte cifre. Configurarea rămâne într-un singur loc.
#
# `trezire_prag_volum` și `trezire_minim_vorbire` sunt mai MICI decât perechile
# lor din dictare, dinadins: în AȘTEPT greșeala ieftină e o probă goală
# (0,02–0,06 s), cea scumpă un cuvânt neauzit. Podeaua camerei e p90 = 0,0023,
# deci 0,004 e tot deasupra zgomotului.
CHEI_TREZIRE = ("cuvant_trezire", "cuvant_adormire",
                "trezire_maxim_secunde", "trezire_prag_tacere",
                "trezire_prag_volum", "trezire_minim_vorbire")


def lista_cuvinte(reglaje: dict, cheie: str) -> list:
    """Un cuvânt de comandă ca listă, oricare ar fi forma lui pe disc.

    Un șir singur rămâne valid; rândurile goale și ce nu e text cad, ca un
    fișier scris strâmb să nu ajungă cuvânt de comandă.
    """
    v = reglaje.get(cheie) or []
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list):
        return []
    return [c.strip() for c in v if isinstance(c, str) and c.strip()]


def cuvinte_adormire(reglaje: dict) -> list:
    """Cuvintele care scot din ENGAGE: „disengage" și a doua lui ușă, „stop"."""
    return lista_cuvinte(reglaje, "cuvant_adormire")


def cuvinte_trezire(reglaje: dict) -> list:
    """Cuvintele care bagă în ENGAGE: „engage" și a doua lui ușă, „start".

    A doua ușă e o cheie la aceeași cameră, nu un al treilea cuvânt de ținut
    minte — exact ce e „stop" pentru „disengage". Regula după care se caută
    fiecare stă în punte, la `cautaCuvantul`, unde s-a și măsurat: aici pleacă
    doar lista.
    """
    return lista_cuvinte(reglaje, "cuvant_trezire")


def citeste_dictare() -> dict:
    """Reglajele din dictare.json. Ridică excepție cu motivul, nu întoarce implicite.

    Fără valori implicite în cod: dacă ar exista, configurarea ar trăi în două
    locuri și fișierul ar putea fi șters fără ca nimeni să observe.
    """
    date = json.loads(DICTARE_FISIER.read_text(encoding="utf-8-sig"))
    lipsa = [c for c in CHEI_DICTARE if c not in date]
    if lipsa:
        raise ValueError(f"chei lipsa in dictare.json: {', '.join(lipsa)}")
    return date


def reglaje_la_cald() -> dict:
    """Reglajele pentru o transcriere: cheile calde de pe disc, restul din instantaneu.

    Dacă fișierul nu se poate citi — prins pe jumătate scris, JSON stricat, o
    cheie ștearsă — se lucrează pe instantaneu și transcrierea merge mai departe.
    Un reglaj greșit n-are voie să omoare dictarea, cum nici lipsa dictării nu
    omoară serverul.

    Eșecul nu se înghite: motivul stă în DICTARE["cald_motiv"] și se vede în
    /api/stare. Se rescrie la fiecare citire, deci se stinge singur când fișierul
    e reparat — o stare care rămâne aprinsă după ce cauza a trecut minte la fel
    de rău ca una care tace.

    Cheile calde LIPSĂ din fișierul proaspăt cad tot pe instantaneu, nu pe o
    excepție: o cheie ștearsă în timpul mersului ar pica exact în mijlocul unei
    fraze, iar instantaneul e o valoare care sigur a funcționat.
    """
    instantaneu = DICTARE["reglaje"]
    try:
        proaspete = citeste_dictare()
    except Exception as ex:
        DICTARE["cald_motiv"] = f"{type(ex).__name__}: {ex}"
        return instantaneu
    DICTARE["cald_motiv"] = ""
    return {**instantaneu,
            **{c: proaspete[c] for c in CHEI_LA_CALD if c in proaspete}}


def reglaje_reci_schimbate() -> dict:
    """Cheile reci care diferă între ce s-a încărcat și ce e acum pe disc.

    Gol în cazul obișnuit. Nevid înseamnă un singur lucru: cineva a schimbat o
    cifră care nu se poate aplica pe un obiect deja construit, iar serverul
    lucrează mai departe cu cea veche. Fără rândul ăsta, singurul semn ar fi
    lipsa oricărui semn.
    """
    instantaneu = DICTARE["reglaje"]
    if not instantaneu:
        return {}
    try:
        pe_disc = citeste_dictare()
    except Exception:
        # Motivul se vede deja în `cald_motiv`; aici n-avem ce compara.
        return {}
    return {c: {"incarcat": instantaneu[c], "pe_disc": pe_disc[c]}
            for c in CHEI_LA_RECE
            if c in instantaneu and c in pe_disc and instantaneu[c] != pe_disc[c]}


def tacere_wav(secunde: float = 1.0, rata: int = 16000) -> io.BytesIO:
    """O secundă de tăcere, în memorie. Doar pentru încălzire."""
    tampon = io.BytesIO()
    with wave.open(tampon, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rata)
        w.writeframes(b"\x00\x00" * int(rata * secunde))
    tampon.seek(0)
    return tampon


def decodeaza(octeti: bytes) -> tuple["np.ndarray", float]:
    """Audio în octeți → vector 16 kHz mono, plus durata.

    Se decodează O SINGURĂ DATĂ, aici, și tot de aici vine DURATA. Fără pasul
    ăsta durata s-ar ghici din antetul containerului — care la MediaRecorder
    poate lipsi cu totul, iar puntea rutează după ea.

    Vectorul e int16, fiindcă amândouă motoarele vor float32 împărțit la 32768:
    conversia se face o dată, la apel, nu de două ori aici.

    PyAV, nu ffmpeg: e deja dependența lui `faster-whisper`, deci nu intră un
    decodor nou în lanț.
    """
    import av
    import numpy as np

    bucati = []
    with av.open(io.BytesIO(octeti)) as intrare:
        flux = intrare.streams.audio[0]
        reesantionator = av.AudioResampler(format="s16", layout="mono",
                                           rate=16000)
        for cadru in intrare.decode(flux):
            for c in reesantionator.resample(cadru):
                bucati.append(c.to_ndarray().reshape(-1))
        for c in reesantionator.resample(None):   # golește tamponul
            bucati.append(c.to_ndarray().reshape(-1))

    esantioane = (np.concatenate(bucati) if bucati
                  else np.zeros(0, dtype="int16"))
    return esantioane, len(esantioane) / 16000.0


def configurare_generare(conducta, reglaje: dict):
    """Configurarea de generare pentru o frază, construită proaspăt.

    **Se pune pe OBIECT, nu ca argumente la `generate()`.** Acolo
    `initial_prompt` e înghițit în tăcere, fără eroare, iar un argument ignorat
    fabrică un defect în altă parte (pare că OpenVINO strică româna).

    Proaspătă la fiecare frază, fiindcă `get_generation_config()` întoarce o
    copie și costă 0,007 ms: așa cheile rămân calde, fără ca două transcrieri
    paralele să se calce pe același obiect.

    `hotwords` NU e opțional: `initial_prompt` condiționează doar prima fereastră
    de 30 s, iar `WhisperPipeline` nu duce contextul mai departe cum face
    `faster-whisper`. Fără el, de la a doua fereastră dispare vocabularul casei
    și motorul intră în buclă.
    """
    config = conducta.get_generation_config()
    config.language = f"<|{reglaje['limba']}|>"
    config.task = "transcribe"
    # Greedy, fiindcă beam search n-are implementare în OpenVINO 2026.3.
    config.num_beams = 1
    config.initial_prompt = reglaje.get("prompt_initial") or ""
    config.hotwords = reglaje.get("hotwords") or ""
    return config


DICTARE_PORNITA = threading.Lock()
DICTARE_CERUTA = False


def cere_incarcarea_dictarii(motiv: str) -> None:
    """Pornește firul de încărcare, o singură dată, oricine ar cere.

    NU LA PORNIREA SERVERULUI: compilarea conductei ține GIL-ul, deci cât durează
    ea SERVERUL NU RĂSPUNDE LA NIMIC (~4 s de îngheț continuu, imediat după
    deschiderea portului). Fix acolo ar cădea cererea punții, iar kiosk-ul ar sta
    alb. Plătește cine așteaptă: puntea întâi, urechea după.

    Două uși, fiindcă serverul nu e doar puntea: dacă nu cere nimeni pagina,
    plasa de la pornire pornește încărcarea oricum, altfel `Alt+A` n-ar avea
    conductă.

    Steagul `se_incarca` rămâne ridicat de la pornire, nu de aici: între boot și
    firul ăsta, urechea e pe drum, nu picată, iar harta n-are voie să arate rece
    ceva ce vine.
    """
    global DICTARE_CERUTA
    with DICTARE_PORNITA:
        if DICTARE_CERUTA:
            return
        DICTARE_CERUTA = True
    print(f"  dictare: incepe incarcarea ({motiv})")
    threading.Thread(target=incarca_dictarea_in_fundal, name="dictare",
                     daemon=True).start()


def incarca_dictarea_in_fundal() -> None:
    """`porneste_dictarea()` pe firul lui, cu steagul coborât ORICUM la capăt.

    `finally`, nu o linie la sfârșit: o compilare care crapă altfel ar lăsa
    serverul spunând „se încarcă" pentru totdeauna, iar puntea ar reîncerca la
    nesfârșit după ceva ce nu mai vine. Un steag care nu se poate coborî e mai
    rău decât lipsa lui.
    """
    try:
        porneste_dictarea()
    finally:
        DICTARE["se_incarca"] = False


def porneste_dictarea() -> None:
    """Compilează conducta iGPU și o încălzește. Orice eșec lasă serverul viu.

    Încălzirea există fiindcă prima rulare a unui model e mult mai lentă decât
    următoarele; fără ea, prima dictare reală ar fi cea lentă, iar omul ar trage
    concluzia greșită.

    **Rezerva NU se încarcă aici**: în repaus n-are de ce să coste ~1,5 GB și
    10–17 s la fiecare pornire. Vezi `incarca_rezerva()`.
    """
    try:
        reglaje = citeste_dictare()
    except FileNotFoundError:
        DICTARE["motiv"] = f"lipseste {DICTARE_FISIER}"
        print(f"  dictare: OPRITA - {DICTARE['motiv']}")
        return
    except Exception as ex:
        DICTARE["motiv"] = f"dictare.json: {type(ex).__name__}: {ex}"
        print(f"  dictare: OPRITA - {DICTARE['motiv']}")
        return

    # `granita_igpu: 0` stinge ruta cu totul și lasă dictarea pe rezervă — plasa
    # de siguranță pentru mașina pe care iGPU-ul nu se compilează. Instantaneul
    # se scrie ORICUM: fără el, `reglaje_la_cald()` n-ar avea pe ce cădea, iar
    # rezerva ar porni fără nicio configurare.
    DICTARE["reglaje"] = reglaje
    if float(reglaje["granita_igpu"]) <= 0:
        DICTARE.update(gata=True, motiv="")
        print("  dictare: iGPU stins din configurare (granita_igpu = 0), "
              "se lucreaza pe rezerva")
        return

    try:
        import openvino_genai as ov_genai
        from huggingface_hub import snapshot_download
    except Exception as ex:
        DICTARE["motiv"] = f"openvino-genai nu e instalat: {ex}"
        print(f"  dictare: OPRITA - {DICTARE['motiv']}")
        return

    t0 = time.perf_counter()
    try:
        # local_files_only=True: dacă modelul nu e în cache, se REFUZĂ pe loc,
        # nu se descarcă. Fără el, primul boot pe o mașină nouă ar atârna tăcut
        # descărcând 0,8 GB — și ar arăta ca un server blocat, nu ca o
        # descărcare. Aducerea modelului rămâne un act deliberat, cerut o dată,
        # nu un efect secundar al pornirii.
        cale = snapshot_download(reglaje["model_igpu"], local_files_only=True)
    except Exception as ex:
        # Modelele stau în .cache\huggingface\hub\, în AFARA depozitului —
        # deci nu vin cu depozitul git și se redescarcă pe mașină nouă.
        DICTARE["motiv"] = (f"modelul {reglaje['model_igpu']} nu e in cache "
                            f"local: {type(ex).__name__}: {ex}")
        print(f"  dictare: OPRITA - {DICTARE['motiv']}")
        return

    try:
        CACHE_OV.mkdir(parents=True, exist_ok=True)
        conducta = ov_genai.WhisperPipeline(
            cale, device=reglaje["dispozitiv_igpu"], CACHE_DIR=str(CACHE_OV))
    except Exception as ex:
        # Un iGPU care nu se compilează NU e o eroare fatală: dictarea merge pe
        # rezervă, mai încet. Dar motivul se ține minte și se arată — altfel
        # singurul semn ar fi „JA.S.Mine a devenit lentă".
        DICTARE.update(gata=True,
                       motiv=(f"conducta pe {reglaje['dispozitiv_igpu']} nu "
                              f"s-a compilat: {type(ex).__name__}: {ex}"))
        print(f"  dictare: iGPU OPRIT - {DICTARE['motiv']}")
        return
    compilare = time.perf_counter() - t0

    t1 = time.perf_counter()
    try:
        import numpy as np
        with DICTARE["lacat"]:
            conducta.generate(np.zeros(16000, dtype="float32"),
                              configurare_generare(conducta, reglaje))
        incalzire = f"{time.perf_counter() - t1:.2f} s"
    except Exception as ex:
        # O încălzire eșuată nu descalifică conducta: prima dictare va fi doar
        # mai lentă. Se spune, nu se ascunde.
        incalzire = f"esuata ({type(ex).__name__})"

    DICTARE.update(conducta=conducta, gata=True, motiv="")
    print(f"  dictare: {reglaje['model_igpu']} pe "
          f"{reglaje['dispozitiv_igpu']} compilat in {compilare:.2f} s, "
          f"incalzire {incalzire}")


def incarca_rezerva() -> bool:
    """`faster-whisper`, încărcat la PRIMA cădere a iGPU-ului. Blocant.

    Se cheamă din threadpool, niciodată din bucla async: încărcarea ține 10–17 s,
    iar pe buclă ar îngheța serverul întreg — RECON, jurnalul, chatul.

    Sub zăvor, și cu a doua verificare ÎNĂUNTRUL lui: două fraze care pică
    deodată intră amândouă pe ușă, iar fără recitire a doua ar construi un al
    doilea `WhisperModel` peste primul. Limita nu e RAM-ul, e commit-ul: o
    instanță în plus poate pica cu `mkl_malloc` având RAM liber.

    Întoarce dacă rezerva e utilizabilă. Motivul rămâne în `REZERVA`, deci se
    vede în /api/stare: o rezervă care a picat trebuie să se poată deosebi de
    una de care n-a avut nimeni nevoie.
    """
    if REZERVA["stare"] == "gata":
        return True
    with REZERVA["lacat"]:
        if REZERVA["stare"] == "gata":
            return True
        REZERVA.update(stare="se_incarca", motiv="")
        try:
            from faster_whisper import WhisperModel
        except Exception as ex:
            REZERVA.update(stare="a_picat",
                           motiv=f"faster-whisper nu e instalat: {ex}")
            print(f"  rezerva: OPRITA - {REZERVA['motiv']}")
            return False

        # Cheile reci ale rezervei se citesc ACUM, de pe disc: obiectul ei se
        # construiește abia aici, deci o cifră schimbată între pornire și prima
        # cădere se aplică singură. După, își spune numele ca oricare cheie rece.
        try:
            reglaje = citeste_dictare()
        except Exception:
            reglaje = DICTARE["reglaje"]

        t0 = time.perf_counter()
        try:
            model = WhisperModel(
                reglaje["model"], device=reglaje["device"],
                compute_type=reglaje["compute_type"],
                # cpu_threads explicit: implicitul faster-whisper e 4, iar
                # mașina are 16 nuclee. 8, nu 16: Open WebUI și Docker rulează
                # în paralel, iar un Whisper care ia tot procesorul ar încetini
                # chiar răspunsul pe care îl grăbește.
                cpu_threads=int(reglaje["cpu_threads"]),
                local_files_only=True)
        except Exception as ex:
            # Aliasul "large-v3-turbo" se rezolvă la depozitul
            # mobiuslabsgmbh/faster-whisper-large-v3-turbo, NU Systran: `small`
            # și `medium` sunt Systran, turbo nu. O comandă de verificare scrisă
            # pe Systran ratează exact modelul ales.
            lipsa = isinstance(ex, (FileNotFoundError, OSError))
            REZERVA.update(
                stare="a_picat",
                motiv=(f"modelul {reglaje['model']} nu e in cache local: {ex}"
                       if lipsa else
                       f"modelul nu s-a incarcat ({reglaje['compute_type']} pe "
                       f"{reglaje['device']}): {type(ex).__name__}: {ex}"))
            print(f"  rezerva: OPRITA - {REZERVA['motiv']}")
            return False

        try:
            # transcribe() întoarce un GENERATOR leneș. Fără consumarea lui nu
            # se transcrie nimic, iar încălzirea ar fi o iluzie care costă zero.
            segmente, _ = model.transcribe(tacere_wav(),
                                           language=reglaje["limba"])
            list(segmente)
        except Exception:
            pass                        # tot ce pierde e viteza primei fraze
        REZERVA.update(model=model, stare="gata", motiv="")
        print(f"  rezerva: faster-whisper incarcat in "
              f"{time.perf_counter() - t0:.2f} s")
        return True


@asynccontextmanager
async def ciclu_viata(_app: FastAPI):
    """Ce se face la pornire și la oprire.

    Nimic de închis pe partea dictării: conducta OpenVINO și rezerva trăiesc
    amândouă ÎN procesul serverului și mor cu el.

    DICTAREA SE ÎNCARCĂ ÎN FUNDAL. Blocant, ea ar ține PORTUL închis ~13 s, iar
    `reporneste-jasmine.vbs` așteaptă serverul înainte să lanseze Chrome: ecranul
    ar sta gol, iar fereastra ar apărea gata caldă, fără ca culorile reci să fi
    apucat să spună ce se poate cere. Până e gata, /api/transcrie răspunde
    „se încarcă", nu „nu e disponibilă" — două lucruri care se repară altfel.
    """
    # Ridicat SINCRON, înainte de fir: o cerere ajunsă în prima milisecundă ar
    # vedea altfel `gata: False` fără explicație, adică „a picat".
    DICTARE.update(se_incarca=True, motiv="se incarca")
    # PLASA, nu drumul obișnuit: pe drumul obișnuit încărcarea o cere puntea,
    # după ce a plecat spre ecran. E pentru cazul în care nu vine nicio punte —
    # server pornit cu mâna, kiosk care n-a mai pornit — fiindcă `Alt+A` are
    # nevoie de conductă chiar și atunci.
    #
    # 30 s, nu mai devreme: o fereastră care întârzie ar cere pagina exact cât
    # ține înghețul compilării, iar plasa ar produce fix defectul pe care
    # încărcarea amânată îl repară.
    plasa = threading.Timer(30.0, cere_incarcarea_dictarii, args=("plasa de la pornire",))
    plasa.daemon = True
    plasa.start()
    sarcina = asyncio.create_task(sincronizeaza_la_pornire())
    try:
        yield
    finally:
        plasa.cancel()
        sarcina.cancel()


app = FastAPI(title="JA.S.Mine", lifespan=ciclu_viata)


# Serverul ascultă doar pe 127.0.0.1, dar orice pagină deschisă în browser poate
# trimite un POST „simplu” spre localhost (rularea plătită RECON, ieșirea), iar
# un nume DNS rebătut poate citi răspunsurile. `Host` străin înseamnă DNS
# rebinding; `Origin` străin pe o cerere care schimbă ceva înseamnă altă pagină.
# Clienții care nu sunt browser (scurtătura, dictarea) nu trimit `Origin`.
GAZDE_PERMISE = {"localhost", "127.0.0.1", "localhost:8000", "127.0.0.1:8000"}
ORIGINI_PERMISE = {"http://localhost:8000", "http://127.0.0.1:8000"}


@app.middleware("http")
async def doar_de_acasa(request, call_next):
    gazda = request.headers.get("host", "")
    if gazda not in GAZDE_PERMISE:
        return JSONResponse({"ok": False, "eroare": f"gazda refuzata: {gazda}",
                             "server": "serverul JA.S.Mine"}, status_code=403)
    origine = request.headers.get("origin")
    if (request.method not in ("GET", "HEAD", "OPTIONS") and origine is not None
            and origine not in ORIGINI_PERMISE):
        return JSONResponse({"ok": False, "eroare": f"origine refuzata: {origine}",
                             "server": "serverul JA.S.Mine"}, status_code=403)
    return await call_next(request)


# ── Puntea ───────────────────────────────────────────────────────────

# Când a fost servită ultima oară puntea. Nu e statistică: e singurul semn
# determinist că s-a deschis o fereastră NOUĂ, și îl citește paznicul ieșirii de
# mai jos. Se poate doar fiindcă puntea e `no-store` (vezi `punte()`): pagina se
# cere o dată per fereastră, nu din cache, nu la fiecare click.
ULTIMA_PUNTE = 0.0


@app.get("/")
def punte():
    """Pagina principală: puntea. NU SE PUNE ÎN CACHE.

    FileResponse trimite `ETag` și `Last-Modified`, dar NICIUN `Cache-Control`,
    iar fără el Chrome cade pe cache euristic: reține pagina cât vrea el, fără
    să întrebe. O modificare pusă pe disc, cu serverul repornit, se poate să nu
    ajungă pe ecran — s-a întâmplat: kiosk-ul servea puntea de acum două
    versiuni, iar nimic de pe server nu arăta asta.

    Nu se economisește nimic prin cache: fișierul e local, se citește o dată
    per deschidere de fereastră.
    """
    global ULTIMA_PUNTE
    ULTIMA_PUNTE = time.monotonic()
    # Urechea se încarcă DUPĂ ce pagina a plecat spre ecran și și-a luat
    # fonturile și primul rând de răspunsuri (~200 ms, toate locale). Motivul
    # întreg e la `cere_incarcarea_dictarii()`.
    ceas = threading.Timer(1.5, cere_incarcarea_dictarii, args=("a plecat puntea",))
    ceas.daemon = True
    ceas.start()
    return FileResponse(PUNTE, media_type="text/html",
                        headers={"Cache-Control": "no-store"})


@app.get("/fonturi/{nume}")
def font(nume: str):
    """Fonturile punții, servite din casă.

    Un `<link>` spre fonts.googleapis.com blochează primul desen, iar rezolvarea
    numelui costă 2,0 s pe mașina asta la fiecare deschidere.

    Numele se verifică pe tipar, nu se lipește în cale: un `nume` venit de afară
    și pus direct într-un `/` deschide tot discul. Tiparul e strâmt dinadins —
    litere mici, cifre, liniuțe, `.woff2` — fiindcă exact atâta scrie puntea.

    Fără `no-store` aici: un font nu se schimbă niciodată fără nume nou, iar
    cache-ul kiosk-ului se golește oricum la fiecare pornire.
    """
    if not re.fullmatch(r"[a-z0-9-]+\.woff2", nume):
        return JSONResponse({"eroare": "nume de font nepotrivit"}, status_code=404)
    cale = FONTURI / nume
    if not cale.is_file():
        return JSONResponse({"eroare": f"fontul {nume} nu e pe disc"}, status_code=404)
    return FileResponse(cale, media_type="font/woff2")


# ── MUNTELE: clipurile camerei lui Ramana ────────────────────────────
# Clipurile stau în AFARA depozitului, ca modelele Whisper, redescărcabile cu
# `py server\descarca-munte.py`. Calea și ordinea vin din `server\munte.json`,
# citit la fiecare cerere — deci ordinea se schimbă fără repornirea serverului.
#
# De pe disc și nu de la YouTube: puntea nu cere NIMIC din afară ca să se
# deseneze. Un `<iframe>` ar cere internet, ar plăti rezolvarea de nume la
# fiecare deschidere și ar aduce reclame într-o încăpere făcută pentru liniște.

MUNTE_FISIER = RADACINA / "server" / "munte.json"


def citeste_munte() -> dict:
    """Configurarea camerei. Un fișier stricat nu strică nimic: camera rămâne, fără clipuri.

    Aceeași alegere ca la reglajele dictării, și din același motiv — chatul lui
    Ramana n-are nicio treabă cu clipurile, deci n-are de ce să cadă cu ele.
    """
    try:
        date = json.loads(MUNTE_FISIER.read_text(encoding="utf-8-sig"))
    except Exception as ex:
        return {"ok": False, "motiv": f"{type(ex).__name__}: {ex}",
                "volum": 0.5, "clipuri": []}
    date["ok"] = True
    return date


@app.get("/api/munte/lista")
def munte_lista():
    """Ce se poate reda, în ordine, plus volumul. Numai ce e PE DISC.

    Un clip scris în munte.json dar lipsă de pe disc nu se trimite: puntea ar
    cere o adresă care întoarce 404, iar `<video>` ar sări peste el cu o eroare
    în consolă în loc de un motiv pe ecran. Ce lipsește se numără (`lipsesc`),
    ca panoul să poată spune că se rulează scriptul de descărcare.
    """
    date = citeste_munte()
    folder = RADACINA / str(date.get("folder") or "")
    gata, lipsesc = [], []
    for c in date.get("clipuri") or []:
        nume = str(c.get("fisier") or "")
        if nume and (folder / nume).is_file():
            gata.append({"titlu": c.get("titlu") or nume, "fisier": nume})
        else:
            lipsesc.append(nume)
    return {"ok": bool(date.get("ok")), "motiv": date.get("motiv", ""),
            "volum": date.get("volum", 0.5), "clipuri": gata, "lipsesc": len(lipsesc)}


class VolumMunte(BaseModel):
    volum: float


@app.post("/api/munte/volum")
def munte_volum(cerere: VolumMunte):
    """Scrie volumul înapoi în munte.json. Sursa unică rămâne fișierul.

    Pe disc și nu în `localStorage`: un volum ținut în browser ar dispărea
    la golirea cache-ului kiosk-ului, care se face la FIECARE pornire.

    Se rescrie tot obiectul, deci rândurile de comentariu (`_`, `_volum`, …) se
    păstrează, în ordine. Ce nu se poate citi nu se rescrie — un fișier stricat
    nu se repară aici.
    """
    v = max(0.0, min(1.0, float(cerere.volum)))
    date = citeste_munte()
    if not date.get("ok"):
        return JSONResponse({"ok": False, "eroare": "munte.json nu se poate citi",
                             "detaliu": date.get("motiv", "")}, status_code=409)
    date.pop("ok", None)
    date["volum"] = round(v, 2)
    try:
        MUNTE_FISIER.write_text(
            json.dumps(date, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8", newline="\n")
    except Exception as ex:
        return JSONResponse({"ok": False, "eroare": "nu s-a putut scrie munte.json",
                             "server": SERVER_LOCAL,
                             "detaliu": f"{type(ex).__name__}: {ex}"}, status_code=500)
    return {"ok": True, "volum": date["volum"]}


@app.get("/munte/{nume}")
def munte_clip(nume: str):
    """Un clip, de pe disc.

    Numele se verifică pe TIPAR, nu se lipește în cale — exact ca la fonturi. Și
    nu e de ajuns tiparul: fișierul trebuie să fie ȘI în munte.json. Altfel ruta
    ar servi orice .mp4 nimerit în folder, iar folderul ăla nu e al depozitului.

    Fără `no-store`: un clip de 25 MB pus în cache e exact ce vrei, iar cache-ul
    kiosk-ului se golește oricum la fiecare pornire.

    `FileResponse` răspunde la `Range`, deci `<video>` poate căuta în clip fără
    să-l descarce întreg.
    """
    if not re.fullmatch(r"[a-z0-9-]+\.mp4", nume):
        return JSONResponse({"eroare": "nume de clip nepotrivit"}, status_code=404)
    date = citeste_munte()
    scrise = {str(c.get("fisier") or "") for c in (date.get("clipuri") or [])}
    if nume not in scrise:
        return JSONResponse({"eroare": f"{nume} nu e in munte.json"}, status_code=404)
    cale = RADACINA / str(date.get("folder") or "") / nume
    if not cale.is_file():
        return JSONResponse(
            {"eroare": f"{nume} nu e pe disc",
             "reparare": "py server" + chr(92) + "descarca-munte.py"}, status_code=404)
    return FileResponse(cale, media_type="video/mp4")


# ── Persona: ușile ───────────────────────────────────────────────────
# Citirea și scrierea sunt endpoint-uri separate, nu un parametru pe același.
# Un GET care schimbă starea motorului ar fi o capcană pentru oricine deschide
# adresa din curiozitate.
#
# Amândouă, plus paznicul de la pornire, trec prin sincronizeaza_si_noteaza(),
# ca starea să nu rămână în urmă pe niciuna.

@app.get("/api/persona/stare")
async def persona_stare():
    """Ce e sincronizat și ce nu. Compară și atât — nu scrie nimic în motor."""
    try:
        raport = await sincronizeaza_persona(scrie=False)
    except Exception as ex:
        return JSONResponse({"ok": False, "eroare": f"{type(ex).__name__}: {ex}"},
                            status_code=502)
    # `incercari` e al paznicului de la pornire: 0 înseamnă „din prima", iar o
    # cifră care crește lângă un motiv înseamnă „mai încearcă", nu „a renunțat".
    # Fără ea, un paznic care lucrează de cinci minute arată identic cu unul mort.
    return {"ok": True,
            "ultima_sincronizare": PERSONA["cand"],
            "motiv": PERSONA["motiv"],
            "incercari": PERSONA["incercari"],
            "straturi": raport}


@app.post("/api/persona/sincronizeaza")
async def persona_sincronizeaza():
    """Duce prompturile în Open WebUI acum, fără repornirea serverului.

    Pentru cazul real: se editează un .md și se vrea efectul imediat.
    """
    try:
        raport, rele = await sincronizeaza_si_noteaza()
    except Exception as ex:
        return JSONResponse({"ok": False, "eroare": f"{type(ex).__name__}: {ex}"},
                            status_code=502)

    return {"ok": not rele, "straturi": raport, "motiv": PERSONA["motiv"]}


# ── Jurnalul (Ship's Log) ────────────────────────────────────────────
# Formatul pe disc: JSONL — un obiect JSON pe rând, adăugat la coadă.
# Exemplu de rând:  {"cand": "2026-07-14T18:42:07", "text": "Mail trimis."}
# Fișierul se poate deschide oricând cu Notepad (sursă unică de adevăr).

class Intrare(BaseModel):
    text: str


def citeste_jurnal() -> list[dict]:
    """Citește toate intrările din fișier. Rândurile corupte se sar, nu opresc tot."""
    intrari = []
    for rand in JURNAL.read_text(encoding="utf-8").splitlines():
        rand = rand.strip()
        if not rand:
            continue
        try:
            intrari.append(json.loads(rand))
        except json.JSONDecodeError:
            continue
    return intrari


@app.get("/api/jurnal")
def jurnal_lista():
    """Toate intrările, cele mai noi primele (cum le afișează puntea)."""
    return list(reversed(citeste_jurnal()))


def scrie_in_jurnal(text: str) -> dict:
    """Adaugă o intrare și întoarce exact ce s-a scris. Codul pune data, nu modelul.

    Stă separat de endpoint fiindcă are doi chemători: puntea, prin HTTP, și
    unealta lui Sky, care primește un dicționar, nu un răspuns HTTP. Un
    JSONResponse întors către unealtă n-ar trece de json.dumps.
    """
    text = (text or "").strip()
    if not text:
        return {"ok": False, "eroare": "text gol"}
    rand = {"cand": datetime.now().isoformat(timespec="seconds"), "text": text}
    # Zăvorul e împărțit cu ștergerea, care rescrie fișierul întreg — vezi
    # JURNAL_LACAT. Aici costă nimic: o linie scrisă la coadă.
    with JURNAL_LACAT, JURNAL.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rand, ensure_ascii=False) + "\n")
    return {"ok": True, "intrare": rand}


@app.post("/api/jurnal")
def jurnal_adauga(intrare: Intrare):
    """Adaugă o intrare. Codul pune data — nu browserul, nu modelul."""
    rezultat = scrie_in_jurnal(intrare.text)
    if not rezultat["ok"]:
        return JSONResponse({"eroare": rezultat["eroare"]}, status_code=400)
    # Puntea așteaptă intrarea goală, fără înveliș — contractul ei nu se schimbă.
    return rezultat["intrare"]


class IntrareStearsa(BaseModel):
    """Care intrare se scoate. Nu există id în jurnal, deci se numesc amândouă
    câmpurile: `cand` singur ar putea prinde două intrări scrise în aceeași
    secundă, iar `text` singur, două idei formulate la fel."""
    cand: str
    text: str


@app.delete("/api/jurnal")
def jurnal_sterge(intrare: IntrareStearsa):
    """Scoate o intrare din listă. NU o aruncă: o mută în `jurnal-inchis.jsonl`.

    Ștergerea definitivă ar contrazice manualul punții: „un jurnal care pierde
    în tăcere e mai rău decât unul care lipsește." Așa, lista rămâne curată și
    rândul se poate scoate înapoi cu mâna.

    ORDINEA E ANUME: întâi se scrie în arhivă, abia apoi se rescrie jurnalul.
    Invers, un disc plin ar lăsa intrarea ștearsă din amândouă locurile. Dacă
    arhiva nu se poate scrie, ștergerea SE REFUZĂ și spune de ce.

    Se lucrează pe RÂNDURILE BRUTE, nu pe `citeste_jurnal()`, care sare rândurile
    corupte: o rescriere pe baza lui le-ar șterge tăcut pe toate.
    """
    with JURNAL_LACAT:
        try:
            randuri = JURNAL.read_text(encoding="utf-8").splitlines(keepends=True)
        except Exception as ex:
            return JSONResponse(
                {"ok": False, "eroare": "jurnalul nu s-a putut citi",
                 "detaliu": f"{type(ex).__name__}: {ex}"}, status_code=500)

        gasit = None
        for i, rand in enumerate(randuri):
            try:
                obiect = json.loads(rand)
            except json.JSONDecodeError:
                continue
            if (obiect.get("cand") == intrare.cand
                    and obiect.get("text") == intrare.text):
                gasit = i
                break

        if gasit is None:
            # 404, nu 500: intrarea chiar nu mai e acolo. Se întâmplă dacă două
            # ferestre ale punții șterg același rând.
            return JSONResponse(
                {"ok": False, "eroare": "intrarea nu mai e în jurnal"},
                status_code=404)

        scos = randuri[gasit]
        try:
            with JURNAL_INCHIS.open("a", encoding="utf-8") as f:
                f.write(scos if scos.endswith("\n") else scos + "\n")
        except Exception as ex:
            return JSONResponse(
                {"ok": False, "eroare": "nu s-a putut scrie în arhivă, "
                                        "nu am șters nimic",
                 "detaliu": f"{type(ex).__name__}: {ex}"}, status_code=500)

        # Rescriere atomică: temporar lângă fișier (același volum, deci `replace`
        # e o singură operație), apoi mutat peste. Scris direct, o cădere la
        # jumătate ar lăsa jurnalul ciuntit.
        temporar = JURNAL.with_suffix(".jsonl.nou")
        try:
            temporar.write_text("".join(randuri[:gasit] + randuri[gasit + 1:]),
                                encoding="utf-8")
            os.replace(temporar, JURNAL)
        except Exception as ex:
            return JSONResponse(
                {"ok": False, "eroare": "jurnalul nu s-a putut rescrie",
                 "detaliu": f"{type(ex).__name__}: {ex}"}, status_code=500)

    return {"ok": True, "arhivat": str(JURNAL_INCHIS)}


# ── Proiectele ───────────────────────────────────────────────────────
# Lista stă în date/proiecte.json, schimbată fără să se atingă cod. Se editează
# de mână: un proiect nou apare rar, iar o ușă de scriere în server plus punte
# n-ar plăti construcția.

@app.get("/api/straturi")
def straturi_lista():
    """Straturile, pentru punte. FĂRĂ câmpul `prompt`.

    Puntea are nevoie de etichetă și de textul de așteptare; calea promptului nu-i
    spune nimic și n-are ce căuta în browser. Id-ul modelului se întoarce fiindcă
    e informație despre ce rulează, nu un secret — dar puntea nu-l trimite nicăieri.
    """
    return {strat: {k: v for k, v in d.items() if k != "prompt"}
            for strat, d in STRATURI.items()}


@app.get("/api/proiecte")
def proiecte_lista():
    """Citește fișierul la FIECARE cerere, fără să-l țină minte.

    Un cache ar face din editarea unui fișier text o operație care cere
    repornirea serverului. Costul e o citire de câteva sute de octeți.

    Fișierul e scris de mână, deci se strică de mână: eroarea spune linia și
    coloana, nu doar că „ceva n-a mers".
    """
    try:
        date = json.loads(PROIECTE.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {"ok": False, "eroare": "date/proiecte.json nu exista"}
    except json.JSONDecodeError as ex:
        return {"ok": False,
                "eroare": f"JSON stricat la linia {ex.lineno}, coloana {ex.colno}"}
    except Exception as ex:
        return {"ok": False, "eroare": f"{type(ex).__name__}: {ex}"}
    if not isinstance(date, list):
        return {"ok": False, "eroare": "fisierul trebuie sa contina o lista"}
    return {"ok": True, "proiecte": date}


# ── RECON: JA.S.Mine ca client MCP către Agent 051 ───────────────────
# Agentul ascultă pe http://127.0.0.1:8051/mcp (Streamable HTTP).
# Vorbim cu el prin SDK-ul oficial, nu cu cereri HTTP scrise de mână: SDK-ul pune
# singur antetul Accept (json + event-stream) și mânuiește Mcp-Session-Id.
# HTTP de mână = 406 sau 400.
#
# Puntea nu vede niciodată agentul direct — cheamă doar /api/recon/*.
# Tokenul trăiește doar aici, în .env-ul serverului.


def descrie_exceptia(ex: BaseException) -> str:
    """Textul unei excepții, cu grupurile desfăcute.

    SDK-ul MCP lucrează pe `TaskGroup`, deci un port închis ajunge sus ca
    „ExceptionGroup: unhandled errors in a TaskGroup (1 sub-exception)" —
    zero informație. Cauza adevărată (`ConnectError: All connection attempts
    failed`) stă înăuntru, la un nivel sau două. Detaliul trebuie să spună unde
    să te uiți.
    """
    parti: list[str] = []
    de_vazut: list[BaseException] = [ex]
    while de_vazut and len(parti) < 3:
        e = de_vazut.pop(0)
        sub = getattr(e, "exceptions", None)
        if sub:
            de_vazut.extend(sub)
            continue
        parti.append(f"{type(e).__name__}: {e}")
    return "; ".join(parti) or f"{type(ex).__name__}: {ex}"


async def cheama_recon(nume_unealta: str, argumente: dict) -> dict:
    """Deschide o sesiune MCP, cheamă o unealtă, întoarce JSON-ul ei brut.

    Contractul agentului: {"ok": true, "date": {...}}
                       sau {"ok": false, "eroare": "...", "detaliu": "..."}
    Nu-l reambalăm — îl dăm mai departe cum e. Cine ramifică, ramifică pe
    "eroare" (stabil), niciodată pe "detaliu" (instabil).
    """
    url = os.environ["RECON_MCP_URL"]
    token = os.environ.get("MCP_TOKEN", "").strip()
    # Antetul se trimite doar dacă avem token. Gol → fără antet deloc.
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as sesiune:
            await sesiune.initialize()
            rezultat = await sesiune.call_tool(nume_unealta, argumente)
            # structuredContent vine null la toate uneltele — se citește din text.
            return json.loads(rezultat.content[0].text)


@app.get("/api/recon/stare")
async def recon_stare():
    """Starea pipeline-ului. Determinist, zero tokeni."""
    try:
        return await cheama_recon("stare_pipeline", {})
    except Exception as ex:
        # Agentul care nu răspunde → status 200 cu ok:false. Serverul JA.S.Mine
        # funcționează, doar agentul din spate nu. Puntea ramifică pe câmpul
        # "ok" din corp. Cauza — oprit sau ocupat — o desface `esec_agent`.
        return await esec_agent(ex)


async def esec_agent(ex: Exception) -> dict:
    """Ce se întoarce când un apel către agentul 051 pică.

    „nu raspunde" e un verdict, iar noi avem doar o excepție. Cât ține o rulare
    (sub un minut) agentul nu răspunde la nimic altceva și e perfect sănătos.
    Mesajul care spune atunci „nu raspunde" îl trimite pe om să repornească exact
    procesul care lucrează. De-asta se întreabă portul: deschis = ocupat.
    """
    url = os.environ.get("RECON_MCP_URL") or ""
    if url and await accepta_conexiuni(url):
        return {"ok": False,
                "eroare": "agentul 051 nu a raspuns la timp",
                "server": "agentul 051",
                "detaliu": "Portul accepta conexiuni, deci agentul NU e oprit. "
                           "Cat ruleaza lantul nu raspunde la altceva. "
                           + descrie_exceptia(ex)}
    return {"ok": False, "eroare": "agentul 051 nu raspunde",
            "server": "agentul 051",
            "detaliu": descrie_exceptia(ex)}


@app.get("/api/recon/oportunitati")
async def recon_oportunitati(limita: int = 12):
    """Ce a găsit agentul, din TOATĂ baza — nu doar ce e nou de la reper.

    Briefingul răspunde la „ce s-a schimbat de când n-am mai citit". Ăsta
    răspunde la „ce ai găsit". Nu se înlocuiesc: reperul mutat golește
    briefingul, iar fără ușa asta conținutul agentului ar deveni invizibil deși
    stă întreg în baza lui. Cine scoate endpointul ăsta trebuie să spună întâi pe
    ce altă ușă ies joburile.

    Citire pură: nu mișcă reperul, nu cheamă modelul, nu costă bani.
    De-asta e GET și se poate repeta.
    """
    try:
        return await cheama_recon("oportunitati_noi", {"limita": limita})
    except Exception as ex:
        return await esec_agent(ex)


@app.get("/api/recon/briefing")
async def recon_briefing():
    """Briefingul zilei. marcheaza_citit e HARDCODAT False."""
    try:
        # Marcarea ca citit e singura operație ireversibilă din contract și nu
        # devine niciodată parametru aici: acest GET doar citește, oricâte ori.
        # Reperul se mută numai la începutul unei rulări, în /api/recon/ruleaza.
        return await cheama_recon("briefing", {"marcheaza_citit": False})
    except Exception as ex:
        return await esec_agent(ex)


# ── RECON: rularea lanțului ──────────────────────────────────────────
# POST, nu GET, fiindcă are efecte reale: ingestia citește Gmail, normalizarea
# și evaluarea cheamă modelul și costă bani, iar reperul se mută ireversibil. Un
# GET trebuie să poată fi repetat de un browser, un proxy sau un prefetch fără
# să se întâmple nimic.

# Lanțul complet, în ordine. Ordinea NU e opțională: filtrul lucrează pe anunțuri
# normalizate, iar evaluarea doar pe cele trecute de filtru. Fiecare unealtă e
# idempotentă (sare peste ce e deja procesat), deci re-rularea e sigură.
# Fără paralelizare și fără retry: uneltele sunt sincrone și costă bani.
#
# `descrie` stă între extragere și normalizare, singurul loc unde poate sta: după
# normalizare, o descriere nouă nu mai schimbă ce a citit modelul, deci ar fi
# muncă plătită de două ori (agentul o numără: `renormalizate_descriere_noua`,
# `rejudecate_descriere_noua`).
#
# Singura din lanț care NU cheamă niciun model: nu costă bani, costă timp străin —
# cere pagini publice de pe eJobs și BestJobs, cu 2 secunde pauză între ele. De-aia
# `limita` e frâna și rămâne 30: plafon ~60 s. Urcată la sute, ne blochează
# serverele alea, nu ne face treaba mai repede.
PASI_PIPELINE = [
    ("ingesteaza", {}),
    ("extrage", {}),
    ("descrie", {"limita": 30}),
    ("normalizeaza", {"limita": 30}),
    ("filtreaza", {}),
    ("evalueaza", {"limita": 30}),
]


# Zăvorul rulării. Agentul are o singură bază: două lanțuri deodată se calcă pe
# picioare, iar munca primului se aruncă.
#
# Zăvorul stă AICI, nu în punte: în JavaScript ar ține doar cât pagina. O
# reîncărcare, un al doilea tab, sau chiar `curl` pornesc o rulare peste alta,
# iar starea „rulez de N secunde" e a serverului oricum.
RULARE = {"pornit": None}


@app.get("/api/recon/ruleaza/stare")
async def recon_ruleaza_stare():
    """Rulează un lanț acum, și de cât timp? Zero tokeni, nu atinge agentul.

    De-asta poate fi întrebat oricât: e singurul drum prin care puntea își
    regăsește cronometrul după o reîncărcare, când tot ce ținea în memorie
    s-a dus. Agentul, cât lucrează, oricum n-ar răspunde.
    """
    pornit = RULARE["pornit"]
    if pornit is None:
        return {"ok": True, "ruleaza": False}
    return {"ok": True, "ruleaza": True, "de_secunde": round(time.time() - pornit)}


@app.post("/api/recon/ruleaza")
async def recon_ruleaza():
    """Mută reperul, rulează lanțul, întoarce briefingul.

    **Reperul se mută ÎNTÂI, și asta e tot mecanismul de „am citit":** dacă
    ceri o rulare nouă, ai terminat cu ce ți-a arătat cea dinainte. Un buton
    separat „am citit" a picat la folosire: după ce închideai panoul, singurul
    drum înapoi la el era încă o rulare întreagă.

    Se poate face aici, deși mutarea e ireversibilă, fiindcă **pierderea
    tăcută nu mai are cum**: lista de joburi vine din toată baza și nu atârnă
    de reper. Ce se pierde e delta — iar delta se umple la loc chiar cu ce
    aduce rularea asta. **Dacă lista ajunge vreodată să depindă de reper, decizia
    se redeschide.**

    La primul pas eșuat se oprește și spune care a fost. Nu continuă lanțul:
    dacă normalizarea a picat, filtrarea de după ar lucra pe date incomplete
    și ar da un rezultat care pare bun.
    """
    # A doua cerere NU pornește nimic și nu e o eroare: e răspunsul „merge deja,
    # uite de cât". Puntea o desenează ca progres, nu ca eșec.
    if RULARE["pornit"] is not None:
        return {"ok": False, "ruleaza_deja": True,
                "de_secunde": round(time.time() - RULARE["pornit"]),
                "eroare": "o rulare e deja in curs",
                "server": "serverul JA.S.Mine"}
    RULARE["pornit"] = time.time()
    try:
        # Reperul, înainte de orice. Efectul lateral e util: briefingul de la
        # sfârșit numără atunci exact ce a adus rularea asta, nu tot ce s-a
        # strâns de la ultima marcare.
        await cheama_recon("briefing", {"marcheaza_citit": True})
        for nume, argumente in PASI_PIPELINE:
            rezultat = await cheama_recon(nume, argumente)
            if not rezultat.get("ok"):
                return {
                    "ok": False,
                    "eroare": rezultat.get("eroare", "esec"),
                    "pas_esuat": nume,
                    "detaliu": rezultat.get("detaliu"),
                }
        # Lanțul a trecut. Briefingul de aici e RECOLTA rulării — reperul a fost
        # mutat la început, deci ce numără e ce a intrat între timp. Citirea
        # rămâne gratuită și repetabilă; nu mai mută nimic.
        return await cheama_recon("briefing", {"marcheaza_citit": False})
    except Exception as ex:
        return await esec_agent(ex)
    finally:
        # Pe TOATE drumurile de ieșire, inclusiv pasul eșuat de mai sus care
        # face `return` din mijlocul lui `try`. Un zăvor care rămâne închis
        # după un eșec ar transforma un defect de-o dată într-unul permanent:
        # butonul n-ar mai porni nimic niciodată, iar mesajul ar spune senin
        # „o rulare e deja în curs" despre una moartă acum o oră.
        RULARE["pornit"] = None


# ── COMMS: raportul de mail ──────────────────────────────────────────
# Ușa deterministă, zero tokeni. GET, fiindcă nu costă bani și se poate repeta —
# spre deosebire de /api/recon/ruleaza. Mută reperul, dar mutarea nu e o pierdere:
# vezi mail.scrie_in_jurnal(). Citirea nu atinge Gmail (readonly + BODY.PEEK).

@app.get("/api/comms/raport")
async def comms_raport(zile: int | None = None):
    """Mailurile necitite din fereastră, grupate pe expeditor.

    Fără `zile` — de la raportul anterior, și reperul se mută.
    Cu `zile=N` — ultimele N zile, reperul rămâne pe loc.

    IMAP e sincron și durează ~2 s. Trece prin `run_in_threadpool`, ca la
    transcriere: fără el, bucla de evenimente a lui uvicorn stă blocată și
    puntea nu mai primește nimic cât se citește cutia poștală.
    """
    try:
        return await run_in_threadpool(mail.raport, zile)
    except Exception as ex:
        # `mail.raport` prinde tot ce ține de rețea și întoarce ok:false. Aici
        # ajunge doar ce n-a prins — un defect al nostru, nu al Gmail-ului.
        return {"ok": False, "eroare": "raportul de mail a eșuat",
                "server": SERVER_LOCAL, "detaliu": descrie_exceptia(ex)}


# ── SENSORS: știrile ─────────────────────────────────────────────────
# Ușa deterministă, zero tokeni. Întoarce TOT ce e în fereastră, cu linkuri și
# cu `spusa` pe fiecare — panoul nu consumă nimic: „dat deja" înseamnă „spus de
# Sky", iar panoul nu spune, doar arată. Așa e și supapa prin care omul se uită
# înapoi la tot, fără să ceară nimic nimănui.

@app.get("/api/sensors/raport")
async def sensors_raport(ore: int | None = None):
    """Știrile din fereastră, economicele întâi.

    Ca la COMMS, prin `run_in_threadpool`: aducerea fluxurilor e sincronă și
    durează ~1 s pe rețea rece. Fără el, bucla de evenimente a lui uvicorn ar
    sta blocată, iar puntea n-ar mai primi nimic cât se citesc site-urile.
    """
    try:
        if ore is not None:
            ore = max(1, min(int(ore), 72))
        return await run_in_threadpool(stiri.panou, ore)
    except Exception as ex:
        # `stiri.panou` prinde singur ce ține de rețea și de fișiere stricate.
        # Aici ajunge doar ce n-a prins — un defect al nostru, nu al site-urilor.
        return {"ok": False, "eroare": "raportul de știri a eșuat",
                "server": SERVER_LOCAL, "detaliu": descrie_exceptia(ex)}


# ── Ieșirea din JA.S.Mine ────────────────────────────────────────────
# Puntea rulează în Chrome `--kiosk --app=`: n-are bară, n-are buton de
# închidere. `×` o închide cu `window.close()`; dacă Chrome refuză, puntea cade
# aici. Unealta preferată, plus o rezervă care nu e formalitate.

# Profilul dedicat e și semnătura: kiosk-ul pornește cu `--user-data-dir` pe
# folderul ăsta (`reporneste-jasmine.vbs`), deci linia de comandă îl conține și
# nicio altă fereastră Chrome nu-l conține.
SEMNATURA_KIOSK = str(RADACINA / "chrome-profil")


def scrie_iesirea(rand: dict) -> None:
    """Un rând în `date\\iesire.jsonl`. Ce a pățit omul când a plecat.

    Fereastra, motorul și serverul scriu în ACELAȘI fișier: e aceeași poveste,
    iar despărțită pe trei ar trebui citită de trei ori.

    UN JURNAL CARE NU SE POATE SCRIE N-ARE VOIE SĂ BLOCHEZE UȘA: măsurătoarea e
    a noastră, ieșirea e a omului.
    """
    try:
        with IESIRE_JURNAL.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"cand": datetime.now().isoformat(timespec="seconds"),
                                **rand}, ensure_ascii=False) + "\n")
    except Exception as ex:
        print(f"  jurnalul iesirii nu s-a putut scrie: {type(ex).__name__}: {ex}")


def arata_fereastra(cod: int) -> dict:
    """Minimizează sau ridică fereastra kiosk. `cod` e un SW_* din user32.

    De ce trece prin server: nu există niciun API web pentru minimizare.
    `window.close()` există, `window.minimize()` nu — o pagină nu poate umbla la
    fereastra care o ține. Deci puntea cere, iar serverul face.

    Aceeași semnătură ca la închidere: se caută după LINIA DE COMANDĂ, și doar
    procesul PĂRINTE (cel fără `--type=`) are fereastră. Fără găsire nu se atinge
    nimic — o tastă care minimizează ferestrele altcuiva ar fi mai rea decât una
    care nu face nimic.
    """
    ps = (
        "$t = Add-Type -MemberDefinition '"
        "[DllImport(\"user32.dll\")] public static extern bool ShowWindowAsync("
        "IntPtr h, int c); "
        "[DllImport(\"user32.dll\")] public static extern bool SetForegroundWindow("
        "IntPtr h); "
        "[DllImport(\"user32.dll\")] public static extern bool IsIconic(IntPtr h);"
        "' -Name Fer -Namespace AG -PassThru; "
        "$n = 0; $c = [int]$env:COD; "
        "@(Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
        "Where-Object { $_.CommandLine -like '*' + $env:SEMNATURA + '*' -and "
        "$_.CommandLine -notlike '*--type=*' }) | ForEach-Object { "
        "$h = Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue; "
        "if ($h -and $h.MainWindowHandle -ne 0) { "
        # Ridicarea se face DOAR dacă fereastra chiar e în bară. Vezi de ce în
        # docstring-ul lui `ridica_puntea` — e o cicatrice, nu o optimizare.
        "if ($c -eq 9 -and -not $t::IsIconic($h.MainWindowHandle)) { return }; "
        "[void]$t::ShowWindowAsync($h.MainWindowHandle, $c); "
        "if ($c -eq 9) { [void]$t::SetForegroundWindow($h.MainWindowHandle) }; "
        "$n++ } }; "
        "$n"
    )
    mediu = dict(os.environ, SEMNATURA=SEMNATURA_KIOSK, COD=str(cod))
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=15, env=mediu,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as ex:
        return {"ok": False, "eroare": f"{type(ex).__name__}: {ex}", "ferestre": 0}
    try:
        cate = int((r.stdout or "0").strip().splitlines()[-1])
    except Exception:
        cate = 0
    # `ferestre: 0` NU e succes. Puntea îl citește și o spune pe ecran: altfel
    # apeși Escape de zece ori întrebându-te ce e stricat.
    return {"ok": cate > 0, "ferestre": cate,
            "detaliu": (r.stderr or "").strip()[:200]}


@app.post("/api/minimizeaza-puntea")
def minimizeaza_puntea():
    """Trimite JA.S.Mine în bară. 6 = SW_MINIMIZE."""
    return arata_fereastra(6)


@app.post("/api/ridica-puntea")
def ridica_puntea():
    """Scoate JA.S.Mine din bară și o aduce în față. 9 = SW_RESTORE.

    Cerut de trezire: dacă JA.S.Mine e minimizată, urechea aude mai departe, deci
    „engage" ar deschide chatul într-o fereastră pe care n-o vede nimeni.

    DOAR DIN BARĂ, ȘI ASTA E O CICATRICE. Ridicată de fiecare dată, JA.S.Mine
    sărea în față peste ce lucra omul, îi lua tastatura, iar clickul cu care se
    întorcea la treabă nimerea butonul „Închide" al panoului. Starea nu trebuie
    ținută minte nicăieri: `IsIconic` o întreabă direct de la fereastră.

    **O unealtă n-are voie să sară peste ce face omul.** Din bară e altceva:
    acolo a chemat-o el, și nu i-ar folosi la nimic ascunsă.
    """
    return arata_fereastra(9)


@app.post("/api/inchide-puntea")
def inchide_puntea():
    """Oprește fereastra kiosk a lui JA.S.Mine. Serverul nu se oprește aici.

    Se caută procesele DUPĂ LINIA DE COMANDĂ, niciodată după numele imaginii.
    `chrome.exe` e un nume pe care îl poartă și ferestrele omului; dacă filtrul
    nu găsește nimic, aici nu se omoară nimic și se raportează `oprite: 0`.

    Prin PowerShell, nu prin `psutil`: PowerShell e deja pe orice Windows, iar o
    dependență nouă doar pentru atât n-ar plăti.

    SE ÎNCHIDE POLITICOS, nu se omoară: cu `Stop-Process -Force`, Chrome scrie
    `exit_type: Crashed` în profil, iar la pornirea următoare restaurează sesiunea
    într-o fereastră NORMALĂ, cu bară de titlu, în loc de kiosk. O ieșire care
    strică intrarea, vizibilă abia data viitoare.

    SPUNE CE A PĂȚIT, în `date\\iesire.jsonl`: o cifră ca `fortate: 9` care
    pleacă într-un `print()` moare cu consola și nu se mai poate afla de ce.
    """
    # `@(...)` nu e decor: cu un singur proces găsit, PowerShell întoarce
    # obiectul, nu o listă, iar numărătoarea de la sfârșit ar ieși altfel. SE
    # PUNE ÎN JURUL APELULUI, nu doar înăuntrul scriptblock-ului: `@()`
    # dinăuntru se PIERDE la returnare, iar bucla de răbdare de mai jos ar ieși
    # la prima tură exact în cazul pentru care există (un `crashpad-handler`
    # întârziat, singur) și l-ar omorî cu forța.
    #
    # Chrome are un proces părinte și câte unul per filă/GPU — toate poartă
    # `--user-data-dir`, deci toate se potrivesc. Dar `WM_CLOSE` se trimite
    # DOAR părintelui (cel fără `--type=`): el își închide singur copiii, curat,
    # și abia asta scrie `exit_type: Normal`.
    #
    # RAPORTEAZĂ, nu aruncă: aceeași cifră poate însemna că WM_CLOSE n-a plecat
    # niciodată (fereastră lipsă sau blocată de un dialog) sau că a plecat și au
    # rămas copii orfani — două locuri diferite de reparat.
    ps = (
        "Add-Type -Name L5 -Namespace N -MemberDefinition "
        "'[DllImport(\"user32.dll\")] public static extern bool IsIconic(IntPtr h); "
        "[DllImport(\"user32.dll\")] public static extern bool IsWindowEnabled(IntPtr h); "
        "[DllImport(\"user32.dll\")] public static extern IntPtr GetDesktopWindow(); "
        "[DllImport(\"user32.dll\")] public static extern IntPtr GetWindow(IntPtr h, uint c); "
        "[DllImport(\"user32.dll\")] public static extern uint GetWindowThreadProcessId"
        "(IntPtr h, out uint p); "
        "[DllImport(\"user32.dll\")] public static extern bool IsWindowVisible(IntPtr h); "
        "[DllImport(\"user32.dll\")] public static extern bool PostMessage"
        "(IntPtr h, uint m, IntPtr w, IntPtr l);'; "
        # TOATE ferestrele procesului, nu doar `MainWindowHandle`. Un proces
        # Chrome poate ține mai multe ferestre de nivel înalt (a doua lansare pe
        # același profil face exact asta), iar `CloseMainWindow()` o închide pe
        # UNA; cealaltă ar rămâne vie, invizibilă și pentru minimizare, și pentru
        # ieșire.
        #
        # `GetWindow(desktop, GW_CHILD)` plus `GW_HWNDNEXT` parcurge ferestrele
        # de nivel înalt fără `EnumWindows`, care ar fi cerut un delegate, deci o
        # clasă compilată la fiecare ieșire.
        "function Ferestrele($procid) { $l = @(); "
        "$h = [N.L5]::GetWindow([N.L5]::GetDesktopWindow(), 5); "
        "while ($h -ne [IntPtr]::Zero) { $q = 0; "
        "[void][N.L5]::GetWindowThreadProcessId($h, [ref]$q); "
        "if ($q -eq $procid -and [N.L5]::IsWindowVisible($h)) { $l += $h }; "
        "$h = [N.L5]::GetWindow($h, 2) }; $l }; "
        # Numele procesului rămas contează mai mult decât numărul lui: `PARINTE`
        # rămas în viață și un `crashpad-handler` întârziat sunt două povești.
        "function Tip($cl) { "
        "if ($cl -match '--utility-sub-type=([\\w.-]+)') { return $matches[1] } "
        "if ($cl -match '--type=([\\w.-]+)') { return $matches[1] } 'PARINTE' }; "
        "$gaseste = { @(Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
        "Where-Object { $_.CommandLine -like '*' + $env:SEMNATURA + '*' }) }; "
        "$toate = @(& $gaseste); "
        "$parinti = @(); "
        "$toate | Where-Object { $_.CommandLine -notlike '*--type=*' } | "
        # `$id` capturat o dată: mai jos se intră într-un `foreach`, iar `$_` de
        # acolo nu mai e procesul.
        "ForEach-Object { $id = $_.ProcessId; "
        "$h = Get-Process -Id $id -ErrorAction SilentlyContinue; "
        # `hwnd = 0` înseamnă proces fără fereastră; `ferestre` gol spune același
        # lucru, dar spune și că s-a căutat.
        "if ($h) { $w = [int64]$h.MainWindowHandle; "
        # WM_CLOSE (0x0010) prin `PostMessage`, la FIECARE fereastră — asta face
        # și `CloseMainWindow()`, dar numai pentru prima. Politicos, deci
        # profilul rămâne `exit_type: Normal`.
        "$fer = @(Ferestrele $id); $postate = @(); "
        "foreach ($x in $fer) { $postate += @{ h = [int64]$x; "
        "postat = [N.L5]::PostMessage($x, 0x0010, [IntPtr]::Zero, [IntPtr]::Zero) } }; "
        "$parinti += @{ pid = $id; hwnd = $w; "
        "iconic = $(if ($w) { [N.L5]::IsIconic($h.MainWindowHandle) } else { $false }); "
        "enabled = $(if ($w) { [N.L5]::IsWindowEnabled($h.MainWindowHandle) } else { $false }); "
        # Lista întreagă în jurnal, nu doar câte au fost. `hwnd` rămâne lângă ea
        # fiindcă e chiar fereastra pe care o vedea serverul înainte: a doua se
        # citește din urmă, nu se deduce.
        "ferestre = @($postate); "
        "inchis = @($postate | Where-Object { $_.postat }).Count -gt 0 } } "
        "else { $parinti += @{ pid = $id; hwnd = 0; iconic = $false; "
        "enabled = $false; ferestre = @(); inchis = $null } } }; "
        # Cinci secunde e răbdare, nu termen: Chrome își încheie copiii în
        # câteva sute de ms. Bucla iese devreme, nu așteaptă degeaba.
        #
        # SE NUMĂRĂ PE CEAS, NU PE ITERAȚII: fiecare tură face și un
        # `Get-CimInstance` (~150 ms), deci 50 de ture a câte 100 ms ar ține
        # ~12,5 s, nu cinci. Ceasul e singurul care știe cât a trecut.
        "$ceas = [Diagnostics.Stopwatch]::StartNew(); "
        "while ($ceas.ElapsedMilliseconds -lt 5000 -and @(& $gaseste).Count -gt 0) "
        "{ Start-Sleep -Milliseconds 100 }; "
        "$ms = [int]$ceas.ElapsedMilliseconds; "
        # Ultima instanță. Aici se pierde marcajul curat — de-asta e ultima,
        # nu prima. Se raportează separat, ca să se vadă când s-a ajuns la ea.
        "$ramase = @(& $gaseste); "
        "$ramase | ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
        "-ErrorAction SilentlyContinue }; "
        "@{ gasite = $toate.Count; parinti = @($parinti); ms = $ms; "
        "ramase = @($ramase | ForEach-Object { @{ pid = $_.ProcessId; "
        "tip = (Tip $_.CommandLine) } }) } | ConvertTo-Json -Compress -Depth 5"
    )
    # Semnătura merge prin mediu, nu lipită în text: are backslash-uri și, la o
    # mutare a lui JA.S.Mine, ar putea avea spații. Un citat greșit ar face filtrul să
    # nu se mai potrivească pe nimic — adică o ieșire care tace.
    mediu = dict(os.environ, SEMNATURA=SEMNATURA_KIOSK)
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=15, env=mediu,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as ex:
        return JSONResponse(
            {"ok": False, "eroare": "nu s-a putut cauta fereastra",
             "detaliu": f"{type(ex).__name__}: {ex}"}, status_code=500)

    if r.returncode != 0:
        return JSONResponse(
            {"ok": False, "eroare": "oprirea a esuat",
             "detaliu": (r.stderr or "").strip()[:400]}, status_code=500)
    iesire = (r.stdout or "").strip().splitlines()
    try:
        raport = json.loads(iesire[-1])
    except (ValueError, IndexError):
        # Un raport pe care nu-l pot citi nu e motiv să spun că ieșirea a eșuat:
        # procesele au fost deja oprite când s-a ajuns aici. Se raportează zero
        # și se scrie ce a venit, ca să se vadă de ce nu s-a putut citi.
        raport = {"necitit": (r.stdout or "").strip()[:400]}

    oprite = int(raport.get("gasite") or 0)
    parinti = raport.get("parinti") or []
    ramase = raport.get("ramase") or []
    fortate = len(ramase)
    print(f"  iesire din JA.S.Mine: {oprite} procese ale kiosk-ului, "
          f"{fortate} au cerut forta")

    # Urma pe disc: `print()` moare cu consola serverului, iar o ieșire care
    # lasă cicatrice pe profil trebuie să lase și o urmă despre ce a pățit.
    #
    # Nu se arată nicăieri pe ecran, ca jurnalul trezirii: `fortate > 0` e rar,
    # iar cât de rar se află din folosire, nu din încă o rundă de probe.
    rand = {"gasite": oprite, "fortate": fortate,
            "ms": raport.get("ms"), "parinti": parinti, "ramase": ramase}
    if "necitit" in raport:
        rand["necitit"] = raport["necitit"]
    scrie_iesirea(rand)

    # `fortate > 0` nu e o eroare, e o cicatrice: profilul rămâne marcat
    # „Crashed" și fereastra următoare se deschide cu bară de titlu. Lângă cifră
    # stă și CE a rămas: un `PARINTE` în viață înseamnă că WM_CLOSE n-a plecat;
    # numai copii, că a plecat și n-a tras după el tot.
    return {"ok": True, "oprite": oprite, "fortate": fortate,
            "ms": raport.get("ms"), "parinti": parinti, "ramase": ramase,
            "semnatura": SEMNATURA_KIOSK}


# ── Motorul de conversație ───────────────────────────────────────────
# Open WebUI rulează în Docker, iar Docker atârnă de JA.S.Mine, nu de logon:
# legat de logon costă ~7 GB de commit toată ziua, legat de aici doar cât o
# folosești. Pornirea stă în `reporneste-jasmine.vbs`, prima linie. Oprirea e
# aici, și e chemată ÎNAINTE de `window.close()`: drumul obișnuit de ieșire nu
# trece prin `/api/inchide-puntea`, care e doar rezerva pentru cazul în care
# Chrome refuză.

def opreste_motorul() -> dict:
    """`docker desktop stop`, cu plafon. RAPORTEAZĂ ce a pățit, nu ridică.

    Sincronă și fără `await` înăuntru fiindcă are un singur chemător care o
    duce în threadpool. `docker` se caută în PATH: dacă nu e acolo, eroarea își
    spune numele, în loc să inventeze modelul unul (aceeași regulă ca la
    `server` din registrul de unelte).
    """
    t0 = time.monotonic()
    try:
        r = subprocess.run(
            ["docker", "desktop", "stop"],
            capture_output=True, text=True, timeout=90,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return {"motor": "nu s-a oprit in 90 s"}
    except Exception as ex:
        return {"motor": "necheamat", "eroare": f"{type(ex).__name__}: {ex}"}
    ms = int((time.monotonic() - t0) * 1000)
    if r.returncode != 0:
        return {"motor": "esuat", "ms": ms,
                "eroare": (r.stderr or r.stdout or "").strip()[:300]}
    return {"motor": "oprit", "ms": ms}


# ── Ieșirea: fereastra, motorul, serverul ────────────────────────────
# Ieșirea stinge și serverul: el nu mai pornește la logon, deci un server lăsat
# viu ar ține ~2,3 GB de commit până la oprirea calculatorului.
#
# ORDINEA E TOT CE CONTEAZĂ AICI. `/api/inchide-puntea` e rezerva pentru cazul
# în care Chrome refuză `window.close()`; un server care moare primul lasă
# rezerva fără ușă. Deci serverul se stinge ULTIMUL, după ce fereastra chiar a
# dispărut — și nu se stinge deloc dacă n-a dispărut.

# Serverul uvicorn, ținut ca să se poată opri curat. `uvicorn.run()` îl
# construiește înăuntru și nu-l dă nimănui, deci `__main__` îl face cu mâna.
# `None` înseamnă „pornit altfel decât prin `py server.py`" — atunci rămâne
# doar ieșirea brutală, care e mai bună decât un server care nu poate muri.
SERVER_UVICORN = None


def asteapta_kioskul_mort(plafon_s: float = 15.0) -> dict:
    """Așteaptă până nu mai există procese ale kiosk-ului. Câte au rămas.

    Un singur PowerShell care așteaptă înăuntru, nu treizeci chemate în buclă
    din Python: fiecare `Get-CimInstance` costă ~150 ms, iar aici se numără pe
    ceas, ca în `inchide_puntea()`.

    ACELAȘI FILTRU, pe LINIA DE COMANDĂ. Aici nu se omoară nimic, dar cifra
    hotărăște dacă moare serverul, iar `chrome.exe` e un nume purtat și de
    ferestrele omului.

    `ramase: -1` înseamnă „nu se poate ști", și se tratează ca „viu".

    `@()` SE PUNE ÎN JURUL APELULUI, nu doar în scriptblock: `@()` dinăuntru se
    pierde la returnare, iar cu UN SINGUR proces găsit PowerShell întoarce
    obiectul, nu lista — `.Count` iese gol și bucla care trebuia să aștepte iese
    la prima tură.
    """
    ps = (
        "$p = [int]$env:PLAFON; "
        "$gaseste = { Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
        "Where-Object { $_.CommandLine -like '*' + $env:SEMNATURA + '*' } }; "
        "$ceas = [Diagnostics.Stopwatch]::StartNew(); "
        "while ($ceas.ElapsedMilliseconds -lt $p -and @(& $gaseste).Count -gt 0) "
        "{ Start-Sleep -Milliseconds 200 }; "
        "@{ ramase = @(& $gaseste).Count; ms = [int]$ceas.ElapsedMilliseconds } | "
        "ConvertTo-Json -Compress"
    )
    mediu = dict(os.environ, SEMNATURA=SEMNATURA_KIOSK,
                 PLAFON=str(int(plafon_s * 1000)))
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=plafon_s + 10, env=mediu,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return json.loads((r.stdout or "").strip().splitlines()[-1])
    except Exception as ex:
        return {"ramase": -1, "eroare": f"{type(ex).__name__}: {ex}"}


def paznicul_iesirii(cerut_la: float) -> None:
    """Stinge serverul — dar numai dacă fereastra a plecat și n-a venit alta.

    Rulează DUPĂ ce a plecat răspunsul (`BackgroundTask`): un server care se
    omoară înainte de propriul răspuns nu se poate proba cu nimic.

    DOUĂ MOTIVE DE A NU STINGE, amândouă scrise în jurnal:

    1. **Fereastra n-a murit.** Un kiosk viu în fața unui server mort n-ar mai
       avea nicio ușă — nici `×`, nici chat, nici măcar mesajul care explică.
    2. **S-a servit o punte NOUĂ.** `docker desktop stop` ține ~14 s, iar în
       intervalul ăsta omul poate redeschide JA.S.Mine: `reporneste-jasmine.vbs`
       găsește serverul viu, nu pornește altul — și serverul ar muri sub
       fereastra abia deschisă. Puntea e `no-store`, deci o cerere a lui `/`
       sosită după apăsarea lui `×` înseamnă exact o fereastră nouă.
    """
    raport = asteapta_kioskul_mort()
    ramase = raport.get("ramase", -1)
    if ULTIMA_PUNTE > cerut_la:
        stare = {"server": "pastrat", "motiv": "punte noua"}
    elif ramase != 0:
        stare = {"server": "pastrat", "motiv": "fereastra inca vie",
                 "ramase": ramase}
    else:
        stare = {"server": "oprit"}
    if "eroare" in raport:
        stare["eroare"] = raport["eroare"]
    stare["ms"] = raport.get("ms")
    print(f"  serverul la iesire: {stare.get('server')} "
          f"({stare.get('motiv', 'fereastra a plecat')})")
    scrie_iesirea(stare)

    if stare["server"] != "oprit":
        return
    # Curat, prin uvicorn: `should_exit` lasă bucla să-și încheie ce are pe
    # mână. Plasa de 5 secunde e pentru cazul în care ceva o ține pe loc — o
    # ieșire care se poate agăța n-ar fi o ieșire. Firul e `daemon`, deci dacă
    # uvicorn iese curat, plasa moare cu procesul, fără s-o aștepte nimeni.
    if SERVER_UVICORN is not None:
        SERVER_UVICORN.should_exit = True
        plasa = threading.Timer(5.0, lambda: os._exit(0))
        plasa.daemon = True
        plasa.start()
    else:
        os._exit(0)


@app.post("/api/iesire")
async def iesire():
    """Ușa de ieșire: stinge motorul, apoi serverul. Fereastra se închide singură.

    Puntea o cheamă din `inchideJasmine()`, cu `keepalive` și fără să aștepte
    răspunsul, ÎNAINTE de `window.close()` — drumul obișnuit nu trece prin
    `/api/inchide-puntea`, aia e doar rezerva.

    THREADPOOL: `docker desktop stop` ține secunde bune, iar pe bucla async ar
    îngheța serverul întreg. Răspunsul se așteaptă totuși — pagina nu-l mai
    citește, dar un `curl` de verificare, da.

    EȘECUL MOTORULUI NU E VIZIBIL NICĂIERI, deliberat: un motor care nu se poate
    opri n-are voie să blocheze ușa. Un Docker rămas în viață costă commit, nu
    corectitudine — iar urma rămâne pe disc, în `date\\iesire.jsonl`.
    """
    # Luat ÎNAINTE de `docker desktop stop`: fereastra de risc e tot intervalul
    # cât ține oprirea motorului, nu doar clipa de la capătul lui.
    cerut_la = time.monotonic()
    raport = await run_in_threadpool(opreste_motorul)
    print(f"  motorul la iesire: {raport.get('motor')}")
    scrie_iesirea(raport)
    return JSONResponse({"ok": raport.get("motor") == "oprit", **raport},
                        background=BackgroundTask(paznicul_iesirii, cerut_la))


# ── Cele trei „servere" ──────────────────────────────────────────────
# În sistem sunt trei lucruri pe care omul le numește „server", și fiecare se
# repornește altfel:
#
#   1. serverul JA.S.Mine  (127.0.0.1:8000)  biroul — jurnalul, proiectele, puntea
#   2. Open WebUI      (Docker, :3000)   motorul de conversație al straturilor
#   3. agentul 051     (MCP, :8051)      uneltele de recunoaștere
#
# Sondele sunt APELURI ADEVĂRATE, nu verificări de port deschis. Un port care
# ascultă nu dovedește nimic despre token, protocol sau sănătatea din spate. La
# agent se cheamă unealta reală: e deterministă și nu costă niciun token.
SONDAJ_HTTP_SECUNDE = 2.0
SONDAJ_MCP_SECUNDE = 4.0


def docker_porneste() -> bool:
    """Există procesul Docker Desktop? Doar atât, și doar ca dezambiguizare.

    ASTA NU E O SONDĂ, ca și `accepta_conexiuni()` de mai jos: se cheamă NUMAI
    după ce sonda adevărată (`GET /health`) a picat, și numai ca să alegem
    cuvântul — „pornește" sau „e oprit". Motorul are nevoie de ~9–12 s după ce
    serverul răspunde, iar în tot intervalul portul e închis; fără întrebarea
    asta, un motor care se ridică și unul care nu există arată identic.

    SE CAUTĂ DUPĂ NUMELE IMAGINII, nu după linia de comandă: regula liniei de
    comandă e despre procesele care se OMOARĂ, iar aici nu se omoară nimic. Fals
    la orice eroare: o interogare care nu merge n-are voie să inventeze o stare.

    Costă ~150 ms și se plătește numai în cazul de eșec.
    """
    ps = ("@(Get-CimInstance Win32_Process -Filter "
          "\"Name='Docker Desktop.exe'\").Count")
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return int((r.stdout or "0").strip().splitlines()[-1]) > 0
    except Exception:
        return False


async def sondeaza_open_webui() -> dict:
    baza = (os.environ.get("OPENWEBUI_URL") or "").rstrip("/")
    if not baza:
        return {"viu": False, "motiv": "OPENWEBUI_URL lipseste din server/.env"}
    try:
        async with httpx.AsyncClient(timeout=SONDAJ_HTTP_SECUNDE) as client:
            r = await client.get(baza + "/health")
        if r.status_code == 200:
            return {"viu": True, "unde": baza, "cum": "viu"}
        # Motorul trăiește și a răspuns, dar cu un cod de eroare. NU se cheamă
        # „oprit": nu se repară pornind ce e deja pornit. Aceeași despărțire ca
        # în cele trei ramuri ale lui `/api/chat`.
        return {"viu": False, "unde": baza, "porneste": False, "cum": "eroare",
                "motiv": f"a raspuns cu {r.status_code}"}
    except Exception as ex:
        # Container oprit, Docker oprit, sau adresa greșită. Toate trei se văd
        # la fel de aici; detaliul spune care.
        #
        # `porneste` desparte „se ridică" de „nu există", și de el atârnă atât
        # culoarea nodurilor pe hartă, cât și mesajul din chat. În threadpool
        # fiindcă `subprocess.run` blochează, iar bucla async ține tot serverul.
        porneste = await run_in_threadpool(docker_porneste)
        return {"viu": False, "unde": baza, "porneste": porneste,
                "cum": "porneste" if porneste else "oprit",
                "motiv": f"nu raspunde ({type(ex).__name__}) — "
                         + ("motorul porneste" if porneste else "verifica Docker")}


async def accepta_conexiuni(url: str) -> bool:
    """Acceptă portul o conexiune TCP? Doar atât, și doar ca dezambiguizare.

    ASTA NU E O SONDĂ și nu redeschide regula „sondele sunt apeluri
    adevărate, nu verificări de port deschis". Sonda rămâne apelul MCP de
    mai jos, întreg. Portul se întreabă NUMAI după ce apelul adevărat a
    expirat, și numai ca să alegem cuvântul din mesaj: „oprit" sau
    „ocupat". Un port deschis nu dovedește niciodată că agentul lucrează —
    dar un port ÎNCHIS dovedește că e oprit, iar asta e tot ce-i cerem.
    """
    try:
        gazda = urlparse(url)
        legatura = asyncio.open_connection(gazda.hostname, gazda.port or 80)
        _, scriitor = await asyncio.wait_for(legatura, 1.0)
        scriitor.close()
        return True
    except Exception:
        return False


async def sondeaza_agent_051() -> dict:
    url = os.environ.get("RECON_MCP_URL") or ""
    if not url:
        return {"viu": False, "motiv": "RECON_MCP_URL lipseste din server/.env"}
    try:
        date = await asyncio.wait_for(cheama_recon("stare_pipeline", {}),
                                      SONDAJ_MCP_SECUNDE)
        if date.get("ok"):
            return {"viu": True, "unde": url}
        # Agentul a răspuns, dar cu eșec: e VIU, doar treaba lui a picat.
        # Diferența contează — „pornește-l" ar fi sfatul greșit.
        return {"viu": True, "unde": url,
                "raspunde_dar_esueaza": date.get("eroare") or "ok:false"}
    except (asyncio.TimeoutError, TimeoutError):
        # A PATRA STARE: ocupat. O rulare a lanțului ține zeci de secunde chiar
        # când nu găsește nimic nou, iar sonda are patru. Deci în fiecare rulare
        # există o fereastră în care agentul e sănătos și ocupat, iar sonda
        # expiră. Un port care acceptă conexiuni înseamnă „ocupat", nu „mort".
        if await accepta_conexiuni(url):
            return {"viu": True, "unde": url,
                    "ocupat": f"nu a raspuns in {SONDAJ_MCP_SECUNDE:.0f} s, dar portul "
                              "accepta conexiuni — ori lucreaza, ori e blocat. Din afara "
                              "nu se deosebesc: uita-te inainte sa-l repornesti."}
        return {"viu": False, "unde": url,
                "motiv": "portul nu accepta conexiuni — agentul e oprit"}
    except Exception as ex:
        return {"viu": False, "unde": url,
                "motiv": f"nu raspunde — {descrie_exceptia(ex)}"}


@app.get("/api/motor")
async def motor():
    """Poate Sky să răspundă ACUM? Doar motorul de conversație, nimic altceva.

    Există separat de `/api/stare` fiindcă puntea îl întreabă ÎN BUCLĂ cât e
    rece, la 2 secunde, iar `/api/stare` sondează și agentul 051 cu un apel MCP
    adevărat. Fiecare sistem cu ușa lui de citire.

    Sonda e aceeași funcție, nu o a doua părere: `/api/stare` și endpointul ăsta
    nu pot ajunge să spună lucruri diferite despre același motor.
    """
    return {"ok": True, "motor": await sondeaza_open_webui()}


@app.get("/api/viu")
def viu():
    """„Mai trăiește serverul meu?" Atât. Zero sonde, zero așteptare.

    Pentru `reporneste-jasmine.vbs`, care întreabă în buclă înainte de a lansa
    Chrome. `/api/stare` face apeluri ADEVĂRATE la Open WebUI și la agent, iar
    cu Docker încă în pornire alea expiră: ~2,5 s plătite din drumul spre
    fereastră.

    Nu redeschide regula sondelor: aia e despre ce arată HARTA. Aici răspunsul
    e chiar faptul că mesajul ăsta a plecat.
    """
    return {"ok": True}


@app.get("/api/stare")
async def stare():
    """Verificare rapidă: serverul trăiește, vede fișierele lui, și ce e cu
    celelalte două servere din spatele lui.

    Cele două sonde se fac în paralel: una moartă n-are voie s-o întârzie pe
    cealaltă.
    """
    open_webui, agent_051 = await asyncio.gather(
        sondeaza_open_webui(), sondeaza_agent_051())
    return {
        # Câmpul vechi rămâne neatins: `reporneste-jasmine.vbs` verifică doar
        # codul 200, dar un client viitor s-ar putea uita la el.
        "server": "ok",
        "radacina": str(RADACINA),
        "punte": PUNTE.exists(),
        "intrari_jurnal": len(citeste_jurnal()),
        "servere": {
            "jasmine": {"viu": True, "unde": "http://127.0.0.1:8000"},
            "open_webui": open_webui,
            "agent_051": agent_051,
        },
        "dictare": {
            "gata": DICTARE["gata"],
            # A treia stare: „încă se compilează" nu e „a picat". Cine o citește
            # știe că mai are de așteptat, nu de reparat.
            "se_incarca": DICTARE["se_incarca"],
            # Modelul iGPU, nu cel al rezervei: ăsta lucrează. `viu` deosebește
            # „dictarea merge" de „dictarea merge PE iGPU".
            "model": DICTARE["reglaje"].get("model_igpu"),
            "dispozitiv": DICTARE["reglaje"].get("dispozitiv_igpu"),
            "igpu": DICTARE["conducta"] is not None,
            "motiv": DICTARE["motiv"] or None,
            # Rezerva se raportează separat, cu motiv: `neincarcat` NU e o
            # defecțiune, e cazul obișnuit. Fără rândul ăsta, singurul semn că
            # iGPU-ul a căzut și s-a aprins `faster-whisper` ar fi lipsa oricărui
            # semn.
            "rezerva": {
                "stare": REZERVA["stare"],
                "motiv": REZERVA["motiv"] or None,
            },
            # Reglajele, în cele două regimuri ale lor. `cer_repornire` e gol în
            # cazul obișnuit; nevid înseamnă că cineva a schimbat în fișier o
            # cifră care se aplică doar la construcție, iar serverul merge mai
            # departe cu cea veche.
            "reglaje": {
                "la_cald": not DICTARE["cald_motiv"],
                "motiv": DICTARE["cald_motiv"] or None,
                "cer_repornire": reglaje_reci_schimbate(),
            },
        },
    }


# Limită de dimensiune: 25 MB ≈ multe minute de opus. Nu e o măsură de
# securitate (serverul ascultă doar pe 127.0.0.1), e o plasă împotriva unui
# client stricat care trimite un fișier greșit și blochează CPU-ul minute.
MAX_AUDIO_OCTETI = 25 * 1024 * 1024


@app.get("/api/dictare")
def dictare_reglaje():
    """Reglajele pentru punte. Citite la FIECARE cerere, ca proiectele.

    Pragul de tăcere se află prin folosire, nu prin discuție — deci trebuie să
    se poată schimba în fișier fără repornirea serverului. La fel se citesc și
    reglajele folosite pe server (`limba`, `prompt_initial`, `hotwords`,
    `beam_size`, `granita_igpu`), în /api/transcrie.

    Ce NU se poate reciti, fiindcă se aplică la construcție: `model_igpu`,
    `dispozitiv_igpu` și cele patru ale rezervei — vezi CHEI_LA_RECE. Schimbate
    în fișier, își spun numele în /api/stare (`dictare.reglaje.cer_repornire`).
    """
    try:
        reglaje = citeste_dictare()
    except Exception as ex:
        return {"ok": False, "eroare": f"{type(ex).__name__}: {ex}"}
    # Trezirea merge la punte doar întreagă. Jumătate de configurare ar fi mai
    # rea decât niciuna: urechea ar sta deschisă toată ziua fără să știe după ce
    # cuvânt ascultă. Și numai cu iGPU-ul viu — pe rezervă fiecare probă ar
    # costa ~13 s de CPU în loc de ~0,25. Puntea vede lipsa ca `trezire: null` și
    # rămâne pe Alt+A.
    #
    # Fără niciun cuvânt valid nu e trezire, oricât de întregi ar fi celelalte
    # chei: o ureche armată care nu se poate trezi ar face COMMAND portocaliu
    # mincinos. Nod rece, motivul sub el, Alt+A.
    trezire = None
    cuvinte_de_trezire = cuvinte_trezire(reglaje)
    if (all(c in reglaje for c in CHEI_TREZIRE) and cuvinte_de_trezire
            and DICTARE["conducta"]):
        trezire = {c: reglaje[c] for c in CHEI_TREZIRE if c != "cuvant_adormire"}
        # Ca și adormirea, pleacă gata listă: puntea n-are de ghicit forma.
        trezire["cuvant_trezire"] = cuvinte_de_trezire
    return {"ok": True, "gata": DICTARE["gata"],
            # Cât e adevărat, puntea reîncearcă; când cade, decide o dată.
            # Semnalul de oprire vine de AICI, nu dintr-un cronometru din pagină.
            # `motiv` merge cu el: când puntea renunță, consola trebuie să spună
            # DE CE, altfel rămâne doar „nu e pornită" — și trei locuri de căutat.
            "se_incarca": DICTARE["se_incarca"],
            "motiv": DICTARE["motiv"] or None,
            # Cuvintele de adormire pleacă SEPARAT de restul trezirii, și
            # necondiționat. `cuvant_adormire` rămâne în CHEI_TREZIRE — o ureche
            # care poate trezi dar nu adormi nu pornește — dar a doua lui ușă e
            # TASTAREA, care n-are nevoie de ureche. Legat de `trezire`, ușa aia
            # ar muri exact când omul e mai tentat s-o folosească: ceva nu merge.
            #
            # Puntea le primește gata ca listă, ca să nu aibă de ghicit forma.
            "cuvinte_adormire": cuvinte_adormire(reglaje),
            "prag_tacere_secunde": reglaje["prag_tacere_secunde"],
            "secunde_razgandire": reglaje["secunde_razgandire"],
            "prag_volum": reglaje["prag_volum"],
            "secunde_minim_vorbire": reglaje["secunde_minim_vorbire"],
            # A doua treaptă a porții, folosită DOAR în ENGAGE: sub atâtea
            # secunde vorbite, rostirea se transcrie dar nu pleacă la Sky decât
            # dacă e cuvânt-comandă. Poarta de la intrare a coborât ca „stop" —
            # o silabă — să se poată naște; asta ține factura pe loc.
            "secunde_incredere_vorbire": reglaje["secunde_incredere_vorbire"],
            "trezire": trezire}


@app.post("/api/transcrie")
async def transcrie(audio: UploadFile = File(...)):
    """Audio în octeți → text. Determinist, zero tokeni, nimic pe disc.

    Endpoint-ul e scris ca să fie folosibil ȘI din afara punții, de orice
    client HTTP: multipart standard, nu un format inventat pentru browser.
    """
    if not DICTARE["gata"]:
        return {"ok": False, "eroare": "dictarea nu e disponibila",
                "detaliu": DICTARE["motiv"]}
    continut = await audio.read()
    if not continut:
        return {"ok": False, "eroare": "audio gol"}
    if len(continut) > MAX_AUDIO_OCTETI:
        return {"ok": False, "eroare": "audio prea mare",
                "detaliu": f"{len(continut)} octeti, limita {MAX_AUDIO_OCTETI}"}

    def lucreaza() -> tuple[str, float, str, str]:
        """Decodează o dată, transcrie pe iGPU, cade pe rezervă doar la excepție.

        Întoarce și CINE a lucrat, și DE CE — dacă a lucrat rezerva. Fără al
        doilea, „a mers mai încet azi" nu se poate deosebi de „iGPU-ul e mort".
        """
        import numpy as np

        # Reglajele se citesc AICI, la fiecare transcriere: `limba`,
        # `prompt_initial`, `hotwords` și `beam_size` se află prin măsurătoare,
        # iar o repornire per cifră încercată ar opri încercările. Cheile reci vin
        # din instantaneu — vezi reglaje_la_cald().
        reglaje = reglaje_la_cald()
        esantioane, secunde = decodeaza(continut)
        if secunde <= 0:
            raise ValueError("audio fara esantioane dupa decodare")
        audio = esantioane.astype(np.float32) / 32768.0

        # Ruta e „tot pe iGPU", nu o ramură pe durată — decizie de proiectare, cu
        # motiv de mașină: mai multe motoare Whisper deodată cer fire din același
        # cip. `granita_igpu` rămâne un plafon, ca să se poată coborî dintr-o
        # cifră; azi e pus peste orice dictare reală.
        motiv_rezerva = ""
        granita = float(reglaje["granita_igpu"])
        if DICTARE["conducta"] is not None and 0 < secunde <= granita:
            try:
                with DICTARE["lacat"]:
                    text = str(DICTARE["conducta"].generate(
                        audio,
                        configurare_generare(DICTARE["conducta"], reglaje)))
                # Spațiile se normalizează, ALTFEL cele două motoare scriu
                # diferit aceeași frază, iar un client care primește text dintr-un
                # endpoint ar trebui să ghicească ce motor l-a produs.
                return (re.sub(r"\s+", " ", text).strip(), secunde,
                        "openvino-igpu", "")
            except Exception as ex:
                # **O dictare nu se pierde fiindcă un motor a picat.** Se cade pe
                # rezervă, și motivul pleacă mai departe în răspuns — un iGPU
                # care cedează în tăcere ar apărea drept „JA.S.Mine a devenit
                # lentă".
                motiv_rezerva = f"iGPU a picat: {type(ex).__name__}: {ex}"
                print(f"  {motiv_rezerva}; se reia pe rezerva")
        elif DICTARE["conducta"] is None:
            motiv_rezerva = DICTARE["motiv"] or "iGPU stins din configurare"
        else:
            motiv_rezerva = (f"peste granita_igpu ({secunde:.1f} s > "
                             f"{granita:g} s)")

        # Rezerva se încarcă ABIA ACUM, la prima nevoie. Suntem deja în
        # threadpool, deci cele 10–17 s nu ating bucla async.
        if not incarca_rezerva():
            raise RuntimeError(f"{motiv_rezerva}; iar rezerva nu e "
                               f"disponibila: {REZERVA['motiv']}")

        # vad_filter: detectorul de voce taie porțiunile fără vorbire înainte
        # de model. Pe audio aproape gol, Whisper prezice text din antrenament,
        # repetat („mulțumim pentru vizionare" ×3 pe o pauză de 30 s).
        # beam_size=1: text identic cu 5, cu ~25% mai rapid.
        # Vectorul, nu octeții: audio-ul e deja decodat mai sus. Trimis ca
        # BytesIO, `faster-whisper` l-ar decoda a doua oară, degeaba.
        segmente, _ = REZERVA["model"].transcribe(
            audio, language=reglaje["limba"], vad_filter=True,
            beam_size=int(reglaje["beam_size"]),
            initial_prompt=reglaje.get("prompt_initial") or None)
        return ("".join(s.text for s in segmente).strip(), secunde,
                "faster-whisper", motiv_rezerva)

    t0 = time.perf_counter()
    try:
        # run_in_threadpool, NU direct: transcrierea a 40 s de audio durează
        # ~12 s de CPU. Rulată în bucla async, ar îngheța tot serverul în timpul
        # ăsta — RECON, jurnalul, chatul.
        text, durata_audio, motor, de_ce = await run_in_threadpool(lucreaza)
    except Exception as ex:
        return {"ok": False, "eroare": "transcrierea a esuat",
                "detaliu": f"{type(ex).__name__}: {ex}"}

    return {"ok": True, "text": text,
            "durata_audio": round(durata_audio, 2),
            "durata_transcriere": round(time.perf_counter() - t0, 2),
            # `motor` e pentru diagnostic: fără el, „a mers mai încet azi" nu se
            # poate deosebi de „iGPU-ul e mort și rezerva a preluat". `motiv` e
            # nevid DOAR când a lucrat rezerva, și spune de ce — puntea îl
            # folosește ca să nu oprească urechea pentru cauza greșită.
            "motor": motor,
            "motiv": de_ce or None}


class ProbaTrezire(BaseModel):
    """O rostire auzită de ureche, și ce s-a ales de ea.

    `urmare` spune și din ce cameră vine: din AȘTEPT (trezit · adormit ·
    aruncat · prea_lung · gol) sau din ENGAGE (dictat). Aceeași ureche, același
    drum, același jurnal — deosebirea o poartă câmpul, nu un al doilea fișier.
    """
    urmare: str = ""
    text: str = ""
    audio: float = 0
    lucru: float = 0
    motor: str | None = None
    # De ce a lucrat rezerva, când a lucrat. Nevid înseamnă „iGPU-ul n-a putut",
    # și e singurul loc în care se poate afla dacă o probă înceată a fost o
    # întâmplare sau începutul unei căderi.
    motiv: str | None = None


@app.post("/api/trezire")
def trezire_proba(proba: ProbaTrezire):
    """Jurnalul probelor de trezire. Nu se vede nicăieri pe ecran, deliberat.

    Rata alarmelor false nu se poate afla din încă zece minute de citit cu voce
    tare: se află din câte ori s-a trezit JA.S.Mine într-o săptămână de folosire.
    Singurul care poate număra asta e chiar ea.

    Rândul poartă și `prea_lung`, deși acolo nu s-a transcris nimic: fără el n-am
    ști niciodată de câte ori a intervenit plafonul, adică de câtă vorbire din
    cameră ne apără. Un plafon care lucrează în tăcere pare o cheltuială
    inutilă exact până în ziua în care e scos.
    """
    rand = {"cand": datetime.now().isoformat(timespec="seconds"),
            **proba.model_dump()}
    try:
        # Lacătul NU e prudență: endpoint-ul e sincron, deci FastAPI îl rulează
        # în threadpool — două rostiri închise aproape una de alta scriu din două
        # fire, iar un `write` lung se sparge în mai multe scrieri de sistem. S-a
        # găsit pe disc un rând rupt în două, cu coada altuia lipită peste.
        with TREZIRE_LACAT, TREZIRE_JURNAL.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rand, ensure_ascii=False) + "\n")
    except Exception as ex:
        # Un jurnal care nu se poate scrie n-are voie să oprească trezirea:
        # măsurătoarea e a noastră, urechea e a omului.
        return {"ok": False, "eroare": f"{type(ex).__name__}: {ex}"}
    return {"ok": True}


# ── CHAT: JA.S.Mine ca client al Open WebUI ──────────────────────────
# Open WebUI e stateless: nu ține minte nimic între cereri. Istoricul e
# treaba noastră — îl trimitem întreg de fiecare dată. Cheia trăiește doar
# aici, în .env; browserul n-o vede niciodată.

class CerereChat(BaseModel):
    """Cererea aduce replica NOUĂ, nu firul întreg.

    Istoricul stă pe disc și de-acolo se citește (sursă unică de adevăr).
    Un fir trimis de client ar putea pierde mesajele de unealtă pe care pagina
    nu le ține.
    """
    mesaj: str | None = None
    # OBLIGATORIU și nevid. Un șir gol ar trece de validare și ar eșua abia la
    # Open WebUI, ca „modelul nu raspunde". Poarta stă la intrare.
    #
    # E o CHEIE DE STRAT din straturi.json („sky", „socrate"), nu un id de model.
    # Traducerea se face în chat(); id-ul modelului nu vine niciodată din browser.
    strat: str = Field(min_length=1)
    id: str | None = None               # lipsește → conversație nouă


# ── Registrul uneltelor ──────────────────────────────────────────────
# Schema e ce vede modelul; `executa` e cod al nostru (modelul nu produce
# fapte, le formulează). Open WebUI primește schemele și întoarce cererea de
# apel, dar NU execută nimic: bucla e a noastră, și bine că e — tokenul
# agentului nu pleacă niciodată din serverul ăsta.
#
# Un dicționar în cod, nu un fișier: schema stă lângă funcția care o execută,
# iar o schemă fără funcție n-ar avea ce chema.

async def unealta_recon_stare(_argumente: dict) -> dict:
    return await cheama_recon("stare_pipeline", {})


async def unealta_recon_briefing(_argumente: dict) -> dict:
    # marcheaza_citit rămâne HARDCODAT False, ca la /api/recon/briefing. Mutarea
    # reperului e ireversibilă: de acolo încolo briefingurile raportează doar ce
    # a intrat după momentul ăsta. Nu devine parametru — un model care are voie
    # să-l pună pe True poate șterge trecutul dintr-o formulare nefericită.
    return await cheama_recon("briefing", {"marcheaza_citit": False})


async def unealta_comms_raport(argumente: dict) -> dict:
    """Raportul de mail. `zile` lipsă = de la raportul anterior, și mută reperul.

    Plafonul de 30 de zile nu apără Gmail-ul, ci fereastra lui Sky: `zile: 3650`
    ar aduce sute de mailuri detaliate într-un singur răspuns. Valoare
    neinterpretabilă → None, adică drumul obișnuit, nu o eroare: un model care
    scrie `zile: "ieri"` trebuie să primească raportul, nu o mustrare.
    """
    zile = argumente.get("zile")
    try:
        zile = int(zile) if zile is not None else None
    except (TypeError, ValueError):
        zile = None
    if zile is not None:
        zile = max(1, min(zile, 30))
    return await run_in_threadpool(mail.raport, zile)


async def unealta_sensors_catalog(argumente: dict) -> dict:
    """Titlurile știrilor noi. Plafonul de 72 de ore apără fereastra lui Sky.

    Valoare neinterpretabilă → None, adică fereastra obișnuită, nu o eroare:
    un model care scrie `ore: "azi"` trebuie să primească știrile, nu o mustrare.
    Tiparul e al lui `unealta_comms_raport`.
    """
    ore = argumente.get("ore")
    try:
        ore = int(ore) if ore is not None else None
    except (TypeError, ValueError):
        ore = None
    if ore is not None:
        ore = max(1, min(ore, 72))
    return await run_in_threadpool(stiri.catalog, ore)


async def unealta_sensors_detalii(argumente: dict) -> dict:
    """Linkurile pentru numerele alese — și tot aici se scrie ce s-a prezentat.

    Nu are plafon pe câte numere primește, deși `plafon_prezentare` spune 12.
    Plafonul e o instrucțiune, nu un gard: dacă omul cere anume douăzeci de
    știri, Sky trebuie să le poată da. Ce se apără cu adevărat — fereastra de
    context — e apărat de catalog, care e singurul care crește cu ziua.
    """
    numere = argumente.get("numere")
    if isinstance(numere, (int, str)):
        numere = [numere]
    if not isinstance(numere, list):
        numere = []
    return await run_in_threadpool(stiri.detalii, numere)


async def unealta_jurnal_scrie(argumente: dict) -> dict:
    return scrie_in_jurnal(str(argumente.get("text") or ""))


async def unealta_jurnal_citeste(argumente: dict) -> dict:
    try:
        limita = int(argumente.get("limita") or 20)
    except (TypeError, ValueError):
        limita = 20
    limita = max(1, min(limita, 200))
    intrari = citeste_jurnal()
    return {"ok": True, "total": len(intrari),
            "intrari": list(reversed(intrari))[:limita]}


async def unealta_proiecte_citeste(_argumente: dict) -> dict:
    return proiecte_lista()


async def unealta_deschide_muntele(_argumente: dict) -> dict:
    """Deschide camera MUNTELE pe punte. NU FACE NIMIC PE SERVER, și asta e ideea.

    Serverul n-are canal către pagină și n-are de ce să capete unul: puntea vede
    DEJA ce unelte s-au chemat — `/api/chat` întoarce lista în `unelte` — deci
    răspunsul ăsta e tot mesajul.

    În pagină: se iese din ENGAGE (camera e scrisă, ca la Socrate, iar acolo
    cântă un clip), se ridică fereastra DACĂ e în bară, și se deschide panoul.
    Vezi `deschideMuntele` în punte.html.

    FĂRĂ PARAMETRU, pe cifră: un enum de o singură valoare costa 109 tokeni de
    schemă la fiecare mesaj al lui Sky, fără parametru 80. Când apare
    AMFITEATRUL, se remăsoară: o a doua unealtă (~80) sau un enum comun (~115).
    """
    return {"ok": True, "camera": "munte"}


# Ce NU intră aici, deliberat:
#
#   recon_ruleaza    — costă bani și durează minute. Un model care îl poate
#                      chema singur poate porni lanțul de cinci ori într-o
#                      conversație, fără ca omul să vadă cum se adună.
#   marcheaza_citit  — ireversibil. Mutat reperul, ce era înainte nu mai apare
#                      în niciun briefing.
#
# Amândouă rămân gestul omului: butonul „Rulează" de pe punte, care mută și
# reperul. AI-ul propune, codul execută, omul decide. Dacă vreodată pare firesc să fie adăugate aici, întrebarea
# de pus nu e „merge?", ci „cine plătește dacă modelul se înșală?".
#
# `comms_raport` MUTĂ REPERUL, deci și el scrie, și seamănă periculos de mult cu
# `marcheaza_citit` de mai sus. Diferența care-l lasă să intre: acolo briefingul e
# singura suprafață peste baza agentului, deci ce sare de fereastră nu mai vede
# nimeni niciodată. Aici mailul rămâne în Gmail, iar fiecare raport se scrie în
# date\comms.jsonl. Mutarea nu ascunde nimic, doar nu repetă — și există `zile`,
# care citește înapoi fără să miște nimic.
#
# Criteriul pentru o unealtă care scrie nu e „scrie?", ci cât costă o greșeală
# și cine o plătește — iar pentru orice unealtă, cât costă schema ei la fiecare
# mesaj față de cât e folosită.

# Câmpul `server` spune DE CINE atârnă unealta, ca eșecul ei să poarte un nume
# în loc de „unealta nu raspunde". E singurul loc unde legătura asta e scrisă.

# DOUĂ LOCURI PENTRU TEXT, și diferența dintre ele e o factură:
#
#   `schema.function.description` se trimite la FIECARE mesaj către strat, chiar
#   și când omul întreabă ce oră e. Acolo intră numai ce ajută modelul să decidă
#   DACĂ să cheme unealta: ce întoarce, când se cheamă, garanțiile care rămân
#   adevărate și când unealta nu e chemată („NU citește mailurile în Gmail").
#
#   `instructiuni` pleacă lipit de REZULTAT, deci se plătește doar când unealta
#   a fost chemată. Acolo intră cum se prezintă ce a venit: „economicele întâi",
#   „reclamele se numără la coadă", „linkul întreg după propoziție".
#
# Textul din `instructiuni` e produs de cod și nu vine de la model, deci rămâne
# fapt, la fel ca restul rezultatului.
UNELTE = {
    # 80 de tokeni de schemă la fiecare mesaj al lui Sky. E ușa principală a
    # camerei, cerută anume („pornește Ramana"); dacă la folosire omul intră tot
    # de pe hartă, se retrage pe criteriul preț raportat la folosire.
    "deschide_muntele": {
        "straturi": ["sky"],
        # Nu atârnă de nimeni din afară: e chiar pagina care a întrebat.
        "server": SERVER_LOCAL,
        "schema": {"type": "function", "function": {
            "name": "deschide_muntele",
            "description": (
                "Deschide camera MUNTELE, a lui Ramana: „deschide muntele”, "
                "„porneste Ramana”. NU cand omul intreaba DESPRE ea."),
            "parameters": {"type": "object", "properties": {}},
        }},
        "instructiuni": ("Spune scurt ca se deschide, o propozitie. Nu descrie ce "
                         "se vede si nu promite ce va spune Ramana: omul se uita "
                         "deja la camera. Acolo se scrie, nu se vorbeste — "
                         "microfonul tace cat e deschisa."),
        "executa": unealta_deschide_muntele,
    },
    "recon_stare": {
        "straturi": ["sky"],
        "server": "agentul 051",
        "schema": {"type": "function", "function": {
            "name": "recon_stare",
            "description": ("Starea pipeline-ului agentului 051 de recunoaștere: "
                            "câte mailuri au fost citite, câte anunțuri extrase, "
                            "câte au trecut filtrele."),
            "parameters": {"type": "object", "properties": {}},
        }},
        "executa": unealta_recon_stare,
    },
    "recon_briefing": {
        "straturi": ["sky"],
        "server": "agentul 051",
        "schema": {"type": "function", "function": {
            "name": "recon_briefing",
            "description": ("Ce e nou la agentul 051 de la ultima citire: anunțuri "
                            "noi, evaluate și filtrate. Doar citește — nu avansează "
                            "reperul. Cum se citește rezultatul vine în răspuns."),
            "parameters": {"type": "object", "properties": {}},
        }},
        "instructiuni": ("„Nou” înseamnă „s-a aflat ceva”, nu „a sosit”: un anunț "
                         "vechi reapare dacă abia acum a primit verdict, și nu e o "
                         "repetiție. `in_asteptare` spune cât lucru nefăcut a rămas "
                         "în toată baza — dacă e mare, lista e provizorie și se "
                         "spune."),
        "executa": unealta_recon_briefing,
    },
    "comms_raport": {
        "straturi": ["sky"],
        # De Gmail atârnă, nu de noi: fără internet sau cu parola de aplicație
        # revocată, unealta pică — și trebuie să spună pe cine să repari.
        "server": "Gmail",
        "schema": {"type": "function", "function": {
            "name": "comms_raport",
            "description": (
                "Mailurile necitite din inbox, de la raportul anterior încoace, "
                "grupate pe expeditor. Se cheamă când omul întreabă ce mailuri "
                "are sau ce e nou pe mail. NU citește mailurile în Gmail — "
                "rămân necitite acolo. Cum se scrie raportul vine în răspuns."),
            "parameters": {"type": "object", "properties": {
                "zile": {"type": "integer",
                         "description": ("Câte zile în urmă, când omul cere explicit "
                                         "o fereastră sau vrea raportul din nou. "
                                         "Lipsă = de la raportul anterior.")},
            }},
        }},
        "instructiuni": (
            "Cum se răspunde: câte sunt și de la cine, apoi o singură propoziție "
            "despre fiecare, scrisă din `subiect` și `extras`. Reclamele și "
            "ofertele comerciale NU primesc propoziție proprie: se numără la "
            "sfârșit, cu numele expeditorilor — „plus 3 reclame: eMAG, ALTEX”. "
            "Un mail de la un magazin care nu e reclamă (o factură, o confirmare "
            "de plată) e mail normal, nu reclamă. `necitite_mai_vechi` se spune "
            "ca un număr, la coadă, dacă e mai mare ca zero."),
        "executa": unealta_comms_raport,
    },
    # DOUĂ unelte pentru o încăpere, deși verdictul e „o singură unealtă, nu una
    # de citit plus una de scris". Aici fac lucruri diferite, ca la RECON: o
    # singură unealtă în două trepte (`numere` lipsă = catalog) ar economisi 65
    # de tokeni, dar ar pune în mâna modelului un comutator de mod și un catalog
    # din care poate răspunde fără să ceară linkuri.
    "sensors_catalog": {
        "straturi": ["sky"],
        # De site-uri atârnă, nu de noi: fără internet, sau cu un flux mutat,
        # unealta pică — și trebuie să spună pe cine să repari.
        "server": "site-urile de știri",
        "schema": {"type": "function", "function": {
            "name": "sensors_catalog",
            "description": (
                "Titlurile știrilor apărute de la ultimul raport, de la Digi24, "
                "Economedia și CNN. Se cheamă când omul întreabă ce știri sunt "
                "sau ce e nou în lume. Doar titluri numerotate, fără linkuri: "
                "linkurile vin din `sensors_detalii`, deci NU răspunzi din "
                "catalog. Ce faci cu ele vine în răspuns."),
            "parameters": {"type": "object", "properties": {
                "ore": {"type": "integer",
                        "description": ("Câte ore în urmă, când omul cere explicit "
                                        "o fereastră. Lipsă = ultimele 24 de ore.")},
            }},
        }},
        # „NU răspunzi din catalog" rămâne ȘI în schemă, scurt. Aia nu e
        # instrucțiune de prezentare: e ce oprește un răspuns inventat din
        # titluri, iar decizia de a nu răspunde se ia înainte de al doilea apel.
        "instructiuni": (
            "Titlurile sunt marcate E (economic) sau G (restul). Alegi cele mai "
            "importante, cel mult câte spune `plafon_prezentare`, ECONOMICELE "
            "ÎNTÂI, apoi ceri `sensors_detalii` cu numerele lor. Fără al doilea "
            "apel n-ai linkuri, deci NU răspunzi din catalog. Două titluri "
            "despre același eveniment se aleg o dată."),
        "executa": unealta_sensors_catalog,
    },
    "sensors_detalii": {
        "straturi": ["sky"],
        "server": "serverul JA.S.Mine",
        "schema": {"type": "function", "function": {
            "name": "sensors_detalii",
            "description": (
                "Linkul, sursa, ora și paragraful pentru știrile alese din "
                "catalog. Se cheamă O SINGURĂ dată, cu toate numerele deodată. "
                "Ce ceri aici se marchează drept spus și NU mai apare în "
                "catalogul următor. Cum răspunzi vine în răspuns."),
            "parameters": {"type": "object", "properties": {
                "numere": {"type": "array", "items": {"type": "integer"},
                           "description": "Numerele știrilor alese din catalog."},
            }, "required": ["numere"]},
        }},
        "instructiuni": (
            "Cum răspunzi: economicele întâi, o propoziție pe știre, scrisă din "
            "`titlu` și `extras`, cu LINKUL ÎNTREG după ea — omul dă click pe el. "
            "Știrile cu `limba: en` (CNN) NU se traduc: titlul rămâne în engleză. "
            "Două știri despre același eveniment primesc o singură propoziție, cu "
            "ambele linkuri. Ce n-ai ales se spune ca număr la coadă, niciodată "
            "ca listă."),
        "executa": unealta_sensors_detalii,
    },
    "jurnal_scrie": {
        "straturi": ["sky"],
        "server": "serverul JA.S.Mine",
        "schema": {"type": "function", "function": {
            "name": "jurnal_scrie",
            "description": ("Consemnează un fapt în jurnalul de bord. Data o pune "
                            "codul."),
            "parameters": {"type": "object", "properties": {
                "text": {"type": "string",
                         "description": "Faptul de consemnat, o frază."},
            }, "required": ["text"]},
        }},
        "executa": unealta_jurnal_scrie,
    },
    "jurnal_citeste": {
        "straturi": ["sky"],
        "server": "serverul JA.S.Mine",
        "schema": {"type": "function", "function": {
            "name": "jurnal_citeste",
            "description": ("Ce s-a consemnat în jurnalul de bord, cele mai recente "
                            "întâi."),
            "parameters": {"type": "object", "properties": {
                "limita": {"type": "integer",
                           "description": "Câte intrări, cele mai recente. Implicit 20."},
            }},
        }},
        "executa": unealta_jurnal_citeste,
    },
    "proiecte_citeste": {
        "straturi": ["sky"],
        "server": "serverul JA.S.Mine",
        "schema": {"type": "function", "function": {
            "name": "proiecte_citeste",
            "description": "Proiectele în lucru și starea lor.",
            "parameters": {"type": "object", "properties": {}},
        }},
        "executa": unealta_proiecte_citeste,
    },
}

def unelte_pentru(strat: str) -> dict:
    """Uneltele pe care le poate folosi un strat anume.

    ATENȚIE: cheile din `straturi` sunt CHEI DE STRAT din straturi.json, nu id-uri
    de model. Pentru Sky cele două coincid, ceea ce face greșeala invizibilă: dacă
    stratul s-ar redenumi, uneltele lui ar dispărea în tăcere. Când adaugi o
    unealtă, scrie aici numele STRATULUI.

    Fiecare unealtă își declară straturile: o unealtă nouă e o singură intrare
    care spune tot despre ea. Un strat necunoscut nu apare în lista niciunei
    unelte, deci primește o listă goală.

    Fără filtrul ăsta, Socrate ar primi toate schemele la fiecare apel, pentru
    unelte pe care nu le cheamă niciodată.
    """
    return {n: u for n, u in UNELTE.items() if strat in u.get("straturi", [])}


# Câte tururi de unelte acceptăm într-o singură cerere. Fiecare tur e un apel
# la model plătit, cu schemele retrimise. Fără plafon, un model încăpățânat ar
# cicla pe banii omului.
MAX_PASI_UNELTE = 5


def aduna_consum(total: dict, parte: dict | None) -> None:
    """Adună consumul unui apel peste total.

    O tură de chat înseamnă mai multe apeluri la model: unul pentru fiecare
    rundă de unelte, plus cel final. Cifra întoarsă e SUMA lor, nu consumul
    ultimului apel — altfel contorul ar minți exact acolo unde uneltele costă
    cel mai mult, fiindcă schemele se retrimit la fiecare pas.
    """
    for cheie, valoare in (parte or {}).items():
        if isinstance(valoare, int):
            total[cheie] = total.get(cheie, 0) + valoare


# Cele două numiri ale aceleiași cifre. Open WebUI vorbește cu
# `api.anthropic.com/v1` prin stratul compatibil OpenAI, deci ce ajunge aici e
# `prompt_tokens`/`completion_tokens`. A doua pereche e forma nativă Anthropic:
# scrisă aici, o schimbare de strat nu mai face contorul să tacă în liniște.
NUMIRI_CONSUM = {"intrare": ("prompt_tokens", "input_tokens"),
                 "iesire": ("completion_tokens", "output_tokens")}


def desparte_consum(consum: dict) -> dict:
    """Intrarea și ieșirea din `usage`, dacă API-ul le-a spus.

    Cheia LIPSEȘTE din rezultat când n-a venit — nu se scrie zero. Un zero ar
    arăta identic cu „n-a costat nimic la intrare", iar intrarea și ieșirea au
    prețuri diferite: un total singur nu spune nimic despre bani.
    """
    gasit = {}
    for nume, chei in NUMIRI_CONSUM.items():
        for cheie in chei:
            if isinstance(consum.get(cheie), int):
                gasit[nume] = consum[cheie]
                break
    # Lipsa nu are voie să fie tăcută: singurul semn că stratul compatibil a
    # schimbat numirile ar fi absența a două câmpuri dintr-un fișier pe care nu-l
    # deschide nimeni. Un singur rând, la prima tură: un avertisment repetat la
    # fiecare replică e zgomot, nu semnal.
    global CONSUM_FARA_DESPARTIRE
    if len(gasit) < 2 and consum and not CONSUM_FARA_DESPARTIRE:
        CONSUM_FARA_DESPARTIRE = True
        print(f"[consum] usage fara despartire intrare/iesire; chei primite: "
              f"{sorted(consum)}. Vezi NUMIRI_CONSUM.", flush=True)
    return gasit


CONSUM_FARA_DESPARTIRE = False


async def executa_unealta(cerere_unealta: dict, permise: dict,
                          strat: str) -> tuple[dict, dict]:
    """Rulează o unealtă cerută de model. Nu ridică niciodată excepție.

    Întoarce (mesajul role:"tool" pentru istoric, însemnarea pentru punte).
    Eșecul se întoarce ca REZULTAT, nu ca excepție: modelul trebuie să poată
    spune omului „agentul nu răspunde" în loc să pice toată cererea.

    `permise` sunt uneltele stratului curent. Verificarea se repetă aici, deși
    schemele au fost deja filtrate: un model poate cere un nume pe care nu i
    l-am dat — ghicit, sau rămas într-un istoric început cu alt strat. Filtrare
    doar la scheme ar fi cosmetică; poarta trebuie să fie și la intrare.
    """
    functie = cerere_unealta.get("function") or {}
    nume = functie.get("name") or ""
    brut = functie.get("arguments")
    try:
        argumente = json.loads(brut) if isinstance(brut, str) and brut.strip() else {}
    except json.JSONDecodeError:
        argumente = {}
    unealta = permise.get(nume)
    if unealta is None:
        # Două motive diferite, două mesaje diferite: „nu există" trimite la o
        # greșeală de nume, „nu ți-e permisă" la o greșeală de configurare.
        rezultat = ({"ok": False,
                     "eroare": f"unealta nepermisa pentru stratul {strat}: {nume}"}
                    if nume in UNELTE else
                    {"ok": False, "eroare": f"unealta necunoscuta: {nume}"})
    else:
        try:
            rezultat = await unealta["executa"](argumente)
        except Exception as ex:
            # EROAREA ÎȘI NUMEȘTE SERVERUL. Un model formulează ce a primit:
            # dacă textul erorii n-are nume, numele îl pune el — și poate trimite
            # omul să pornească serverul JA.S.Mine când cauza e agentul 051.
            cine = unealta.get("server") or SERVER_LOCAL
            # „nu raspunde" e adevărat despre o dependență din alt proces.
            # Despre serverul ăsta ar fi absurd: el tocmai răspunde, altfel
            # mesajul n-ar exista.
            rezultat = {"ok": False,
                        "eroare": (f"{cine} nu a putut executa unealta"
                                   if cine == SERVER_LOCAL
                                   else f"{cine} nu raspunde"),
                        "server": cine,
                        "detaliu": descrie_exceptia(ex)}
        # Instrucțiunea de prezentare călătorește cu REZULTATUL, nu cu schema:
        # schema se plătește la fiecare mesaj, rezultatul doar când unealta e
        # chemată. Vezi comentariul de deasupra UNELTE.
        #
        # Numai pe reușită: un eșec n-are ce prezenta.
        instructiuni = unealta.get("instructiuni")
        if instructiuni and isinstance(rezultat, dict) and rezultat.get("ok", True):
            rezultat["cum_raspunzi"] = instructiuni
    mesaj = {"role": "tool", "tool_call_id": cerere_unealta.get("id", ""),
             "name": nume,
             "content": json.dumps(rezultat, ensure_ascii=False)}
    return mesaj, {"nume": nume, "argumente": argumente,
                   "reusit": bool(rezultat.get("ok", True))}


@app.post("/api/chat")
async def chat(cerere: CerereChat):
    """Citește firul de pe disc, adaugă replica nouă, cheamă modelul, salvează.

    Serverul e sursa istoricului, nu clientul. Clientul aduce o replică și
    primește înapoi firul întreg, așa cum arată pe disc — cu tot cu cererile
    de unealtă și rezultatele lor.

    Consumul se întoarce mereu. Nu se estimează: e cifra API-ului, adunată
    peste toate apelurile din tură.
    """
    url = os.environ["OPENWEBUI_URL"].rstrip("/") + "/api/chat/completions"
    cheie = os.environ["OPENWEBUI_KEY"]

    # Stratul se traduce în model AICI, o singură dată. Un strat necunoscut se
    # refuză pe loc: pasat mai departe, ar eșua la Open WebUI cu „modelul nu
    # raspunde", departe de cauză.
    descriere = STRATURI.get(cerere.strat)
    if descriere is None:
        return JSONResponse({
            "ok": False,
            "eroare": f"strat necunoscut: {cerere.strat}; "
                      f"cunoscute: {', '.join(sorted(STRATURI))}",
            "straturi": sorted(STRATURI),
        }, status_code=400)
    model = descriere["model"]

    # Uneltele se dau pe strat, nu tuturor: Socrate nu cheamă niciuna, deci nu
    # are de ce să plătească schemele lor la fiecare apel.
    permise = unelte_pentru(cerere.strat)
    scheme = [u["schema"] for u in permise.values()]

    # Id-ul și firul se hotărăsc înainte de model: un fir care nu se poate
    # salva n-are voie să coste un apel. Fișier lipsă înseamnă conversație nouă;
    # fișier care există dar nu se citește se refuză — tratat ca fir nou, ar fi
    # suprascris la salvare.
    if cerere.id:
        id_conv = cerere.id
        try:
            cale_conv(id_conv)
        except ValueError:
            return JSONResponse({"ok": False, "eroare": f"id de conversatie invalid: {id_conv!r}",
                                 "server": "serverul JA.S.Mine"}, status_code=400)
    else:
        id_conv = id_conv_nou()
    try:
        try:
            d = citeste_conv(id_conv)
        except FileNotFoundError:
            d = {"titlu": "", "strat": cerere.strat,
                 "cand": datetime.now().isoformat(timespec="seconds"),
                 "tokeni": 0, "mesaje": []}
        except Exception as ex:
            return JSONResponse({
                "ok": False, "eroare": "conversatia exista dar nu se poate citi; "
                                       "n-am trimis nimic si n-am scris nimic",
                "server": "serverul JA.S.Mine", "fisier": str(cale_conv(id_conv)),
                "detaliu": f"{type(ex).__name__}: {ex}"}, status_code=409)
        return await _chat_pe_fir(cerere, id_conv, d, model, permise, scheme, url, cheie)
    finally:
        ID_REZERVATE.discard(id_conv)


async def _chat_pe_fir(cerere, id_conv, d, model, permise, scheme, url, cheie):
    istoric = list(d.get("mesaje") or [])

    if cerere.mesaj is not None:
        noi = [{"role": "user", "content": cerere.mesaj}]
    else:
        return {"ok": False, "eroare": "cerere fara mesaj"}

    mesaje = istoric + noi
    consum = {}
    apeluri = []
    oprit_la_limita = False
    raspuns = ""
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            for _pas in range(MAX_PASI_UNELTE):
                corp = {"model": model, "messages": mesaje}
                # Câmpul lipsește de tot când stratul n-are unelte. Un „tools": []"
                # trimis degeaba tot ar putea costa antetul formatului — iar
                # economia asta e chiar rostul filtrării.
                if scheme:
                    corp["tools"] = scheme
                r = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {cheie}"},
                    json=corp,
                )
                r.raise_for_status()
                date = r.json()
                aduna_consum(consum, date.get("usage"))
                mesaj = ((date.get("choices") or [{}])[0].get("message")) or {}
                cerute = mesaj.get("tool_calls") or []
                if not cerute:
                    raspuns = mesaj.get("content") or ""
                    break
                # Mesajul assistant cu tool_calls intră în istoric ÎNAINTE de
                # rezultate: fără el, mesajele role:"tool" n-ar avea de ce să
                # atârne și modelul ar primi un istoric imposibil.
                mesaje.append({"role": "assistant",
                               "content": mesaj.get("content") or "",
                               "tool_calls": cerute})
                # Modelul poate cere mai multe unelte într-o singură tură.
                for cerere_unealta in cerute:
                    m, insemnare = await executa_unealta(cerere_unealta, permise,
                                                        cerere.strat)
                    mesaje.append(m)
                    apeluri.append(insemnare)
            else:
                # Bucla s-a consumat fără ca modelul să se oprească. Tăcerea sau
                # un răspuns pe jumătate ar fi mai rele decât o spunere directă.
                oprit_la_limita = True
                cerute_pana_aici = ", ".join(a["nume"] for a in apeluri) or "niciuna"
                raspuns = (f"M-am oprit după {MAX_PASI_UNELTE} pași de unelte, fără "
                           f"să ajung la un răspuns. Unelte chemate până aici: "
                           f"{cerute_pana_aici}. Nu continui — ar costa mai departe "
                           f"fără să se apropie de nimic.")
    # Eșecul NU se salvează. Un schimb pe jumătate ar strica istoricul.
    #
    # Trei ramuri, fiindcă sunt trei cauze pe care omul le repară în locuri
    # diferite: Docker, configurarea Open WebUI, restul.
    except httpx.RequestError as ex:
        # Nici măcar n-am ajuns la Open WebUI: container oprit, Docker oprit,
        # port greșit în .env.
        #
        # ȘI SE ÎMPARTE ÎN DOUĂ, fiindcă se repară în două feluri: cu răbdare
        # sau cu Docker. După ce serverul răspunde mai trec ~9–12 s până poate
        # răspunde Sky; un singur mesaj l-ar trimite pe om să pornească ceva
        # care tocmai pornește.
        porneste = await run_in_threadpool(docker_porneste)
        return {"ok": False,
                # Cu diacritice: textul ăsta ajunge pe ecran, ca replica lui Sky.
                "eroare": ("Motorul de conversație încă pornește — mai durează "
                           "câteva secunde." if porneste else
                           "Open WebUI nu raspunde — verifica Docker"),
                "server": "Open WebUI",
                "porneste": porneste,
                "detaliu": f"{type(ex).__name__}: {ex}"}
    except httpx.HTTPStatusError as ex:
        # Open WebUI trăiește și a răspuns, dar cu un cod de eroare: cheie
        # greșită (401), model inexistent în Workspace (404), sau modelul din
        # cloud a picat (5xx). Serverul e viu — de-asta NU spune „nu raspunde".
        return {"ok": False,
                "eroare": f"Open WebUI a raspuns cu {ex.response.status_code}",
                "server": "Open WebUI",
                "detaliu": f"model={model}; {ex.response.text[:200]}"}
    except Exception as ex:
        return {"ok": False, "eroare": "chatul a esuat",
                "server": "serverul JA.S.Mine",
                "detaliu": f"{type(ex).__name__}: {ex}"}

    # Salvarea e secundară: dacă pică, răspunsul tot ajunge la om.
    try:
        # Pe disc ajung și cererile de unealtă, și rezultatele lor. Fără ele, la
        # redeschiderea conversației modelul ar vedea un răspuns cu cifre care
        # iese din senin.
        d["mesaje"] = mesaje + [{"role": "assistant", "content": raspuns}]
        d["tokeni"] = d.get("tokeni", 0) + consum.get("total_tokens", 0)
        # Despărțirea, lângă total. Un fișier vechi fără câmpurile astea le
        # primește de la tura în care apar; o cifră reconstruită retroactiv ar
        # sta în același câmp cu una măsurată.
        for nume, valoare in desparte_consum(consum).items():
            d[nume] = d.get(nume, 0) + valoare
        # Titlul: primele cuvinte ale primei întrebări. Un titlu bun cerut
        # modelului ar costa un apel în plus la fiecare conversație nouă.
        if not d.get("titlu"):
            prima = next((m.get("content") or "" for m in d["mesaje"]
                          if m.get("role") == "user"), "")
            d["titlu"] = (prima[:60] + "…") if len(prima) > 60 else prima
        scrie_conv(id_conv, d)
    except Exception as ex:
        return {"ok": True, "id": id_conv, "raspuns": raspuns, "consum": consum,
                "unelte": apeluri,
                "avertisment": f"nu s-a salvat pe disc: {type(ex).__name__}"}

    # `mesaje` și `titlu` se întorc mereu: clientul nu-și mai ține propria copie
    # a firului, deci afișează ce a ajuns pe disc. Titlul vine tot de aici, ca
    # regula celor 60 de caractere să existe într-un singur loc.
    raspuns_final = {"ok": True, "id": id_conv, "raspuns": raspuns,
                     "consum": consum, "tokeni_conversatie": d["tokeni"],
                     "unelte": apeluri, "mesaje": d["mesaje"],
                     "titlu": d["titlu"]}
    if oprit_la_limita:
        raspuns_final["oprit_la_limita"] = MAX_PASI_UNELTE
    return raspuns_final


# ── Conversațiile pe disc ────────────────────────────────────────────
# Un fișier JSON per conversație, în date/conversatii/. Deschis cu Notepad,
# citibil. Numele fișierului e id-ul: data-ora, deci sortabil natural.
#
# De ce fișier întreg și nu JSONL ca jurnalul: jurnalul e append-only, aici
# titlul se schimbă după prima replică. Un fișier mic rescris e mai simplu
# decât un JSONL cu antet mutabil.

def cale_conv(id_conv: str) -> Path:
    """Refuză orice id care nu e strict alfanumeric-cu-cratime.

    Id-ul vine din browser. Fără verificarea asta, un id de forma
    "../../server/.env" ar scrie în afara folderului de date.
    """
    if not re.fullmatch(r"[0-9A-Za-z_-]{1,64}", id_conv):
        raise ValueError("id invalid")
    return CONVERSATII / f"{id_conv}.json"


# Id-urile date firelor noi care încă n-au ajuns pe disc: fișierul se scrie
# abia după model, deci două fire pornite în aceeași secundă nu s-ar vedea unul
# pe altul pe disc.
ID_REZERVATE: set[str] = set()


def id_conv_nou() -> str:
    baza = datetime.now().strftime("%Y%m%d-%H%M%S")
    id_conv, n = baza, 1
    while id_conv in ID_REZERVATE or cale_conv(id_conv).exists():
        n += 1
        id_conv = f"{baza}-{n}"
    ID_REZERVATE.add(id_conv)
    return id_conv


def scrie_conv(id_conv: str, date: dict) -> None:
    # Temporar lângă fișier, apoi `os.replace`: o cădere la jumătate lasă
    # fișierul vechi întreg, nu un JSON ciuntit.
    cale = cale_conv(id_conv)
    temporar = cale.with_suffix(".json.nou")
    temporar.write_text(json.dumps(date, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    os.replace(temporar, cale)


def citeste_conv(id_conv: str) -> dict:
    return json.loads(cale_conv(id_conv).read_text(encoding="utf-8"))


@app.get("/api/conversatii")
def conversatii_lista():
    """Lista conversațiilor, cele mai noi primele. Fără mesaje — doar antetul."""
    lista = []
    for f in CONVERSATII.glob("*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            antet = {
                "id": f.stem,
                "titlu": d.get("titlu", "(fără titlu)"),
                "strat": d.get("strat", ""),
                "cand": d.get("cand", ""),
                "tokeni": d.get("tokeni", 0),
                "mesaje": len(d.get("mesaje", [])),
            }
            # Despărțirea se dă doar unde există pe disc. Cu `.get(..., 0)`,
            # conversațiile vechi ar arăta zero — un contor care arată la fel
            # când nu știe și când știe că n-a fost nimic.
            for nume in ("intrare", "iesire"):
                if isinstance(d.get(nume), int):
                    antet[nume] = d[nume]
            lista.append(antet)
        except Exception:
            continue          # un fișier corupt nu ascunde restul listei
    return sorted(lista, key=lambda c: c["id"], reverse=True)


@app.get("/api/conversatii/{id_conv}")
def conversatie_citeste(id_conv: str):
    try:
        return {"ok": True, "date": citeste_conv(id_conv)}
    except Exception:
        return {"ok": False, "eroare": "conversatia nu exista"}


@app.delete("/api/conversatii/{id_conv}")
def conversatie_sterge(id_conv: str):
    """Scoate conversația din listă. NU o aruncă: o mută în `conversatii-inchise/`.

    Gestul e dintr-o singură apăsare, fără confirmare, fiindcă e reversibil:
    mutată, conversația se ia înapoi cu mâna. Un fir plătit pierdut definitiv
    dintr-un click ar cere confirmare.

    O singură operație: `os.replace` pe ACELAȘI volum e atomic, deci nu există
    clipa în care fișierul e în amândouă locurile sau în niciunul.
    """
    try:
        sursa = cale_conv(id_conv)
        tinta = CONVERSATII_INCHISE / sursa.name
        os.replace(sursa, tinta)
        return {"ok": True, "arhivat": str(tinta)}
    except Exception as ex:
        return {"ok": False, "eroare": "nu s-a putut sterge",
                "detaliu": f"{type(ex).__name__}: {ex}"}


if __name__ == "__main__":
    print(f"JA.S.Mine pornește. Rădăcina: {RADACINA}")
    print("Deschide: http://localhost:8000")
    # Nu `uvicorn.run(app, ...)`: acela construiește serverul înăuntru și nu-l
    # dă nimănui, iar ieșirea are nevoie de el ca să se poată opri curat
    # (`paznicul_iesirii`).
    SERVER_UVICORN = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=8000))
    SERVER_UVICORN.run()
