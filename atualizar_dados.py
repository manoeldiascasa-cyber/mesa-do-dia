#!/usr/bin/env python3
"""Robô da Mesa do Dia.

Busca o último pregão fechado das principais ações da B3 e do Ibovespa
(fonte: Yahoo Finance, gratuita e não oficial) e grava o arquivo data.json,
que o app (index.html) lê ao abrir.

Roda sozinho no GitHub Actions; não precisa de chave de API nem de instalação.
"""
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------- configuração
# Liquidez mínima: volume médio diário em reais (média de 20 pregões x preço).
MIN_VOLUME_FINANCEIRO = 20_000_000

TICKERS = [
    "ABEV3", "ALOS3", "ASAI3", "AURE3", "AZZA3", "B3SA3", "BBAS3", "BBDC3", "BBDC4",
    "BBSE3", "BEEF3", "BPAC11", "BRAP4", "BRFS3", "BRKM5", "CMIG4", "CMIN3", "COGN3",
    "CPFE3", "CPLE6", "CRFB3", "CSAN3", "CSNA3", "CYRE3", "DXCO3", "EGIE3", "ELET3",
    "ELET6", "EMBR3", "ENEV3", "ENGI11", "EQTL3", "EZTC3", "FLRY3", "GGBR4", "GOAU4",
    "HAPV3", "HYPE3", "IGTI11", "ITSA4", "ITUB4", "JBSS3", "KLBN11", "LREN3", "MGLU3",
    "MRFG3", "MRVE3", "MULT3", "NATU3", "PCAR3", "PETR3", "PETR4", "PETZ3", "PRIO3",
    "RADL3", "RAIL3", "RAIZ4", "RDOR3", "RECV3", "RENT3", "SANB11", "SBSP3", "SLCE3",
    "SMFT3", "SUZB3", "TAEE11", "TIMS3", "TOTS3", "UGPA3", "USIM5", "VALE3", "VAMO3",
    "VBBR3", "VIVA3", "VIVT3", "WEGE3", "YDUQ3",
]
IBOV = "^BVSP"

SAIDA = Path(__file__).with_name("data.json")
HOSTS = ["query1.finance.yahoo.com", "query2.finance.yahoo.com"]
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
TZ_BR = ZoneInfo("America/Sao_Paulo")


# --------------------------------------------------------------------- download
def http_json(url, tentativas=4):
    ultimo = None
    for i in range(tentativas):
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            ultimo = e
            if e.code == 404:
                return None
            time.sleep(2 * (i + 1))
        except Exception as e:  # rede, JSON inválido etc.
            ultimo = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"falha ao baixar: {ultimo}")


def baixar_candles(simbolo):
    """Devolve a lista de candles diários [{d,o,h,l,c,v}] em ordem de data."""
    q = urllib.parse.quote(simbolo, safe="")
    dados = None
    erro = None
    for host in HOSTS:
        url = f"https://{host}/v8/finance/chart/{q}?range=4mo&interval=1d&includePrePost=false"
        try:
            dados = http_json(url)
            break
        except RuntimeError as e:
            erro = e
    else:
        raise erro
    if not dados:
        return []
    res = (dados.get("chart") or {}).get("result")
    if not res:
        return []
    r = res[0]
    ts = r.get("timestamp") or []
    cot = ((r.get("indicators") or {}).get("quote") or [{}])[0]
    desloc = (r.get("meta") or {}).get("gmtoffset", -10800)
    por_data = {}
    for i, t in enumerate(ts):
        try:
            o, h, l, c = cot["open"][i], cot["high"][i], cot["low"][i], cot["close"][i]
            v = cot["volume"][i] or 0
        except (KeyError, IndexError, TypeError):
            continue
        if None in (o, h, l, c):
            continue
        d = (datetime.fromtimestamp(t, timezone.utc) + timedelta(seconds=desloc)).date().isoformat()
        por_data[d] = {"d": d, "o": o, "h": h, "l": l, "c": c, "v": v}
    candles = [por_data[d] for d in sorted(por_data)]
    # Descarta o candle de hoje enquanto o pregão ainda não terminou.
    agora = datetime.now(TZ_BR)
    if candles and candles[-1]["d"] == agora.date().isoformat() and (agora.hour, agora.minute) < (18, 20):
        candles.pop()
    return candles


# ----------------------------------------------------------------------- cálculo
def consistente(c):
    return c["h"] >= max(c["o"], c["c"], c["l"]) and c["l"] <= min(c["o"], c["c"], c["h"]) and c["l"] > 0


def atr14(candles):
    trs = []
    for i in range(1, len(candles)):
        a, ant = candles[i], candles[i - 1]["c"]
        trs.append(max(a["h"] - a["l"], abs(a["h"] - ant), abs(a["l"] - ant)))
    ult = trs[-14:]
    return sum(ult) / len(ult) if ult else None


def volume_medio(candles):
    anteriores = [c["v"] for c in candles[-21:-1]]
    return sum(anteriores) / len(anteriores) if len(anteriores) >= 15 else None


def resumo_acao(t, candles):
    u = candles[-1]
    return {
        "t": t, "o": round(u["o"], 2), "h": round(u["h"], 2), "l": round(u["l"], 2), "c": round(u["c"], 2),
        "v": int(u["v"]), "avg": int(volume_medio(candles) or 0) or None,
        "atr": round(atr14(candles), 3) if len(candles) >= 15 else None,
    }


def sem_hora(d):
    return {k: v for k, v in d.items() if k != "updated_at"}


# -------------------------------------------------------------------------- main
def main(baixar=baixar_candles, pausa=0.4):
    ok, pulados = {}, []
    for t in TICKERS:
        try:
            cs = baixar(t + ".SA")
        except Exception as e:
            pulados.append(f"{t}: {e}")
            continue
        if len(cs) < 15:
            pulados.append(f"{t}: poucos dados")
            continue
        if not consistente(cs[-1]):
            pulados.append(f"{t}: candle inconsistente")
            continue
        ok[t] = cs
        time.sleep(pausa)

    if len(ok) < 10:
        print("Poucas ações baixadas; mantendo o data.json anterior.", file=sys.stderr)
        for p in pulados[:15]:
            print("  -", p, file=sys.stderr)
        return 1

    sessao = Counter(cs[-1]["d"] for cs in ok.values()).most_common(1)[0][0]
    acoes = []
    for t, cs in sorted(ok.items()):
        if cs[-1]["d"] != sessao:
            pulados.append(f"{t}: sem pregão em {sessao}")
            continue
        r = resumo_acao(t, cs)
        fin = (r["avg"] or r["v"]) * r["c"]
        if fin < MIN_VOLUME_FINANCEIRO:
            pulados.append(f"{t}: liquidez baixa")
            continue
        acoes.append(r)

    ibov = None
    try:
        cs = baixar(IBOV)
        if len(cs) >= 15 and cs[-1]["d"] == sessao and consistente(cs[-1]):
            u = cs[-1]
            ibov = {"o": round(u["o"], 2), "h": round(u["h"], 2), "l": round(u["l"], 2),
                    "c": round(u["c"], 2), "atr": round(atr14(cs), 2)}
        else:
            pulados.append("Ibovespa: sem dados do mesmo pregão")
    except Exception as e:
        pulados.append(f"Ibovespa: {e}")

    novo = {
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "session_date": sessao,
        "source": "Yahoo Finance",
        "stocks": acoes,
        "ibov": ibov,
        "skipped": pulados,
    }
    if SAIDA.exists():
        try:
            if sem_hora(json.loads(SAIDA.read_text(encoding="utf-8"))) == sem_hora(novo):
                print(f"Sem novidades (pregão de {sessao}); data.json mantido.")
                return 0
        except ValueError:
            pass
    SAIDA.write_text(json.dumps(novo, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"OK: pregão de {sessao}, {len(acoes)} ações, Ibovespa {'ok' if ibov else 'indisponível'}, {len(pulados)} puladas.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
