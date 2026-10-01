r"""SENSORS — știrile: ce a apărut la Digi24, Economedia și CNN de la ultimul raport.

DOUĂ TREPTE, și ăsta e tot rostul fișierului:

    catalog()  → toate titlurile din fereastră, numerotate, FĂRĂ linkuri
    detalii()  → linkul, sursa, ora și paragraful, doar pentru numerele alese

De ce așa: nu există semnal determinist pentru „important". Ori vede modelul tot
și alege, ori tăietura o face codul și „selecția" e o vorbă. Măsurat pe cutia
asta: o cotă de 9 știri generale, cele mai recente, acoperea **ultimele 45 de
minute din 24 de ore** — Digi24 publică ~70 pe zi. Deci modelul vede tot.
Se poate plăti fiindcă **catalogul nu poartă linkuri**: un URL românesc e titlul
întreg scris cu cratime, ~45 de tokeni bucata, și e partea scumpă a unei știri.
Se plătesc doar cele care ajung pe ecran.

CE ÎNSEAMNĂ „DAT DEJA": ce a PREZENTAT modelul, adică numerele cerute prin
`detalii()` — nu ce a citit codul. O știre din catalog pe care modelul n-a
ales-o rămâne în catalogul următor cât timp e în fereastră. Altfel raportul de
dimineață ar consuma 100 de știri ca să spună 12, iar restul ar dispărea fără să
le vadă nimeni.

Nu are FastAPI înăuntru, deliberat: se poate rula direct, la probe, fără server.
    py server\stiri.py            (catalogul, fereastra implicită)
    py server\stiri.py 6          (ultimele 6 ore)
    py server\stiri.py 24 panou   (ce ar arăta panoul: tot, cu linkuri)
"""

import html
import json
import re
import socket
import ssl
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path

RADACINA = Path(__file__).resolve().parent.parent
CALE_CONFIG = Path(__file__).resolve().parent / "stiri.json"
CALE_CATALOG = RADACINA / "date" / "sensors-catalog.json"
CALE_PREZENTATE = RADACINA / "date" / "sensors.json"
CALE_JURNAL = RADACINA / "date" / "sensors.jsonl"

TIMEOUT_SECUNDE = 15

# Cât ține un catalog. Peste asta, numerele din el nu mai înseamnă nimic sigur:
# între timp au apărut știri noi și numerotarea s-ar fi schimbat. Nu se ghicește
# — se spune, ca modelul să ceară catalogul din nou.
MINUTE_VALABILITATE_CATALOG = 30

# Câte linkuri prezentate se țin minte, și cât. Plafon dublu, ca fișierul să nu
# crească la nesfârșit: numărul apără de o zi cu multe rapoarte, zilele apără de
# un link care ar rămâne blocat pentru totdeauna.
MAX_PREZENTATE = 400
ZILE_PREZENTATE = 7

UA = "JA.S.Mine/1.0 (cititor personal de fluxuri)"

# Ce se folosește dacă `stiri.json` lipsește sau e stricat. NU e o copie de
# rezervă a configurației reale — e minimul care ține raportul în viață, și
# faptul că s-a căzut pe el se SPUNE în răspuns. Un fișier prost scris care s-ar
# aplica tăcut e mai rău decât unul care se plânge.
CONFIG_IMPLICIT = {
    "ore_implicit": 24,
    "plafon_prezentare": 12,
    "max_catalog": 150,
    "max_panou": 40,
    "lungime_extras": 180,
    "cai_economice": ["economie", "business", "economy", "markets"],
    "cai_excluse": [],
    "surse": [],
}


# =============================================================
# Funcții pure — primesc date, întorc date. Nu ating rețeaua.
# =============================================================


class _ExtragatorText(HTMLParser):
    """Scoate din HTML doar ce ar citi un om cu ochii.

    Portat din `mail.py`, unde a fost scris pentru mailurile-pagină-web ale
    magazinelor. Aici e nevoie de el fiindcă descrierile din RSS vin cu `<p>`,
    `<a>` și uneori cu un `<style>` întreg în față.
    """

    ETICHETE_DE_IGNORAT = {"script", "style", "head", "title"}
    ETICHETE_BLOC = {
        "p", "div", "br", "tr", "td", "th", "li", "ul", "ol",
        "h1", "h2", "h3", "h4", "h5", "h6", "table", "section", "article",
    }

    def __init__(self):
        super().__init__()
        self._bucati = []
        self._in_zona_ignorata = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.ETICHETE_DE_IGNORAT:
            self._in_zona_ignorata += 1
        elif tag in self.ETICHETE_BLOC:
            self._bucati.append(" ")

    def handle_endtag(self, tag):
        if tag in self.ETICHETE_DE_IGNORAT and self._in_zona_ignorata > 0:
            self._in_zona_ignorata -= 1
        elif tag in self.ETICHETE_BLOC:
            self._bucati.append(" ")

    def handle_data(self, data):
        if self._in_zona_ignorata == 0:
            self._bucati.append(data)

    def text(self) -> str:
        return "".join(self._bucati)


def curata_textul(brut: str) -> str:
    """HTML → o singură linie de text citibil, fără entități și fără spații duble.

    O singură linie, spre deosebire de `mail.py`: în catalog fiecare știre e un
    rând, iar un `\\n` venit dintr-o descriere ar rupe numerotarea.
    """
    if not brut:
        return ""
    extragator = _ExtragatorText()
    try:
        extragator.feed(brut)
        text = extragator.text()
    except Exception:
        # HTML stricat n-are voie să oprească raportul. Cădem pe o curățare
        # brutală, care nu poate eșua.
        text = re.sub(r"<[^>]+>", " ", brut)
    # `\xa0` e spațiul care nu rupe rândul: arată ca un spațiu, dar e alt
    # caracter, și strică orice comparație de text.
    return " ".join(html.unescape(text).replace("\xa0", " ").split())


def canonic(link: str) -> str:
    """Forma pe care o comparăm ca să știm că două linkuri sunt aceeași știre.

    Se taie parametrii de urmărire (`?utm_source=...`), ancora, `www.` și bara
    finală. Fără asta, aceeași știre venită prin două fluxuri ar apărea de două
    ori, iar una deja prezentată s-ar întoarce a doua zi cu alt `?`.
    """
    fara = (link or "").split("?")[0].split("#")[0].strip().rstrip("/")
    return fara.replace("://www.", "://").lower()


def e_economica(link: str, categoria_sursei: str, cai_economice: list) -> bool:
    """Categoria se citește din DOUĂ semne, amândouă deterministe.

    Sursa și-o declară în configurație (Economedia e economică toată), iar calea
    URL o poate ridica (`/business/` la CNN, `/stiri/economie/` la Digi24).
    Modelul nu judecă: codul dă faptele.
    """
    if categoria_sursei == "economie":
        return True
    cale = (link or "").lower()
    return any("/" + bucata.strip("/") + "/" in cale for bucata in cai_economice)


def e_exclusa(link: str, cai_excluse: list) -> bool:
    cale = (link or "").lower()
    return any(bucata.lower() in cale for bucata in cai_excluse)


def formateaza_sumar(raport: dict, pentru_model: bool = False) -> str:
    """Un rând despre raport, plus ce s-a stricat pe drum, în paranteză.

    Două variante, fiindcă sunt doi cititori. Modelului i se spune ce are de
    făcut mai departe; omului care se uită la panou, nu — el vede lista întreagă
    și alege cu ochii. Un panou care scrie „cere `sensors_detalii`" ar fi o
    instrucțiune scăpată pe ecranul greșit.

    Sursele care au mers nu se numesc: pentru cine citește, „a mers" e tăcerea.
    Se numesc doar cele picate și cele vechi, fiindcă fiecare se repară în altă
    parte — iar dacă textul erorii n-are nume, numele îl inventează modelul.
    """
    if not raport["total"]:
        randuri = [f"Nicio știre nouă în {raport['fereastra']}."]
    elif pentru_model:
        randuri = [f"{raport['total']} știri noi în {raport['fereastra']} "
                   f"(E = economic, G = restul). Alege cel mult "
                   f"{raport['plafon_prezentare']}, economicele întâi, și cere "
                   f"`sensors_detalii` cu numerele lor."]
    else:
        randuri = [f"{raport['total']} știri în {raport['fereastra']}."]

    coada = []
    if raport["deja_prezentate"]:
        coada.append(f"{raport['deja_prezentate']} "
                     + ("deja spuse mai devreme" if pentru_model
                        else "deja spuse de Sky, scrise stins"))
    if raport["taiate"]:
        coada.append(f"{raport['taiate']} "
                     + ("tăiate de plafonul catalogului" if pentru_model
                        else "nearătate aici"))
    for sursa in raport["surse"]:
        if not sursa["ok"]:
            coada.append(f"{sursa['nume']}: {sursa['eroare']}")
        elif sursa.get("prea_veche"):
            coada.append(f"{sursa['nume']}: nimic în fereastră, "
                         f"cea mai nouă știre e {sursa['prea_veche']}")
    coada += raport.get("avertismente", [])

    if coada:
        randuri.append("(" + " · ".join(coada) + ")")
    return "\n".join(randuri)


def formateaza_catalog(raport: dict, stiri: list) -> str:
    """Sumarul, plus câte un rând numerotat per știre. Asta citește modelul.

    Text, nu JSON, și nu din economie de dragul economiei: măsurat pe 107 știri,
    aceleași titluri costă 7.531 de tokeni în JSON și 4.305 aici. Diferența sunt
    cheile, repetate de 107 ori.

    ATENȚIE dacă cineva vrea să adauge aici structura numerotată, „ca s-o aibă
    și modelul": măsurat, rezultatul uneltei sare de la 4.400 la 10.764 de
    tokeni. Aceleași titluri, de două ori, o dată ca text și o dată ca JSON.
    Numerele trăiesc în text; legătura lor cu linkurile trăiește pe disc.
    """
    randuri = [formateaza_sumar(raport, pentru_model=True)]
    for s in stiri:
        eticheta = "E" if s["categorie"] == "economie" else "G"
        randuri.append(f"{s['nr']} {eticheta} {s['titlu']}")
    return "\n".join(randuri)


def formateaza_detalii(alese: list, lipsa: list) -> str:
    bucati = [f"{len(alese)} știri cu link."]
    if lipsa:
        bucati.append("Numere care nu există în catalog: "
                      + ", ".join(str(n) for n in lipsa) + ".")
    return " ".join(bucati)


# =============================================================
# Configurația: reglaj CALD, citit la fiecare raport.
# =============================================================


def citeste_configuratia() -> tuple[dict, list]:
    """`stiri.json`, cu tot ce lipsește completat din `CONFIG_IMPLICIT`.

    Se citește la FIECARE raport, nu la pornirea serverului: un site adăugat în
    fișier trebuie să apară fără repornire. Asta e tot rostul fișierului.

    `utf-8-sig`, nu `utf-8`: PowerShell 5.1 pune BOM la începutul fișierelor pe
    care le scrie, iar un BOM citit ca text a mai oprit o dată serverul.

    Întoarce și avertismentele — ce s-a stricat se SPUNE, nu se aplică tăcut.
    """
    avertismente = []
    config = dict(CONFIG_IMPLICIT)
    try:
        pe_disc = json.loads(CALE_CONFIG.read_text(encoding="utf-8-sig"))
        if not isinstance(pe_disc, dict):
            raise ValueError("fișierul nu conține un obiect JSON")
        config.update({k: v for k, v in pe_disc.items() if not k.startswith("_")})
    except FileNotFoundError:
        avertismente.append(f"{CALE_CONFIG.name} lipsește; nicio sursă configurată")
    except Exception as ex:
        avertismente.append(f"{CALE_CONFIG.name} nu se poate citi ({type(ex).__name__}); "
                            f"merg pe configurația implicită")

    surse = []
    for sursa in config.get("surse") or []:
        if not isinstance(sursa, dict):
            continue
        nume = str(sursa.get("nume") or "").strip()
        url = str(sursa.get("url") or "").strip()
        fel = str(sursa.get("fel") or "rss").strip()
        if not nume or not url:
            avertismente.append("o sursă fără nume sau fără url, sărită")
            continue
        if fel not in ("rss", "sitemap-news"):
            avertismente.append(f"{nume}: formatul „{fel}” nu e cunoscut, sărită")
            continue
        surse.append({
            "nume": nume,
            "url": url,
            "fel": fel,
            "categorie": str(sursa.get("categorie") or "general"),
            "limba": str(sursa.get("limba") or "ro"),
            "max": int(sursa.get("max") or 60),
        })
    config["surse"] = surse
    if not surse and not avertismente:
        avertismente.append("nicio sursă în configurație")
    return config, avertismente


# =============================================================
# Starea pe disc: ce s-a prezentat, ultimul catalog, jurnalul.
# =============================================================


def citeste_prezentate() -> dict:
    """`{link canonic: momentul în care Sky l-a spus}`.

    Fișier lipsă sau stricat → dicționar gol, nu eroare. Tiparul e al lui
    `citeste_reper` din `mail.py`: o citire care pică întoarce instantaneul, nu
    oprește funcția. Consecința e că se repetă un raport, nu că nu mai există.
    """
    try:
        date = json.loads(CALE_PREZENTATE.read_text(encoding="utf-8-sig"))
        return {str(k): str(v) for k, v in (date.get("prezentate") or {}).items()}
    except Exception:
        return {}


def scrie_prezentate(prezentate: dict) -> None:
    limita = (datetime.now(timezone.utc) - timedelta(days=ZILE_PREZENTATE)).isoformat()
    proaspete = {k: v for k, v in prezentate.items() if v >= limita}
    # Cele mai noi întâi, ca tăierea la MAX_PREZENTATE să scoată vechimea.
    ordonate = sorted(proaspete.items(), key=lambda kv: kv[1], reverse=True)
    pastrate = dict(ordonate[:MAX_PREZENTATE])
    try:
        CALE_PREZENTATE.parent.mkdir(parents=True, exist_ok=True)
        CALE_PREZENTATE.write_text(
            json.dumps({"prezentate": pastrate}, ensure_ascii=False, indent=1),
            encoding="utf-8")
    except Exception:
        # Evidența n-are voie să omoare fapta. Prețul e că știrile spuse acum
        # se pot repeta o dată — nu că raportul nu mai iese.
        pass


def scrie_catalog(raport: dict, stiri: list) -> None:
    try:
        CALE_CATALOG.parent.mkdir(parents=True, exist_ok=True)
        CALE_CATALOG.write_text(json.dumps({
            "moment": raport["moment"],
            "fereastra": raport["fereastra"],
            "stiri": stiri,
        }, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def citeste_catalog() -> dict | None:
    try:
        return json.loads(CALE_CATALOG.read_text(encoding="utf-8-sig"))
    except Exception:
        return None


def scrie_in_jurnal(fel: str, continut: dict) -> None:
    """Un rând per raport. Dacă nu se poate scrie, raportul se dă oricum."""
    try:
        CALE_JURNAL.parent.mkdir(parents=True, exist_ok=True)
        rand = {"moment": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
                "fel": fel}
        rand.update(continut)
        with CALE_JURNAL.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rand, ensure_ascii=False) + "\n")
    except Exception:
        pass


# =============================================================
# Funcții care ating rețeaua.
# =============================================================


def _adu(url: str) -> bytes:
    cerere = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(cerere, timeout=TIMEOUT_SECUNDE) as raspuns:
        return raspuns.read()


def _traduce_eroarea(ex: Exception) -> str:
    """Numele defectului, în limba omului care trebuie să-l repare."""
    if isinstance(ex, urllib.error.HTTPError):
        return f"site-ul a răspuns {ex.code}"
    if isinstance(ex, (socket.timeout, TimeoutError)):
        return f"nu a răspuns în {TIMEOUT_SECUNDE} secunde"
    if isinstance(ex, ssl.SSLError):
        return "conexiunea securizată a eșuat"
    # `gaierror` ajunge aici împachetat în `URLError`, deci se caută în `reason`,
    # nu în tipul de la suprafață. Fără asta, în raport ieșea „[Errno 11001]
    # getaddrinfo failed" — adevărat, și nefolositor pentru cine trebuie să
    # aleagă între „pornește internetul" și „ai greșit url-ul".
    if isinstance(ex, (socket.gaierror,)) or (
            isinstance(ex, urllib.error.URLError)
            and isinstance(getattr(ex, "reason", None), socket.gaierror)):
        return "adresa nu se poate rezolva (internet căzut sau url greșit)"
    if isinstance(ex, urllib.error.URLError):
        return f"nu se poate ajunge la site ({ex.reason})"
    if isinstance(ex, ET.ParseError):
        return "răspunsul nu e XML valid"
    return f"{type(ex).__name__}"


NS_SITEMAP = {
    "s": "http://www.sitemaps.org/schemas/sitemap/0.9",
    "n": "http://www.google.com/schemas/sitemap-news/0.9",
}


def _parseaza_rss(octeti: bytes, sursa: dict, config: dict) -> list:
    radacina = ET.fromstring(octeti)
    lungime = int(config["lungime_extras"])
    iesire = []
    for item in radacina.findall(".//item"):
        ia = lambda eticheta: (item.findtext(eticheta) or "").strip()
        try:
            data = parsedate_to_datetime(ia("pubDate")).astimezone(timezone.utc)
        except Exception:
            # Fără dată nu putem spune dacă e din fereastră. O știre fără dată
            # e mai periculoasă decât una lipsă: ar sta veșnic în catalog.
            continue
        iesire.append({
            "titlu": curata_textul(ia("title")),
            "link": ia("link"),
            "data": data,
            "extras": curata_textul(ia("description"))[:lungime],
        })
    return iesire


def _parseaza_sitemap(octeti: bytes, sursa: dict, config: dict) -> list:
    radacina = ET.fromstring(octeti)
    iesire = []
    for intrare in radacina.findall("s:url", NS_SITEMAP):
        stire = intrare.find("n:news", NS_SITEMAP)
        if stire is None:
            continue
        brut = stire.findtext("n:publication_date", default="", namespaces=NS_SITEMAP)
        try:
            data = datetime.fromisoformat(brut.replace("Z", "+00:00")).astimezone(timezone.utc)
        except Exception:
            continue
        iesire.append({
            "titlu": curata_textul(stire.findtext("n:title", default="",
                                                  namespaces=NS_SITEMAP)),
            "link": intrare.findtext("s:loc", default="", namespaces=NS_SITEMAP),
            "data": data,
            # Sitemap-ul de știri nu poartă descriere. De la CNN vin titlu și
            # link; un drum în plus pe fiecare articol, doar pentru primul
            # paragraf, ar dubla latența raportului.
            "extras": "",
        })
    return iesire


def aduna_sursa(sursa: dict, config: dict, de_cand: datetime) -> dict:
    """O sursă, adusă și parsată. Nu ridică excepții — le întoarce cu nume.

    O sursă picată nu omoară raportul: se raportează separat, cu numele ei,
    fiindcă fiecare se repară în altă parte (internet, url greșit, site mutat).
    """
    rezultat = {"nume": sursa["nume"], "ok": True, "nr": 0, "eroare": None,
                "prea_veche": None}
    try:
        octeti = _adu(sursa["url"])
        brute = (_parseaza_rss if sursa["fel"] == "rss" else _parseaza_sitemap)(
            octeti, sursa, config)
    except Exception as ex:
        rezultat.update(ok=False, eroare=_traduce_eroarea(ex))
        return rezultat

    # VECHIMEA E O EROARE, NU O TĂCERE. `rss.cnn.com` răspunde 200, cu XML
    # valid, și întoarce știri din 2017 — cine l-ar fi pus în configurație
    # n-ar fi văzut nimic stricat, doar un raport tot mai sărac.
    cea_mai_noua = max((s["data"] for s in brute), default=None)

    in_fereastra = []
    for stire in brute:
        if stire["data"] < de_cand or not stire["link"] or not stire["titlu"]:
            continue
        if e_exclusa(stire["link"], config["cai_excluse"]):
            continue
        stire["sursa"] = sursa["nume"]
        stire["limba"] = sursa["limba"]
        stire["categorie"] = ("economie" if e_economica(
            stire["link"], sursa["categorie"], config["cai_economice"]) else "general")
        in_fereastra.append(stire)

    in_fereastra.sort(key=lambda s: s["data"], reverse=True)
    rezultat["stiri"] = in_fereastra[:sursa["max"]]
    rezultat["nr"] = len(rezultat["stiri"])

    if not in_fereastra and cea_mai_noua is not None:
        zile = (datetime.now(timezone.utc) - cea_mai_noua).days
        rezultat["prea_veche"] = (f"din {cea_mai_noua.astimezone():%d.%m.%Y}"
                                  if zile >= 1 else "de azi, dar în afara ferestrei")
    return rezultat


def _aduna_tot(ore: int, config: dict) -> tuple[list, list]:
    """Toate sursele, în paralel, deduplicate pe link canonic și ordonate.

    În paralel fiindcă una moartă n-are voie s-o întârzie pe cealaltă — aceeași
    regulă ca la sondele din `/api/stare`.

    Ordinea: economicele întâi, apoi restul; în fiecare grup cele mai noi
    primele; la egalitate de dată, alfabetic. Al treilea criteriu nu e cosmetic:
    fără el, două știri publicate în aceeași secundă și-ar schimba locul între
    rulări, iar catalogul ar părea că se mișcă singur.
    """
    de_cand = datetime.now(timezone.utc) - timedelta(hours=ore)
    surse = config["surse"]
    if not surse:
        return [], []
    with ThreadPoolExecutor(max_workers=max(1, len(surse))) as executor:
        rapoarte = list(executor.map(
            lambda s: aduna_sursa(s, config, de_cand), surse))

    toate = []
    for raport_sursa in rapoarte:
        toate.extend(raport_sursa.pop("stiri", []))

    vazute = set()
    unice = []
    for stire in sorted(toate, key=lambda s: (0 if s["categorie"] == "economie" else 1,
                                              -s["data"].timestamp(), s["titlu"])):
        cheie = canonic(stire["link"])
        if cheie in vazute:
            continue
        vazute.add(cheie)
        stire["cheie"] = cheie
        unice.append(stire)
    return unice, rapoarte


def _raport_gol(fereastra: str, config: dict, avertismente: list,
                rapoarte: list) -> dict:
    return {
        "moment": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "fereastra": fereastra,
        "total": 0, "deja_prezentate": 0, "taiate": 0,
        "plafon_prezentare": int(config["plafon_prezentare"]),
        "stiri": [], "surse": rapoarte, "avertismente": avertismente,
    }


def catalog(ore: int | None = None) -> dict:
    """Titlurile din fereastră pe care Sky nu le-a spus încă. FĂRĂ linkuri.

    Scrie catalogul pe disc, ca `detalii()` să știe ce înseamnă „47". NU
    marchează nimic drept prezentat: ce s-a dat înseamnă ce a spus modelul, iar
    modelul n-a spus încă nimic.

    Contractul e al lui 051 și COMMS, ca să fie unul singur în toată casa:
        {"ok": true,  "date": {"raport": {...}, "text": "..."}}
        {"ok": false, "eroare": "...", "server": "..."}
    """
    config, avertismente = citeste_configuratia()
    ore = int(ore or config["ore_implicit"])
    fereastra = f"ultimele {ore} ore" if ore != 24 else "ultimele 24 de ore"

    unice, rapoarte = _aduna_tot(ore, config)
    prezentate = citeste_prezentate()

    noi = [s for s in unice if s["cheie"] not in prezentate]
    deja = len(unice) - len(noi)

    plafon = int(config["max_catalog"])
    taiate = max(0, len(noi) - plafon)
    noi = noi[:plafon]

    raport = _raport_gol(fereastra, config, avertismente, rapoarte)
    raport.update(total=len(noi), deja_prezentate=deja, taiate=taiate)
    # Numai sursele cu ceva de spus. O sursă care a mers nu-i spune modelului
    # nimic pe care să-l poată folosi, și costă în fiecare raport.
    raport["surse"] = [s for s in rapoarte if not s["ok"] or s.get("prea_veche")]

    numerotate = [{"nr": i + 1, "categorie": s["categorie"], "titlu": s["titlu"]}
                  for i, s in enumerate(noi)]

    # Pe disc se scrie tot, cu link — asta e legătura dintre număr și știre.
    scrie_catalog(raport, [
        {"nr": i + 1, "categorie": s["categorie"], "titlu": s["titlu"],
         "link": s["link"], "cheie": s["cheie"], "sursa": s["sursa"],
         "limba": s["limba"], "extras": s["extras"],
         "data": s["data"].astimezone().isoformat(timespec="minutes")}
        for i, s in enumerate(noi)
    ])
    scrie_in_jurnal("catalog", {"fereastra": fereastra, "total": len(noi),
                                "deja_prezentate": deja})
    return {"ok": True, "date": {"raport": raport,
                                 "text": formateaza_catalog(raport, numerotate)}}


def detalii(numere: list) -> dict:
    """Linkul, sursa, ora și paragraful pentru numerele alese din catalog.

    AICI se scrie ce s-a prezentat. Marcajul stă în treapta a doua, nu în prima,
    fiindcă „dat deja" înseamnă „spus de Sky", nu „citit de cod": altfel
    raportul de dimineață ar consuma o sută de știri ca să spună douăsprezece.
    """
    pe_disc = citeste_catalog()
    if not pe_disc or not pe_disc.get("stiri"):
        return {"ok": False, "server": "serverul JA.S.Mine",
                "eroare": "Nu există niciun catalog. Cheamă întâi sensors_catalog."}

    try:
        varsta = datetime.now(timezone.utc) - datetime.fromisoformat(
            pe_disc["moment"]).astimezone(timezone.utc)
    except Exception:
        varsta = timedelta(0)
    if varsta > timedelta(minutes=MINUTE_VALABILITATE_CATALOG):
        # Numerele dintr-un catalog vechi arată la fel, dar pot însemna altceva.
        # O eroare cu nume, nu știrea greșită și nu tăcere.
        return {"ok": False, "server": "serverul JA.S.Mine",
                "eroare": (f"Catalogul are {int(varsta.total_seconds() // 60)} de "
                           f"minute și nu mai e valabil. Cere-l din nou cu "
                           f"sensors_catalog.")}

    dupa_numar = {int(s["nr"]): s for s in pe_disc["stiri"]}
    cerute, vazute = [], set()
    for numar in numere or []:
        try:
            numar = int(numar)
        except (TypeError, ValueError):
            continue
        if numar not in vazute:
            vazute.add(numar)
            cerute.append(numar)

    alese = [dupa_numar[n] for n in cerute if n in dupa_numar]
    lipsa = [n for n in cerute if n not in dupa_numar]
    if not alese:
        return {"ok": False, "server": "serverul JA.S.Mine",
                "eroare": ("Niciun număr din cele cerute nu e în catalog. "
                           "Cere catalogul din nou.")}

    # Economicele întâi și aici, ca ordinea din răspuns să nu depindă de ordinea
    # în care le-a scris modelul.
    alese.sort(key=lambda s: (0 if s["categorie"] == "economie" else 1, s["nr"]))

    prezentate = citeste_prezentate()
    acum = datetime.now(timezone.utc).isoformat()
    for stire in alese:
        prezentate[stire["cheie"]] = acum
    scrie_prezentate(prezentate)
    scrie_in_jurnal("detalii", {"numere": [s["nr"] for s in alese],
                                "linkuri": [s["link"] for s in alese]})

    curatate = [{
        "nr": s["nr"], "categorie": s["categorie"], "sursa": s["sursa"],
        "limba": s["limba"], "ora": s["data"][11:16], "titlu": s["titlu"],
        "link": s["link"], "extras": s["extras"],
    } for s in alese]
    return {"ok": True, "date": {"raport": {"stiri": curatate, "lipsa": lipsa},
                                 "text": formateaza_detalii(curatate, lipsa)}}


def panou(ore: int | None = None) -> dict:
    """Ce vede omul pe hartă: TOT ce e în fereastră, cu linkuri, pe zero tokeni.

    Spre deosebire de COMMS, panoul NU consumă nimic — nici nu marchează, nici
    nu rescrie catalogul modelului. Aici „dat deja" înseamnă „spus de Sky", iar
    panoul nu spune, doar arată. Așa panoul e și supapa: ușa prin care te uiți
    înapoi la tot, oricând, fără să ceri nimic nimănui.
    """
    config, avertismente = citeste_configuratia()
    ore = int(ore or config["ore_implicit"])
    fereastra = f"ultimele {ore} ore" if ore != 24 else "ultimele 24 de ore"

    unice, rapoarte = _aduna_tot(ore, config)
    prezentate = citeste_prezentate()

    plafon = int(config["max_panou"])
    taiate = max(0, len(unice) - plafon)

    raport = _raport_gol(fereastra, config, avertismente, rapoarte)
    raport.update(total=len(unice), taiate=taiate,
                  deja_prezentate=sum(1 for s in unice if s["cheie"] in prezentate))
    raport["stiri"] = [{
        "categorie": s["categorie"], "titlu": s["titlu"], "link": s["link"],
        "sursa": s["sursa"], "limba": s["limba"],
        "data": s["data"].astimezone().isoformat(timespec="minutes"),
        "spusa": s["cheie"] in prezentate,
    } for s in unice[:plafon]]
    return {"ok": True, "date": {"raport": raport,
                                 "text": formateaza_sumar(raport)}}


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ore_cerute = int(sys.argv[1]) if len(sys.argv) > 1 else None
    ca_panou = len(sys.argv) > 2 and sys.argv[2].startswith("panou")

    inceput = datetime.now()
    r = panou(ore_cerute) if ca_panou else catalog(ore_cerute)
    durata = (datetime.now() - inceput).total_seconds()

    if not r["ok"]:
        print("EȘEC:", r["eroare"])
    else:
        raport = r["date"]["raport"]
        if ca_panou:
            for s in raport["stiri"]:
                semn = "·" if s["spusa"] else " "
                print(f" {semn} [{'E' if s['categorie'] == 'economie' else 'G'}] "
                      f"{s['data'][11:16]} {s['sursa'][:16]:<16} {s['titlu'][:70]}")
                print(f"     {s['link']}")
            print()
        print(r["date"]["text"])
    # La catalog, `surse` poartă doar ce s-a stricat: o listă goală înseamnă că
    # au mers toate. La panou sunt toate, cu numărul lor.
    rele = r.get("date", {}).get("raport", {}).get("surse", [])
    print(f"\n  {durata:.2f} s · surse: " + (", ".join(
        f"{s['nume']} {'ok' if s['ok'] else 'PICAT'} ({s['nr']})" for s in rele)
        or "toate în regulă"))
