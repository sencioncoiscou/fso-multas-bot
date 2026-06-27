import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

# ============================================================
# FSO MULTAS BOT
# ============================================================
# IMPORTANTE:
# - No pongas el token aquí.
# - El token debe estar como variable de entorno: DISCORD_TOKEN
# - En Koyeb/Replit: Environment Variable / Secret -> DISCORD_TOKEN
# ============================================================

TOKEN = os.getenv("DISCORD_TOKEN")
DB_PATH = "multas.db"

# Fallbacks del servidor de prueba original.
# En cada servidor nuevo usa /multa configurar_roles y /multa configurar_canales.
DEFAULT_CANAL_MULTAS = 1519131680898678784
DEFAULT_CANAL_ECONOMIA = 1518803509603205213
DEFAULT_CANAL_STAFF = 1519528124570669207
DEFAULT_ROL_POLICIA = 0
DEFAULT_ROL_STAFF = 1519528205311283211
DEFAULT_ROL_SANCIONADO = 1519528305995546684

PLAZO_DIAS = 3

intents = discord.Intents.default()
intents.members = True
intents.message_content = False

bot = commands.Bot(command_prefix="!", intents=intents)
multa = app_commands.Group(name="multa", description="Sistema oficial de multas FSO")


# ============================================================
# UTILIDADES DE TIEMPO Y DB
# ============================================================

def ahora() -> datetime:
    return datetime.now(timezone.utc)


def conectar_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def agregar_columna_si_no_existe(conn, tabla: str, columna: str, definicion: str):
    cols = [row[1] for row in conn.execute(f"PRAGMA table_info({tabla})").fetchall()]
    if columna not in cols:
        conn.execute(f"ALTER TABLE {tabla} ADD COLUMN {columna} {definicion}")


def iniciar_db():
    with conectar_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS guild_config (
                guild_id INTEGER PRIMARY KEY,
                rol_policia_id INTEGER DEFAULT 0,
                rol_staff_id INTEGER DEFAULT 0,
                rol_sancionado_id INTEGER DEFAULT 0,
                canal_multas_id INTEGER DEFAULT 0,
                canal_economia_id INTEGER DEFAULT 0,
                canal_staff_id INTEGER DEFAULT 0
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS multas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                usuario_id INTEGER NOT NULL,
                oficial_id INTEGER NOT NULL,
                monto INTEGER NOT NULL DEFAULT 0,
                razon TEXT NOT NULL,
                tipo TEXT NOT NULL DEFAULT 'Presencial',
                prueba_url TEXT,
                created_at TEXT NOT NULL,
                due_at TEXT,
                status TEXT NOT NULL DEFAULT 'pendiente',
                advertencias INTEGER NOT NULL DEFAULT 0,
                pago_confirmado_por INTEGER,
                pago_confirmado_at TEXT
            )
            """
        )

        # Migraciones por si vienes de una versión vieja.
        agregar_columna_si_no_existe(conn, "multas", "oficial_id", "INTEGER DEFAULT 0")
        agregar_columna_si_no_existe(conn, "multas", "monto", "INTEGER DEFAULT 0")
        agregar_columna_si_no_existe(conn, "multas", "razon", "TEXT DEFAULT ''")
        agregar_columna_si_no_existe(conn, "multas", "tipo", "TEXT DEFAULT 'Presencial'")
        agregar_columna_si_no_existe(conn, "multas", "prueba_url", "TEXT")
        agregar_columna_si_no_existe(conn, "multas", "due_at", "TEXT")
        agregar_columna_si_no_existe(conn, "multas", "status", "TEXT DEFAULT 'pendiente'")
        agregar_columna_si_no_existe(conn, "multas", "advertencias", "INTEGER DEFAULT 0")
        agregar_columna_si_no_existe(conn, "multas", "pago_confirmado_por", "INTEGER")
        agregar_columna_si_no_existe(conn, "multas", "pago_confirmado_at", "TEXT")

        conn.commit()


def get_config(guild_id: int):
    with conectar_db() as conn:
        row = conn.execute("SELECT * FROM guild_config WHERE guild_id = ?", (guild_id,)).fetchone()
        if row:
            return dict(row)

    return {
        "guild_id": guild_id,
        "rol_policia_id": DEFAULT_ROL_POLICIA,
        "rol_staff_id": DEFAULT_ROL_STAFF,
        "rol_sancionado_id": DEFAULT_ROL_SANCIONADO,
        "canal_multas_id": DEFAULT_CANAL_MULTAS,
        "canal_economia_id": DEFAULT_CANAL_ECONOMIA,
        "canal_staff_id": DEFAULT_CANAL_STAFF,
    }


def guardar_config(guild_id: int, **kwargs):
    config = get_config(guild_id)
    config.update(kwargs)
    with conectar_db() as conn:
        conn.execute(
            """
            INSERT INTO guild_config
            (guild_id, rol_policia_id, rol_staff_id, rol_sancionado_id, canal_multas_id, canal_economia_id, canal_staff_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                rol_policia_id = excluded.rol_policia_id,
                rol_staff_id = excluded.rol_staff_id,
                rol_sancionado_id = excluded.rol_sancionado_id,
                canal_multas_id = excluded.canal_multas_id,
                canal_economia_id = excluded.canal_economia_id,
                canal_staff_id = excluded.canal_staff_id
            """,
            (
                guild_id,
                int(config.get("rol_policia_id") or 0),
                int(config.get("rol_staff_id") or 0),
                int(config.get("rol_sancionado_id") or 0),
                int(config.get("canal_multas_id") or 0),
                int(config.get("canal_economia_id") or 0),
                int(config.get("canal_staff_id") or 0),
            ),
        )
        conn.commit()


def buscar_multa(multa_id: int):
    with conectar_db() as conn:
        return conn.execute("SELECT * FROM multas WHERE id = ?", (multa_id,)).fetchone()


def texto_estado(estado: str) -> str:
    estados = {
        "pendiente": "🟡 Pendiente",
        "pagada": "🟢 Pagada",
        "sancionada": "🔴 Sancionada",
        "cancelada": "⚫ Cancelada",
        "warn": "⚠️ Warn",
    }
    return estados.get((estado or "").lower(), estado)


def normalizar_tipo(tipo: str) -> str:
    tipo = (tipo or "Presencial").strip().lower()
    if tipo == "digital":
        return "Digital"
    if tipo == "warn":
        return "Warn"
    return "Presencial"


def es_pagable(tipo: str) -> bool:
    return normalizar_tipo(tipo) in ("Presencial", "Digital")


# ============================================================
# PERMISOS
# ============================================================

def tiene_rol(member: discord.Member, role_id: int) -> bool:
    if not role_id:
        return False
    return any(role.id == role_id for role in getattr(member, "roles", []))


def es_admin(member: discord.Member) -> bool:
    return bool(getattr(member, "guild_permissions", None) and member.guild_permissions.administrator)


def es_staff(member: discord.Member) -> bool:
    if es_admin(member):
        return True
    if not member.guild:
        return False
    cfg = get_config(member.guild.id)
    return tiene_rol(member, int(cfg.get("rol_staff_id") or 0))


def es_policia(member: discord.Member) -> bool:
    if es_staff(member):
        return True
    if not member.guild:
        return False
    cfg = get_config(member.guild.id)
    return tiene_rol(member, int(cfg.get("rol_policia_id") or 0))


def puede_configurar(member: discord.Member) -> bool:
    return es_staff(member) or es_admin(member)


async def negar(interaction: discord.Interaction, mensaje: str = "❌ No tienes permiso para usar este comando."):
    if interaction.response.is_done():
        await interaction.followup.send(mensaje, ephemeral=True)
    else:
        await interaction.response.send_message(mensaje, ephemeral=True)


async def obtener_canal(guild: discord.Guild, channel_id: int):
    if not channel_id:
        return None
    ch = guild.get_channel(channel_id)
    if ch:
        return ch
    try:
        fetched = await bot.fetch_channel(channel_id)
        return fetched
    except Exception:
        return None


async def canales_configurados(interaction: discord.Interaction):
    cfg = get_config(interaction.guild.id)
    multas_ch = await obtener_canal(interaction.guild, int(cfg.get("canal_multas_id") or 0))
    economia_ch = await obtener_canal(interaction.guild, int(cfg.get("canal_economia_id") or 0))
    staff_ch = await obtener_canal(interaction.guild, int(cfg.get("canal_staff_id") or 0))

    if not multas_ch or not economia_ch or not staff_ch:
        await interaction.followup.send(
            "❌ Este servidor todavía no tiene los canales configurados. Usa `/multa configurar_canales`.",
            ephemeral=True,
        )
        return None, None, None

    return multas_ch, economia_ch, staff_ch


# ============================================================
# EMBEDS
# ============================================================

def embed_multa_creada(multa_id: int, tipo: str, usuario: discord.Member, oficial: discord.Member, monto: int, razon: str, limite: datetime, economia_id: int, prueba_url: Optional[str] = None):
    embed = discord.Embed(
        title="🚔 REGISTRO DE MULTA",
        description=(
            "Se ha generado una multa oficial al ciudadano mencionado.\n"
            "Revise la información correspondiente y proceda con el pago dentro del plazo establecido."
        ),
        color=discord.Color.red() if tipo == "Presencial" else discord.Color.blue(),
        timestamp=ahora(),
    )
    embed.add_field(name="📌 TIPO DE MULTA", value=f"**{tipo}**", inline=False)
    embed.add_field(name="👤 DATOS", value=f"**Ciudadano:** {usuario.mention}\n**ID:** `{usuario.id}`", inline=False)
    embed.add_field(name="🚔 QUIEN LA PONE", value=f"**Oficial responsable:** {oficial.mention}\n**ID del oficial:** `{oficial.id}`", inline=False)
    embed.add_field(
        name="📋 DETALLES DE LA MULTA",
        value=f"**Monto total:** R$ {monto:,}\n**Motivo:** {razon}\n**ID de multa:** `#{multa_id}`".replace(",", ","),
        inline=False,
    )
    embed.add_field(
        name="⏰ INFORMACIÓN IMPORTANTE",
        value=(
            f"El ciudadano dispone de un plazo máximo de **{PLAZO_DIAS} días** para pagar la multa.\n"
            "Si no realiza el pago dentro del plazo establecido, recibirá advertencias y luego será sancionado.\n"
            f"**Fecha límite:** <t:{int(limite.timestamp())}:F>"
        ),
        inline=False,
    )
    embed.add_field(name="💳 PAGO", value=f"Debe pagar esta multa en el canal de economía: <#{economia_id}>\nBanco de Florida State.", inline=False)
    if prueba_url:
        embed.add_field(name="📎 PRUEBA ADJUNTA", value=f"[Ver prueba]({prueba_url})", inline=False)
    embed.set_footer(text="FSO Multas • Sistema oficial de sanciones")
    return embed


def embed_warn(multa_id: int, usuario: discord.Member, oficial: discord.Member, razon: str, prueba_url: Optional[str] = None):
    embed = discord.Embed(
        title="⚠️ ADVERTENCIA REGISTRADA",
        description=(
            "Se ha registrado una advertencia oficial al ciudadano mencionado. "
            "Esta advertencia no requiere pago, pero queda guardada en el historial."
        ),
        color=discord.Color.gold(),
        timestamp=ahora(),
    )
    embed.add_field(name="👤 CIUDADANO", value=f"**Usuario:** {usuario.mention}\n**ID:** `{usuario.id}`", inline=False)
    embed.add_field(name="🚔 OFICIAL RESPONSABLE", value=f"**Oficial:** {oficial.mention}\n**ID del oficial:** `{oficial.id}`", inline=False)
    embed.add_field(name="📋 DETALLES DE LA ADVERTENCIA", value=f"**Motivo:** {razon}\n**Estado:** ⚠️ WARN\n**ID de registro:** `#{multa_id}`", inline=False)
    if prueba_url:
        embed.add_field(name="📎 PRUEBA ADJUNTA", value=f"[Ver prueba]({prueba_url})", inline=False)
    embed.set_footer(text="FSO Multas • Advertencia oficial")
    return embed


def embed_pago(row: sqlite3.Row, usuario: discord.Member, oficial: discord.Member):
    embed = discord.Embed(
        title="✅ MULTA PAGADA",
        description="Se ha confirmado el pago de una multa registrada en el sistema.",
        color=discord.Color.green(),
        timestamp=ahora(),
    )
    embed.add_field(name="👤 CIUDADANO", value=f"**Usuario:** {usuario.mention}\n**ID:** `{usuario.id}`", inline=False)
    embed.add_field(name="🚔 OFICIAL QUE CONFIRMA", value=f"**Oficial:** {oficial.mention}\n**ID:** `{oficial.id}`", inline=False)
    embed.add_field(name="📋 DETALLES DEL PAGO", value=f"**ID de multa:** `#{row['id']}`\n**Monto pagado:** R$ {int(row['monto']):,}\n**Estado:** 🟢 PAGADA".replace(",", ","), inline=False)
    embed.add_field(name="🏦 PAGO", value="El pago fue confirmado en el Banco de Florida State.", inline=False)
    embed.set_footer(text="FSO Multas • Pago confirmado")
    return embed


# ============================================================
# COMANDOS DE CONFIGURACIÓN
# ============================================================

@multa.command(name="configurar_roles", description="Configurar roles de Policía, Staff y Sancionado")
@app_commands.describe(policia="Rol que podrá crear/listar/eliminar/pagar multas", staff="Rol que podrá usar todo", sancionado="Rol que se aplica al no pagar")
async def configurar_roles(interaction: discord.Interaction, policia: discord.Role, staff: discord.Role, sancionado: discord.Role):
    await interaction.response.defer(ephemeral=True)
    if not puede_configurar(interaction.user):
        await interaction.followup.send("❌ No tienes permiso para configurar el sistema de multas.", ephemeral=True)
        return

    guardar_config(
        interaction.guild.id,
        rol_policia_id=policia.id,
        rol_staff_id=staff.id,
        rol_sancionado_id=sancionado.id,
    )
    await interaction.followup.send(
        f"✅ Roles configurados correctamente.\n**Policía:** {policia.mention}\n**Staff:** {staff.mention}\n**Sancionado:** {sancionado.mention}",
        ephemeral=True,
    )


@multa.command(name="configurar_canales", description="Configurar canales de multas, economía y logs de staff")
@app_commands.describe(multas="Canal donde se publican multas", economia="Canal donde se pagan multas", staff="Canal privado de logs/avisos de staff")
async def configurar_canales(interaction: discord.Interaction, multas: discord.TextChannel, economia: discord.TextChannel, staff: discord.TextChannel):
    await interaction.response.defer(ephemeral=True)
    if not puede_configurar(interaction.user):
        await interaction.followup.send("❌ No tienes permiso para configurar el sistema de multas.", ephemeral=True)
        return

    guardar_config(
        interaction.guild.id,
        canal_multas_id=multas.id,
        canal_economia_id=economia.id,
        canal_staff_id=staff.id,
    )
    await interaction.followup.send(
        f"✅ Canales configurados correctamente.\n**Multas:** {multas.mention}\n**Economía:** {economia.mention}\n**Staff/logs:** {staff.mention}",
        ephemeral=True,
    )


# ============================================================
# COMANDOS PRINCIPALES
# ============================================================

@multa.command(name="crear", description="Crear una multa presencial, digital o warn")
@app_commands.describe(
    tipo="Tipo de registro",
    usuario="Ciudadano al que se le registra la multa o advertencia",
    motivo="Motivo de la multa o advertencia",
    valor="Monto de la multa. Para Warn puede dejarse en 0",
    prueba="Prueba opcional: imagen o video",
)
@app_commands.choices(tipo=[
    app_commands.Choice(name="Presencial", value="Presencial"),
    app_commands.Choice(name="Digital", value="Digital"),
    app_commands.Choice(name="Warn", value="Warn"),
])
async def crear(
    interaction: discord.Interaction,
    tipo: app_commands.Choice[str],
    usuario: discord.Member,
    motivo: str,
    valor: int = 0,
    prueba: Optional[discord.Attachment] = None,
):
    await interaction.response.defer(ephemeral=True)

    if not es_policia(interaction.user):
        await interaction.followup.send("❌ No tienes permiso para usar este comando.", ephemeral=True)
        return

    tipo_nombre = normalizar_tipo(tipo.value)
    multas_ch, economia_ch, staff_ch = await canales_configurados(interaction)
    if not multas_ch:
        return

    if es_pagable(tipo_nombre) and valor <= 0:
        await interaction.followup.send("❌ Para multas Presencial o Digital debes poner un valor mayor a 0.", ephemeral=True)
        return

    fecha = ahora()
    limite = fecha + timedelta(days=PLAZO_DIAS) if es_pagable(tipo_nombre) else None
    status = "pendiente" if es_pagable(tipo_nombre) else "warn"
    prueba_url = prueba.url if prueba else None
    monto = int(valor) if es_pagable(tipo_nombre) else 0

    with conectar_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO multas
            (guild_id, usuario_id, oficial_id, monto, razon, tipo, prueba_url, created_at, due_at, status, advertencias)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                interaction.guild.id,
                usuario.id,
                interaction.user.id,
                monto,
                motivo,
                tipo_nombre,
                prueba_url,
                fecha.isoformat(),
                limite.isoformat() if limite else None,
                status,
            ),
        )
        multa_id = cur.lastrowid
        conn.commit()

    if tipo_nombre == "Warn":
        embed = embed_warn(multa_id, usuario, interaction.user, motivo, prueba_url)
        await multas_ch.send(content=f"⚠️ {usuario.mention}, recibiste una advertencia oficial.", embed=embed)
        await staff_ch.send(f"⚠️ WARN registrado: `#{multa_id}` para {usuario.mention} por {interaction.user.mention}.")
        await interaction.followup.send(f"✅ Advertencia `#{multa_id}` registrada correctamente.", ephemeral=True)
        return

    embed = embed_multa_creada(multa_id, tipo_nombre, usuario, interaction.user, monto, motivo, limite, economia_ch.id, prueba_url)
    await multas_ch.send(content=f"{usuario.mention}, tienes una nueva multa pendiente.", embed=embed)
    await staff_ch.send(f"📌 Nueva multa `#{multa_id}` registrada para {usuario.mention}. Tipo: **{tipo_nombre}** | Monto: **R$ {monto:,}**".replace(",", ","))
    await interaction.followup.send(f"✅ Multa `#{multa_id}` creada correctamente.", ephemeral=True)


@multa.command(name="lista", description="Ver multas pendientes")
@app_commands.describe(usuario="Opcional: filtrar por usuario")
async def lista(interaction: discord.Interaction, usuario: Optional[discord.Member] = None):
    await interaction.response.defer(ephemeral=True)
    if not es_policia(interaction.user):
        await interaction.followup.send("❌ No tienes permiso para usar este comando.", ephemeral=True)
        return

    params = [interaction.guild.id]
    filtro_usuario = ""
    if usuario:
        filtro_usuario = "AND usuario_id = ?"
        params.append(usuario.id)

    with conectar_db() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM multas
            WHERE guild_id = ?
              AND status = 'pendiente'
              AND tipo IN ('Presencial', 'Digital')
              {filtro_usuario}
            ORDER BY due_at ASC
            LIMIT 15
            """,
            tuple(params),
        ).fetchall()

    if not rows:
        await interaction.followup.send("✅ No hay multas pendientes.", ephemeral=True)
        return

    embed = discord.Embed(title="📋 MULTAS PENDIENTES", color=discord.Color.orange(), timestamp=ahora())
    for row in rows:
        limite = datetime.fromisoformat(row["due_at"]) if row["due_at"] else None
        valor = (
            f"**Usuario:** <@{row['usuario_id']}>\n"
            f"**Tipo:** {row['tipo']}\n"
            f"**Monto:** R$ {int(row['monto']):,}\n"
            f"**Motivo:** {row['razon']}\n"
            f"**Estado:** {texto_estado(row['status'])}\n"
            f"**Vence:** <t:{int(limite.timestamp())}:R>" if limite else "Sin fecha límite"
        ).replace(",", ",")
        embed.add_field(name=f"Multa #{row['id']}", value=valor, inline=False)

    await interaction.followup.send(embed=embed, ephemeral=True)


@multa.command(name="historial", description="Ver historial completo de multas y warns de un usuario")
@app_commands.describe(usuario="Usuario a consultar")
async def historial(interaction: discord.Interaction, usuario: discord.Member):
    await interaction.response.defer(ephemeral=True)
    if not es_policia(interaction.user):
        await interaction.followup.send("❌ No tienes permiso para usar este comando.", ephemeral=True)
        return

    with conectar_db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM multas
            WHERE guild_id = ? AND usuario_id = ?
            ORDER BY id DESC
            LIMIT 20
            """,
            (interaction.guild.id, usuario.id),
        ).fetchall()

    if not rows:
        await interaction.followup.send("📋 Este usuario no tiene historial de multas.", ephemeral=True)
        return

    embed = discord.Embed(title=f"📋 HISTORIAL DE {usuario.display_name}", color=discord.Color.blurple(), timestamp=ahora())
    for row in rows:
        creado = datetime.fromisoformat(row["created_at"]) if row["created_at"] else None
        limite_txt = "No aplica"
        if row["due_at"]:
            limite = datetime.fromisoformat(row["due_at"])
            limite_txt = f"<t:{int(limite.timestamp())}:F>"

        valor = (
            f"**Tipo:** {row['tipo']}\n"
            f"**Monto:** R$ {int(row['monto']):,}\n"
            f"**Motivo:** {row['razon']}\n"
            f"**Estado:** {texto_estado(row['status'])}\n"
            f"**Oficial:** <@{row['oficial_id']}>\n"
            f"**Creada:** <t:{int(creado.timestamp())}:F>\n" if creado else ""
        ).replace(",", ",")
        valor += f"**Fecha límite:** {limite_txt}\n"
        if row["prueba_url"]:
            valor += f"**Prueba:** [Ver prueba]({row['prueba_url']})\n"
        if row["pago_confirmado_por"]:
            valor += f"**Pago confirmado por:** <@{row['pago_confirmado_por']}>\n"

        embed.add_field(name=f"Registro #{row['id']}", value=valor[:1024], inline=False)

    await interaction.followup.send(embed=embed, ephemeral=True)


@multa.command(name="pagar", description="Marcar una multa como pagada")
@app_commands.describe(id="ID de la multa")
async def pagar(interaction: discord.Interaction, id: int):
    await interaction.response.defer(ephemeral=True)
    if not es_policia(interaction.user):
        await interaction.followup.send("❌ No tienes permiso para marcar multas como pagadas.", ephemeral=True)
        return

    row = buscar_multa(id)
    if not row or row["guild_id"] != interaction.guild.id:
        await interaction.followup.send("❌ No encontré esa multa.", ephemeral=True)
        return

    if row["status"] != "pendiente":
        await interaction.followup.send("❌ Esa multa no está pendiente o no requiere pago.", ephemeral=True)
        return

    with conectar_db() as conn:
        conn.execute(
            """
            UPDATE multas
            SET status = 'pagada', pago_confirmado_por = ?, pago_confirmado_at = ?
            WHERE id = ?
            """,
            (interaction.user.id, ahora().isoformat(), id),
        )
        conn.commit()

    cfg = get_config(interaction.guild.id)
    multas_ch, _economia_ch, staff_ch = await canales_configurados(interaction)
    if not multas_ch:
        return

    try:
        usuario = interaction.guild.get_member(row["usuario_id"]) or await interaction.guild.fetch_member(row["usuario_id"])
    except Exception:
        usuario = None

    if usuario:
        rol_sancionado = interaction.guild.get_role(int(cfg.get("rol_sancionado_id") or 0))
        if rol_sancionado and rol_sancionado in usuario.roles:
            try:
                await usuario.remove_roles(rol_sancionado, reason="Multa pagada")
            except Exception:
                pass

        await multas_ch.send(embed=embed_pago(row, usuario, interaction.user))
    else:
        await multas_ch.send(f"✅ La multa `#{id}` fue marcada como pagada.")

    await staff_ch.send(f"✅ {interaction.user.mention} marcó como pagada la multa `#{id}`.")
    await interaction.followup.send(f"✅ Multa `#{id}` marcada como pagada.", ephemeral=True)


@multa.command(name="eliminar", description="Eliminar/cancelar una multa por ID")
@app_commands.describe(id="ID de la multa")
async def eliminar(interaction: discord.Interaction, id: int):
    await interaction.response.defer(ephemeral=True)
    if not es_policia(interaction.user):
        await interaction.followup.send("❌ No tienes permiso para usar este comando.", ephemeral=True)
        return

    row = buscar_multa(id)
    if not row or row["guild_id"] != interaction.guild.id:
        await interaction.followup.send("❌ No encontré esa multa.", ephemeral=True)
        return

    with conectar_db() as conn:
        conn.execute("UPDATE multas SET status = 'cancelada' WHERE id = ?", (id,))
        conn.commit()

    cfg = get_config(interaction.guild.id)
    staff_ch = await obtener_canal(interaction.guild, int(cfg.get("canal_staff_id") or 0))
    if staff_ch:
        await staff_ch.send(f"⚫ {interaction.user.mention} canceló/eliminó la multa `#{id}` de <@{row['usuario_id']}>.")

    await interaction.followup.send(f"✅ Multa `#{id}` cancelada/eliminada correctamente.", ephemeral=True)


@multa.command(name="limpiar", description="Eliminar todas las multas pendientes de un usuario")
@app_commands.describe(usuario="Usuario al que se le limpiarán las multas pendientes")
async def limpiar(interaction: discord.Interaction, usuario: discord.Member):
    await interaction.response.defer(ephemeral=True)
    if not es_staff(interaction.user):
        await interaction.followup.send("❌ No tienes permiso para usar este comando.", ephemeral=True)
        return

    with conectar_db() as conn:
        cur = conn.execute(
            """
            UPDATE multas
            SET status = 'cancelada'
            WHERE guild_id = ? AND usuario_id = ? AND status = 'pendiente'
            """,
            (interaction.guild.id, usuario.id),
        )
        conn.commit()
        cantidad = cur.rowcount

    await interaction.followup.send(f"✅ Se limpiaron/cancelaron **{cantidad}** multa(s) pendientes de {usuario.mention}.", ephemeral=True)


# ============================================================
# RECORDATORIOS Y SANCIONES
# ============================================================

async def enviar_recordatorio(row: sqlite3.Row, numero: int, mensaje: str):
    guild = bot.get_guild(row["guild_id"])
    if not guild:
        return

    cfg = get_config(guild.id)
    multas_ch = await obtener_canal(guild, int(cfg.get("canal_multas_id") or 0))
    staff_ch = await obtener_canal(guild, int(cfg.get("canal_staff_id") or 0))
    economia_id = int(cfg.get("canal_economia_id") or 0)

    texto = (
        f"⚠️ **Advertencia {numero}/3**\n"
        f"<@{row['usuario_id']}>, recuerda pagar tu multa `#{row['id']}`.\n"
        f"**Monto:** R$ {int(row['monto']):,}\n"
        f"{mensaje}\n"
        f"Canal de pago: <#{economia_id}>"
    ).replace(",", ",")

    if multas_ch:
        await multas_ch.send(texto)
    if staff_ch:
        await staff_ch.send(f"📌 Log recordatorio {numero}/3 enviado para multa `#{row['id']}` de <@{row['usuario_id']}>.")

    with conectar_db() as conn:
        conn.execute("UPDATE multas SET advertencias = ? WHERE id = ?", (numero, row["id"]))
        conn.commit()


@tasks.loop(minutes=5)
async def revisar_multas():
    await bot.wait_until_ready()

    with conectar_db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM multas
            WHERE status = 'pendiente'
              AND tipo IN ('Presencial', 'Digital')
              AND due_at IS NOT NULL
            """
        ).fetchall()

    for row in rows:
        try:
            limite = datetime.fromisoformat(row["due_at"])
        except Exception:
            continue

        restante = limite - ahora()
        segundos = restante.total_seconds()
        guild = bot.get_guild(row["guild_id"])
        cfg = get_config(row["guild_id"])

        if segundos <= 0:
            with conectar_db() as conn:
                conn.execute("UPDATE multas SET status = 'sancionada' WHERE id = ?", (row["id"],))
                conn.commit()

            if guild:
                try:
                    member = guild.get_member(row["usuario_id"]) or await guild.fetch_member(row["usuario_id"])
                    rol = guild.get_role(int(cfg.get("rol_sancionado_id") or 0))
                    if rol:
                        await member.add_roles(rol, reason="No pagó multa en el plazo establecido")
                except Exception:
                    pass

                multas_ch = await obtener_canal(guild, int(cfg.get("canal_multas_id") or 0))
                staff_ch = await obtener_canal(guild, int(cfg.get("canal_staff_id") or 0))
                if multas_ch:
                    await multas_ch.send(f"🔴 <@{row['usuario_id']}>, no pagaste la multa `#{row['id']}` a tiempo. Se aplicó sanción.")
                if staff_ch:
                    await staff_ch.send(f"🚨 La multa `#{row['id']}` de <@{row['usuario_id']}> venció y fue sancionada.")
            continue

        # Plazo de 3 días:
        # aviso 1 cuando faltan 2 días o menos
        # aviso 2 cuando falta 1 día o menos
        # aviso 3 cuando faltan 6 horas o menos
        if segundos <= 6 * 3600 and row["advertencias"] < 3:
            await enviar_recordatorio(row, 3, "Última advertencia. Si no pagas, tendrás consecuencias.")
        elif segundos <= 24 * 3600 and row["advertencias"] < 2:
            await enviar_recordatorio(row, 2, "Te queda 1 día o menos para pagar.")
        elif segundos <= 2 * 24 * 3600 and row["advertencias"] < 1:
            await enviar_recordatorio(row, 1, "Te quedan 2 días o menos para pagar.")


# ============================================================
# EVENTOS
# ============================================================

@bot.event
async def on_ready():
    iniciar_db()

    try:
        synced_global = await bot.tree.sync()
        print(f"Comandos globales sincronizados: {len(synced_global)}")
    except Exception as e:
        print(f"Error sincronizando comandos globales: {e}")

    for guild in bot.guilds:
        try:
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            print(f"Comandos sincronizados en {guild.name} ({guild.id}): {len(synced)}")
        except Exception as e:
            print(f"Error sincronizando comandos en {guild.name}: {e}")

    if not revisar_multas.is_running():
        revisar_multas.start()
        print("Sistema de recordatorios iniciado.")

    print(f"Conectado como {bot.user} (ID: {bot.user.id})")


bot.tree.add_command(multa)

if not TOKEN:
    raise RuntimeError("Falta DISCORD_TOKEN en variables de entorno/secrets.")

iniciar_db()
bot.run(TOKEN)
