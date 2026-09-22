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
     "me.li",
     "meli.la",
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


def money_from_andes_amount(tag):
    """
    Lê os componentes visuais de preço usados pelo Mercado Livre:
    .andes-money-amount__fraction e .andes-money-amount__cents
    """
    if not tag:
        return None

    fraction_tag = tag.select_one(".andes-money-amount__fraction")
    cents_tag = tag.select_one(".andes-money-amount__cents")

    if not fraction_tag:
        return None

    fraction_digits = re.sub(
        r"\D",
        "",
        fraction_tag.get_text(" ", strip=True),
    )

    if not fraction_digits:
        return None

    value = float(fraction_digits)

    if cents_tag:
        cents_digits = re.sub(
            r"\D",
            "",
            cents_tag.get_text(" ", strip=True),
        )

        if cents_digits:
            cents_digits = cents_digits[:2].ljust(2, "0")
            value += int(cents_digits) / 100

    return value


def tag_is_old_or_installment_price(tag):
    """
    Evita confundir preço antigo ou valor de parcela com o preço atual.
    """
    current = tag

    for _ in range(5):
        if current is None:
            break

        classes = " ".join(
            current.get("class", [])
            if hasattr(current, "get")
            else []
        ).lower()

        if any(
            marker in classes
            for marker in (
                "original",
                "previous",
                "installment",
                "installments",
                "discount",
            )
        ):
            return True

        if getattr(current, "name", None) in ("s", "del"):
            return True

        current = getattr(current, "parent", None)

    return False


def extract_current_price_from_dom(soup):
    """
    Procura primeiro os seletores específicos do preço principal do
    Mercado Livre e só depois usa um fallback mais amplo.
    """
    selectors = (
        ".ui-pdp-price__second-line .andes-money-amount",
        ".ui-pdp-price__main-container .andes-money-amount",
        ".ui-pdp-price__main-container [itemprop='price']",
        "[data-testid='price-part'] .andes-money-amount",
        ".ui-pdp-price__second-line [itemprop='price']",
    )

    for selector in selectors:
        for tag in soup.select(selector):
            content = (
                tag.get("content")
                or tag.get("value")
            )

            parsed = number(content)

            if parsed is None:
                parsed = money_from_andes_amount(tag)

            if parsed is not None and parsed > 0:
                return parsed

    # Fallback: procura um andes-money-amount que não seja
    # preço anterior nem parcela.
    for tag in soup.select(".andes-money-amount"):
        if tag_is_old_or_installment_price(tag):
            continue

        parsed = money_from_andes_amount(tag)

        if parsed is not None and parsed > 0:
            return parsed

    return None


def extract_original_price_from_dom(soup):
    selectors = (
        ".ui-pdp-price__original-value .andes-money-amount",
        ".ui-pdp-price__original-value",
        ".andes-money-amount--previous",
        "s.andes-money-amount",
        "del.andes-money-amount",
    )

    for selector in selectors:
        for tag in soup.select(selector):
            parsed = money_from_andes_amount(tag)

            if parsed is None:
                parsed = number(
                    tag.get("content")
                    or tag.get_text(" ", strip=True)
                )

            if parsed is not None and parsed > 0:
                return parsed

    return None


def extract_serialized_price(source):
    """
    Fallback para preços presentes em JSON/estado serializado da página.
    Os padrões mais específicos vêm primeiro para reduzir falso positivo.
    """
    patterns = (
        r'"current_price"\s*:\s*"?([0-9]+(?:[.,][0-9]+)?)"?',
        r'"currentPrice"\s*:\s*\{[^{}]{0,250}?"(?:value|amount)"\s*:\s*"?([0-9]+(?:[.,][0-9]+)?)"?',
        r'"price"\s*:\s*\{[^{}]{0,250}?"(?:value|amount)"\s*:\s*"?([0-9]+(?:[.,][0-9]+)?)"?',
        r'"price"\s*:\s*"?([0-9]+(?:[.,][0-9]+)?)"?',
        r'"priceValue"\s*:\s*"?([0-9]+(?:[.,][0-9]+)?)"?',
    )

    return first_regex_number(
        html.unescape(source),
        patterns,
    )


def compact_count(value):
    if value is None:
        return None

    try:
        value = int(value)
    except (TypeError, ValueError):
        return None

    if value >= 1_000_000:
        amount = value / 1_000_000
        rendered = f"{amount:.1f}".rstrip("0").rstrip(".")
        return rendered.replace(".", ",") + " MI"

    if value >= 1_000:
        amount = value / 1_000
        rendered = f"{amount:.1f}".rstrip("0").rstrip(".")
        return rendered.replace(".", ",") + " MIL"

    return str(value)


def extract_installments(soup, source):
    page_text = clean(
        soup.get_text(" ", strip=True)
    ) or ""

    patterns = (
        r'(\d{1,2})x\s+de\s+(R\$\s*[\d.]+,\d{2})\s+sem\s+juros',
        r'(\d{1,2})\s*x\s*(R\$\s*[\d.]+,\d{2})\s+sem\s+juros',
        r'em\s+(\d{1,2})x\s+de\s+(R\$\s*[\d.]+,\d{2})\s+sem\s+juros',
    )

    for pattern in patterns:
        match = re.search(
            pattern,
            page_text,
            re.IGNORECASE,
        )

        if match:
            return {
                "count": int(match.group(1)),
                "amount_text": clean(match.group(2)),
                "no_interest": True,
            }

    normalized = html.unescape(source)

    count_match = re.search(
        r'"installments"\s*:\s*(\d{1,2})',
        normalized,
        re.IGNORECASE,
    )

    amount_match = re.search(
        r'"installment_amount"\s*:\s*"?([0-9]+(?:[.,][0-9]+)?)"?',
        normalized,
        re.IGNORECASE,
    )

    if count_match and amount_match:
        amount = number(
            amount_match.group(1)
        )

        if amount is not None:
            return {
                "count": int(count_match.group(1)),
                "amount_text": brl(amount),
                "no_interest": bool(
                    re.search(
                        r'"interest_rate"\s*:\s*0(?:\.0+)?',
                        normalized,
                        re.IGNORECASE,
                    )
                ),
            }

    return None


def extract_rating_and_reviews(soup):
    page_text = clean(
        soup.get_text(" ", strip=True)
    ) or ""

    rating = None
    reviews = None

    rating_patterns = (
        r'([0-5](?:[.,]\d)?)\s*(?:de\s*5|/5)',
        r'Avalia[cç][aã]o\s*([0-5](?:[.,]\d)?)',
    )

    for pattern in rating_patterns:
        match = re.search(
            pattern,
            page_text,
            re.IGNORECASE,
        )

        if match:
            candidate = number(
                match.group(1)
            )

            if (
                candidate is not None
                and 0 <= candidate <= 5
            ):
                rating = candidate
                break

    review_patterns = (
        r'([\d.]+)\s+opini[oõ]es',
        r'([\d.]+)\s+avalia[cç][oõ]es',
    )

    for pattern in review_patterns:
        match = re.search(
            pattern,
            page_text,
            re.IGNORECASE,
        )

        if match:
            try:
                reviews = int(
                    re.sub(
                        r"\D",
                        "",
                        match.group(1),
                    )
                )
            except ValueError:
                pass
            break

    return rating, reviews


def extract_ranking(soup):
    page_text = clean(
        soup.get_text(" ", strip=True)
    ) or ""

    found = []

    if re.search(
        r'\bmais\s+vendido\b',
        page_text,
        re.IGNORECASE,
    ):
        found.append("🏆 <b>MAIS VENDIDO</b>")

    ranking_match = re.search(
        r'\b([1-9]\d*)[º°]\s+em\s+([A-Za-zÀ-ÿ0-9][A-Za-zÀ-ÿ0-9 &/\\-]{2,50})',
        page_text,
        re.IGNORECASE,
    )

    if ranking_match:
        position = ranking_match.group(1)
        category = clean(
            ranking_match.group(2)
        )

        if category:
            category = re.split(
                r'\s{2,}|Comprar|Frete|Avalia|R\$',
                category,
                maxsplit=1,
                flags=re.IGNORECASE,
            )[0].strip(" -|")

            if category:
                medal = (
                    "🥇" if position == "1"
                    else "🥈" if position == "2"
                    else "🥉" if position == "3"
                    else "🏅"
                )

                found.append(
                    f"{medal} <b>{html.escape(position + 'º em ' + category)}</b>"
                )

    return found[:2]


def extract_features(soup, title):
    candidates = []

    selectors = (
        ".ui-pdp-features__item",
        ".ui-pdp-highlights__content li",
        ".ui-pdp-specs__table tr",
        ".ui-pdp-specs__body tr",
        ".andes-list__item",
    )

    for selector in selectors:
        for tag in soup.select(selector):
            value = clean(
                tag.get_text(
                    " ",
                    strip=True,
                )
            )

            if not value:
                continue

            if len(value) < 4 or len(value) > 120:
                continue

            lower = value.lower()

            blocked = (
                "mercado pago",
                "devolução",
                "devolucao",
                "estoque",
                "vendido por",
                "formas de pagamento",
                "meios de pagamento",
                "perguntas e respostas",
                "mais produtos",
            )

            if any(
                word in lower
                for word in blocked
            ):
                continue

            if value not in candidates:
                candidates.append(value)

            if len(candidates) >= 7:
                return candidates

    return candidates[:7]


def feature_emoji(feature):
    lower = feature.lower()

    rules = (
        (("litro", "capacidade"), "🍽️"),
        (("w", "potência", "potencia"), "⚡"),
        (("display", "tela"), "🖥️"),
        (("espelh", "acabamento"), "✨"),
        (("segurança", "seguranca", "bloqueio"), "🔒"),
        (("cm", "mm", "dimens"), "📏"),
        (("127v", "220v", "voltagem", "voltage"), "🔌"),
        (("bluetooth", "wireless", "sem fio"), "📶"),
        (("bateria", "mah", "autonomia"), "🔋"),
        (("gb", "tb", "armazen"), "💾"),
        (("hz", "fps"), "🎮"),
        (("câmera", "camera", "mp"), "📸"),
    )

    for keys, emoji in rules:
        if any(
            key in lower
            for key in keys
        ):
            return emoji

    return "✅"


def product_emoji(title):
    lower = (title or "").lower()

    rules = (
        (("micro-ondas", "microondas", "air fryer", "panela", "cozinha"), "🍽️"),
        (("teclado", "mouse", "headset", "controle", "gamer"), "🎮"),
        (("celular", "smartphone", "iphone", "galaxy", "redmi", "motorola"), "📱"),
        (("tv", "televisor", "monitor"), "📺"),
        (("fone", "earbuds"), "🎧"),
        (("moto", "capacete", "baú", "bau"), "🏍️"),
        (("carro", "automotivo"), "🚗"),
        (("tênis", "tenis", "camiseta", "roupa", "calça", "vestido"), "👕"),
    )

    for keys, emoji in rules:
        if any(
            key in lower
            for key in keys
        ):
            return emoji

    return "🔥"


def sales_pitch(product):
    title = (
        product.get("title")
        or "esse produto"
    )

    if (
        product.get("discount")
        and product.get("price") is not None
    ):
        return (
            f"👀 <b>Com {product['discount']}% de desconto, "
            "vale conferir se você já estava de olho nesse tipo de produto.</b>"
        )

    return (
        "👀 <b>Uma opção pra conferir se você estava procurando "
        "esse tipo de produto e o valor fizer sentido pra você.</b>"
    )


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
        "installments": None,
        "rating": None,
        "reviews": None,
        "ranking": [],
        "features": [],
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


            aggregate_rating = node.get(
                "aggregateRating"
            )

            if isinstance(
                aggregate_rating,
                dict,
            ):

                if product["rating"] is None:

                    product["rating"] = number(
                        aggregate_rating.get(
                            "ratingValue"
                        )
                    )

                if product["reviews"] is None:

                    reviews_value = (
                        aggregate_rating.get(
                            "reviewCount"
                        )
                        or aggregate_rating.get(
                            "ratingCount"
                        )
                    )

                    if reviews_value is not None:

                        try:
                            product["reviews"] = int(
                                float(
                                    str(
                                        reviews_value
                                    ).replace(
                                        ".",
                                        ""
                                    ).replace(
                                        ",",
                                        "."
                                    )
                                )
                            )
                        except (
                            TypeError,
                            ValueError,
                        ):
                            pass


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
                property="product:price:amount",
            )
            or meta(
                soup,
                property="og:price:amount",
            )
            or meta(
                soup,
                itemprop="price",
            )
            or meta(
                soup,
                name="twitter:data1",
            )
        )


    # ========================================================
    # 3. PREÇO VISUAL DA PÁGINA
    # ========================================================

    if product["price"] is None:

        product["price"] = extract_current_price_from_dom(
            soup
        )


    if product["original_price"] is None:

        product["original_price"] = extract_original_price_from_dom(
            soup
        )


    # ========================================================
    # 4. DADOS SERIALIZADOS NO HTML
    # ========================================================

    normalized = html.unescape(
        source
    )


    if product["installments"] is None:
        product["installments"] = extract_installments(
            soup,
            source,
        )


    if (
        product["rating"] is None
        or product["reviews"] is None
    ):
        rating, reviews = extract_rating_and_reviews(
            soup
        )

        if product["rating"] is None:
            product["rating"] = rating

        if product["reviews"] is None:
            product["reviews"] = reviews


    if not product["ranking"]:
        product["ranking"] = extract_ranking(
            soup
        )


    if not product["features"]:
        product["features"] = extract_features(
            soup,
            product["title"],
        )


    if product["price"] is None:

        product["price"] = extract_serialized_price(
            source
        )


    if product["original_price"] is None:

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
    # 5. FALLBACK /items
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
    # 6. CALCULA O DESCONTO
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


    logger.info(
        (
            "Produto extraído | item=%s | "
            "titulo=%r | preco=%s | anterior=%s | "
            "desconto=%s | frete_gratis=%s | full=%s | imagem=%s | "
            "parcelas=%s | nota=%s | avaliacoes=%s | ranking=%s | recursos=%s"
        ),
        product["item_id"],
        product["title"],
        product["price"],
        product["original_price"],
        product["discount"],
        product["free_shipping"],
        product["full"],
        bool(product["image"]),
        product.get("installments"),
        product.get("rating"),
        product.get("reviews"),
        product.get("ranking"),
        len(product.get("features", [])),
    )

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

    discount = product.get(
        "discount"
    )

    emoji = product_emoji(
        title
    )

    headline = title.upper()

    if (
        discount
        and discount > 0
    ):
        headline += (
            f" COM {discount}% OFF!"
        )
    else:
        headline += " EM OFERTA!"

    lines = [
        (
            f"{emoji} <b>"
            + html.escape(headline)
            + "</b> 🔥"
        ),
        "",
        (
            "✨ <b>"
            + html.escape(title)
            + "</b>"
        ),
        "",
    ]


    if (
        product.get(
            "original_price"
        )
        and product.get(
            "price"
        )
    ):
        lines.append(
            "💰 De <b>"
            + brl(
                product[
                    "original_price"
                ]
            )
            + "</b>"
        )

        current_line = (
            "🔥 <b>POR "
            + brl(
                product[
                    "price"
                ]
            )
        )

        if discount:
            current_line += (
                f" — {discount}% OFF"
            )

        current_line += "</b>"

        lines.append(
            current_line
        )

    else:
        lines.append(
            "🔥 <b>POR "
            + brl(
                product["price"]
            )
            + "</b>"
        )


    installments = product.get(
        "installments"
    )

    if installments:
        installment_line = (
            f"💳 <b>{installments['count']}x de "
            f"{html.escape(installments['amount_text'])}"
        )

        if installments.get(
            "no_interest"
        ):
            installment_line += (
                " sem juros"
            )

        installment_line += "</b>"

        lines.append(
            installment_line
        )


    social_proof = []

    for item in product.get(
        "ranking",
        []
    ):
        social_proof.append(
            item
        )


    rating = product.get(
        "rating"
    )

    reviews = product.get(
        "reviews"
    )

    if rating is not None:
        rating_text = (
            str(
                round(
                    rating,
                    1,
                )
            )
            .replace(
                ".",
                ",",
            )
        )

        rating_line = (
            "⭐ <b>"
            + rating_text
            + "/5"
        )

        compact_reviews = compact_count(
            reviews
        )

        if compact_reviews:
            rating_line += (
                " com +"
                + compact_reviews
                + " avaliações"
            )

        rating_line += "</b> 😱"

        social_proof.append(
            rating_line
        )


    if social_proof:
        lines.append("")
        lines.extend(
            social_proof[:3]
        )


    features = product.get(
        "features",
        []
    )

    if features:
        lines.append("")

        for feature in features[:7]:
            lines.append(
                feature_emoji(
                    feature
                )
                + " <b>"
                + html.escape(
                    feature
                )
                + "</b>"
            )


    shipping_lines = []

    if (
        product.get(
            "free_shipping"
        )
        is True
    ):
        shipping_lines.append(
            "🚚 <b>Frete grátis</b>"
        )

    if (
        product.get(
            "full"
        )
        is True
    ):
        shipping_lines.append(
            "⚡ <b>FULL</b>"
        )

    if shipping_lines:
        lines.append("")
        lines.extend(
            shipping_lines
        )


    lines.extend(
        [
            "",
            sales_pitch(product),
            "",
        ]
    )


    voltage_found = any(
        re.search(
            r'\b(?:127|220)\s*v\b|voltagem',
            feature,
            re.IGNORECASE,
        )
        for feature in features
    )

    if voltage_found:
        lines.append(
            "⚠️ <b>Confira a voltagem antes da compra. "
            "Preço e disponibilidade podem mudar.</b>"
        )
    else:
        lines.append(
            "⚠️ <b>Preço e disponibilidade podem mudar.</b>"
        )


    exact_link = html.escape(
        original_url,
        quote=False,
    )

    lines.extend(
        [
            "",
            "👉 <b>VER OFERTA:</b>",
            "🔗 <b>"
            + exact_link
            + "</b>",
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
