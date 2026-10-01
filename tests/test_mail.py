"""Funcțiile pure din `server/mail.py`: gruparea, domeniul, corpul, antetele.

Rulare, din rădăcina depozitului:  py tests/test_mail.py
Iese cu 0 dacă trece, cu 1 dacă nu. Fără rețea, fără server, fără Gmail.

Ce probează: domeniul de grupare (subdomenii de expediere, sufixe compuse,
adrese fără @), eticheta grupului, ordinea stabilă a grupării, corpul scos din
multipart (text simplu cu entități, HTML cu <style>, atașament sărit),
antetele codificate, data în ISO și ascunderea parolei într-un mesaj de eroare.

Ce NU acoperă: IMAP-ul viu (`raport`, `_adu_antete`, `_adu_corpuri`, `_cauta`),
cele două garanții de citire (`readonly=True`, `BODY.PEEK[]`), reperul și
jurnalul de pe disc.
"""
import sys
from email.message import EmailMessage
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

import mail  # noqa: E402

picate = []


def egal(primit, asteptat, ce):
    ok = primit == asteptat
    print(("TRECE " if ok else "PICĂ  ") + ce + ("" if ok else f": {primit!r} ≠ {asteptat!r}"))
    if not ok:
        picate.append(ce)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # consola e cp1252, textele au diacritice
    # domeniu_din
    egal(mail.domeniu_din("ALTEX <newsletter@news.altex.ro>"), "altex.ro",
         "subdomeniul de expediere se taie")
    egal(mail.domeniu_din("x@altex.ro"), "altex.ro", "domeniu simplu rămâne")
    egal(mail.domeniu_din("Shop <a@mail.shop.co.uk>"), "shop.co.uk",
         "sufix compus păstrează trei bucăți")
    egal(mail.domeniu_din("fără adresă"), "necunoscut", "fără @ → necunoscut")
    egal(mail.domeniu_din("A <B@NEWS.EMAG.RO.>"), "emag.ro", "majuscule și punct final")

    # eticheta_din
    egal(mail.eticheta_din('"Anthropic, PBC" <a@anthropic.com>', "anthropic.com"),
         "Anthropic", "forma juridică se taie")
    egal(mail.eticheta_din("<a@x.ro>", "x.ro"), "x.ro", "fără nume → domeniul")
    egal(mail.eticheta_din("Ion via LinkedIn <a@linkedin.com>", "linkedin.com"),
         "Ion", "„via” se taie")

    # grupeaza
    mailuri = [{"domeniu": "emag.ro", "eticheta": e} for e in ("eMAG", "eMAG Genius", "eMAG.ro")]
    mailuri += [{"domeniu": "linkedin.com", "eticheta": "LinkedIn"}] * 3
    mailuri += [{"domeniu": "b.ro", "eticheta": "Beta"}, {"domeniu": "a.ro", "eticheta": "Alfa"}]
    g = mail.grupeaza(mailuri)
    egal([(x["eticheta"], x["nr"]) for x in g],
         [("eMAG", 3), ("LinkedIn", 3), ("Alfa", 1), ("Beta", 1)],
         "ordine: număr descrescător, apoi alfabetic; la egalitate de nume, cel mai scurt")
    egal(mail.grupeaza(list(reversed(mailuri))), g, "aceleași date în altă ordine → același raport")

    # _extrage_corpul: text simplu cu entități
    m = EmailMessage()
    m.set_content("Verific&#259;-&#539;i   comanda\n\n\n\nmulțumim")
    egal(mail._extrage_corpul(m), "Verifică-ți comanda\nmulțumim",
         "text simplu: entitățile se traduc, spațiile și rândurile goale pleacă")

    # _extrage_corpul: HTML cu <style>, plus atașament
    m = EmailMessage()
    m.add_alternative("<html><head><style>p{color:red}</style></head>"
                      "<body><p>Ofertele noi</p><p>Vezi acum</p></body></html>", subtype="html")
    m.add_attachment(b"%PDF-1.4 secret", maintype="application", subtype="pdf",
                     filename="factura.pdf")
    corp = mail._extrage_corpul(m)
    egal("color:red" in corp, False, "HTML: stilul nu ajunge în text")
    egal("Ofertele noi" in corp and "Vezi acum" in corp, True, "HTML: textul vizibil ajunge")
    egal("Ofertele noiVezi" in corp, False, "HTML: blocurile nu se lipesc")
    egal("PDF" in corp, False, "atașamentul se sare")

    # _decodifica_antet
    egal(mail._decodifica_antet("=?UTF-8?B?Sm9idXJpIG5vaQ==?= azi"), "Joburi noi azi",
         "antet codificat base64, amestecat cu text simplu")
    egal(mail._decodifica_antet(None), "", "antet lipsă → gol")

    # _data_in_iso
    egal(mail._data_in_iso("Wed, 01 Oct 2026 10:00:00 +0300"), "2026-10-01T10:00:00+03:00",
         "data RFC 2822 → ISO")
    egal(mail._data_in_iso("nu e dată"), "nu e dată", "dată stricată → valoarea brută")

    # _ascunde_parola
    egal("abcd1234" in mail._ascunde_parola("login failed for abcd1234", "abcd1234"), False,
         "parola nu iese în mesajul de eroare")

    print("\n" + ("TOTUL TRECE" if not picate else f"{len(picate)} PICATE"))
    return 1 if picate else 0


if __name__ == "__main__":
    sys.exit(main())
