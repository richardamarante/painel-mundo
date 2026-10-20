"""Lotes do arquivo horário, do mês mais novo para o mais antigo, e o tempo estimado de cada um.

Cada lote é um disparo do workflow arquivo.yml com até 8 meses (8 x 31 pedaços = 248, abaixo dos 256 da matriz). Os
disparos entram na fila do Actions e rodam um depois do outro, porque o teto de 20 jobs simultâneos vale para a conta.

    python lotes.py                       # só lista os lotes e a estimativa
    python lotes.py --disparar            # dispara todos com o gh (repete quando a rede falha)
    python lotes.py --disparar --so 1     # só o primeiro lote

Fontes: as cópias do opendata no source.coop (bkr/icon/icon-global-raw), de 12/03/2026 em diante; o arquivo zarr da
Open Climate Fix (ocf.py), de 21/03/2023 a 15/08/2025; e a reanálise ICON-DREAM do DWD (leitor_dream.py) de 2015-01 a
20/03/2023, de 13/08/2025 a 11/03/2026 (a antiga lacuna) e nos buracos da OCF (28-29/05/2023, 16/03 a 04/06/2024 e
27/01 a 03/04/2025). Nas horas da DREAM as camadas agua, cape e neve ficam sem quadro: ela não tem TQV, CAPE_ML nem
H_SNOW.
"""

from __future__ import annotations

import argparse
import datetime as dt
import subprocess
import time

import arquivo_lote

PRIMEIRO_MES_RAW = "2026-03"
PRIMEIRO_MES = "2015-01"  # DREAM até a OCF
LACUNA = ("2025-09", "2026-02")  # sem cópia do ICON operacional: sai da DREAM, mas num lote à parte
MESES_POR_LOTE = 8


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--de", default=PRIMEIRO_MES)
    ap.add_argument("--ate", default="", help="último mês (padrão: o mês passado)")
    ap.add_argument("--por-lote", type=int, default=MESES_POR_LOTE)
    ap.add_argument("--min-por-job", type=float, default=4.7, help="minutos de um pedaço (job inteiro), medidos no 2026-09")
    ap.add_argument("--min-por-job-ocf", type=float, default=4.7, help="minutos de um pedaço da OCF (2 GB por rodada)")
    ap.add_argument("--min-por-job-dream", type=float, default=4.7, help="minutos de um pedaço da DREAM (medir no 2019-01)")
    ap.add_argument("--min-juntar", type=float, default=2.5, help="minutos do job que junta e envia um mês, medidos no 2026-09")
    ap.add_argument("--disparar", action="store_true")
    ap.add_argument("--so", type=int, default=0, help="dispara só os N primeiros lotes")
    ap.add_argument("--repo", default="richardamarante/painel-mundo")
    a = ap.parse_args()

    hoje = dt.date.today()
    ate = a.ate or (hoje.replace(day=1) - dt.timedelta(days=1)).strftime("%Y-%m")
    meses = arquivo_lote.expandir_meses(f"{a.de}..{ate}")
    # blocos por fonte, para cada disparo ser de uma fonte só: cópias brutas, lacuna (DREAM), OCF, DREAM antes da OCF
    blocos = [
        [m for m in meses if m > LACUNA[1]],
        [m for m in meses if LACUNA[0] <= m <= LACUNA[1]],
        [m for m in meses if "2023-03" <= m < LACUNA[0]],
        [m for m in meses if m < "2023-03"],
    ]
    lotes = [b[k : k + a.por_lote] for b in blocos for k in range(0, len(b), a.por_lote)]
    total = 0.0
    for i, lote in enumerate(lotes, 1):
        pecas = 31 * len(lote)
        por_job = a.min_por_job if lote[-1] >= PRIMEIRO_MES_RAW else a.min_por_job_ocf if lote[-1] >= "2023-03" and lote[0] < LACUNA[0] else a.min_por_job_dream
        minutos = -(-pecas // 20) * por_job + -(-len(lote) // 20) * a.min_juntar
        total += minutos
        print(f"lote {i}: {lote[0]}..{lote[-1]} ({len(lote)} meses, ~{pecas} jobs), ~{minutos / 60:.1f} h")
    print(f"total: {len(meses)} meses em {len(lotes)} lotes, ~{total / 60:.1f} h de relógio")
    if not a.disparar:
        return
    if a.de < PRIMEIRO_MES:
        raise SystemExit(f"o arquivo horário começa em {PRIMEIRO_MES}")
    for lote in lotes[: a.so or None]:
        intervalo = f"{lote[-1]}..{lote[0]}"
        for k in range(8):
            r = subprocess.run(["gh", "workflow", "run", "arquivo.yml", "-R", a.repo, "-f", f"meses={intervalo}", "-f", "conferir=0"])
            if r.returncode == 0:
                print(f"disparado {intervalo}")
                break
            time.sleep(5 * (k + 1))
        else:
            raise SystemExit(f"não consegui disparar {intervalo}")


if __name__ == "__main__":
    main()
