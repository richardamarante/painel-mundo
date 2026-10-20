import os
import struct
import tempfile
import unittest

import arquivo


class TestArquivo(unittest.TestCase):
    def test_ida_e_volta(self):
        with tempfile.TemporaryDirectory() as d:
            quadros = {}
            for h in (0, 5, 719):
                p = os.path.join(d, f"{h}.webp")
                with open(p, "wb") as f:
                    f.write(os.urandom(100 + h))
                quadros[h] = (p, arquivo.ORIGEM_ICON)
            destino = os.path.join(d, arquivo.caminho_no_repo("t2m", 2026, 9))
            tam = arquivo.gravar(destino, "t2m", 2026, 9, quadros)
            self.assertEqual(tam, os.path.getsize(destino))
            with open(destino, "rb") as f:
                cab = arquivo.ler_cabecalho(f.read(arquivo.TAM_CABECALHO))
            self.assertEqual((cab["camada"], cab["ano"], cab["mes"], cab["horas"], cab["formato"]), ("t2m", 2026, 9, 720, "webp"))
            for h, (p, _) in quadros.items():
                with open(p, "rb") as f:
                    self.assertEqual(arquivo.ler_quadro(destino, h), f.read())
            self.assertIsNone(arquivo.ler_quadro(destino, 1))
            self.assertEqual(cab["indice"][0][0], arquivo.TAM_CABECALHO)
            self.assertEqual(arquivo.TAM_CABECALHO, 11968)
            self.assertEqual(struct.calcsize("<4sHHHBB16s36x"), arquivo.TAM_FIXO)


class TestTrechoDoArquivo(unittest.TestCase):
    def test_trecho_com_lacuna(self):
        import historico

        t = historico.trecho_do_arquivo([{"mes": "2026-09", "horas": [0, 1, 3]}, {"mes": "2026-10", "horas": [0]}], "u/{camada}")
        self.assertEqual((t["inicio"], t["fim"], t["arquivo"], t["passo_h"]), ("2026-09-01T00:00:00Z", "2026-10-01T00:00:00Z", "pna1", 1))
        self.assertEqual(t["lacunas"][0], ["2026-09-01T02:00:00Z", "2026-09-01T02:00:00Z"])
        self.assertEqual(t["lacunas"][1], ["2026-09-01T04:00:00Z", "2026-09-30T23:00:00Z"])
        self.assertIsNone(historico.trecho_do_arquivo([], "u"))


if __name__ == "__main__":
    unittest.main()
