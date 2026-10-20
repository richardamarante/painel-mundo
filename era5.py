"""ERA5 nas camadas que a DREAM não tem: agua (TQV), cape (CAPE_ML) e neve (H_SNOW).

Nas horas da ICON-DREAM o arquivo horário fica sem quadro dessas três camadas (tamanho 0 no índice). Aqui elas vêm da
reanálise ERA5 do ECMWF, lida da cópia pública ARCO-ERA5 do Google Cloud (zarr v2, um pedaço por hora e variável,
721 x 1440 a 0,25 grau, blosc/lz4), por HTTPS anônimo e sem custo:

    https://storage.googleapis.com/gcp-public-data-arco-era5/ar/full_37-1h-0p25deg-chunk-1.zarr-v3

Variáveis e conversão:

* agua  <- total_column_water_vapour (kg/m², a mesma grandeza e unidade do TQV)
* cape  <- convective_available_potential_energy (J/kg). O CAPE do ERA5 é o da parcela mais instável (parte de
           vários níveis até 350 hPa acima da superfície); o CAPE_ML do ICON é o da camada de mistura. Mesma unidade,
           valores um pouco maiores no ERA5 onde há inversão perto do solo.
* neve  <- snow_depth (m de equivalente em água, média na célula) x 1000 / snow_density (kg/m³) = altura da neve em
           metros, a grandeza do H_SNOW. Sem neve (sd = 0) a altura é 0. A densidade do ERA5 fica entre 100 e 450.
           Nas geleiras (Groenlândia, Antártida) o ERA5 fixa sd = 10 m de água, que vira mais de 20 m de neve e
           satura a escala de 4 m da camada, como o ICON nas mesmas áreas.

A conversão é feita a 0,25 grau; o campo convertido é interpolado (bilinear, longitude periódica) para a grade de
0,125 grau do gerar.py e codificado com o mesmo `gerar.quadro_escalar` (mesma escala, unidade, bits e raiz) e o mesmo
WebP sem perda do arquivo_lote (method=0, quality=0). O quadro entra no índice com origem 4 (arquivo.ORIGEM_ERA5).

Subcomandos:
    python era5.py preencher --mes 2019-01 --trabalho /mnt/era5 [--enviar richardmelo/painel-nacional-icon]
    python era5.py conferir --mes 2024-08 --trabalho /mnt/era5 [--horas 24]
    python era5.py meses [--meses dream]      (a lista de meses do workflow era5.yml; 'dream' = todos os da DREAM)

`preencher` lê do HF o índice da t2m do mês (horas de origem 3, a DREAM) e o de cada uma das três camadas; gera o
ERA5 só nas horas da DREAM em que a camada está vazia, mantém byte a byte os quadros que já existem (o ICON nos meses
mistos) e envia ao HF só os três .bin e `meses/<AAAA-MM>-era5.json`. Rodar depois que o lote do mês terminou: o juntar
do arquivo_lote também não apaga mais um quadro de origem 4 (ver `arquivo_lote.manter_era5`).

Dados: ERA5 (Hersbach et al. 2020, doi:10.24381/cds.adbb2d47), Copernicus Climate Change Service, licença CC BY 4.0;
cópia ARCO-ERA5 do Google Research.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import io
import json
import os
import shutil
import time

import numpy as np

import arquivo
import gerar

ARCO = "https://storage.googleapis.com/gcp-public-data-arco-era5/ar/full_37-1h-0p25deg-chunk-1.zarr-v3"
HF_BASE = "https://huggingface.co/datasets/richardmelo/painel-nacional-icon/resolve/main"
ATRIBUICAO_ERA5 = "ERA5 (ECMWF, Copernicus Climate Change Service, CC BY 4.0, doi:10.24381/cds.adbb2d47), cópia ARCO-ERA5 do Google"
CAMADAS_ERA5 = ("agua", "cape", "neve")
VARIAVEIS = {
    "agua": ("total_column_water_vapour",),
    "cape": ("convective_available_potential_energy",),
    "neve": ("snow_depth", "snow_density"),
}
UTC = dt.timezone.utc
T0_ARCO = dt.datetime(1900, 1, 1, tzinfo=UTC)  # o índice do tempo no zarr é "horas desde 1900-01-01"
NLAT, NLON, PASSO = 721, 1440, 0.25  # latitude de 90 a -90, longitude de 0 a 359,75


# --- leitura ----------------------------------------------------------------------------------------------------

_SESSAO = {}


def _sessao():
    import requests

    pid = os.getpid()
    if pid not in _SESSAO:
        s = requests.Session()
        s.mount("https://", requests.adapters.HTTPAdapter(pool_connections=8, pool_maxsize=16))
        _SESSAO[pid] = s
    return _SESSAO[pid]


def _get(url: str, tentativas: int = 8) -> bytes:
    ultimo = None
    for k in range(tentativas):
        try:
            r = _sessao().get(url, timeout=120)
            if r.status_code == 200:
                return r.content
            ultimo = RuntimeError(f"HTTP {r.status_code} em {url}")
            if r.status_code == 404:
                break
        except Exception as e:  # rede instável
            ultimo = e
        time.sleep(1 + 2 * k)
    raise RuntimeError(f"falha ao ler {url}: {ultimo}")


def ler_era5(var: str, t: dt.datetime) -> np.ndarray:
    """Um campo horário do ERA5 (721 x 1440, float32, norte em cima, longitude 0 a 359,75)."""
    import numcodecs

    k = int((t - T0_ARCO).total_seconds() // 3600)
    bruto = numcodecs.Blosc().decode(_get(f"{ARCO}/{var}/{k}.0.0"))
    return np.frombuffer(bruto, "<f4").reshape(NLAT, NLON)


# --- interpolação para a grade do gerar.py -------------------------------------------------------------------------

_LAT = gerar.LAT0 - gerar.DY * np.arange(gerar.NY)
_LON = gerar.LON0 + gerar.DX * np.arange(gerar.NX)
_FR = (90.0 - _LAT) / PASSO
_R0 = np.floor(_FR).astype(np.int64)
_WR = (_FR - _R0)[:, None]
_FC = (_LON % 360.0) / PASSO
_C0 = np.floor(_FC).astype(np.int64) % NLON
_C1 = (_C0 + 1) % NLON
_WC = (_FC - np.floor(_FC))[None, :]


def para_grade(campo: np.ndarray) -> np.ndarray:
    """Bilinear de 0,25 para 0,125 grau (2880 x 1363), longitude periódica. Na grade de destino cada ponto cai num nó
    do ERA5 ou no meio entre dois, então a interpolação não inventa extremos."""
    f = np.nan_to_num(campo.astype(np.float64), nan=0.0)
    linhas = (1.0 - _WR) * f[_R0] + _WR * f[_R0 + 1]
    return (1.0 - _WC) * linhas[:, _C0] + _WC * linhas[:, _C1]


def altura_neve(sd: np.ndarray, rsn: np.ndarray) -> np.ndarray:
    """Equivalente em água (m) e densidade (kg/m³) -> altura da neve (m). Densidade ausente ou absurda vira 100 kg/m³
    (a da neve nova no ERA5)."""
    sd = np.nan_to_num(sd.astype(np.float64), nan=0.0)
    rho = np.nan_to_num(rsn.astype(np.float64), nan=100.0)
    rho = np.where(rho < 50.0, 100.0, rho)
    return np.where(sd > 0.0, sd * 1000.0 / rho, 0.0)


def campos_era5(t: dt.datetime, camadas=CAMADAS_ERA5) -> dict[str, np.ndarray]:
    """O valor físico de cada camada na grade de 0,125 grau, na unidade do gerar.CAMADAS."""
    saida = {}
    if "agua" in camadas:
        saida["agua"] = para_grade(ler_era5("total_column_water_vapour", t))
    if "cape" in camadas:
        saida["cape"] = para_grade(ler_era5("convective_available_potential_energy", t))
    if "neve" in camadas:
        saida["neve"] = para_grade(altura_neve(ler_era5("snow_depth", t), ler_era5("snow_density", t)))
    return saida


def _hora(args: tuple) -> dict:
    """Um trabalhador: lê, converte, interpola e grava os quadros de uma hora."""
    import arquivo_lote

    t_iso, camadas, pasta = args
    t = dt.datetime.fromisoformat(t_iso)
    t0 = time.perf_counter()
    campos = campos_era5(t, camadas)
    t1 = time.perf_counter()
    tam = {}
    for c, g in campos.items():
        destino = os.path.join(pasta, c, t.strftime("%Y%m%d%H") + ".webp")
        tam[c] = arquivo_lote.gravar_webp_rapido(gerar.quadro_escalar(g, gerar.CAMADAS[c]), destino, "webp")
    return {"t": t_iso, "tamanhos": tam, "ler": t1 - t0, "webp": time.perf_counter() - t1}


# --- índices no HF ----------------------------------------------------------------------------------------------


def _faixa_hf(url: str, a: int, b: int) -> bytes | None:
    import requests

    for k in range(8):
        try:
            r = _sessao().get(url, headers={"Range": f"bytes={a}-{b}"}, timeout=120)
            if r.status_code in (200, 206):
                return r.content
            if r.status_code == 404:
                return None
        except requests.RequestException:
            pass
        time.sleep(2 + 3 * k)
    raise RuntimeError(f"falha ao ler {url}")


def indice_hf(camada: str, ano: int, mes: int, base: str = HF_BASE) -> list[tuple[int, int, int]] | None:
    """[(offset, tamanho, origem)] por hora do mês, do .bin no HF; None se o arquivo não existe."""
    cab = _faixa_hf(f"{base}/{arquivo.caminho_no_repo(camada, ano, mes)}", 0, arquivo.TAM_CABECALHO - 1)
    return None if cab is None else arquivo.ler_cabecalho(cab)["indice"]


def quadro_hf(camada: str, ano: int, mes: int, offset: int, tamanho: int, base: str = HF_BASE) -> bytes:
    return _faixa_hf(f"{base}/{arquivo.caminho_no_repo(camada, ano, mes)}", offset, offset + tamanho - 1)


def baixar_bin(camada: str, ano: int, mes: int, destino: str, base: str = HF_BASE) -> str:
    os.makedirs(os.path.dirname(destino), exist_ok=True)
    url = f"{base}/{arquivo.caminho_no_repo(camada, ano, mes)}"
    for k in range(6):
        try:
            with _sessao().get(url, stream=True, timeout=300) as r, open(destino + ".part", "wb") as f:
                r.raise_for_status()
                shutil.copyfileobj(r.raw, f, 1 << 22)
            os.replace(destino + ".part", destino)
            return destino
        except Exception as e:
            print(f"  download de {url} falhou ({e}); de novo", flush=True)
            time.sleep(5 + 10 * k)
    raise RuntimeError(f"não consegui baixar {url}")


# --- preencher --------------------------------------------------------------------------------------------------


def preencher(a: argparse.Namespace) -> dict:
    ano, m = map(int, a.mes.split("-"))
    inicio = arquivo.inicio_do_mes(ano, m)
    n = arquivo.horas_do_mes(ano, m)
    t0 = time.time()
    ref = indice_hf("t2m", ano, m)
    if ref is None:
        raise SystemExit(f"{a.mes}: sem t2m no HF; o lote do mês ainda não terminou")
    dream = [k for k, (_, tam, org) in enumerate(ref) if tam and org == arquivo.ORIGEM_DREAM]
    if not dream:
        print(f"{a.mes}: nenhuma hora da DREAM, nada a preencher", flush=True)
        return {}
    pasta = os.path.join(a.trabalho, "quadros", a.mes)
    saida = os.path.join(a.trabalho, "saida")
    shutil.rmtree(saida, ignore_errors=True)
    existentes, alvo = {}, {}
    for c in CAMADAS_ERA5:
        idx = indice_hf(c, ano, m) or [(0, 0, 0)] * n
        existentes[c] = {k: e for k, e in enumerate(idx) if e[1]}
        alvo[c] = [k for k in dream if k not in existentes[c] or (a.refazer and existentes[c][k][2] == arquivo.ORIGEM_ERA5)]
        if a.limite:  # teste: só as primeiras horas
            alvo[c] = alvo[c][: a.limite]
    horas = sorted({k for c in CAMADAS_ERA5 for k in alvo[c]})
    print(f"{a.mes}: {len(dream)} horas da DREAM; a gerar: " + ", ".join(f"{c} {len(alvo[c])}" for c in CAMADAS_ERA5), flush=True)
    if not horas:  # o mês já está preenchido: nada a gerar nem a enviar
        print(f"{a.mes}: já preenchido", flush=True)
        return {}

    # quadros do ERA5, uma hora por trabalhador
    t1 = time.time()
    tarefas = [((inicio + dt.timedelta(hours=k)).isoformat(), tuple(c for c in CAMADAS_ERA5 if k in alvo[c]), pasta) for k in horas]
    soma = {"ler": 0.0, "webp": 0.0}
    with cf.ProcessPoolExecutor(a.jobs) as ex:
        for j, r in enumerate(ex.map(_hora, tarefas, chunksize=2), 1):
            soma["ler"] += r["ler"]
            soma["webp"] += r["webp"]
            if j % 48 == 0 or j == len(tarefas):
                print(f"  {j}/{len(tarefas)} horas ({time.time() - t1:.0f} s)", flush=True)
    t_gerar = time.time() - t1

    # o .bin de cada camada: os quadros que já estavam (byte a byte) e os novos do ERA5
    tamanhos, contagem = {}, {}
    for c in CAMADAS_ERA5:
        quadros = {}
        manter = {k: e for k, e in existentes[c].items() if k not in alvo[c]}
        if manter:
            antigo = baixar_bin(c, ano, m, os.path.join(a.trabalho, "antigo", f"{c}.bin"))
            with open(antigo, "rb") as f:
                for k, (off, tam, org) in manter.items():
                    f.seek(off)
                    p = os.path.join(pasta, c, f"antigo-{k:03d}.webp")
                    with open(p, "wb") as q:
                        q.write(f.read(tam))
                    quadros[k] = (p, org)
            os.remove(antigo)
        for k in alvo[c]:
            quadros[k] = (os.path.join(pasta, c, (inicio + dt.timedelta(hours=k)).strftime("%Y%m%d%H") + ".webp"), arquivo.ORIGEM_ERA5)
        tamanhos[c] = arquivo.gravar(os.path.join(saida, arquivo.caminho_no_repo(c, ano, m)), c, ano, m, quadros)
        contagem[c] = {"era5": len(alvo[c]), "mantidos": len(manter)}
    shutil.rmtree(pasta, ignore_errors=True)

    info = {
        "mes": a.mes,
        "origem": ATRIBUICAO_ERA5 + "; agua = total_column_water_vapour, cape = convective_available_potential_energy, "
        "neve = snow_depth x 1000 / snow_density; bilinear de 0,25 para 0,125 grau",
        "origem_no_indice": arquivo.ORIGEM_ERA5,
        "horas_dream": len(dream),
        "quadros": contagem,
        "bytes": tamanhos,
        "segundos_gerando": round(t_gerar, 1),
        "cpu_segundos": {k: round(v) for k, v in soma.items()},
        "segundos_total": round(time.time() - t0, 1),
        "gerado": dt.datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    os.makedirs(os.path.join(saida, "meses"), exist_ok=True)
    with open(os.path.join(saida, "meses", f"{a.mes}-era5.json"), "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=1)
    print(json.dumps(info, ensure_ascii=False), flush=True)
    if a.enviar:
        from huggingface_hub import HfApi

        api = HfApi()
        for k in range(6):
            try:
                t2 = time.time()
                api.upload_folder(repo_id=a.enviar, repo_type="dataset", folder_path=saida, commit_message=f"{a.mes}: agua, cape e neve do ERA5")
                print(f"enviado em {time.time() - t2:.0f} s", flush=True)
                break
            except Exception as e:
                print(f"envio falhou ({e}); de novo em {30 * (k + 1)} s", flush=True)
                time.sleep(30 * (k + 1))
        else:
            raise SystemExit("não consegui enviar ao HF")
    return info


# --- conferir ---------------------------------------------------------------------------------------------------


def _cores(x: np.ndarray) -> np.ndarray:
    """0..1 -> RGB numa rampa azul escuro, verde, amarelo (sem rosa)."""
    paradas = np.array([[18, 24, 60], [30, 90, 140], [40, 150, 120], [150, 200, 70], [250, 230, 90]], dtype=np.float64)
    p = np.clip(x, 0, 1) * (len(paradas) - 1)
    i = np.minimum(p.astype(int), len(paradas) - 2)
    w = (p - i)[..., None]
    return ((1 - w) * paradas[i] + w * paradas[i + 1]).astype(np.uint8)


def conferir(a: argparse.Namespace) -> dict:
    """ERA5 processado contra o quadro do ICON (operacional, origem 1 ou 2) já no HF, nas mesmas horas: os dois
    decodificados para o valor físico (o mesmo decodificar_quadro do arquivo_lote) e comparados pixel a pixel."""
    from PIL import Image, ImageDraw

    import arquivo_lote

    ano, m = map(int, a.mes.split("-"))
    inicio = arquivo.inicio_do_mes(ano, m)
    n = arquivo.horas_do_mes(ano, m)
    t0 = time.time()
    idx = {c: indice_hf(c, ano, m) for c in CAMADAS_ERA5}
    comuns = [k for k in range(n) if all(idx[c] and idx[c][k][1] and idx[c][k][2] in (arquivo.ORIGEM_ICON, arquivo.ORIGEM_OCF) for c in CAMADAS_ERA5)]
    if not comuns:
        raise SystemExit(f"{a.mes}: sem horas do ICON operacional nas três camadas")
    passo = max(1, len(comuns) // a.horas)
    horas = comuns[::passo][: a.horas]
    w = np.cos(np.radians(_LAT))[:, None] * np.ones((1, gerar.NX))
    acc = {c: [] for c in CAMADAS_ERA5}
    figura = []
    for j, k in enumerate(horas):
        t = inicio + dt.timedelta(hours=k)
        e5 = campos_era5(t)
        for c in CAMADAS_ERA5:
            off, tam, _ = idx[c][k]
            q_ic = np.asarray(Image.open(io.BytesIO(quadro_hf(c, ano, m, off, tam))).convert("RGB"))
            v_ic = arquivo_lote.decodificar_quadro(q_ic, c)[c]
            v_e5 = arquivo_lote.decodificar_quadro(gerar.quadro_escalar(e5[c], gerar.CAMADAS[c]), c)[c]
            acc[c].append((v_e5, v_ic))
            if j == len(horas) // 2:
                cc = gerar.CAMADAS[c]
                norm = lambda v: ((v - cc["min"]) / (cc["max"] - cc["min"])) ** (0.5 if cc.get("raiz") else 1)
                lado = np.concatenate([_cores(norm(v_e5[::3, ::3])), np.full((v_e5[::3, ::3].shape[0], 8, 3), 255, np.uint8), _cores(norm(v_ic[::3, ::3]))], axis=1)
                im = Image.fromarray(lado)
                ImageDraw.Draw(im).text((6, 4), f"{c}  ERA5 (esq.) x ICON (dir.)  {t:%Y-%m-%d %H} UTC", fill=(255, 255, 255))
                figura.append(np.asarray(im))
        print(f"  {t:%Y-%m-%d %H} UTC ({time.time() - t0:.0f} s)", flush=True)
    resultado = {}
    for c, pares in acc.items():
        e = np.stack([x for x, _ in pares])
        o = np.stack([y for _, y in pares])
        ww = np.broadcast_to(w, e.shape)
        d = e - o
        r = {
            "media_era5": round(float(np.average(e, weights=ww)), 3), "media_icon": round(float(np.average(o, weights=ww)), 3),
            "vies": round(float(np.average(d, weights=ww)), 3), "rmse": round(float(np.sqrt(np.average(d * d, weights=ww))), 3),
            "correlacao": round(float(np.corrcoef(e.ravel(), o.ravel())[0, 1]), 4),
            "p1_p99_era5": [round(float(x), 3) for x in np.percentile(e, [1, 99])],
            "p1_p99_icon": [round(float(x), 3) for x in np.percentile(o, [1, 99])],
            "max_era5": round(float(e.max()), 3), "max_icon": round(float(o.max()), 3),
        }
        if c in ("neve", "cape"):  # onde algum dos dois tem o fenômeno
            mk = (e > 0) | (o > 0)
            r["fracao_com_valor_era5"] = round(float((e > 0).mean()), 4)
            r["fracao_com_valor_icon"] = round(float((o > 0).mean()), 4)
            r["correlacao_onde_ha"] = round(float(np.corrcoef(e[mk], o[mk])[0, 1]), 4) if mk.sum() > 2 else None
            r["vies_onde_ha"] = round(float(d[mk].mean()), 3) if mk.any() else None
        resultado[c] = r
    os.makedirs(a.trabalho, exist_ok=True)
    png = os.path.join(a.trabalho, f"era5-x-icon-{a.mes}.png")
    Image.fromarray(np.concatenate(figura, axis=0)).save(png)
    info = {"mes": a.mes, "horas": [(inicio + dt.timedelta(hours=k)).strftime("%Y-%m-%dT%H") for k in horas],
            "media_ponderada": "cos(lat)", "camadas": resultado, "figura": png, "segundos": round(time.time() - t0)}
    with open(os.path.join(a.trabalho, f"era5-x-icon-{a.mes}.json"), "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=1)
    print(json.dumps(info, ensure_ascii=False, indent=1), flush=True)
    return info


# --- meses da DREAM ---------------------------------------------------------------------------------------------


DREAM = "2015-01..2023-03,2023-05,2024-03..2024-06,2025-01..2025-04,2025-08..2026-03"


def expandir(texto: str) -> list[str]:
    """'2019-01', '2019-01..2019-03', listas com vírgula, ou 'dream': os meses com horas da DREAM (2015-01 a 2023-03,
    os buracos da OCF em 05/2023, 03 a 06/2024 e 01 a 04/2025, e de 2025-08 a 2026-03). Do mais novo ao mais antigo."""
    texto = texto.replace(" ", "")
    if texto == "dream":
        texto = DREAM
    ms = set()
    for parte in filter(None, texto.split(",")):
        a, _, b = parte.partition("..")
        (y, m), (y2, m2) = sorted([tuple(map(int, a.split("-"))), tuple(map(int, (b or a).split("-")))])
        while (y, m) <= (y2, m2):
            ms.add(f"{y:04d}-{m:02d}")
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return sorted(ms, reverse=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("preencher")
    p.add_argument("--mes", required=True)
    p.add_argument("--trabalho", required=True)
    p.add_argument("--jobs", type=int, default=8)
    p.add_argument("--enviar", default="")
    p.add_argument("--refazer", action="store_true", help="gera de novo também as horas que já têm ERA5 (origem 4)")
    p.add_argument("--limite", type=int, default=0, help="teste: só as N primeiras horas de cada camada")
    p = sub.add_parser("conferir")
    p.add_argument("--mes", required=True)
    p.add_argument("--trabalho", required=True)
    p.add_argument("--horas", type=int, default=24)
    p = sub.add_parser("meses")
    p.add_argument("--meses", default="dream")
    a = ap.parse_args()
    if a.cmd == "meses":
        ms = expandir(a.meses)
        print(f"{len(ms)} meses: {ms}")
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a") as f:
                f.write(f"meses={json.dumps(ms)}\n")
        return
    {"preencher": preencher, "conferir": conferir}[a.cmd](a)


if __name__ == "__main__":
    main()
