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
| `enviar.py` | envia a rodada ao R2, atualiza o histórico, manda o manifesto por último e deixa só as duas rodadas mais recentes |
| `historico.py` | o passado do modo: horas 1 a 6 de cada rodada que saiu de cena, quadro diário (média, 0,5°) e o trecho de cada um no manifesto |
| `arquivo.py` | formato PNA1 do arquivo horário no Hugging Face: um `.bin` por camada e mês, índice no começo |
| `arquivo_lote.py` | gera o arquivo horário em pedaços de 4 rodadas (um job cada) e junta o mês (`.github/workflows/arquivo.yml`) |
| `ocf.py` | leitor do arquivo zarr da Open Climate Fix (03/2023 a 08/2025): blocos por faixa e o float64 do GRIB reconstruído do float32 |
| `era5.py` | agua, cape e neve do ERA5 (cópia ARCO-ERA5 do Google) nas horas da DREAM, com origem 4 no índice (`.github/workflows/era5.yml`) |
| `lotes.py` | lista os lotes do passado, do mais novo para o mais antigo, com a estimativa de tempo, e dispara (`--disparar`) |
| `test_historico.py` | testes do histórico e do fluxo do `enviar.py` com um R2 de mentira: `python -m unittest test_historico` |

## Passado e previsão numa série só

A rodada atual é a previsão (`mundo/<rodada>/<camada>/<hhh>.webp`). O passado vem das rodadas que saem de cena: a cada
publicação, as horas **1 a 6** de cada rodada anterior (as seis horas até a rodada seguinte, sem buraco) são copiadas,
dentro do R2 e sem subir nada de novo, para chaves organizadas por dia:

```
mundo/historico/AAAA/MM/DD/<camada>/HH.webp    uma hora, na grade da previsão (0,125°)
mundo/historico/AAAA/MM/DD/<camada>/dia.webp   o dia inteiro, média do dia em 0,5° (com --compactar)
```

Horas 1 a 6 e não 0 a 5 porque a chuva é a diferença de dois acumulados e a hora 0 sai zerada, o que piscaria a chuva a
cada 6 horas. O DWD só guarda cerca de um dia de rodadas, então não há como preencher o passado de trás para frente: o
histórico cresce 6 horas a cada rodada publicada (7 dias levam uma semana).

O `manifest.json` (versão 2) mantém os campos da versão 1 (a previsão) e ganha `serie.trechos`, do mais antigo ao mais
novo, cada um com `passo_h`, `inicio` e `fim`, `grade` (se for diferente da do manifesto), `formato`, `url` (modelo
relativo à base, com `{aaaa} {mm} {dd} {camada} {hh}`) e `lacunas` (intervalos sem quadro). Um trecho novo, com outra
grade ou outro passo (a reanálise ERA5 diária de 0,5° de 2015 em diante, por exemplo), entra sem mudar o cliente, e o
manifesto não cresce com a quantidade de quadros: cada trecho é uma regra, não uma lista.

Opções do `enviar.py` (nenhuma é usada no fluxo do GitHub por enquanto, então **nada do histórico é apagado**):

* `--compactar` grava o quadro diário dos dias encerrados (média do dia no valor físico; rajada pelo máximo; chuva pela
  taxa média);
* `--podar-horario-dias N` apaga os quadros horários com mais de N dias, só dos dias que já têm o quadro diário;
* `--sem-historico` publica como antes, sem `serie`.

Espaço: uma hora de todas as camadas ocupa cerca de 13 MB (WebP sem perda), ou seja 310 MB por dia e 2,2 GB para 7 dias
horários; o quadro diário de 0,5° pesa cerca de 1/16 do horário. Sem poda, o horário cresce 9 GB por mês.

Variáveis: vento a 10 m (com rajada), temperatura, sensação térmica, ponto de orvalho e umidade a 2 m, chuva (com
neve), altura da neve, nuvens, pressão ao nível do mar, água precipitável e CAPE.

**Dados:** DWD (ICON), [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/), <https://opendata.dwd.de/>.

## Arquivo horário no Hugging Face

O passado de 0,125° hora a hora vai para o dataset público `richardmelo/painel-nacional-icon`, em
`<camada>/<AAAA>/<AAAA-MM>.bin` (formato em `arquivo.py`) e `meses/<AAAA-MM>.json`. O workflow **Arquivo horário**
corta cada mês em pedaços de 4 rodadas (um dia), um job por pedaço, até 20 ao mesmo tempo e 256 por disparo (8 meses).
Cada job baixa os GRIB das suas rodadas de uma vez e faz uma rodada por núcleo; os quadros sobem como artefato, e um
job por mês junta tudo e envia. O quadro tem os mesmos pixels do `gerar.py` (`--conferir` gera uma rodada por mês pelo `gerar.py` e compara os
códigos decodificados); o WebP sem perda do arquivo sai com `method=0, quality=0`, 10 vezes mais rápido que o
`method=4` do `gerar.py` e uns 65% maior.

Fontes por período: cópias do opendata no source.coop (`bkr/icon/icon-global-raw`) de 12/03/2026 em diante; arquivo
zarr da Open Climate Fix (`bkr/icon/icon`, `ocf.py`) de 21/03/2023 a 15/08/2025, com os buracos de 28-29/05/2023,
16/03 a 04/06/2024 e 27/01 a 03/04/2025. O float32 da OCF volta exatamente ao float64 que o eccodes dá ao GRIB (o GRIB
guarda inteiros de 16 bits num passo 2^E, mais grosso que o arredondamento do float32), então os quadros são os mesmos.
Antes da OCF (2015-01 a 20/03/2023), entre o fim dela e as cópias brutas (rodadas de 12/08/2025 06 UTC a 11/03/2026)
e nos buracos da OCF, a fonte é a reanálise ICON-DREAM-Global do DWD (`leitor_dream.py`, CC BY 4.0,
doi:10.5676/dwd/icon-dream_v1), que no opendata é contínua de 2010-01 até o mês passado. Ela roda na mesma grade R03B07
do ICON global (`numberOfGridUsed` 26, 2.949.120 células), então os pesos `pesos-icon-0125-v1` do cache servem. Cada
variável vem de um GRIB mensal, lido por faixa só nas horas pedidas. Mesmas siglas e unidades para t_2m, td_2m, pmsl,
clct, u_10m, v_10m e vmax_10m (esta com janela de 1 a 3 h, desde o início do ciclo de 3 h); a chuva horária da DREAM
vira o acumulado desde a rodada; relhum_2m sai de T e Td com a pressão de saturação do ICON; snow_gsp e snow_con não
existem (fração de neve 0). TQV, CAPE_ML e H_SNOW não existem em passo nenhum: nas horas da DREAM as camadas `agua`,
`cape` e `neve` ficam sem quadro (tamanho 0 no índice) e o resto da hora entra com origem 3. Com `--conferir`, o
primeiro pedaço de um mês da DREAM gera também a rodada 2026091500 da DREAM e compara os valores decodificados com os
quadros operacionais do mesmo horário já no HF (viés, RMSE, correlação, PNG lado a lado no artefato).

O `step` dos zarr da OCF é int64 (85 passos) até abril de 2024 e float64 (73 passos) depois; o `ocf.py` lê pelo
`dtype` e pelas `units` do zarr. Passo que o zarr não tem vira passo sem quadro; zip que não existe, que tem o zarr de
outra rodada (2024/4/5/..._00 diz 2024-04-08) ou que não fecha vira rodada sem cópia. Se o opendata do DWD não
entregar a DREAM (`leitor_dream.responde()`), as rodadas dela e os buracos da OCF também ficam sem cópia, sem derrubar o
dia. O juntar não envia um mês com 0 horas nem com menos horas do que o `meses/AAAA-MM.json` do HF já tem.

### agua, cape e neve do ERA5 nas horas da DREAM

`era5.py` (workflow **ERA5 nas horas da DREAM**) preenche essas três camadas com a reanálise ERA5 do ECMWF (CC BY 4.0,
doi:10.24381/cds.adbb2d47), lida da cópia pública ARCO-ERA5 do Google Cloud por HTTPS anônimo (zarr, um pedaço por
hora e variável, 0,25°): `total_column_water_vapour` para agua, `convective_available_potential_energy` para cape e
`snow_depth` (m de água) x 1000 / `snow_density` (kg/m³) para neve, a altura em metros como o H_SNOW. A conversão é a
0,25°, a interpolação para 0,125° é bilinear com a longitude periódica, e o quadro sai do mesmo `gerar.quadro_escalar`
(mesma escala, unidade, bits e raiz) com o WebP do arquivo. O índice marca esses quadros com origem 4. Só as horas de
origem 3 da t2m em que a camada está vazia são geradas; os quadros que já existem (o ICON nos meses mistos) passam byte a
byte, e só os três `.bin` e `meses/<AAAA-MM>-era5.json` sobem. O juntar do `arquivo_lote.py` mantém os quadros de origem
4 que o HF já tem (`manter_era5`), então refazer o lote de um mês não apaga o ERA5.

Diferenças esperadas contra o ICON (conferência em 2024-08, `python era5.py conferir --mes 2024-08`): TQV quase igual
(correlação 0,99); o CAPE do ERA5 é o da parcela mais instável e o do ICON o da camada de mistura (correlação ~0,83,
ERA5 menor sobre o oceano tropical); a neve bate fora das geleiras, e nas geleiras e ilhas do Ártico o ERA5 fixa 10 m de
água, que satura a escala de 4 m.

GRIB ausente na cópia (404) não derruba o pedaço: o passo que depende dele (e o seguinte, se o arquivo for de um
acumulado) fica sem quadro, e o mês registra `grib_ausentes` e `passos_sem_quadro` no `meses/<AAAA-MM>.json`.

Segredos do repositório: `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `HF_TOKEN`.
