"""Publica uma rodada do modo Mundo no bucket R2 do Painel Nacional.

Envia os quadros da rodada (pulando o que já está lá com o mesmo tamanho), manda o manifest.json por último, para
o site só apontar para a rodada nova quando os quadros já chegaram, e apaga as rodadas mais velhas, deixando as duas
mais recentes (quem estava com o site aberto na rodada anterior não perde os quadros).

Chaves no ambiente: R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY (segredos do repositório).
Uso: python enviar.py --saida <pasta de gerar.py> [--prefixo mundo] [--manter 2]
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import re
import time

import boto3
from botocore.config import Config

TIPOS = {".json": "application/json", ".png": "image/png", ".webp": "image/webp"}


def cliente():
    conta = os.environ["R2_ACCOUNT_ID"]
    return boto3.client(
        "s3",
        endpoint_url=f"https://{conta}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
        config=Config(retries={"max_attempts": 8, "mode": "adaptive"}, max_pool_connections=32),
    )


def listar(s3, bucket: str, prefixo: str) -> dict[str, int]:
    saida: dict[str, int] = {}
    for pagina in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefixo):
        for o in pagina.get("Contents", []):
            saida[o["Key"]] = o["Size"]
    return saida


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--saida", required=True)
    ap.add_argument("--bucket", default="painel-dados")
    ap.add_argument("--prefixo", default="mundo")
    ap.add_argument("--manter", type=int, default=2, help="quantas rodadas ficam no bucket")
    ap.add_argument("--paralelo", type=int, default=16)
    a = ap.parse_args()

    with open(os.path.join(a.saida, "manifest.json"), encoding="utf-8") as f:
        manifesto = json.load(f)
    rodada = manifesto["caminho"]
    pasta = os.path.join(a.saida, rodada)
    s3 = cliente()

    locais = []
    for raiz, _, nomes in os.walk(pasta):
        for n in nomes:
            cam = os.path.join(raiz, n)
            rel = os.path.relpath(cam, a.saida).replace(os.sep, "/")
            locais.append((cam, f"{a.prefixo}/{rel}", os.path.getsize(cam)))
    remotos = listar(s3, a.bucket, f"{a.prefixo}/{rodada}/")
    pendentes = [x for x in locais if remotos.get(x[1]) != x[2]]
    total = sum(x[2] for x in pendentes)
    print(f"rodada {rodada}: {len(locais)} quadros, {len(pendentes)} a enviar ({total / 2**20:.0f} MiB)", flush=True)

    def enviar(x):
        cam, chave, _ = x
        ext = os.path.splitext(cam)[1]
        s3.upload_file(cam, a.bucket, chave, ExtraArgs={"ContentType": TIPOS.get(ext, "application/octet-stream")})

    t0 = time.time()
    with cf.ThreadPoolExecutor(a.paralelo) as ex:
        for k, _ in enumerate(ex.map(enviar, pendentes), 1):
            if k % 100 == 0 or k == len(pendentes):
                print(f"  {k}/{len(pendentes)} ({time.time() - t0:.0f} s)", flush=True)

    # o manifesto por último: a partir daqui o site vê a rodada nova
    s3.upload_file(
        os.path.join(a.saida, "manifest.json"),
        a.bucket,
        f"{a.prefixo}/manifest.json",
        ExtraArgs={"ContentType": "application/json"},
    )
    print("manifesto publicado", flush=True)

    # rodadas velhas saem, ficam as `manter` mais recentes
    todas = listar(s3, a.bucket, f"{a.prefixo}/")
    rodadas = sorted({m.group(1) for k in todas if (m := re.match(rf"{re.escape(a.prefixo)}/(\d{{10}})/", k))})
    velhas = rodadas[: -a.manter] if len(rodadas) > a.manter else []
    apagar = [k for k in todas if any(k.startswith(f"{a.prefixo}/{r}/") for r in velhas)]
    for i in range(0, len(apagar), 1000):
        s3.delete_objects(Bucket=a.bucket, Delete={"Objects": [{"Key": k} for k in apagar[i : i + 1000]]})
    if velhas:
        print(f"rodadas apagadas: {', '.join(velhas)} ({len(apagar)} arquivos)", flush=True)
    print("pronto", flush=True)


if __name__ == "__main__":
    main()
