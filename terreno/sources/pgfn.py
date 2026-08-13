"""Comprei PGFN — leilões da Procuradoria-Geral da Fazenda Nacional.

Imóveis vindos de dívida ativa da União. Costumam sair bem abaixo do mercado,
então vale a pena estar na busca mesmo que o volume seja pequeno.

Contrato da API, descoberto lendo o bundle da SPA (`/config.json` aponta o
gateway, `AnuncioAPI-*.js` mostra as rotas):

    GET https://comprei.pgfn.gov.br/gateway/anuncio/publico?ufs=<codigo>&size=N

É público — `/gateway/anuncio` (sem `publico`) responde 401, este não. O `ufs`
exige um código numérico interno do Comprei (ver `CODIGO_UF` e a nota abaixo
-- não é IBGE nem a sigla, `ufs=SP` devolve erro 500). A resposta é uma
página do Spring Data (`content` / `totalElements` / `pageable`).

O host corta conexões sob rajada, por isso este módulo é o mais devagar de
todos e busca um estado por vez.

O mapeamento dos campos de cada item é deliberadamente tolerante: o formato do
`content` não pôde ser confirmado durante o desenvolvimento (o servidor cortou
a conexão antes), então cada campo é procurado entre vários nomes plausíveis e
o que faltar é preenchido pelo extrator de texto do pipeline. Na primeira
execução real, `--verbose` registra as chaves observadas para ajuste fino.

IMPORTANTE (corrigido 2026-08-13): `ufs` **não** é o código IBGE do estado --
é um código interno do próprio Comprei, sem relação com IBGE (ex.: SP é `80`,
não `35`; o `35` que a versão anterior deste módulo usava é outro estado no
sistema do Comprei -- por isso toda consulta em SP sempre voltava vazia,
mesmo com o servidor respondendo 200 e havendo anúncios reais no ar). Os
valores em `CODIGO_UF` abaixo foram obtidos empiricamente (uma consulta por
código 1..99, olhando o campo `endereco` do primeiro resultado de cada
resposta não-vazia) porque o mapeamento não está em nenhum lugar estático do
bundle da SPA -- é resolvido em runtime. Estados que devolveram sempre vazio
nesse levantamento (RS, RO, RR, AP) foram deixados de fora por não haver
como confirmar o código certo sem que o estado tenha ao menos um anúncio
ativo no momento do teste; se algum desses aparecer em `criteria.estados`,
o código loga um aviso em vez de adivinhar.
"""

from __future__ import annotations

import logging

from .. import http
from ..models import Listing
from ..units import area_to_ha, price_to_brl

log = logging.getLogger("terreno.sources.pgfn")

NAME = "pgfn"
API = "https://comprei.pgfn.gov.br/gateway/anuncio/publico"
SITE = "https://comprei.pgfn.gov.br/anuncio/detalhe"

# Códigos internos do Comprei para cada UF -- NÃO são códigos IBGE, ver nota
# no topo do arquivo. Confirmados empiricamente contra dados reais em
# 2026-08-13; RS/RO/RR/AP ficaram sem anúncios ativos durante o
# levantamento, então não há como confirmar o código certo e foram
# deliberadamente omitidos (ver `fetch()`).
CODIGO_UF = {
    "DF": 10, "GO": 11, "MT": 12, "MS": 13, "TO": 14,
    "PA": 20, "AM": 21, "AC": 22,
    "CE": 30, "MA": 31, "PI": 32,
    "PE": 40, "RN": 41, "PB": 42, "AL": 43,
    "BA": 50, "SE": 51,
    "MG": 60,
    "RJ": 70, "ES": 72,
    "SP": 80,
    "PR": 90, "SC": 91,
}

PAGE_SIZE = 50

# Palavras que indicam imóvel rural no título/descrição do lote. O Comprei
# anuncia de tudo — veículos, maquinário, imóveis urbanos —, então filtrar aqui
# evita carregar o pipeline com lotes irrelevantes.
RURAL = ("fazenda", "chacara", "chácara", "sitio", "sítio", "terreno", "gleba",
         "area rural", "área rural", "imovel rural", "imóvel rural", "lote",
         "hectare", "alqueire", "rural")


def _pick(item: dict, *names, default=""):
    """Primeiro valor não vazio entre vários nomes de campo possíveis."""
    for name in names:
        value = item.get(name)
        if value not in (None, "", [], {}):
            return value
    return default


def fetch(criteria, store, budgets) -> list[Listing]:
    out: list[Listing] = []
    max_pages = int(budgets.get("max_paginas_por_fonte", 5))
    schema_logged = False

    for uf in criteria.states:
        codigo = CODIGO_UF.get(uf.upper())
        if not codigo:
            log.warning("pgfn: sem código conhecido do Comprei para %s, pulando", uf)
            continue

        for page in range(max_pages):
            data = http.get_json(
                API,
                params={"ufs": codigo, "size": PAGE_SIZE, "page": page},
                # Sem isto, o backend Spring serve o `PageImpl` como XML em vez
                # de JSON quando o pedido chega sem uma preferência explícita
                # -- medido 2026-08-12 através do desbloqueador (ZenRows), que
                # usa seus próprios cabeçalhos-padrão de navegador na falta de
                # `custom_headers`, e um deles nunca é "Accept: application/
                # json". `data.get(...)` abaixo então recebia `None` (json()
                # falhava) mesmo com a página tendo sido buscada com sucesso.
                headers={"Accept": "application/json"},
                retries=2,
            )
            if not data:
                break

            content = data.get("content") or []
            if not content and page == 0:
                log.info("pgfn: nenhum anúncio ativo em %s", uf)
            if not content:
                break

            if not schema_logged:
                log.debug("pgfn: campos do item -> %s", sorted(content[0].keys()))
                schema_logged = True

            for item in content:
                listing = _to_listing(item, uf)
                if listing:
                    out.append(listing)

            if data.get("last") is True or len(content) < PAGE_SIZE:
                break

    log.info("pgfn: %d lotes rurais", len(out))
    return out


def _to_listing(item: dict, uf: str) -> Listing | None:
    titulo = str(_pick(item, "titulo", "nome", "descricaoResumida", "descricao"))
    descricao = str(_pick(item, "descricao", "descricaoDetalhada", "observacao"))
    texto = f"{titulo} {descricao}".lower()

    # Só imóveis: o Comprei também leiloa veículos e maquinário.
    if not any(palavra in texto for palavra in RURAL):
        return None

    identificador = _pick(item, "id", "idAnuncio", "codigo", "numeroAnuncio")
    if not identificador:
        return None

    preco = _pick(item, "valorMinimo", "valor", "preco", "valorAvaliacao",
                  "valorLance", default=None)
    endereco = _pick(item, "endereco", "localizacao", "municipio", default={})
    if isinstance(endereco, dict):
        municipio = str(_pick(endereco, "municipio", "cidade", "nomeMunicipio"))
        sigla = str(_pick(endereco, "uf", "siglaUf", "estado", default=uf))
    else:
        municipio, sigla = str(endereco), uf

    imagem = _pick(item, "urlImagem", "imagem", "foto", "urlFoto")
    if isinstance(imagem, list):
        imagem = imagem[0] if imagem else ""
    if isinstance(imagem, dict):
        imagem = _pick(imagem, "url", "uri", "src")

    return Listing(
        source=NAME,
        source_id=str(identificador),
        url=f"{SITE}/{identificador}",
        title=titulo or f"Lote PGFN {identificador}",
        description=descricao,
        price=price_to_brl(str(preco)) if preco is not None else None,
        area_ha=area_to_ha(f"{titulo} {descricao}", sigla or uf),
        municipality=municipio,
        uf=(sigla or uf)[:2].upper(),
        image=str(imagem or ""),
    )
