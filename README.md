# painel-mundo

Rotina que alimenta o modo **Mundo** do [Painel Nacional](https://painelnacional2.vercel.app/mapa?modo=mundo): baixa a
rodada mais recente do modelo global ICON, do serviço meteorológico alemão (DWD), remapeia a grade icosaédrica (~13 km)
para 0,125° regular e grava um quadro por hora e por variável, sem perda, com a escala no `manifest.json`. Os quadros
vão para um bucket Cloudflare R2, que o site lê por um Worker.

Roda sozinha no GitHub Actions quatro vezes por dia (`.github/workflows/rodada.yml`), umas 3 a 4 horas depois de
cada rodada (00, 06, 12 e 18 UTC), e só trabalha quando há rodada nova. Para rodar na mão: aba **Actions** →
**Rodada do Mundo** → **Run workflow**.

| Arquivo | O que faz |
| --- | --- |
| `gerar.py` | baixa os GRIB do DWD, remapeia e grava os quadros (WebP sem perda) e o manifesto |
| `rodada_nova.py` | compara a rodada mais recente do DWD com a que o site já mostra |
| `enviar.py` | envia a rodada ao R2, o manifesto por último, e deixa só as duas rodadas mais recentes |

Variáveis: vento a 10 m (com rajada), temperatura, sensação térmica, ponto de orvalho e umidade a 2 m, chuva (com
neve), altura da neve, nuvens, pressão ao nível do mar, água precipitável e CAPE.

**Dados:** DWD (ICON), [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/), <https://opendata.dwd.de/>.

Segredos do repositório: `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`.
