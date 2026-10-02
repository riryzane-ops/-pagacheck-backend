import os
import re
import json
import sqlite3
import tempfile
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from datetime import datetime, timezone

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


# ============================================================
# CONFIGURAZIONE
# ============================================================
BOT_TOKEN = os.environ["BOT_TOKEN"]
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "pagacheck-webhook")
PORT = int(os.environ.get("PORT", "10000"))
DATABASE_PATH = os.environ.get("DATABASE_PATH", "pagacheck.db")
WEB_FILE = os.environ.get("WEB_FILE", "PagaCheck_V1_4_1_web.html")

MAX_FILE_SIZE = 20 * 1024 * 1024
PROCESSED_UPDATES = set()


# ============================================================
# DATABASE: persistenza minima del profilo e dello storico
# ============================================================
def db_connect():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def db_init():
    with db_connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                first_name TEXT,
                username TEXT,
                profile_status TEXT NOT NULL DEFAULT 'missing',
                contract_json TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS slips (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                received_at TEXT NOT NULL,
                period TEXT,
                pages INTEGER,
                method TEXT,
                data_json TEXT NOT NULL,
                states_json TEXT NOT NULL,
                FOREIGN KEY(telegram_id) REFERENCES users(telegram_id)
            )
            """
        )
        conn.commit()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def ensure_user(tg_user):
    uid = tg_user.id
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO users(telegram_id, first_name, username, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                first_name=excluded.first_name,
                username=excluded.username,
                updated_at=excluded.updated_at
            """,
            (uid, tg_user.first_name or "", tg_user.username or "", now_iso()),
        )
        conn.commit()


def get_user(uid):
    with db_connect() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (uid,)
        ).fetchone()
    return row


def save_contract(uid, contract, status="pending_confirmation"):
    with db_connect() as conn:
        conn.execute(
            """
            UPDATE users
            SET contract_json=?, profile_status=?, updated_at=?
            WHERE telegram_id=?
            """,
            (json.dumps(contract, ensure_ascii=False), status, now_iso(), uid),
        )
        conn.commit()


def confirm_profile(uid):
    with db_connect() as conn:
        conn.execute(
            "UPDATE users SET profile_status='confirmed', updated_at=? WHERE telegram_id=?",
            (now_iso(), uid),
        )
        conn.commit()


def get_profile(uid):
    row = get_user(uid)
    if not row:
        return None
    contract = json.loads(row["contract_json"]) if row["contract_json"] else None
    return {
        "status": row["profile_status"],
        "contract": contract,
        "first_name": row["first_name"],
        "username": row["username"],
    }


def save_slip(uid, data, pages, method, states):
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO slips(telegram_id, received_at, period, pages, method, data_json, states_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uid,
                now_iso(),
                data.get("period", ""),
                pages,
                method,
                json.dumps(data, ensure_ascii=False),
                json.dumps(states, ensure_ascii=False),
            ),
        )
        conn.commit()


def count_slips(uid):
    with db_connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM slips WHERE telegram_id=?", (uid,)
        ).fetchone()
    return int(row["n"])


# ============================================================
# LETTURA / NORMALIZZAZIONE
# ============================================================
MONEY_PATTERN = re.compile(
    r"(?<!\d)(?P<amount>"
    r"(?:\d{1,3}(?:[.\s]\d{3})+|\d+)"
    r"(?:[,.]\d{2})"
    r")(?!\d)"
)

LABELS = [
    ("Retribuzione base", [r"retribuzione\s+base", r"paga\s+base", r"minimo\s+tabellare", r"minimo"]),
    ("EDR", [r"\bedr\b"]),
    ("Totale retribuzione", [r"totale\s+retribuzione", r"totale\s+competenze", r"totale\s+lordo"]),
    ("Straordinario", [r"straordinari", r"straordinario"]),
    ("Contributi", [r"contributi", r"inps", r"contributi\s+previdenziali"]),
    ("IRPEF", [r"irpef", r"ritenuta\s+irpef"]),
    ("Netto", [r"netto\s+a\s+pagare", r"netto\s+pagare", r"netto"]),
]


def normalize_amount(value):
    value = value.strip().replace(" ", "")
    if "," in value and "." in value:
        if value.rfind(",") > value.rfind("."):
            integer, decimal = value.rsplit(",", 1)
            return f"{integer.replace('.', '')},{decimal}"
        integer, decimal = value.rsplit(".", 1)
        return f"{integer.replace(',', '')},{decimal}"
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
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        if not any(re.search(pattern, line, re.I) for pattern in label_patterns):
            continue
        candidates = MONEY_PATTERN.findall(line)
        if candidates:
            return normalize_amount(candidates[-1])
        for next_line in lines[index + 1:index + 3]:
            candidates = MONEY_PATTERN.findall(next_line)
            if candidates:
                return normalize_amount(candidates[-1])
    return None


def extract_amount_rows(text):
    rows = []
    for label, patterns in LABELS:
        value = find_amount_near_label(text, patterns)
        if value is not None:
            rows.append((label, value))
    return rows


def first_match(patterns, text):
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return match.group(1).strip()
    return ""


def extract_contract(text):
    t = clean_ocr_text(text)
    return {
        "ccnl": first_match([
            r"CCNL[^\n:]*[:\-]\s*([^\n]+)",
            r"contratto collettivo[^\n:]*[:\-]\s*([^\n]+)",
        ], t),
        "level": first_match([
            r"(?:livello|liv\.)[^\n:]{0,15}[:\-]?\s*([A-Za-z0-9 .-]{1,12})",
        ], t),
        "role": first_match([
            r"qualifica[^\n:]*[:\-]\s*([^\n]+)",
            r"mansione[^\n:]*[:\-]\s*([^\n]+)",
        ], t),
        "start": first_match([
            r"data di assunzione[^\n:]*[:\-]\s*([0-9./-]+)",
            r"assunzione[^\n:]*[:\-]\s*([0-9./-]+)",
        ], t),
        "hours": first_match([
            r"(?:orario|ore settimanali)[^\n:]*[:\-]\s*([^\n]+)",
        ], t),
        "levelDate": first_match([
            r"decorrenza[^\n:]*livello[^\n:]*[:\-]\s*([0-9./-]+)",
        ], t),
    }


def extract_slip(text):
    t = clean_ocr_text(text)

    def amount(patterns):
        value = find_amount_near_label(t, patterns)
        return value or ""

    return {
        "period": first_match([
            r"(?:periodo|competenza|mese)[^\n:]*[:\-]\s*([^\n]+)",
        ], t),
        "level": first_match([
            r"(?:livello|liv\.)[^\n:]{0,15}[:\-]?\s*([A-Za-z0-9 .-]{1,12})",
        ], t),
        "base": amount([r"retribuzione\s+base", r"paga\s+base", r"minimo\s+tabellare"]),
        "total": amount([r"totale\s+retribuzione", r"totale\s+competenze", r"totale\s+lordo"]),
        "overtime": amount([r"straordinari[oa]?"]),
        "contributions": amount([r"contributi", r"INPS"]),
        "irpef": amount([r"IRPEF"]),
        "net": amount([r"netto\s+a\s+pagare", r"netto\s+pagare"]),
    }


def ocr_image(path):
    with Image.open(path) as image:
        image = image.convert("RGB")
        image = ImageEnhance.Contrast(image).enhance(1.5)
        image = ImageEnhance.Sharpness(image).enhance(1.4)
        image = image.filter(ImageFilter.SHARPEN)
        text = pytesseract.image_to_string(image, lang="ita+eng", config="--psm 6")
    return clean_ocr_text(text)


def read_document(path):
    if path.suffix.lower() == ".pdf":
        reader = PdfReader(str(path))
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
        return clean_ocr_text(text), len(reader.pages), "testo PDF"
    return ocr_image(path), 1, "OCR foto"


# ============================================================
# CLASSIFICAZIONE DOCUMENTO
# ============================================================
def looks_like_contract(text):
    lower = text.lower()
    signals = [
        "contratto di lavoro",
        "lettera di assunzione",
        "contratto individuale",
        "ccnl applicato",
        "data di assunzione",
        "mansione",
        "qualifica professionale",
    ]
    return sum(1 for s in signals if s in lower) >= 2


def looks_like_payslip(text):
    lower = text.lower()
    signals = [
        "cedolino",
        "busta paga",
        "retribuzione",
        "contributi",
        "irpef",
        "netto",
        "competenze",
        "inps",
    ]
    return sum(1 for s in signals if s in lower) >= 2


def classify_document(text, profile):
    # Se il profilo ha giÃ  un contratto confermato, un documento con segnali
    # forti di cedolino viene trattato come cedolino. In caso contrario
    # manteniamo una classificazione prudente.
    if looks_like_contract(text) and not profile.get("contract"):
        return "contract"
    if looks_like_payslip(text):
        return "slip"
    if looks_like_contract(text):
        return "contract"
    return "unknown"


# ============================================================
# MOTORE DI CONTROLLO: prudente, senza inventare regole mancanti
# ============================================================
def compare_profile_to_slip(profile, slip):
    states = []
    contract = profile.get("contract") if profile else None

    if not contract:
        states.append({
            "control": "Profilo contrattuale",
            "state": "white",
            "title": "Contratto non acquisito",
            "explanation": "Senza il contratto individuale non posso stabilire con sufficiente sicurezza quale livello, qualifica e condizioni si applichino al lavoratore.",
        })
        return states

    if profile.get("status") != "confirmed":
        states.append({
            "control": "Profilo contrattuale",
            "state": "yellow",
            "title": "Profilo da confermare",
            "explanation": "Ho letto il contratto, ma i dati estratti devono essere confermati prima di usarli come base del controllo.",
        })
    else:
        states.append({
            "control": "Profilo contrattuale",
            "state": "green",
            "title": "Profilo disponibile",
            "explanation": "Il profilo contrattuale confermato puÃ² essere usato come base del confronto.",
        })

    contract_level = (contract.get("fields") or {}).get("level", "").strip()
    slip_level = (slip.get("level") or "").strip()

    if contract_level and slip_level:
        if contract_level.casefold() == slip_level.casefold():
            states.append({
                "control": "Livello",
                "state": "green",
                "title": "Livello coerente",
                "explanation": f"Il livello letto sul cedolino ({slip_level}) coincide con quello presente nel profilo contrattuale ({contract_level}).",
            })
        else:
            states.append({
                "control": "Livello",
                "state": "red",
                "title": "Possibile differenza di livello",
                "explanation": f"Il contratto/profilo indica '{contract_level}', mentre sul cedolino Ã¨ stato letto '{slip_level}'. Prima di considerarlo un errore bisogna verificare le date di decorrenza e gli eventuali documenti successivi.",
            })
    else:
        states.append({
            "control": "Livello",
            "state": "white",
            "title": "Livello non verificabile",
            "explanation": "Non sono disponibili entrambi i dati con sufficiente chiarezza per un confronto.",
        })

    # Controlli matematici indipendenti quando i dati necessari sono presenti.
    total = amount_to_float(slip.get("total"))
    net = amount_to_float(slip.get("net"))
    if total is not None and net is not None:
        if net <= total:
            states.append({
                "control": "Rapporto lordo/netto",
                "state": "green",
                "title": "Nessuna incoerenza matematica evidente",
                "explanation": "Il netto letto non supera il totale delle competenze. Questo controllo non certifica perÃ² il corretto calcolo di tutte le singole voci.",
            })
        else:
            states.append({
                "control": "Rapporto lordo/netto",
                "state": "red",
                "title": "Dato da verificare",
                "explanation": "Il netto letto risulta superiore al totale delle competenze. Potrebbe dipendere da una lettura OCR errata o da una struttura del cedolino non ancora interpretata correttamente.",
            })
    else:
        states.append({
            "control": "Rapporto lordo/netto",
            "state": "white",
            "title": "Calcolo non verificabile",
            "explanation": "Mancano uno o piÃ¹ importi necessari per questo controllo.",
        })

    states.append({
        "control": "CCNL e normativa",
        "state": "white",
        "title": "Verifica normativa non ancora eseguita",
        "explanation": "Il server ha acquisito il profilo e il cedolino, ma non deve inventare minimi, maggiorazioni o regole di legge. Per questo controllo servono il CCNL identificato e la normativa applicabile al periodo.",
    })

    states.append({
        "control": "Timbrature",
        "state": "white",
        "title": "Timbrature non disponibili",
        "explanation": "Non risultano timbrature strutturate collegate a questo controllo. Se vengono fornite, possono essere confrontate con ore ordinarie, straordinari, sesti giorni, festivi e altre voci.",
    })

    return states


def old_consistency_notes(rows):
    values = {label: amount_to_float(value) for label, value in rows}
    notes = []
    netto = values.get("Netto")
    contributi = values.get("Contributi")
    irpef = values.get("IRPEF")
    totale = values.get("Totale retribuzione")
    if netto is not None and totale is not None and netto > totale:
        notes.append("Il netto risulta superiore al totale retribuzione letto: dato da verificare.")
    if contributi is not None and totale is not None and contributi > totale:
        notes.append("I contributi letti risultano superiori al totale retribuzione: possibile lettura OCR errata o dato da verificare.")
    if irpef is not None and totale is not None and irpef > totale:
        notes.append("L'IRPEF letta risulta superiore al totale retribuzione: dato da verificare.")
    return notes


# ============================================================
# TELEGRAM
# ============================================================
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
            last_error = RuntimeError("Il file scaricato e' vuoto.")
        except (NetworkError, TimedOut) as error:
            last_error = error
        except Exception as error:
            last_error = error
        if attempt < attempts:
            await asyncio.sleep(attempt * 2)
    raise last_error or RuntimeError("Download fallito.")


async def start(update, context):
    ensure_user(update.effective_user)
    profile = get_profile(update.effective_user.id)
    slip_count = count_slips(update.effective_user.id)
    contract_state = "presente" if profile and profile.get("contract") else "mancante"
    await update.message.reply_text(
        "ð Benvenuto in PagaCheck.\n\n"
        "Prima costruiamo il profilo dal contratto individuale. Poi analizziamo i cedolini mese per mese.\n\n"
        f"ð Contratto: {contract_state}\n"
        f"ð¶ Cedolini acquisiti: {slip_count}\n\n"
        "Invia prima il contratto oppure usa i pulsanti qui sotto.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("ð Carica contratto", callback_data="contract")],
            [InlineKeyboardButton("ð¶ Carica cedolino", callback_data="slip")],
            [InlineKeyboardButton("ð¤ Mostra profilo", callback_data="profile")],
        ]),
    )


async def help_cmd(update, context):
    ensure_user(update.effective_user)
    await update.message.reply_text(
        "PagaCheck usa questa sequenza:\n\n"
        "1. Contratto individuale\n"
        "2. Conferma del profilo\n"
        "3. Cedolini\n"
        "4. Confronto con contratto e dati disponibili\n\n"
        "Puoi inviare un PDF multipagina come unico documento."
    )


async def profile_cmd(update, context):
    ensure_user(update.effective_user)
    profile = get_profile(update.effective_user.id)
    if not profile or not profile.get("contract"):
        await update.message.reply_text("Non ho ancora acquisito il contratto individuale.")
        return
    fields = profile["contract"].get("fields", {})
    lines = [
        "ð¤ Profilo PagaCheck",
        f"Stato: {'confermato' if profile['status'] == 'confirmed' else 'da confermare'}",
        "",
        f"CCNL: {fields.get('ccnl') or 'non riconosciuto'}",
        f"Livello: {fields.get('level') or 'non riconosciuto'}",
        f"Qualifica/mansione: {fields.get('role') or 'non riconosciuta'}",
        f"Assunzione: {fields.get('start') or 'non riconosciuta'}",
        f"Orario: {fields.get('hours') or 'non riconosciuto'}",
        f"Decorrenza livello: {fields.get('levelDate') or 'non riconosciuta'}",
    ]
    if profile["status"] != "confirmed":
        lines += ["", "ð¡ Se i dati sono corretti, usa /conferma."]
    await update.message.reply_text("\n".join(lines))


async def confirm_cmd(update, context):
    ensure_user(update.effective_user)
    profile = get_profile(update.effective_user.id)
    if not profile or not profile.get("contract"):
        await update.message.reply_text("Non c'Ã¨ ancora un contratto da confermare.")
        return
    confirm_profile(update.effective_user.id)
    await update.message.reply_text(
        "â Profilo confermato.\n\n"
        "Da questo momento il contratto confermato viene usato come base del confronto. "
        "Il livello scritto sul cedolino non viene considerato automaticamente corretto: viene confrontato con il profilo e con le decorrenze disponibili."
    )


async def callback(update, context):
    await update.callback_query.answer()
    data = update.callback_query.data
    if data == "contract":
        await update.callback_query.message.reply_text("ð Inviami il PDF o una foto del contratto individuale.")
    elif data == "slip":
        await update.callback_query.message.reply_text("ð¶ Inviami il cedolino. Se Ã¨ un PDF di piÃ¹ pagine, mandalo come unico file.")
    elif data == "profile":
        profile = get_profile(update.effective_user.id)
        if not profile or not profile.get("contract"):
            await update.callback_query.message.reply_text("Non ho ancora acquisito il contratto individuale.")
        else:
            fields = profile["contract"].get("fields", {})
            await update.callback_query.message.reply_text(
                "ð¤ Profilo: " + ("confermato" if profile["status"] == "confirmed" else "da confermare") + "\n\n"
                f"CCNL: {fields.get('ccnl') or 'non riconosciuto'}\n"
                f"Livello: {fields.get('level') or 'non riconosciuto'}\n"
                f"Qualifica/mansione: {fields.get('role') or 'non riconosciuta'}\n\n"
                "Per confermare: /conferma"
            )


async def process_document(update, path):
    ensure_user(update.effective_user)
    uid = update.effective_user.id
    profile = get_profile(uid) or {"status": "missing", "contract": None}

    text, pages, method = read_document(path)
    if not text.strip():
        await update.message.reply_text(
            "âª Documento ricevuto, ma non Ã¨ stato possibile estrarre testo. "
            "Se Ã¨ un PDF scansione, prova con una foto nitida delle pagine."
        )
        return

    doc_type = classify_document(text, profile)

    if doc_type == "contract" and not profile.get("contract"):
        fields = extract_contract(text)
        contract = {"fields": fields, "source_pages": pages}
        save_contract(uid, contract, status="pending_confirmation")

        lines = [
            "ð Contratto acquisito",
            "",
            f"Pagine: {pages}",
            f"Metodo: {method}",
            "",
            "Dati letti:",
            f"â¢ CCNL: {fields.get('ccnl') or 'non riconosciuto'}",
            f"â¢ Livello: {fields.get('level') or 'non riconosciuto'}",
            f"â¢ Qualifica/mansione: {fields.get('role') or 'non riconosciuta'}",
            f"â¢ Assunzione: {fields.get('start') or 'non riconosciuta'}",
            f"â¢ Orario: {fields.get('hours') or 'non riconosciuto'}",
            f"â¢ Decorrenza livello: {fields.get('levelDate') or 'non riconosciuta'}",
            "",
            "ð¡ Prima di usare questi dati per controllare i cedolini, verifica che siano corretti.",
            "Se sono corretti: /conferma",
            "Se qualcosa non torna: non confermare ancora.",
        ]
        await update.message.reply_text("\n".join(lines))
        return

    if doc_type == "unknown":
        await update.message.reply_text(
            "âª Non riesco a stabilire con sufficiente sicurezza se questo documento sia un contratto o un cedolino.\n\n"
            "Invia il contratto individuale oppure il cedolino in PDF originale/foto nitida."
        )
        return

    # Cedolino: estrazione + vecchi controlli preliminari + nuovo motore prudente.
    slip = extract_slip(text)
    rows = extract_amount_rows(text)
    notes = old_consistency_notes(rows)
    states = compare_profile_to_slip(profile, slip)
    save_slip(uid, slip, pages, method, states)

    lines = [
        "ð¶ PagaCheck â cedolino acquisito",
        f"Pagine: {pages}",
        f"Metodo: {method}",
        "",
        "Dati letti:",
    ]
    labels = {
        "period": "Periodo",
        "level": "Livello",
        "base": "Retribuzione base",
        "total": "Totale competenze",
        "overtime": "Straordinari",
        "contributions": "Contributi",
        "irpef": "IRPEF",
        "net": "Netto",
    }
    found = False
    for key, label in labels.items():
        if slip.get(key):
            lines.append(f"â¢ {label}: {slip[key]}")
            found = True
    if not found:
        lines.append("â¢ Nessuna voce economica riconosciuta con sufficiente sicurezza.")

    if notes:
        lines += ["", "Controlli preliminari:"]
        lines += [f"â¢ {note}" for note in notes]

    lines += ["", "Esito dei controlli:"]
    icons = {"green": "ð¢", "yellow": "ð¡", "red": "ð´", "white": "âª"}
    for state in states:
        lines.append(f"{icons.get(state['state'], 'âª')} {state['control']}: {state['title']}")
        lines.append(f"   {state['explanation']}")

    lines += [
        "",
        "Nota importante: un dato letto dal cedolino non viene considerato automaticamente corretto nÃ© automaticamente errato. Quando mancano contratto, CCNL, decorrenze o timbrature, PagaCheck segnala il controllo come non verificabile o da verificare.",
    ]
    await update.message.reply_text("\n".join(lines))


async def document(update, context):
    doc = update.message.document
    extension = Path(doc.file_name or "documento.pdf").suffix.lower()
    allowed = {".pdf", ".jpg", ".jpeg", ".png", ".webp"}
    if extension not in allowed:
        await update.message.reply_text("Invia un PDF oppure una foto JPG, PNG o WEBP.")
        return
    if doc.file_size and doc.file_size > MAX_FILE_SIZE:
        await update.message.reply_text("Il file supera il limite di 20 MB.")
        return

    await update.message.reply_text("ð¥ Documento ricevuto. Analizzoâ¦")
    temp_dir = Path(tempfile.mkdtemp())
    path = temp_dir / f"documento{extension}"
    try:
        await download_with_retry(doc.file_id, path, attempts=3)
        await process_document(update, path)
    except (NetworkError, TimedOut):
        await update.message.reply_text(
            "La connessione con Telegram si Ã¨ interrotta durante il download.\n\n"
            "Ho effettuato piÃ¹ tentativi automatici. Riprova inviando nuovamente il documento."
        )
    except Exception:
        await update.message.reply_text(
            "Non riesco a leggere il documento. Prova con il PDF originale oppure con una foto piÃ¹ nitida."
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
    path = temp_dir / "documento.jpg"
    try:
        await download_with_retry(update.message.photo[-1].file_id, path, attempts=3)
        await process_document(update, path)
    except (NetworkError, TimedOut):
        await update.message.reply_text(
            "La connessione con Telegram si Ã¨ interrotta durante il download. Riprova a inviare la foto."
        )
    except Exception:
        await update.message.reply_text(
            "Non riesco a leggere la foto. Prova con una foto piÃ¹ nitida e ben illuminata."
        )
    finally:
        try:
            if path.exists():
                path.unlink()
            temp_dir.rmdir()
        except Exception:
            pass


async def text_message(update, context):
    ensure_user(update.effective_user)
    await update.message.reply_text(
        "Sono pronto. ð Inviami prima il contratto individuale; poi potrai inviare i cedolini.\n\n"
        "Comandi utili: /profilo e /conferma"
    )


# ============================================================
# APP TELEGRAM / FASTAPI
# ============================================================
bot = Application.builder().token(BOT_TOKEN).build()
bot.add_handler(CommandHandler("start", start))
bot.add_handler(CommandHandler("help", help_cmd))
bot.add_handler(CommandHandler("profilo", profile_cmd))
bot.add_handler(CommandHandler("conferma", confirm_cmd))
bot.add_handler(MessageHandler(filters.Document.ALL, document))
bot.add_handler(MessageHandler(filters.PHOTO, photo))
bot.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message))
bot.add_handler(CallbackQueryHandler(callback))


@asynccontextmanager
async def lifespan(app_instance):
    db_init()
    await bot.initialize()
    await bot.start()

    if PUBLIC_URL:
        await bot.bot.set_webhook(
            f"{PUBLIC_URL}/telegram/{WEBHOOK_SECRET}",
            drop_pending_updates=True,
        )
    else:
        await bot.updater.start_polling(drop_pending_updates=True)

    try:
        yield
    finally:
        if bot.updater and bot.updater.running:
            await bot.updater.stop()
        await bot.stop()
        await bot.shutdown()


app = FastAPI(
    title="PagaCheck Backend",
    version="1.5.0",
    lifespan=lifespan,
)


@app.get("/health", response_class=PlainTextResponse)
async def health():
    return "PagaCheck OK â V1.5.0"


@app.get("/")
async def home():
    return FileResponse(WEB_FILE)


@app.post(f"/telegram/{WEBHOOK_SECRET}")
async def webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, bot.bot)
    update_id = update.update_id

    if update_id in PROCESSED_UPDATES:
        return {"ok": True}

    PROCESSED_UPDATES.add(update_id)
    if len(PROCESSED_UPDATES) > 5000:
        PROCESSED_UPDATES.clear()

    bot.create_task(bot.process_update(update))
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=PORT)
