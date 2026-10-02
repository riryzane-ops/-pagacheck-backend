import os
import re
import tempfile
import asyncio
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, PlainTextResponse

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)
from telegram.error import NetworkError, TimedOut

import pytesseract
from PIL import Image
from pypdf import PdfReader


BOT_TOKEN = os.environ["BOT_TOKEN"]
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "pagacheck-webhook")
PORT = int(os.environ.get("PORT", "10000"))

app = FastAPI(title="PagaCheck Backend", version="1.3")

bot = Application.builder().token(BOT_TOKEN).build()

# Evita di elaborare due volte lo stesso aggiornamento Telegram
PROCESSED_UPDATES = set()

PATTERNS = [
    (
        "Retribuzione base",
        r"(?:retribuzione\s+base|paga\s+base|minimo)[^\d]{0,20}([\d.]+,\d{2})",
    ),
    (
        "EDR",
        r"\bEDR\b[^\d]{0,20}([\d.]+,\d{2})",
    ),
    (
        "Totale retribuzione",
        r"totale\s+retribuzione[^\d]{0,20}([\d.]+,\d{2})",
    ),
    (
        "Straordinario",
        r"straordinari[^\d]{0,30}([\d.]+,\d{2})",
    ),
    (
        "Contributi",
        r"(?:contributi|inps)[^\d]{0,30}([\d.]+,\d{2})",
    ),
    (
        "IRPEF",
        r"irpef[^\d]{0,30}([\d.]+,\d{2})",
    ),
    (
        "Netto",
        r"netto(?:\s+a)?\s+pagare[^\d]{0,30}([\d.]+,\d{2})",
    ),
]


def extract(text):
    out = []

    for label, pattern in PATTERNS:
        match = re.search(pattern, text, re.I)

        if match:
            out.append((label, match.group(1)))

    return out


def analyze(path):
    if path.suffix.lower() == ".pdf":
        reader = PdfReader(str(path))

        text = "\n".join(
            (page.extract_text() or "")
            for page in reader.pages
        )

        if not text.strip():
            return [], "PDF scansione"

        return extract(text), "testo PDF"

    with Image.open(path) as image:
        text = pytesseract.image_to_string(
            image.convert("RGB"),
            lang="ita+eng",
        )

    return extract(text), "OCR foto"


async def download_with_retry(file_id, destination, attempts=3):
    last_error = None

    for attempt in range(1, attempts + 1):

        try:
            telegram_file = await bot.bot.get_file(
                file_id,
                read_timeout=60,
                connect_timeout=30,
                pool_timeout=30,
            )

            if destination.exists():
                destination.unlink()

            await telegram_file.download_to_drive(
                str(destination),
                read_timeout=120,
                write_timeout=120,
                connect_timeout=30,
                pool_timeout=30,
            )

            if destination.exists() and destination.stat().st_size > 0:
                return

        except (NetworkError, TimedOut) as error:
            last_error = error

            if attempt < attempts:
                await asyncio.sleep(attempt * 2)

        except Exception as error:
            last_error = error

            if attempt < attempts:
                await asyncio.sleep(attempt * 2)

    raise last_error


async def start(update, context):
    await update.message.reply_text(
        "👋 Benvenuto in PagaCheck V1.3.\n\n"
        "📎 Inviami una foto o un PDF della busta paga.\n\n"
        "⚠️ Controllo preliminare: non sostituisce una verifica professionale.",
        reply_markup=InlineKeyboardMarkup(
            [[
                InlineKeyboardButton(
                    "📄 Invia busta paga",
                    callback_data="upload",
                )
            ]]
        ),
    )


async def help_cmd(update, context):
    await update.message.reply_text(
        "Invia una foto o un PDF della busta paga."
    )


async def callback(update, context):
    await update.callback_query.answer()

    await update.callback_query.message.reply_text(
        "Perfetto. 📎 Inviami qui la foto o il PDF della busta paga."
    )


async def process(update, path):
    try:
        rows, method = analyze(path)

        if not rows:
            await update.message.reply_text(
                "🔎 Documento ricevuto, ma non ho riconosciuto "
                "abbastanza voci.\n\n"
                "Prova con un'immagine più nitida oppure "
                "con il PDF originale."
            )
            return

        message = [
            "✅ PagaCheck — prima lettura completata",
            f"Metodo: {method}",
            "",
            "Voci riconosciute:",
        ]

        message += [
            f"• {label}: € {value}"
            for label, value in rows
        ]

        message += [
            "",
            "📌 Il controllo CCNL e la verifica completa "
            "delle anomalie sono il livello successivo.",
            "",
            "⚠️ I dati letti non certificano da soli "
            "un errore nella busta paga.",
        ]

        await update.message.reply_text(
            "\n".join(message)
        )

    except Exception:
        await update.message.reply_text(
            "⚠️ Ho ricevuto il documento, ma si è verificato "
            "un problema durante l'analisi.\n\n"
            "Prova nuovamente tra qualche secondo."
        )


async def doc(update, context):
    document = update.message.document

    extension = Path(
        document.file_name or "cedolino.pdf"
    ).suffix.lower()

    allowed = {
        ".pdf",
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
    }

    if extension not in allowed:
        await update.message.reply_text(
            "Invia un PDF oppure una foto JPG, PNG o WEBP."
        )
        return

    if document.file_size and document.file_size > 20 * 1024 * 1024:
        await update.message.reply_text(
            "⚠️ Il file supera il limite di 20 MB."
        )
        return

    await update.message.reply_text(
        "📥 Ricevuto. Analizzo…"
    )

    temp_dir = Path(tempfile.mkdtemp())
    path = temp_dir / f"cedolino{extension}"

    try:
        await download_with_retry(
            document.file_id,
            path,
            attempts=3,
        )

        await process(update, path)

    except (NetworkError, TimedOut):
        await update.message.reply_text(
            "⚠️ La connessione con Telegram si è interrotta "
            "durante il download.\n\n"
            "Ho già effettuato più tentativi automatici. "
            "Riprova inviando nuovamente il documento."
        )

    except Exception:
        await update.message.reply_text(
            "⚠️ Non riesco a leggere il documento.\n\n"
            "Prova a inviarlo nuovamente."
        )

    finally:
        try:
            if path.exists():
                path.unlink()

            temp_dir.rmdir()

        except Exception:
            pass


async def photo(update, context):
    await update.message.reply_text(
        "📥 Foto ricevuta. Avvio OCR…"
    )

    temp_dir = Path(tempfile.mkdtemp())
    path = temp_dir / "cedolino.jpg"

    try:
        photo_file = update.message.photo[-1]

        await download_with_retry(
            photo_file.file_id,
            path,
            attempts=3,
        )

        await process(update, path)

    except (NetworkError, TimedOut):
        await update.message.reply_text(
            "⚠️ La connessione con Telegram si è interrotta "
            "durante il download.\n\n"
            "Riprova a inviare la foto."
        )

    except Exception:
        await update.message.reply_text(
            "⚠️ Non riesco a leggere la foto.\n\n"
            "Prova con una foto più nitida e ben illuminata."
        )

    finally:
        try:
            if path.exists():
                path.unlink()

            temp_dir.rmdir()

        except Exception:
            pass


async def text(update, context):
    await update.message.reply_text(
        "Sono pronto. 📎 Mandami una foto o un PDF "
        "della busta paga."
    )


bot.add_handler(
    CommandHandler("start", start)
)

bot.add_handler(
    CommandHandler("help", help_cmd)
)

bot.add_handler(
    MessageHandler(filters.Document.ALL, doc)
)

bot.add_handler(
    MessageHandler(filters.PHOTO, photo)
)

bot.add_handler(
    MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        text,
    )
)

bot.add_handler(
    CallbackQueryHandler(callback)
)


@app.get(
    "/health",
    response_class=PlainTextResponse,
)
async def health():
    return "PagaCheck OK"


@app.get("/")
async def home():
    return FileResponse("PagaCheck_V1_2.html")


@app.post(f"/telegram/{WEBHOOK_SECRET}")
async def webhook(request: Request):

    data = await request.json()

    update = Update.de_json(
        data,
        bot.bot,
    )

    update_id = update.update_id

    # Se Telegram reinvia lo stesso aggiornamento,
    # non lo elaboriamo una seconda volta.
    if update_id in PROCESSED_UPDATES:
        return {"ok": True}

    PROCESSED_UPDATES.add(update_id)

    # Manteniamo la memoria sotto controllo.
    if len(PROCESSED_UPDATES) > 5000:
        PROCESSED_UPDATES.clear()

    bot.create_task(
        bot.process_update(update)
    )

    return {"ok": True}


@app.on_event("startup")
async def startup():

    await bot.initialize()
    await bot.start()

    if PUBLIC_URL:
        await bot.bot.set_webhook(
            f"{PUBLIC_URL}/telegram/{WEBHOOK_SECRET}",
            drop_pending_updates=True,
        )

    else:
        await bot.updater.start_polling(
            drop_pending_updates=True
        )


@app.on_event("shutdown")
async def shutdown():

    if bot.updater and bot.updater.running:
        await bot.updater.stop()

    await bot.stop()
    await bot.shutdown()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=PORT,
    )
