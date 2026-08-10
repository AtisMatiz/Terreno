"""Comprei PGFN — leilões da Procuradoria-Geral da Fazenda Nacional.

Imóveis vindos de dívida ativa da União. Costumam sair bem abaixo do mercado,
então vale a pena estar na busca mesmo que o volume seja pequeno.

Contrato da API, descoberto lendo o bundle da SPA (`/config.json` aponta o
gateway, `AnuncioAPI-*.js` mostra as rotas):

    GET https://comprei.pgfn.gov.br/gateway/anuncio/publico?ufs=<IBGE>&size=N

É público — `/gateway/anuncio` (sem `publico`) responde 401, este não. O `ufs`
é o **código IBGE numérico** do estado, não a sigla: `ufs=SP` devolve erro 500,
`ufs=35` funciona. A resposta é uma página do Spring Data
(`content` / `totalElements` / `pageable`).

O host corta conexões sob rajada, por isso este módulo é o mais devagar de
todos e busca um estado por vez.

O mapeamento dos campos de cada item é deliberadamente tolerante: o formato do
`content` não pôde ser confirmado durante o desenvolvimento (o servidor cortou
a conexão antes), então cada campo é procurado entre vários nomes plausíveis e
o que faltar é preenchido pelo extrator de texto do pipeline. Na primeira
execução real, `--verbose` registra as chaves observadas para ajuste fino.
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

# Códigos IBGE das unidades federativas — o parâmetro `ufs` exige o número.
IBGE = {
    "RO": 11, "AC": 12, "AM": 13, "RR": 14, "PA": 15, "AP": 16, "TO": 17,
    "MA": 21, "PI": 22, "CE": 23, "RN": 24, "PB": 25, "PE": 26, "AL": 27,
    "SE": 28, "BA": 29, "MG": 31, "ES": 32, "RJ": 33, "SP": 35, "PR": 41,
    "SC": 42, "RS": 43, "MS": 50, "MT": 51, "GO": 52, "DF": 53,
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
        codigo = IBGE.get(uf.upper())
        if not codigo:
            continue

        for page in range(max_pages):
            data = http.get_json(
                API,
                params={"ufs": codigo, "size": PAGE_SIZE, "page": page},
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
