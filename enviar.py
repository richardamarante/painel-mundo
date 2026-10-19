"""Publica uma rodada do modo Mundo no bucket R2 do Painel Nacional.

Envia os quadros da rodada (pulando o que já está lá com o mesmo tamanho), manda o manifest.json por último, para
o site só apontar para a rodada nova quando os quadros já chegaram, e apaga as rodadas mais velhas, deixando as duas
mais recentes (quem estava com o site aberto na rodada anterior não perde os quadros).

Antes do manifesto, o histórico (historico.py): as horas 1 a 6 de cada rodada que saiu de cena são copiadas, dentro do
próprio R2, para `historico/AAAA/MM/DD/<camada>/HH.<formato>`, e o manifesto ganha `serie.trechos` com o que o bucket
tem do passado. `--compactar` grava também o quadro diário (média do dia, 0,5 grau) dos dias encerrados;
`--podar-horario-dias N` apaga os quadros horários com mais de N dias, mas só dos dias que já têm o quadro diário.
Sem essas duas opções nada do histórico é apagado.

Chaves no ambiente: R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY (segredos do repositório).
Uso: python enviar.py --saida <pasta de gerar.py> [--prefixo mundo] [--manter 2]
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import json
import os
import re
import time

import boto3
from botocore.config import Config

import historico

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


def atualizar_historico(s3, a, manifesto: dict, rodada: str) -> None:
    """Copia as horas das rodadas antigas para o histórico, compacta e poda se pedido, e põe `serie` no manifesto."""
    camadas = sorted(manifesto["camadas"])
    formato = manifesto["formato"]
    existentes = listar(s3, a.bucket, f"{a.prefixo}/")

    pares = historico.copias_a_fazer(existentes, a.prefixo, rodada, camadas, formato)

    def copiar(par):
        origem, destino = par
        s3.copy_object(Bucket=a.bucket, Key=destino, CopySource={"Bucket": a.bucket, "Key": origem})
        return destino, existentes[origem]

    with cf.ThreadPoolExecutor(a.paralelo) as ex:
        for destino, tamanho in ex.map(copiar, pares):
            existentes[destino] = tamanho
    print(f"histórico: {len(pares)} quadros copiados das rodadas anteriores", flush=True)

    if a.compactar:
        hoje = dt.datetime.now(dt.timezone.utc).date()
        for dia in historico.dias_a_compactar(existentes, camadas, hoje):
            n = historico.compactar_dia(s3, a.bucket, a.prefixo, dia, camadas, formato)
            print(f"histórico: {dia} -> {n} quadros diários", flush=True)
        existentes = listar(s3, a.bucket, f"{a.prefixo}/")

    if a.podar_horario_dias > 0:
        limite = historico.instante_da_rodada(rodada, 0) - dt.timedelta(days=a.podar_horario_dias)
        apagar = historico.chaves_a_podar(existentes, camadas, limite)
        for i in range(0, len(apagar), 1000):
            s3.delete_objects(Bucket=a.bucket, Delete={"Objects": [{"Key": k} for k in apagar[i : i + 1000]]})
        for k in apagar:
            existentes.pop(k, None)
        print(f"histórico: {len(apagar)} quadros horários antigos apagados (o dia já tem média)", flush=True)

    manifesto["versao"] = 2
    manifesto["serie"] = historico.serie_do_manifesto(existentes, camadas, formato)
    with open(os.path.join(a.saida, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifesto, f, ensure_ascii=False, indent=1)
    for t in manifesto["serie"]["trechos"]:
        print(f"  trecho {t['id']}: {t['inicio']} a {t['fim']} (passo {t['passo_h']} h, {len(t['lacunas'])} lacunas)", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--saida", required=True)
    ap.add_argument("--bucket", default="painel-dados")
    ap.add_argument("--prefixo", default="mundo")
    ap.add_argument("--manter", type=int, default=2, help="quantas rodadas ficam no bucket")
    ap.add_argument("--paralelo", type=int, default=16)
    ap.add_argument("--sem-historico", action="store_true", help="não mexe no histórico e não põe `serie` no manifesto")
    ap.add_argument("--compactar", action="store_true", help="grava o quadro diário (média do dia, 0,5 grau) dos dias encerrados")
    ap.add_argument("--podar-horario-dias", type=int, default=0, help="apaga os quadros horários com mais de N dias que já têm quadro diário (0 = nunca)")
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

    if not a.sem_historico:
        # o histórico é um acréscimo: se falhar, a previsão sai do mesmo jeito (o site só perde o passado nesta rodada)
        try:
            atualizar_historico(s3, a, manifesto, rodada)
        except Exception as e:  # noqa: BLE001
            print(f"AVISO: histórico não atualizado ({e!r}); a rodada segue sem a série", flush=True)

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
