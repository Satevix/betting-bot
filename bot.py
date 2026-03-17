"""
Betting Manager Bot — Telegram
pip install python-telegram-bot anthropic requests
"""
import os, json, re, logging, base64, requests
from datetime import datetime
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton, BotCommand
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
import anthropic

# ── CONFIG ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_KEY    = os.environ["ANTHROPIC_KEY"]
GOOGLE_SHEET_URL = os.environ["GOOGLE_SHEET_URL"]
AUTHORIZED_USER  = int(os.environ.get("AUTHORIZED_USER", "0"))
STATE_FILE       = "state.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── STATE ─────────────────────────────────────────────────────────────────────
DEFAULT_STATE = {
    "capital": 1_000_000, "capital_inicial": 1_000_000,
    "pct_ap": 0.3, "cuota_ref": 1.833,
    "racha": 0, "num_mg": 0,
    "partidos": [], "movimientos": [],
}

def load():
    try:
        with open(STATE_FILE) as f: return {**DEFAULT_STATE, **json.load(f)}
    except: return dict(DEFAULT_STATE)

def save(s):
    with open(STATE_FILE, "w") as f: json.dump(s, f, ensure_ascii=False, indent=2)

# ── FÓRMULA ───────────────────────────────────────────────────────────────────
def calc_am(s):
    return max(1000, round(s["capital"] * s["pct_ap"] / 100))

def calc_apuesta(s, cuota):
    AM  = calc_am(s)
    div = cuota - 1
    if s["num_mg"] == 0:
        ap = AM
    else:
        base = s["capital"] * s["pct_ap"] / 100
        ap   = round(((base * (1 + s["num_mg"] * 0.5)) + s["racha"]) / div)
    ap = max(ap, 1000)
    gan_neta = round(ap * div) - s["racha"]
    return ap, AM, gan_neta

# ── SHEETS ────────────────────────────────────────────────────────────────────
def sheets(action, data):
    try:
        params = {"action": action, "data": json.dumps(data)}
        requests.get(GOOGLE_SHEET_URL, params=params, timeout=10)
    except Exception as e:
        log.error(f"Sheets error: {e}")

# ── AUTH ──────────────────────────────────────────────────────────────────────
def auth(update):
    return AUTHORIZED_USER == 0 or update.effective_user.id == AUTHORIZED_USER

# ── CLAUDE: ANALIZAR IMAGEN ───────────────────────────────────────────────────
def analizar_imagen(image_bytes: bytes) -> list:
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    b64    = base64.standard_b64encode(image_bytes).decode()
    msg    = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1000,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
            {"type": "text", "text": (
                "Analiza esta imagen de BetPlay u otra casa de apuestas.\n"
                "Extrae TODOS los partidos de fútbol visibles.\n"
                "Responde SOLO con JSON válido, sin texto extra ni markdown.\n"
                'Formato: [{"local":"...","visitante":"...","fecha":"YYYY-MM-DD",'
                '"hora":"HH:MM","liga":"...","cuota_local":null,"cuota_empate":null,"cuota_visitante":null}]\n'
                "Si no hay fecha usa hoy. Si no hay hora usa null."
            )}
        ]}]
    )
    text = re.sub(r'```json|```', '', msg.content[0].text).strip()
    try:    return json.loads(text)
    except: return []

# ── FORMAT ────────────────────────────────────────────────────────────────────
def fmt(n):   return f"${abs(round(n)):,}".replace(",", ".")
def fsign(n): return ("+" if n >= 0 else "-") + fmt(n)
def now_ts(): return datetime.now().isoformat()

# ── TECLADOS ──────────────────────────────────────────────────────────────────
def menu_keyboard():
    return ReplyKeyboardMarkup([
        [KeyboardButton("📋 Partidos"),   KeyboardButton("💰 Capital")],
        [KeyboardButton("🎯 Apostar"),    KeyboardButton("✅ Resultado")],
        [KeyboardButton("⬆ Ingreso"),    KeyboardButton("⬇ Egreso")],
        [KeyboardButton("⚙ Config"),     KeyboardButton("📸 Analizar foto")],
    ], resize_keyboard=True, persistent=True)

def cancelar_keyboard():
    return ReplyKeyboardMarkup([[KeyboardButton("❌ Cancelar")]], resize_keyboard=True)

# ── RESUMEN ───────────────────────────────────────────────────────────────────
def resumen(s):
    mg = f"🔴 MG n={s['num_mg']} · Pérd: {fmt(s['racha'])}" if s["num_mg"] > 0 else "🟢 Sin racha"
    prog = sum(1 for p in s["partidos"] if p["estado"] == "programado")
    apst = sum(1 for p in s["partidos"] if p["estado"] == "apostado")
    ap, AM, gan = calc_apuesta(s, s["cuota_ref"])
    tipo = f"MG n={s['num_mg']}" if s["num_mg"] > 0 else "Normal"
    return (
        f"⚽ *Betting Manager*\n\n"
        f"💰 Capital: *{fmt(s['capital'])}*\n"
        f"📊 AM: {fmt(AM)} · %AP: {s['pct_ap']}%\n"
        f"{mg}\n\n"
        f"📋 Programados: {prog} | 🎯 En juego: {apst}\n\n"
        f"*Próxima apuesta ({tipo}):*\n"
        f"💵 {fmt(ap)} · Si ganas: {fsign(gan)}"
    )

# ── LISTA PARTIDOS ────────────────────────────────────────────────────────────
def lista_texto(partidos):
    if not partidos: return "_No hay partidos._"
    icons = {"programado":"📋","apostado":"🎯","ganado":"✅","perdido":"❌","cancelado":"🚫"}
    lines = []
    for i, p in enumerate(partidos):
        ic    = icons.get(p["estado"], "•")
        hora  = f" · 🕐 {p['hora']}" if p.get("hora") else ""
        liga  = f" · _{p['liga']}_"  if p.get("liga") else ""
        ap    = f"\n   💵 {fmt(p['apuesta'])} · A: *{p['apuesta_a']}* · ×{p['cuota']}" if p.get("apuesta") else ""
        lines.append(
            f"{ic} *{i+1}. {p['local']} vs {p['visitante']}*\n"
            f"   📅 {p.get('fecha','')}{hora}{liga}{ap}"
        )
    return "\n\n".join(lines)

def botones_inline(partidos, accion):
    if not partidos: return None
    btns = []
    for i, p in enumerate(partidos):
        hora  = f" {p['hora']}" if p.get("hora") else ""
        label = f"{'🎯' if accion=='apostar' else '✅'} {i+1}. {p['local']} vs {p['visitante']}{hora}"
        btns.append([InlineKeyboardButton(label, callback_data=f"{accion}:{p['id']}")])
    return InlineKeyboardMarkup(btns)

# ── /START ────────────────────────────────────────────────────────────────────
async def cmd_start(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not auth(u): return
    s = load()
    await u.message.reply_text(resumen(s), parse_mode="Markdown", reply_markup=menu_keyboard())

# ── HANDLER MENÚ PRINCIPAL ────────────────────────────────────────────────────
async def handle_menu(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not auth(u): return
    texto = u.message.text
    s     = load()

    if texto == "📋 Partidos":
        activos  = [p for p in s["partidos"] if p["estado"] in ("programado","apostado")]
        cerrados = [p for p in s["partidos"] if p["estado"] in ("ganado","perdido")][-5:]
        resp = ""
        if activos:
            resp += f"*🎯 Activos ({len(activos)}):*\n\n{lista_texto(activos)}"
        if cerrados:
            resp += f"\n\n*📁 Últimos resultados:*\n\n{lista_texto(cerrados)}"
        if not resp:
            resp = "No hay partidos.\nEnvía una 📸 foto de BetPlay para agregar."
        await u.message.reply_text(resp, parse_mode="Markdown", reply_markup=menu_keyboard())

    elif texto == "💰 Capital":
        jug  = [p for p in s["partidos"] if p["estado"] in ("ganado","perdido")]
        gan  = [p for p in jug if p["estado"] == "ganado"]
        neto = sum(p.get("gan_neta",0) for p in gan)
        var  = s["capital"] - s["capital_inicial"]
        movs = s.get("movimientos",[])
        bal  = sum(m["monto"] if m["tipo"]=="ingreso" else -m["monto"] for m in movs)
        await u.message.reply_text(
            f"💰 *Estado del Capital*\n\n"
            f"Actual:    *{fmt(s['capital'])}*\n"
            f"Inicial:   {fmt(s['capital_inicial'])}\n"
            f"Variación: {fsign(var)}\n"
            f"Movimientos: {fsign(bal)}\n\n"
            f"Jugados: {len(jug)} · Ganados: {len(gan)} · Perdidos: {len(jug)-len(gan)}\n"
            f"Ganancia neta: {fsign(neto)}\n\n"
            f"{'🔴 MG n='+str(s['num_mg'])+' · Pérd: '+fmt(s['racha']) if s['num_mg']>0 else '🟢 Sin racha'}",
            parse_mode="Markdown", reply_markup=menu_keyboard()
        )

    elif texto == "🎯 Apostar":
        prog = [p for p in s["partidos"] if p["estado"] == "programado"]
        if not prog:
            await u.message.reply_text("No hay partidos programados.\nEnvía una 📸 foto para agregar.", reply_markup=menu_keyboard()); return
        await u.message.reply_text(
            f"*🎯 ¿A qué partido quieres apostar?*\n\n{lista_texto(prog)}",
            parse_mode="Markdown",
            reply_markup=botones_inline(prog, "apostar")
        )

    elif texto == "✅ Resultado":
        apst = [p for p in s["partidos"] if p["estado"] == "apostado"]
        if not apst:
            await u.message.reply_text("No hay partidos en juego.", reply_markup=menu_keyboard()); return
        await u.message.reply_text(
            f"*✅ ¿De qué partido registras el resultado?*\n\n{lista_texto(apst)}",
            parse_mode="Markdown",
            reply_markup=botones_inline(apst, "resultado")
        )

    elif texto == "⬆ Ingreso":
        ctx.user_data.clear()
        ctx.user_data["mov_tipo"] = "ingreso"
        await u.message.reply_text(
            f"⬆ *Registrar ingreso*\n\nCapital actual: {fmt(s['capital'])}\n\nEscribe el monto:",
            parse_mode="Markdown", reply_markup=cancelar_keyboard()
        )

    elif texto == "⬇ Egreso":
        ctx.user_data.clear()
        ctx.user_data["mov_tipo"] = "egreso"
        await u.message.reply_text(
            f"⬇ *Registrar egreso / retiro*\n\nCapital disponible: {fmt(s['capital'])}\n\nEscribe el monto:",
            parse_mode="Markdown", reply_markup=cancelar_keyboard()
        )

    elif texto == "⚙ Config":
        await u.message.reply_text(
            f"⚙ *Configuración*\n\n"
            f"• % AP: `{s['pct_ap']}%` → AM: {fmt(calc_am(s))}\n"
            f"• Cuota ref: `{s['cuota_ref']}`\n\n"
            f"Para cambiar:\n`/config pct 0.5`\n`/config cuota 2.0`",
            parse_mode="Markdown", reply_markup=menu_keyboard()
        )

    elif texto == "📸 Analizar foto":
        await u.message.reply_text("📸 Envía la captura de BetPlay.", reply_markup=menu_keyboard())

    elif texto == "❌ Cancelar":
        ctx.user_data.clear()
        await u.message.reply_text("Operación cancelada.", reply_markup=menu_keyboard())

# ── CALLBACK: BOTONES INLINE ──────────────────────────────────────────────────
async def handle_callback(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = u.callback_query
    await query.answer()
    if not auth(u): return

    partes = query.data.split(":")
    accion = partes[0]
    pid    = int(partes[1])
    s      = load()
    p      = next((x for x in s["partidos"] if x["id"] == pid), None)
    if not p:
        await query.edit_message_text("❌ Partido no encontrado."); return

    hora  = f" · 🕐 {p['hora']}" if p.get("hora") else ""
    fecha = f"📅 {p.get('fecha','')}{hora}"

    if accion == "apostar":
        ctx.user_data["partido_id"] = pid
        btns = [[
            InlineKeyboardButton(p["local"],    callback_data=f"equipo:{pid}:{p['local']}"),
            InlineKeyboardButton(p["visitante"],callback_data=f"equipo:{pid}:{p['visitante']}"),
            InlineKeyboardButton("Empate",      callback_data=f"equipo:{pid}:Empate"),
        ]]
        await query.edit_message_text(
            f"🎯 *{p['local']} vs {p['visitante']}*\n{fecha}\n\n¿A quién le apuestas?",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(btns)
        )

    elif accion == "equipo":
        equipo = partes[2]
        ctx.user_data["partido_id"] = pid
        ctx.user_data["equipo"]     = equipo
        ctx.user_data["esperando"]  = "cuota"
        ap, AM, gan = calc_apuesta(s, s["cuota_ref"])
        await query.edit_message_text(
            f"🎯 *{p['local']} vs {p['visitante']}*\n"
            f"{fecha}\n"
            f"👤 A favor de: *{equipo}*\n\n"
            f"Escribe la cuota del partido\n_(ej: 1.85)_:",
            parse_mode="Markdown"
        )

    elif accion == "resultado":
        ctx.user_data["partido_id"] = pid
        btns = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Gané",  callback_data=f"res_tipo:{pid}:ganado"),
            InlineKeyboardButton("❌ Perdí", callback_data=f"res_tipo:{pid}:perdido"),
        ]])
        await query.edit_message_text(
            f"✅ *{p['local']} vs {p['visitante']}*\n"
            f"{fecha}\n"
            f"💵 Apuesta: {fmt(p['apuesta'])} · A: {p['apuesta_a']}\n\n"
            f"¿Cómo terminó?",
            parse_mode="Markdown", reply_markup=btns
        )

    elif accion == "res_tipo":
        resultado = partes[2]
        ctx.user_data["partido_id"] = pid
        ctx.user_data["resultado"]  = resultado
        ctx.user_data["esperando"]  = "marcador"
        await query.edit_message_text(
            f"{'✅' if resultado=='ganado' else '❌'} *{p['local']} vs {p['visitante']}*\n\n"
            f"Escribe el marcador final _(ej: 2-1)_:",
            parse_mode="Markdown"
        )

# ── HANDLER TEXTO LIBRE (flujos) ──────────────────────────────────────────────
async def handle_texto(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not auth(u): return
    texto     = u.message.text.strip()
    esperando = ctx.user_data.get("esperando")
    mov_tipo  = ctx.user_data.get("mov_tipo")
    s         = load()

    if texto == "❌ Cancelar":
        ctx.user_data.clear()
        await u.message.reply_text("Operación cancelada.", reply_markup=menu_keyboard()); return

    # ── Cuota ─────────────────────────────────────────────────────────────────
    if esperando == "cuota":
        try:
            cuota = float(texto.replace(",",".")); assert cuota > 1
        except:
            await u.message.reply_text("❌ Escribe la cuota como número ej: `1.85`", parse_mode="Markdown"); return

        pid    = ctx.user_data.get("partido_id")
        equipo = ctx.user_data.get("equipo")
        p      = next((x for x in s["partidos"] if x["id"] == pid), None)
        if not p:
            await u.message.reply_text("❌ Partido no encontrado.", reply_markup=menu_keyboard()); return

        ap, AM, gan_neta = calc_apuesta(s, cuota)
        tipo = "Normal" if s["num_mg"]==0 else f"MG n={s['num_mg']} (×{1+s['num_mg']*0.5})"
        idx  = s["partidos"].index(p)
        s["partidos"][idx].update({
            "apuesta_a": equipo, "cuota": cuota, "apuesta": ap, "AM": AM,
            "gan_neta_esp": gan_neta,
            "tipo_apuesta": "Normal" if s["num_mg"]==0 else f"MG n={s['num_mg']}",
            "num_mg": s["num_mg"], "perdida_acum_al_apostar": s["racha"],
            "estado": "apostado", "ts_apuesta": now_ts(),
        })
        save(s)
        sheets("actualizar_partido", s["partidos"][idx])
        ctx.user_data.clear()

        hora = f" · 🕐 {p['hora']}" if p.get("hora") else ""
        await u.message.reply_text(
            f"🎯 *Apuesta registrada*\n\n"
            f"📋 {p['local']} vs {p['visitante']}\n"
            f"📅 {p.get('fecha','')}{hora}\n"
            f"👤 A favor de: *{equipo}*\n"
            f"📊 Cuota: {cuota} · Tipo: {tipo}\n\n"
            f"💵 *Apostar: {fmt(ap)}*\n"
            f"Si ganas: {fsign(gan_neta)} neto\n\n"
            f"💰 Capital: {fmt(s['capital'])}",
            parse_mode="Markdown", reply_markup=menu_keyboard()
        )

    # ── Marcador ──────────────────────────────────────────────────────────────
    elif esperando == "marcador":
        if not re.match(r'^\d+-\d+$', texto):
            await u.message.reply_text("❌ Escribe el marcador así: `2-1`", parse_mode="Markdown"); return

        pid       = ctx.user_data.get("partido_id")
        resultado = ctx.user_data.get("resultado")
        p         = next((x for x in s["partidos"] if x["id"] == pid), None)
        if not p:
            await u.message.reply_text("❌ Partido no encontrado.", reply_markup=menu_keyboard()); return

        gano     = resultado == "ganado"
        ap       = p["apuesta"]; cuota = p["cuota"]
        pa       = p.get("perdida_acum_al_apostar", 0)
        ganancia = round(ap * (cuota - 1)) if gano else 0
        gan_neta = ganancia - pa
        idx      = s["partidos"].index(p)
        s["partidos"][idx].update({
            "estado": "ganado" if gano else "perdido",
            "marcador": texto, "ganancia": ganancia,
            "gan_neta": gan_neta, "ts_resultado": now_ts(),
        })
        if gano:
            s["capital"] += ganancia; s["racha"] = 0; s["num_mg"] = 0
            msg_res = f"🎉 *¡Ganaste!*\nBruto: {fmt(ganancia)} · *Neto: {fsign(gan_neta)}*"
        else:
            s["capital"] -= ap; s["racha"] += ap; s["num_mg"] += 1
            ap_sig, _, gn_sig = calc_apuesta(s, cuota)
            msg_res = (f"😞 *Perdiste* {fmt(ap)}\n"
                      f"Pérd. acumulada: {fmt(s['racha'])}\n"
                      f"🔴 MG n={s['num_mg']} → próxima: *{fmt(ap_sig)}*")

        save(s)
        sheets("actualizar_partido", s["partidos"][idx])
        sheets("actualizar_capital", {"capital":s["capital"],"racha":s["racha"],"num_mg":s["num_mg"],"ts":now_ts()})
        ctx.user_data.clear()

        hora = f" · 🕐 {p['hora']}" if p.get("hora") else ""
        await u.message.reply_text(
            f"{'✅' if gano else '❌'} *Resultado registrado*\n\n"
            f"📋 {p['local']} vs {p['visitante']}\n"
            f"📅 {p.get('fecha','')}{hora}\n"
            f"⚽ Marcador: *{texto}*\n\n"
            f"{msg_res}\n\n"
            f"💰 Capital: *{fmt(s['capital'])}*",
            parse_mode="Markdown", reply_markup=menu_keyboard()
        )

    # ── Monto movimiento ──────────────────────────────────────────────────────
    elif mov_tipo and not ctx.user_data.get("mov_monto"):
        try:
            monto = int(texto.replace(".","").replace(",","")); assert monto > 0
        except:
            await u.message.reply_text("❌ Escribe solo el número ej: `500000`", parse_mode="Markdown"); return
        if mov_tipo == "egreso" and monto > s["capital"]:
            await u.message.reply_text(f"❌ El egreso ({fmt(monto)}) supera el capital ({fmt(s['capital'])})."); return
        ctx.user_data["mov_monto"] = monto
        await u.message.reply_text(
            f"{'⬆' if mov_tipo=='ingreso' else '⬇'} Monto: *{fmt(monto)}*\n\n"
            f"Escribe una descripción (o `-` para omitir):",
            parse_mode="Markdown", reply_markup=cancelar_keyboard()
        )

    # ── Descripción movimiento ────────────────────────────────────────────────
    elif mov_tipo and ctx.user_data.get("mov_monto"):
        desc  = "" if texto == "-" else texto
        monto = ctx.user_data["mov_monto"]
        now   = datetime.now()
        mov   = {
            "tipo": mov_tipo, "monto": monto, "descripcion": desc,
            "fecha": now.strftime("%Y-%m-%d"), "hora": now.strftime("%H:%M"),
            "ts": now.isoformat(),
        }
        s["capital"] += monto if mov_tipo == "ingreso" else -monto
        s.setdefault("movimientos",[]).append(mov)
        save(s)
        sheets("registrar_movimiento", {**mov, "capital_resultante": s["capital"]})
        sheets("actualizar_capital", {"capital":s["capital"],"racha":s["racha"],"num_mg":s["num_mg"],"ts":now_ts(),"evento":mov_tipo})
        ctx.user_data.clear()
        await u.message.reply_text(
            f"{'⬆' if mov_tipo=='ingreso' else '⬇'} *{mov_tipo.capitalize()} registrado*\n\n"
            f"Monto: {'+'if mov_tipo=='ingreso' else '-'}{fmt(monto)}\n"
            f"Descripción: {desc or '—'}\n"
            f"Fecha: {mov['fecha']} {mov['hora']}\n\n"
            f"💰 Capital: *{fmt(s['capital'])}*",
            parse_mode="Markdown", reply_markup=menu_keyboard()
        )

# ── FOTO ──────────────────────────────────────────────────────────────────────
async def handle_photo(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not auth(u): return
    msg   = await u.message.reply_text("🔍 Analizando imagen con IA...", reply_markup=menu_keyboard())
    photo = u.message.photo[-1]
    f     = await ctx.bot.get_file(photo.file_id)
    img   = bytes(await f.download_as_bytearray())
    partidos = analizar_imagen(img)

    if not partidos:
        await msg.edit_text("❌ No detecté partidos. Usa una captura clara de BetPlay."); return

    s = load()
    nuevos = []
    for p in partidos:
        nuevo = {
            "id": int(datetime.now().timestamp()*1000),
            "local": p.get("local",""), "visitante": p.get("visitante",""),
            "fecha": p.get("fecha", datetime.now().strftime("%Y-%m-%d")),
            "hora":  p.get("hora") or "", "liga": p.get("liga",""),
            "cuota_l": p.get("cuota_local"), "cuota_e": p.get("cuota_empate"), "cuota_v": p.get("cuota_visitante"),
            "estado": "programado", "apuesta_a": None, "cuota": None, "apuesta": None,
            "marcador": None, "ganancia": None, "gan_neta": None, "ts_registro": now_ts(),
        }
        s["partidos"].append(nuevo)
        nuevos.append(nuevo)
        sheets("registrar_partido", nuevo)

    save(s)
    base  = len(s["partidos"]) - len(nuevos)
    lines = []
    for i, p in enumerate(nuevos):
        hora  = f" · 🕐 {p['hora']}" if p.get("hora") else ""
        liga  = f" · _{p['liga']}_" if p.get("liga") else ""
        cuotas= f"\n   📊 L:{p['cuota_l']} · E:{p['cuota_e']} · V:{p['cuota_v']}" if p.get("cuota_l") else ""
        lines.append(
            f"📋 *{base+i+1}. {p['local']} vs {p['visitante']}*\n"
            f"   📅 {p['fecha']}{hora}{liga}{cuotas}"
        )

    prog = [p for p in s["partidos"] if p["estado"] == "programado"]
    await msg.edit_text(
        f"✅ *{len(nuevos)} partido(s) detectado(s)*\n\n" +
        "\n\n".join(lines) +
        f"\n\n_Toca un partido para apostar:_",
        parse_mode="Markdown",
        reply_markup=botones_inline(prog, "apostar")
    )

# ── /CONFIG ───────────────────────────────────────────────────────────────────
async def cmd_config(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not auth(u): return
    s = load()
    if not ctx.args or len(ctx.args) < 2:
        await u.message.reply_text(
            f"⚙ *Config* · % AP: {s['pct_ap']}% · Cuota ref: {s['cuota_ref']}\n\n"
            f"`/config pct 0.5` o `/config cuota 2.0`",
            parse_mode="Markdown", reply_markup=menu_keyboard()
        ); return
    campo = ctx.args[0].lower()
    try: val = float(ctx.args[1].replace(",","."))
    except: await u.message.reply_text("❌ Valor inválido."); return
    if campo == "pct":
        if not 0.1 <= val <= 10: await u.message.reply_text("❌ % entre 0.1 y 10."); return
        s["pct_ap"] = val; save(s)
        await u.message.reply_text(f"✅ % AP → *{val}%* · AM = {fmt(calc_am(s))}", parse_mode="Markdown", reply_markup=menu_keyboard())
    elif campo == "cuota":
        if val <= 1: await u.message.reply_text("❌ Cuota > 1."); return
        s["cuota_ref"] = val; save(s)
        await u.message.reply_text(f"✅ Cuota ref → *{val}*", parse_mode="Markdown", reply_markup=menu_keyboard())

# ── SETUP COMANDOS ────────────────────────────────────────────────────────────
async def post_init(app):
    await app.bot.set_my_commands([
        BotCommand("start",  "🏠 Menú principal"),
        BotCommand("config", "⚙ Configuración"),
    ])

# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("config", cmd_config))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(
        filters.TEXT & filters.Regex(
            r'^(📋 Partidos|💰 Capital|🎯 Apostar|✅ Resultado|⬆ Ingreso|⬇ Egreso|⚙ Config|📸 Analizar foto|❌ Cancelar)$'
        ), handle_menu
    ))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_texto))
    log.info("Bot iniciado ✅")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
