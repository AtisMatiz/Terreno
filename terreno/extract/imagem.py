"""Leitura das fotos do anúncio por um modelo com visão.

Deliberadamente **não** roda em tudo. Palavra-chave é grátis e roda em todos os
resultados; imagem custa tokens por anúncio, e a maioria dos anúncios nunca vai
interessar. Então esta etapa só olha o que já passou por uma nota alta
(`nota_minima_imagem`, 70/100 por padrão): o gasto acompanha a lista curta, não
o tamanho da varredura. Num run que descobre centenas de candidatos e aprova
oito, isso é a diferença entre centenas de chamadas e oito.

Usa Haiku de propósito. A tarefa é "descreva o que aparece nesta foto de um
imóvel rural" -- reconhecimento concreto, não raciocínio -- e é exatamente onde
o modelo mais barato entrega o mesmo resultado que um caro.

Desligado a menos que `ENABLE_LLM=1` e `ANTHROPIC_API_KEY` estejam definidos, e
qualquer falha é silenciosa: a nota determinística já existe e continua valendo.
A imagem só acrescenta evidência, nunca substitui a nota.
"""

from __future__ import annotations

import logging
import os

from .. import http

log = logging.getLogger("terreno.extract.imagem")

MODEL = os.getenv("TERRENO_MODELO_IMAGEM", "claude-haiku-4-5-20251001")

# Tetos de segurança. A URL vem de página de terceiro, então o que volta não é
# confiável nem em tipo nem em tamanho.
MAX_BYTES = 5 * 1024 * 1024
TIPOS_OK = {"image/jpeg", "image/png", "image/gif", "image/webp"}

_PROMPT = (
    "Esta é a foto principal de um anúncio de imóvel rural no Brasil. "
    "Descreva apenas o que é visível na imagem, em português, de forma curta.\n\n"
    "Interessa especificamente: presença de água (nascente, riacho, rio, lago, "
    "represa, cachoeira), construções (casa, galpão, curral, cercas), "
    "topografia (plano, ondulado, íngreme), cobertura vegetal (mata nativa, "
    "pasto, monocultura, eucalipto) e estado de conservação.\n\n"
    "Regras: não invente nada que não esteja na imagem; não repita texto do "
    "anúncio; se a foto não mostrar o imóvel (mapa, logotipo, foto de "
    "corretor, imagem genérica), diga isso e nada mais."
)

_SCHEMA = {
    "name": "leitura_foto",
    "description": "O que a foto do anúncio mostra sobre o imóvel.",
    "input_schema": {
        "type": "object",
        "properties": {
            "mostra_o_imovel": {
                "type": "boolean",
                "description": "false para mapa, logotipo, foto de corretor ou imagem genérica",
            },
            "resumo": {"type": "string", "description": "Uma frase curta em português."},
            "agua_visivel": {"type": "boolean"},
            "construcoes_visiveis": {"type": "boolean"},
            "topografia": {
                "type": "string",
                "enum": ["plano", "ondulado", "ingreme", "indeterminado"],
            },
            "vegetacao": {"type": "string"},
        },
        "required": ["mostra_o_imovel", "resumo"],
    },
}


def _client():
    try:
        import anthropic
    except ImportError:
        log.warning("pacote anthropic não instalado — leitura de imagem indisponível")
        return None
    key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not key:
        return None
    return anthropic.Anthropic(api_key=key)


def _baixar(url: str) -> tuple[bytes, str] | None:
    """Bytes da imagem e o media type, ou None. Passa pelo http.py do projeto
    para herdar throttle, retry e o transporte de navegador."""
    resp = http.get(url, timeout=25, retries=1)
    if resp is None:
        return None
    tipo = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
    if tipo not in TIPOS_OK:
        log.debug("imagem: %s não é imagem suportada (%s)", url, tipo or "sem tipo")
        return None
    dados = resp.content
    if not dados or len(dados) > MAX_BYTES:
        log.debug("imagem: %s tem tamanho inviável (%d bytes)", url, len(dados or b""))
        return None
    return dados, tipo


def analisar(url: str) -> dict:
    """Lê a foto principal de um anúncio. Devolve {} em qualquer falha.

    Só é chamado para anúncios que já passaram do corte de nota -- o filtro
    mora em `pipeline.enriquecer_imagens`, não aqui, para que o custo fique
    visível no lugar onde a decisão de gastar é tomada.
    """
    if not url:
        return {}
    client = _client()
    if client is None:
        return {}

    baixado = _baixar(url)
    if baixado is None:
        return {}
    dados, tipo = baixado

    import base64
    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=400,
            tools=[_SCHEMA],
            tool_choice={"type": "tool", "name": "leitura_foto"},
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64", "media_type": tipo,
                        "data": base64.standard_b64encode(dados).decode("ascii"),
                    }},
                    {"type": "text", "text": _PROMPT},
                ],
            }],
        )
    except Exception as exc:  # noqa: BLE001 — caminho opcional, não derruba o run
        log.warning("imagem: falha ao analisar %s: %s", url, exc)
        return {}

    for bloco in resp.content:
        if getattr(bloco, "type", "") == "tool_use":
            return dict(bloco.input or {})
    return {}
