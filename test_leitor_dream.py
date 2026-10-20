"""Testes do leitor da DREAM sem rede: python -m unittest test_leitor_dream

O teste com rede (uma mensagem de verdade, por faixa) só roda com DREAM_REDE=1.
"""

import datetime as dt
import os
import struct
import tempfile
import unittest
from unittest import mock

import numpy as np

import leitor_dream as L

UTC = dt.timezone.utc


def _mensagem(ref: dt.datetime, passo_h: int, modelo: int = 0, fim: dt.datetime | None = None, cat: int = 0, par: int = 0) -> bytes:
    """Seções 0, 1, 3 e 4 de uma mensagem GRIB2 com o mínimo que o leitor olha, mais o fim '7777'."""
    s1 = struct.pack(">IBHHBBBHBBBBBBB", 21, 1, 78, 255, 28, 1, 1, ref.year, ref.month, ref.day, ref.hour, 0, 0, 0, 1)
    s3 = struct.pack(">IBBIBBHB", 35, 3, 0, L.N_CELULAS, 0, 0, 101, 6) + L.GRADE_ICON.to_bytes(3, "big") + b"\x01" + b"\xab" * 16
    corpo4 = struct.pack(">HHBBBBBHBBI", 0, modelo, cat, par, 2, 0, 0, 0, 0, 1, passo_h) + b"\x01\x00\x00\x00\x00\x00" + b"\xff" * 6
    if modelo == 8:
        corpo4 += struct.pack(">HBBBBB", fim.year, fim.month, fim.day, fim.hour, 0, 0) + b"\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
    s4 = struct.pack(">IB", 5 + len(corpo4), 4) + corpo4
    resto = s1 + s3 + s4 + b"7777"
    return b"GRIB\x00\x00\x00\x02" + struct.pack(">Q", 16 + len(resto)) + resto


class TestCabecalho(unittest.TestCase):
    def test_instantaneo(self):
        ref = dt.datetime(2018, 12, 31, 21, tzinfo=UTC)
        c = L.ler_cabecalho(_mensagem(ref, 3))
        self.assertEqual(c["validade"], dt.datetime(2019, 1, 1, 0, tzinfo=UTC))
        self.assertEqual(c["grade"], L.GRADE_ICON)
        self.assertEqual(c["tamanho"], len(_mensagem(ref, 3)))

    def test_acumulado_usa_o_fim_do_intervalo(self):
        ref = dt.datetime(2019, 1, 1, 0, tzinfo=UTC)
        c = L.ler_cabecalho(_mensagem(ref, 1, modelo=8, fim=dt.datetime(2019, 1, 1, 2, tzinfo=UTC), cat=1, par=52))
        self.assertEqual(c["validade"], dt.datetime(2019, 1, 1, 2, tzinfo=UTC))
        self.assertEqual(c["parametro"], (0, 1, 52))

    def test_cortado(self):
        m = _mensagem(dt.datetime(2019, 1, 1, tzinfo=UTC), 1)
        with self.assertRaises(ValueError):
            L.ler_cabecalho(m[:40])


class TestCampos(unittest.TestCase):
    def test_umidade(self):
        t = np.array([273.15, 300.0, 250.0])
        self.assertTrue(np.allclose(L.umidade_relativa(t, t), 100.0))
        # 30 °C com orvalho a 20 °C: ~55 % (tabela psicrométrica)
        self.assertAlmostEqual(float(L.umidade_relativa(np.array([303.15]), np.array([293.15]))[0]), 55.1, delta=0.5)

    def test_chuva_do_mes_anterior(self):
        self.assertEqual(L._meses_candidatos(dt.datetime(2019, 2, 1, 0, tzinfo=UTC)), [(2019, 2), (2019, 1)])
        self.assertEqual(L._meses_candidatos(dt.datetime(2019, 2, 1, 1, tzinfo=UTC)), [(2019, 2)])

    def test_caminho_do_cache_e_acumulado(self):
        """O gerar.py lê <cache>/<rodada>/<var>/<hhh>.grib2.bz2; a diferença de dois passos da chuva é a hora exata."""
        import gerar

        horas = {}

        def falso_baixar(cache, var, t):
            horas.setdefault(var, []).append(t)
            return f"{var}|{t:%Y%m%d%H}"

        def falso_decod(chave):
            var, t = chave.split("|")
            return np.full(L.N_CELULAS, float(int(t[-2:])) + (0.5 if var == "TOT_PREC" else 0.0))

        with tempfile.TemporaryDirectory() as d, mock.patch.object(L, "baixar_msg", falso_baixar), mock.patch.object(L, "_decodificar", falso_decod):
            c = lambda v, h: L.ler_grib(gerar.caminho_cache(d, "2019011506", v, h))
            self.assertEqual(c("t_2m", 3)[0], 9.0)  # 06 + 3 h
            self.assertEqual(c("tot_prec", 0)[0], 0.0)
            # chuva das 09 UTC = 9,5; acumulado no passo 3 menos o do passo 2
            self.assertAlmostEqual(c("tot_prec", 3)[0] - c("tot_prec", 2)[0], 9.5)
            self.assertEqual(c("snow_gsp", 4).max(), 0.0)
            with self.assertRaises(KeyError):
                c("tqv", 1)
            ur = c("relhum_2m", 1)
            self.assertTrue(np.allclose(ur, 100.0))  # T e Td falsos iguais
        self.assertEqual(sorted(set(L.CAMADAS_DREAM)), sorted(set(gerar.CAMADAS) - {"agua", "cape", "neve"}))
        for camada in L.CAMADAS_DREAM:
            for v in gerar.PRECISA[camada]:
                self.assertTrue(v in L.DIRETAS or v in L.ZERADAS or v == "relhum_2m", v)


@unittest.skipUnless(os.environ.get("DREAM_REDE") == "1", "sem rede (DREAM_REDE=1 liga)")
class TestRede(unittest.TestCase):
    def test_primeira_hora_de_2019(self):
        with tempfile.TemporaryDirectory() as d:
            t = dt.datetime(2019, 1, 1, 1, tzinfo=UTC)
            v = L._decodificar(L.baixar_msg(d, "T_2M", t))
            self.assertEqual(len(v), L.N_CELULAS)
            self.assertTrue(200 < v.min() < v.mean() < v.max() < 330)


if __name__ == "__main__":
    unittest.main()
