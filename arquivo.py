"""Arquivo horário do modo Global: um arquivo por camada e por mês, lido por faixa (HTTP Range) direto do navegador.

O passado de 0,125 grau, hora a hora, não cabe no R2 grátis (~13,7 MB por hora nas 11 camadas). Ele mora num dataset
público do Hugging Face, em `<camada>/<AAAA>/<AAAA-MM>.bin`. Cada arquivo tem um cabeçalho de tamanho fixo com o
índice das horas do mês e, depois, os quadros (os mesmos WebP sem perda que o gerar.py grava, byte a byte) em
sequência. O cliente lê o cabeçalho com um pedido de faixa e cada quadro com outro.

Formato v1 (little endian):

    0     4   magia b"PNA1"
    4     2   versão (1)
    6     2   horas do mês (n, até 744)
    8     2   ano
    10    1   mês
    11    1   formato dos quadros (1 = webp, 2 = png)
    12    16  camada (ASCII, completada com zeros)
    28    36  reservado (zeros)
    64    744 x 16  índice, uma entrada por hora do mês (hora 0 = dia 1 às 00 UTC):
                    offset (u64, a partir do começo do arquivo), tamanho (u32, 0 = sem quadro), origem (u8), 3 zeros
    11968 ...  quadros

O cabeçalho tem sempre 11.968 bytes, qualquer que seja o mês: o cliente pede `bytes=0-11967` sem saber nada antes.
"""

from __future__ import annotations

import calendar
import datetime as dt
import os
import struct

MAGIA = b"PNA1"
VERSAO = 1
HORAS_MAX = 744
TAM_FIXO = 64
TAM_ENTRADA = 16
TAM_CABECALHO = TAM_FIXO + HORAS_MAX * TAM_ENTRADA  # 11968
FORMATOS = {"webp": 1, "png": 2}

# origem de cada quadro (fica no índice: o cliente e quem audita sabem de onde veio cada hora)
ORIGEM_NENHUMA = 0
ORIGEM_ICON = 1  # ICON global operacional do DWD (cópias do opendata no source.coop, bkr/icon)
ORIGEM_OCF = 2  # ICON global operacional, arquivo zarr da Open Climate Fix
ORIGEM_DREAM = 3  # ICON-DREAM global (reanálise do DWD)

UTC = dt.timezone.utc


def horas_do_mes(ano: int, mes: int) -> int:
    return calendar.monthrange(ano, mes)[1] * 24


def inicio_do_mes(ano: int, mes: int) -> dt.datetime:
    return dt.datetime(ano, mes, 1, tzinfo=UTC)


def caminho_no_repo(camada: str, ano: int, mes: int) -> str:
    return f"{camada}/{ano:04d}/{ano:04d}-{mes:02d}.bin"


def gravar(destino: str, camada: str, ano: int, mes: int, quadros: dict[int, tuple[str, int]], formato: str = "webp") -> int:
    """
    Grava o arquivo do mês. `quadros` é {hora do mês: (caminho do quadro no disco, origem)}. Devolve o tamanho final.
    Os quadros são copiados sem mexer em um byte.
    """
    n = horas_do_mes(ano, mes)
    if any(not 0 <= h < n for h in quadros):
        raise ValueError("hora fora do mês")
    nome = camada.encode("ascii")
    if len(nome) > 16:
        raise ValueError("nome de camada longo demais")
    tmp = destino + ".tmp"
    os.makedirs(os.path.dirname(destino) or ".", exist_ok=True)
    indice = bytearray(HORAS_MAX * TAM_ENTRADA)
    with open(tmp, "wb") as f:
        f.write(b"\0" * TAM_CABECALHO)
        pos = TAM_CABECALHO
        for h in sorted(quadros):
            caminho, origem = quadros[h]
            with open(caminho, "rb") as q:
                dados = q.read()
            f.write(dados)
            struct.pack_into("<QIB3x", indice, h * TAM_ENTRADA, pos, len(dados), origem)
            pos += len(dados)
        fixo = struct.pack("<4sHHHBB16s36x", MAGIA, VERSAO, n, ano, mes, FORMATOS[formato], nome)
        f.seek(0)
        f.write(fixo + bytes(indice))
    os.replace(tmp, destino)
    return pos


def ler_cabecalho(dados: bytes) -> dict:
    """Cabeçalho (os primeiros TAM_CABECALHO bytes) -> {camada, ano, mes, horas, formato, indice: [(offset, tamanho, origem)]}."""
    magia, versao, n, ano, mes, formato, nome = struct.unpack_from("<4sHHHBB16s", dados, 0)
    if magia != MAGIA or versao != VERSAO:
        raise ValueError("não é um arquivo PNA1")
    indice = [struct.unpack_from("<QIB", dados, TAM_FIXO + h * TAM_ENTRADA) for h in range(n)]
    fmt = {v: k for k, v in FORMATOS.items()}[formato]
    return {"camada": nome.rstrip(b"\0").decode("ascii"), "ano": ano, "mes": mes, "horas": n, "formato": fmt, "indice": indice}


def ler_quadro(caminho: str, hora: int) -> bytes | None:
    with open(caminho, "rb") as f:
        cab = ler_cabecalho(f.read(TAM_CABECALHO))
        offset, tamanho, _ = cab["indice"][hora]
        if not tamanho:
            return None
        f.seek(offset)
        return f.read(tamanho)
