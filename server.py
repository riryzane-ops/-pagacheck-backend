import os, re, tempfile
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, PlainTextResponse
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
import pytesseract
from PIL import Image
from pypdf import PdfReader

BOT_TOKEN=os.environ["BOT_TOKEN"]
PUBLIC_URL=os.environ.get("PUBLIC_URL","").rstrip("/")
WEBHOOK_SECRET=os.environ.get("WEBHOOK_SECRET","pagacheck-webhook")
PORT=int(os.environ.get("PORT","10000"))
app=FastAPI(title="PagaCheck Backend",version="1.2")
bot=Application.builder().token(BOT_TOKEN).build()

PATTERNS=[
("Retribuzione base",r"(?:retribuzione\s+base|paga\s+base|minimo)[^\d]{0,20}([\d.]+,\d{2})"),
("EDR",r"\bEDR\b[^\d]{0,20}([\d.]+,\d{2})"),
("Totale retribuzione",r"totale\s+retribuzione[^\d]{0,20}([\d.]+,\d{2})"),
("Straordinario",r"straordinari[^\d]{0,30}([\d.]+,\d{2})"),
("Contributi",r"(?:contributi|inps)[^\d]{0,30}([\d.]+,\d{2})"),
("IRPEF",r"irpef[^\d]{0,30}([\d.]+,\d{2})"),
("Netto",r"netto(?:\s+a)?\s+pagare[^\d]{0,30}([\d.]+,\d{2})")]

def extract(text):
    out=[]
    for label,pat in PATTERNS:
        m=re.search(pat,text,re.I)
        if m: out.append((label,m.group(1)))
    return out

def analyze(path):
    if path.suffix.lower()==".pdf":
        r=PdfReader(str(path)); text="\n".join((p.extract_text() or "") for p in r.pages)
        if not text.strip(): return [],"PDF scansione: OCR PDF da completare nella prossima build"
        return extract(text),"testo PDF"
    with Image.open(path) as im:
        return extract(pytesseract.image_to_string(im.convert("RGB"),lang="ita+eng")),"OCR foto"

async def start(u,c):
    await u.message.reply_text("👋 Benvenuto in PagaCheck V1.2.\n\n📎 Inviami una foto o un PDF della busta paga.\n\n⚠️ Controllo preliminare: non sostituisce una verifica professionale.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📄 Invia busta paga",callback_data="upload")]]))
async def help_cmd(u,c): await u.message.reply_text("Invia una foto o un PDF della busta paga.")
async def callback(u,c):
    await u.callback_query.answer(); await u.callback_query.message.reply_text("Perfetto. 📎 Inviami qui la foto o il PDF della busta paga.")
async def process(u,path):
    rows,method=analyze(path)
    if not rows:
        await u.message.reply_text("🔎 Documento ricevuto, ma non ho riconosciuto abbastanza voci. Prova con un'immagine più nitida o il PDF originale."); return
    msg=["✅ PagaCheck — prima lettura completata",f"Metodo: {method}","","Voci riconosciute:"]
    msg += [f"• {a}: € {b}" for a,b in rows]
    msg += ["","📌 Il controllo CCNL e la verifica completa delle anomalie sono il livello successivo.","⚠️ I dati letti non certificano da soli un errore nella busta paga."]
    await u.message.reply_text("\n".join(msg))
async def doc(u,c):
    d=u.message.document; s=Path(d.file_name or "cedolino.pdf").suffix.lower()
    if s not in {".pdf",".jpg",".jpeg",".png",".webp"}: await u.message.reply_text("Invia PDF o foto JPG/PNG/WEBP."); return
    await u.message.reply_text("📥 Ricevuto. Analizzo…")
    td=Path(tempfile.mkdtemp()); p=td/("cedolino"+s)
    try:
        f=await d.get_file(); await f.download_to_drive(str(p)); await process(u,p)
    except Exception: await u.message.reply_text("⚠️ Problema tecnico durante la lettura. Riprova con il documento originale.")
    finally:
        try: p.unlink(); td.rmdir()
        except: pass
async def photo(u,c):
    await u.message.reply_text("📥 Foto ricevuta. Avvio OCR…")
    td=Path(tempfile.mkdtemp()); p=td/"cedolino.jpg"
    try:
        f=await u.message.photo[-1].get_file(); await f.download_to_drive(str(p)); await process(u,p)
    except Exception: await u.message.reply_text("⚠️ Non riesco a leggere la foto. Prova più nitida e ben illuminata.")
    finally:
        try: p.unlink(); td.rmdir()
        except: pass
async def text(u,c): await u.message.reply_text("Sono pronto. 📎 Mandami una foto o un PDF della busta paga.")

bot.add_handler(CommandHandler("start",start)); bot.add_handler(CommandHandler("help",help_cmd)); bot.add_handler(MessageHandler(filters.CallbackQuery.ALL,callback)); bot.add_handler(MessageHandler(filters.Document.ALL,doc)); bot.add_handler(MessageHandler(filters.PHOTO,photo)); bot.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,text))
@app.get("/health",response_class=PlainTextResponse)
async def health(): return "PagaCheck OK"
@app.get("/")
async def home(): return FileResponse("PagaCheck_V1_2.html")
@app.post(f"/telegram/{WEBHOOK_SECRET}")
async def webhook(request:Request):
    u=Update.de_json(await request.json(),bot.bot); await bot.process_update(u); return {"ok":True}
@app.on_event("startup")
async def startup():
    await bot.initialize(); await bot.start()
    if PUBLIC_URL: await bot.bot.set_webhook(f"{PUBLIC_URL}/telegram/{WEBHOOK_SECRET}",drop_pending_updates=True)
    else: await bot.updater.start_polling(drop_pending_updates=True)
@app.on_event("shutdown")
async def shutdown():
    if bot.updater and bot.updater.running: await bot.updater.stop()
    await bot.stop(); await bot.shutdown()
if __name__=="__main__":
    import uvicorn; uvicorn.run("server:app",host="0.0.0.0",port=PORT)
