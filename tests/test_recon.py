"""Contractul MCP cu Agentul 051, pe agentul viu, numai pe ce citește JA.S.Mine.

Rulare, din rădăcina depozitului:  py tests/test_recon.py
Iese cu 0 dacă trece, cu 1 dacă nu. Cere agentul 051 pornit (port 8051) și
`server/.env` cu RECON_MCP_URL și MCP_TOKEN; serverul JA.S.Mine nu trebuie pornit.

Fără token real sau fără agent care să răspundă, testul e SĂRIT, nu picat (iese
cu 0 și spune de ce): e un test de integrare, iar o clonă proaspătă n-are agentul.
Pică numai ce se strică DUPĂ ce agentul a răspuns — adică forma contractului.

Trece prin `server.cheama_recon`, clientul din producție, deci probează și
transportul, și autentificarea, și forma {"ok", "date"}. Cheile verificate sunt
cele pe care le citesc puntea și uneltele lui Sky: dacă agentul redenumește una,
panoul RECON rămâne gol fără nicio eroare, iar testul ăsta pică.

Nu costă bani și nu schimbă nimic: `stare_pipeline`, `oportunitati_noi` și
`briefing` cu `marcheaza_citit=False`. Că reperul NU se mută se probează: momentul
`ultim_briefing` e același înainte și după.

Ce NU acoperă: rularea (`/api/recon/ruleaza` — costă și mută reperul), uneltele
cu model, valorile (o bază goală trece, dacă forma e bună).
"""
import asyncio
import sys
from pathlib import Path

RADACINA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RADACINA / "server"))

import server  # noqa: E402

PICATE = []

# Valoarea din `server/.env.example`: un token care începe așa nu e unul real.
TOKEN_FALS = "your_"
SARIT = "SĂRIT — necesită Agent051 pornit și MCP_TOKEN real"


def verifica(ok: bool, ce: str, detaliu: str = "") -> None:
    print(("TRECE " if ok else "PICĂ  ") + ce + ("" if ok or not detaliu else f": {detaliu}"))
    if not ok:
        PICATE.append(ce)


def are_chei(d: dict, chei: list[str], unde: str) -> None:
    lipsa = [k for k in chei if k not in d]
    verifica(not lipsa, f"{unde} are {', '.join(chei)}", f"lipsesc {lipsa}")


async def unelte_expuse() -> set[str]:
    url = server.os.environ["RECON_MCP_URL"]
    token = server.os.environ.get("MCP_TOKEN", "").strip()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with server.streamablehttp_client(url, headers=headers) as (read, write, _):
        async with server.ClientSession(read, write) as sesiune:
            await sesiune.initialize()
            return {u.name for u in (await sesiune.list_tools()).tools}


async def sesiune(nume: set[str]) -> None:
    for u in ("stare_pipeline", "oportunitati_noi", "briefing"):
        verifica(u in nume, f"agentul expune `{u}`")

    stare = await server.cheama_recon("stare_pipeline", {})
    verifica(stare.get("ok") is True, "stare_pipeline: ok", str(stare)[:200])
    d = stare.get("date") or {}
    are_chei(d, ["etape", "cost", "ultim_briefing"], "stare_pipeline.date")
    are_chei(d.get("etape") or {}, [
        "versiune_criterii", "mailuri", "anunturi", "cu_descriere", "normalizate_ok",
        "filtrate_trece", "filtrate_respins", "filtrate_incert",
        "evaluate_calitativ_curente", "evaluate_calitativ_vechi"], "etape")
    are_chei(d.get("cost") or {}, ["apeluri_total", "tokeni_total"], "cost")
    reper_inainte = d.get("ultim_briefing")

    op = await server.cheama_recon("oportunitati_noi", {"limita": 3})
    verifica(op.get("ok") is True, "oportunitati_noi: ok", str(op)[:200])
    od = op.get("date") or {}
    are_chei(od, ["oportunitati", "total_candidate"], "oportunitati_noi.date")
    lista = od.get("oportunitati") or []
    verifica(isinstance(lista, list) and len(lista) <= 3, "oportunitati_noi respectă `limita`",
             f"{len(lista)} întoarse")
    for i, o in enumerate(lista):
        are_chei(o, ["titlu", "firma", "link", "motiv"], f"oportunitatea {i + 1}")

    br = await server.cheama_recon("briefing", {"marcheaza_citit": False})
    verifica(br.get("ok") is True, "briefing: ok", str(br)[:200])
    bd = br.get("date") or {}
    are_chei(bd, ["briefing", "text", "reper_mutat"], "briefing.date")
    b = bd.get("briefing") or {}
    are_chei(b, ["de_la", "mesaje_noi", "anunturi_noi", "merita_atentie", "in_asteptare",
                 "nimic_nou"], "briefing.briefing")
    verifica(isinstance(b.get("merita_atentie"), list), "merita_atentie e listă")
    are_chei(b.get("in_asteptare") or {}, ["total", "fara_descriere", "nenormalizate",
                                           "neevaluate"], "in_asteptare")
    verifica(bd.get("reper_mutat") is False, "briefing fără marcare: reper_mutat false")

    dupa = await server.cheama_recon("stare_pipeline", {})
    reper_dupa = (dupa.get("date") or {}).get("ultim_briefing")
    verifica(reper_dupa == reper_inainte, "reperul rămâne pe loc",
             f"{reper_inainte} → {reper_dupa}")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # consola e cp1252, textele au diacritice
    token = server.os.environ.get("MCP_TOKEN", "").strip()
    if not token or token.startswith(TOKEN_FALS):
        print(f"{SARIT}: MCP_TOKEN lipsește din server/.env sau e cel din .env.example.")
        return 0
    # Prima conexiune hotărăște între „sărit" și „picat": dacă agentul nu răspunde
    # sau respinge tokenul, nu e nimic de probat.
    try:
        nume = asyncio.run(unelte_expuse())
    except Exception as ex:
        print(f"{SARIT}: agentul nu răspunde sau respinge tokenul la "
              f"{server.os.environ.get('RECON_MCP_URL')} ({type(ex).__name__}: {ex}).")
        return 0
    try:
        asyncio.run(sesiune(nume))
    except Exception as ex:
        print(f"PICĂ  agentul 051 a răspuns, apoi a picat la "
              f"{server.os.environ.get('RECON_MCP_URL')}: {type(ex).__name__}: {ex}")
        return 1
    print("\nTOTUL TRECE" if not PICATE else f"\n{len(PICATE)} PICATE")
    return 0 if not PICATE else 1


if __name__ == "__main__":
    sys.exit(main())
