import os
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import Update, ReactionTypeEmoji
from telegram.ext import (
    Application,
    ContextTypes,
    MessageHandler,
    filters,
)

# =========================
# CONFIGURAÇÃO
# =========================

TOKEN = os.getenv("BOT_TOKEN")

if not TOKEN:
    raise RuntimeError(
        "BOT_TOKEN não encontrado. Adicione a variável BOT_TOKEN no Render."
    )

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger("MZoneTechBot")


# =========================
# SERVIDOR PARA O RENDER
# =========================

class HealthHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"MZone Tech Bot online!")

    def log_message(self, format, *args):
        return


def iniciar_servidor():
    port = int(os.environ.get("PORT", 10000))

    server = HTTPServer(("0.0.0.0", port), HealthHandler)

    logger.info("Servidor HTTP iniciado na porta %s", port)

    server.serve_forever()


# =========================
# REAÇÃO AUTOMÁTICA ❤️
# =========================

async def reagir_mensagem(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    mensagem = update.effective_message

    if not mensagem:
        return

    try:
        await context.bot.set_message_reaction(
            chat_id=mensagem.chat_id,
            message_id=mensagem.message_id,
            reaction=[
                ReactionTypeEmoji(
                    emoji="❤"
                )
            ],
            is_big=False,
        )

        logger.info(
            "❤️ Reação adicionada | Chat: %s | Mensagem: %s",
            mensagem.chat_id,
            mensagem.message_id,
        )

    except Exception as erro:
        logger.warning(
            "Não foi possível reagir à mensagem %s: %s",
            mensagem.message_id,
            erro,
        )


# =========================
# TRATAMENTO DE ERROS
# =========================

async def erro_bot(
    update: object,
    context: ContextTypes.DEFAULT_TYPE
):
    logger.error(
        "Erro durante atualização do bot:",
        exc_info=context.error,
    )


# =========================
# INICIALIZAÇÃO
# =========================

def main():

    # Mantém o serviço Web do Render ativo
    servidor_thread = threading.Thread(
        target=iniciar_servidor,
        daemon=True,
    )

    servidor_thread.start()

    # Cria o bot
    app = (
        Application.builder()
        .token(TOKEN)
        .build()
    )

    # Reage automaticamente às mensagens
    app.add_handler(
        MessageHandler(
            filters.ALL,
            reagir_mensagem,
        )
    )

    app.add_error_handler(erro_bot)

    logger.info("🤖 MZone Tech Bot iniciado!")

    # Inicia o bot por long polling
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
