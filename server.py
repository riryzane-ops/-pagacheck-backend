import os
import re
import tempfile
import asyncio
from contextlib import asynccontextmanager
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
from PIL import Image, ImageEnhance, ImageFilter
from pypdf import PdfReader


BOT_TOKEN = os.environ["BOT_TOKEN"]
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "pagacheck-webhook")
PORT = int(os.environ.get("PORT", "10000"))

PROCESSED_UPDATES = set()
MAX_FILE_SIZE = 20 * 1024 * 1024

LABELS = [
    (
        "Retribuzione base",
        [
            r"retribuzione\s+base",
            r"paga\s+base",
            r"minimo\s+tabellare",
            r"minimo",
        ],
    ),
    ("EDR", [r"\bedr\b"]),
    (
        "Totale retribuzione",
        [
            r"totale\s+retribuzione",
            r"totale\s+competenze",
            r"totale\s+lordo",
        ],
    ),
    (
        "Straordinario",
        [r"straordinari", r"straordinario"],
    ),
    (
        "Contributi",
        [
            r"contributi",
            r"inps",
            r"contributi\s+previdenziali",
        ],
    ),
    (
        "IRPEF",
        [r"irpef", r"ritenuta\s+irpef"],
    ),
    (
        "Netto",
        [
            r"netto\s+a\s+pagare",
            r"netto\s+pagare",
            r"netto",
        ],
    ),
]

MONEY_PATTERN = re.compile(
    r"(?<!\d)"
    r"(?P<amount>"
    r"(?:\d{1,3}(?:[.\s]\d{3})+|\d+)"
    r"(?:[,.]\d{2})"
    r")"
    r"(?!\d)"
)


def normalize_amount(value):
    value = value.strip().replace(" ", "")

    if "," in value and "." in value:
        if value.rfind(",") > value.rfind("."):
            integer, decimal = value.rsplit(",", 1)
            integer = integer.replace(".", "")
            return f"{integer},{decimal}"

        integer, decimal = value.rsplit(".", 1)
        integer = integer.replace(",", "")
        return f"{integer},{decimal}"

    if "," in value:
        integer, decimal = value.rsplit(",", 1)
        return f"{integer.replace('.', '')},{decimal}"

    if "." in value:
        parts = value.split(".")
        if len(parts) == 2 and len(parts[1]) == 2:
            return f"{parts[0]},{parts[1]}"

    return value


def amount_to_float(value):
    try:
        return float(value.replace(".", "").replace(",", "."))
    except (ValueError, AttributeError):
        return None


def clean_ocr_text(text):
    text = text.replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def find_amount_near_label(text, label_patterns):
    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip()
    ]

    for index, line in enumerate(lines):
        if not any(
            re.search(pattern, line, re.I)
            for pattern in label_patterns
        ):
            continue

        candidates = MONEY_PATTERN.findall(line)

        if candidates:
            return normalize_amount(candidates[-1])

        for next_line in lines[index + 1:index + 3]:
            candidates = MONEY_PATTERN.findall(next_line)
            if candidates:
                return normalize_amount(candidates[-1])

    return None


def extract(text):
    text = clean_ocr_text(text)
    out = []

    for label, patterns in LABELS:
        value = find_amount_near_label(text, patterns)
        if value is not None:
            out.append((label, value))

    return out


def consistency_checks(rows):
    values = {
        label: amount_to_float(value)
        for label, value in rows
    }

    notes = []

    netto = values.get("Netto")
    contributi = values.get("Contributi")
    irpef = values.get("IRPEF")
    totale = values.get("Totale retribuzione")

    if netto is not None and totale is not None and netto > totale:
        notes.append(
            "Il netto risulta superiore al totale retribuzione letto: "
            "dato da verificare."
        )

    if (
        contributi is not None
        and totale is not None
        and contributi > totale
    ):
        notes.append(
            "I contributi letti risultano superiori al totale retribuzione: "
            "possibile lettura OCR errata o dato da verificare."
        )

    if irpef is not None and totale is not None and irpef > totale:
        notes.append(
            "L'IRPEF letta risulta superiore al totale retribuzione: "
            "dato da verificare."
        )

    return notes


def analyze(path):
    if path.suffix.lower() == ".pdf":
        reader = PdfReader(str(path))
        text = "\n".join(
            (page.extract_text() or "")
            for page in reader.pages
        )

        if not text.strip():
            return [], "PDF scansione", []

        rows = extract(text)
        notes = consistency_checks(rows)
        return rows, "testo PDF", notes

    with Image.open(path) as image:
        image = image.convert("RGB")
        image = ImageEnhance.Contrast(image).enhance(1.5)
        image = ImageEnhance.Sharpness(image).enhance(1.4)
        image = image.filter(ImageFilter.SHARPEN)

        text = pytesseract.image_to_string(
            image,
            lang="ita+eng",
            config="--psm 6",
        )

    rows = extract(text)
    notes = consistency_checks(rows)
    return rows, "OCR foto", notes


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

            last_error = RuntimeError("Il file scaricato Ã¨ vuoto.")

        except (NetworkError, TimedOut) as error:
            last_error = error

        except Exception as error:
            last_error = error

        if attempt < attempts:
            await asyncio.sleep(attempt * 2)

    if last_error is not None:
        raise last_error

    raise RuntimeError("Download fallito.")


async def start(update, context):
    await update.message.reply_text(
        "ð Benvenuto in PagaCheck V1.4.\n\n"
        "ð Inviami una foto o un PDF della busta paga.\n\n"
        "â ï¸ Controllo preliminare: non sostituisce una verifica professionale.",
        reply_markup=InlineKeyboardMarkup(
            [[
                InlineKeyboardButton(
                    "ð Invia busta paga",
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
        "Perfetto. ð Inviami qui la foto o il PDF della busta paga."
    )


async def process(update, path):
    try:
        rows, method, notes = analyze(path)

        if not rows:
            await update.message.reply_text(
                "ð Documento ricevuto, ma non ho riconosciuto "
                "abbastanza voci.\n\n"
                "Prova con un'immagine piÃ¹ nitida oppure "
                "con il PDF originale."
            )
            return

        message = [
            "â PagaCheck â prima lettura completata",
            f"Metodo: {method}",
            "",
            "Voci riconosciute:",
        ]

        message += [
            f"â¢ {label}: â¬ {value}"
            for label, value in rows
        ]

        if notes:
            message += ["", "ð Controlli preliminari:"]
            message += [f"â¢ {note}" for note in notes]

        message += [
            "",
            "ð Il controllo CCNL e la verifica completa "
            "delle anomalie sono il livello successivo.",
            "",
            "â ï¸ I dati letti non certificano da soli "
            "un errore nella busta paga.",
        ]

        await update.message.reply_text("\n".join(message))

    except Exception:
        await update.message.reply_text(
            "â ï¸ Ho ricevuto il documento, ma si Ã¨ verificato "
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

    if document.file_size and document.file_size > MAX_FILE_SIZE:
        await update.message.reply_text(
            "â ï¸ Il file supera il limite di 20 MB."
        )
        return

    await update.message.reply_text("ð¥ Ricevuto. Analizzoâ¦")

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
            "â ï¸ La connessione con Telegram si Ã¨ interrotta "
            "durante il download.\n\n"
            "Ho giÃ  effettuato piÃ¹ tentativi automatici. "
            "Riprova inviando nuovamente il documento."
        )

    except Exception:
        await update.message.reply_text(
            "â ï¸ Non riesco a leggere il documento.\n\n"
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
    await update.message.reply_text("ð¥ Foto ricevuta. Avvio OCRâ¦")

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
            "â ï¸ La connessione con Telegram si Ã¨ interrotta "
            "durante il download.\n\n"
            "Riprova a inviare la foto."
        )

    except Exception:
        await update.message.reply_text(
            "â ï¸ Non riesco a leggere la foto.\n\n"
            "Prova con una foto piÃ¹ nitida e ben illuminata."
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
        "Sono pronto. ð Mandami una foto o un PDF della busta paga."
    )


bot = Application.builder().token(BOT_TOKEN).build()

bot.add_handler(CommandHandler("start", start))
bot.add_handler(CommandHandler("help", help_cmd))
bot.add_handler(MessageHandler(filters.Document.ALL, doc))
bot.add_handler(MessageHandler(filters.PHOTO, photo))
bot.add_handler(
    MessageHandler(filters.TEXT & ~filters.COMMAND, text)
)
bot.add_handler(CallbackQueryHandler(callback))


@asynccontextmanager
async def lifespan(app_instance):
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

    try:
        yield
    finally:
        if bot.updater and bot.updater.running:
            await bot.updater.stop()

        await bot.stop()
        await bot.shutdown()


app = FastAPI(
    title="PagaCheck Backend",
    version="1.4",
    lifespan=lifespan,
)


@app.get("/health", response_class=PlainTextResponse)
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

    if update_id in PROCESSED_UPDATES:
        return {"ok": True}

    PROCESSED_UPDATES.add(update_id)

    if len(PROCESSED_UPDATES) > 5000:
        PROCESSED_UPDATES.clear()

    bot.create_task(
        bot.process_update(update)
    )

    return {"ok": True}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=PORT,
    )
