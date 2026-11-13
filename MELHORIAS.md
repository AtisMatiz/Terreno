# Melhorias a fazer (registro)

Itens conhecidos que ficaram para depois. Atualizado em 2026-11-06.

## OLX
- **Aluguel do OLX rende 0 hoje** (mantido como está, por decisão do dono). A URL de aluguel
  com região (`/imoveis/aluguel/estado-sp/<região>`) ignora a região: 0 a 3 de ~49 anúncios
  são da região. A palavra `q=sitio|chacara` funciona como filtro de texto, mas os poucos
  anúncios locais são descartados como "não parece imóvel rural". Testado: `q=chacara+taubate`
  devolve só o Vale, mas casa bairros urbanos ("Chácara Guizard"). Ideias: busca por
  `q=<tipo>+<município rural>` só para municípios rurais; área vinda da página de detalhe.
- **MG sem região no OLX**: `regiao-de-pouso-alegre-e-itajuba` (0/50) e
  `regiao-de-pocos-de-caldas-e-varginha` (1-2/50) não funcionam como página de região. MG
  entra só pela página do estado inteiro (poucos anúncios na área). Falta achar o slug certo
  do Sul de Minas (ver "regiões vistas nas URLs" no log).
- **SP vale-do-paraiba**: a página de região também mistura anúncios de fora (0 a 10 de 50 na
  região em algumas rodadas, quando o Cloudflare serve o HTTP). O filtro de município corta,
  mas rende pouco.
- **Cloudflare**: o navegador do Playwright é barrado com frequência; o curl_cffi cobre o
  grosso e o ZenRows (teto de 15 por rodada) acaba cedo no OLX.

## Leilão
- Spy, Frazão, Resale e Milan ainda sem lotes (opção: renderizar no navegador).
- Município via Facebook: melhorar a busca (muitos "?/SP").

## Cobertura
- Lacuna: terreno acima de 3 ha com preço abaixo de R$ 100 mil não entra em nenhuma seção.

## Telegram
- Causa raiz da lentidão da API do Telegram a partir do runner é desconhecida. Relé na Vercel
  só se voltar a falhar. Desde 2026-11-06 um timeout de leitura nunca reenvia (evita
  mensagens duplicadas).

## Município
- Páginas de listagem por cidade (ex.: mohallemimoveis.com.br/.../campos-do-jordao-sp) trazem
  o texto do primeiro anúncio, que pode ser de outra cidade (Piranguçu/MG saiu como
  "Campos do Jordão - SP"). Ideia: quando o título diz "área rural de <lugar>" e o lugar não
  é da região, descartar. O caso de 2026-11-13 foi dispensado à mão (data/vendidos.json).
