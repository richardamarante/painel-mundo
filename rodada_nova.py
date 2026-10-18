"""Diz se há rodada nova do ICON para publicar.

Compara a rodada completa mais recente do DWD com a que o site já mostra (manifest.json servido pelo Worker).
Escreve `rodada=AAAAMMDDHH` e `nova=true|false` em $GITHUB_OUTPUT (ou na tela, fora do GitHub).
"""

from __future__ import annotations

import json
import os
import urllib.request

from gerar import descobrir_rodada

MANIFESTO = os.environ.get("MANIFESTO_URL", "https://painel-dados.painelnacional.workers.dev/mundo/manifest.json")


def publicada() -> str:
    try:
        with urllib.request.urlopen(MANIFESTO, timeout=30) as r:
            return json.load(r).get("caminho", "")
    except Exception:
        return ""


def main() -> None:
    rodada = descobrir_rodada()
    atual = publicada()
    nova = rodada > atual
    linhas = [f"rodada={rodada}", f"nova={'true' if nova else 'false'}"]
    print(f"DWD: {rodada} | no site: {atual or '(nenhuma)'} | publicar: {nova}", flush=True)
    destino = os.environ.get("GITHUB_OUTPUT")
    if destino:
        with open(destino, "a", encoding="utf-8") as f:
            f.write("\n".join(linhas) + "\n")


if __name__ == "__main__":
    main()
