r"""Aduce pe disc clipurile camerei MUNTELE. Se ruleaza cu mana, rar.

    py server\descarca-munte.py

De ce un script si nu o unealta a serverului: descarcarea dureaza minute, cere
internet si scrie sute de megaocteti. Serverul JA.S.Mine n-are voie sa faca
niciuna din cele trei in timpul unei cereri.

De ce yt-dlp si ffmpeg ca BINARE de sine statatoare, in afara depozitului:
nu sunt dependente ale serverului, deci nu stau in requirements.txt. Le cere doar
scriptul asta, rar, iar serverul nu trebuie sa le vada.

De ce e nevoie de ffmpeg, desi planul spera sa scape fara el: masurat, NICIUNUL
din cele trei clipuri nu mai are format PROGRESIV (video si audio in acelasi
flux). `yt-dlp -F` arata numai „video only" si „audio only",
deci fluxurile trebuie lipite. Daca vreodata YouTube da inapoi formatele
progresive, ffmpeg poate iesi — dar se verifica, nu se presupune.

Clipurile stau in AFARA depozitului, ca modelele Whisper: mari, redescarcabile,
n-au ce cauta nici in git, nici in copia de siguranta.

Se descarca DOAR ce lipseste. Rulat a doua oara, scriptul nu face nimic si o
spune — ca sa poata fi rulat linistit dupa ce s-a adaugat un clip in
server\munte.json.
"""
import json
import subprocess
import sys
from pathlib import Path

AICI = Path(__file__).resolve().parent
CONFIG = AICI / "munte.json"

# Formatul: cea mai buna imagine pana in 720p, plus cel mai bun sunet, lipite in
# mp4. Plafonul e acolo fiindca un clip de fundal intr-un panou nu castiga nimic
# din 1080p, iar octetii se platesc la fiecare deschidere de camera.
#
# `vcodec^=avc1` NU e cosmetica. Cerut doar `ext=mp4`, YouTube a dat AV1 la
# primul clip si VP9 la al treilea (masurat): amandoua sunt legale
# in container mp4 si amandoua merg in Chrome, dar decodarea lor hardware nu e
# garantata pe masina asta. Un decodor software care invarte ventilatorul intr-o
# camera facuta pentru liniste e exact greseala pe care n-o vezi in cod.
# h264 se decodeaza hardware peste tot; iar clipurile astea vin din surse vechi,
# unde codecul modern nu castiga nimic.
FORMAT = ("bestvideo[height<=720][vcodec^=avc1]+bestaudio[ext=m4a]/"
          "bestvideo[height<=720][ext=mp4]+bestaudio/best[height<=720]/best")


def refuz(mesaj: str) -> None:
    """Acelasi tipar ca la server: se spune CE lipseste, nu prin ce linie s-a aflat."""
    print()
    print("  MUNTELE - NU POT DESCARCA")
    print()
    for rand in mesaj.splitlines():
        print("  " + rand)
    print()
    sys.exit(1)


def citeste_config() -> dict:
    # utf-8-sig, ca peste tot: PowerShell 5.1 pune BOM, iar un fisier care arata
    # perfect in editor ar crapa la prima linie.
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        refuz(f"Nu exista {CONFIG}.")
    except json.JSONDecodeError as ex:
        refuz(f"{CONFIG} e stricat: linia {ex.lineno}, coloana {ex.colno}.")
    return {}


def main() -> None:
    date = citeste_config()
    folder = AICI.parent / date["folder"]
    unelte = AICI.parent / date["unelte"]
    ytdlp = unelte / "yt-dlp.exe"
    ffmpeg = unelte / "ffmpeg.exe"

    # Amandoua se verifica INAINTE de prima descarcare. Fara ffmpeg, yt-dlp ar
    # aduce fericit primele doua clipuri si ar pica la lipire pe al treilea —
    # adica dupa minute de asteptare, cu jumatate de treaba facuta.
    lipsa = [str(c) for c in (ytdlp, ffmpeg) if not c.is_file()]
    if lipsa:
        refuz("Lipsesc binarele:\n  " + "\n  ".join(lipsa)
              + "\n\nSe iau de la:\n"
                "  https://github.com/yt-dlp/yt-dlp/releases/latest\n"
                "  https://github.com/yt-dlp/FFmpeg-Builds/releases/latest\n"
                "si se pun ca fisiere .exe in folderul de mai sus.")

    folder.mkdir(parents=True, exist_ok=True)
    clipuri = date.get("clipuri") or []
    lipsesc = [c for c in clipuri if not (folder / c["fisier"]).is_file()]

    print(f"MUNTELE: {len(clipuri)} clipuri in lista, {len(lipsesc)} lipsesc de pe disc.")
    if not lipsesc:
        print("Nimic de facut.")
        return

    esecuri = []
    for c in lipsesc:
        tinta = folder / c["fisier"]
        print()
        print(f"--- {c['titlu']}")
        print(f"    {c['sursa']}")
        cod = subprocess.call([
            str(ytdlp), c["sursa"],
            "-f", FORMAT,
            "--merge-output-format", "mp4",
            "--ffmpeg-location", str(ffmpeg),
            # Numele il alegem NOI, nu titlul de la sursa: titlul se poate
            # schimba sau poate contine caractere care nu incap intr-o cale.
            "-o", str(tinta),
            "--no-playlist",       # linkurile poarta &list=RD..., adica radioul
            "--no-part", "--no-mtime",
        ])
        # Un cod de iesire 0 cu fisierul lipsa e tot esec: singurul lucru care
        # conteaza e ce a ajuns pe disc.
        if cod != 0 or not tinta.is_file():
            esecuri.append(c["fisier"])

    print()
    print("--- Ce e pe disc acum:")
    total = 0
    for c in clipuri:
        cale = folder / c["fisier"]
        if cale.is_file():
            mb = cale.stat().st_size / 1024 / 1024
            total += mb
            print(f"    {c['fisier']:<28} {mb:7.1f} MB   {c['titlu']}")
        else:
            print(f"    {c['fisier']:<28}   LIPSA   {c['titlu']}")
    print(f"    {'':<28} {total:7.1f} MB total")

    if esecuri:
        print()
        print("  N-au putut fi aduse: " + ", ".join(esecuri))
        sys.exit(1)


if __name__ == "__main__":
    main()
