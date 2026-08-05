import os
import re
import json
from datetime import datetime, timedelta, date
from functools import wraps

import psycopg
from psycopg.rows import dict_row
from flask import (
    Flask,
    request,
    redirect,
    url_for,
    render_template_string,
    jsonify,
    make_response,
    session,
)


APP_TITLE = "CONJUGA CIEBTEC"

# =========================================================
# PostgreSQL
# =========================================================
# No Render, crie uma variável DATABASE_URL com a Internal
# Database URL do seu PostgreSQL. O código também aceita
# URLs que começam com postgres://.
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

STATUS_PENDING = "PENDING"
STATUS_APPROVED_RANK = "APPROVED_RANK"
STATUS_APPROVED_NO_RANK = "APPROVED_NO_RANK"
STATUS_NOT_APPROVED = "NOT_APPROVED"

ROLE_ADMIN = "admin"
ROLE_REVIEWER = "reviewer"

DEFAULT_DAILY_LIMIT = 5
SETTING_DAILY_LIMIT_MODE = "daily_limit_mode"
MODE_LIMITED = "limited"
MODE_UNLIMITED = "unlimited"

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")


# =========================================================
# BANCO
# =========================================================
def db_connect():
    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL não configurada. "
            "Crie a variável DATABASE_URL no Render apontando "
            "para o seu PostgreSQL."
        )

    url = DATABASE_URL

    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]

    return psycopg.connect(url, row_factory=dict_row)


def db_init():
    conn = db_connect()

    try:
        with conn.cursor() as cur:

            # =================================================
            # TABELA DE REGRAS
            #
            # "right" precisa estar entre aspas porque RIGHT
            # é uma palavra reservada/função do PostgreSQL.
            # =================================================
            cur.execute("""
                CREATE TABLE IF NOT EXISTS rules (
                    id BIGSERIAL PRIMARY KEY,
                    wrong TEXT NOT NULL,
                    "right" TEXT NOT NULL,
                    notes TEXT,
                    created_at TEXT NOT NULL,
                    contributor TEXT,
                    status TEXT,
                    reviewed_at TEXT
                );
            """)

            # =================================================
            # TABELA DE CONFIGURAÇÕES
            # =================================================
            cur.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
            """)

            # Regras antigas importadas ou registros sem status.
            cur.execute("""
                UPDATE rules
                SET status = %s
                WHERE status IS NULL OR TRIM(status) = '';
            """, (STATUS_APPROVED_NO_RANK,))

            # Configuração inicial do limite diário.
            cur.execute("""
                INSERT INTO settings (key, value)
                VALUES (%s, %s)
                ON CONFLICT (key) DO NOTHING;
            """, (SETTING_DAILY_LIMIT_MODE, MODE_LIMITED))

        conn.commit()

    finally:
        conn.close()


# =========================================================
# AUTORIZAÇÃO
# =========================================================
def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        admin_pass = os.environ.get("ADMIN_PASSWORD", "")
        review_pass = os.environ.get("REVIEW_PASSWORD", "")

        if not admin_pass or not review_pass:
            return (
                "ADMIN_PASSWORD e/ou REVIEW_PASSWORD "
                "não configuradas no Render.",
                500,
            )

        if session.get("role") in (ROLE_ADMIN, ROLE_REVIEWER):
            return fn(*args, **kwargs)

        return redirect(url_for("login", next=request.path))

    return wrapper


def reviewer_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if session.get("role") == ROLE_REVIEWER:
            return fn(*args, **kwargs)

        return "Acesso restrito ao professor revisor.", 403

    return wrapper


# =========================================================
# LOGIN
# =========================================================
LOGIN_HTML = """
<!doctype html>
<html lang="pt-br">
<head>
<meta charset="utf-8">
<title>Login - {{title}}</title>
<style>
body{font-family:Arial,sans-serif;max-width:520px;margin:60px auto;padding:0 16px}
input{width:100%;padding:10px;font-size:16px;box-sizing:border-box}
button,.btn{padding:10px 14px;font-size:16px;cursor:pointer;border-radius:10px;border:1px solid #6b6b6b;background:#6b6b6b;color:#fff}
button:hover,.btn:hover{background:#5a5a5a}
.box{border:1px solid #ddd;border-radius:10px;padding:16px}
.muted{color:#666}.err{color:#b00020;margin-top:10px}
a{text-decoration:none}
</style>
</head>
<body>

<h1>Área restrita</h1>

<p class="muted">
Entre com a senha para acessar o painel.
</p>

<div class="box">

<form method="post" action="{{url_for('login')}}">

<label><b>Senha</b></label><br>

<input
    type="password"
    name="password"
    required
    autofocus
>

<input
    type="hidden"
    name="next"
    value="{{next_url}}"
>

<br><br>

<button type="submit">Entrar</button>

</form>

{% if error %}
<div class="err">
<b>{{error}}</b>
</div>
{% endif %}

</div>

<p class="muted">
<a class="btn" href="{{url_for('home')}}">
← Voltar para a ferramenta
</a>
</p>

</body>
</html>
"""


@app.route("/login", methods=["GET", "POST"])
def login():
    next_url = request.values.get("next", "/admin")

    if request.method == "POST":

        password = request.form.get("password", "")

        admin_pass = os.environ.get("ADMIN_PASSWORD", "")
        review_pass = os.environ.get("REVIEW_PASSWORD", "")

        if not admin_pass or not review_pass:
            return render_template_string(
                LOGIN_HTML,
                title=APP_TITLE,
                error=(
                    "ADMIN_PASSWORD e/ou REVIEW_PASSWORD "
                    "não configuradas no Render."
                ),
                next_url=next_url,
            )

        if password == review_pass:
            session["role"] = ROLE_REVIEWER
            return redirect(next_url or "/admin")

        if password == admin_pass:
            session["role"] = ROLE_ADMIN
            return redirect(next_url or "/admin")

        return render_template_string(
            LOGIN_HTML,
            title=APP_TITLE,
            error="Senha incorreta.",
            next_url=next_url,
        )

    return render_template_string(
        LOGIN_HTML,
        title=APP_TITLE,
        error="",
        next_url=next_url,
    )


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("home"))


# =========================================================
# CONFIGURAÇÕES
# =========================================================
def get_daily_limit_mode():
    conn = db_connect()

    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT value FROM settings WHERE key = %s",
                (SETTING_DAILY_LIMIT_MODE,),
            )

            row = cur.fetchone()

    finally:
        conn.close()

    if not row:
        return MODE_LIMITED

    value = (row["value"] or "").strip().lower()

    return (
        MODE_UNLIMITED
        if value == MODE_UNLIMITED
        else MODE_LIMITED
    )


def set_daily_limit_mode(mode):
    if mode not in (MODE_LIMITED, MODE_UNLIMITED):
        mode = MODE_LIMITED

    conn = db_connect()

    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO settings (key, value)
                VALUES (%s, %s)
                ON CONFLICT (key)
                DO UPDATE SET value = EXCLUDED.value
            """, (
                SETTING_DAILY_LIMIT_MODE,
                mode,
            ))

        conn.commit()

    finally:
        conn.close()


def is_daily_limit_enabled():
    return get_daily_limit_mode() == MODE_LIMITED


# =========================================================
# REGRAS
# =========================================================
def get_pending_count():
    conn = db_connect()

    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS total FROM rules WHERE status = %s",
                (STATUS_PENDING,),
            )

            n = int(cur.fetchone()["total"])

    finally:
        conn.close()

    return n


def get_rules_list(view="default"):
    conn = db_connect()

    where = []
    params = []

    if view == "all":
        pass

    elif view == "pending":
        where.append("status = %s")
        params.append(STATUS_PENDING)

    elif view == "approved_rank":
        where.append("status = %s")
        params.append(STATUS_APPROVED_RANK)

    elif view == "approved_no_rank":
        where.append("status = %s")
        params.append(STATUS_APPROVED_NO_RANK)

    elif view == "not_approved":
        where.append("status = %s")
        params.append(STATUS_NOT_APPROVED)

    else:
        where.append("status IN (%s, %s)")
        params.extend([
            STATUS_APPROVED_RANK,
            STATUS_APPROVED_NO_RANK,
        ])

    sql = "SELECT * FROM rules"

    if where:
        sql += " WHERE " + " AND ".join(where)

    sql += " ORDER BY id DESC"

    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

    finally:
        conn.close()

    return rows


def is_duplicate_rule(wrong, right):
    conn = db_connect()

    try:
        with conn.cursor() as cur:

            # "right" precisa estar entre aspas no PostgreSQL.
            cur.execute("""
                SELECT COUNT(*) AS total
                FROM rules
                WHERE LOWER(TRIM(wrong)) = LOWER(TRIM(%s))
                  AND LOWER(TRIM("right")) = LOWER(TRIM(%s))
            """, (
                wrong,
                right,
            ))

            n = int(cur.fetchone()["total"])

    finally:
        conn.close()

    return n > 0


def count_rules_today(contributor):
    if not contributor:
        return 0

    today = date.today().isoformat()

    conn = db_connect()

    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) AS total
                FROM rules
                WHERE contributor IS NOT NULL
                  AND TRIM(contributor) <> ''
                  AND LOWER(TRIM(contributor)) = LOWER(TRIM(%s))
                  AND created_at LIKE %s
            """, (
                contributor,
                f"{today}%",
            ))

            n = int(cur.fetchone()["total"])

    finally:
        conn.close()

    return n


def add_rule(wrong, right, notes="", contributor=""):
    conn = db_connect()

    try:
        with conn.cursor() as cur:

            # "right" precisa estar entre aspas no PostgreSQL.
            cur.execute("""
                INSERT INTO rules
                (
                    wrong,
                    "right",
                    notes,
                    created_at,
                    contributor,
                    status,
                    reviewed_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (
                wrong.strip(),
                right.strip(),
                (notes or "").strip(),
                datetime.now().isoformat(timespec="seconds"),
                (contributor or "").strip(),
                STATUS_PENDING,
                None,
            ))

        conn.commit()

    finally:
        conn.close()


def set_rule_status(rule_id, new_status):
    conn = db_connect()

    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE rules
                SET status = %s,
                    reviewed_at = %s
                WHERE id = %s
            """, (
                new_status,
                datetime.now().isoformat(timespec="seconds"),
                rule_id,
            ))

        conn.commit()

    finally:
        conn.close()


def delete_rule(rule_id):
    conn = db_connect()

    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM rules WHERE id = %s",
                (rule_id,),
            )

        conn.commit()

    finally:
        conn.close()


def clear_rules():
    conn = db_connect()

    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM rules")

        conn.commit()

    finally:
        conn.close()


def get_hall_of_fame(limit=3):
    conn = db_connect()

    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT contributor, COUNT(*) AS total
                FROM rules
                WHERE contributor IS NOT NULL
                  AND TRIM(contributor) <> ''
                  AND status = %s
                GROUP BY contributor
                ORDER BY total DESC, contributor ASC
                LIMIT %s
            """, (
                STATUS_APPROVED_RANK,
                limit,
            ))

            rows = cur.fetchall()

    finally:
        conn.close()

    return rows


def get_top_week(days=7, limit=3):
    since = (
        datetime.now() - timedelta(days=days)
    ).isoformat(timespec="seconds")

    conn = db_connect()

    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT contributor, COUNT(*) AS total
                FROM rules
                WHERE contributor IS NOT NULL
                  AND TRIM(contributor) <> ''
                  AND status = %s
                  AND reviewed_at IS NOT NULL
                  AND reviewed_at >= %s
                GROUP BY contributor
                ORDER BY total DESC, contributor ASC
                LIMIT %s
            """, (
                STATUS_APPROVED_RANK,
                since,
                limit,
            ))

            rows = cur.fetchall()

    finally:
        conn.close()

    return rows


def get_news_feed(limit=3):
    conn = db_connect()

    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    wrong,
                    "right",
                    contributor,
                    status,
                    reviewed_at
                FROM rules
                WHERE status IN (%s, %s)
                  AND reviewed_at IS NOT NULL
                ORDER BY reviewed_at DESC
                LIMIT %s
            """, (
                STATUS_APPROVED_RANK,
                STATUS_APPROVED_NO_RANK,
                limit,
            ))

            rows = cur.fetchall()

    finally:
        conn.close()

    return rows


# =========================================================
# CORREÇÃO
# =========================================================
def apply_case_like(source_text, replacement):
    if source_text.isupper():
        return replacement.upper()

    if (
        source_text[:1].isupper()
        and source_text[1:].islower()
    ):
        return replacement[:1].upper() + replacement[1:]

    return replacement


def correct_text(text):
    rules = get_rules_list(view="default")

    corrected = text
    changes = []

    for r in rules:

        wrong = r["wrong"]
        right = r["right"]

        if not wrong:
            continue

        if " " not in wrong.strip():
            pattern = re.compile(
                rf"\b{re.escape(wrong)}\b",
                re.IGNORECASE,
            )
        else:
            pattern = re.compile(
                rf"(?<!\w){re.escape(wrong)}(?!\w)",
                re.IGNORECASE,
            )

        def _repl(match):
            original = match.group(0)

            repl = apply_case_like(
                original,
                right,
            )

            changes.append({
                "de": original,
                "para": repl,
                "contributor": r["contributor"] or "",
            })

            return repl

        corrected, _ = pattern.subn(
            _repl,
            corrected,
        )

    return corrected, changes


def status_label(status):
    if status == STATUS_PENDING:
        return "Pendente de revisão"

    if status == STATUS_APPROVED_RANK:
        return "Aprovada (pontua para o ranking)"

    if status == STATUS_APPROVED_NO_RANK:
        return "Aprovada (não pontua para o ranking)"

    if status == STATUS_NOT_APPROVED:
        return "Não aprovada"

    return status or ""


# =========================================================
# HOME
# =========================================================
HOME_HTML = """
<!doctype html>
<html lang="pt-br">
<head>
<meta charset="utf-8">
<title>{{title}}</title>

<style>
body{
    font-family:Arial,sans-serif;
    max-width:900px;
    margin:30px auto;
    padding:0 16px;
    color:#222
}

textarea{
    width:100%;
    min-height:120px;
    padding:10px;
    font-size:16px;
    box-sizing:border-box
}

button,.btnlink{
    padding:10px 14px;
    font-size:16px;
    cursor:pointer;
    border-radius:10px;
    border:1px solid #6b6b6b;
    background:#6b6b6b;
    color:#fff
}

button:hover,.btnlink:hover{
    background:#5a5a5a
}

.box{
    border:1px solid #e6e6e6;
    border-radius:14px;
    padding:16px;
    margin-top:16px;
    background:#fff;
    box-shadow:0 1px 10px rgba(0,0,0,.04)
}

.muted{
    color:#666
}

.changes li{
    margin:6px 0
}

a{
    text-decoration:none
}

.pill{
    display:inline-block;
    padding:6px 12px;
    border-radius:999px;
    background:#f4f4f4;
    color:#444;
    font-size:12px
}

.header{
    margin:14px 0 18px
}

.logos{
    display:flex;
    gap:14px;
    align-items:center;
    flex-wrap:wrap;
    margin-bottom:10px
}

.logos img{
    max-height:60px;
    max-width:220px;
    width:auto;
    height:auto;
    object-fit:contain
}

.credit{
    margin:0;
    color:#444;
    background:#f7f7f7;
    border:1px solid #e6e6e6;
    padding:14px;
    border-radius:12px;
    line-height:1.5;
    font-size:16px
}

h2{
    margin-top:26px;
    border-left:6px solid #6b6b6b;
    padding-left:12px
}

.change-author{
    color:#777;
    font-size:13px;
    margin-left:5px
}
</style>
</head>

<body>

<h1>{{title}}</h1>

<div class="header">

<div class="logos">

<img
    src="{{url_for('static', filename='logo_dchtxxi.jpg')}}"
    alt="Logo DCHT XXI"
>

<img
    src="{{url_for('static', filename='logo_ciebtec.jpg')}}"
    alt="Logo CIEBTEC"
>

<img
    src="{{url_for('static', filename='logo_pibid.jpg')}}"
    alt="Logo PIBID"
>

</div>

<p class="credit">
Este site foi desenvolvido pelo discente do DCHT XXI,
Tauan Borges, em parceria com o PIBID e o CIEBTEC,
com o intuito de fomentar a educação científica,
a cultura digital e a escrita adequada.
</p>

</div>

<p class="muted">
Digite uma frase e a ferramenta tentará corrigir
com base nas regras cadastradas pela turma.
</p>

<p>
<a class="btnlink" href="{{url_for('admin')}}">
🔒 Ir para o painel
</a>
</p>

<form method="post" action="{{url_for('home')}}">

<label>
<b>Frase do aluno</b>
</label>

<br>

<textarea
    name="text"
    placeholder="Digite aqui a frase que deseja analisar..."
>{{text or ""}}</textarea>

<br><br>

<button type="submit">
✅ Corrigir
</button>

</form>

{% if result is not none %}

<div class="box">

<h2>Resultado</h2>

<p>
<b>Texto corrigido:</b>
</p>

<div
    class="box"
    style="background:#fafafa;box-shadow:none;"
>
{{result}}
</div>

<h2 style="margin-top:18px;">
Alterações encontradas ({{changes|length}})
</h2>

{% if changes %}

<ul class="changes">

{% for c in changes %}

<li>

<code>{{c.de}}</code>
→
<code>{{c.para}}</code>

{% if c.contributor %}

<span class="change-author">
por @{{c.contributor}}
</span>

{% endif %}

</li>

{% endfor %}

</ul>

{% else %}

<p class="muted">
Nenhuma regra cadastrada bateu com o texto.
</p>

{% endif %}

</div>

{% endif %}

</body>
</html>
"""


# =========================================================
# PAINEL
# =========================================================
ADMIN_HTML = """
<!doctype html>
<html lang="pt-br">

<head>

<meta charset="utf-8">

<title>Painel - {{title}}</title>

<style>

body{
    font-family:Arial,sans-serif;
    max-width:1100px;
    margin:30px auto;
    padding:0 16px;
    color:#222
}

input,textarea{
    width:100%;
    padding:10px;
    font-size:15px;
    box-sizing:border-box
}

button,.filterbtn{
    padding:10px 14px;
    font-size:15px;
    cursor:pointer;
    border-radius:10px;
    border:1px solid #6b6b6b;
    background:#6b6b6b;
    color:#fff
}

button:hover,.filterbtn:hover{
    background:#5a5a5a
}

table{
    width:100%;
    border-collapse:collapse;
    margin-top:18px
}

th,td{
    border-bottom:1px solid #eee;
    padding:10px;
    vertical-align:top;
    text-align:left
}

.row{
    display:grid;
    grid-template-columns:1fr 1fr;
    gap:12px
}

.muted{
    color:#666
}

.danger{
    background:#fff6f6;
    border:1px solid #ffd0d0;
    padding:12px;
    border-radius:12px
}

.info{
    background:#eef6ff;
    border:1px solid #b9dcff;
    padding:12px;
    border-radius:12px
}

a{
    text-decoration:none
}

code{
    background:#f4f4f4;
    padding:2px 6px;
    border-radius:6px
}

details{
    margin-top:16px
}

summary{
    cursor:pointer;
    font-weight:bold
}

.btn-row{
    display:flex;
    gap:10px;
    flex-wrap:wrap;
    align-items:center
}

.pill{
    display:inline-block;
    padding:6px 12px;
    border-radius:999px;
    background:#f4f4f4;
    color:#444;
    font-size:12px
}

.box{
    border:1px solid #e6e6e6;
    border-radius:14px;
    padding:16px;
    margin-top:16px;
    background:#fff;
    box-shadow:0 1px 10px rgba(0,0,0,.04)
}

h2{
    margin-top:30px;
    border-left:6px solid #6b6b6b;
    padding-left:12px
}

.boards{
    display:grid;
    grid-template-columns:1fr 1fr;
    gap:14px
}

@media(max-width:900px){

    .boards{
        grid-template-columns:1fr
    }

    .row{
        grid-template-columns:1fr
    }
}

.leaderboard ol{
    margin:10px 0 0 22px;
    padding:0
}

.leaderboard li{
    margin:8px 0
}

.medal{
    display:inline-block;
    min-width:26px;
    text-align:center
}

.filters{
    display:flex;
    flex-wrap:wrap;
    gap:8px;
    margin-top:10px
}

.msg{
    margin-top:12px
}

.warn{
    background:#fff7e6;
    border:1px solid #ffd9a8;
    padding:10px;
    border-radius:12px
}

.ok{
    background:#ecfff1;
    border:1px solid #b6f2c4;
    padding:10px;
    border-radius:12px
}

.dupwarn{
    display:none;
    margin-top:10px
}

.change-author{
    color:#777;
    font-size:13px;
    margin-left:5px
}

</style>

</head>

<body>

<h1>Painel</h1>

<p>

<a
    class="filterbtn"
    href="{{url_for('home')}}"
>
← Voltar para a ferramenta
</a>

</p>

<div class="btn-row">

<form
    method="post"
    action="{{url_for('logout')}}"
>

<button type="submit">
Sair
</button>

</form>

{% if role == "reviewer" %}

<a
    class="pill"
    href="{{url_for('admin_review')}}"
>
📥 Fila de revisão: {{pending_count}} pendente(s)
</a>

{% else %}

<span class="pill">
Pendentes: {{pending_count}}
(somente o professor revisa)
</span>

{% endif %}

</div>

{% if ui_msg %}

<div
    class="msg {% if ui_msg_kind=='warn' %}warn{% else %}ok{% endif %}"
>

<b>{{ui_msg}}</b>

</div>

{% endif %}

<div class="boards">

<div class="box leaderboard">

<h2>🏅 Top da Semana</h2>

<p class="muted">
Somente regras aprovadas que entram no ranking
(últimos 7 dias).
</p>

{% if top_week %}

<ol>

{% for l in top_week %}

<li>

{% if loop.index==1 %}

<span class="medal">🥇</span>

{% elif loop.index==2 %}

<span class="medal">🥈</span>

{% else %}

<span class="medal">🥉</span>

{% endif %}

<b>{{l.contributor}}</b>
— {{l.total}}

</li>

{% endfor %}

</ol>

{% else %}

<p class="muted">
Ainda não há pontuações esta semana.
</p>

{% endif %}

</div>


<div class="box leaderboard">

<h2>🏆 Hall da Fama</h2>

<p class="muted">
Ranking geral — quem mais ajudou a construir
a base de regras.
</p>

{% if hall %}

<ol>

{% for l in hall %}

<li>

{% if loop.index==1 %}

<span class="medal">👑</span>

{% elif loop.index==2 %}

<span class="medal">🥈</span>

{% else %}

<span class="medal">🥉</span>

{% endif %}

<b>{{l.contributor}}</b>
— {{l.total}}

</li>

{% endfor %}

</ol>

{% else %}

<p class="muted">
Ainda não há contribuições no hall.
</p>

{% endif %}

</div>

</div>


<div class="box">

<h2>📰 Novidades</h2>

<p class="muted">
Últimas regras aprovadas:
</p>

{% if news %}

<ul style="margin:8px 0 0 18px;">

{% for n in news %}

<li style="margin:8px 0;">

<code>{{n.wrong}}</code>
→
<code>{{n.right}}</code>

<span class="muted">
(por <b>@{{n.contributor or "—"}}</b>)
</span>

</li>

{% endfor %}

</ul>

{% else %}

<p class="muted">
Nenhuma regra aprovada ainda.
</p>

{% endif %}

</div>


<div class="danger">

<b>Fluxo:</b>
novas regras entram como
<b>Pendentes</b>
e serão revisadas pelo professor.

<br>

<span class="muted">
Limite: alunos podem enviar até
<b>5 regras por dia</b>,
quando o limite estiver ativado.
</span>

</div>


<h2>✏️ Contribuir com uma nova regra</h2>

<p class="muted">
Cadastre erros comuns e suas correções.
Se já existir igual, o sistema avisa.
</p>


<form
    id="ruleForm"
    method="post"
    action="{{url_for('admin_add')}}"
>

<div class="row">

<div>

<label>
<b>Forma errada</b>
</label>

<input
    id="wrong"
    name="wrong"
    placeholder="Ex.: nós vai"
    required
    value="{{prefill_wrong or ''}}"
>

</div>


<div>

<label>
<b>Forma correta</b>
</label>

<input
    id="right"
    name="right"
    placeholder="Ex.: nós vamos"
    required
    value="{{prefill_right or ''}}"
>

</div>

</div>

<br>

<label>
<b>Username do aluno (para o ranking)</b>
</label>

<input
    id="contributor"
    name="contributor"
    placeholder="Ex.: ana_1info"
    required
    value="{{prefill_contributor or ''}}"
>

<br><br>

<label>
<b>Observação (opcional)</b>
</label>

<textarea
    name="notes"
    placeholder="Ex.: comentário rápido"
>{{prefill_notes or ''}}</textarea>

<div
    id="dupwarn"
    class="dupwarn warn"
>

<b>⚠️ Atenção:</b>
essa regra já existe na base.
O envio será bloqueado.

</div>

<br>

<button
    id="submitBtn"
    type="submit"
>
Enviar contribuição
</button>

</form>


{% if role == "reviewer" %}

<details>

<summary>
Backup/Restaurar (Importar/Exportar)
</summary>

<div class="info">

<p class="muted">
O PostgreSQL é o banco permanente da aplicação.
O backup JSON continua disponível como camada
extra de segurança.
</p>

<div class="btn-row">

<form
    method="get"
    action="{{url_for('admin_export_download')}}"
>

<button type="submit">
Exportar regras (.json)
</button>

</form>


<form
    method="post"
    action="{{url_for('admin_clear')}}"
    onsubmit="return confirm('Tem certeza que deseja APAGAR TODAS as regras?');"
>

<button type="submit">
Apagar todas as regras
</button>

</form>

</div>


<h3>
Importar regras
</h3>

<form
    method="post"
    action="{{url_for('admin_import')}}"
>

<textarea
    name="json_payload"
    placeholder='Cole aqui o JSON do backup'
    style="min-height:170px;"
></textarea>

<br><br>

<label>

<input
    type="checkbox"
    name="replace_all"
    value="1"
>

Substituir tudo antes de importar

</label>

<br><br>

<button type="submit">
Importar
</button>

</form>


{% if import_msg %}

<p>
<b>{{import_msg}}</b>
</p>

{% endif %}

</div>

</details>

{% endif %}


<h2>
Regras
</h2>

{% if role == "reviewer" %}

<div class="filters">

<a
    class="filterbtn"
    href="{{url_for('admin',view='default')}}"
>
✅ Aprovadas
</a>

<a
    class="filterbtn"
    href="{{url_for('admin',view='approved_rank')}}"
>
🏆 Pontuam
</a>

<a
    class="filterbtn"
    href="{{url_for('admin',view='approved_no_rank')}}"
>
📌 Não pontuam
</a>

<a
    class="filterbtn"
    href="{{url_for('admin',view='all')}}"
>
📚 Todas
</a>

<a
    class="filterbtn"
    href="{{url_for('admin',view='not_approved')}}"
>
🚫 Não aprovadas
</a>

</div>

{% endif %}


<table>

<thead>

<tr>

<th>Errado</th>
<th>Correto</th>
<th>Username</th>
<th>Status</th>
<th>Observação</th>
<th>Criada em</th>
<th>Revisada em</th>
<th>Ação</th>

</tr>

</thead>

<tbody>

{% for r in rules %}

<tr>

<td>
<code>{{r.wrong}}</code>
</td>

<td>
<code>{{r.right}}</code>
</td>

<td class="muted">
{{r.contributor or ""}}
</td>

<td class="muted">
{{status_label(r.status)}}
</td>

<td class="muted">
{{r.notes or ""}}
</td>

<td class="muted">
{{r.created_at}}
</td>

<td class="muted">
{{r.reviewed_at or ""}}
</td>

<td>

{% if role=="reviewer" %}

<form
    method="post"
    action="{{url_for('admin_delete',rule_id=r.id)}}"
    onsubmit="return confirm('Excluir esta regra?');"
>

<button type="submit">
Excluir
</button>

</form>

{% else %}

<span class="muted">
—
</span>

{% endif %}

</td>

</tr>

{% endfor %}


{% if not rules %}

<tr>

<td
    colspan="8"
    class="muted"
>
Nenhuma regra para este filtro.
</td>

</tr>

{% endif %}

</tbody>

</table>


<script>

const wrongEl = document.getElementById("wrong");
const rightEl = document.getElementById("right");
const warnEl = document.getElementById("dupwarn");
const btnEl = document.getElementById("submitBtn");


async function checkDup(){

    const w = (wrongEl.value || "").trim();
    const r = (rightEl.value || "").trim();

    if(!w || !r){

        warnEl.style.display = "none";
        btnEl.disabled = false;

        return;
    }

    try{

        const qs = new URLSearchParams({
            wrong:w,
            right:r
        });

        const resp = await fetch(
            "/api/check-duplicate?" + qs.toString()
        );

        const data = await resp.json();

        warnEl.style.display =
            data.exists === true
            ? "block"
            : "none";

        btnEl.disabled =
            data.exists === true;

    }catch(e){

        warnEl.style.display = "none";
        btnEl.disabled = false;

    }
}


wrongEl.addEventListener(
    "input",
    checkDup
);

rightEl.addEventListener(
    "input",
    checkDup
);

</script>

</body>
</html>
"""


# =========================================================
# REVISÃO
# =========================================================
REVIEW_HTML = """
<!doctype html>
<html lang="pt-br">

<head>

<meta charset="utf-8">

<title>
Fila de revisão - {{title}}
</title>

<style>

body{
    font-family:Arial,sans-serif;
    max-width:1100px;
    margin:30px auto;
    padding:0 16px
}

button,.btnlink{
    padding:10px 14px;
    font-size:14px;
    cursor:pointer;
    border-radius:10px;
    border:1px solid #6b6b6b;
    background:#6b6b6b;
    color:#fff
}

button:hover,.btnlink:hover{
    background:#5a5a5a
}

table{
    width:100%;
    border-collapse:collapse;
    margin-top:18px
}

th,td{
    border-bottom:1px solid #eee;
    padding:10px;
    vertical-align:top;
    text-align:left
}

.muted{
    color:#666
}

code{
    background:#f4f4f4;
    padding:2px 6px;
    border-radius:6px
}

.btns{
    display:flex;
    gap:8px;
    flex-wrap:wrap
}

.top{
    display:flex;
    gap:10px;
    align-items:center;
    flex-wrap:wrap
}

a{
    text-decoration:none
}

.pill{
    display:inline-block;
    padding:6px 12px;
    border-radius:999px;
    background:#f4f4f4;
    color:#444;
    font-size:12px
}

.box{
    border:1px solid #e6e6e6;
    border-radius:14px;
    padding:16px;
    margin-top:16px
}

</style>

</head>

<body>

<div class="top">

<h1 style="margin:0">
Fila de revisão
</h1>

<span class="pill">
{{pending_count}} pendente(s)
</span>

</div>


<p>

<a
    class="btnlink"
    href="{{url_for('admin')}}"
>
← Voltar para o painel
</a>

</p>


{% if msg %}

<p>
<b>{{msg}}</b>
</p>

{% endif %}


<div class="box">

<b>
Limite de contribuições:
</b>

{% if daily_limit_mode == "limited" %}

5 regras por aluno/dia.

<form
    method="post"
    action="{{url_for('admin_toggle_daily_limit')}}"
    style="display:inline;margin-left:10px"
>

<input
    type="hidden"
    name="mode"
    value="unlimited"
>

<button type="submit">
Ativar ilimitado
</button>

</form>

{% else %}

Ilimitado.

<form
    method="post"
    action="{{url_for('admin_toggle_daily_limit')}}"
    style="display:inline;margin-left:10px"
>

<input
    type="hidden"
    name="mode"
    value="limited"
>

<button type="submit">
Reativar limite de 5
</button>

</form>

{% endif %}

</div>


<table>

<thead>

<tr>

<th>Errado</th>
<th>Correto</th>
<th>Username</th>
<th>Observação</th>
<th>Criada em</th>
<th>Decisão</th>

</tr>

</thead>


<tbody>

{% for r in pending %}

<tr>

<td>
<code>{{r.wrong}}</code>
</td>

<td>
<code>{{r.right}}</code>
</td>

<td class="muted">
{{r.contributor or ""}}
</td>

<td class="muted">
{{r.notes or ""}}
</td>

<td class="muted">
{{r.created_at}}
</td>

<td>

<div class="btns">


<form
    method="post"
    action="{{url_for('admin_review_decide',rule_id=r.id)}}"
>

<input
    type="hidden"
    name="decision"
    value="rank"
>

<button type="submit">
Aprovar (pontua)
</button>

</form>


<form
    method="post"
    action="{{url_for('admin_review_decide',rule_id=r.id)}}"
>

<input
    type="hidden"
    name="decision"
    value="no_rank"
>

<button type="submit">
Aprovar (não pontua)
</button>

</form>


<form
    method="post"
    action="{{url_for('admin_review_decide',rule_id=r.id)}}"
    onsubmit="return confirm('Marcar como NÃO aprovada?');"
>

<input
    type="hidden"
    name="decision"
    value="not_approved"
>

<button type="submit">
Não aprovar
</button>

</form>


</div>

</td>

</tr>

{% endfor %}


{% if not pending %}

<tr>

<td
    colspan="6"
    class="muted"
>
Nenhuma regra pendente no momento.
</td>

</tr>

{% endif %}

</tbody>

</table>

</body>
</html>
"""


# =========================================================
# ROTAS
# =========================================================
@app.route("/api/check-duplicate", methods=["GET"])
def api_check_duplicate():

    wrong = (
        request.args.get("wrong") or ""
    ).strip()

    right = (
        request.args.get("right") or ""
    ).strip()

    if not wrong or not right:
        return jsonify({
            "exists": False
        })

    return jsonify({
        "exists": is_duplicate_rule(
            wrong,
            right,
        )
    })


@app.route("/", methods=["GET", "POST"])
def home():

    text = ""
    result = None
    changes = []

    if request.method == "POST":

        text = request.form.get(
            "text",
            "",
        )

        result, changes = correct_text(
            text
        )

    return render_template_string(
        HOME_HTML,
        title=APP_TITLE,
        text=text,
        result=result,
        changes=changes,
    )


@app.route("/admin", methods=["GET"])
@admin_required
def admin():

    role = session.get("role")

    import_msg = request.args.get(
        "msg",
        "",
    )

    ui_msg = request.args.get(
        "ui_msg",
        "",
    )

    ui_msg_kind = request.args.get(
        "ui_kind",
        "ok",
    )

    if role == ROLE_REVIEWER:

        view = (
            request.args.get(
                "view",
                "default",
            ).strip()
            or "default"
        )

    else:

        view = "default"

    rules = get_rules_list(
        view=view
    )

    hall = get_hall_of_fame(
        3
    )

    top_week = get_top_week(
        days=7,
        limit=3,
    )

    news = get_news_feed(
        3
    )

    pending_count = get_pending_count()

    return render_template_string(
        ADMIN_HTML,
        title=APP_TITLE,
        rules=rules,
        hall=hall,
        top_week=top_week,
        news=news,
        pending_count=pending_count,
        import_msg=import_msg,
        status_label=status_label,
        role=role,
        ui_msg=ui_msg,
        ui_msg_kind=ui_msg_kind,
        prefill_wrong=request.args.get(
            "w",
            "",
        ),
        prefill_right=request.args.get(
            "r",
            "",
        ),
        prefill_contributor=request.args.get(
            "c",
            "",
        ),
        prefill_notes=request.args.get(
            "n",
            "",
        ),
    )


@app.route("/admin/add", methods=["POST"])
@admin_required
def admin_add():

    wrong = (
        request.form.get(
            "wrong",
            "",
        )
        or ""
    ).strip()

    right = (
        request.form.get(
            "right",
            "",
        )
        or ""
    ).strip()

    notes = (
        request.form.get(
            "notes",
            "",
        )
        or ""
    ).strip()

    contributor = (
        request.form.get(
            "contributor",
            "",
        )
        or ""
    ).strip()


    if not wrong or not right or not contributor:

        return redirect(
            url_for(
                "admin",
                ui_msg=(
                    "Preencha errado, correto "
                    "e username."
                ),
                ui_kind="warn",
                w=wrong,
                r=right,
                c=contributor,
                n=notes,
            )
        )


    if is_duplicate_rule(
        wrong,
        right,
    ):

        return redirect(
            url_for(
                "admin",
                ui_msg=(
                    "Essa regra já existe "
                    "na base. Envie uma diferente 🙂"
                ),
                ui_kind="warn",
                w=wrong,
                r=right,
                c=contributor,
                n=notes,
            )
        )


    if (
        session.get("role") == ROLE_ADMIN
        and is_daily_limit_enabled()
    ):

        if count_rules_today(
            contributor
        ) >= DEFAULT_DAILY_LIMIT:

            return redirect(
                url_for(
                    "admin",
                    ui_msg=(
                        "Limite diário atingido "
                        "(5 regras hoje). "
                        "Amanhã você pode enviar mais 🙂"
                    ),
                    ui_kind="warn",
                    w=wrong,
                    r=right,
                    c=contributor,
                    n=notes,
                )
            )


    add_rule(
        wrong,
        right,
        notes,
        contributor,
    )


    if session.get("role") == ROLE_REVIEWER:

        return redirect(
            url_for("admin_review")
        )


    return redirect(
        url_for(
            "admin",
            ui_msg=(
                "Contribuição enviada! "
                "Agora ela vai para a revisão "
                "do professor ✅"
            ),
            ui_kind="ok",
        )
    )


@app.route("/admin/revisao", methods=["GET"])
@reviewer_required
def admin_review():

    pending = get_rules_list(
        view="pending"
    )

    return render_template_string(
        REVIEW_HTML,
        title=APP_TITLE,
        pending=pending,
        pending_count=len(pending),
        msg=request.args.get(
            "msg",
            "",
        ),
        daily_limit_mode=get_daily_limit_mode(),
    )


@app.route("/admin/revisao/limite", methods=["POST"])
@reviewer_required
def admin_toggle_daily_limit():

    mode = (
        request.form.get(
            "mode",
            MODE_LIMITED,
        )
        or MODE_LIMITED
    ).strip().lower()


    if mode == MODE_UNLIMITED:

        set_daily_limit_mode(
            MODE_UNLIMITED
        )

        msg = (
            "Envios ilimitados ativados."
        )

    else:

        set_daily_limit_mode(
            MODE_LIMITED
        )

        msg = (
            "Limite diário de 5 contribuições "
            "por aluno reativado."
        )


    return redirect(
        url_for(
            "admin_review",
            msg=msg,
        )
    )


@app.route(
    "/admin/revisao/decidir/<int:rule_id>",
    methods=["POST"],
)
@reviewer_required
def admin_review_decide(rule_id):

    decision = (
        request.form.get(
            "decision",
            "",
        )
        or ""
    ).strip().lower()


    if decision == "rank":

        set_rule_status(
            rule_id,
            STATUS_APPROVED_RANK,
        )

        msg = (
            "Regra aprovada "
            "(pontua para o ranking)."
        )


    elif decision == "no_rank":

        set_rule_status(
            rule_id,
            STATUS_APPROVED_NO_RANK,
        )

        msg = (
            "Regra aprovada "
            "(não pontua para o ranking)."
        )


    elif decision == "not_approved":

        set_rule_status(
            rule_id,
            STATUS_NOT_APPROVED,
        )

        msg = (
            "Regra marcada como não aprovada."
        )


    else:

        msg = "Decisão inválida."


    return redirect(
        url_for(
            "admin_review",
            msg=msg,
        )
    )


@app.route(
    "/admin/delete/<int:rule_id>",
    methods=["POST"],
)
@reviewer_required
def admin_delete(rule_id):

    delete_rule(
        rule_id
    )

    return redirect(
        url_for("admin")
    )


# =========================================================
# EXPORTAÇÃO / IMPORTAÇÃO
# =========================================================
@app.route("/admin/export", methods=["GET"])
@reviewer_required
def admin_export():

    rules = get_rules_list(
        view="all"
    )

    data = []

    for r in rules:

        data.append({
            "wrong": r["wrong"],
            "right": r["right"],
            "notes": r["notes"] or "",
            "contributor": r["contributor"] or "",
            "status": r["status"] or "",
            "created_at": r["created_at"],
            "reviewed_at": r["reviewed_at"],
        })


    return jsonify({
        "exported_at": datetime.now().isoformat(
            timespec="seconds"
        ),
        "rules": data,
    })


@app.route(
    "/admin/export/download",
    methods=["GET"],
)
@reviewer_required
def admin_export_download():

    payload = admin_export().get_json()

    body = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
    )

    resp = make_response(
        body
    )

    resp.headers["Content-Type"] = (
        "application/json; charset=utf-8"
    )

    resp.headers["Content-Disposition"] = (
        'attachment; filename="regras-backup.json"'
    )

    return resp


@app.route(
    "/admin/import",
    methods=["POST"],
)
@reviewer_required
def admin_import():

    json_payload = (
        request.form.get(
            "json_payload",
            "",
        )
        or ""
    ).strip()

    replace_all = (
        request.form.get(
            "replace_all"
        ) == "1"
    )


    if not json_payload:

        return redirect(
            url_for(
                "admin",
                msg=(
                    "Nada importado: "
                    "o campo está vazio."
                ),
            )
        )


    try:

        payload = json.loads(
            json_payload
        )

        rules = payload.get(
            "rules",
            []
        )


        if not isinstance(
            rules,
            list,
        ):

            return redirect(
                url_for(
                    "admin",
                    msg=(
                        "Erro: o JSON não tem "
                        "uma lista válida em 'rules'."
                    ),
                )
            )


        conn = db_connect()

        try:

            with conn.cursor() as cur:

                if replace_all:
                    cur.execute(
                        "DELETE FROM rules"
                    )


                count = 0


                for item in rules:

                    if not isinstance(
                        item,
                        dict,
                    ):
                        continue


                    wrong = (
                        item.get(
                            "wrong"
                        )
                        or ""
                    ).strip()


                    right = (
                        item.get(
                            "right"
                        )
                        or ""
                    ).strip()


                    notes = (
                        item.get(
                            "notes"
                        )
                        or ""
                    ).strip()


                    contributor = (
                        item.get(
                            "contributor"
                        )
                        or ""
                    ).strip()


                    status = (
                        item.get(
                            "status"
                        )
                        or ""
                    ).strip()


                    if not status:
                        status = (
                            STATUS_APPROVED_NO_RANK
                        )


                    created_at = (
                        item.get(
                            "created_at"
                        )
                        or ""
                    ).strip()


                    if not created_at:

                        created_at = (
                            datetime.now()
                            .isoformat(
                                timespec="seconds"
                            )
                        )


                    reviewed_at = item.get(
                        "reviewed_at",
                        None,
                    )


                    if wrong and right:

                        # "right" precisa estar entre
                        # aspas no PostgreSQL.
                        cur.execute("""
                            INSERT INTO rules
                            (
                                wrong,
                                "right",
                                notes,
                                created_at,
                                contributor,
                                status,
                                reviewed_at
                            )
                            VALUES (
                                %s,
                                %s,
                                %s,
                                %s,
                                %s,
                                %s,
                                %s
                            )
                        """, (
                            wrong,
                            right,
                            notes,
                            created_at,
                            contributor,
                            status,
                            reviewed_at,
                        ))

                        count += 1


            conn.commit()

        finally:

            conn.close()


        return redirect(
            url_for(
                "admin",
                msg=(
                    f"Importação concluída: "
                    f"{count} regra(s) adicionada(s)."
                ),
            )
        )


    except Exception as e:

        return redirect(
            url_for(
                "admin",
                msg=(
                    "Erro ao importar JSON: "
                    f"{str(e)}"
                ),
            )
        )


@app.route(
    "/admin/clear",
    methods=["POST"],
)
@reviewer_required
def admin_clear():

    clear_rules()

    return redirect(
        url_for(
            "admin",
            msg="Todas as regras foram apagadas.",
        )
    )


# =========================================================
# API
# =========================================================
@app.route(
    "/api/correct",
    methods=["POST"],
)
def api_correct():

    data = (
        request.get_json(
            force=True,
            silent=True,
        )
        or {}
    )

    text = data.get(
        "text",
        "",
    )

    corrected, changes = correct_text(
        text
    )

    return jsonify({
        "input": text,
        "corrected": corrected,
        "changes": changes,
    })


# =========================================================
# INICIALIZAÇÃO
# =========================================================
try:

    db_init()

    print(
        "[CONJUGA CIEBTEC] "
        "Banco PostgreSQL inicializado com sucesso."
    )

except Exception as startup_error:

    print(
        "[CONJUGA CIEBTEC] "
        f"Banco ainda não inicializado: {startup_error}"
    )


if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=int(
            os.environ.get(
                "PORT",
                "10000",
            )
        ),
    )
