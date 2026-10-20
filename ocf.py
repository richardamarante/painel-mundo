"""Leitor do arquivo ICON da Open Climate Fix (zarr por rodada) para o caminho de geração do gerar.py.

Cópia pública, sem login: https://data.source.coop/bkr/icon/icon/AAAA/M/D/AAAAMMDD_HH.zarr.zip (o original fica em
huggingface.co/datasets/openclimatefix/dwd-icon-global, com acesso por conta). Uma rodada por zip sem compressão
(~42 GB), zarr v2, cada variável `(step, values)` em float32 na grade icosaédrica nativa (2.949.120 células, na mesma
ordem do GRIB), blocos de 37 passos x 122.500 células, blosc2/zstd. Vai de 21/03/2023 00 UTC a 12/08/2025 00 UTC, com
três buracos: 28-29/05/2023, 16/03 a 04/06/2024 e 27/01 a 03/04/2025 (zips que faltam ou que têm 0 byte).

Não se baixa o zip: o índice dele (~240 KB) diz onde está cada bloco, e cada bloco do passo 0-36 sai com um pedido de
faixa (HTTP Range). As horas 1 a 6 de uma rodada pedem o bloco 0 das 14 variáveis, ~2 GB por rodada.

O float32 da OCF é o GRIB decodificado pelo cfgrib (float64 do eccodes, arredondado para float32). O GRIB guarda cada
valor como Y = (R + X * 2^E) * 10^-D, com X inteiro; quando o passo 2^E * 10^-D é maior que o arredondamento do float32
(o caso de todas as 14 variáveis), o X de cada célula volta sem ambiguidade e o float64 reconstruído é exatamente o que o
eccodes dá ao `gerar.ler_grib`. Com o mesmo float64, o remapeamento, os `quadro_*` e o WebP saem byte a byte iguais. A
reconstrução confere a si mesma: arredondado de volta para float32, o valor tem de ser o da OCF em todas as células.
Quando não fecha (ou o passo é fino demais para garantir), o campo segue com o float32 promovido a float64 e conta em
`inexatos` (os códigos do quadro podem mudar em células isoladas, na fronteira de um degrau de quantização).

O campo baixado fica no cache do pedaço, no lugar do GRIB: `<cache>/<rodada>/<var>/<hhh>.ocf.npy` (float32 cru). O
`instalar()` faz o `gerar.ler_grib` e o `gerar.baixar` reconhecerem esse arquivo, então o gerar.py roda sem mudar uma
linha.
"""

from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import json
import os
import struct
import time
import urllib.error
import urllib.request
import zipfile

import numpy as np

BASE = "https://data.source.coop/bkr/icon/icon"
PRIMEIRA_RODADA = "2023032100"
ULTIMA_RODADA = "2025081200"  # passos até 72 h: as horas cobertas vão até 15/08/2025
# buracos de verdade (mais de 72 h sem rodada ou zips de 0 byte), do relatório icon-historico.md
BURACOS = [("2023-05-28T07", "2023-05-29T23"), ("2024-03-16T19", "2024-06-04T23"), ("2025-01-27T01", "2025-04-03T23")]
AGENTE = {"User-Agent": "painel-mundo/1.0"}
VALORES = 2949120
FALTANTE = 9999.0  # o que o eccodes devolve numa célula sem valor (bitmap); na OCF é NaN

_LEITOR_GRIB = None
_BAIXAR = None
CONTADORES = {"exatos": 0, "inexatos": 0, "constantes": 0}


def url_rodada(rodada: str) -> str:
    a, m, d = int(rodada[:4]), int(rodada[4:6]), int(rodada[6:8])
    return f"{BASE}/{a}/{m}/{d}/{rodada[:8]}_{rodada[8:]}.zarr.zip"


def caminho_npy(caminho_grib: str) -> str:
    return caminho_grib[: -len(".grib2.bz2")] + ".ocf.npy" if caminho_grib.endswith(".grib2.bz2") else caminho_grib + ".ocf.npy"


def cobre(rodada: str) -> bool:
    return PRIMEIRA_RODADA <= rodada <= ULTIMA_RODADA


# --- HTTP por faixa -------------------------------------------------------------------------------------------


def _pedir(url: str, inicio: int | None = None, fim: int | None = None, metodo: str = "GET", tentativas: int = 8):
    h = dict(AGENTE)
    if inicio is not None:
        h["Range"] = f"bytes={inicio}-{fim}"
    ultimo = None
    for k in range(tentativas):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, method=metodo, headers=h), timeout=120) as r:
                return r.headers, (r.read() if metodo == "GET" else b"")
        except urllib.error.HTTPError as e:
            if e.code in (403, 404, 416):
                raise
            ultimo = e
        except Exception as e:  # rede instável
            ultimo = e
        time.sleep(2 + 3 * k)
    raise RuntimeError(f"falha ao pedir {url} [{inicio}-{fim}]: {ultimo}")


def tamanho(url: str) -> int:
    """Tamanho do zip (0 se não existe)."""
    try:
        cab, _ = _pedir(url, metodo="HEAD")
    except urllib.error.HTTPError:
        return 0
    return int(cab.get("Content-Length") or 0)


class _Faixa:
    """Arquivo remoto só para o zipfile ler o índice (poucos pedidos, com leitura adiantada)."""

    def __init__(self, url: str, n: int):
        self.url, self.n, self.pos, self.buf, self.buf0 = url, n, 0, b"", 0

    def seekable(self):
        return True

    def readable(self):
        return True

    def seek(self, o, w=0):
        self.pos = o if w == 0 else (self.pos + o if w == 1 else self.n + o)
        return self.pos

    def tell(self):
        return self.pos

    def read(self, k=-1):
        if k < 0:
            k = self.n - self.pos
        if not (self.buf0 <= self.pos and self.pos + k <= self.buf0 + len(self.buf)):
            ini = max(0, min(self.pos, self.n - (1 << 20)) if k < (1 << 20) else self.pos)
            fim = min(self.n, max(self.pos + k, ini + (1 << 20))) - 1
            _, self.buf = _pedir(self.url, ini, fim)
            self.buf0 = ini
        a = self.pos - self.buf0
        d = self.buf[a : a + k]
        self.pos += len(d)
        return d


class Rodada:
    """Índice de um zarr.zip remoto: metadados do zarr e onde fica cada bloco."""

    def __init__(self, rodada: str, n: int | None = None):
        self.rodada = rodada
        self.url = url_rodada(rodada)
        self.n = n if n is not None else tamanho(self.url)
        if self.n < 1 << 30:
            raise FileNotFoundError(f"{self.url}: {self.n} bytes")
        z = zipfile.ZipFile(_Faixa(self.url, self.n))
        self.info = {i.filename: i for i in z.infolist()}
        self.meta = json.loads(z.read(".zmetadata"))["metadata"]
        t = np.frombuffer(z.read("time/0"), "<i8")[0]
        inicio = dt.datetime(1970, 1, 1) + dt.timedelta(microseconds=int(t) // 1000)
        if inicio.strftime("%Y%m%d%H") != rodada:
            raise ValueError(f"{self.url}: o zarr diz {inicio}, não {rodada}")
        self.passos = self._passos(z)

    def _passos(self, z) -> list[float]:
        import numcodecs

        za = self.meta["step/.zarray"]
        bruto = z.read("step/0")
        if za["compressor"]:
            bruto = numcodecs.get_codec(za["compressor"]).decode(bruto)
        return np.frombuffer(bruto, "<f8").tolist()

    def bloco(self, nome: str) -> bytes:
        """Bytes comprimidos de um bloco (um pedido de faixa; o cabeçalho local do zip vem junto e é descartado)."""
        i = self.info[nome]
        _, d = _pedir(self.url, i.header_offset, i.header_offset + 30 + len(nome) + 256 + i.compress_size - 1)
        n, m = struct.unpack_from("<HH", d, 26)
        dados = d[30 + n + m : 30 + n + m + i.compress_size]
        if len(dados) != i.compress_size:
            raise RuntimeError(f"{nome}: bloco incompleto")
        return dados

    def campos(self, var: str, horas: list[int], conexoes: int = 8) -> dict[int, np.ndarray]:
        """{hora: float32 (VALORES,)} das horas pedidas, do bloco de passos onde elas caem."""
        import blosc2

        za = self.meta[f"{var}/.zarray"]
        (ns, nv), (cs, cv) = za["shape"], za["chunks"]
        if nv != VALORES or za["dtype"] != "<f4" or za["order"] != "C" or za["filters"]:
            raise ValueError(f"{var}: formato inesperado {za}")
        linhas = {h: self.passos.index(float(h)) for h in horas}
        blocos_t = sorted({k // cs for k in linhas.values()})
        saida = {h: np.empty(VALORES, np.float32) for h in horas}

        def um(arg):
            bt, j = arg
            bruto = blosc2.decompress(self.bloco(f"{var}/{bt}.{j}"))
            a = np.frombuffer(bruto, "<f4").reshape(cs, cv)
            w = min(cv, nv - j * cv)
            for h, k in linhas.items():
                if k // cs == bt:
                    saida[h][j * cv : j * cv + w] = a[k - bt * cs, :w]

        nj = -(-nv // cv)
        with cf.ThreadPoolExecutor(conexoes) as ex:
            list(ex.map(um, [(bt, j) for bt in blocos_t for j in range(nj)]))
        return saida


def baixar_rodada(rodada: str, pedidos: list[tuple[str, int]], cache: str, caminho_cache, conexoes: int = 16) -> int:
    """Baixa os (variável, hora) pedidos de uma rodada para o cache, cada um em `caminho_npy(caminho_cache(...))`.

    Devolve os bytes baixados. Levanta FileNotFoundError se a rodada não está no arquivo.
    """
    r = Rodada(rodada)
    por_var: dict[str, list[int]] = {}
    for v, h in pedidos:
        if not os.path.exists(caminho_npy(caminho_cache(cache, rodada, v, h))):
            por_var.setdefault(v, []).append(h)
    total = 0
    # algumas variáveis ao mesmo tempo, cada uma com alguns blocos em paralelo
    def var_inteira(v):
        nonlocal total
        cs = r.meta[f"{v}/.zarray"]["chunks"][0]
        total += sum(r.info[n].compress_size for n in r.info if n.startswith(f"{v}/") and n.split("/")[1].split(".")[0] in {str(r.passos.index(float(h)) // cs) for h in por_var[v]})
        for h, a in r.campos(v, por_var[v], conexoes=max(2, conexoes // 4)).items():
            destino = caminho_npy(caminho_cache(cache, rodada, v, h))
            os.makedirs(os.path.dirname(destino), exist_ok=True)
            np.save(destino + ".tmp.npy", a)
            os.replace(destino + ".tmp.npy", destino)

    with cf.ThreadPoolExecutor(4) as ex:
        list(ex.map(var_inteira, sorted(por_var)))
    return total


# --- float32 -> float64 do eccodes ----------------------------------------------------------------------------


def reconstruir(v32: np.ndarray) -> tuple[np.ndarray, str]:
    """O float64 que o eccodes deu antes do cfgrib arredondar para float32. Devolve (valores, 'exato'|'constante'|'inexato')."""
    ok = ~np.isnan(v32)
    x32 = v32[ok] if not ok.all() else v32
    if x32.size == 0:
        return np.full(v32.shape, FALTANTE), "constante"
    vmin, vmax = float(x32.min()), float(x32.max())
    saida = np.full(v32.shape, FALTANTE)
    if vmin == vmax:
        saida[ok] = vmin
        return saida, "constante"
    # R é float32 no GRIB e é o mínimo do campo (X = 0); com D = 0 (o DWD não usa escala decimal nessas variáveis) o
    # menor valor é o próprio R. O passo 2^E: do mais grosso que cabe na faixa ao mais fino, o primeiro que reproduz o
    # float32 em todas as células.
    R = vmin
    x = x32.astype(np.float64) - R
    ulp = float(np.spacing(np.float32(max(abs(vmin), abs(vmax)))))
    e = int(np.ceil(np.log2(vmax - vmin)))
    while 2.0**e > ulp:
        passo = 2.0**e
        X = np.rint(x / passo)
        y = X * passo + R
        if np.array_equal(y.astype(np.float32), x32):
            saida[ok] = y
            return saida, "exato"
        e -= 1
    saida[ok] = x32.astype(np.float64)
    return saida, "inexato"


def ler_npy(caminho: str) -> np.ndarray:
    v, estado = reconstruir(np.load(caminho))
    CONTADORES[{"exato": "exatos", "inexato": "inexatos", "constante": "constantes"}[estado]] += 1
    return v


def instalar() -> None:
    """`gerar.ler_grib` e `gerar.baixar` passam a aceitar o campo da OCF no lugar do GRIB."""
    import gerar

    global _LEITOR_GRIB, _BAIXAR
    if _LEITOR_GRIB is not None:
        return
    _LEITOR_GRIB, _BAIXAR = gerar.ler_grib, gerar.baixar

    def ler_grib(caminho: str):
        p = caminho_npy(caminho)
        return ler_npy(p) if os.path.exists(p) else _LEITOR_GRIB(caminho)

    def baixar(url: str, destino: str, *a, **k):
        if os.path.exists(caminho_npy(destino)):
            return
        return _BAIXAR(url, destino, *a, **k)

    gerar.ler_grib = ler_grib
    gerar.baixar = baixar
