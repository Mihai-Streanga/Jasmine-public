r"""COMMS — raportul de mail: ce a intrat necitit de la raportul anterior.

REGULA ABSOLUTĂ a acestui fișier, preluată cuvânt cu cuvânt din agentul 051
(`ingest/gmail.py`): NU SE MODIFICĂ NIMIC ÎN GMAIL. Nu se șterge, nu se mută,
nu se marchează nimic drept citit. Doar se privește. Promisiunea se ține în
două locuri, nu într-unul: `readonly=True` la selectarea folderului și
`BODY.PEEK[]` la aducerea mesajelor. Vezi comentariile de la fiecare.

DE CE codul de citire e copiat din 051 în loc să fie importat: sunt două
programe separate, cu `.venv` separat, care trebuie să poată muri unul fără
celălalt (agenții sunt programe separate). Un import ar
lega serverul JA.S.Mine de un folder din afara depozitului, iar o restaurare pe
mașină nouă ar porni fără el, tăcut.

CE FACE 051 ȘI NU FACE FIȘIERUL ĂSTA: 051 citește eticheta `Joburi` și extrage
anunțuri. Aici se citește INBOX-ul, **fără** `Joburi` — altfel aceleași mailuri
ar fi raportate de două ori, pe două uși, cu două numere care se pot contrazice.

Nu are FastAPI înăuntru, deliberat: se poate rula direct, la probe, fără server.
    py server\mail.py
    py server\mail.py 3      (ultimele 3 zile, fără să miște reperul)
"""

import email
import email.header
import email.utils
import html
import imaplib
import json
import os
import socket
import ssl
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

from dotenv import load_dotenv

SERVER_GMAIL = "imap.gmail.com"
PORT_IMAP_SSL = 993

# Dacă serverul nu răspunde în atâtea secunde, renunțăm. Fără asta, un internet
# căzut ar lăsa un raport cerut pe voce să atârne la nesfârșit.
TIMEOUT_SECUNDE = 20

RADACINA = Path(__file__).resolve().parent.parent
CALE_REPER = RADACINA / "date" / "comms.json"
CALE_JURNAL = RADACINA / "date" / "comms.jsonl"

# Câte mailuri primesc rând propriu, cu extras din corp. Restul intră doar la
# numărătoarea pe domenii. Plafonul e o cheltuială de context: fiecare mail
# detaliat costă subiect + ~400 de caractere în fereastra lui Sky.
MAX_DETALIATE = 25

# Cât din corp se dă mai departe. Nu e „rezumatul" — e materialul din care Sky
# scrie propoziția. Codul nu rezumă: modelul formulează, codul dă faptele.
LUNGIME_EXTRAS = 400

# Cât se aduce din fiecare mail. Un newsletter HTML are sute de KB și nu ne
# trebuie decât începutul. Vezi `_adu_corpuri`.
OCTETI_CORP = 8192

# A doua încercare, pentru mailurile din care primii 8 KB n-au dat niciun text.
# NU e un plafon ales din burtă: mailurile de marketing încep cu un bloc
# `<head><style>` de zeci de KB, iar extractorul sare peste `style` — la 8 KB
# suntem încă în CSS și textul cules e gol. Măsurat pe cutia reală: la 8 KB
# ieșeau goale 3 din 9; la 64 KB, zero. Se plătește doar pentru cele goale,
# nu pentru toate.
OCTETI_CORP_MARE = 65536


# =============================================================
# Funcții pure — primesc date, întorc date. Nu ating rețeaua.
# =============================================================


def _decodifica_antet(valoare_bruta) -> str:
    """Traduce un antet de mail (Subject, From) în text normal, citibil.

    Anteturile au voie, prin standard, să conțină doar ASCII. Un subiect cu
    diacritice sau emoji ajunge codificat: `=?UTF-8?B?Sm9idXJp...?=`. Fără
    traducerea asta, în raport ar ajunge mizeria, nu subiectul.

    Un singur antet poate amesteca bucăți în codificări diferite, de aceea se
    iau pe rând și se lipesc la loc.
    """
    if not valoare_bruta:
        return ""
    bucati = []
    for continut, codificare in email.header.decode_header(valoare_bruta):
        if isinstance(continut, bytes):
            # errors="replace": un octet netraductibil devine un semn de
            # întrebare. Un subiect ciudat n-are voie să oprească raportul.
            bucati.append(continut.decode(codificare or "utf-8", errors="replace"))
        else:
            bucati.append(continut)
    return " ".join("".join(bucati).split())


def _ascunde_parola(text: str, parola: str) -> str:
    """Plasă de siguranță: parola nu iese niciodată într-un mesaj de eroare.

    Nu ne așteptăm să se întâmple. Dar un mesaj de eroare ajunge în jurnale, în
    capturi de ecran, în conversații — și de acolo nu-l mai poți retrage.
    """
    if parola and parola in text:
        return text.replace(parola, "***")
    return text


class _ExtragatorText(HTMLParser):
    """Scoate din HTML doar ce ar citi un om cu ochii.

    Mailurile de la eMAG sau ALTEX sunt pagini web: tabele, culori, butoane.
    Nouă ne trebuie textul. Folosim `html.parser`, care vine cu Python — fără
    biblioteci noi pentru un lucru atât de mic.
    """

    # Ce e înăuntrul lor e cod și stiluri, nu text pentru om.
    ETICHETE_DE_IGNORAT = {"script", "style", "head", "title"}

    # Etichete care rup rândul vizual. Fără ele, textul s-ar lipi:
    # „Ofertele noiVezi acum" în loc de două rânduri.
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
            self._bucati.append("\n")

    def handle_endtag(self, tag):
        if tag in self.ETICHETE_DE_IGNORAT and self._in_zona_ignorata > 0:
            self._in_zona_ignorata -= 1
        elif tag in self.ETICHETE_BLOC:
            self._bucati.append("\n")

    def handle_data(self, data):
        if self._in_zona_ignorata == 0:
            self._bucati.append(data)

    def text(self) -> str:
        return "".join(self._bucati)


def _curata_spatiile(text: str) -> str:
    """Face textul citibil: fără spații de prisos și fără rânduri goale."""
    randuri = []
    for rand in text.splitlines():
        # \xa0 e „spațiul care nu rupe rândul", foarte des în HTML. Arată ca un
        # spațiu, dar e alt caracter — și strică orice căutare de text.
        curat = " ".join(rand.replace("\xa0", " ").split())
        if curat:
            randuri.append(curat)
    return "\n".join(randuri)


def _html_in_text(html: str) -> str:
    extragator = _ExtragatorText()
    try:
        extragator.feed(html)
    except Exception:
        # HTML stricat (și mailurile au adesea HTML stricat) n-are voie să
        # oprească raportul. Păstrăm ce s-a apucat să culeagă.
        pass
    return _curata_spatiile(extragator.text())


def _extrage_corpul(mesaj: email.message.Message) -> str:
    """Din mail, textul pe care l-ar citi un om. Doar text — link-urile nu ne trebuie.

    Aici mă abat de la 051, care păstrează ȘI HTML-ul brut: acolo link-urile
    anunțurilor trăiesc doar în `href`. Raportul de față nu are ce face cu
    link-urile — el spune „ce e în mail", iar mailul se deschide în Gmail.

    ATENȚIE: mesajul primit aici e TRUNCHIAT la primii `OCTETI_CORP` octeți.
    O parte MIME tăiată la mijloc poate ieși pe jumătate, sau deloc. E acceptat:
    ne trebuie primele ~400 de caractere, iar dacă nu iese nimic, subiectul
    rămâne și el spune destul.
    """
    parti_text = []
    parti_html = []

    for parte in mesaj.walk():
        tip = parte.get_content_type()

        # Atașamentele nu ne interesează și pot fi mari.
        dispozitie = str(parte.get("Content-Disposition") or "")
        if "attachment" in dispozitie.lower():
            continue
        if tip not in ("text/plain", "text/html"):
            continue

        try:
            # decode=True desface codificarea de transport (base64,
            # quoted-printable). Pe un corp trunchiat poate întoarce ceva
            # parțial — sau nimic. Ambele sunt în regulă.
            octeti = parte.get_payload(decode=True)
        except Exception:
            continue
        if not octeti:
            continue

        codificare = parte.get_content_charset() or "utf-8"
        try:
            continut = octeti.decode(codificare, errors="replace")
        except LookupError:
            # Codificare exotică, pe care Python n-o cunoaște. Nu abandonăm.
            continut = octeti.decode("utf-8", errors="replace")

        (parti_text if tip == "text/plain" else parti_html).append(continut)

    if parti_text:
        # `html.unescape` și pe ramura de text simplu: mailurile de la eMAG
        # trimit `Verific&#259;-&#539;i` chiar în partea `text/plain`. Pe ramura
        # HTML traducerea o face deja HTMLParser (`convert_charrefs`), dar aici
        # n-o face nimeni — și fără ea, în raport ar ajunge entitățile brute.
        return html.unescape(_curata_spatiile("\n".join(parti_text)))
    if parti_html:
        return _html_in_text("\n".join(parti_html))
    return ""


def _data_in_iso(valoare_bruta: str) -> str:
    """Data mailului în ISO 8601. Se sortează corect chiar și ca șir de caractere."""
    if not valoare_bruta:
        return ""
    try:
        momentul = email.utils.parsedate_to_datetime(valoare_bruta)
    except (TypeError, ValueError):
        # Antet `Date` stricat. Păstrăm valoarea brută — mai bine ceva
        # imperfect decât nimic. De-asta fereastra se taie pe UID, nu pe dată.
        return valoare_bruta
    return valoare_bruta if momentul is None else momentul.isoformat()


def domeniu_din(expeditor: str) -> str:
    """Domeniul expeditorului: `ALTEX <newsletter@news.altex.ro>` → `altex.ro`.

    Cheia de grupare e domeniul, nu numele afișat: numele se schimbă de la o
    campanie la alta („eMAG", „eMAG Genius", „eMAG.ro"), domeniul nu.

    Se taie subdomeniile de expediere în masă (`news.`, `e2.`, `mail.`) prin
    păstrarea ultimelor două bucăți — altfel `news.altex.ro` și `altex.ro` ar
    ajunge două grupuri diferite pentru același expeditor. Regula greșește pe
    domeniile cu sufix compus (`ceva.co.uk` → `co.uk`), de aceea sufixele alea
    sunt scrise mai jos: n-are rost o bibliotecă de liste publice pentru un caz
    care apare o dată la o sută de mailuri.
    """
    _, adresa = email.utils.parseaddr(expeditor or "")
    if "@" not in adresa:
        return "necunoscut"
    gazda = adresa.rsplit("@", 1)[1].lower().strip(".")
    bucati = gazda.split(".")
    if len(bucati) <= 2:
        return gazda
    # Sufixe la care „ultimele două bucăți" ar da chiar sufixul, nu domeniul.
    SUFIXE_COMPUSE = {"co.uk", "com.au", "co.jp", "com.br", "co.nz", "org.uk", "gov.uk"}
    if ".".join(bucati[-2:]) in SUFIXE_COMPUSE:
        return ".".join(bucati[-3:])
    return ".".join(bucati[-2:])


def eticheta_din(expeditor: str, domeniu: str) -> str:
    """Numele de arătat pentru un grup: „LinkedIn", nu „linkedin.com".

    Numele afișat din antet, dacă există; altfel domeniul. Când e nume, se ia
    prima bucată dinaintea unei virgule sau a unei paranteze — „Anthropic, PBC"
    devine „Anthropic", fiindcă într-un rând de raport contează cine, nu forma
    juridică.
    """
    nume, _ = email.utils.parseaddr(expeditor or "")
    nume = (nume or "").strip().strip('"').strip()
    if not nume:
        return domeniu
    for separator in (",", "(", " via ", " @ "):
        if separator in nume:
            nume = nume.split(separator, 1)[0].strip()
    return nume or domeniu


def grupeaza(mailuri: list) -> list:
    """Numărătoarea pe expeditori: „3 LinkedIn, 2 eMAG, 1 Anthropic".

    Cheia e domeniul; eticheta e numele cel mai des întâlnit pentru el, ca un
    expeditor care semnează diferit de la un mail la altul să nu-și schimbe
    numele grupului după cine a venit ultimul.

    Ordinea: descrescător după număr, apoi alfabetic — ca aceleași date să dea
    întotdeauna același raport. Fără al doilea criteriu, două grupuri cu același
    număr și-ar schimba locul între rulări, iar raportul ar părea că se mișcă
    singur.
    """
    strans: dict[str, dict] = {}
    for m in mailuri:
        g = strans.setdefault(m["domeniu"], {"domeniu": m["domeniu"], "nr": 0, "_nume": {}})
        g["nr"] += 1
        g["_nume"][m["eticheta"]] = g["_nume"].get(m["eticheta"], 0) + 1

    grupuri = []
    for g in strans.values():
        # Cel mai des întâlnit nume; la egalitate, cel mai scurt. Regula a doua
        # nu e cosmetică: eMAG semnează „eMAG", „eMAG Genius" și „eMAG.ro", câte
        # o dată fiecare, iar fără ea grupul s-ar numi după ordinea alfabetică.
        eticheta = min(g["_nume"].items(), key=lambda x: (-x[1], len(x[0]), x[0]))[0]
        grupuri.append({"domeniu": g["domeniu"], "eticheta": eticheta, "nr": g["nr"]})
    grupuri.sort(key=lambda g: (-g["nr"], g["eticheta"].lower()))
    return grupuri


def formateaza_text(raport: dict) -> str:
    """Aceeași informație, în 1–3 rânduri, de pus în fața unui om fără prelucrare.

    Tiparul e al briefingului lui 051: structura e sursa, textul e rezumatul.
    Ușa deterministă (panoul de pe hartă) arată textul ăsta; Sky îl primește și
    el, dar are și mailurile, deci scrie mai bine de-atât.
    """
    de_la = raport.get("de_la")
    reper = f"de la {de_la[:16].replace('T', ' ')}" if de_la else "de la început"

    if raport["nimic_nou"]:
        rand = f"Niciun mail nou {reper}."
    else:
        bucati = [f"{g['nr']} {g['eticheta']}" for g in raport["grupuri"]]
        rand = f"{raport['total']} mailuri necitite {reper}: " + ", ".join(bucati) + "."

    coada = []
    if raport["necitite_mai_vechi"]:
        coada.append(f"{raport['necitite_mai_vechi']} necitite mai vechi în inbox")
    if raport["total"] > len(raport["mailuri"]):
        coada.append(f"detaliate primele {len(raport['mailuri'])}")
    return rand + (f"\nPlus {', '.join(coada)}." if coada else "")


# =============================================================
# Starea pe disc: reperul și jurnalul rapoartelor.
# =============================================================


def citeste_reper() -> dict:
    """Unde s-a oprit raportul anterior.

    Fișier lipsă sau stricat → „de la începutul timpului", nu eroare. Tiparul e
    al lui `reglaje_la_cald` din server.py: o citire care pică întoarce
    instantaneul, nu oprește funcția. Un raport care refuză să pornească fiindcă
    un fișier de stare s-a stricat ar fi mai rău decât unul care începe de la zero.
    """
    try:
        date = json.loads(CALE_REPER.read_text(encoding="utf-8-sig"))
        return {
            "ultim_raport": date.get("ultim_raport"),
            "uid": int(date.get("uid") or 0),
            "uidvalidity": str(date.get("uidvalidity") or ""),
        }
    except Exception:
        return {"ultim_raport": None, "uid": 0, "uidvalidity": ""}


def scrie_reper(moment: str, uid: int, uidvalidity: str) -> None:
    CALE_REPER.parent.mkdir(parents=True, exist_ok=True)
    CALE_REPER.write_text(
        json.dumps({"ultim_raport": moment, "uid": uid, "uidvalidity": uidvalidity},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def scrie_in_jurnal(raport: dict) -> None:
    """Un rând per raport: momentul, fereastra, id-urile mailurilor raportate.

    ĂSTA e motivul pentru care reperul are voie să se mute singur, fără gestul
    separat pe care 051 îl cere la `marcheaza_citit`. Acolo, briefingul e
    singura suprafață peste baza agentului: ce sare de fereastră nu mai vede
    nimeni, niciodată. Aici, un raport pierdut pe drum — server picat, răspuns
    neafișat — se poate reciti de aici, iar mailul e oricum în Gmail.

    Dacă jurnalul nu se poate scrie, raportul se dă oricum. Aceeași regulă ca la
    ieșirea din punte: evidența nu are voie să omoare fapta.
    """
    try:
        CALE_JURNAL.parent.mkdir(parents=True, exist_ok=True)
        rand = {
            "moment": raport["pana_la"],
            "de_la": raport["de_la"],
            "total": raport["total"],
            "id_uri": [m["id"] for m in raport["mailuri"]],
        }
        with CALE_JURNAL.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rand, ensure_ascii=False) + "\n")
    except Exception:
        pass


# =============================================================
# Funcții care ating rețeaua.
# =============================================================


def _citeste_configuratia() -> dict:
    load_dotenv(Path(__file__).resolve().parent / ".env")

    utilizator = os.getenv("GMAIL_USER", "").strip()
    parola = os.getenv("GMAIL_APP_PASSWORD", "").strip()
    folder = os.getenv("GMAIL_FOLDER_COMMS", "INBOX").strip() or "INBOX"
    exclusa = os.getenv("GMAIL_ETICHETA_EXCLUSA", "").strip()

    # Verificăm întâi că avem cu ce lucra. Fără asta, eroarea de mai jos ar fi
    # „autentificare eșuată", ceea ce te-ar trimite să cauți o parolă greșită
    # când de fapt cheia lipsește din .env.
    lipsesc = [n for n, v in (("GMAIL_USER", utilizator),
                              ("GMAIL_APP_PASSWORD", parola)) if not v]
    if lipsesc:
        return {"ok": False, "eroare": (
            f"Lipsesc din server\\.env: {', '.join(lipsesc)}. "
            "Sunt aceleași valori ca ale agentului 051.")}

    return {"ok": True, "utilizator": utilizator, "parola": parola,
            "folder": folder, "exclusa": exclusa}


def _traduce_eroarea(exceptie: Exception, parola: str) -> str:
    """Traduce o eroare tehnică într-un mesaj pe înțelesul unui om."""
    if isinstance(exceptie, imaplib.IMAP4.error):
        detaliu = _ascunde_parola(str(exceptie), parola).upper()
        if "AUTHENTICATIONFAILED" in detaliu or "INVALID CREDENTIALS" in detaliu:
            return ("Parola de aplicație Gmail e greșită sau revocată. "
                    "Se generează alta din setările de securitate Google, și se "
                    "schimbă în două locuri: server\\.env și .env-ul agentului 051.")
        return f"Gmail a respins cererea: {_ascunde_parola(str(exceptie), parola)}"
    return "Nu mă pot conecta la Gmail. Verifică internetul."


def _inchide_curat(conexiune) -> None:
    """Închide conexiunea, orice s-ar fi întâmplat înainte.

    Fără asta, o conexiune rămasă deschisă ține ocupată o sesiune pe serverul
    Gmail, iar Gmail limitează numărul de sesiuni simultane — iar agentul 051
    folosește același cont.
    """
    if conexiune is None:
        return
    try:
        conexiune.close()
    except Exception:
        pass
    try:
        conexiune.logout()
    except Exception:
        pass


def _cauta(conexiune, criterii: list) -> list[int]:
    """UID SEARCH, întors ca listă de numere. Eșecul dă listă goală, nu excepție."""
    stare, rezultat = conexiune.uid("SEARCH", None, *criterii)
    if stare != "OK" or not rezultat or not rezultat[0]:
        return []
    return [int(x) for x in rezultat[0].split()]


def _adu_antete(conexiune, uiduri: list[int]) -> dict[int, dict]:
    """Antetele tuturor mailurilor din fereastră, într-o singură cerere.

    Doar antetele: gruparea și numărătoarea n-au nevoie de corp, iar corpurile
    ar fi sute de KB pentru un raport care încape în trei rânduri. Corpul se
    aduce separat, doar pentru cele detaliate (`_adu_corpuri`).

    BODY.PEEK, nu BODY: PEEK înseamnă literalmente „trage cu ochiul" — aduce
    același conținut, dar NU atinge steagul de necitit. Fără el, primul raport
    ți-ar marca toate mailurile ca citite și n-ai avea cum să dai înapoi.
    """
    if not uiduri:
        return {}
    stare, date = conexiune.uid(
        "FETCH", ",".join(str(u) for u in uiduri),
        "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)])")
    if stare != "OK":
        return {}

    antete: dict[int, dict] = {}
    for element in date:
        if not isinstance(element, tuple):
            continue
        # Prefixul arată așa: `12 (UID 17332 BODY[HEADER.FIELDS (...)] {215}`.
        prefix = element[0].decode("utf-8", errors="replace")
        if "UID " not in prefix:
            continue
        dupa = prefix.split("UID ", 1)[1]
        cifre = "".join(c for c in dupa.split()[0] if c.isdigit())
        if not cifre:
            continue
        mesaj = email.message_from_bytes(element[1])
        antete[int(cifre)] = {
            "expeditor": _decodifica_antet(mesaj.get("From")),
            "subiect": _decodifica_antet(mesaj.get("Subject")),
            "data": _data_in_iso(mesaj.get("Date")),
            "id": (mesaj.get("Message-ID") or "").strip(),
        }
    return antete


def _adu_corpuri(conexiune, uiduri: list[int]) -> dict[int, str]:
    """Începutul corpului, doar pentru mailurile care primesc rând propriu.

    `<0.OCTETI_CORP>` e aducere PARȚIALĂ: primii atâția octeți, nu tot mailul.
    Un newsletter de 400 KB ar costa secunde la fiecare raport pentru ceva din
    care folosim 400 de caractere.

    Un mail care nu se poate citi nu oprește restul — rămâne fără extras, cu
    subiectul lui, care de obicei spune destul.
    """
    def incearca(uid: int, octeti: int) -> str:
        stare, date = conexiune.uid("FETCH", str(uid), f"(BODY.PEEK[]<0.{octeti}>)")
        if stare != "OK" or not date or not isinstance(date[0], tuple):
            return ""
        return _extrage_corpul(email.message_from_bytes(date[0][1]))

    corpuri: dict[int, str] = {}
    for uid in uiduri:
        try:
            text = incearca(uid, OCTETI_CORP)
            # Gol nu înseamnă „mail fără text", ci de obicei „încă n-am ieșit
            # din CSS-ul din <head>". Se mai cere o dată, mai mult, doar pentru
            # ăsta. Vezi OCTETI_CORP_MARE.
            if not text:
                text = incearca(uid, OCTETI_CORP_MARE)
            if text:
                corpuri[uid] = text[:LUNGIME_EXTRAS]
        except Exception:
            continue
    return corpuri


def raport(zile: int | None = None) -> dict:
    """Mailurile necitite din fereastră, grupate pe expeditor.

    `zile=None` (implicit) — fereastra e de la raportul anterior până acum, și
    reperul SE MUTĂ. Asta e „ce e nou".

    `zile=N` — fereastra e ultimele N zile, și reperul NU se mută. Asta e
    „arată-mi din nou" / „ce-am ratat": supapa care face mutarea automată
    nevinovată, fiindcă o fereastră consumată nu mai e o ușă închisă.

    Întoarce contractul agentului 051, ca să fie unul singur în toată casa:
        {"ok": true,  "date": {"raport": {...}, "text": "..."}}
        {"ok": false, "eroare": "...", "detaliu": "..."}
    La eșec cheia `date` LIPSEȘTE, nu vine goală — un `{}` s-ar citi ca „zero
    mailuri", ceea ce e cu totul altceva decât „n-a mers".

    Zero mailuri noi NU e eroare: e răspuns valid cu `nimic_nou: true`.
    """
    config = _citeste_configuratia()
    if not config["ok"]:
        return {"ok": False, "eroare": config["eroare"], "server": "Gmail"}

    reper = citeste_reper()
    conexiune = None
    try:
        conexiune = imaplib.IMAP4_SSL(SERVER_GMAIL, PORT_IMAP_SSL, timeout=TIMEOUT_SECUNDE)
        conexiune.login(config["utilizator"], config["parola"])

        # readonly=True: doar ne uităm. Serverul știe de la început că nu
        # scriem, și ne-ar refuza orice încercare. E prima din cele două
        # garanții; a doua e BODY.PEEK, în `_adu_antete` și `_adu_corpuri`.
        stare, raspuns = conexiune.select(f'"{config["folder"]}"', readonly=True)
        if stare != "OK":
            return {"ok": False, "server": "Gmail",
                    "eroare": f"Folderul {config['folder']} nu există în Gmail."}

        # UIDVALIDITY: dacă Gmail îl schimbă, toate UID-urile de dinainte devin
        # fără înțeles și un reper vechi ar tăia fereastra aiurea. Se compară
        # la fiecare conectare, iar la nepotrivire se pornește de la zero — cu
        # un raport mai lung o singură dată, nu cu unul greșit la nesfârșit.
        uidvalidity = (conexiune.response("UIDVALIDITY")[1] or [b""])[0].decode() or ""
        reper_valid = bool(reper["uid"]) and reper["uidvalidity"] == uidvalidity

        criterii = ["UNSEEN"]
        if config["exclusa"]:
            # Eticheta lui 051. Raportată și aici, ar fi aceeași informație pe
            # două uși, cu două numere care se pot contrazice.
            criterii += ["NOT", "X-GM-LABELS", f'"{config["exclusa"]}"']

        toate = _cauta(conexiune, criterii)

        if zile:
            de_cand = datetime.now(timezone.utc) - timedelta(days=int(zile))
            # SINCE lucrează pe dată, nu pe oră, și pe data internă a serverului.
            in_fereastra = set(_cauta(conexiune, criterii +
                                      ["SINCE", de_cand.strftime("%d-%b-%Y")]))
            uiduri = [u for u in toate if u in in_fereastra]
            de_la = de_cand.isoformat()
        elif reper_valid:
            # `UID n:*` întoarce ÎNTOTDEAUNA cel puțin cel mai mare UID, chiar
            # când n e peste el — o ciudățenie a protocolului, măsurată pe
            # cutia asta. De-asta rezultatul se mai filtrează o dată în cod;
            # fără filtrul ăsta, ultimul mail s-ar raporta la nesfârșit.
            uiduri = [u for u in _cauta(conexiune, criterii + ["UID", f"{reper['uid'] + 1}:*"])
                      if u > reper["uid"]]
            de_la = reper["ultim_raport"]
        else:
            uiduri = list(toate)
            de_la = None

        uiduri.sort()
        antete = _adu_antete(conexiune, uiduri)

        # Cele mai recente primesc rând propriu. Restul intră doar la numărătoare.
        detaliate = sorted(uiduri, reverse=True)[:MAX_DETALIATE]
        corpuri = _adu_corpuri(conexiune, detaliate)

        mailuri, toate_mailurile = [], []
        for uid in sorted(uiduri, reverse=True):
            a = antete.get(uid)
            if not a:
                continue
            domeniu = domeniu_din(a["expeditor"])
            baza = {
                "uid": uid,
                "id": a["id"] or f"uid:{uid}",
                "expeditor": a["expeditor"],
                "domeniu": domeniu,
                "eticheta": eticheta_din(a["expeditor"], domeniu),
                "subiect": a["subiect"],
                "data": a["data"],
            }
            toate_mailurile.append(baza)
            if uid in detaliate:
                mailuri.append({**baza, "extras": corpuri.get(uid, "")})

        acum = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        rezultat = {
            "de_la": de_la,
            "pana_la": acum,
            "fereastra": f"ultimele {int(zile)} zile" if zile else "de la raportul anterior",
            "total": len(toate_mailurile),
            "grupuri": grupeaza(toate_mailurile),
            "mailuri": mailuri,
            # Câte au rămas necitite dinaintea ferestrei. Un NUMĂR, nicio listă:
            # o a doua listă ar crește la nesfârșit până când singurul fel de a
            # o opri ar fi să intri în Gmail și să bifezi — exact corvoada pe
            # care unealta o scutește.
            "necitite_mai_vechi": max(0, len(toate) - len(toate_mailurile)),
            "reper_mutat": False,
            "nimic_nou": not toate_mailurile,
        }

        # Reperul se mută doar pe drumul implicit, și doar dacă a văzut ceva.
        # Cu `zile` e o recitire: ar muta reperul pentru cineva care tocmai a
        # cerut să se uite înapoi.
        if zile is None and uiduri:
            scrie_reper(acum, max(uiduri), uidvalidity)
            rezultat["reper_mutat"] = True
            scrie_in_jurnal(rezultat)

        return {"ok": True, "date": {"raport": rezultat, "text": formateaza_text(rezultat)}}

    except (imaplib.IMAP4.error, socket.gaierror, socket.timeout, TimeoutError,
            ConnectionError, ssl.SSLError, OSError) as eroare:
        return {"ok": False, "server": "Gmail",
                "eroare": _traduce_eroarea(eroare, config["parola"]),
                "detaliu": f"{type(eroare).__name__}: {_ascunde_parola(str(eroare), config['parola'])}"}
    finally:
        # Se execută ÎNTOTDEAUNA — și când totul a mers, și când a crăpat pe drum.
        _inchide_curat(conexiune)


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    zile = int(sys.argv[1]) if len(sys.argv) > 1 else None
    r = raport(zile)
    if not r["ok"]:
        print("EȘEC:", r["eroare"])
        print("detaliu:", r.get("detaliu", "-"))
    else:
        print(r["date"]["text"], "\n")
        for m in r["date"]["raport"]["mailuri"]:
            print(f"  [{m['eticheta']}] {m['subiect']}")
            print(f"      {(m['extras'] or '(fără extras)')[:150]}")
