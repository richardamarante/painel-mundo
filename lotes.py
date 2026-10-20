"""Lotes do arquivo horário, do mês mais novo para o mais antigo, e o tempo estimado de cada um.

Cada lote é um disparo do workflow arquivo.yml com até 8 meses (8 x 31 pedaços = 248, abaixo dos 256 da matriz). Os
disparos entram na fila do Actions e rodam um depois do outro, porque o teto de 20 jobs simultâneos vale para a conta.

    python lotes.py                       # só lista os lotes e a estimativa
    python lotes.py --disparar            # dispara todos com o gh (repete quando a rede falha)
    python lotes.py --disparar --so 1     # só o primeiro lote

Fonte de hoje: as cópias do opendata no source.coop (bkr/icon/icon-global-raw), de 12/03/2026 em diante. Antes disso o
arquivo_lote.py ainda não tem leitor (OCF zarr de 03/2023 a 08/2025, DREAM antes e nos buracos).
"""

from __future__ import annotations

import argparse
import datetime as dt
import subprocess
import time

import arquivo_lote

PRIMEIRO_MES_RAW = "2026-03"
MESES_POR_LOTE = 8


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--de", default=PRIMEIRO_MES_RAW)
    ap.add_argument("--ate", default="", help="último mês (padrão: o mês passado)")
    ap.add_argument("--por-lote", type=int, default=MESES_POR_LOTE)
    ap.add_argument("--min-por-job", type=float, default=6.0, help="minutos de um pedaço, medidos no 2026-09")
    ap.add_argument("--min-juntar", type=float, default=12.0, help="minutos do job que junta e envia um mês")
    ap.add_argument("--disparar", action="store_true")
    ap.add_argument("--so", type=int, default=0, help="dispara só os N primeiros lotes")
    ap.add_argument("--repo", default="richardamarante/painel-mundo")
    a = ap.parse_args()

    hoje = dt.date.today()
    ate = a.ate or (hoje.replace(day=1) - dt.timedelta(days=1)).strftime("%Y-%m")
    meses = arquivo_lote.expandir_meses(f"{a.de}..{ate}")
    lotes = [meses[k : k + a.por_lote] for k in range(0, len(meses), a.por_lote)]
    total = 0.0
    for i, lote in enumerate(lotes, 1):
        pecas = 31 * len(lote)
        minutos = -(-pecas // 20) * a.min_por_job + -(-len(lote) // 20) * a.min_juntar
        total += minutos
        print(f"lote {i}: {lote[0]}..{lote[-1]} ({len(lote)} meses, ~{pecas} jobs), ~{minutos / 60:.1f} h")
    print(f"total: {len(meses)} meses em {len(lotes)} lotes, ~{total / 60:.1f} h de relógio")
    if not a.disparar:
        return
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
