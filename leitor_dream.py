"""Leitor da reanálise ICON-DREAM-Global do DWD para o arquivo horário: 2015-01 até a OCF (21/03/2023) e a lacuna
entre o fim da OCF (15/08/2025) e o começo das cópias brutas do opendata (12/03/2026). Também tapa os buracos da OCF.

A DREAM no opendata é contínua: em 20/10/2026 o servidor tinha de 2010-01 a 2026-09 em todas as variáveis usadas aqui,
com um mês novo por mês (`cobre` e `tem_dados`).

Entrega ao caminho de geração (`gerar.processar_passo`) os mesmos campos que o ICON operacional entrega, com as mesmas
siglas, unidades e alturas, na mesma grade nativa: a DREAM roda na grade icosaédrica R03B07 do ICON global
(`numberOfGridUsed` 26, 2.949.120 células, o mesmo `uuidOfHGrid` dos `clat`/`clon` do opendata). Os pesos
`pesos-0125.npz` do cache servem sem refazer, e o quadro sai pelos mesmos `quadro_*` e o mesmo WebP.

Fonte: https://opendata.dwd.de/climate_environment/REA/ICON-DREAM-Global/hourly/<VAR>/ICON-DREAM-Global_<AAAAMM>_<VAR>_hourly.grb
(CC BY 4.0; Valmassoi et al. 2025, doi:10.5676/dwd/icon-dream_v1). Um GRIB2 por variável e mês, uma mensagem por hora,
CCSDS. Ciclos de 3 h (00, 03, ..., 21 UTC) com previsões de +1 a +3 h: a hora 00 UTC é o +3 do ciclo de 21 UTC.

Como cada campo do gerar.py sai da DREAM:

    t_2m td_2m pmsl clct u_10m v_10m   mesma sigla, mesma unidade (K, Pa, %, m/s), a 2 m e 10 m, instantâneo
    vmax_10m   máximo desde o início do ciclo de 3 h: janela de 1, 2 ou 3 h (o operacional é 1 h). Fica como vem.
    tot_prec   a DREAM grava a chuva de cada hora (passo k-1..k, kg/m²); aqui vira o acumulado desde a rodada,
               para que a diferença que o gerar.py faz (passo h menos passo anterior) devolva a hora exata
    relhum_2m  não está no servidor: calculada de T_2M e TD_2M com a pressão de saturação sobre água do ICON
               (Tetens, 610,78 Pa, 17,269, 35,86 K), a mesma que o ICON usa para tirar a TD_2M da umidade
    snow_gsp snow_con   não existem: zero, então a fração de neve da chuva sai 0
    tqv cape_ml h_snow  não existem em nenhum passo (nem no diário nem no mensal): as camadas `agua`, `cape` e `neve`
               não são geradas para esse trecho (CAMADAS_DREAM)

Ligação com o gerar.py: `instalar()` troca `gerar.ler_grib` por `ler_grib`, que reconhece o caminho do cache
(`<cache>/<rodada>/<var>/<hhh>.grib2.bz2`) e devolve o campo da DREAM válido em rodada + hhh. A rodada aqui é só um
rótulo (00, 06, 12, 18 UTC, horas 1 a 6, como o operacional), então o `juntar` do arquivo_lote monta o mês igual.
Antes, `preparar(cache, rodada, passos)` baixa por faixa (HTTP Range) só as mensagens das horas pedidas.

Uso de teste:
    python leitor_dream.py --rodada 2019011500 --passos 1-6 --cache DIR --saida DIR
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import functools
import json
import os
import re
import struct
import threading
import time

import numpy as np

BASE_DREAM = "https://opendata.dwd.de/climate_environment/REA/ICON-DREAM-Global/hourly"
ATRIBUICAO_DREAM = "Dados: DWD (ICON-DREAM-Global, reanálise), CC BY 4.0, doi:10.5676/dwd/icon-dream_v1"
UTC = dt.timezone.utc
INICIO = dt.datetime(2015, 1, 1, tzinfo=UTC)  # começo do arquivo horário do painel (a DREAM tem desde 2010-01)
ULTIMO_MES = (2026, 9)  # último mês visto no servidor em 20/10/2026; `tem_dados` pergunta ao servidor depois dele
INICIO_OCF = dt.datetime(2023, 3, 21, tzinfo=UTC)  # ocf.PRIMEIRA_RODADA
FIM_OCF = dt.datetime(2025, 8, 12, 6, tzinfo=UTC)  # a rodada depois de ocf.ULTIMA_RODADA (o arquivo usa as horas 1 a 6)
INICIO_RAW = dt.datetime(2026, 3, 12, tzinfo=UTC)  # primeira cópia bruta do opendata no source.coop
N_CELULAS = 2949120
GRADE_ICON = 26  # numberOfGridUsed do ICON global R03B07

# sigla do gerar.py -> pasta e sigla da DREAM
DIRETAS = {
    "t_2m": "T_2M",
    "td_2m": "TD_2M",
    "pmsl": "PMSL",
    "clct": "CLCT",
    "u_10m": "U_10M",
    "v_10m": "V_10M",
    "vmax_10m": "VMAX_10M",
    "tot_prec": "TOT_PREC",
}
ZERADAS = ("snow_gsp", "snow_con")
AUSENTES = ("tqv", "cape_ml", "h_snow")
VARIAVEIS_DREAM = sorted(set(DIRETAS.values()))

# camadas do gerar.py que saem da DREAM (as outras precisam de uma variável que a DREAM não tem)
CAMADAS_DREAM = ["t2m", "td2m", "ur2m", "pnm", "nuvens", "sensacao", "vento", "chuva"]

# pressão de saturação sobre água do ICON (mo_thdyn_functions.sat_pres_water)
C1ES, C3LES, C4LES, TMELT = 610.78, 17.269, 35.86, 273.15

_local = threading.local()
_meses_no_servidor: dict[tuple[int, int], bool] = {}


def tem_dados(rodada: str, passos: list[int] | None = None) -> bool:
    """A DREAM tem as horas desta rodada-rótulo? (todas as variáveis saem juntas, então basta a T_2M do mês)."""
    r = dt.datetime.strptime(rodada, "%Y%m%d%H").replace(tzinfo=UTC)
    horas = [r + dt.timedelta(hours=h) for h in (passos or [1, 6])]
    if min(horas) < dt.datetime(2010, 1, 1, 1, tzinfo=UTC):
        return False
    for t in horas:
        am = (t.year, t.month)
        if am <= ULTIMO_MES:
            continue
        if am not in _meses_no_servidor:
            try:
                r_ = _sessao().head(url_mes("T_2M", *am), timeout=60)
                _meses_no_servidor[am] = r_.status_code == 200
            except Exception:
                _meses_no_servidor[am] = False
        if not _meses_no_servidor[am]:
            return False
    return True


_RESPONDE: dict[str, bool] = {}


def responde() -> bool:
    """O opendata do DWD entrega o corpo dos arquivos da DREAM? Desde 20/10/2026 ~09 UTC ele manda o cabeçalho e corta
    o corpo de tudo em ICON-DREAM-Global (o HEAD dá 200). Um pedido de 64 KB do GRIB de um mês, com poucas tentativas;
    o resultado vale para o processo inteiro, para cada rodada não esperar os ~10 min de tentativas do `_faixa`."""
    if "ok" not in _RESPONDE:
        try:
            _RESPONDE["ok"] = len(_faixa(url_mes("T_2M", 2020, 1), 0, (1 << 16) - 1, tentativas=4)) == 1 << 16
        except Exception as e:
            print(f"aviso: a DREAM não responde ({e}); as rodadas dela ficam sem cópia", flush=True)
            _RESPONDE["ok"] = False
    return _RESPONDE["ok"]


def cobre(rodada: str) -> bool:
    """Rodadas em que a DREAM é a fonte do arquivo: antes da OCF (desde 2015) e entre o fim da OCF e as cópias brutas."""
    r = dt.datetime.strptime(rodada, "%Y%m%d%H").replace(tzinfo=UTC)
    return INICIO - dt.timedelta(hours=6) <= r < INICIO_OCF or FIM_OCF <= r < INICIO_RAW
_travas: dict[tuple, threading.Lock] = {}
_trava_geral = threading.Lock()


def _trava(chave: tuple) -> threading.Lock:
    with _trava_geral:
        return _travas.setdefault(chave, threading.Lock())


def _sessao():
    import requests

    if not hasattr(_local, "s"):
        _local.s = requests.Session()
        _local.s.headers["User-Agent"] = "painel-mundo/1.0"
    return _local.s


def url_mes(var_dream: str, ano: int, mes: int) -> str:
    return f"{BASE_DREAM}/{var_dream}/ICON-DREAM-Global_{ano:04d}{mes:02d}_{var_dream}_hourly.grb"


def _faixa(url: str, a: int, b: int, tentativas: int = 14) -> bytes:
    # O opendata do DWD derruba conexões e responde 503 quando muitos jobs pedem ao mesmo tempo (20 jobs x 32 conexões
    # derrubaram 2/3 dos dias de 2015-01): espera crescente com sorteio, até ~1 min, e respeita o Retry-After.
    import random

    ultimo: Exception | None = None
    for k in range(tentativas):
        espera = min(60.0, 2.0 * 1.6**k) * (0.5 + random.random())
        try:
            r = _sessao().get(url, headers={"Range": f"bytes={a}-{b}"}, timeout=120)
            if r.status_code == 416:
                return b""
            if r.status_code in (429, 503) and r.headers.get("Retry-After", "").isdigit():
                espera = max(espera, float(r.headers["Retry-After"]))
            r.raise_for_status()
            if r.status_code != 206:
                raise RuntimeError(f"o servidor não respondeu por faixa ({r.status_code})")
            return r.content
        except Exception as e:  # rede instável ou servidor cheio: espera e tenta de novo
            ultimo = e
            _local.__dict__.pop("s", None)  # conexão derrubada: sessão nova na próxima tentativa
            time.sleep(espera)
    raise RuntimeError(f"falha na faixa {a}-{b} de {url}: {ultimo}")


# --- cabeçalho GRIB2 sem decodificar os dados -------------------------------------------------------------------


def ler_cabecalho(dados: bytes) -> dict:
    """Seções 0 a 4 de uma mensagem GRIB2 (os primeiros KB bastam): tamanho, validade, parâmetro e janela do passo.

    Validade: no gabarito 4.0, referência + passo; no 4.8 (acumulado, máximo), o fim do intervalo estatístico.
    """
    if dados[:4] != b"GRIB" or dados[7] != 2:
        raise ValueError("não é GRIB2")
    total = struct.unpack(">Q", dados[8:16])[0]
    pos = 16
    ref = sec4 = sec3 = None
    while pos + 5 <= len(dados) and sec4 is None:
        n, num = struct.unpack(">IB", dados[pos : pos + 5])
        if pos + n > len(dados):
            raise ValueError("cabeçalho cortado: peça mais bytes")
        s = dados[pos : pos + n]
        if num == 1:
            y, mo, d, h, mi, se = struct.unpack(">HBBBBB", s[12:19])
            ref = dt.datetime(y, mo, d, h, mi, se, tzinfo=UTC)
        elif num == 3:
            sec3 = s
        elif num == 4:
            sec4 = s
        pos += n
    if ref is None or sec4 is None:
        raise ValueError("cabeçalho cortado: peça mais bytes")
    modelo = struct.unpack(">H", sec4[7:9])[0]
    cat, par = sec4[9], sec4[10]
    unidade = sec4[17]
    passo = struct.unpack(">I", sec4[18:22])[0]
    fator = {0: 60, 1: 3600, 13: 1}[unidade]
    inicio_passo = ref + dt.timedelta(seconds=passo * fator)
    if modelo == 8:
        y, mo, d, h, mi, se = struct.unpack(">HBBBBB", sec4[34:41])
        validade = dt.datetime(y, mo, d, h, mi, se, tzinfo=UTC)
    elif modelo == 0:
        validade = inicio_passo
    else:
        raise ValueError(f"gabarito 4.{modelo} não previsto")
    grade = None
    if sec3 is not None and struct.unpack(">H", sec3[12:14])[0] == 101:
        grade = int.from_bytes(sec3[15:18], "big")
    return {
        "tamanho": total,
        "referencia": ref,
        "validade": validade,
        "parametro": (dados[6], cat, par),
        "janela_h": (validade - ref).total_seconds() / 3600 - (passo * fator / 3600 if modelo == 8 else 0),
        "grade": grade,
    }


# --- índice de cada arquivo mensal -----------------------------------------------------------------------------


def _caminho_indice(cache: str, var_dream: str, ano: int, mes: int) -> str:
    return os.path.join(cache, "dream", "indice", f"{var_dream}_{ano:04d}{mes:02d}.json")


def indice_mes(cache: str, var_dream: str, ano: int, mes: int, ate: dt.datetime | None = None) -> dict[str, list[int]]:
    """{validade 'AAAAMMDDHH': [offset, tamanho]} das mensagens do arquivo mensal, por faixas pequenas.

    O arquivo não tem índice e cada mensagem só diz o próprio tamanho. Para chegar à validade `ate` sem andar o mês de
    uma em uma (0,6 s por pedido daqui), o leitor salta: estima o offset pelo tamanho médio das mensagens já vistas
    (quase constante dentro de uma variável), baixa uma janela em volta, acha o começo de mensagem ("GRIB", edição 2,
    tamanho plausível, a seguinte também começa com "GRIB") e anda dali. O índice fica em cache (JSON), esparso; sem
    `ate`, anda até o fim do arquivo.
    """
    with _trava((cache, var_dream, ano, mes)):
        return _indice_mes(cache, var_dream, ano, mes, ate)


def _achar_inicio(url: str, alvo: int, media: float) -> int | None:
    """O começo de mensagem mais perto de `alvo`, numa janela que cresce até achar."""
    for meia in (262_144, 1_048_576, int(1.2 * media)):
        a = max(0, alvo - meia)
        dados = _faixa(url, a, alvo + meia + 4096)
        achados = []
        i = dados.find(b"GRIB")
        while i >= 0:
            if i + 4096 <= len(dados) and dados[i + 7] == 2:  # os 2 bytes reservados vêm 0xffff na DREAM
                try:
                    c = ler_cabecalho(dados[i : i + 4096])
                    if 0.5 * media < c["tamanho"] < 2 * media and c["grade"] in (None, GRADE_ICON):
                        achados.append(a + i)
                except (ValueError, KeyError, struct.error):
                    pass
            i = dados.find(b"GRIB", i + 1)
        for p in sorted(achados, key=lambda p: abs(p - alvo)):
            c = ler_cabecalho(_faixa(url, p, p + 4095))
            seguinte = _faixa(url, p + c["tamanho"], p + c["tamanho"] + 15)
            if seguinte[:4] == b"GRIB" or not seguinte:
                return p
    return None


def _indice_mes(cache: str, var_dream: str, ano: int, mes: int, ate: dt.datetime | None) -> dict[str, list[int]]:
    destino = _caminho_indice(cache, var_dream, ano, mes)
    estado = {"completo": False, "msgs": {}}
    if os.path.exists(destino):
        with open(destino, encoding="utf-8") as f:
            estado = json.load(f)
    msgs = estado["msgs"]
    chave_ate = ate.strftime("%Y%m%d%H") if ate is not None else None
    if estado["completo"] or (chave_ate is not None and chave_ate in msgs):
        return msgs
    url = url_mes(var_dream, ano, mes)
    antes = [k for k in msgs if chave_ate is None or k < chave_ate]
    if antes:
        k = max(antes)
        pos, ultimo = msgs[k][0] + msgs[k][1], dt.datetime.strptime(k, "%Y%m%d%H").replace(tzinfo=UTC)
    else:
        pos, ultimo = 0, None
    saltou = False
    while True:
        if ate is not None and ultimo is not None and msgs:
            falta = int((ate - ultimo).total_seconds() // 3600) - 1  # mensagens entre a última vista e a pedida
            if falta > 3:
                media = sum(n for _, n in msgs.values()) / len(msgs)
                alvo = int(pos + (falta - 1) * media)
                p = None
                for _ in range(5):  # o tamanho das mensagens varia (CCSDS): se o salto passou da hora, recua
                    p = _achar_inicio(url, alvo, media)
                    if p is None or p <= pos:
                        p = None
                        break
                    c = ler_cabecalho(_faixa(url, p, p + 4095))
                    msgs[c["validade"].strftime("%Y%m%d%H")] = [p, c["tamanho"]]
                    if c["validade"] <= ate:
                        break
                    passou = int((c["validade"] - ate).total_seconds() // 3600) + 2
                    alvo, p = int(alvo - passou * media), None
                    if alvo <= pos:
                        break
                if p is not None:
                    pos, saltou = p, True
                else:
                    ultimo = None  # sem salto: anda de uma em uma daqui em diante
        cab = _faixa(url, pos, pos + 4095)
        if not cab:
            estado["completo"] = ate is None and not antes  # andou do começo ao fim
            break
        c = ler_cabecalho(cab)
        if c["grade"] not in (None, GRADE_ICON):
            raise RuntimeError(f"{url}: grade {c['grade']}, esperava {GRADE_ICON}")
        msgs[c["validade"].strftime("%Y%m%d%H")] = [pos, c["tamanho"]]
        pos += c["tamanho"]
        ultimo = c["validade"]
        if ate is not None and c["validade"] >= ate:
            break
    os.makedirs(os.path.dirname(destino), exist_ok=True)
    with open(destino + ".tmp", "w", encoding="utf-8") as f:
        json.dump(estado, f)
    os.replace(destino + ".tmp", destino)
    return msgs


def _meses_candidatos(t: dt.datetime) -> list[tuple[int, int]]:
    """A mensagem válida em t costuma estar no arquivo do mês de t; a da chuva das 00 UTC do dia 1 está no anterior."""
    a = (t.year, t.month)
    u = t - dt.timedelta(hours=1)
    b = (u.year, u.month)
    return [a] if a == b else [a, b]


def caminho_msg(cache: str, var_dream: str, t: dt.datetime) -> str:
    return os.path.join(cache, "dream", "msg", var_dream, f"{t:%Y%m%d%H}.grb")


def baixar_msg(cache: str, var_dream: str, t: dt.datetime) -> str:
    destino = caminho_msg(cache, var_dream, t)
    if os.path.exists(destino) and os.path.getsize(destino) > 0:
        return destino
    chave = t.strftime("%Y%m%d%H")
    for ano, mes in _meses_candidatos(t):
        msgs = indice_mes(cache, var_dream, ano, mes, ate=t)
        if chave in msgs:
            off, n = msgs[chave]
            dados = _faixa(url_mes(var_dream, ano, mes), off, off + n - 1)
            if len(dados) != n or dados[:4] != b"GRIB" or dados[-4:] != b"7777":
                raise RuntimeError(f"mensagem incompleta: {var_dream} {chave}")
            os.makedirs(os.path.dirname(destino), exist_ok=True)
            with open(destino + ".part", "wb") as f:
                f.write(dados)
            os.replace(destino + ".part", destino)
            return destino
    raise KeyError(f"sem {var_dream} válido em {chave} na DREAM")


def horas_validas(rodada: str, passos: list[int]) -> list[dt.datetime]:
    r = dt.datetime.strptime(rodada, "%Y%m%d%H").replace(tzinfo=UTC)
    return [r + dt.timedelta(hours=h) for h in passos]


def preparar(cache: str, rodada: str, passos: list[int], conexoes: int = 16) -> int:
    """Baixa as mensagens DREAM que a rodada precisa (os passos e, para a chuva, as horas desde a rodada). Devolve bytes."""
    r = dt.datetime.strptime(rodada, "%Y%m%d%H").replace(tzinfo=UTC)
    tarefas = set()
    for t in horas_validas(rodada, passos):
        for v in VARIAVEIS_DREAM:
            tarefas.add((v, t))
    for k in range(1, max(passos) + 1):
        tarefas.add(("TOT_PREC", r + dt.timedelta(hours=k)))
    # índices primeiro (um por variável e mês, em paralelo), da hora mais cedo para a mais tarde: um salto até a
    # primeira e, dali, uma mensagem por hora; depois as mensagens
    horas_do_mes: dict[tuple, list] = {}
    for v, t in tarefas:
        for a, m in _meses_candidatos(t):
            horas_do_mes.setdefault((v, a, m), []).append(t)

    def indexar(x):
        (v, a, m), ts = x
        for t in sorted(ts):
            indice_mes(cache, v, a, m, ate=t)

    with cf.ThreadPoolExecutor(min(conexoes, len(horas_do_mes))) as ex:
        list(ex.map(indexar, sorted(horas_do_mes.items())))
    with cf.ThreadPoolExecutor(conexoes) as ex:
        caminhos = list(ex.map(lambda x: baixar_msg(cache, *x), sorted(tarefas)))
    return sum(os.path.getsize(c) for c in caminhos)


# --- campos ----------------------------------------------------------------------------------------------------


@functools.lru_cache(maxsize=24)
def _decodificar(caminho: str) -> np.ndarray:
    import eccodes

    with open(caminho, "rb") as f:
        g = eccodes.codes_new_from_message(f.read())
    try:
        if eccodes.codes_get(g, "numberOfDataPoints") != N_CELULAS or eccodes.codes_get(g, "numberOfGridUsed") != GRADE_ICON:
            raise RuntimeError(f"{caminho}: grade diferente da do ICON global")
        v = np.asarray(eccodes.codes_get_values(g), dtype=np.float64)
    finally:
        eccodes.codes_release(g)
    v.flags.writeable = False
    return v


def _esat_agua(t_k: np.ndarray) -> np.ndarray:
    return C1ES * np.exp(C3LES * (t_k - TMELT) / (t_k - C4LES))


def umidade_relativa(t_k: np.ndarray, td_k: np.ndarray) -> np.ndarray:
    """UR a 2 m (%) de T e Td, com a pressão de saturação sobre água do ICON."""
    return 100.0 * _esat_agua(td_k) / _esat_agua(t_k)


def campo(cache: str, var: str, rodada: str, passo: int) -> np.ndarray:
    """O campo `var` (sigla do gerar.py) válido em rodada + passo, na grade nativa, na unidade do opendata."""
    r = dt.datetime.strptime(rodada, "%Y%m%d%H").replace(tzinfo=UTC)
    t = r + dt.timedelta(hours=passo)
    if var in DIRETAS and var != "tot_prec":
        return _decodificar(baixar_msg(cache, DIRETAS[var], t))
    if var == "relhum_2m":
        return umidade_relativa(campo(cache, "t_2m", rodada, passo), campo(cache, "td_2m", rodada, passo))
    if var == "tot_prec":
        # acumulado desde a rodada: o gerar.py faz passo h menos passo anterior e recupera a chuva da hora h
        acc = np.zeros(N_CELULAS)
        for k in range(1, passo + 1):
            acc = acc + _decodificar(baixar_msg(cache, "TOT_PREC", r + dt.timedelta(hours=k)))
        return acc
    if var in ZERADAS:
        return np.zeros(N_CELULAS)
    if var in AUSENTES:
        raise KeyError(f"{var} não existe na DREAM: a camada que o usa fica de fora (CAMADAS_DREAM)")
    raise KeyError(var)


_PADRAO = re.compile(r"(?P<cache>.*)[\\/](?P<rodada>\d{10})[\\/](?P<var>[a-z_0-9]+)[\\/](?P<passo>\d{3})\.grib2\.bz2$")


def ler_grib(caminho: str) -> np.ndarray:
    """Substituto do gerar.ler_grib: o caminho do cache vira (rodada, variável, passo) e o campo vem da DREAM."""
    m = _PADRAO.match(caminho)
    if not m:
        raise ValueError(f"caminho fora do padrão do cache: {caminho}")
    return campo(m["cache"], m["var"], m["rodada"], int(m["passo"]))


def instalar() -> None:
    import gerar

    gerar.ler_grib = ler_grib


def _iniciar(pesos: str) -> None:
    import gerar

    instalar()
    gerar._iniciar(pesos)


def gerar_rodada(cache: str, saida: str, rodada: str, passos: list[int], jobs: int = 4, camadas: list[str] | None = None, formato: str = "webp") -> None:
    """Os quadros de uma rodada-rótulo da DREAM pelo gerar.processar_passo, um passo por processo."""
    import gerar

    camadas = camadas or CAMADAS_DREAM
    pesos = os.path.join(cache, "pesos-0125.npz")
    if not os.path.exists(pesos):
        raise SystemExit(f"sem {pesos}: copie os pesos do ICON (a grade é a mesma)")
    escada = gerar.passos_de(gerar.ESCADA, int(rodada[8:]))
    anterior = {h: (escada[escada.index(h) - 1] if escada.index(h) else None) for h in passos}
    tarefas = [(cache, saida, rodada, h, anterior[h], camadas, formato) for h in passos]
    with cf.ProcessPoolExecutor(jobs, initializer=_iniciar, initargs=(pesos,)) as ex:
        for k, _ in enumerate(ex.map(gerar.processar_passo, tarefas), 1):
            print(f"  {k}/{len(tarefas)} passos", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rodada", required=True, help="rótulo AAAAMMDDHH (00, 06, 12 ou 18 UTC)")
    ap.add_argument("--passos", default="1-6")
    ap.add_argument("--cache", required=True)
    ap.add_argument("--saida", required=True)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--conexoes", type=int, default=16)
    a = ap.parse_args()
    import gerar

    passos = gerar.passos_de(a.passos, int(a.rodada[8:]))
    t0 = time.time()
    n = preparar(a.cache, a.rodada, passos, a.conexoes)
    print(f"DREAM {a.rodada} passos {passos}: {n / 1e6:.0f} MB em {time.time() - t0:.0f} s", flush=True)
    t1 = time.time()
    gerar_rodada(a.cache, a.saida, a.rodada, passos, a.jobs)
    print(f"quadros em {time.time() - t1:.0f} s", flush=True)


if __name__ == "__main__":
    main()
