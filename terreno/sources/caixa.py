"""Imóveis da Caixa — retomados de financiamento, vendidos com desconto.

A Caixa publica um CSV por estado, aberto e sem autenticação:

    https://venda-imoveis.caixa.gov.br/listaweb/Lista_imoveis_<UF>.csv

Latin-1, separado por `;`, duas linhas de cabeçalho antes dos dados. Funciona
de IP de datacenter — é uma das poucas fontes que roda no GitHub Actions.

Expectativa realista: o acervo é quase todo urbano. Em SP, de 3.162 imóveis,
2.181 são apartamentos e 781 casas; sobram 3 glebas e 2 imóveis rurais. Por
isso o filtro de tipo aqui é rígido: a fonte contribui esses poucos lotes e
nada mais, ao custo de uma requisição por estado. O desconto sobre a avaliação
costuma passar de 40%, o que compensa mantê-la.

Detalhe chato: o site fica atrás do Radware Bot Manager, que devolve uma página
de CAPTCHA (HTTP 200, `text/html`) para a `requests` mas deixa o `curl` passar.
A diferença é a impressão digital do TLS, não os cabeçalhos — mandar o mesmo
User-Agent não adianta. Por isso este módulo tenta a `requests` primeiro e cai
para o `curl` quando reconhece a parede. Se nenhum dos dois passar, ele diz que
foi bloqueado, em vez de relatar "0 imóveis".
"""

from __future__ import annotations

import csv
import io
import logging
import re
import shutil
import subprocess

from .. import http
from ..models import Listing

log = logging.getLogger("terreno.sources.caixa")

NAME = "caixa"
CSV_URL = "https://venda-imoveis.caixa.gov.br/listaweb/Lista_imoveis_{uf}.csv"

# Só estes tipos interessam. "Terreno" entra porque a Caixa classifica assim
# algumas glebas rurais, mas o filtro de área do pipeline descarta os lotes
# urbanos de 300 m² que dominam essa categoria.
TIPOS = re.compile(r"^\s*(gleba|chacara|chácara|sitio|sítio|fazenda|terreno|"
                   r"im[óo]vel rural|[áa]rea rural)", re.I)

COL = {  # índices das colunas do CSV
    "id": 0, "uf": 1, "cidade": 2, "bairro": 3, "endereco": 4,
    "preco": 5, "avaliacao": 6, "desconto": 7, "financiamento": 8,
    "descricao": 9, "modalidade": 10, "link": 11,
}


def fetch(criteria, store, budgets) -> list[Listing]:
    out: list[Listing] = []

    for uf in criteria.states:
        texto = _baixar(uf.upper())
        if texto is None:
            continue

        linhas = texto.splitlines()
        if len(linhas) < 3:
            log.warning("caixa: CSV de %s veio vazio", uf)
            continue

        leitor = csv.reader(io.StringIO("\n".join(linhas[2:])), delimiter=";")
        total = rurais = 0
        for linha in leitor:
            if len(linha) <= COL["link"]:
                continue
            total += 1
            listing = _to_listing(linha, uf)
            if listing:
                rurais += 1
                out.append(listing)
        log.info("caixa %s: %d imóveis, %d rurais", uf, total, rurais)

    log.info("caixa: %d lotes", len(out))
    return out


def _parece_csv(texto: str) -> bool:
    """A parede do Radware devolve HTML com status 200 — checar o corpo é o
    único jeito de distinguir bloqueio de resposta boa."""
    inicio = texto.lstrip()[:400].lower()
    return "<html" not in inicio and "<head" not in inicio and ";" in texto[:2000]


def _baixar(uf: str) -> str | None:
    """CSV do estado, em texto. Tenta requests, depois curl_cffi (impressão
    digital de navegador), depois o binário curl, desiste com aviso explícito.

    A parede do Radware devolve HTTP 200 com uma página de CAPTCHA, não um
    403 — por isso o fallback automático de curl_cffi em http.py (que só liga
    depois de um 403/429) nunca chega a ser acionado aqui. Este source
    precisa pedir o transporte alternativo diretamente.
    """
    url = CSV_URL.format(uf=uf)

    resp = http.get(url, timeout=60, retries=1)
    if resp is not None:
        texto = resp.content.decode("latin-1", errors="replace")
        if _parece_csv(texto):
            return texto
        log.info("caixa %s: parede de bot na requests, tentando curl_cffi", uf)

    if http._cffi is not None:
        alt = http._via_cffi(url, None, None, 60, False)
        if alt is not None:
            texto = alt.content.decode("latin-1", errors="replace")
            if _parece_csv(texto):
                log.info("caixa %s: liberado via curl_cffi", uf)
                return texto
        log.info("caixa %s: curl_cffi também bloqueado, tentando curl", uf)

    if not shutil.which("curl"):
        log.warning("caixa %s: bloqueado e curl indisponível", uf)
        return None

    try:
        saida = subprocess.run(
            ["curl", "-sSL", "--max-time", "90", "-H", f"User-Agent: {http.UA}", url],
            capture_output=True, timeout=120, check=False,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        log.warning("caixa %s: curl falhou (%s)", uf, exc)
        return None

    texto = saida.stdout.decode("latin-1", errors="replace")
    if _parece_csv(texto):
        return texto

    log.warning("caixa %s: bloqueado por CAPTCHA (Radware) em todos os transportes", uf)
    return None


def _num(valor: str) -> float | None:
    """"300.600,00" -> 300600.0"""
    valor = (valor or "").strip()
    if not valor:
        return None
    try:
        return float(valor.replace(".", "").replace(",", "."))
    except ValueError:
        return None


def _to_listing(linha: list[str], uf: str) -> Listing | None:
    descricao = linha[COL["descricao"]].strip()
    if not TIPOS.match(descricao):
        return None

    # "Terreno, 371.26 de área total, 0.00 de área privativa, 2022.00 de área
    # do terreno." — aqui os números usam ponto decimal, ao contrário do preço.
    area_ha = None
    m = re.search(r"([\d.]+)\s+de área do terreno", descricao)
    if not m:
        m = re.search(r"([\d.]+)\s+de área total", descricao)
    if m:
        try:
            area_ha = round(float(m.group(1)) / 10_000, 4) or None
        except ValueError:
            area_ha = None

    preco = _num(linha[COL["preco"]])
    avaliacao = _num(linha[COL["avaliacao"]])
    desconto = (linha[COL["desconto"]] or "").strip()

    cidade = linha[COL["cidade"]].strip().title()
    partes = [descricao]
    if avaliacao and preco and avaliacao > preco:
        partes.append(f"Avaliado em R$ {avaliacao:,.2f}".replace(",", "."))
    if desconto and desconto not in ("0.00", "0,00"):
        partes.append(f"Desconto de {desconto}%")
    partes.append(f"Modalidade: {linha[COL['modalidade']].strip()}")
    partes.append(linha[COL["endereco"]].strip())

    return Listing(
        source=NAME,
        source_id=linha[COL["id"]].strip(),
        url=linha[COL["link"]].strip(),
        title=f"{descricao.split(',')[0].strip()} em {cidade}/{uf} — Caixa",
        description=" · ".join(p for p in partes if p),
        price=preco,
        area_ha=area_ha,
        municipality=cidade,
        uf=uf.upper(),
    )
