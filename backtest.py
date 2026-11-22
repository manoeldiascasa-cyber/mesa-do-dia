#!/usr/bin/env python3
"""Simulado (backtest) do analisador da Mesa do Dia.

Para cada pregão dos últimos 2 anos, refaz o que o app mostraria na manhã seguinte
(top 10 ações com gatilho, stop e alvos) e confere no pregão seguinte o que teria acontecido.

Dois níveis de precisão:
  * Candles diários (2 anos): não dá para saber se o stop ou o alvo veio primeiro dentro do mesmo dia,
    então o resultado vem como um intervalo (cenário pessimista e cenário otimista).
  * Candles de 5 minutos (últimos ~60 dias): ordem exata dos eventos; é a medida mais confiável, mas com
    poucos dias.

Grava backtest_resultado.json e backtest_relatorio.md. Roda no GitHub Actions (precisa de acesso ao Yahoo).
"""
import json
import math
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import atualizar_dados as ad

PARAMS_ACAO = {"ke": 0.10, "ks": 0.50, "k1": 0.75, "k2": 1.25}   # iguais aos do app
PARAMS_WIN = {"ke": 0.10, "ks": 0.15, "k1": 0.25, "k2": 0.45}
TOPO = 10
PASTA = Path(__file__).parent


# ------------------------------------------------------------ réplica do analisador
def r2(x):
    return math.floor(x * 100 + 0.5) / 100


def r5(x):
    return math.floor(x / 5 + 0.5) * 5


def vol_score(p):
    if p < 1:
        return p * 0.4
    if p < 1.5:
        return 0.4 + (p - 1) / 0.5 * 0.6
    if p <= 4:
        return 1
    if p <= 6:
        return 1 - (p - 4) / 2 * 0.6
    return 0.4


def analisar_acao(r, P=PARAMS_ACAO):
    """r: {o,h,l,c,v,avg,atr}. Mesma conta do analyzeStock do index.html."""
    rng = r["h"] - r["l"]
    atr = r.get("atr")
    A = (atr if atr and atr > 0 else rng) or r["c"] * 0.02
    atr_pct = A / r["c"] * 100
    avg = r.get("avg")
    rvol = r["v"] / avg if avg and avg > 0 else None
    cp = (r["c"] - r["l"]) / rng if rng > 0 else 0.5
    corpo = abs(r["c"] - r["o"]) / rng if rng > 0 else 0
    score = (15 if rvol is None else min(1, max(0, rvol / 2)) * 30) + vol_score(atr_pct) * 25 \
        + abs(cp - 0.5) * 2 * 25 + corpo * 20
    sg = 1 if cp >= 0.5 else -1
    entry = r2(r["c"] + sg * P["ke"] * A)
    return {
        "score": math.floor(score + 0.5), "side": "C" if sg == 1 else "V", "sg": sg,
        "entry": entry, "stop": r2(entry - sg * P["ks"] * A),
        "t1": r2(entry + sg * P["k1"] * A), "t2": r2(entry + sg * P["k2"] * A),
    }


def resumo_no_indice(cs, i):
    """Mesmo resumo que o robô grava no data.json, usando só dados até o candle i."""
    u = cs[i]
    anteriores = cs[i - 20:i]
    avg = sum(x["v"] for x in anteriores) / 20
    trs = []
    for j in range(i - 13, i + 1):
        trs.append(max(cs[j]["h"] - cs[j]["l"], abs(cs[j]["h"] - cs[j - 1]["c"]), abs(cs[j]["l"] - cs[j - 1]["c"])))
    return {"o": round(u["o"], 2), "h": round(u["h"], 2), "l": round(u["l"], 2), "c": round(u["c"], 2),
            "v": int(u["v"]), "avg": int(avg) or None, "atr": round(sum(trs) / 14, 3)}


def niveis_win(w, P=PARAMS_WIN):
    """w: {h,l,c,atr}. Cenário de compra e de venda, como no app (arredondado de 5 em 5 pontos)."""
    rng = w["h"] - w["l"]
    A = (w["atr"] if w.get("atr") and w["atr"] > 0 else rng) or 1
    pv = (w["h"] + w["l"] + w["c"]) / 3
    def cen(sg):
        e = r5(w["c"] + sg * P["ke"] * A)
        return {"entry": e, "stop": r5(e - sg * P["ks"] * A), "t1": r5(e + sg * P["k1"] * A),
                "t2": r5(e + sg * P["k2"] * A), "sg": sg, "side": "C" if sg == 1 else "V"}
    return {"C": cen(1), "V": cen(-1), "vies": "C" if w["c"] >= pv else "V"}


# ------------------------------------------------------------------- simulação
def _entrada(sig, barra):
    """Preço de entrada se o gatilho disparou, senão None. Gap além do alvo 1: ignora a operação."""
    sg = sig["sg"]
    if sg == 1:
        if barra["h"] < sig["entry"]:
            return None
        fill = max(barra["o"], sig["entry"])
        return None if fill >= sig["t1"] else fill
    if barra["l"] > sig["entry"]:
        return None
    fill = min(barra["o"], sig["entry"])
    return None if fill <= sig["t1"] else fill


def _resultado(sig, fill, saida, res, extra=None):
    risco = abs(fill - sig["stop"])
    pnl = (saida - fill) * sig["sg"]
    out = {"fill": fill, "saida": saida, "res": res, "r": pnl / risco if risco > 0 else 0.0,
           "pnl_pct": pnl / fill * 100}
    if extra:
        out.update(extra)
    return out


def simular_diario(sig, barra):
    """Devolve {'pess': ..., 'otim': ...} ou None se o gatilho não disparou."""
    fill = _entrada(sig, barra)
    if fill is None:
        return None
    sg = sig["sg"]
    parou = barra["l"] <= sig["stop"] if sg == 1 else barra["h"] >= sig["stop"]
    alvo1 = barra["h"] >= sig["t1"] if sg == 1 else barra["l"] <= sig["t1"]
    alvo2 = barra["h"] >= sig["t2"] if sg == 1 else barra["l"] <= sig["t2"]
    if parou:
        pess = _resultado(sig, fill, sig["stop"], "stop")
    elif alvo1:
        pess = _resultado(sig, fill, sig["t1"], "alvo1")
    else:
        pess = _resultado(sig, fill, barra["c"], "fechamento")
    if alvo1:
        otim = _resultado(sig, fill, sig["t1"], "alvo1")
    elif parou:
        otim = _resultado(sig, fill, sig["stop"], "stop")
    else:
        otim = _resultado(sig, fill, barra["c"], "fechamento")
    otim["alvo2"] = bool(alvo2 and alvo1)
    return {"pess": pess, "otim": otim}


def simular_5min(sig, barras):
    """Ordem exata dos eventos com candles de 5 min. Dentro do mesmo candle, o stop vale primeiro."""
    sg, fill = sig["sg"], None
    for b in barras:
        if fill is None:
            fill = _entrada(sig, b)
            if fill is None:
                if (sg == 1 and b["h"] >= sig["entry"]) or (sg == -1 and b["l"] <= sig["entry"]):
                    return None  # gap além do alvo: operação ignorada
                continue
        parou = b["l"] <= sig["stop"] if sg == 1 else b["h"] >= sig["stop"]
        alvo1 = b["h"] >= sig["t1"] if sg == 1 else b["l"] <= sig["t1"]
        if parou:
            return _resultado(sig, fill, sig["stop"], "stop")
        if alvo1:
            return _resultado(sig, fill, sig["t1"], "alvo1")
    if fill is None:
        return None
    return _resultado(sig, fill, barras[-1]["c"], "fechamento")


# ----------------------------------------------------------------- estatísticas
def estat(trades):
    n = len(trades)
    if not n:
        return {"n": 0}
    rs = [t["r"] for t in trades]
    ganhos = sum(r for r in rs if r > 0)
    perdas = -sum(r for r in rs if r < 0)
    cont = lambda nome: round(100 * sum(1 for t in trades if t["res"] == nome) / n, 1)
    return {
        "n": n, "alvo1_pct": cont("alvo1"), "stop_pct": cont("stop"), "fechamento_pct": cont("fechamento"),
        "lucro_pct": round(100 * sum(1 for r in rs if r > 0) / n, 1),
        "R_medio": round(statistics.mean(rs), 3), "R_mediano": round(statistics.median(rs), 3),
        "fator_lucro": round(ganhos / perdas, 2) if perdas else None,
        "retorno_medio_pct": round(statistics.mean(t["pnl_pct"] for t in trades), 3),
    }


def curva_r(trades_por_dia):
    acum, pico, dd, pts = 0.0, 0.0, 0.0, []
    for d in sorted(trades_por_dia):
        acum += sum(t["r"] for t in trades_por_dia[d])
        pico = max(pico, acum)
        dd = max(dd, pico - acum)
        pts.append([d, round(acum, 2)])
    return pts, round(dd, 2)


# ------------------------------------------------------------------------ dados
def baixar_yf(simbolo, periodo, intervalo):
    yf = ad.obter_yfinance()
    erro = None
    for tent in range(3):
        try:
            df = yf.Ticker(simbolo).history(period=periodo, interval=intervalo, auto_adjust=False, actions=False)
            break
        except Exception as e:  # noqa: BLE001
            erro = e
            time.sleep(3 * (tent + 1))
    else:
        raise RuntimeError(f"{simbolo}: {erro}")
    out = []
    for ts, lin in df.iterrows():
        o, h, l, c = lin["Open"], lin["High"], lin["Low"], lin["Close"]
        if any(x != x for x in (o, h, l, c)):
            continue
        t = ts.tz_convert("America/Sao_Paulo") if ts.tzinfo else ts
        out.append({"ts": t.isoformat(), "d": t.date().isoformat(), "hm": (t.hour, t.minute),
                    "o": float(o), "h": float(h), "l": float(l), "c": float(c), "v": float(lin["Volume"] or 0)})
    return out


def sem_hoje_incompleto(cs):
    return ad.sem_candle_incompleto(cs)


# ------------------------------------------------------------------ orquestração
def executar(baixar=baixar_yf, pausa=0.4, log=print):
    ibov = sem_hoje_incompleto(baixar("^BVSP", "2y", "1d"))
    if len(ibov) < 100:
        raise RuntimeError("histórico do Ibovespa insuficiente")
    cal = [c["d"] for c in ibov]
    dias_ok = {d for d in cal}

    diario, falhas = {}, []
    for t in ad.TICKERS:
        try:
            cs = sem_hoje_incompleto(baixar(t + ".SA", "2y", "1d"))
        except Exception as e:  # noqa: BLE001
            falhas.append(f"{t}: {e}")
            continue
        cs = [c for c in cs if c["d"] in dias_ok and c["h"] >= max(c["o"], c["c"], c["l"]) and c["l"] > 0]
        if len(cs) >= 60:
            diario[t] = cs
        time.sleep(pausa)
    if len(diario) < 15:
        raise RuntimeError(f"poucas ações com histórico ({len(diario)}); falhas: {falhas[:5]}")
    log(f"Histórico diário: {len(diario)} ações, {len(cal)} pregões ({cal[0]} a {cal[-1]}).")

    pos = {t: {c["d"]: i for i, c in enumerate(cs)} for t, cs in diario.items()}

    # --- sinais: o que o app mostraria no fim de cada pregão
    sinais = []  # (data_do_trade, ticker, sig, barra_do_dia_seguinte, candle_do_sinal)
    for k in range(21, len(cal) - 1):
        d0, d1 = cal[k], cal[k + 1]
        do_dia = []
        for t, cs in diario.items():
            i0, i1 = pos[t].get(d0), pos[t].get(d1)
            if i0 is None or i1 is None or i1 != i0 + 1 or i0 < 21:
                continue
            r = resumo_no_indice(cs, i0)
            if (r["avg"] or r["v"]) * r["c"] < ad.MIN_VOLUME_FINANCEIRO:
                continue
            sig = analisar_acao(r)
            sig["t"] = t
            do_dia.append((sig, cs[i1], cs[i0]))
        do_dia.sort(key=lambda x: (-x[0]["score"], x[0]["t"]))
        for rank, (sig, barra, base) in enumerate(do_dia, 1):
            sig["rank"], sig["n_dia"] = rank, len(do_dia)
            sinais.append((d1, sig["t"], sig, barra, base))
    log(f"Sinais gerados: {len(sinais)}.")

    # --- simulação com candles diários (pessimista e otimista)
    por = {"pess": [], "otim": []}
    direcao = {"topo": [0, 0, 0, 0], "todos": [0, 0, 0, 0]}  # [acertou_vs_fech_ant, n, acertou_vs_abertura, n]
    for d1, t, sig, barra, base in sinais:
        for chave in ("topo", "todos"):
            if chave == "topo" and sig["rank"] > TOPO:
                continue
            acerto_ant = (barra["c"] > base["c"]) == (sig["sg"] == 1)
            acerto_abe = (barra["c"] > barra["o"]) == (sig["sg"] == 1)
            direcao[chave][0] += acerto_ant
            direcao[chave][1] += 1
            direcao[chave][2] += acerto_abe
            direcao[chave][3] += 1
        res = simular_diario(sig, barra)
        if res:
            for k2 in ("pess", "otim"):
                por[k2].append({**res[k2], "d": d1, "t": t, "side": sig["side"], "score": sig["score"],
                                "rank": sig["rank"], "n_dia": sig["n_dia"]})

    def grupos(trades):
        topo = [x for x in trades if x["rank"] <= TOPO]
        fundo = [x for x in trades if x["rank"] > x["n_dia"] - TOPO]
        faixas = {}
        for nome, lo, hi in (("<50", 0, 50), ("50-60", 50, 60), ("60-70", 60, 70), ("70-80", 70, 80), (">=80", 80, 101)):
            faixas[nome] = estat([x for x in trades if lo <= x["score"] < hi])
        por_dia = defaultdict(list)
        for x in topo:
            por_dia[x["d"]].append(x)
        pts, dd = curva_r(por_dia)
        return {"topo10": estat(topo), "todos": estat(trades), "ultimos10": estat(fundo),
                "topo10_compra": estat([x for x in topo if x["side"] == "C"]),
                "topo10_venda": estat([x for x in topo if x["side"] == "V"]),
                "por_faixa_de_nota": faixas, "curva_R_topo10": pts, "drawdown_max_R": dd}

    acoes = {"pessimista": grupos(por["pess"]), "otimista": grupos(por["otim"])}
    dias_sinal = sorted({s[0] for s in sinais})
    acoes["dias_simulados"] = len(dias_sinal)
    acoes["operacoes_por_dia_topo10"] = round(
        sum(1 for x in por["pess"] if x["rank"] <= TOPO) / max(1, len(dias_sinal)), 2)
    acoes["direcao"] = {
        "topo10_fechou_na_direcao_vs_fechamento_anterior_pct": round(100 * direcao["topo"][0] / max(1, direcao["topo"][1]), 1),
        "topo10_fechou_na_direcao_vs_abertura_pct": round(100 * direcao["topo"][2] / max(1, direcao["topo"][3]), 1),
        "todos_vs_fechamento_anterior_pct": round(100 * direcao["todos"][0] / max(1, direcao["todos"][1]), 1),
        "todos_vs_abertura_pct": round(100 * direcao["todos"][2] / max(1, direcao["todos"][3]), 1),
    }

    # --- simulação com candles de 5 minutos (ordem exata), últimos ~60 dias
    intra = {"disponivel": False}
    try:
        b5 = {}
        for t in diario:
            try:
                barras = baixar(t + ".SA", "60d", "5m")
            except Exception:  # noqa: BLE001
                continue
            dia = defaultdict(list)
            for b in barras:
                dia[b["d"]].append(b)
            b5[t] = {d: sorted(v, key=lambda x: x["hm"]) for d, v in dia.items() if len(v) >= 40}
            time.sleep(pausa)
        hoje = datetime.now(ad.TZ_BR)
        trades5 = []
        for d1, t, sig, barra, base in sinais:
            barras = b5.get(t, {}).get(d1)
            if not barras:
                continue
            if d1 == hoje.date().isoformat() and (hoje.hour, hoje.minute) < (18, 20):
                continue
            res = simular_5min(sig, barras)
            if res:
                trades5.append({**res, "d": d1, "t": t, "side": sig["side"], "score": sig["score"],
                                "rank": sig["rank"], "n_dia": sig["n_dia"]})
        dias5 = sorted({x["d"] for x in trades5})
        if trades5:
            g = grupos(trades5)
            g.pop("por_faixa_de_nota", None)
            intra = {"disponivel": True, "dias": len(dias5), "de": dias5[0], "ate": dias5[-1], **g}
    except Exception as e:  # noqa: BLE001
        intra = {"disponivel": False, "erro": str(e)[:200]}
    acoes["candles_5min"] = intra

    # --- mini índice (Ibovespa como aproximação, em pontos)
    win_trades = {"vies": {"pess": [], "otim": []}, "compra": {"pess": [], "otim": []}, "venda": {"pess": [], "otim": []}}
    for k in range(15, len(ibov) - 1):
        base, barra = ibov[k], ibov[k + 1]
        trs = [max(ibov[j]["h"] - ibov[j]["l"], abs(ibov[j]["h"] - ibov[j - 1]["c"]), abs(ibov[j]["l"] - ibov[j - 1]["c"]))
               for j in range(k - 13, k + 1)]
        niv = niveis_win({"h": base["h"], "l": base["l"], "c": base["c"], "atr": sum(trs) / 14})
        for nome, lado in (("compra", "C"), ("venda", "V")):
            res = simular_diario(niv[lado], barra)
            if not res:
                continue
            for k2 in ("pess", "otim"):
                win_trades[nome][k2].append({**res[k2], "d": barra["d"]})
                if lado == niv["vies"]:
                    win_trades["vies"][k2].append({**res[k2], "d": barra["d"]})
    win = {n: {"pessimista": estat(v["pess"]), "otimista": estat(v["otim"])} for n, v in win_trades.items()}
    win["dias"] = len(ibov) - 16

    saida = {
        "gerado_em": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "periodo": [cal[0], cal[-1]], "acoes_no_universo": len(diario),
        "parametros_acoes": PARAMS_ACAO, "parametros_win": PARAMS_WIN,
        "acoes": acoes, "win_ibovespa": win, "falhas_download": falhas[:20],
        "avisos": [
            "Sem custos, corretagem, impostos ou deslizamento (slippage).",
            "Universo atual de ações: empresas que saíram da bolsa ou mudaram de código não entram (viés de sobrevivência).",
            "Candles diários: ordem entre stop e alvo desconhecida; o resultado real fica entre o pessimista e o otimista.",
            "Mini índice simulado sobre o Ibovespa em pontos, sem o ajuste do contrato futuro.",
        ],
    }
    return saida


def relatorio(s):
    a, p, o, c5 = s["acoes"], s["acoes"]["pessimista"], s["acoes"]["otimista"], s["acoes"]["candles_5min"]
    L = [f"# Simulado da Mesa do Dia", "",
         f"Período: {s['periodo'][0]} a {s['periodo'][1]} · {a['dias_simulados']} pregões · {s['acoes_no_universo']} ações · "
         f"gerado em {s['gerado_em']}", "",
         "## Top 10 ações (candles diários)", "",
         "| Cenário | Operações | Chegou ao alvo 1 | Parou no stop | Saiu no fechamento | R médio | Fator de lucro |",
         "|---|---|---|---|---|---|---|"]
    for nome, g in (("Pessimista", p["topo10"]), ("Otimista", o["topo10"])):
        L.append(f"| {nome} | {g['n']} | {g['alvo1_pct']}% | {g['stop_pct']}% | {g['fechamento_pct']}% | {g['R_medio']} | {g['fator_lucro']} |")
    if c5.get("disponivel"):
        g = c5["topo10"]
        L += ["", f"## Top 10 ações (candles de 5 min, ordem exata, {c5['dias']} pregões de {c5['de']} a {c5['ate']})", "",
              f"Operações {g['n']} · alvo 1 {g['alvo1_pct']}% · stop {g['stop_pct']}% · fechamento {g['fechamento_pct']}% · "
              f"R médio {g['R_medio']} · fator de lucro {g['fator_lucro']}"]
    d = a["direcao"]
    L += ["", "## Direção", "",
          f"Top 10 fechou na direção sugerida: {d['topo10_fechou_na_direcao_vs_fechamento_anterior_pct']}% (vs. fechamento anterior), "
          f"{d['topo10_fechou_na_direcao_vs_abertura_pct']}% (vs. abertura do dia). "
          f"Todas as ações líquidas: {d['todos_vs_fechamento_anterior_pct']}% e {d['todos_vs_abertura_pct']}%.", "",
          "## A nota funciona?", "", "| Faixa de nota | Operações | Alvo 1 | Stop | R médio |", "|---|---|---|---|---|"]
    for k, g in p["por_faixa_de_nota"].items():
        if g["n"]:
            L.append(f"| {k} | {g['n']} | {g['alvo1_pct']}% | {g['stop_pct']}% | {g['R_medio']} |")
    L += ["", f"Top 10 vs. últimos 10 (pessimista): R médio {p['topo10']['R_medio']} vs. {p['ultimos10']['R_medio']}.",
          "", "## Mini índice (Ibovespa em pontos)", "", "| Escolha | Cenário | Operações | Alvo 1 | Stop | R médio |", "|---|---|---|---|---|---|"]
    for nome, rot in (("vies", "Lado do viés"), ("compra", "Sempre compra"), ("venda", "Sempre venda")):
        for cen in ("pessimista", "otimista"):
            g = s["win_ibovespa"][nome][cen]
            if g["n"]:
                L.append(f"| {rot} | {cen} | {g['n']} | {g['alvo1_pct']}% | {g['stop_pct']}% | {g['R_medio']} |")
    L += ["", "## Avisos", ""] + [f"- {x}" for x in s["avisos"]]
    return "\n".join(L) + "\n"


def exportar_dados(baixar=baixar_yf, pausa=0.4, log=print, pasta=None):
    """Guarda os dados brutos (diário de 5 anos e 5 min de 60 dias) para pesquisa de filtros."""
    import csv
    import gzip
    pasta = pasta or (PASTA / "dados")
    pasta.mkdir(exist_ok=True)
    simbolos = ["^BVSP"] + [t + ".SA" for t in ad.TICKERS]
    hoje = datetime.now(ad.TZ_BR)
    n_d = n_5 = 0
    with gzip.open(pasta / "diario.csv.gz", "wt", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ticker", "data", "o", "h", "l", "c", "v"])
        for s in simbolos:
            try:
                cs = sem_hoje_incompleto(baixar(s, "5y", "1d"))
            except Exception as e:  # noqa: BLE001
                log(f"diário {s}: {e}")
                continue
            for c in cs:
                w.writerow([s.replace(".SA", ""), c["d"], round(c["o"], 2), round(c["h"], 2), round(c["l"], 2),
                            round(c["c"], 2), int(c["v"])])
                n_d += 1
            time.sleep(pausa)
    with gzip.open(pasta / "intraday5.csv.gz", "wt", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ticker", "data", "hora", "o", "h", "l", "c", "v"])
        for s in simbolos:
            try:
                barras = baixar(s, "60d", "5m")
            except Exception as e:  # noqa: BLE001
                log(f"5min {s}: {e}")
                continue
            for b in barras:
                if b["d"] == hoje.date().isoformat() and (hoje.hour, hoje.minute) < (18, 20):
                    continue
                w.writerow([s.replace(".SA", ""), b["d"], f"{b['hm'][0]:02d}:{b['hm'][1]:02d}", round(b["o"], 2),
                            round(b["h"], 2), round(b["l"], 2), round(b["c"], 2), int(b["v"])])
                n_5 += 1
            time.sleep(pausa)
    log(f"Dados brutos salvos: {n_d} candles diários, {n_5} candles de 5 min.")
    return n_d, n_5


def salvar_dados_no_git(log=print):
    """No GitHub Actions, grava a pasta dados/ no repositório."""
    import os
    import subprocess
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    try:
        subprocess.run(["git", "config", "user.name", "robo-mesa-do-dia"], check=True)
        subprocess.run(["git", "config", "user.email", "actions@users.noreply.github.com"], check=True)
        subprocess.run(["git", "add", "dados"], check=True)
        if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode != 0:
            subprocess.run(["git", "commit", "-m", "Dados brutos para pesquisa"], check=True)
            subprocess.run(["git", "push"], check=True)
            log("Dados brutos enviados ao repositório.")
    except Exception as e:  # noqa: BLE001
        log("Não consegui salvar os dados brutos no repositório:", e)


def main():
    try:
        n_d, n_5 = exportar_dados()
        if n_d:
            salvar_dados_no_git()
    except Exception as e:  # noqa: BLE001
        print("Exportação dos dados brutos falhou:", e, file=sys.stderr)
    try:
        s = executar()
    except Exception as e:  # noqa: BLE001
        print("Falha no simulado:", e, file=sys.stderr)
        return 1
    (PASTA / "backtest_resultado.json").write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")
    texto = relatorio(s)
    (PASTA / "backtest_relatorio.md").write_text(texto, encoding="utf-8")
    print(texto)
    return 0


if __name__ == "__main__":
    sys.exit(main())
