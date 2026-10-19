"""Testes do histórico (sem rede e sem R2): python -m unittest test_historico"""

from __future__ import annotations

import datetime as dt
import io
import unittest

import numpy as np

import historico as h
from gerar import CAMADAS, NX, NY, codigo, quadro_chuva, quadro_escalar, quadro_vento

UTC = dt.timezone.utc
CAMADAS_TESTE = ["t2m", "vento"]


def bucket_com_rodadas(rodadas: list[str], horas=range(0, 10), camadas=CAMADAS_TESTE, ext="webp") -> dict[str, int]:
    return {f"mundo/{r}/{c}/{x:03d}.{ext}": 100 + x for r in rodadas for c in camadas for x in horas}


class Chaves(unittest.TestCase):
    def test_chaves_por_dia(self):
        t = dt.datetime(2026, 10, 9, 7, tzinfo=UTC)
        self.assertEqual(h.chave_horaria("mundo", t, "t2m", "webp"), "mundo/historico/2026/10/09/t2m/07.webp")
        self.assertEqual(h.chave_diaria("mundo", t.date(), "vento", "webp"), "mundo/historico/2026/10/09/vento/dia.webp")

    def test_hora_de_cada_rodada_vira_o_instante_certo(self):
        self.assertEqual(h.instante_da_rodada("2026101818", 6), dt.datetime(2026, 10, 19, 0, tzinfo=UTC))


class Copias(unittest.TestCase):
    def test_so_rodadas_anteriores_e_horas_1_a_6(self):
        ex = bucket_com_rodadas(["2026101806", "2026101812"])
        pares = h.copias_a_fazer(ex, "mundo", "2026101812", CAMADAS_TESTE, "webp")
        destinos = {d for _, d in pares}
        self.assertEqual(len(pares), 6 * 2)
        self.assertIn("mundo/historico/2026/10/18/t2m/07.webp", destinos)  # 06 + 1 h
        self.assertIn("mundo/historico/2026/10/18/vento/12.webp", destinos)  # 06 + 6 h
        self.assertNotIn("mundo/historico/2026/10/18/t2m/06.webp", destinos)  # hora 0 não entra
        self.assertTrue(all(o.startswith("mundo/2026101806/") for o, _ in pares))

    def test_nao_copia_de_novo(self):
        ex = bucket_com_rodadas(["2026101806", "2026101812"])
        for o, d in h.copias_a_fazer(ex, "mundo", "2026101812", CAMADAS_TESTE, "webp"):
            ex[d] = ex[o]
        self.assertEqual(h.copias_a_fazer(ex, "mundo", "2026101812", CAMADAS_TESTE, "webp"), [])

    def test_hora_incompleta_nao_entra(self):
        ex = bucket_com_rodadas(["2026101806", "2026101812"])
        del ex["mundo/2026101806/vento/003.webp"]
        destinos = {d for _, d in h.copias_a_fazer(ex, "mundo", "2026101812", CAMADAS_TESTE, "webp")}
        self.assertNotIn("mundo/historico/2026/10/18/t2m/09.webp", destinos)
        self.assertNotIn("mundo/historico/2026/10/18/vento/09.webp", destinos)
        self.assertEqual(len(destinos), 5 * 2)


class Trechos(unittest.TestCase):
    def historico(self, rodadas):
        ex = bucket_com_rodadas(rodadas + ["2099010100"])
        for o, d in h.copias_a_fazer(ex, "mundo", "2099010100", CAMADAS_TESTE, "webp"):
            ex[d] = ex[o]
        return {k: v for k, v in ex.items() if "/historico/" in k}

    def test_serie_continua_sem_lacuna(self):
        ex = self.historico(["2026101800", "2026101806", "2026101812"])
        (tr,) = h.serie_do_manifesto(ex, CAMADAS_TESTE, "webp")["trechos"]
        self.assertEqual((tr["id"], tr["passo_h"]), ("horario", 1))
        self.assertEqual((tr["inicio"], tr["fim"]), ("2026-10-18T01:00:00Z", "2026-10-18T18:00:00Z"))
        self.assertEqual(tr["lacunas"], [])
        self.assertEqual(tr["url"], "historico/{aaaa}/{mm}/{dd}/{camada}/{hh}")

    def test_rodada_que_falta_vira_lacuna(self):
        ex = self.historico(["2026101800", "2026101812"])
        (tr,) = h.serie_do_manifesto(ex, CAMADAS_TESTE, "webp")["trechos"]
        self.assertEqual(tr["lacunas"], [["2026-10-18T07:00:00Z", "2026-10-18T12:00:00Z"]])

    def test_so_entra_hora_com_todas_as_camadas(self):
        ex = self.historico(["2026101800"])
        del ex["mundo/historico/2026/10/18/vento/03.webp"]
        (tr,) = h.serie_do_manifesto(ex, CAMADAS_TESTE, "webp")["trechos"]
        self.assertEqual(tr["lacunas"], [["2026-10-18T03:00:00Z", "2026-10-18T03:00:00Z"]])

    def test_trecho_diario_entra_antes_do_horario(self):
        ex = self.historico(["2026101800"])
        for d in (8, 9, 11):
            for c in CAMADAS_TESTE:
                ex[f"mundo/historico/2026/10/{d:02d}/{c}/dia.webp"] = 10
        trechos = h.serie_do_manifesto(ex, CAMADAS_TESTE, "webp")["trechos"]
        self.assertEqual([t["id"] for t in trechos], ["diario", "horario"])
        d = trechos[0]
        self.assertEqual((d["passo_h"], d["inicio"], d["fim"]), (24, "2026-10-08T12:00:00Z", "2026-10-11T12:00:00Z"))
        self.assertEqual(d["lacunas"], [["2026-10-10T12:00:00Z", "2026-10-10T12:00:00Z"]])
        self.assertEqual((d["grade"]["dx"], d["grade"]["nx"]), (0.5, 720))

    def test_serie_vazia(self):
        self.assertEqual(h.serie_do_manifesto({}, CAMADAS_TESTE, "webp"), {"trechos": []})


class Poda(unittest.TestCase):
    def test_so_poda_horas_de_dia_com_quadro_diario(self):
        ex = {}
        for dia in (1, 2):
            for hh in range(24):
                for c in CAMADAS_TESTE:
                    ex[f"mundo/historico/2026/10/{dia:02d}/{c}/{hh:02d}.webp"] = 1
        for c in CAMADAS_TESTE:
            ex[f"mundo/historico/2026/10/01/{c}/dia.webp"] = 1  # só o dia 1 tem média
        limite = dt.datetime(2026, 10, 10, tzinfo=UTC)
        apagar = h.chaves_a_podar(ex, CAMADAS_TESTE, limite)
        self.assertEqual(len(apagar), 24 * 2)
        self.assertTrue(all("/10/01/" in k and not k.endswith("dia.webp") for k in apagar))

    def test_dia_incompleto_nao_perde_as_horas(self):
        ex = {f"mundo/historico/2026/10/01/{c}/{hh:02d}.webp": 1 for hh in range(24) for c in CAMADAS_TESTE}
        ex["mundo/historico/2026/10/01/t2m/dia.webp"] = 1  # falta o vento
        self.assertEqual(h.chaves_a_podar(ex, CAMADAS_TESTE, dt.datetime(2026, 10, 10, tzinfo=UTC)), [])

    def test_dias_a_compactar(self):
        ex = {f"mundo/historico/2026/10/01/{c}/{hh:02d}.webp": 1 for hh in range(24) for c in CAMADAS_TESTE}
        ex.update({f"mundo/historico/2026/10/02/{c}/{hh:02d}.webp": 1 for hh in range(24) for c in CAMADAS_TESTE})
        self.assertEqual(h.dias_a_compactar(ex, CAMADAS_TESTE, dt.date(2026, 10, 2)), [dt.date(2026, 10, 1)])  # o dia de hoje não


class MediaDiaria(unittest.TestCase):
    def test_grade_diaria_cobre_o_mundo(self):
        g = h.GRADE_DIARIA
        self.assertAlmostEqual(g["nx"] * g["dx"], 360.0)
        self.assertEqual((g["nx"], g["ny"]), (720, 340))
        self.assertAlmostEqual(g["lat0"], 84.9375)
        self.assertAlmostEqual(g["lat0"] - (g["ny"] - 1) * g["dy"], -84.5625)  # as 3 últimas linhas (abaixo de 84,8 S) ficam de fora

    def test_escalar_media_em_valor_fisico(self):
        c = CAMADAS["t2m"]
        a = quadro_escalar(np.full((NY, NX), 10.0), c)
        b = quadro_escalar(np.full((NY, NX), 20.0), c)
        r = h.media_do_dia("t2m", [a, b])
        self.assertEqual(r.shape, (340, 720, 3))
        valor = h._escalar(r, c)
        np.testing.assert_allclose(valor, 15.0, atol=0.15)

    def test_escalar_em_raiz(self):
        c = CAMADAS["cape"]
        a = quadro_escalar(np.full((NY, NX), 0.0), c)
        b = quadro_escalar(np.full((NY, NX), 1000.0), c)
        valor = h._escalar(h.media_do_dia("cape", [a, b]), c)
        np.testing.assert_allclose(valor, 500.0, atol=40.0)  # a raiz quantiza grosso: tolerância

    def test_vento_media_dos_componentes_e_maximo_da_rajada(self):
        c = CAMADAS["vento"]
        q1 = quadro_vento(np.full((NY, NX), 10.0), np.full((NY, NX), -4.0), np.full((NY, NX), 20.0), c)
        q2 = quadro_vento(np.full((NY, NX), 20.0), np.full((NY, NX), 4.0), np.full((NY, NX), 30.0), c)
        r = h.media_do_dia("vento", [q1, q2])
        u = h._codigo_para_valor(r[..., 0], c["min"], c["max"], 8, False)
        v = h._codigo_para_valor(r[..., 1], c["min"], c["max"], 8, False)
        rajada = h._codigo_para_valor(r[..., 2], c["gmin"], c["gmax"], 8, False)
        np.testing.assert_allclose(u, 15.0, atol=0.5)
        np.testing.assert_allclose(v, 0.0, atol=0.5)
        np.testing.assert_allclose(rajada, 30.0, atol=0.4)

    def test_chuva_taxa_media_e_neve_ponderada(self):
        c = CAMADAS["chuva"]
        sem = quadro_chuva(np.zeros((NY, NX)), np.zeros((NY, NX)), c)
        neve = quadro_chuva(np.full((NY, NX), 16.0), np.ones((NY, NX)), c)
        r = h.media_do_dia("chuva", [sem, neve])
        taxa = (r[..., 0].astype(float) / 255.0) ** 2 * c["max"]
        np.testing.assert_allclose(taxa, 8.0, atol=0.3)
        np.testing.assert_allclose(r[..., 1] / 255.0, 1.0, atol=0.01)  # a pouca chuva que houve foi toda neve

    def test_chuva_sem_chuva_nao_divide_por_zero(self):
        c = CAMADAS["chuva"]
        sem = quadro_chuva(np.zeros((NY, NX)), np.zeros((NY, NX)), c)
        r = h.media_do_dia("chuva", [sem, sem])
        self.assertEqual(int(r.max()), 0)


class FalsoS3:
    def __init__(self, objetos):
        self.objetos = dict(objetos)

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objetos[Key])}

    def put_object(self, Bucket, Key, Body, ContentType):
        self.objetos[Key] = Body


class Compactacao(unittest.TestCase):
    def test_compactar_dia_grava_um_quadro_por_camada(self):
        dia = dt.date(2026, 10, 1)
        c = CAMADAS["t2m"]
        objetos = {}
        for hh in range(24):
            t = dt.datetime(2026, 10, 1, hh, tzinfo=UTC)
            objetos[h.chave_horaria("mundo", t, "t2m", "png")] = h.codificar_imagem(quadro_escalar(np.full((NY, NX), float(hh)), c), "png")
        s3 = FalsoS3(objetos)
        n = h.compactar_dia(s3, "b", "mundo", dia, ["t2m"], "png")
        self.assertEqual(n, 1)
        saida = h._decodificar_imagem(s3.objetos["mundo/historico/2026/10/01/t2m/dia.png"])
        self.assertEqual(saida.shape, (340, 720, 3))
        np.testing.assert_allclose(h._escalar(saida, c), 11.5, atol=0.15)

    def test_dia_com_poucas_horas_fica_sem_quadro(self):
        c = CAMADAS["t2m"]
        objetos = {
            h.chave_horaria("mundo", dt.datetime(2026, 10, 1, hh, tzinfo=UTC), "t2m", "png"): h.codificar_imagem(quadro_escalar(np.zeros((NY, NX)), c), "png")
            for hh in range(10)
        }
        self.assertEqual(h.compactar_dia(FalsoS3(objetos), "b", "mundo", dt.date(2026, 10, 1), ["t2m"], "png"), 0)


class S3Completo(FalsoS3):
    """O que o enviar.py usa: listar (paginator), copy_object e delete_objects."""

    def __init__(self, objetos):
        super().__init__(objetos)
        self.copias = 0
        self.apagados = []

    def get_paginator(self, _nome):
        s3 = self

        class Pag:
            def paginate(self, Bucket, Prefix):
                yield {"Contents": [{"Key": k, "Size": len(v) if isinstance(v, bytes) else v} for k, v in sorted(s3.objetos.items()) if k.startswith(Prefix)]}

        return Pag()

    def copy_object(self, Bucket, Key, CopySource):
        self.objetos[Key] = self.objetos[CopySource["Key"]]
        self.copias += 1

    def delete_objects(self, Bucket, Delete):
        for o in Delete["Objects"]:
            self.objetos.pop(o["Key"], None)
            self.apagados.append(o["Key"])


class Enviar(unittest.TestCase):
    """O fluxo do enviar.py com um R2 de mentira (sem boto3 instalado: o módulo é trocado por um vazio)."""

    @classmethod
    def setUpClass(cls):
        import sys
        import types

        for nome in ("boto3", "botocore", "botocore.config"):
            sys.modules.setdefault(nome, types.ModuleType(nome))
        sys.modules["botocore.config"].Config = object
        import enviar

        cls.enviar = enviar

    def manifesto(self, rodada):
        return {"caminho": rodada, "formato": "webp", "camadas": {c: {} for c in CAMADAS_TESTE}, "versao": 1}

    def args(self, **kw):
        import argparse

        base = dict(bucket="b", prefixo="mundo", saida=".", paralelo=2, compactar=False, podar_horario_dias=0, sem_historico=False)
        base.update(kw)
        return argparse.Namespace(**base)

    def rodar(self, s3, rodada, **kw):
        import os
        import tempfile

        manifesto = self.manifesto(rodada)
        with tempfile.TemporaryDirectory() as pasta:
            self.enviar.atualizar_historico(s3, self.args(saida=pasta, **kw), manifesto, rodada)
        return manifesto

    def test_copia_as_rodadas_anteriores_e_lista_o_trecho(self):
        s3 = S3Completo({k: b"x" * v for k, v in bucket_com_rodadas(["2026101806", "2026101812"]).items()})
        m = self.rodar(s3, "2026101812")
        self.assertEqual(m["versao"], 2)
        (t,) = m["serie"]["trechos"]
        self.assertEqual((t["inicio"], t["fim"], t["lacunas"]), ("2026-10-18T07:00:00Z", "2026-10-18T12:00:00Z", []))
        self.assertEqual(s3.copias, 12)
        # a segunda execução não copia de novo
        self.rodar(s3, "2026101812")
        self.assertEqual(s3.copias, 12)
        # a rodada atual e as rodadas em si não são tocadas
        self.assertEqual(s3.apagados, [])
        self.assertIn("mundo/2026101806/t2m/001.webp", s3.objetos)

    def test_nada_e_apagado_sem_pedir(self):
        s3 = S3Completo({k: b"x" * v for k, v in bucket_com_rodadas(["2026101806", "2026101812"]).items()})
        self.rodar(s3, "2026101812", compactar=False, podar_horario_dias=0)
        self.assertEqual(s3.apagados, [])

    def test_poda_so_o_que_tem_media_diaria(self):
        objetos = {k: b"x" for k in bucket_com_rodadas(["2026101812"], horas=range(0, 3))}
        for hh in range(24):
            for c in CAMADAS_TESTE:
                objetos[f"mundo/historico/2026/10/01/{c}/{hh:02d}.webp"] = b"x"
                objetos[f"mundo/historico/2026/10/02/{c}/{hh:02d}.webp"] = b"x"
        for c in CAMADAS_TESTE:
            objetos[f"mundo/historico/2026/10/01/{c}/dia.webp"] = b"x"
        s3 = S3Completo(objetos)
        m = self.rodar(s3, "2026101812", podar_horario_dias=7)
        self.assertEqual(len(s3.apagados), 48)
        self.assertTrue(all("/2026/10/01/" in k for k in s3.apagados))
        self.assertIn("mundo/historico/2026/10/02/t2m/05.webp", s3.objetos)
        self.assertEqual([t["id"] for t in m["serie"]["trechos"]], ["diario", "horario"])


if __name__ == "__main__":
    unittest.main()
