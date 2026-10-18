#!/usr/bin/env python3
"""Modo Mundo: baixa uma rodada do ICON global (DWD), remapeia para 0,125 grau e grava os quadros em PNG.

Receita e opções em tools/mundo/LEIA.md. Em resumo:

    python tools/mundo/gerar.py --saida DIR --cache DIR [--passos 0-78] [--jobs 6]

Dado de entrada: https://opendata.dwd.de/weather/nwp/icon/grib/ (CC BY 4.0, grade icosaédrica, ~13 km).
Dado de saída: DIR/manifest.json e DIR/<rodada>/<camada>/<hhh>.png (ou .webp), escala fixa por camada (declarada
no manifesto), sem perda. Nada aqui sobe para lugar nenhum: a hospedagem é decisão à parte.
"""
from __future__ import annotations

import argparse
import bz2
import concurrent.futures as cf
import datetime as dt
import json
import os
import re
import shutil
import time
import urllib.request

import numpy as np

BASE = "https://opendata.dwd.de/weather/nwp/icon/grib"
ATRIBUICAO = "Dados: DWD (ICON), CC BY 4.0"

# Grade de saída: regular, 0,125 grau. Latitude de 85,125 a -85,125 (cobre o limite do Web Mercator, 85,0511),
# longitude de -180 a 179,875. A primeira linha é o norte.
LON0, DX, NX = -180.0, 0.125, 2880
LAT0, DY, NY = 85.125, 0.125, 1363

INVARIANTES = ["clat", "clon"]

# Camadas gravadas. `bits` é a precisão do valor quantizado; a escala é fixa e vai no manifesto.
# Camada escalar: R = byte alto e G = byte baixo do código (alinhado à esquerda em 16 bits), B = 0.
CAMADAS = {
    "t2m": {"unidade": "°C", "min": -70.0, "max": 55.0, "bits": 10, "tipo": "escalar"},
    "td2m": {"unidade": "°C", "min": -80.0, "max": 40.0, "bits": 10, "tipo": "escalar"},
    "ur2m": {"unidade": "%", "min": 0.0, "max": 100.0, "bits": 8, "tipo": "escalar"},
    "pnm": {"unidade": "hPa", "min": 860.0, "max": 1090.0, "bits": 10, "tipo": "escalar"},
    "nuvens": {"unidade": "%", "min": 0.0, "max": 100.0, "bits": 6, "tipo": "escalar"},
    # temperatura aparente (Steadman, a do Bureau of Meteorology): calculada de T, umidade e vento, sem baixar nada novo
    "sensacao": {"unidade": "°C", "min": -70.0, "max": 60.0, "bits": 10, "tipo": "escalar"},
    # coluna de vapor d'água (TQV)
    "agua": {"unidade": "kg/m²", "min": 0.0, "max": 80.0, "bits": 8, "tipo": "escalar"},
    # CAPE da camada de mistura; o código guarda a raiz do valor normalizado ("raiz": o shader eleva ao quadrado)
    "cape": {"unidade": "J/kg", "min": 0.0, "max": 4000.0, "bits": 8, "tipo": "escalar", "raiz": True},
    # altura da neve no solo, também em raiz
    "neve": {"unidade": "m", "min": 0.0, "max": 4.0, "bits": 8, "tipo": "escalar", "raiz": True},
    # R = u, G = v (m/s, mesma escala), B = rajada (m/s); 8 bits cada
    "vento": {"unidade": "m/s", "min": -50.0, "max": 50.0, "gmin": 0.0, "gmax": 75.0, "bits": 8, "tipo": "vento"},
    # R = raiz da taxa (mm/h) na escala 0..max, G = fração de neve 0..1; 8 bits cada
    "chuva": {"unidade": "mm/h", "min": 0.0, "max": 64.0, "bits": 8, "tipo": "chuva"},
}

# Entradas do DWD que cada camada consome (pasta em minúsculas; a sigla do arquivo é a mesma em maiúsculas).
PRECISA = {
    "t2m": ["t_2m"],
    "td2m": ["td_2m"],
    "ur2m": ["relhum_2m"],
    "pnm": ["pmsl"],
    "nuvens": ["clct"],
    "vento": ["u_10m", "v_10m", "vmax_10m"],
    "chuva": ["tot_prec", "snow_gsp", "snow_con"],
    "sensacao": ["t_2m", "relhum_2m", "u_10m", "v_10m"],
    "agua": ["tqv"],
    "cape": ["cape_ml"],
    "neve": ["h_snow"],
}
ACUMULADAS = ("tot_prec", "snow_gsp", "snow_con")
# Escada de passos do ICON global: de hora em hora até 78 h e de 3 em 3 h até 180 h (120 h nas rodadas 06 e 18).
ESCADA = "0-78,81-180/3"
# A rajada máxima é um extremo do período: o DWD não publica o passo 0. Ali a rajada vale a velocidade do vento.
SEM_PASSO_ZERO = ("vmax_10m",)


def passos_de(texto: str, rodada_hh: int) -> list[int]:
    """'0-78,81-180/3' vira a lista de horas de previsão. Sem '/n' o passo é 1."""
    saida: list[int] = []
    for parte in texto.split(","):
        m = re.fullmatch(r"(\d+)-(\d+)(?:/(\d+))?", parte.strip())
        if not m:
            raise SystemExit(f"passos inválidos: {parte!r}")
        a, b, p = int(m[1]), int(m[2]), int(m[3] or 1)
        saida += list(range(a, b + 1, p))
    teto = 180 if rodada_hh in (0, 12) else 120
    return sorted({h for h in saida if h <= teto})


def listar(url: str) -> list[str]:
    with urllib.request.urlopen(url, timeout=60) as r:
        html = r.read().decode("utf-8", "replace")
    return re.findall(r'href="([^"]+)"', html)


def descobrir_rodada(passo_final: int = 78) -> str:
    """A rodada mais recente que já tem o passo final da precipitação (quando o DWD termina de subir)."""
    achadas: list[str] = []
    for hh in ("18", "12", "06", "00"):
        try:
            nomes = listar(f"{BASE}/{hh}/tot_prec/")
        except Exception:
            continue
        datas = {m.group(1) for n in nomes if (m := re.search(r"_(\d{10})_", n))}
        for d in datas:
            if any(re.search(rf"_{d}_{passo_final:03d}_", n) for n in nomes):
                achadas.append(d)
    if not achadas:
        raise SystemExit("nenhuma rodada completa encontrada no DWD")
    return max(achadas)


def url_do(var: str, rodada: str, h: int | None) -> str:
    hh = rodada[8:]
    sig = var.upper()
    if h is None:
        return f"{BASE}/{hh}/{var}/icon_global_icosahedral_time-invariant_{rodada}_{sig}.grib2.bz2"
    return f"{BASE}/{hh}/{var}/icon_global_icosahedral_single-level_{rodada}_{h:03d}_{sig}.grib2.bz2"


def baixar(url: str, destino: str, tentativas: int = 6) -> None:
    if os.path.exists(destino) and os.path.getsize(destino) > 0:
        return
    os.makedirs(os.path.dirname(destino), exist_ok=True)
    ultimo: Exception | None = None
    for k in range(tentativas):
        try:
            tmp = destino + ".part"
            with urllib.request.urlopen(url, timeout=120) as r, open(tmp, "wb") as f:
                shutil.copyfileobj(r, f, 1 << 20)
            os.replace(tmp, destino)
            return
        except Exception as e:  # rede instável: espera e tenta de novo
            ultimo = e
            time.sleep(2 + 3 * k)
    raise RuntimeError(f"falha ao baixar {url}: {ultimo}")


def caminho_cache(cache: str, rodada: str, var: str, h: int | None) -> str:
    nome = "invariante" if h is None else f"{h:03d}"
    return os.path.join(cache, rodada, var, f"{nome}.grib2.bz2")


def ler_grib(caminho: str) -> np.ndarray:
    import eccodes

    with open(caminho, "rb") as f:
        bruto = bz2.decompress(f.read())
    g = eccodes.codes_new_from_message(bruto)
    try:
        return np.asarray(eccodes.codes_get_values(g), dtype=np.float64)
    finally:
        eccodes.codes_release(g)


# --- remapeamento ---------------------------------------------------------------------------------------------


def construir_pesos(clat: np.ndarray, clon: np.ndarray, destino: str) -> None:
    """Pesos baricêntricos (3 por ponto) da grade icosaédrica para a regular, por triangulação de Delaunay em lon/lat.

    O DWD publica pesos de vizinho mais próximo (ICON_GLOBAL2WORLD_0125_EASY); a interpolação linear dá um campo
    mais liso na mesma grade de saída. Fica em cache: só é calculado uma vez para a grade ICON.
    """
    from scipy.spatial import Delaunay

    # o DWD grava clat e clon em graus; em radianos o maior valor não passa de pi
    if np.abs(clat).max() <= 3.2:
        clat, clon = np.degrees(clat), np.degrees(clon)
    lat = np.asarray(clat)
    lon = ((np.asarray(clon) + 180.0) % 360.0) - 180.0
    ok = np.where(np.abs(lat) <= 89.0)[0]
    pad_e = ok[lon[ok] > 170.0]
    pad_w = ok[lon[ok] < -170.0]
    orig = np.concatenate([ok, pad_e, pad_w])
    plon = np.concatenate([lon[ok], lon[pad_e] - 360.0, lon[pad_w] + 360.0])
    plat = np.concatenate([lat[ok], lat[pad_e], lat[pad_w]])
    t0 = time.time()
    tri = Delaunay(np.column_stack([plon, plat]))
    print(f"  Delaunay de {len(orig)} pontos em {time.time() - t0:.0f} s", flush=True)
    qlon = LON0 + DX * np.arange(NX)
    qlat = LAT0 - DY * np.arange(NY)
    QX, QY = np.meshgrid(qlon, qlat)
    q = np.column_stack([QX.ravel(), QY.ravel()])
    idx = np.empty((len(q), 3), dtype=np.int32)
    pesos = np.empty((len(q), 3), dtype=np.float32)
    T = tri.transform
    for a in range(0, len(q), 500_000):
        qq = q[a : a + 500_000]
        s = tri.find_simplex(qq)
        if (s < 0).any():
            raise RuntimeError("ponto fora da triangulação")
        b = np.einsum("ijk,ik->ij", T[s, :2], qq - T[s, 2])
        pesos[a : a + 500_000] = np.column_stack([b, 1.0 - b.sum(axis=1)])
        idx[a : a + 500_000] = orig[tri.simplices[s]]
    np.savez(destino, idx=idx, pesos=pesos, n_origem=len(clat))
    print(f"  pesos gravados em {destino} ({time.time() - t0:.0f} s)", flush=True)


class Remapeador:
    def __init__(self, arquivo: str):
        import scipy.sparse as sp

        z = np.load(arquivo)
        n = len(z["idx"])
        linhas = np.repeat(np.arange(n, dtype=np.int32), 3)
        self.M = sp.csr_matrix((z["pesos"].ravel().astype(np.float64), (linhas, z["idx"].ravel())), shape=(n, int(z["n_origem"])))

    def __call__(self, valores: np.ndarray) -> np.ndarray:
        return (self.M @ valores).reshape(NY, NX)


# --- quantização e PNG ----------------------------------------------------------------------------------------


def codigo(g: np.ndarray, vmin: float, vmax: float, bits: int, raiz: bool = False) -> np.ndarray:
    n = (1 << bits) - 1
    x = np.clip((g - vmin) / (vmax - vmin), 0.0, 1.0)
    if raiz:
        x = np.sqrt(x)
    c = np.clip(np.rint(x * n), 0, n).astype(np.uint32)
    return c << (16 - bits)


def quadro_escalar(g: np.ndarray, c: dict) -> np.ndarray:
    c16 = codigo(g, c["min"], c["max"], c["bits"], bool(c.get("raiz")))
    rgb = np.zeros((NY, NX, 3), dtype=np.uint8)
    rgb[..., 0] = c16 >> 8
    rgb[..., 1] = c16 & 255
    return rgb


def quadro_vento(u: np.ndarray, v: np.ndarray, rajada: np.ndarray, c: dict) -> np.ndarray:
    rgb = np.zeros((NY, NX, 3), dtype=np.uint8)
    rgb[..., 0] = codigo(u, c["min"], c["max"], 8) >> 8
    rgb[..., 1] = codigo(v, c["min"], c["max"], 8) >> 8
    rgb[..., 2] = codigo(rajada, c["gmin"], c["gmax"], 8) >> 8
    return rgb


def quadro_chuva(taxa: np.ndarray, neve: np.ndarray, c: dict) -> np.ndarray:
    rgb = np.zeros((NY, NX, 3), dtype=np.uint8)
    raiz = np.sqrt(np.clip(taxa, 0, c["max"]) / c["max"])
    rgb[..., 0] = np.clip(np.rint(raiz * 255), 0, 255).astype(np.uint8)
    rgb[..., 1] = np.clip(np.rint(neve * 255), 0, 255).astype(np.uint8)
    return rgb


def suavizar(g: np.ndarray, sigma: float) -> np.ndarray:
    """Gaussiana com a longitude periódica (o mundo dá a volta)."""
    from scipy.ndimage import gaussian_filter

    return gaussian_filter(g, sigma=sigma, mode=("nearest", "wrap"))


def gravar_imagem(rgb: np.ndarray, destino: str, formato: str) -> int:
    from PIL import Image

    os.makedirs(os.path.dirname(destino), exist_ok=True)
    tmp = destino + ".tmp"
    im = Image.fromarray(rgb, "RGB")
    if formato == "webp":
        im.save(tmp, format="WEBP", lossless=True, quality=100, method=4)
    else:
        im.save(tmp, format="PNG", compress_level=6)
    os.replace(tmp, destino)
    return os.path.getsize(destino)


# --- processamento de um passo --------------------------------------------------------------------------------

_REMAP: Remapeador | None = None


def _iniciar(pesos: str) -> None:
    global _REMAP
    _REMAP = Remapeador(pesos)


def processar_passo(args: tuple) -> dict:
    cache, saida, rodada, h, anterior, camadas, formato = args
    assert _REMAP is not None
    rm = _REMAP

    def campo(var: str, passo: int) -> np.ndarray:
        return rm(ler_grib(caminho_cache(cache, rodada, var, passo)))

    tamanhos: dict[str, int] = {}
    nome = f"{h:03d}.{formato}"

    def gravar(camada: str, rgb: np.ndarray) -> None:
        tamanhos[camada] = gravar_imagem(rgb, os.path.join(saida, rodada, camada, nome), formato)

    if "t2m" in camadas:
        gravar("t2m", quadro_escalar(campo("t_2m", h) - 273.15, CAMADAS["t2m"]))
    if "td2m" in camadas:
        gravar("td2m", quadro_escalar(campo("td_2m", h) - 273.15, CAMADAS["td2m"]))
    if "ur2m" in camadas:
        gravar("ur2m", quadro_escalar(campo("relhum_2m", h), CAMADAS["ur2m"]))
    if "pnm" in camadas:
        # a pressão reduzida ao mar tem ruído de célula sobre relevo alto (Andes, Himalaia): um filtro gaussiano de
        # 2 células (~28 km) tira o serrilhado das isolinhas sem mexer nos sistemas de escala sinótica
        gravar("pnm", quadro_escalar(suavizar(campo("pmsl", h) / 100.0, 2.0), CAMADAS["pnm"]))
    if "nuvens" in camadas:
        gravar("nuvens", quadro_escalar(campo("clct", h), CAMADAS["nuvens"]))
    if "sensacao" in camadas:
        t = campo("t_2m", h) - 273.15
        ur = np.clip(campo("relhum_2m", h), 0.0, 100.0)
        vento = np.hypot(campo("u_10m", h), campo("v_10m", h))
        e = ur / 100.0 * 6.105 * np.exp(17.27 * t / (237.7 + t))
        gravar("sensacao", quadro_escalar(t + 0.33 * e - 0.70 * vento - 4.00, CAMADAS["sensacao"]))
    if "agua" in camadas:
        gravar("agua", quadro_escalar(campo("tqv", h), CAMADAS["agua"]))
    if "cape" in camadas:
        gravar("cape", quadro_escalar(campo("cape_ml", h), CAMADAS["cape"]))
    if "neve" in camadas:
        gravar("neve", quadro_escalar(campo("h_snow", h), CAMADAS["neve"]))
    if "vento" in camadas:
        u, v = campo("u_10m", h), campo("v_10m", h)
        rajada = campo("vmax_10m", h) if h > 0 else np.hypot(u, v)
        gravar("vento", quadro_vento(u, v, rajada, CAMADAS["vento"]))
    if "chuva" in camadas:
        if anterior is None:
            taxa = np.zeros((NY, NX))
            neve = np.zeros((NY, NX))
        else:
            dt_h = h - anterior
            dtot = np.maximum(campo("tot_prec", h) - campo("tot_prec", anterior), 0.0)
            dneve = np.maximum(
                (campo("snow_gsp", h) + campo("snow_con", h)) - (campo("snow_gsp", anterior) + campo("snow_con", anterior)), 0.0
            )
            taxa = dtot / dt_h
            neve = np.where(dtot > 1e-4, np.clip(dneve / np.maximum(dtot, 1e-4), 0.0, 1.0), 0.0)
        gravar("chuva", quadro_chuva(taxa, neve, CAMADAS["chuva"]))
    return {"h": h, "tamanhos": tamanhos}


# --- principal ------------------------------------------------------------------------------------------------


def baixar_e_processar(a: argparse.Namespace, rodada: str, camadas: list[str], passos: list[int]) -> None:
    vars_ = sorted({v for c in camadas for v in PRECISA[c]})
    escada = passos_de(ESCADA, int(rodada[8:]))

    # pesos de remapeamento (uma vez por grade)
    pesos = os.path.join(a.cache, "pesos-0125.npz")
    if not os.path.exists(pesos):
        print("calculando pesos de remapeamento", flush=True)
        for v in INVARIANTES:
            baixar(url_do(v, rodada, None), caminho_cache(a.cache, rodada, v, None))
        clat = ler_grib(caminho_cache(a.cache, rodada, "clat", None))
        clon = ler_grib(caminho_cache(a.cache, rodada, "clon", None))
        construir_pesos(clat, clon, pesos)

    # a chuva é a diferença entre acumulados: cada passo precisa do anterior da lista pedida
    anterior = {h: (escada[escada.index(h) - 1] if escada.index(h) else None) for h in passos}
    faltam: list[tuple[str, int]] = []
    for v in vars_:
        horas = {h for h in passos if not (h == 0 and v in SEM_PASSO_ZERO)}
        if v in ACUMULADAS:
            horas |= {p for p in anterior.values() if p is not None}
        faltam += [(v, h) for h in sorted(horas)]
    t0 = time.time()
    print(f"baixando {len(faltam)} arquivos (os já baixados são pulados)", flush=True)
    feitos = 0
    with cf.ThreadPoolExecutor(a.conexoes) as ex:
        futs = [ex.submit(baixar, url_do(v, rodada, h), caminho_cache(a.cache, rodada, v, h)) for v, h in faltam]
        for f in cf.as_completed(futs):
            f.result()
            feitos += 1
            if feitos % 50 == 0 or feitos == len(futs):
                print(f"  {feitos}/{len(futs)} ({time.time() - t0:.0f} s)", flush=True)

    # remapeamento e PNG
    tarefas = [(a.cache, a.saida, rodada, h, anterior[h], camadas, a.formato) for h in passos]
    t1 = time.time()
    with cf.ProcessPoolExecutor(a.jobs, initializer=_iniciar, initargs=(pesos,)) as ex:
        for k, _ in enumerate(ex.map(processar_passo, tarefas), 1):
            if k % 10 == 0 or k == len(tarefas):
                print(f"  {k}/{len(tarefas)} passos ({time.time() - t1:.0f} s)", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--saida", required=True, help="diretório de saída (manifest.json e uma pasta por rodada)")
    ap.add_argument("--cache", required=True, help="diretório dos GRIB baixados e dos pesos de remapeamento")
    ap.add_argument("--rodada", default="auto", help="AAAAMMDDHH ou auto (a mais recente completa)")
    ap.add_argument("--passos", default=ESCADA, help="horas de previsão, ex.: 0-78,81-180/3")
    ap.add_argument("--camadas", default=",".join(CAMADAS), help="subconjunto das camadas")
    ap.add_argument("--formato", choices=["png", "webp"], default="png", help="png (padrão) ou webp sem perda (~35%% menor)")
    ap.add_argument("--jobs", type=int, default=6, help="processos de remapeamento")
    ap.add_argument("--conexoes", type=int, default=10, help="downloads em paralelo")
    ap.add_argument("--so-manifesto", action="store_true", help="não baixa nem remapeia: só refaz o manifest.json do que há no disco")
    ap.add_argument("--limpar-cache", action="store_true", help="apaga os GRIB desta rodada ao terminar")
    a = ap.parse_args()

    rodada = descobrir_rodada() if a.rodada == "auto" else a.rodada
    camadas = [c for c in a.camadas.split(",") if c in CAMADAS]
    escada = passos_de(ESCADA, int(rodada[8:]))
    passos = [h for h in passos_de(a.passos, int(rodada[8:])) if h in escada]
    print(f"rodada {rodada}, {len(passos)} passos, camadas {camadas}", flush=True)
    os.makedirs(a.cache, exist_ok=True)
    os.makedirs(a.saida, exist_ok=True)
    if not a.so_manifesto:
        baixar_e_processar(a, rodada, camadas, passos)
    else:
        camadas = [c for c in CAMADAS if os.path.isdir(os.path.join(a.saida, rodada, c))]

    # manifesto: junta as camadas já gravadas desta rodada (rodadas parciais e reprocessamentos de uma camada só entram)
    pasta = os.path.join(a.saida, rodada)
    caminho_manifesto = os.path.join(a.saida, "manifest.json")
    todas = set(camadas)
    if os.path.exists(caminho_manifesto):
        with open(caminho_manifesto, encoding="utf-8") as f:
            antigo = json.load(f)
        if antigo.get("caminho") == rodada and antigo.get("formato") == a.formato:
            todas |= {c for c in antigo.get("camadas", {}) if c in CAMADAS}
    horas_de: dict[str, set[int]] = {}
    tam: dict[str, list[int]] = {}
    for c in sorted(todas):
        pasta_c = os.path.join(pasta, c)
        arquivos = sorted(f for f in os.listdir(pasta_c) if f.endswith("." + a.formato)) if os.path.isdir(pasta_c) else []
        horas_de[c] = {int(f[:3]) for f in arquivos}
        tam[c] = [os.path.getsize(os.path.join(pasta_c, f)) for f in arquivos]
    # só entra no manifesto a hora que existe em todas as camadas
    horas_ok = sorted(set.intersection(*horas_de.values())) if horas_de else []
    inicio = dt.datetime.strptime(rodada, "%Y%m%d%H").replace(tzinfo=dt.timezone.utc)
    manifesto = {
        "versao": 1,
        "modelo": "ICON",
        "atribuicao": ATRIBUICAO,
        "rodada": inicio.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "caminho": rodada,
        "formato": a.formato,
        "grade": {"lon0": LON0, "lat0": LAT0, "dx": DX, "dy": DY, "nx": NX, "ny": NY},
        "passos": horas_ok,
        "camadas": {
            c: {**CAMADAS[c], "bytes_medio": int(np.mean(tam[c])) if tam[c] else 0, "bytes_total": int(np.sum(tam[c])) if tam[c] else 0}
            for c in sorted(todas)
        },
        "gerado": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    with open(caminho_manifesto, "w", encoding="utf-8") as f:
        json.dump(manifesto, f, ensure_ascii=False, indent=1)
    total = sum(sum(v) for v in tam.values())
    print(f"pronto: {len(horas_ok)} quadros por camada, {total / 1e6:.0f} MB em {pasta}", flush=True)
    for c in sorted(todas):
        if tam[c]:
            print(f"  {c}: média {np.mean(tam[c]) / 1e6:.2f} MB/quadro, total {sum(tam[c]) / 1e6:.0f} MB", flush=True)
    if a.limpar_cache:
        shutil.rmtree(os.path.join(a.cache, rodada), ignore_errors=True)


if __name__ == "__main__":
    main()
