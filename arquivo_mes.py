"""Gera um mês do arquivo horário (ver arquivo.py) a partir das cópias do ICON operacional e, se pedido, sobe para o HF.

Fonte: as cópias do opendata do DWD no source.coop (`bkr/icon/icon-global-raw`, de 03/2026 em diante), que guardam
cada rodada numa pasta com a hora da cópia (rodada + 8 h). O processamento é o do gerar.py, sem mudar nada: mesmo
remapeamento, mesmos `quadro_*`, mesmo WebP. Cada hora do mês vem das horas 1 a 6 da rodada que a antecede, como no
histórico do R2 (historico.py), então o quadro é o mesmo que o histórico recente teria.

Uso:
    python arquivo_mes.py --mes 2026-09 --trabalho /mnt/arquivo [--dias 2] [--enviar richardmelo/painel-nacional-icon]
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import json
import os
import shutil
import time
import urllib.error
import urllib.request

import arquivo
import gerar

ESPELHO = "https://data.source.coop/bkr/icon/icon-global-raw"
HORAS = (1, 2, 3, 4, 5, 6)
UTC = dt.timezone.utc

# o source.coop recusa o agente padrão do urllib (403)
_op = urllib.request.build_opener()
_op.addheaders = [("User-Agent", "painel-mundo/1.0")]
urllib.request.install_opener(_op)
_url_dwd = gerar.url_do


def url_espelho(var: str, rodada: str, h: int | None) -> str:
    pasta = (dt.datetime.strptime(rodada, "%Y%m%d%H") + dt.timedelta(hours=8)).strftime("%Y%m%d%H")
    return _url_dwd(var, rodada, h).replace(gerar.BASE, f"{ESPELHO}/{pasta}/opendata.dwd.de/weather/nwp/icon/grib")


gerar.url_do = url_espelho


def existe(url: str) -> bool:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=60):
            return True
    except urllib.error.HTTPError:
        return False


def rodadas_do_mes(ano: int, mes: int, fim: dt.datetime) -> list[dt.datetime]:
    """Rodadas cujas horas 1 a 6 caem no mês: da última de 18 UTC do mês anterior até a última antes do fim."""
    r = arquivo.inicio_do_mes(ano, mes) - dt.timedelta(hours=6)
    saida = []
    while r + dt.timedelta(hours=1) < fim:
        saida.append(r)
        r += dt.timedelta(hours=6)
    return saida


def processar_rodada(r: dt.datetime, a: argparse.Namespace, camadas: list[str]) -> tuple[str, bool, float]:
    rodada = r.strftime("%Y%m%d%H")
    t0 = time.time()
    if not existe(url_espelho("t_2m", rodada, 6)):
        return rodada, False, 0.0
    ns = argparse.Namespace(cache=a.cache, saida=a.quadros, formato="webp", jobs=a.jobs, conexoes=a.conexoes)
    try:
        gerar.baixar_e_processar(ns, rodada, camadas, list(HORAS))
        ok = True
    except Exception as e:  # rodada incompleta no espelho: fica de lacuna
        print(f"  {rodada}: falhou ({e})", flush=True)
        ok = False
    finally:
        shutil.rmtree(os.path.join(a.cache, rodada), ignore_errors=True)
    return rodada, ok, time.time() - t0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mes", required=True, help="AAAA-MM")
    ap.add_argument("--trabalho", required=True, help="pasta de trabalho (GRIB, quadros e arquivos do mês)")
    ap.add_argument("--dias", type=int, default=0, help="só os primeiros N dias (teste)")
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--conexoes", type=int, default=16)
    ap.add_argument("--paralelo", type=int, default=2, help="rodadas ao mesmo tempo (uma baixa enquanto a outra remapeia)")
    ap.add_argument("--enviar", default="", help="repositório do dataset no HF (ex.: richardmelo/painel-nacional-icon)")
    a = ap.parse_args()

    ano, mes = map(int, a.mes.split("-"))
    inicio = arquivo.inicio_do_mes(ano, mes)
    n = arquivo.horas_do_mes(ano, mes)
    fim = inicio + dt.timedelta(hours=n if not a.dias else min(n, 24 * a.dias))
    camadas = list(gerar.CAMADAS)
    a.cache = os.path.join(a.trabalho, "cache")
    a.quadros = os.path.join(a.trabalho, "quadros")
    saida = os.path.join(a.trabalho, "saida")
    os.makedirs(a.cache, exist_ok=True)
    pesos = os.path.join(a.cache, "pesos-0125.npz")

    rodadas = rodadas_do_mes(ano, mes, fim)
    print(f"{a.mes}: {len(rodadas)} rodadas, até {fim:%Y-%m-%d %H} UTC", flush=True)
    t0 = time.time()
    faltaram: list[str] = []
    # a primeira rodada sozinha: ela calcula os pesos de remapeamento se ainda não houver
    pendentes = list(rodadas)
    if not os.path.exists(pesos) and pendentes:
        _, ok, s = processar_rodada(pendentes[0], a, camadas)
        if not ok:
            faltaram.append(pendentes[0].strftime("%Y%m%d%H"))
        pendentes = pendentes[1:]
    with cf.ThreadPoolExecutor(a.paralelo) as ex:
        for rodada, ok, s in ex.map(lambda r: processar_rodada(r, a, camadas), pendentes):
            print(f"  {rodada}: {'ok' if ok else 'sem cópia'} em {s:.0f} s ({time.time() - t0:.0f} s no total)", flush=True)
            if not ok:
                faltaram.append(rodada)
    t_gerar = time.time() - t0

    # quadros -> hora do mês; só entra a hora que tem todas as camadas
    por_hora: dict[int, dict[str, str]] = {}
    for r in rodadas:
        rodada = r.strftime("%Y%m%d%H")
        for h in HORAS:
            t = r + dt.timedelta(hours=h)
            if not inicio <= t < fim:
                continue
            k = int((t - inicio).total_seconds() // 3600)
            for c in camadas:
                p = os.path.join(a.quadros, rodada, c, f"{h:03d}.webp")
                if os.path.exists(p):
                    por_hora.setdefault(k, {})[c] = p
    horas_ok = sorted(k for k, cs in por_hora.items() if len(cs) == len(camadas))

    tamanhos: dict[str, int] = {}
    for c in camadas:
        destino = os.path.join(saida, arquivo.caminho_no_repo(c, ano, mes))
        tamanhos[c] = arquivo.gravar(destino, c, ano, mes, {k: (por_hora[k][c], arquivo.ORIGEM_ICON) for k in horas_ok})
    shutil.rmtree(a.quadros, ignore_errors=True)

    info = {
        "mes": a.mes,
        "formato": "pna1",
        "quadros": "webp",
        "origem": "ICON global operacional (DWD), cópias do opendata no source.coop (bkr/icon), horas 1 a 6 de cada rodada",
        "horas_no_mes": n,
        "horas": horas_ok,
        "rodadas_sem_copia": faltaram,
        "bytes": tamanhos,
        "bytes_total": sum(tamanhos.values()),
        "segundos_gerando": round(t_gerar),
        "gerado": dt.datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    os.makedirs(os.path.join(saida, "meses"), exist_ok=True)
    with open(os.path.join(saida, "meses", f"{a.mes}.json"), "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=1)
    print(f"{a.mes}: {len(horas_ok)} de {n} horas, {info['bytes_total'] / 1e9:.2f} GB, {t_gerar / 60:.1f} min gerando", flush=True)

    if a.enviar:
        from huggingface_hub import HfApi

        t1 = time.time()
        HfApi().upload_folder(repo_id=a.enviar, repo_type="dataset", folder_path=saida, commit_message=f"{a.mes}: ICON operacional")
        print(f"enviado em {time.time() - t1:.0f} s", flush=True)


if __name__ == "__main__":
    main()
