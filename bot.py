"""
Betting Manager Bot — Telegram
pip install python-telegram-bot anthropic requests
"""
import os, json, re, logging, base64, requests
from datetime import datetime
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters
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
    "capital": 1_000_000,
    "capital_inicial": 1_000_000,
    "pct_ap": 0.3,
    "cuota_ref": 1.833,
    "racha": 0,
    "num_mg": 0,
    "partidos": [],
    "movimientos": [],
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
        r = requests.post(GOOGLE_SHEET_URL, json={"action": action, "data": data}, timeout=10)
        return r.json()
    except Exception as e:
        log.error(f"Sheets error: {e}")
        return {"ok": False}

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
            {"type": "text",  "text": (
                "Analiza esta imagen de BetPlay u otra casa de apuestas deportivas.\n"
                "Extrae TODOS los partidos de fútbol visibles.\n"
                "Responde SOLO con JSON válido, sin texto extra ni bloques markdown.\n"
                'Formato: [{"local":"...","visitante":"...","fecha":"YYYY-MM-DD",'
                '"hora":"HH:MM","liga":"...","cuota_local":null,"cuota_empate":null,"cuota_visitante":null}]\n'
                "Si no hay fecha usa hoy. Si no hay hora usa null. "
                "Si no hay cuotas visibles usa null."
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

def resumen(s):
    mg = (f"🔴 MG n={s['num_mg']} · Pérd: {fmt(s['racha'])}"
          if s["num_mg"] > 0 else "🟢 Sin racha")
    prog = sum(1 for p in s["partidos"] if p["estado"] == "programado")
    apst = sum(1 for p in s["partidos"] if p["estado"] == "apostado")
    return (
        f"💰 *Capital:* {fmt(s['capital'])}\n"
        f"📊 *AM:* {fmt(calc_am(s))} ({s['pct_ap']}%)\n"
        f"{mg}\n"
        f"📋 Programados: {prog} | En juego: {apst}"
    )

def lista_partidos(s, estado_filter=None):
    ps = s["partidos"]
    if estado_filter: ps = [p for p in ps if p["estado"] == estado_filter]
    if not ps: return "_(ninguno)_"
    icons = {"programado":"📋","apostado":"🎯","ganado":"✅","perdido":"❌","cancelado":"🚫"}
    lines = []
    for i, p in enumerate(ps):
        ic  = icons.get(p["estado"], "•")
        ap  = f" · Apuesta: {fmt(p['apuesta'])}" if p.get("apuesta") else ""
        hor = f" {p['hora']}" if p.get("hora") else ""
        lines.append(f"{ic} *{i+1}.* {p['local']} vs {p['visitante']}\n   📅 {p['fecha']}{hor}{ap}")
    return "\n\n".join(lines)

# ── HANDLERS ──────────────────────────────────────────────────────────────────

async def cmd_start(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not auth(u): return
    s = load()
    await u.message.reply_text(
        f"⚽ *Betting Manager Bot*\n\n{resumen(s)}\n\n"
        f"*Comandos:*\n"
        f"📸 Foto → extrae partidos\n"
        f"`/partidos` — ver todos\n"
        f"`/apostar <#> <equipo> <cuota>` — registrar apuesta\n"
        f"`/resultado <#> <goles_l>-<goles_v> <ganado|perdido>`\n"
        f"`/capital` — estado financiero\n"
        f"`/movimiento <ingreso|egreso> <monto> [desc]`\n"
        f"`/config pct <valor>` — cambiar % AP\n"
        f"`/cancelar <#>` — cancelar partido",
        parse_mode="Markdown"
    )

async def cmd_capital(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not auth(u): return
    s = load()
    jug  = [p for p in s["partidos"] if p["estado"] in ("ganado","perdido")]
    gan  = [p for p in jug if p["estado"] == "ganado"]
    neto = sum(p.get("gan_neta",0) for p in gan)
    var  = s["capital"] - s["capital_inicial"]
    movs = s.get("movimientos", [])
    bal  = sum(m["monto"] if m["tipo"]=="ingreso" else -m["monto"] for m in movs)
    await u.message.reply_text(
        f"💰 *Estado del Capital*\n\n"
        f"Actual:   {fmt(s['capital'])}\n"
        f"Inicial:  {fmt(s['capital_inicial'])}\n"
        f"Variación: {fsign(var)}\n"
        f"Balance movimientos: {fsign(bal)}\n\n"
        f"Jugados: {len(jug)} | Ganados: {len(gan)} | Perdidos: {len(jug)-len(gan)}\n"
        f"Ganancia neta total: {fsign(neto)}\n\n"
        f"{'🔴 MG n='+str(s['num_mg'])+' · Pérd: '+fmt(s['racha']) if s['num_mg']>0 else '🟢 Sin racha'}",
        parse_mode="Markdown"
    )

async def cmd_partidos(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not auth(u): return
    s = load()
    await u.message.reply_text(
        f"📋 *Partidos*\n\n{lista_partidos(s)}",
        parse_mode="Markdown"
    )

async def cmd_apostar(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Uso: /apostar 2 Nacional 1.85"""
    if not auth(u): return
    s = load()
    if not ctx.args or len(ctx.args) < 3:
        await u.message.reply_text(
            "Uso: `/apostar <número> <equipo> <cuota>`\n"
            "Ejemplo: `/apostar 1 Nacional 1.85`\n\n"
            f"*Partidos programados:*\n{lista_partidos(s,'programado')}",
            parse_mode="Markdown"
        ); return

    try:
        idx    = int(ctx.args[0]) - 1
        equipo = " ".join(ctx.args[1:-1])
        cuota  = float(ctx.args[-1].replace(",","."))
        assert cuota > 1
    except:
        await u.message.reply_text("❌ Formato: `/apostar 1 Nacional 1.85`", parse_mode="Markdown"); return

    prog = [p for p in s["partidos"] if p["estado"] == "programado"]
    if idx < 0 or idx >= len(prog):
        await u.message.reply_text(f"❌ Número inválido. Hay {len(prog)} partidos programados."); return

    p = prog[idx]
    opciones = [p["local"].lower(), p["visitante"].lower(), "empate"]
    if equipo.lower() not in opciones:
        await u.message.reply_text(
            f"❌ Equipo no reconocido.\n"
            f"Opciones: `{p['local']}`, `{p['visitante']}`, `Empate`",
            parse_mode="Markdown"
        ); return

    ap, AM, gan_neta = calc_apuesta(s, cuota)
    real_idx = s["partidos"].index(p)
    s["partidos"][real_idx].update({
        "apuesta_a": equipo, "cuota": cuota, "apuesta": ap, "AM": AM,
        "gan_neta_esp": gan_neta,
        "tipo_apuesta": "Normal" if s["num_mg"]==0 else f"MG n={s['num_mg']}",
        "num_mg": s["num_mg"],
        "perdida_acum_al_apostar": s["racha"],
        "estado": "apostado",
        "ts_apuesta": now_ts(),
    })
    save(s)
    sheets("actualizar_partido", s["partidos"][real_idx])

    tipo = "Normal" if s["num_mg"]==0 else f"🔴 MG n={s['num_mg']} (×{1+s['num_mg']*0.5})"
    await u.message.reply_text(
        f"🎯 *Apuesta registrada*\n\n"
        f"Partido: {p['local']} vs {p['visitante']}\n"
        f"A favor: *{equipo}* · Cuota: {cuota}\n"
        f"Tipo: {tipo}\n\n"
        f"💵 *Apostar: {fmt(ap)}*\n"
        f"Si ganas: {fsign(gan_neta)} neto",
        parse_mode="Markdown"
    )

async def cmd_resultado(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Uso: /resultado 1 2-1 ganado"""
    if not auth(u): return
    s = load()
    if not ctx.args or len(ctx.args) < 3:
        await u.message.reply_text(
            "Uso: `/resultado <número> <marcador> <ganado|perdido>`\n"
            "Ejemplo: `/resultado 1 2-1 ganado`\n\n"
            f"*En juego:*\n{lista_partidos(s,'apostado')}",
            parse_mode="Markdown"
        ); return
    try:
        idx      = int(ctx.args[0]) - 1
        marcador = ctx.args[1]
        resultado = ctx.args[2].lower()
        assert resultado in ("ganado","perdido")
    except:
        await u.message.reply_text("❌ Formato: `/resultado 1 2-1 ganado`", parse_mode="Markdown"); return

    apost = [p for p in s["partidos"] if p["estado"] == "apostado"]
    if idx < 0 or idx >= len(apost):
        await u.message.reply_text(f"❌ Número inválido. Hay {len(apost)} partidos en juego."); return

    p      = apost[idx]
    ri     = s["partidos"].index(p)
    gano   = resultado == "ganado"
    ap     = p["apuesta"]
    cuota  = p["cuota"]
    pa     = p.get("perdida_acum_al_apostar", 0)
    ganancia  = round(ap * (cuota - 1)) if gano else 0
    gan_neta  = ganancia - pa

    s["partidos"][ri].update({
        "estado": "ganado" if gano else "perdido",
        "marcador": marcador, "ganancia": ganancia,
        "gan_neta": gan_neta, "ts_resultado": now_ts(),
    })

    if gano:
        s["capital"] += ganancia
        s["racha"]    = 0
        s["num_mg"]   = 0
        msg = f"🎉 ¡Ganaste!\nBruto: {fmt(ganancia)} · *Neto: {fsign(gan_neta)}*"
    else:
        s["capital"] -= ap
        s["racha"]   += ap
        s["num_mg"]  += 1
        ap_sig, _, gn_sig = calc_apuesta(s, cuota)
        msg = (f"😞 Perdiste {fmt(ap)}\n"
               f"Pérd. acumulada: {fmt(s['racha'])}\n"
               f"🔴 MG n={s['num_mg']} → próxima apuesta: *{fmt(ap_sig)}*")

    save(s)
    sheets("actualizar_partido", s["partidos"][ri])
    sheets("actualizar_capital", {
        "capital": s["capital"], "racha": s["racha"],
        "num_mg": s["num_mg"], "ts": now_ts()
    })

    await u.message.reply_text(
        f"{'✅' if gano else '❌'} *Resultado registrado*\n\n"
        f"{p['local']} vs {p['visitante']} → {marcador}\n\n"
        f"{msg}\n\n💰 Capital: *{fmt(s['capital'])}*",
        parse_mode="Markdown"
    )

async def cmd_movimiento(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Uso: /movimiento ingreso 500000 Recarga  |  /movimiento egreso 200000 Retiro"""
    if not auth(u): return
    s = load()
    if not ctx.args or len(ctx.args) < 2:
        await u.message.reply_text(
            "Uso: `/movimiento <ingreso|egreso> <monto> [descripción]`\n"
            "Ejemplo: `/movimiento ingreso 500000 Recarga mensual`",
            parse_mode="Markdown"
        ); return
    tipo = ctx.args[0].lower()
    if tipo not in ("ingreso","egreso"):
        await u.message.reply_text("❌ Tipo debe ser `ingreso` o `egreso`.", parse_mode="Markdown"); return
    try:
        monto = int(ctx.args[1].replace(".","").replace(",",""))
        assert monto > 0
    except:
        await u.message.reply_text("❌ Monto inválido."); return

    desc = " ".join(ctx.args[2:]) if len(ctx.args) > 2 else tipo.capitalize()
    if tipo == "egreso" and monto > s["capital"]:
        await u.message.reply_text(f"❌ El egreso ({fmt(monto)}) supera el capital ({fmt(s['capital'])})."); return

    now = datetime.now()
    mov = {
        "tipo": tipo, "monto": monto, "descripcion": desc,
        "fecha": now.strftime("%Y-%m-%d"),
        "hora":  now.strftime("%H:%M"),
        "ts":    now.isoformat(),
    }
    s["capital"] += monto if tipo == "ingreso" else -monto
    s.setdefault("movimientos", []).append(mov)
    save(s)
    sheets("registrar_movimiento", {**mov, "capital_resultante": s["capital"]})

    await u.message.reply_text(
        f"{'⬆' if tipo=='ingreso' else '⬇'} *{tipo.capitalize()} registrado*\n\n"
        f"Monto: {'+'  if tipo=='ingreso' else '-'}{fmt(monto)}\n"
        f"Descripción: {desc}\n"
        f"Fecha: {mov['fecha']} {mov['hora']}\n\n"
        f"💰 Capital actual: *{fmt(s['capital'])}*",
        parse_mode="Markdown"
    )

async def cmd_config(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Uso: /config pct 0.5"""
    if not auth(u): return
    s = load()
    if not ctx.args or len(ctx.args) < 2:
        await u.message.reply_text(
            f"Config actual:\n"
            f"• % AP: {s['pct_ap']}% → AM: {fmt(calc_am(s))}\n"
            f"• Cuota ref: {s['cuota_ref']}\n\n"
            f"Cambiar: `/config pct 0.5` o `/config cuota 2.0`",
            parse_mode="Markdown"
        ); return
    campo = ctx.args[0].lower()
    try:
        val = float(ctx.args[1].replace(",","."))
    except:
        await u.message.reply_text("❌ Valor inválido."); return
    if campo == "pct":
        if not 0.1 <= val <= 10:
            await u.message.reply_text("❌ % debe estar entre 0.1 y 10."); return
        s["pct_ap"] = val
        save(s)
        await u.message.reply_text(f"✅ % AP → *{val}%* | AM = {fmt(calc_am(s))}", parse_mode="Markdown")
    elif campo == "cuota":
        if val <= 1:
            await u.message.reply_text("❌ Cuota debe ser mayor a 1."); return
        s["cuota_ref"] = val
        save(s)
        await u.message.reply_text(f"✅ Cuota ref → *{val}*", parse_mode="Markdown")
    else:
        await u.message.reply_text("❌ Campo desconocido. Usa `pct` o `cuota`.", parse_mode="Markdown")

async def cmd_cancelar(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Uso: /cancelar 2"""
    if not auth(u): return
    s = load()
    if not ctx.args:
        await u.message.reply_text("Uso: `/cancelar <número>`", parse_mode="Markdown"); return
    try: idx = int(ctx.args[0]) - 1
    except: await u.message.reply_text("❌ Número inválido."); return
    activos = [p for p in s["partidos"] if p["estado"] in ("programado","apostado")]
    if idx < 0 or idx >= len(activos):
        await u.message.reply_text(f"❌ Número inválido. Hay {len(activos)} partidos activos."); return
    p  = activos[idx]
    ri = s["partidos"].index(p)
    s["partidos"][ri]["estado"] = "cancelado"
    save(s)
    await u.message.reply_text(f"🚫 Cancelado: {p['local']} vs {p['visitante']}")

async def handle_photo(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not auth(u): return
    msg = await u.message.reply_text("🔍 Analizando imagen...")
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
            "id":         int(datetime.now().timestamp()*1000),
            "local":      p.get("local",""),
            "visitante":  p.get("visitante",""),
            "fecha":      p.get("fecha", datetime.now().strftime("%Y-%m-%d")),
            "hora":       p.get("hora",""),
            "liga":       p.get("liga",""),
            "cuota_l":    p.get("cuota_local"),
            "cuota_e":    p.get("cuota_empate"),
            "cuota_v":    p.get("cuota_visitante"),
            "estado":     "programado",
            "apuesta_a":  None, "cuota": None, "apuesta": None,
            "marcador":   None, "ganancia": None, "gan_neta": None,
            "ts_registro": now_ts(),
        }
        s["partidos"].append(nuevo)
        nuevos.append(nuevo)
        sheets("registrar_partido", nuevo)

    save(s)
    total = len(s["partidos"])
    base  = total - len(nuevos)
    lines = []
    for i, p in enumerate(nuevos):
        cuotas = ""
        if p.get("cuota_l"):
            cuotas = f"\n   Cuotas: L {p['cuota_l']} · E {p['cuota_e']} · V {p['cuota_v']}"
        hora  = f" {p['hora']}" if p.get("hora") else ""
        lines.append(f"*{base+i+1}.* {p['local']} vs {p['visitante']}\n   📅 {p['fecha']}{hora} · {p.get('liga','')}{cuotas}")

    await msg.edit_text(
        f"✅ *{len(nuevos)} partido(s) detectado(s)*\n\n" +
        "\n\n".join(lines) +
        f"\n\nPara apostar:\n`/apostar <número> <equipo> <cuota>`",
        parse_mode="Markdown"
    )

# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",       cmd_start))
    app.add_handler(CommandHandler("capital",     cmd_capital))
    app.add_handler(CommandHandler("partidos",    cmd_partidos))
    app.add_handler(CommandHandler("apostar",     cmd_apostar))
    app.add_handler(CommandHandler("resultado",   cmd_resultado))
    app.add_handler(CommandHandler("movimiento",  cmd_movimiento))
    app.add_handler(CommandHandler("config",      cmd_config))
    app.add_handler(CommandHandler("cancelar",    cmd_cancelar))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    log.info("Bot iniciado ✅")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
