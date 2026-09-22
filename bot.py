import asyncio
import html
import json
import logging
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from telegram import ReactionTypeEmoji, Update
from telegram.constants import ChatType, ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


# ============================================================
# CONFIGURAÇÃO
# ============================================================

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
GROUP_ID_RAW = os.getenv("TELEGRAM_GROUP_ID", "").strip()
ADMIN_ID_RAW = os.getenv("TELEGRAM_ADMIN_ID", "").strip()

if not BOT_TOKEN:
    raise RuntimeError(
        "TELEGRAM_BOT_TOKEN não encontrado no Render."
    )

if not GROUP_ID_RAW:
    raise RuntimeError(
        "TELEGRAM_GROUP_ID não encontrado no Render."
    )

if not ADMIN_ID_RAW:
    raise RuntimeError(
        "TELEGRAM_ADMIN_ID não encontrado no Render."
    )


try:
    ADMIN_ID = int(ADMIN_ID_RAW)
except ValueError as exc:
    raise RuntimeError(
        "TELEGRAM_ADMIN_ID precisa ser numérico."
    ) from exc


# Pode ser:
# -1001234567890
# ou:
# @nome_do_canal

GROUP_ID = (
    int(GROUP_ID_RAW)
    if re.fullmatch(r"-?\d+", GROUP_ID_RAW)
    else GROUP_ID_RAW
)


# ============================================================
# LOGS
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s | "
        "%(levelname)s | "
        "%(name)s | "
        "%(message)s"
    ),
)

logger = logging.getLogger("MZoneTechBot")

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)


# ============================================================
# CONFIGURAÇÃO DO MERCADO LIVRE
# ============================================================

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 "
        "(Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 "
        "(KHTML, like Gecko) "
        "Chrome/153.0.0.0 "
        "Safari/537.36"
    ),
    "Accept-Language": (
        "pt-BR,pt;q=0.9,en;q=0.8"
    ),
    "Accept": (
        "text/html,"
        "application/xhtml+xml,"
        "application/xml;q=0.9,"
        "*/*;q=0.8"
    ),
}


ML_DOMAINS = (
    "mercadolivre.com.br",
    "mercadolivre.com",
    "mercadolibre.com",
)


URL_RE = re.compile(
    r"https?://[^\s<>'\"]+",
    re.IGNORECASE,
)


ITEM_RE = re.compile(
    r"\bMLB[-_]?(\d{6,})\b",
    re.IGNORECASE,
)


# ============================================================
# HEALTH CHECK DO RENDER
# ============================================================

class HealthHandler(BaseHTTPRequestHandler):

    def do_GET(self):

        self.send_response(200)

        self.send_header(
            "Content-Type",
            "text/plain; charset=utf-8",
        )

        self.end_headers()

        self.wfile.write(
            b"MZone Tech Bot OK"
        )

    def log_message(
        self,
        format,
        *args,
    ):
        return


def start_health_server():

    try:

        port = int(
            os.getenv(
                "PORT",
                "10000",
            )
        )

        server = ThreadingHTTPServer(
            (
                "0.0.0.0",
                port,
            ),
            HealthHandler,
        )

        threading.Thread(
            target=server.serve_forever,
            daemon=True,
        ).start()

        logger.info(
            "Health check ativo na porta %s.",
            port,
        )

    except Exception:

        logger.exception(
            "Falha ao iniciar o health check."
        )


# ============================================================
# FUNÇÕES AUXILIARES
# ============================================================

def extract_url(text):

    match = URL_RE.search(
        text or ""
    )

    if not match:
        return None

    return match.group(0).rstrip(
        ".,;:!?)]}"
    )


def is_ml_url(url):

    try:

        host = (
            urlparse(url).hostname
            or ""
        ).lower()

    except ValueError:

        return False

    return any(
        host == domain
        or host.endswith(
            "." + domain
        )
        for domain in ML_DOMAINS
    )


def clean(value):

    if value is None:
        return None

    value = re.sub(
        r"\s+",
        " ",
        str(value),
    ).strip()

    return value or None


def number(value):

    if value is None:
        return None

    if isinstance(
        value,
        (int, float),
    ):
        return float(value)

    value = str(value)

    value = value.replace(
        "R$",
        "",
    )

    value = value.replace(
        "\xa0",
        "",
    )

    value = value.replace(
        " ",
        "",
    )

    value = re.sub(
        r"[^0-9,.\-]",
        "",
        value,
    )

    if not value:
        return None

    try:

        if (
            "," in value
            and "." in value
        ):

            value = (
                value
                .replace(".", "")
                .replace(",", ".")
            )

        elif "," in value:

            value = value.replace(
                ",",
                ".",
            )

        return float(value)

    except ValueError:

        return None


def brl(value):

    value = f"{value:,.2f}"

    return (
        "R$ "
        + value
        .replace(",", "X")
        .replace(".", ",")
        .replace("X", ".")
    )


def meta(
    soup,
    **attrs,
):

    tag = soup.find(
        "meta",
        attrs=attrs,
    )

    if not tag:
        return None

    return clean(
        tag.get("content")
    )


def walk_json(value):

    if isinstance(
        value,
        dict,
    ):

        yield value

        for child in value.values():

            yield from walk_json(
                child
            )

    elif isinstance(
        value,
        list,
    ):

        for child in value:

            yield from walk_json(
                child
            )


def find_item_id(
    *texts,
):

    for text in texts:

        if not text:
            continue

        match = ITEM_RE.search(
            text
        )

        if match:

            return (
                "MLB"
                + match.group(1)
            )

    return None


def first_regex_number(
    source,
    patterns,
):

    for pattern in patterns:

        match = re.search(
            pattern,
            source,
            re.IGNORECASE,
        )

        if match:

            parsed = number(
                match.group(1)
            )

            if parsed is not None:

                return parsed

    return None


# ============================================================
# MERCADO LIVRE
# ============================================================

def get_page(
    original_url,
):

    response = requests.get(
        original_url,
        headers=HEADERS,
        timeout=(
            10,
            20,
        ),
        allow_redirects=True,
    )

    logger.info(
        (
            "Mercado Livre respondeu "
            "status=%s host=%s"
        ),
        response.status_code,
        urlparse(
            response.url
        ).hostname,
    )

    response.raise_for_status()

    if not is_ml_url(
        response.url
    ):

        raise ValueError(
            (
                "O link redirecionou "
                "para fora do Mercado Livre."
            )
        )

    if len(
        response.text or ""
    ) < 500:

        raise ValueError(
            (
                "A página retornou "
                "conteúdo insuficiente."
            )
        )

    return (
        response.text,
        response.url,
    )


def get_public_item(
    item_id,
):
    """
    Tenta consultar /items sem chave.

    Se o Mercado Livre exigir autenticação,
    retorna None e o bot continua usando
    os dados da página pública.
    """

    try:

        response = requests.get(
            (
                "https://api.mercadolibre.com/"
                f"items/{item_id}"
            ),
            headers={
                "User-Agent": (
                    HEADERS[
                        "User-Agent"
                    ]
                ),
                "Accept": (
                    "application/json"
                ),
            },
            timeout=(
                8,
                12,
            ),
        )

        if response.status_code != 200:

            logger.info(
                (
                    "/items/%s "
                    "retornou %s."
                ),
                item_id,
                response.status_code,
            )

            return None

        data = response.json()

        if isinstance(
            data,
            dict,
        ):
            return data

        return None

    except Exception:

        logger.exception(
            (
                "Falha no fallback "
                "/items/%s."
            ),
            item_id,
        )

        return None


def scrape_product(
    original_url,
):

    source, final_url = get_page(
        original_url
    )

    soup = BeautifulSoup(
        source,
        "html.parser",
    )

    product = {
        "title": None,
        "price": None,
        "original_price": None,
        "discount": None,
        "free_shipping": None,
        "full": None,
        "image": None,
        "item_id": find_item_id(
            final_url,
            source,
        ),
    }


    # ========================================================
    # 1. JSON-LD
    # ========================================================

    scripts = soup.find_all(
        "script",
        attrs={
            "type": (
                "application/ld+json"
            )
        },
    )

    for script in scripts:

        raw = (
            script.string
            or script.get_text(
                strip=True
            )
        )

        if not raw:
            continue

        try:

            payload = json.loads(
                raw
            )

        except (
            json.JSONDecodeError,
            TypeError,
        ):

            continue


        for node in walk_json(
            payload
        ):

            node_type = node.get(
                "@type"
            )

            is_product = (
                node_type == "Product"
                or (
                    isinstance(
                        node_type,
                        list,
                    )
                    and "Product"
                    in node_type
                )
            )

            if not is_product:
                continue


            if not product["title"]:

                product["title"] = clean(
                    node.get("name")
                )


            if not product["image"]:

                image = node.get(
                    "image"
                )

                if isinstance(
                    image,
                    str,
                ):

                    product["image"] = (
                        image
                    )

                elif (
                    isinstance(
                        image,
                        list,
                    )
                    and image
                ):

                    first = image[0]

                    if isinstance(
                        first,
                        str,
                    ):

                        product["image"] = (
                            first
                        )

                    elif isinstance(
                        first,
                        dict,
                    ):

                        product["image"] = clean(
                            first.get("url")
                            or first.get(
                                "contentUrl"
                            )
                        )

                elif isinstance(
                    image,
                    dict,
                ):

                    product["image"] = clean(
                        image.get("url")
                        or image.get(
                            "contentUrl"
                        )
                    )


            offers = node.get(
                "offers"
            )

            if isinstance(
                offers,
                list,
            ):

                offers = (
                    offers[0]
                    if offers
                    else None
                )


            if isinstance(
                offers,
                dict,
            ):

                if (
                    product["price"]
                    is None
                ):

                    product["price"] = number(
                        offers.get("price")
                        or offers.get(
                            "lowPrice"
                        )
                    )


                shipping = offers.get(
                    "shippingDetails"
                )

                if isinstance(
                    shipping,
                    list,
                ):

                    shipping = (
                        shipping[0]
                        if shipping
                        else None
                    )


                if isinstance(
                    shipping,
                    dict,
                ):

                    rate = shipping.get(
                        "shippingRate"
                    )

                    if isinstance(
                        rate,
                        dict,
                    ):

                        shipping_price = number(
                            rate.get(
                                "value"
                            )
                            or rate.get(
                                "price"
                            )
                            or rate.get(
                                "amount"
                            )
                        )

                        if (
                            shipping_price
                            is not None
                        ):

                            product[
                                "free_shipping"
                            ] = (
                                shipping_price
                                == 0
                            )


    # ========================================================
    # 2. META TAGS
    # ========================================================

    if not product["title"]:

        product["title"] = (
            meta(
                soup,
                property="og:title",
            )
            or meta(
                soup,
                name="twitter:title",
            )
        )

        if product["title"]:

            product["title"] = re.sub(
                (
                    r"\s*[|–-]\s*"
                    r"Mercado\s+Livre.*$"
                ),
                "",
                product["title"],
                flags=re.IGNORECASE,
            ).strip()


    if not product["image"]:

        product["image"] = (
            meta(
                soup,
                property="og:image",
            )
            or meta(
                soup,
                name="twitter:image",
            )
        )


    if product["price"] is None:

        product["price"] = number(
            meta(
                soup,
                property=(
                    "product:price:amount"
                ),
            )
            or meta(
                soup,
                itemprop="price",
            )
        )


    # ========================================================
    # 3. DADOS SERIALIZADOS NO HTML
    # ========================================================

    normalized = html.unescape(
        source
    )


    product[
        "original_price"
    ] = first_regex_number(
        normalized,
        [
            (
                r'"original_price"'
                r'\s*:\s*"?'
                r'([0-9]+(?:[.,][0-9]+)?)'
                r'"?'
            ),
            (
                r'"originalPrice"'
                r'\s*:\s*"?'
                r'([0-9]+(?:[.,][0-9]+)?)'
                r'"?'
            ),
            (
                r'"previous_price"'
                r'\s*:\s*"?'
                r'([0-9]+(?:[.,][0-9]+)?)'
                r'"?'
            ),
        ],
    )


    raw_discount = first_regex_number(
        normalized,
        [
            (
                r'"discount_percentage"'
                r'\s*:\s*"?'
                r'([0-9]+(?:[.,][0-9]+)?)'
                r'"?'
            ),
            (
                r'"discountPercent"'
                r'\s*:\s*"?'
                r'([0-9]+(?:[.,][0-9]+)?)'
                r'"?'
            ),
            (
                r'"discount_rate"'
                r'\s*:\s*"?'
                r'([0-9]+(?:[.,][0-9]+)?)'
                r'"?'
            ),
        ],
    )


    if (
        raw_discount
        and 0 < raw_discount < 100
    ):

        product[
            "discount"
        ] = round(
            raw_discount
        )


    if (
        product["free_shipping"]
        is None
    ):

        if re.search(
            (
                r'"(?:free_shipping|'
                r'freeShipping)"'
                r'\s*:\s*true'
            ),
            normalized,
            re.IGNORECASE,
        ):

            product[
                "free_shipping"
            ] = True

        elif re.search(
            (
                r'"(?:free_shipping|'
                r'freeShipping)"'
                r'\s*:\s*false'
            ),
            normalized,
            re.IGNORECASE,
        ):

            product[
                "free_shipping"
            ] = False


    if (
        product["full"]
        is None
        and re.search(
            (
                r'"logistic_type"'
                r'\s*:\s*'
                r'"fulfillment"'
            ),
            normalized,
            re.IGNORECASE,
        )
    ):

        product["full"] = True


    # ========================================================
    # 4. FALLBACK /items
    # ========================================================

    if product["item_id"]:

        api = get_public_item(
            product["item_id"]
        )

        if api:

            if not product["title"]:

                product["title"] = clean(
                    api.get("title")
                )


            if product["price"] is None:

                product["price"] = number(
                    api.get("price")
                )


            if (
                product[
                    "original_price"
                ]
                is None
            ):

                product[
                    "original_price"
                ] = number(
                    api.get(
                        "original_price"
                    )
                )


            if not product["image"]:

                pictures = api.get(
                    "pictures"
                )

                if (
                    isinstance(
                        pictures,
                        list,
                    )
                    and pictures
                ):

                    picture = pictures[0]

                    if isinstance(
                        picture,
                        dict,
                    ):

                        product["image"] = (
                            picture.get(
                                "secure_url"
                            )
                            or picture.get(
                                "url"
                            )
                        )

                if not product["image"]:

                    product["image"] = (
                        api.get(
                            "thumbnail"
                        )
                    )


            shipping = api.get(
                "shipping"
            )

            if isinstance(
                shipping,
                dict,
            ):

                if (
                    product[
                        "free_shipping"
                    ]
                    is None
                    and isinstance(
                        shipping.get(
                            "free_shipping"
                        ),
                        bool,
                    )
                ):

                    product[
                        "free_shipping"
                    ] = shipping[
                        "free_shipping"
                    ]


                if (
                    product["full"]
                    is None
                    and shipping.get(
                        "logistic_type"
                    )
                ):

                    product["full"] = (
                        shipping[
                            "logistic_type"
                        ]
                        == "fulfillment"
                    )


    # ========================================================
    # 5. CALCULA O DESCONTO
    # ========================================================

    if (
        product["original_price"]
        is not None
        and product["price"]
        is not None
        and product["original_price"]
        > product["price"]
        > 0
    ):

        if (
            product["discount"]
            is None
        ):

            product[
                "discount"
            ] = round(
                (
                    (
                        product[
                            "original_price"
                        ]
                        - product[
                            "price"
                        ]
                    )
                    / product[
                        "original_price"
                    ]
                )
                * 100
            )

    else:

        product[
            "original_price"
        ] = None


    return product


# ============================================================
# MENSAGEM DA MZONE TECH
# ============================================================

def build_offer(
    product,
    original_url,
):

    title = (
        clean(
            product["title"]
        )
        or "Produto em oferta"
    )


    if len(title) > 180:

        title = (
            title[:177].rstrip()
            + "..."
        )


    lines = [
        "🔥 <b>OFERTA MZONE TECH</b>",
        "",
        (
            "🎯 <b>"
            + html.escape(title)
            + "</b>"
        ),
    ]


    if (
        product["original_price"]
        and product["price"]
    ):

        price_line = (
            "💰 De <s>"
            + brl(
                product[
                    "original_price"
                ]
            )
            + "</s> por <b>"
            + brl(
                product[
                    "price"
                ]
            )
            + "</b>"
        )

    else:

        price_line = (
            "💰 <b>"
            + brl(
                product[
                    "price"
                ]
            )
            + "</b>"
        )


    if product["discount"]:

        price_line += (
            " — <b>"
            + str(
                product[
                    "discount"
                ]
            )
            + "% OFF</b>"
        )


    lines.append(
        price_line
    )


    shipping = []


    if (
        product[
            "free_shipping"
        ]
        is True
    ):

        shipping.append(
            "🚚 <b>Frete grátis</b>"
        )


    if (
        product["full"]
        is True
    ):

        shipping.append(
            "⚡ <b>FULL</b>"
        )


    if shipping:

        lines.append(
            " • ".join(
                shipping
            )
        )


    # IMPORTANTE:
    # este é EXATAMENTE o link que
    # o administrador enviou ao bot.
    #
    # Não troca por link final,
    # não encurta e não gera outro.

    exact_link = html.escape(
        original_url,
        quote=False,
    )


    lines.extend(
        [
            "",
            (
                "⚡ Vale conferir enquanto "
                "esse valor estiver disponível."
            ),
            "",
            (
                "🛒 <b>Comprar no "
                "Mercado Livre:</b>"
            ),
            exact_link,
        ]
    )


    return "\n".join(
        lines
    )


# ============================================================
# TELEGRAM
# ============================================================

def is_target_chat(
    chat,
):

    if isinstance(
        GROUP_ID,
        int,
    ):

        return (
            chat.id
            == GROUP_ID
        )


    username = getattr(
        chat,
        "username",
        None,
    )


    return (
        bool(username)
        and (
            "@"
            + username
        ).lower()
        == str(
            GROUP_ID
        ).lower()
    )


async def try_heart_reaction(
    message,
):

    try:

        await message.set_reaction(
            ReactionTypeEmoji(
                "❤"
            )
        )

    except Exception:

        logger.warning(
            (
                "Não foi possível "
                "reagir à mensagem %s."
            ),
            getattr(
                message,
                "message_id",
                "?",
            ),
        )


async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.effective_user:
        return


    if (
        update.effective_user.id
        != ADMIN_ID
    ):
        return


    if not update.effective_chat:
        return


    if (
        update.effective_chat.type
        != ChatType.PRIVATE
    ):
        return


    await (
        update
        .effective_message
        .reply_text(
            (
                "MZone Tech Bot ativo ✅\n\n"
                "Me mande no privado "
                "um link de produto "
                "do Mercado Livre."
            )
        )
    )


async def send_offer(
    context,
    product,
    offer_text,
):

    sent = None


    # Primeiro tenta publicar
    # com a imagem do produto.

    if product["image"]:

        try:

            sent = (
                await context.bot.send_photo(
                    chat_id=GROUP_ID,
                    photo=(
                        product["image"]
                    ),
                    caption=offer_text,
                    parse_mode=(
                        ParseMode.HTML
                    ),
                )
            )

        except Exception:

            logger.exception(
                (
                    "Falha ao enviar "
                    "a imagem. "
                    "Publicando somente "
                    "o texto."
                )
            )


    # Se não houver imagem
    # ou se o Telegram não conseguir
    # baixar a imagem, publica texto.

    if sent is None:

        sent = (
            await context.bot.send_message(
                chat_id=GROUP_ID,
                text=offer_text,
                parse_mode=(
                    ParseMode.HTML
                ),
                disable_web_page_preview=False,
            )
        )


    # Mantém o coração automático
    # na oferta publicada.

    await try_heart_reaction(
        sent
    )


async def handle_private_link(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    message = (
        update.effective_message
    )

    user = (
        update.effective_user
    )

    chat = (
        update.effective_chat
    )


    if (
        not message
        or not user
        or not chat
    ):

        return


    # Só funciona no privado.

    if (
        chat.type
        != ChatType.PRIVATE
    ):

        return


    # Só o usuário configurado
    # como administrador pode usar.

    if user.id != ADMIN_ID:

        logger.warning(
            (
                "Usuário não autorizado "
                "tentou usar o bot: %s"
            ),
            user.id,
        )

        return


    # Pega o link original
    # exatamente como foi enviado.

    original_url = extract_url(
        message.text
        or ""
    )


    if not original_url:

        await message.reply_text(
            (
                "Me mande um link "
                "de produto do "
                "Mercado Livre."
            )
        )

        return


    if not is_ml_url(
        original_url
    ):

        await message.reply_text(
            (
                "Esse link não parece "
                "ser do Mercado Livre."
            )
        )

        return


    await message.reply_text(
        (
            "🔎 Link recebido. "
            "Vou identificar o produto "
            "e publicar."
        )
    )


    try:

        # A leitura da página usa requests,
        # então roda em outra thread para
        # não travar o Telegram.

        product = (
            await asyncio.to_thread(
                scrape_product,
                original_url,
            )
        )


        # Nome e preço são considerados
        # informações mínimas para evitar
        # publicar produto errado.

        missing_required = []


        if not product["title"]:

            missing_required.append(
                "nome do produto"
            )


        if (
            product["price"]
            is None
        ):

            missing_required.append(
                "preço atual"
            )


        if missing_required:

            logger.warning(
                (
                    "Dados essenciais "
                    "ausentes: %s"
                ),
                missing_required,
            )


            await message.reply_text(
                (
                    "⚠️ Não consegui confirmar "
                    + ", ".join(
                        missing_required
                    )
                    + ".\n\n"
                    "Essa oferta não foi "
                    "publicada, mas o bot "
                    "continua funcionando."
                )
            )

            return


        offer_text = build_offer(
            product,
            original_url,
        )


        await send_offer(
            context,
            product,
            offer_text,
        )


        # Verifica informações
        # opcionais que não foram
        # confirmadas.

        missing_optional = []


        if not product["image"]:

            missing_optional.append(
                "imagem"
            )


        if (
            product[
                "free_shipping"
            ]
            is None
        ):

            missing_optional.append(
                "frete grátis"
            )


        if (
            product["full"]
            is None
        ):

            missing_optional.append(
                "Full"
            )


        if missing_optional:

            await message.reply_text(
                (
                    "✅ Oferta publicada.\n\n"
                    "⚠️ Não consegui confirmar "
                    "automaticamente: "
                    + ", ".join(
                        missing_optional
                    )
                    + "."
                )
            )

        else:

            await message.reply_text(
                (
                    "✅ Oferta publicada "
                    "no grupo/canal."
                )
            )


        logger.info(
            (
                "Oferta publicada | "
                "item=%s | destino=%s"
            ),
            (
                product["item_id"]
                or "não identificado"
            ),
            GROUP_ID,
        )


    except requests.Timeout:

        logger.exception(
            (
                "Timeout consultando "
                "o Mercado Livre."
            )
        )


        await message.reply_text(
            (
                "⚠️ O Mercado Livre demorou "
                "demais para responder.\n\n"
                "A oferta não foi publicada, "
                "mas o bot continua "
                "funcionando."
            )
        )


    except requests.RequestException as exc:

        logger.exception(
            (
                "Erro HTTP consultando "
                "o Mercado Livre: %s"
            ),
            exc,
        )


        await message.reply_text(
            (
                "⚠️ Não consegui abrir "
                "esse link do Mercado Livre "
                "agora.\n\n"
                "A oferta não foi publicada, "
                "mas o bot continua "
                "funcionando."
            )
        )


    except Exception as exc:

        logger.exception(
            (
                "Erro inesperado ao "
                "processar oferta: %s"
            ),
            exc,
        )


        await message.reply_text(
            (
                "⚠️ Deu um erro ao processar "
                "esse produto.\n\n"
                "A oferta não foi publicada, "
                "mas o bot continua "
                "funcionando."
            )
        )


async def react_to_target_chat(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    message = (
        update.effective_message
    )

    chat = (
        update.effective_chat
    )

    user = (
        update.effective_user
    )


    if (
        not message
        or not chat
    ):

        return


    if not is_target_chat(
        chat
    ):

        return


    # Não tenta reagir a mensagem
    # de outro bot.

    if (
        user
        and user.is_bot
    ):

        return


    await try_heart_reaction(
        message
    )


async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):

    logger.error(
        "Erro não tratado.",
        exc_info=context.error,
    )


    try:

        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                "⚠️ O bot encontrou um "
                "erro inesperado, mas "
                "continua ativo.\n\n"
                "Confira os logs do Render."
            ),
        )

    except Exception:

        logger.exception(
            (
                "Falha ao avisar "
                "o administrador."
            )
        )


# ============================================================
# INICIALIZAÇÃO
# ============================================================

def main():

    start_health_server()


    app = (
        Application
        .builder()
        .token(
            BOT_TOKEN
        )
        .build()
    )


    # /start no privado

    app.add_handler(
        CommandHandler(
            "start",
            start_command,
        ),
        group=0,
    )


    # Recebe o link no privado.

    app.add_handler(
        MessageHandler(
            (
                filters.TEXT
                & ~filters.COMMAND
            ),
            handle_private_link,
        ),
        group=0,
    )


    # Mantém a função de reagir
    # automaticamente no grupo/canal.

    app.add_handler(
        MessageHandler(
            filters.ALL,
            react_to_target_chat,
        ),
        group=1,
    )


    app.add_error_handler(
        error_handler
    )


    logger.info(
        "MZone Tech Bot iniciado."
    )


    app.run_polling(
        allowed_updates=(
            Update.ALL_TYPES
        ),
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
