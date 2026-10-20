"""Arquivo horário (arquivo.py) em pedaços: cada job do Actions gera algumas rodadas, e um passo por mês junta tudo.

Um mês inteiro num job só passava de 6 horas (124 rodadas em série). Aqui o mês vira pedaços de N rodadas (4 por
padrão, um dia), cada pedaço num job da matriz, com as N rodadas em paralelo nos 4 núcleos do runner. Cada pedaço sobe
os seus quadros como artefato; o job `juntar` do mês baixa os artefatos, grava os 11 arquivos PNA1 e envia ao HF.

O quadro sai do mesmo código do gerar.py: `gerar.processar_passo`, com o mesmo `Remapeador`, os mesmos `quadro_*` e o
mesmo WebP. A única diferença é que cada campo do GRIB é lido e remapeado uma vez por passo (a t_2m, por exemplo, entra
na t2m e na sensação), e o acumulado da hora anterior, que o passo anterior já leu, não é lido de novo. Os valores são
os mesmos, só não se repete a conta. `--conferir` gera também uma rodada pelo caminho de hoje
(`gerar.baixar_e_processar`) e compara os quadros byte a byte.

Subcomandos:
    python arquivo_lote.py planejar --meses 2026-09 [--rodadas-por-job 4]   (escreve pecas= e meses= no GITHUB_OUTPUT)
    python arquivo_lote.py gerar --mes 2026-09 --rodadas 2026090100,2026090106 --trabalho /mnt/mundo [--conferir 1]
    python arquivo_lote.py juntar --mes 2026-09 --trabalho /mnt/mundo [--enviar richardmelo/painel-nacional-icon]
"""

from __future__ import annotations

import argparse
import bz2
import collections
import concurrent.futures as cf
import datetime as dt
import glob
import hashlib
import json
import os
import shutil
import sys
import time

import arquivo
import arquivo_mes  # troca gerar.url_do pelo espelho do source.coop
import gerar

HORAS = arquivo_mes.HORAS
UTC = dt.timezone.utc
MAX_MATRIZ = 256


def faixa_do_mes(mes: str) -> tuple[int, int, dt.datetime, dt.datetime]:
    ano, m = map(int, mes.split("-"))
    inicio = arquivo.inicio_do_mes(ano, m)
    return ano, m, inicio, inicio + dt.timedelta(hours=arquivo.horas_do_mes(ano, m))


def passos_no_mes(rodada: str, inicio: dt.datetime, fim: dt.datetime) -> list[int]:
    """Horas 1 a 6 da rodada que caem dentro do mês (a 18 UTC do último dia do mês anterior só entra com a hora 6)."""
    r = dt.datetime.strptime(rodada, "%Y%m%d%H").replace(tzinfo=UTC)
    return [h for h in HORAS if inicio <= r + dt.timedelta(hours=h) < fim]


def expandir_meses(texto: str) -> list[str]:
    meses = []
    for parte in texto.replace(" ", "").split(","):
        if ".." in parte:
            a, b = parte.split("..")
            (y, m), (y2, m2) = map(int, a.split("-")), map(int, b.split("-"))
            if (y, m) > (y2, m2):
                (y, m), (y2, m2) = (y2, m2), (y, m)
            while (y, m) <= (y2, m2):
                meses.append(f"{y:04d}-{m:02d}")
                y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        elif parte:
            meses.append(parte)
    return sorted(set(meses), reverse=True)


# --- planejar -------------------------------------------------------------------------------------------------


def planejar(a: argparse.Namespace) -> None:
    agora = dt.datetime.now(UTC)
    meses = expandir_meses(a.meses)
    pecas = []
    for mes in meses:
        ano, m, inicio, fim = faixa_do_mes(mes)
        # rodadas cuja hora 1 já passou há pelo menos 8 h (a cópia do source.coop sai na hora da rodada + 8 h)
        rodadas = [r.strftime("%Y%m%d%H") for r in arquivo_mes.rodadas_do_mes(ano, m, min(fim, agora - dt.timedelta(hours=8)))]
        grupos = [rodadas[k : k + a.rodadas_por_job] for k in range(0, len(rodadas), a.rodadas_por_job)]
        if len(grupos) > 1 and len(grupos[-1]) == 1:  # uma rodada sozinha não paga o job: vai para o pedaço anterior
            grupos[-2] += grupos.pop()
        pecas += [{"mes": mes, "k": k, "rodadas": ",".join(g)} for k, g in enumerate(grupos)]
    if len(pecas) > MAX_MATRIZ:
        raise SystemExit(f"{len(pecas)} pedaços passam do limite de {MAX_MATRIZ} da matriz: dispare menos meses por vez")
    print(f"{len(meses)} meses, {len(pecas)} pedaços")
    for p in pecas:
        print(f"  {p['mes']} #{p['k']}: {p['rodadas']}")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"meses={json.dumps(meses)}\n")
            f.write(f"pecas={json.dumps(pecas)}\n")


# --- gerar ----------------------------------------------------------------------------------------------------

_T: dict[str, float] = collections.defaultdict(float)
_MEMO: dict[str, tuple[int, object, object]] = {}  # caminho do GRIB -> (passo, valores lidos, campo remapeado)


def _passo_do_caminho(caminho: str) -> int:
    nome = os.path.basename(caminho).split(".")[0]
    return -1 if nome == "invariante" else int(nome)


def _ler_grib_medido(caminho: str):
    """Igual ao gerar.ler_grib, com o tempo do bz2 e do eccodes à parte e uma leitura por arquivo."""
    import eccodes
    import numpy as np

    if caminho in _MEMO:
        return _MEMO[caminho][1]
    t0 = time.perf_counter()
    with open(caminho, "rb") as f:
        bruto = bz2.decompress(f.read())
    t1 = time.perf_counter()
    g = eccodes.codes_new_from_message(bruto)
    try:
        v = np.asarray(eccodes.codes_get_values(g), dtype=np.float64)
    finally:
        eccodes.codes_release(g)
    _T["bz2"] += t1 - t0
    _T["grib"] += time.perf_counter() - t1
    v.flags.writeable = False
    _MEMO[caminho] = (_passo_do_caminho(caminho), v, None)
    return v


class _RemapMemo:
    """Envolve o Remapeador: o campo remapeado de cada GRIB fica guardado enquanto o passo (ou o seguinte) precisar."""

    def __init__(self, rm):
        self.rm = rm
        self.por_id: dict[int, str] = {}

    def __call__(self, valores):
        caminho = next((c for c, (_, v, _r) in _MEMO.items() if v is valores), None)
        if caminho is not None and _MEMO[caminho][2] is not None:
            return _MEMO[caminho][2]
        t0 = time.perf_counter()
        g = self.rm(valores)
        _T["remap"] += time.perf_counter() - t0
        g.flags.writeable = False
        if caminho is not None:
            p, v, _ = _MEMO[caminho]
            _MEMO[caminho] = (p, v, g)
        return g


def _gravar_imagem_medido(rgb, destino, formato):
    t0 = time.perf_counter()
    n = _gravar_original(rgb, destino, formato)
    _T["webp"] += time.perf_counter() - t0
    return n


_gravar_original = gerar.gravar_imagem


def _iniciar_trabalhador(pesos: str) -> None:
    gerar._iniciar(pesos)
    gerar._REMAP = _RemapMemo(gerar._REMAP)
    gerar.ler_grib = _ler_grib_medido
    gerar.gravar_imagem = _gravar_imagem_medido


def _rodada_inteira(args: tuple) -> dict:
    """Um trabalhador faz os passos de uma rodada em ordem: o acumulado do passo anterior já está na memória."""
    cache, saida, rodada, passos, anterior, formato = args
    _T.clear()
    _MEMO.clear()
    t0 = time.perf_counter()
    for h in passos:
        manter = {h, anterior[h]}
        for c in [c for c, (p, _, _) in _MEMO.items() if p not in manter]:
            del _MEMO[c]
        t1 = time.perf_counter()
        gerar.processar_passo((cache, saida, rodada, h, anterior[h], list(gerar.CAMADAS), formato))
        _T["total"] += time.perf_counter() - t1
    _MEMO.clear()
    return {"rodada": rodada, "passos": passos, "segundos": time.perf_counter() - t0, **{k: round(v, 1) for k, v in _T.items()}}


def arquivos_da_rodada(rodada: str, passos: list[int]) -> tuple[list[tuple[str, int]], dict[int, int | None]]:
    """Os mesmos (variável, passo) que gerar.baixar_e_processar baixa para esses passos, e o passo anterior de cada um."""
    escada = gerar.passos_de(gerar.ESCADA, int(rodada[8:]))
    anterior = {h: (escada[escada.index(h) - 1] if escada.index(h) else None) for h in passos}
    vars_ = sorted({v for c in gerar.CAMADAS for v in gerar.PRECISA[c]})
    faltam: list[tuple[str, int]] = []
    for v in vars_:
        horas = {h for h in passos if not (h == 0 and v in gerar.SEM_PASSO_ZERO)}
        if v in gerar.ACUMULADAS:
            horas |= {p for p in anterior.values() if p is not None}
        faltam += [(v, h) for h in sorted(horas)]
    return faltam, anterior


def gerar_pedaco(a: argparse.Namespace) -> None:
    _, _, inicio, fim = faixa_do_mes(a.mes)
    cache = os.path.join(a.trabalho, "cache")
    saida = os.path.join(a.trabalho, "parte", "quadros")
    os.makedirs(cache, exist_ok=True)
    pesos = os.path.join(cache, "pesos-0125.npz")
    if not os.path.exists(pesos):
        raise SystemExit(f"sem {pesos}: o job dos pesos devia ter deixado no cache")
    rodadas = [r for r in a.rodadas.split(",") if r]
    t0 = time.time()

    # 1. quais rodadas têm cópia (HEAD do último passo de cada uma)
    passos = {r: passos_no_mes(r, inicio, fim) for r in rodadas}
    passos = {r: p for r, p in passos.items() if p}
    with cf.ThreadPoolExecutor(8) as ex:
        tem = dict(zip(passos, ex.map(lambda r: arquivo_mes.existe(gerar.url_do("t_2m", r, max(passos[r]))), passos)))
    sem_copia = [r for r in passos if not tem[r]]
    passos = {r: p for r, p in passos.items() if tem[r]}

    # 2. todos os GRIB do pedaço de uma vez
    tarefas, baixar = [], []
    for r, ps in passos.items():
        faltam, anterior = arquivos_da_rodada(r, ps)
        baixar += [(gerar.url_do(v, r, h), gerar.caminho_cache(cache, r, v, h)) for v, h in faltam]
        tarefas.append((cache, saida, r, ps, anterior, "webp"))
    t1 = time.time()
    with cf.ThreadPoolExecutor(a.conexoes) as ex:
        list(ex.map(lambda x: gerar.baixar(*x), baixar))
    mb = sum(os.path.getsize(d) for _, d in baixar) / 1e6
    t_baixar = time.time() - t1
    print(f"{len(baixar)} GRIB ({mb:.0f} MB) em {t_baixar:.0f} s, {len(passos)} rodadas, sem cópia: {sem_copia or 'nenhuma'}", flush=True)

    # 3. uma rodada por trabalhador
    t2 = time.time()
    medidas = []
    with cf.ProcessPoolExecutor(min(a.jobs, max(1, len(tarefas))), initializer=_iniciar_trabalhador, initargs=(pesos,)) as ex:
        for m in ex.map(_rodada_inteira, tarefas):
            medidas.append(m)
            print(f"  {m['rodada']}: {len(m['passos'])} passos em {m['segundos']:.0f} s "
                  f"(bz2 {m.get('bz2', 0)}, grib {m.get('grib', 0)}, remap {m.get('remap', 0)}, webp {m.get('webp', 0)})", flush=True)
    t_proc = time.time() - t2

    conferencia = None
    if a.conferir and tarefas:
        conferencia = conferir(tarefas[-1][2], tarefas[-1][3], cache, saida, a.trabalho)

    for r in passos:
        shutil.rmtree(os.path.join(cache, r), ignore_errors=True)
    soma = {k: round(sum(m.get(k, 0) for m in medidas), 1) for k in ("bz2", "grib", "remap", "webp", "total")}
    info = {
        "mes": a.mes,
        "rodadas": list(passos),
        "rodadas_sem_copia": sem_copia,
        "grib": len(baixar),
        "mb_baixados": round(mb),
        "segundos_baixando": round(t_baixar, 1),
        "segundos_processando": round(t_proc, 1),
        "segundos_total": round(time.time() - t0, 1),
        "cpu_por_etapa": soma,
        "conferencia": conferencia,
    }
    os.makedirs(os.path.join(a.trabalho, "parte", "partes"), exist_ok=True)
    nome = f"{a.mes}-{rodadas[0]}.json"
    with open(os.path.join(a.trabalho, "parte", "partes", nome), "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=1)
    print(json.dumps(info, ensure_ascii=False), flush=True)
    if conferencia and conferencia["diferentes"]:
        raise SystemExit(f"quadros diferentes do gerar.py: {conferencia['diferentes'][:5]}")


def _sha(caminho: str) -> str:
    with open(caminho, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def conferir(rodada: str, passos: list[int], cache: str, saida: str, trabalho: str) -> dict:
    """Gera a rodada de novo pelo caminho de hoje (gerar.baixar_e_processar, num processo limpo) e compara os bytes."""
    import subprocess

    ref = os.path.join(trabalho, "referencia")
    codigo = (
        "import sys, argparse; sys.path.insert(0, sys.argv[1]); import arquivo_mes, gerar; "
        "ns = argparse.Namespace(cache=sys.argv[2], saida=sys.argv[3], formato='webp', jobs=4, conexoes=16); "
        "gerar.baixar_e_processar(ns, sys.argv[4], list(gerar.CAMADAS), [int(x) for x in sys.argv[5].split(',')])"
    )
    t0 = time.time()
    subprocess.run([sys.executable, "-c", codigo, os.path.dirname(os.path.abspath(__file__)), cache, ref, rodada, ",".join(map(str, passos))], check=True)
    iguais, diferentes = 0, []
    for c in gerar.CAMADAS:
        for h in passos:
            nome = f"{h:03d}.webp"
            p1, p2 = os.path.join(saida, rodada, c, nome), os.path.join(ref, rodada, c, nome)
            if os.path.exists(p1) and os.path.exists(p2) and _sha(p1) == _sha(p2):
                iguais += 1
            else:
                diferentes.append(f"{c}/{nome}")
    shutil.rmtree(ref, ignore_errors=True)
    r = {"rodada": rodada, "quadros_iguais": iguais, "diferentes": diferentes, "segundos": round(time.time() - t0)}
    print(f"conferência contra o gerar.py: {r}", flush=True)
    return r


# --- juntar ---------------------------------------------------------------------------------------------------


def juntar(a: argparse.Namespace) -> None:
    ano, m, inicio, fim = faixa_do_mes(a.mes)
    n = arquivo.horas_do_mes(ano, m)
    quadros = os.path.join(a.trabalho, "parte", "quadros")
    partes = []
    for p in sorted(glob.glob(os.path.join(a.trabalho, "parte", "partes", f"{a.mes}-*.json"))):
        with open(p, encoding="utf-8") as f:
            partes.append(json.load(f))
    camadas = list(gerar.CAMADAS)
    por_hora: dict[int, dict[str, str]] = {}
    for rodada in sorted(os.listdir(quadros)) if os.path.isdir(quadros) else []:
        r = dt.datetime.strptime(rodada, "%Y%m%d%H").replace(tzinfo=UTC)
        for h in HORAS:
            t = r + dt.timedelta(hours=h)
            if not inicio <= t < fim:
                continue
            k = int((t - inicio).total_seconds() // 3600)
            for c in camadas:
                p = os.path.join(quadros, rodada, c, f"{h:03d}.webp")
                if os.path.exists(p):
                    por_hora.setdefault(k, {})[c] = p
    horas_ok = sorted(k for k, cs in por_hora.items() if len(cs) == len(camadas))

    saida = os.path.join(a.trabalho, "saida")
    tamanhos: dict[str, int] = {}
    for c in camadas:
        destino = os.path.join(saida, arquivo.caminho_no_repo(c, ano, m))
        tamanhos[c] = arquivo.gravar(destino, c, ano, m, {k: (por_hora[k][c], arquivo.ORIGEM_ICON) for k in horas_ok})
        for k in por_hora:  # libera o disco camada a camada
            if c in por_hora[k]:
                os.remove(por_hora[k][c])

    cpu = {k: round(sum(p["cpu_por_etapa"].get(k, 0) for p in partes)) for k in ("bz2", "grib", "remap", "webp", "total")}
    info = {
        "mes": a.mes,
        "formato": "pna1",
        "quadros": "webp",
        "origem": "ICON global operacional (DWD), cópias do opendata no source.coop (bkr/icon), horas 1 a 6 de cada rodada",
        "horas_no_mes": n,
        "horas": horas_ok,
        "rodadas_sem_copia": sorted(r for p in partes for r in p["rodadas_sem_copia"]),
        "bytes": tamanhos,
        "bytes_total": sum(tamanhos.values()),
        "pedacos": len(partes),
        "segundos_gerando": round(sum(p["segundos_total"] for p in partes)),
        "cpu_segundos": cpu,
        "conferencia": [p["conferencia"] for p in partes if p.get("conferencia")],
        "gerado": dt.datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    os.makedirs(os.path.join(saida, "meses"), exist_ok=True)
    with open(os.path.join(saida, "meses", f"{a.mes}.json"), "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=1)
    print(f"{a.mes}: {len(horas_ok)} de {n} horas, {info['bytes_total'] / 1e9:.2f} GB, {len(partes)} pedaços, "
          f"{info['segundos_gerando'] / 60:.0f} min de job somados; cpu {cpu}", flush=True)
    if len(horas_ok) < n and fim <= dt.datetime.now(UTC) - dt.timedelta(hours=8):
        faltam = sorted(set(range(n)) - set(horas_ok))
        print(f"  faltam {len(faltam)} horas: {faltam[:24]}{' ...' if len(faltam) > 24 else ''}", flush=True)

    if a.enviar:
        from huggingface_hub import HfApi

        api = HfApi()
        for k in range(6):
            try:
                t1 = time.time()
                api.upload_folder(repo_id=a.enviar, repo_type="dataset", folder_path=saida, commit_message=f"{a.mes}: ICON operacional")
                print(f"enviado em {time.time() - t1:.0f} s", flush=True)
                break
            except Exception as e:  # commits de vários meses ao mesmo tempo, ou o HF ocupado
                print(f"envio falhou ({e}); de novo em {30 * (k + 1)} s", flush=True)
                time.sleep(30 * (k + 1))
        else:
            raise SystemExit("não consegui enviar ao HF")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("planejar")
    p.add_argument("--meses", required=True)
    p.add_argument("--rodadas-por-job", type=int, default=4)
    p = sub.add_parser("gerar")
    p.add_argument("--mes", required=True)
    p.add_argument("--rodadas", required=True)
    p.add_argument("--trabalho", required=True)
    p.add_argument("--jobs", type=int, default=4)
    p.add_argument("--conexoes", type=int, default=32)
    p.add_argument("--conferir", type=int, default=0, help="1 = gera a última rodada também pelo gerar.py e compara")
    p = sub.add_parser("juntar")
    p.add_argument("--mes", required=True)
    p.add_argument("--trabalho", required=True)
    p.add_argument("--enviar", default="")
    a = ap.parse_args()
    {"planejar": planejar, "gerar": gerar_pedaco, "juntar": juntar}[a.cmd](a)


if __name__ == "__main__":
    main()
