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
import era5  # agua, cape e neve das horas da DREAM (o juntar não apaga)
import arquivo_mes  # troca gerar.url_do pelo espelho do source.coop
import gerar
import leitor_dream as dream  # 2015-01 a 20/03/2023 e 13/08/2025 a 11/03/2026: reanálise ICON-DREAM do DWD
import ocf  # rodadas de 21/03/2023 a 12/08/2025: arquivo zarr da Open Climate Fix

HORAS = arquivo_mes.HORAS
UTC = dt.timezone.utc
MAX_MATRIZ = 256
# WebP sem perda do arquivo: method=0 e quality=0 (o esforço do modo sem perda). Os pixels decodificados são os mesmos
# do method=4 do gerar.py, só o arquivo muda: medido numa hora das 11 camadas, 2,0 s e 22,2 MB contra 21,4 s e 13,3 MB
# (method=1 quality=0: 7,6 s e 15,6 MB). A conferência compara os códigos decodificados, não os bytes.
WEBP_METODO, WEBP_QUALIDADE = 0, 0


# Conexões ao mesmo tempo de um job com o opendata do DWD (DREAM). Com 20 jobs, 20 x 6 = 120 no servidor.
DREAM_CONEXOES = 6

def fonte(rodada: str) -> str:
    if ocf.cobre(rodada):
        return "ocf"
    return "dream" if dream.cobre(rodada) else "icon"


ORIGEM = {"icon": arquivo.ORIGEM_ICON, "ocf": arquivo.ORIGEM_OCF, "dream": arquivo.ORIGEM_DREAM}
# a DREAM não tem TQV, CAPE_ML nem H_SNOW: essas camadas ficam sem quadro nas horas dela (tamanho 0 no índice)
CAMADAS_DA_FONTE = {"icon": list(gerar.CAMADAS), "ocf": list(gerar.CAMADAS), "dream": list(dream.CAMADAS_DREAM)}
# conferência da DREAM: a mesma rodada gerada da DREAM e lida do arquivo operacional (bruto) já no Hugging Face
REF_DREAM = "2026091500"


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
            ultimo = grupos.pop()  # (grupos[-2] += grupos.pop() somaria no pedaço errado: o índice -2 muda depois do pop)
            grupos[-1] = grupos[-1] + ultimo
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
_FONTE = {"atual": "icon"}


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
    if _FONTE["atual"] == "dream":  # campo da DREAM (mensagens já baixadas por dream.preparar)
        v = dream.ler_grib(caminho)
        _T["dream"] += time.perf_counter() - t0
        v.flags.writeable = False
        _MEMO[caminho] = (_passo_do_caminho(caminho), v, None)
        return v
    if os.path.exists(ocf.caminho_npy(caminho)):  # campo da OCF (float32), de volta ao float64 do eccodes
        v = ocf.ler_npy(ocf.caminho_npy(caminho))
        _T["ocf"] += time.perf_counter() - t0
        v.flags.writeable = False
        _MEMO[caminho] = (_passo_do_caminho(caminho), v, None)
        return v
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


def gravar_webp_rapido(rgb, destino, formato):
    """O gerar.gravar_imagem com o WebP sem perda mais rápido (mesmos pixels, arquivo maior)."""
    if formato != "webp":
        return _gravar_original(rgb, destino, formato)
    from PIL import Image

    os.makedirs(os.path.dirname(destino), exist_ok=True)
    tmp = destino + ".tmp"
    Image.fromarray(rgb, "RGB").save(tmp, format="WEBP", lossless=True, quality=WEBP_QUALIDADE, method=WEBP_METODO)
    os.replace(tmp, destino)
    return os.path.getsize(destino)


def _gravar_imagem_medido(rgb, destino, formato):
    t0 = time.perf_counter()
    n = gravar_webp_rapido(rgb, destino, formato)
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
    cache, saida, rodada, passos, anterior, formato, fonte_r = args
    _T.clear()
    _MEMO.clear()
    _FONTE["atual"] = fonte_r
    t0 = time.perf_counter()
    for h in passos:
        manter = {h, anterior[h]}
        for c in [c for c, (p, _, _) in _MEMO.items() if p not in manter]:
            del _MEMO[c]
        t1 = time.perf_counter()
        gerar.processar_passo((cache, saida, rodada, h, anterior[h], CAMADAS_DA_FONTE[fonte_r], formato))
        _T["total"] += time.perf_counter() - t1
    _MEMO.clear()
    inexatos = ocf.CONTADORES["inexatos"]
    ocf.CONTADORES.update(exatos=0, inexatos=0, constantes=0)
    return {"rodada": rodada, "fonte": fonte_r, "passos": passos, "segundos": time.perf_counter() - t0, "ocf_inexatos": inexatos, **{k: round(v, 1) for k, v in _T.items()}}


def arquivos_da_rodada(rodada: str, passos: list[int], camadas: list[str] | None = None) -> tuple[list[tuple[str, int]], dict[int, int | None]]:
    """Os mesmos (variável, passo) que gerar.baixar_e_processar baixa para esses passos, e o passo anterior de cada um."""
    escada = gerar.passos_de(gerar.ESCADA, int(rodada[8:]))
    anterior = {h: (escada[escada.index(h) - 1] if escada.index(h) else None) for h in passos}
    vars_ = sorted({v for c in (camadas or gerar.CAMADAS) for v in gerar.PRECISA[c]})
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

    # 1. quais rodadas têm cópia (HEAD do último passo de cada uma, ou do zarr.zip da OCF)
    passos = {r: passos_no_mes(r, inicio, fim) for r in rodadas}
    passos = {r: p for r, p in passos.items() if p}

    fontes = {r: fonte(r) for r in passos}

    def dream_tem(r: str) -> bool:
        # a DREAM fora do ar (o opendata do DWD manda o cabeçalho e corta o corpo) vira rodada sem cópia, não dia caído
        try:
            return dream.tem_dados(r, passos[r]) and dream.responde()
        except Exception as e:
            print(f"aviso: {r}: DREAM indisponível ({e})", flush=True)
            return False

    def tem_copia(r: str) -> bool:
        try:
            if fontes[r] == "ocf":
                return ocf.tamanho(ocf.url_rodada(r)) > 1 << 30  # os buracos da OCF têm zips de 0 byte
            if fontes[r] == "dream":
                return dream_tem(r)
            return arquivo_mes.existe(gerar.url_do("t_2m", r, max(passos[r])))
        except Exception as e:
            print(f"aviso: {r}: não deu para conferir a cópia ({e})", flush=True)
            return False

    with cf.ThreadPoolExecutor(8) as ex:
        tem = dict(zip(passos, ex.map(tem_copia, passos)))
    # buraco da OCF (ou rodada bruta que falta): a DREAM tapa, sem as camadas que ela não tem
    for r in [r for r in passos if not tem[r] and fontes[r] != "dream" and dream_tem(r)]:
        fontes[r], tem[r] = "dream", True
    sem_copia = [r for r in passos if not tem[r]]
    passos = {r: p for r, p in passos.items() if tem[r]}

    # 2. todos os campos do pedaço de uma vez (GRIB do espelho, ou blocos do zarr da OCF por faixa)
    tarefas, baixar, da_ocf, da_dream = [], [], {}, []
    for r, ps in passos.items():
        faltam, anterior = arquivos_da_rodada(r, ps, CAMADAS_DA_FONTE[fontes[r]])
        if fontes[r] == "ocf":
            da_ocf[r] = faltam
        elif fontes[r] == "dream":
            da_dream.append(r)
        else:
            baixar += [(gerar.url_do(v, r, h), gerar.caminho_cache(cache, r, v, h)) for v, h in faltam]
        tarefas.append((cache, saida, r, ps, anterior, "webp", fontes[r]))
    t1 = time.time()
    mb_ocf = 0.0

    def baixar_ocf(r: str) -> float:
        # zip que sumiu, zip com o zarr de outra rodada (ocf.RodadaTrocada: 2024/4/5/..._00 diz 2024-04-08) ou leitura
        # que não fecha: a rodada vira lacuna, o dia segue
        try:
            return ocf.baixar_rodada(r, da_ocf[r], cache, gerar.caminho_cache, conexoes=max(4, a.conexoes // max(1, len(da_ocf)))) / 1e6
        except FileNotFoundError:
            return -1.0
        except Exception as e:
            print(f"aviso: {r}: zarr da OCF ignorado ({type(e).__name__}: {e})", flush=True)
            return -1.0

    def baixar_dream(r: str) -> float:  # mensagens do GRIB mensal por faixa (dream.preparar)
        # poucas conexões por job ao opendata do DWD (ver dream._faixa): DREAM_CONEXOES no total, divididas entre as rodadas
        try:
            return dream.preparar(cache, r, passos[r], conexoes=max(1, DREAM_CONEXOES // max(1, len(da_dream)))) / 1e6
        except Exception as e:
            print(f"aviso: {r}: DREAM falhou ({type(e).__name__}: {e}); a rodada fica sem cópia", flush=True)
            return -1.0

    mb_dream = 0.0
    with cf.ThreadPoolExecutor(max(1, len(da_dream))) as ex3:
        for r, n in zip(list(da_dream), ex3.map(baixar_dream, list(da_dream))):
            if n < 0:
                sem_copia.append(r)
                del passos[r]
                tarefas = [t for t in tarefas if t[2] != r]
            else:
                mb_dream += n
    def baixar_ou_falta(url: str, destino: str) -> str | None:
        """gerar.baixar, mas um 404 (GRIB que a cópia não tem) volta como falta em vez de derrubar o pedaço."""
        try:
            gerar.baixar(url, destino)
            return None
        except RuntimeError as e:
            if "404" in str(e):
                return url
            raise

    with cf.ThreadPoolExecutor(a.conexoes) as ex:
        fut = [ex.submit(baixar_ou_falta, *x) for x in baixar]
        with cf.ThreadPoolExecutor(max(1, len(da_ocf))) as ex2:
            for r, n in zip(list(da_ocf), ex2.map(baixar_ocf, list(da_ocf))):
                if n < 0:  # o zip sumiu entre o HEAD e a leitura
                    sem_copia.append(r)
                    del passos[r]
                    tarefas = [t for t in tarefas if t[2] != r]
                else:
                    mb_ocf += n
        ausentes = [u for u in (f.result() for f in fut) if u]
    # passo que o zarr da OCF não tem: o .npy não aparece no cache e o passo segue o mesmo caminho do GRIB ausente
    faltando = {d for u, d in baixar if u in set(ausentes)}
    for r, pedidos in da_ocf.items():
        if r in passos:
            sem = [(v, h) for v, h in pedidos if not os.path.exists(ocf.caminho_npy(gerar.caminho_cache(cache, r, v, h)))]
            faltando |= {gerar.caminho_cache(cache, r, v, h) for v, h in sem}
            ausentes += [f"{ocf.url_rodada(r)}#{v}+{h}" for v, h in sem]
    # GRIB ausente: só os passos que dependem dele viram lacuna (o próprio passo e, se for acumulado, o seguinte)
    horas_sem_quadro = []
    if faltando:
        novas = []
        for t in tarefas:
            r, ps, anterior = t[2], t[3], t[4]
            ruins = {h for h in ps for v in sorted({v for c in CAMADAS_DA_FONTE[t[6]] for v in gerar.PRECISA[c]})
                     if gerar.caminho_cache(cache, r, v, h) in faltando
                     or (v in gerar.ACUMULADAS and anterior[h] is not None and gerar.caminho_cache(cache, r, v, anterior[h]) in faltando)}
            horas_sem_quadro += [f"{r}+{h}" for h in sorted(ruins)]
            resto = [h for h in ps if h not in ruins]
            if resto:
                passos[r] = resto
                novas.append(t[:3] + (resto,) + t[4:])
            else:
                sem_copia.append(r)
                del passos[r]
        tarefas = novas
        print(f"aviso: {len(ausentes)} GRIB ausentes na cópia (404); passos sem quadro: {horas_sem_quadro}", flush=True)
    mb = sum(os.path.getsize(d) for _, d in baixar if os.path.exists(d)) / 1e6 + mb_ocf + mb_dream
    t_baixar = time.time() - t1
    print(f"{len(baixar)} GRIB, {sum(len(v) for v in da_ocf.values())} campos da OCF e {len(da_dream)} rodadas da DREAM ({mb:.0f} MB) em {t_baixar:.0f} s, "
          f"{len(passos)} rodadas, sem cópia: {sem_copia or 'nenhuma'}", flush=True)

    # 3. uma rodada por trabalhador
    t2 = time.time()
    medidas = []
    with cf.ProcessPoolExecutor(min(a.jobs, max(1, len(tarefas))), initializer=_iniciar_trabalhador, initargs=(pesos,)) as ex:
        for m in ex.map(_rodada_inteira, tarefas):
            medidas.append(m)
            print(f"  {m['rodada']}: {len(m['passos'])} passos em {m['segundos']:.0f} s "
                  f"[{m['fonte']}] (bz2 {m.get('bz2', 0)}, grib {m.get('grib', 0)}, ocf {m.get('ocf', 0)}, dream {m.get('dream', 0)}, remap {m.get('remap', 0)}, "
                  f"webp {m.get('webp', 0)}; campos da OCF inexatos: {m['ocf_inexatos']})", flush=True)
    t_proc = time.time() - t2

    conferencia = None
    if a.conferir and tarefas and tarefas[-1][6] == "dream":
        conferencia = conferir_dream(cache, a.trabalho, a.jobs)
    elif a.conferir and tarefas:
        conferencia = conferir(tarefas[-1][2], tarefas[-1][3], cache, saida, a.trabalho)

    for r in rodadas:
        shutil.rmtree(os.path.join(cache, r), ignore_errors=True)
    shutil.rmtree(os.path.join(cache, "dream"), ignore_errors=True)
    soma = {k: round(sum(m.get(k, 0) for m in medidas), 1) for k in ("bz2", "grib", "ocf", "dream", "remap", "webp", "total")}
    info = {
        "mes": a.mes,
        "rodadas": list(passos),
        "fontes": {r: fontes[r] for r in passos},
        "rodadas_sem_copia": sem_copia,
        "grib_ausentes": ausentes,
        "horas_sem_quadro": horas_sem_quadro,
        "ocf_campos_inexatos": sum(m["ocf_inexatos"] for m in medidas),
        "webp": {"method": WEBP_METODO, "quality": WEBP_QUALIDADE},
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
    if info["ocf_campos_inexatos"]:
        print(f"aviso: {info['ocf_campos_inexatos']} campos da OCF não voltaram exatos ao float64 do GRIB", flush=True)
    if conferencia and conferencia.get("diferentes"):
        raise SystemExit(f"quadros diferentes do gerar.py: {conferencia['diferentes'][:5]}")


def _sha(caminho: str) -> str:
    with open(caminho, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _pixels(caminho: str):
    import numpy as np
    from PIL import Image

    with Image.open(caminho) as im:
        return np.asarray(im.convert("RGB"))


def conferir(rodada: str, passos: list[int], cache: str, saida: str, trabalho: str) -> dict:
    """Gera a rodada de novo pelo caminho de hoje (gerar.baixar_e_processar, num processo limpo) e compara os quadros.

    O gerar.py grava com method=4 e o arquivo com o WebP rápido: a comparação é dos pixels decodificados (os códigos),
    e `bytes_iguais` conta os que também batem byte a byte. Rodada da OCF: o processo limpo lê o mesmo campo da OCF
    pelo `ocf.instalar()`, sem a memória de campos nem o trabalhador do arquivo_lote."""
    import subprocess

    ref = os.path.join(trabalho, "referencia")
    codigo = (
        "import sys, argparse, multiprocessing as mp; sys.path.insert(0, sys.argv[1]); import arquivo_mes, gerar, ocf; ocf.instalar(); "
        "sys.platform != 'win32' and mp.set_start_method('fork', force=True); "
        "ns = argparse.Namespace(cache=sys.argv[2], saida=sys.argv[3], formato='webp', jobs=4, conexoes=16); "
        "gerar.baixar_e_processar(ns, sys.argv[4], list(gerar.CAMADAS), [int(x) for x in sys.argv[5].split(',')])"
    )
    t0 = time.time()
    subprocess.run([sys.executable, "-c", codigo, os.path.dirname(os.path.abspath(__file__)), cache, ref, rodada, ",".join(map(str, passos))], check=True)
    iguais, bytes_iguais, diferentes, tam, tam_ref = 0, 0, [], 0, 0
    for c in gerar.CAMADAS:
        for h in passos:
            nome = f"{h:03d}.webp"
            p1, p2 = os.path.join(saida, rodada, c, nome), os.path.join(ref, rodada, c, nome)
            if os.path.exists(p1) and os.path.exists(p2) and (_pixels(p1) == _pixels(p2)).all():
                iguais += 1
                bytes_iguais += _sha(p1) == _sha(p2)
                tam, tam_ref = tam + os.path.getsize(p1), tam_ref + os.path.getsize(p2)
            else:
                diferentes.append(f"{c}/{nome}")
    shutil.rmtree(ref, ignore_errors=True)
    r = {"rodada": rodada, "fonte": fonte(rodada), "quadros_iguais": iguais, "bytes_iguais": bytes_iguais, "diferentes": diferentes,
         "bytes_arquivo": tam, "bytes_gerar_py": tam_ref, "segundos": round(time.time() - t0)}
    print(f"conferência contra o gerar.py: {r}", flush=True)
    return r


HF_BASE = "https://huggingface.co/datasets/richardmelo/painel-nacional-icon/resolve/main"


def decodificar_quadro(rgb, camada: str):
    """O valor físico de cada pixel (o inverso dos quadro_* do gerar.py), um array por canal com significado."""
    import numpy as np

    c = gerar.CAMADAS[camada]
    if c["tipo"] == "escalar":
        n = (1 << c["bits"]) - 1
        x = ((rgb[..., 0].astype(np.uint32) << 8 | rgb[..., 1]) >> (16 - c["bits"])) / n
        if c.get("raiz"):
            x = x * x
        return {camada: c["min"] + x * (c["max"] - c["min"])}
    if c["tipo"] == "vento":
        e = lambda k, a, b: a + rgb[..., k] / 255.0 * (b - a)
        return {"u": e(0, c["min"], c["max"]), "v": e(1, c["min"], c["max"]), "rajada": e(2, c["gmin"], c["gmax"])}
    return {"chuva": (rgb[..., 0] / 255.0) ** 2 * c["max"], "fracao_neve": rgb[..., 1] / 255.0}


def _quadro_hf(camada: str, t: dt.datetime) -> bytes | None:
    import requests

    url = f"{HF_BASE}/{arquivo.caminho_no_repo(camada, t.year, t.month)}"
    k = int((t - arquivo.inicio_do_mes(t.year, t.month)).total_seconds() // 3600)
    e = dream._faixa(url, arquivo.TAM_FIXO + k * arquivo.TAM_ENTRADA, arquivo.TAM_FIXO + (k + 1) * arquivo.TAM_ENTRADA - 1)
    off, n = int.from_bytes(e[:8], "little"), int.from_bytes(e[8:12], "little")
    return dream._faixa(url, off, off + n - 1) if n else None


def conferir_dream(cache: str, trabalho: str, jobs: int, rodada: str = REF_DREAM, passos: tuple = (1, 2, 3, 4, 5, 6)) -> dict:
    """Gera da DREAM uma rodada que o arquivo operacional (cópias brutas) já tem no HF e compara, camada a camada, os
    valores físicos decodificados dos dois quadros: viés, erro quadrático, correlação e a faixa de cada um. Escala e
    unidade erradas aparecem como viés de dezenas ou correlação baixa; a diferença de modelo (reanálise com assimilação
    de 3 h contra a previsão de +1 a +6 h) fica em poucos graus, hPa ou m/s. Grava um PNG lado a lado (t2m, pnm, chuva)."""
    import io

    import numpy as np
    from PIL import Image

    t0 = time.time()
    ref = os.path.join(trabalho, "conferencia_dream")
    dream.preparar(cache, rodada, list(passos))
    dream.gerar_rodada(cache, ref, rodada, list(passos), jobs=jobs)
    r0 = dt.datetime.strptime(rodada, "%Y%m%d%H").replace(tzinfo=UTC)
    por_canal: dict[str, dict] = {}
    figuras = []
    for camada in dream.CAMADAS_DREAM:
        acc: dict[str, list] = {}
        for h in passos:
            b = _quadro_hf(camada, r0 + dt.timedelta(hours=h))
            p = os.path.join(ref, rodada, camada, f"{h:03d}.webp")
            if b is None or not os.path.exists(p):
                continue
            q_op = np.asarray(Image.open(io.BytesIO(b)).convert("RGB"))
            q_dr = _pixels(p)
            if h == 3 and camada in ("t2m", "pnm", "chuva"):  # cinza do valor decodificado: DREAM à esquerda
                c = gerar.CAMADAS[camada]
                cinza = lambda q: 255 * np.clip((next(iter(decodificar_quadro(q[::4, ::4], camada).values())) - c["min"]) / (c["max"] - c["min"]), 0, 1) ** (0.5 if camada == "chuva" else 1)
                figuras.append(np.concatenate([cinza(q_dr), cinza(q_op)], axis=1).astype(np.uint8))
            for canal, v_dr in decodificar_quadro(q_dr, camada).items():
                v_op = decodificar_quadro(q_op, camada)[canal]
                acc.setdefault(canal, []).append((v_dr.ravel(), v_op.ravel()))
        for canal, pares in acc.items():
            d = np.concatenate([x for x, _ in pares])
            o = np.concatenate([y for _, y in pares])
            por_canal[canal] = {
                "media_dream": round(float(d.mean()), 3), "media_oper": round(float(o.mean()), 3),
                "p1_p99_dream": [round(float(x), 2) for x in np.percentile(d, [1, 99])],
                "p1_p99_oper": [round(float(x), 2) for x in np.percentile(o, [1, 99])],
                "vies": round(float((d - o).mean()), 3), "rmse": round(float(np.sqrt(((d - o) ** 2).mean())), 3),
                "correlacao": round(float(np.corrcoef(d, o)[0, 1]), 4) if d.std() > 0 and o.std() > 0 else None,
                "horas": len(pares),
            }
    if figuras:
        os.makedirs(os.path.join(trabalho, "parte", "conferencia"), exist_ok=True)
        Image.fromarray(np.concatenate(figuras, axis=0), "L").save(os.path.join(trabalho, "parte", "conferencia", f"dream-x-oper-{rodada}.png"))
    shutil.rmtree(ref, ignore_errors=True)
    r = {"tipo": "dream_contra_operacional", "rodada": rodada, "passos": list(passos), "canais": por_canal, "segundos": round(time.time() - t0)}
    print(f"conferência DREAM x operacional (HF): {json.dumps(r, ensure_ascii=False)}", flush=True)
    return r


# --- juntar ---------------------------------------------------------------------------------------------------


def manter_era5(camada: str, ano: int, m: int, novos: dict[int, tuple[str, int]], pasta: str, repo: str) -> tuple[dict, bool]:
    """agua, cape e neve das horas da DREAM vêm do ERA5 (era5.py, origem 4), que roda depois do lote. Se o lote do mês
    rodar de novo, o juntar não pode trocar esses quadros por vazio: as horas de origem 4 que o .bin do HF já tem e que
    o lote não gerou continuam lá (o quadro é copiado byte a byte). Devolve (quadros, pular): pular = True quando o lote
    não tem nenhum quadro da camada e o HF já tem ERA5, e aí o .bin do HF fica intacto."""
    base = f"https://huggingface.co/datasets/{repo}/resolve/main"
    idx = era5.indice_hf(camada, ano, m, base)
    if not idx:
        return novos, False
    velhos = {k: (off, tam) for k, (off, tam, org) in enumerate(idx) if tam and org == arquivo.ORIGEM_ERA5 and k not in novos}
    if not velhos:
        return novos, False
    if not novos:
        return novos, True
    os.makedirs(pasta, exist_ok=True)
    saida = dict(novos)
    for k, (off, tam) in velhos.items():
        p = os.path.join(pasta, f"{k:03d}.webp")
        with open(p, "wb") as f:
            f.write(era5.quadro_hf(camada, ano, m, off, tam, base))
        saida[k] = (p, arquivo.ORIGEM_ERA5)
    print(f"  {camada}: {len(velhos)} quadros do ERA5 mantidos do HF", flush=True)
    return saida, False


def horas_no_hf(repo: str, mes: str) -> int:
    """Horas do mês que o HF já tem (meses/AAAA-MM.json); 0 se o mês não existe lá. Erro de rede levanta: sem saber o
    que está lá, o juntar não envia."""
    import requests

    url = f"https://huggingface.co/datasets/{repo}/resolve/main/meses/{mes}.json"
    for k in range(6):
        try:
            r = requests.get(url, timeout=60)
            if r.status_code == 404:
                return 0
            r.raise_for_status()
            return len(r.json().get("horas", []))
        except Exception as e:
            ultimo = e
            time.sleep(5 * (k + 1))
    raise SystemExit(f"{mes}: não consegui ler {url} ({ultimo}); não envio sem saber o que o HF tem")


def juntar(a: argparse.Namespace) -> None:
    ano, m, inicio, fim = faixa_do_mes(a.mes)
    n = arquivo.horas_do_mes(ano, m)
    quadros = os.path.join(a.trabalho, "parte", "quadros")
    partes = []
    for p in sorted(glob.glob(os.path.join(a.trabalho, "parte", "partes", f"{a.mes}-*.json"))):
        with open(p, encoding="utf-8") as f:
            partes.append(json.load(f))
    camadas = list(gerar.CAMADAS)
    fonte_da_rodada = {r: f for p in partes for r, f in p.get("fontes", {}).items()}
    por_hora: dict[int, dict[str, str]] = {}
    fonte_da_hora: dict[int, str] = {}
    origem_da_hora: dict[int, int] = {}
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
                    fonte_da_hora[k] = fonte_da_rodada.get(rodada) or fonte(rodada)
                    origem_da_hora[k] = ORIGEM[fonte_da_hora[k]]
    horas_ok = sorted(k for k, cs in por_hora.items() if set(CAMADAS_DA_FONTE[fonte_da_hora[k]]) <= set(cs))

    saida = os.path.join(a.trabalho, "saida")
    tamanhos: dict[str, int] = {}
    era5_mantidos: dict[str, int] = {}
    for c in camadas:
        destino = os.path.join(saida, arquivo.caminho_no_repo(c, ano, m))
        novos = {k: (por_hora[k][c], origem_da_hora[k]) for k in horas_ok if c in por_hora[k]}
        if c in era5.CAMADAS_ERA5:
            novos, pular = manter_era5(c, ano, m, novos, os.path.join(a.trabalho, "era5_mantido", c), a.enviar or "richardmelo/painel-nacional-icon")
            era5_mantidos[c] = sum(1 for _, o in novos.values() if o == arquivo.ORIGEM_ERA5)
            if pular:  # nada novo e o HF já tem o ERA5: não sobe um .bin vazio por cima
                print(f"  {c}: sem quadro novo; o .bin do HF (com ERA5) fica como está", flush=True)
                continue
        tamanhos[c] = arquivo.gravar(destino, c, ano, m, novos)
        for k in por_hora:  # libera o disco camada a camada
            if c in por_hora[k]:
                os.remove(por_hora[k][c])

    cpu = {k: round(sum(p["cpu_por_etapa"].get(k, 0) for p in partes)) for k in ("bz2", "grib", "ocf", "dream", "remap", "webp", "total")}
    fontes = sorted({f for p in partes for f in p.get("fontes", {}).values()})
    origem = {
        "icon": "ICON global operacional (DWD), cópias do opendata no source.coop (bkr/icon)",
        "ocf": "ICON global operacional (DWD), arquivo zarr da Open Climate Fix (cópia no source.coop, bkr/icon/icon)",
        "dream": dream.ATRIBUICAO_DREAM + " (sem agua, cape e neve: a DREAM não tem TQV, CAPE_ML nem H_SNOW)",
    }
    info = {
        "mes": a.mes,
        "formato": "pna1",
        "quadros": "webp",
        "origem": "; ".join(origem[f] for f in fontes) + ", horas 1 a 6 de cada rodada",
        "horas_no_mes": n,
        "horas": horas_ok,
        "horas_por_fonte": dict(collections.Counter(fonte_da_hora[k] for k in horas_ok)),
        "rodadas_sem_copia": sorted(r for p in partes for r in p["rodadas_sem_copia"]),
        "grib_ausentes": sorted(u for p in partes for u in p.get("grib_ausentes", [])),
        "passos_sem_quadro": sorted(h for p in partes for h in p.get("horas_sem_quadro", [])),
        "ocf_campos_inexatos": sum(p.get("ocf_campos_inexatos", 0) for p in partes),
        "webp": sorted({json.dumps(p.get("webp", {"method": 4, "quality": 100})) for p in partes}),
        "bytes": tamanhos,
        "bytes_total": sum(tamanhos.values()),
        "pedacos": len(partes),
        "segundos_gerando": round(sum(p["segundos_total"] for p in partes)),
        "cpu_segundos": cpu,
        "conferencia": [p["conferencia"] for p in partes if p.get("conferencia")],
        "era5_mantidos": era5_mantidos,
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
        no_hf = horas_no_hf(a.enviar, a.mes)
        if not horas_ok or len(horas_ok) < no_hf:
            # um mês vazio ou menor do que o do HF apagaria horas boas (o 2015-01 subiu com 0 de 744 com a DREAM fora)
            raise SystemExit(f"{a.mes}: não enviado, {len(horas_ok)} horas novas contra {no_hf} no HF")
        from huggingface_hub import HfApi

        api = HfApi()
        for k in range(6):
            try:
                t1 = time.time()
                api.upload_folder(repo_id=a.enviar, repo_type="dataset", folder_path=saida, commit_message=f"{a.mes}: ICON ({', '.join(fontes)})")
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
