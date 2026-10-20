"""Histórico do modo Mundo: o passado recente (e, mais para trás, o passado em passo diário) que vem antes da previsão.

A previsão é a rodada mais recente do ICON, em `mundo/<rodada>/<camada>/<hhh>.webp`. O passado é montado com as
primeiras horas de cada rodada que já saiu de cena (a análise e as primeiras horas de previsão são o que o modelo
sabia do tempo que estava passando), copiadas para chaves organizadas por dia:

    mundo/historico/AAAA/MM/DD/<camada>/HH.webp     um quadro por hora, na grade da previsão (0,125 grau)
    mundo/historico/AAAA/MM/DD/<camada>/dia.webp    um quadro por dia, média do dia, em 0,5 grau

O manifesto lista a série em trechos (`serie.trechos`), cada um com o passo, a grade, o modelo da URL do quadro e as
lacunas: o cliente monta a linha do tempo a partir deles, então um trecho novo (outra grade, outro passo) entra sem
mudar o cliente, e o histórico pode crescer sem que o manifesto cresça com ele.

Por que as horas 1 a 6 de cada rodada e não 0 a 5: a chuva é a diferença entre dois acumulados e a hora 0 não tem
acumulado anterior (o quadro sai zerado), o que piscaria a chuva a cada 6 horas; as horas 1 a 6 também cobrem as 6
horas até a rodada seguinte, sem buraco. A rajada máxima do DWD também só existe a partir da hora 1.

Nada aqui apaga o que não foi pedido: `chaves_a_podar` só devolve quadros horários de dias que já têm o quadro diário
completo, e quem apaga (enviar.py) só o faz com --podar-horario-dias.
"""

from __future__ import annotations

import datetime as dt
import io
import re
from typing import Iterable

import numpy as np

from gerar import CAMADAS, DX, LAT0, LON0, NX, NY, codigo

HORAS_ANALISE = (1, 2, 3, 4, 5, 6)
RAIZ = "historico"
URL_HORARIA = RAIZ + "/{aaaa}/{mm}/{dd}/{camada}/{hh}"
URL_DIARIA = RAIZ + "/{aaaa}/{mm}/{dd}/{camada}/dia"

# Grade do quadro diário: blocos de 4 x 4 células da grade de 0,125 grau (média). O centro do primeiro bloco fica
# a 1,5 célula do canto: lon0 = -180 + 1,5 * 0,125 e lat0 = 85,125 - 1,5 * 0,125.
FATOR_DIARIO = 4
GRADE_DIARIA = {
    "lon0": LON0 + 1.5 * DX,
    "lat0": LAT0 - 1.5 * DX,
    "dx": DX * FATOR_DIARIO,
    "dy": DX * FATOR_DIARIO,
    "nx": NX // FATOR_DIARIO,
    "ny": NY // FATOR_DIARIO,
}
# a quantidade mínima de horas para o dia ter média (rodadas faltando deixam buracos; abaixo disso o dia fica sem quadro)
MINIMO_DE_HORAS_NO_DIA = 20

UTC = dt.timezone.utc


def iso(t: dt.datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def instante_da_rodada(rodada: str, hora: int) -> dt.datetime:
    return dt.datetime.strptime(rodada, "%Y%m%d%H").replace(tzinfo=UTC) + dt.timedelta(hours=hora)


def chave_horaria(prefixo: str, t: dt.datetime, camada: str, ext: str) -> str:
    return f"{prefixo}/" + URL_HORARIA.format(aaaa=f"{t:%Y}", mm=f"{t:%m}", dd=f"{t:%d}", camada=camada, hh=f"{t:%H}") + f".{ext}"


def chave_diaria(prefixo: str, dia: dt.date, camada: str, ext: str) -> str:
    return f"{prefixo}/" + URL_DIARIA.format(aaaa=f"{dia:%Y}", mm=f"{dia:%m}", dd=f"{dia:%d}", camada=camada) + f".{ext}"


_RE_RODADA = re.compile(r"^(?P<p>.+?)/(?P<r>\d{10})/(?P<c>[^/]+)/(?P<h>\d{3})\.(?P<e>[a-z]+)$")
_RE_HIST = re.compile(r"^(?P<p>.+?)/historico/(?P<a>\d{4})/(?P<m>\d{2})/(?P<d>\d{2})/(?P<c>[^/]+)/(?P<q>\d{2}|dia)\.(?P<e>[a-z]+)$")


def rodadas_no_bucket(chaves: Iterable[str]) -> list[str]:
    return sorted({m["r"] for k in chaves if (m := _RE_RODADA.match(k))})


def horas_da_rodada(chaves: Iterable[str], rodada: str) -> dict[str, set[int]]:
    """camada -> horas de previsão presentes."""
    saida: dict[str, set[int]] = {}
    for k in chaves:
        m = _RE_RODADA.match(k)
        if m and m["r"] == rodada:
            saida.setdefault(m["c"], set()).add(int(m["h"]))
    return saida


def quadros_do_historico(chaves: Iterable[str]) -> tuple[dict[dt.datetime, set[str]], dict[dt.date, set[str]]]:
    """(hora -> camadas, dia -> camadas) dos quadros do histórico que estão no bucket."""
    horarios: dict[dt.datetime, set[str]] = {}
    diarios: dict[dt.date, set[str]] = {}
    for k in chaves:
        m = _RE_HIST.match(k)
        if not m:
            continue
        dia = dt.date(int(m["a"]), int(m["m"]), int(m["d"]))
        if m["q"] == "dia":
            diarios.setdefault(dia, set()).add(m["c"])
        else:
            t = dt.datetime(dia.year, dia.month, dia.day, int(m["q"]), tzinfo=UTC)
            horarios.setdefault(t, set()).add(m["c"])
    return horarios, diarios


def copias_a_fazer(existentes: dict[str, int], prefixo: str, atual: str, camadas: list[str], ext: str) -> list[tuple[str, str]]:
    """
    Pares (origem, destino) das horas 1 a 6 de cada rodada anterior à `atual` que ainda não estão no histórico.
    Só vale a hora que existe em todas as camadas (senão o quadro do histórico ficaria sem uma delas).
    """
    pares: list[tuple[str, str]] = []
    for rodada in rodadas_no_bucket(existentes):
        if rodada >= atual:
            continue
        horas = horas_da_rodada(existentes, rodada)
        for h in HORAS_ANALISE:
            if not all(h in horas.get(c, ()) for c in camadas):
                continue
            t = instante_da_rodada(rodada, h)
            for c in camadas:
                origem = f"{prefixo}/{rodada}/{c}/{h:03d}.{ext}"
                destino = chave_horaria(prefixo, t, c, ext)
                if existentes.get(destino) != existentes.get(origem):
                    pares.append((origem, destino))
    return pares


def _lacunas(esperados: list[dt.datetime], presentes: set[dt.datetime]) -> list[list[str]]:
    saida: list[list[str]] = []
    inicio = fim = None
    for t in esperados:
        if t in presentes:
            if inicio is not None:
                saida.append([iso(inicio), iso(fim)])
                inicio = fim = None
        else:
            inicio = inicio or t
            fim = t
    return saida


def montar_trechos(
    horarios: dict[dt.datetime, set[str]],
    diarios: dict[dt.date, set[str]],
    camadas: list[str],
    formato: str,
) -> list[dict]:
    """
    Trechos do manifesto, do mais antigo ao mais novo. Só entra o instante que tem todas as camadas. `lacunas` lista os
    intervalos (inclusivos) sem quadro dentro de cada trecho.
    """
    todas = set(camadas)
    trechos: list[dict] = []
    dias = sorted(d for d, cs in diarios.items() if todas <= cs)
    if dias:
        meio_dia = lambda d: dt.datetime(d.year, d.month, d.day, 12, tzinfo=UTC)
        esperados = [meio_dia(dias[0] + dt.timedelta(days=i)) for i in range((dias[-1] - dias[0]).days + 1)]
        trechos.append(
            {
                "id": "diario",
                "passo_h": 24,
                "inicio": iso(esperados[0]),
                "fim": iso(esperados[-1]),
                "grade": GRADE_DIARIA,
                "formato": formato,
                "url": URL_DIARIA,
                "lacunas": _lacunas(esperados, {meio_dia(d) for d in dias})
            }
        )
    horas = sorted(t for t, cs in horarios.items() if todas <= cs)
    if horas:
        esperados = [horas[0] + dt.timedelta(hours=i) for i in range(int((horas[-1] - horas[0]).total_seconds() // 3600) + 1)]
        trechos.append(
            {
                "id": "horario",
                "passo_h": 1,
                "inicio": iso(horas[0]),
                "fim": iso(horas[-1]),
                "formato": formato,
                "url": URL_HORARIA,
                "lacunas": _lacunas(esperados, set(horas)),
            }
        )
    return trechos


def serie_do_manifesto(existentes: dict[str, int], camadas: list[str], formato: str) -> dict:
    horarios, diarios = quadros_do_historico(existentes)
    return {"trechos": montar_trechos(horarios, diarios, camadas, formato)}


def chaves_a_podar(existentes: dict[str, int], camadas: list[str], limite: dt.datetime) -> list[str]:
    """
    Quadros horários anteriores a `limite`, só dos dias que já têm o quadro diário em todas as camadas: o histórico
    horário vira diário e o horário velho sai. Dia sem quadro diário nunca perde as horas.
    """
    horarios, diarios = quadros_do_historico(existentes)
    com_dia = {d for d, cs in diarios.items() if set(camadas) <= cs}
    velhas = {t for t in horarios if t < limite and t.date() in com_dia}
    return [k for k in existentes if (m := _RE_HIST.match(k)) and m["q"] != "dia" and dt.datetime(int(m["a"]), int(m["m"]), int(m["d"]), int(m["q"]), tzinfo=UTC) in velhas]


# --- média diária ---------------------------------------------------------------------------------------------


def _decodificar_imagem(dados: bytes) -> np.ndarray:
    from PIL import Image

    return np.asarray(Image.open(io.BytesIO(dados)).convert("RGB"))


def _codigo_para_valor(n: np.ndarray, vmin: float, vmax: float, bits: int, raiz: bool) -> np.ndarray:
    x = n.astype(np.float64) / ((1 << bits) - 1)
    if raiz:
        x = x * x
    return vmin + x * (vmax - vmin)


def _escalar(rgb: np.ndarray, c: dict) -> np.ndarray:
    c16 = (rgb[..., 0].astype(np.uint32) << 8) | rgb[..., 1].astype(np.uint32)
    return _codigo_para_valor(c16 >> (16 - c["bits"]), c["min"], c["max"], c["bits"], bool(c.get("raiz")))


def reduzir_blocos(campo: np.ndarray, fator: int = FATOR_DIARIO, como: str = "media") -> np.ndarray:
    ny, nx = campo.shape[0] // fator, campo.shape[1] // fator
    b = campo[: ny * fator, : nx * fator].reshape(ny, fator, nx, fator)
    return b.max(axis=(1, 3)) if como == "max" else b.mean(axis=(1, 3))


def media_do_dia(camada: str, quadros: list[np.ndarray]) -> np.ndarray:
    """
    Quadro diário (RGB, na grade GRADE_DIARIA) de uma camada a partir dos quadros horários (RGB, grade de 0,125 grau).
    A média é feita no valor físico, não no código: escalares e componentes do vento pela média; rajada pelo máximo do
    dia; chuva pela taxa média (mm/h) com a fração de neve ponderada pela taxa.
    """
    c = CAMADAS[camada]
    if camada == "vento":
        u = np.mean([_codigo_para_valor(q[..., 0], c["min"], c["max"], 8, False) for q in quadros], axis=0)
        v = np.mean([_codigo_para_valor(q[..., 1], c["min"], c["max"], 8, False) for q in quadros], axis=0)
        rajada = np.max([_codigo_para_valor(q[..., 2], c["gmin"], c["gmax"], 8, False) for q in quadros], axis=0)
        rgb = np.zeros((*reduzir_blocos(u).shape, 3), dtype=np.uint8)
        rgb[..., 0] = codigo(reduzir_blocos(u), c["min"], c["max"], 8) >> 8
        rgb[..., 1] = codigo(reduzir_blocos(v), c["min"], c["max"], 8) >> 8
        rgb[..., 2] = codigo(reduzir_blocos(rajada, como="max"), c["gmin"], c["gmax"], 8) >> 8
        return rgb
    if camada == "chuva":
        taxa = np.stack([(q[..., 0].astype(np.float64) / 255.0) ** 2 * c["max"] for q in quadros])
        neve = np.stack([q[..., 1].astype(np.float64) / 255.0 for q in quadros])
        media = taxa.mean(axis=0)
        peso = taxa.sum(axis=0)
        fracao = np.divide((neve * taxa).sum(axis=0), peso, out=np.zeros_like(peso), where=peso > 1e-9)
        m, f = reduzir_blocos(media), reduzir_blocos(fracao)
        rgb = np.zeros((*m.shape, 3), dtype=np.uint8)
        rgb[..., 0] = np.clip(np.rint(np.sqrt(np.clip(m, 0, c["max"]) / c["max"]) * 255), 0, 255).astype(np.uint8)
        rgb[..., 1] = np.clip(np.rint(f * 255), 0, 255).astype(np.uint8)
        return rgb
    valor = np.mean([_escalar(q, c) for q in quadros], axis=0)
    c16 = codigo(reduzir_blocos(valor), c["min"], c["max"], c["bits"], bool(c.get("raiz")))
    rgb = np.zeros((*c16.shape, 3), dtype=np.uint8)
    rgb[..., 0] = c16 >> 8
    rgb[..., 1] = c16 & 255
    return rgb


def codificar_imagem(rgb: np.ndarray, formato: str) -> bytes:
    from PIL import Image

    saida = io.BytesIO()
    im = Image.fromarray(rgb, "RGB")
    if formato == "webp":
        im.save(saida, format="WEBP", lossless=True, quality=100, method=4)
    else:
        im.save(saida, format="PNG", compress_level=6)
    return saida.getvalue()


def dias_a_compactar(existentes: dict[str, int], camadas: list[str], hoje: dt.date) -> list[dt.date]:
    """Dias já encerrados (antes de `hoje`) com horas suficientes em todas as camadas e sem o quadro diário completo."""
    horarios, diarios = quadros_do_historico(existentes)
    por_dia: dict[dt.date, dict[str, int]] = {}
    for t, cs in horarios.items():
        for c in cs:
            por_dia.setdefault(t.date(), {}).setdefault(c, 0)
            por_dia[t.date()][c] += 1
    saida = []
    for d, contagem in sorted(por_dia.items()):
        if d >= hoje or set(camadas) <= diarios.get(d, set()):
            continue
        if all(contagem.get(c, 0) >= MINIMO_DE_HORAS_NO_DIA for c in camadas):
            saida.append(d)
    return saida


def compactar_dia(s3, bucket: str, prefixo: str, dia: dt.date, camadas: list[str], formato: str) -> int:
    """Baixa as horas do dia, grava `dia.<formato>` de cada camada e devolve quantos quadros diários foram gravados."""
    tipo = "image/webp" if formato == "webp" else "image/png"
    n = 0
    for c in camadas:
        quadros = []
        for h in range(24):
            chave = chave_horaria(prefixo, dt.datetime(dia.year, dia.month, dia.day, h, tzinfo=UTC), c, formato)
            try:
                quadros.append(_decodificar_imagem(s3.get_object(Bucket=bucket, Key=chave)["Body"].read()))
            except Exception:  # hora que falta no dia: a média usa as que há (o mínimo foi conferido antes)
                continue
        if len(quadros) < MINIMO_DE_HORAS_NO_DIA:
            continue
        corpo = codificar_imagem(media_do_dia(c, quadros), formato)
        s3.put_object(Bucket=bucket, Key=chave_diaria(prefixo, dia, c, formato), Body=corpo, ContentType=tipo)
        n += 1
    return n


# --- arquivo horário no Hugging Face (arquivo.py) -------------------------------------------------------------

ARQUIVO_HF = "richardmelo/painel-nacional-icon"


def url_do_arquivo_hf(repo: str = ARQUIVO_HF) -> str:
    return f"https://huggingface.co/datasets/{repo}/resolve/main/" + "{camada}/{aaaa}/{aaaa}-{mm}.bin"


def ler_meses_do_hf(repo: str = ARQUIVO_HF, tempo: int = 30) -> list[dict]:
    """Os `meses/<AAAA-MM>.json` do dataset (público: sem token)."""
    import json
    import urllib.request

    with urllib.request.urlopen(f"https://huggingface.co/api/datasets/{repo}/tree/main/meses", timeout=tempo) as r:
        arquivos = [x["path"] for x in json.load(r) if x.get("path", "").endswith(".json")]
    saida = []
    for caminho in sorted(arquivos):
        with urllib.request.urlopen(f"https://huggingface.co/datasets/{repo}/resolve/main/{caminho}", timeout=tempo) as r:
            saida.append(json.load(r))
    return saida


def trecho_do_arquivo(meses: list[dict], url: str, formato: str = "webp") -> dict | None:
    """
    Trecho do manifesto para o arquivo horário: um só, do primeiro ao último instante com quadro em todos os meses, com
    as horas que faltam em `lacunas`. Vai no fim de `serie.trechos`: onde ele cruza com o horário do R2, o do R2 vence
    (no empate de passo o cliente fica com o primeiro trecho), que sai num pedido só e já está no cache do navegador.
    """
    instantes: set[dt.datetime] = set()
    for m in meses:
        ano, mes = map(int, m["mes"].split("-"))
        inicio = dt.datetime(ano, mes, 1, tzinfo=UTC)
        instantes |= {inicio + dt.timedelta(hours=h) for h in m.get("horas", [])}
    if not instantes:
        return None
    horas = sorted(instantes)
    esperados = [horas[0] + dt.timedelta(hours=i) for i in range(int((horas[-1] - horas[0]).total_seconds() // 3600) + 1)]
    return {
        "id": "arquivo",
        "passo_h": 1,
        "inicio": iso(horas[0]),
        "fim": iso(horas[-1]),
        "formato": formato,
        "arquivo": "pna1",
        "url": url,
        "lacunas": _lacunas(esperados, instantes),
    }
