import os
import re
import json
import sqlite3
from datetime import datetime
from functools import wraps
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

APP_TITLE = "CONJUGA CIEBTEC"  # se quiser: "CONJUGANDO CIEBTEC"

# =========================================================
# Render Free: use /tmp (gravável). Pode resetar em reinícios.
# =========================================================
DB_PATH = os.environ.get("DB_PATH", "/tmp/regras.sqlite")

# Status internos
STATUS_PENDING = "PENDING"
STATUS_APPROVED_RANK = "APPROVED_RANK"
STATUS_APPROVED_NO_RANK = "APPROVED_NO_RANK"
STATUS_NOT_APPROVED = "NOT_APPROVED"

# Papéis (roles)
ROLE_ADMIN = "admin"         # alunos responsáveis
ROLE_REVIEWER = "reviewer"   # professor

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")

# ----------------------------
# Autorização por papel
# ----------------------------
def admin_required(fn):
    """Permite acessar /admin para admin (alunos) e reviewer (professor)."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        admin_pass = os.environ.get("ADMIN_PASSWORD", "")
        review_pass = os.environ.get("REVIEW_PASSWORD", "")
        if not admin_pass or not review_pass:
            return "ADMIN_PASSWORD e/ou REVIEW_PASSWORD não configuradas no Render.", 500

        if session.get("role") in (ROLE_ADMIN, ROLE_REVIEWER):
            return fn(*args, **kwargs)

        return redirect(url_for("login", next=request.path))
    return wrapper


def reviewer_required(fn):
    """Acesso exclusivo do professor para revisão/aprovação e ações sensíveis."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if session.get("role") == ROLE_REVIEWER:
            return fn(*args, **kwargs)
        return "Acesso restrito ao professor revisor.", 403
    return wrapper


# ----------------------------
# Login
# ----------------------------
LOGIN_HTML = """
<!doctype html>
<html lang="pt-br">
<head>
  <meta charset="utf-8">
  <title>Login - {{title}}</title>
  <style>
    body { font-family: Arial, sans-serif; max-width: 520px; margin: 60px auto; padding: 0 16px; }
    input { width: 100%; padding: 10px; font-size: 16px; }
    button { padding: 10px 14px; font-size: 16px; cursor: pointer; }
    .box { border: 1px solid #ddd; border-radius: 10px; padding: 16px; }
    .muted { color:#666; }
    .err { color:#b00020; margin-top: 10px; }
    a { text-decoration: none; }
  </style>
</head>
<body>
  <h1>Área restrita</h1>
  <p class="muted">Entre com a senha para acessar o painel.</p>

  <div class="box">
    <form method="post" action="{{url_for('login')}}">
      <label><b>Senha</b></label><br>
      <input type="password" name="password" required autofocus>
      <input type="hidden" name="next" value="{{next_url}}">
      <br><br>
      <button type="submit">Entrar</button>
    </form>

    {% if error %}
      <div class="err"><b>{{error}}</b></div>
    {% endif %}
  </div>

  <p class="muted"><a href="{{url_for('home')}}">← Voltar para a ferramenta</a></p>
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
                error="ADMIN_PASSWORD e/ou REVIEW_PASSWORD não configuradas no Render.",
                next_url=next_url,
            )

        # Professor
        if password == review_pass:
            session["role"] = ROLE_REVIEWER
            return redirect(next_url or "/admin")

        # Alunos responsáveis
        if password == admin_pass:
            session["role"] = ROLE_ADMIN
            return redirect(next_url or "/admin")

        return render_template_string(LOGIN_HTML, title=APP_TITLE, error="Senha incorreta.", next_url=next_url)

    return render_template_string(LOGIN_HTML, title=APP_TITLE, error="", next_url=next_url)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("home"))


# ----------------------------
# Banco de dados (SQLite)
# ----------------------------
def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def db_init():
    conn = db_connect()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            wrong TEXT NOT NULL,
            right TEXT NOT NULL,
            notes TEXT,
            created_at TEXT NOT NULL
        );
    """)

    # Migrações seguras
    for stmt in [
        "ALTER TABLE rules ADD COLUMN contributor TEXT;",
        "ALTER TABLE rules ADD COLUMN status TEXT;",
        "ALTER TABLE rules ADD COLUMN reviewed_at TEXT;",
    ]:
        try:
            cur.execute(stmt)
        except sqlite3.OperationalError:
            pass

    # Para regras antigas sem status: não pontuam por padrão (evita inflar ranking)
    cur.execute("""
        UPDATE rules
        SET status = ?
        WHERE status IS NULL OR TRIM(status) = '';
    """, (STATUS_APPROVED_NO_RANK,))

    conn.commit()
    conn.close()


db_init()


def get_pending_count() -> int:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM rules WHERE status = ?;", (STATUS_PENDING,))
    n = int(cur.fetchone()[0])
    conn.close()
    return n


def get_rules_list(view: str = "default"):
    """
    view:
      - default: mostra só aprovadas (pontua e não pontua)
      - all: tudo (inclui pendentes e não aprovadas)
      - pending: só pendentes
      - approved_rank: só aprovadas que pontuam
      - approved_no_rank: só aprovadas que não pontuam
      - not_approved: só não aprovadas
    """
    conn = db_connect()
    cur = conn.cursor()

    where = []
    params = []

    if view == "all":
        pass
    elif view == "pending":
        where.append("status = ?")
        params.append(STATUS_PENDING)
    elif view == "approved_rank":
        where.append("status = ?")
        params.append(STATUS_APPROVED_RANK)
    elif view == "approved_no_rank":
        where.append("status = ?")
        params.append(STATUS_APPROVED_NO_RANK)
    elif view == "not_approved":
        where.append("status = ?")
        params.append(STATUS_NOT_APPROVED)
    else:
        # default: esconda PENDING e NOT_APPROVED
        where.append("status IN (?, ?)")
        params.extend([STATUS_APPROVED_RANK, STATUS_APPROVED_NO_RANK])

    sql = "SELECT * FROM rules"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC;"

    cur.execute(sql, params)
    rows = cur.fetchall()
    conn.close()
    return rows


def add_rule(wrong: str, right: str, notes: str = "", contributor: str = ""):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO rules (wrong, right, notes, created_at, contributor, status, reviewed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            wrong.strip(),
            right.strip(),
            (notes or "").strip(),
            datetime.now().isoformat(timespec="seconds"),
            (contributor or "").strip(),
            STATUS_PENDING,
            None,
        ),
    )
    conn.commit()
    conn.close()


def set_rule_status(rule_id: int, new_status: str):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "UPDATE rules SET status = ?, reviewed_at = ? WHERE id = ?",
        (new_status, datetime.now().isoformat(timespec="seconds"), rule_id),
    )
    conn.commit()
    conn.close()


def delete_rule(rule_id: int):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("DELETE FROM rules WHERE id = ?", (rule_id,))
    conn.commit()
    conn.close()


def clear_rules():
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("DELETE FROM rules;")
    conn.commit()
    conn.close()


def get_leaderboard(limit: int = 10):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT contributor, COUNT(*) as total
        FROM rules
        WHERE contributor IS NOT NULL
          AND TRIM(contributor) <> ''
          AND status = ?
        GROUP BY contributor
        ORDER BY total DESC, contributor ASC
        LIMIT ?;
        """,
        (STATUS_APPROVED_RANK, limit),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


# ----------------------------
# Correção: múltiplos erros
# ----------------------------
def apply_case_like(source_text: str, replacement: str) -> str:
    if source_text.isupper():
        return replacement.upper()
    if source_text[:1].isupper() and source_text[1:].islower():
        return replacement[:1].upper() + replacement[1:]
    return replacement


def correct_text(text: str):
    rules = get_rules_list(view="default")  # só aprovadas entram na correção por padrão
    corrected = text
    changes = []

    for r in rules:
        wrong = r["wrong"]
        right = r["right"]
        if not wrong:
            continue

        if " " not in wrong.strip():
            pattern = re.compile(rf"\b{re.escape(wrong)}\b", re.IGNORECASE)
        else:
            pattern = re.compile(rf"(?<!\w){re.escape(wrong)}(?!\w)", re.IGNORECASE)

        def _repl(match):
            original = match.group(0)
            repl = apply_case_like(original, right)
            changes.append({"de": original, "para": repl})
            return repl

        corrected, _ = pattern.subn(_repl, corrected)

    return corrected, changes


# ----------------------------
# Helpers de exibição
# ----------------------------
def status_label(status: str) -> str:
    if status == STATUS_PENDING:
        return "Pendente de revisão"
    if status == STATUS_APPROVED_RANK:
        return "Aprovada (pontua para o ranking)"
    if status == STATUS_APPROVED_NO_RANK:
        return "Aprovada (não pontua para o ranking)"
    if status == STATUS_NOT_APPROVED:
        return "Não aprovada"
    return status or ""


# ----------------------------
# Templates (HTML)
# ----------------------------
HOME_HTML = """
<!doctype html>
<html lang="pt-br">
<head>
  <meta charset="utf-8">
  <title>{{title}}</title>
  <style>
    body { font-family: Arial, sans-serif; max-width: 900px; margin: 30px auto; padding: 0 16px; }
    textarea { width: 100%; min-height: 120px; padding: 10px; font-size: 16px; }
    button { padding: 10px 14px; font-size: 16px; cursor: pointer; }
    .box { border: 1px solid #ddd; border-radius: 8px; padding: 14px; margin-top: 16px; }
    .muted { color: #666; }
    .changes li { margin: 6px 0; }

    a { text-decoration: none; }

    .pill { display:inline-block; padding: 4px 10px; border-radius: 999px; background:#f4f4f4; color:#444; font-size: 12px; }

    .header { margin: 14px 0 18px; }
    .logos { display: flex; gap: 14px; align-items: center; flex-wrap: wrap; margin-bottom: 10px; }
    .logos img { max-height: 60px; max-width: 220px; width: auto; height: auto; object-fit: contain; }
    .credit { margin: 0; color: #444; background: #f7f7f7; border: 1px solid #e6e6e6; padding: 10px 12px; border-radius: 10px; line-height: 1.4; }

    /* Botãozinho (link com cara de botão) */
    .btn-link {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 10px 14px;
      border-radius: 12px;
      border: 1px solid #d8e6ff;
      background: #eef5ff;
      color: #1456c2;
      font-weight: 700;
      line-height: 1;
      margin-top: 10px;
    }
    .btn-link:hover { filter: brightness(0.98); }
  </style>
</head>
<body>
  <h1>{{title}}</h1>

  <div class="header">
    <div class="logos">
      <img src="{{url_for('static', filename='logo_dchtxxi.jpg')}}" alt="Logo DCHT XXI">
      <img src="{{url_for('static', filename='logo_ciebtec.jpg')}}" alt="Logo CIEBTEC">
      <img src="{{url_for('static', filename='logo_pibid.jpg')}}" alt="Logo PIBID">
    </div>

    <p class="credit">
      Este site foi desenvolvido pelo discente do DCHT XXI, Tauan Borges, em parceria com o PIBID e o CIEBTEC,
      com o intuito de fomentar a educação científica, a cultura digital e a escrita adequada.
    </p>
  </div>

  <p class="muted">
    Digite uma frase e a ferramenta tentará corrigir com base nas regras cadastradas pela turma.
    <br>
    <a class="btn-link" href="{{url_for('admin')}}">🔐 Abrir Painel</a>
  </p>

  <p class="muted">
    <span class="pill">Banco: {{db_path}}</span>
  </p>

  <form method="post" action="{{url_for('home')}}">
    <label><b>Frase do aluno</b></label><br>
    <textarea name="text" placeholder="Ex.: nós vai amanhã e eles foi ontem...">{{text or ""}}</textarea><br><br>
    <button type="submit">Corrigir</button>
  </form>

  {% if result is not none %}
    <div class="box">
      <h2>Resultado</h2>
      <p><b>Texto corrigido:</b></p>
      <div class="box" style="background:#fafafa;">{{result}}</div>

      <h3>Alterações encontradas ({{changes|length}})</h3>
      {% if changes %}
        <ul class="changes">
          {% for c in changes %}
            <li><code>{{c.de}}</code> → <code>{{c.para}}</code></li>
          {% endfor %}
        </ul>
      {% else %}
        <p class="muted">Nenhuma regra cadastrada bateu com o texto.</p>
      {% endif %}
    </div>
  {% endif %}
</body>
</html>
"""


ADMIN_HTML = """
<!doctype html>
<html lang="pt-br">
<head>
  <meta charset="utf-8">
  <title>Painel - {{title}}</title>
  <style>
    body { font-family: Arial, sans-serif; max-width: 1100px; margin: 30px auto; padding: 0 16px; }
    input, textarea { width: 100%; padding: 10px; font-size: 15px; }
    button { padding: 10px 14px; font-size: 15px; cursor: pointer; }
    table { width: 100%; border-collapse: collapse; margin-top: 18px; }
    th, td { border-bottom: 1px solid #eee; padding: 10px; vertical-align: top; text-align: left; }
    .row { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
    .muted { color: #666; }
    .danger { background: #ffecec; border: 1px solid #ffbcbc; padding: 10px; border-radius: 8px; }
    .info { background: #eef6ff; border: 1px solid #b9dcff; padding: 10px; border-radius: 8px; }
    a { text-decoration: none; }
    code { background: #f4f4f4; padding: 2px 6px; border-radius: 6px; }
    details { margin-top: 16px; }
    summary { cursor: pointer; font-weight: bold; }
    .btn-row { display:flex; gap: 10px; flex-wrap: wrap; align-items:center; }
    .pill { display:inline-block; padding: 4px 10px; border-radius: 999px; background:#f4f4f4; color:#444; font-size: 12px; }

    /* Cards mais atrativos */
    .box { border: 1px solid #e6e6e6; border-radius: 14px; padding: 16px; margin-top: 16px; background: #fff; box-shadow: 0 1px 10px rgba(0,0,0,0.04); }

    /* Títulos mais bonitos */
    h2 { margin-top: 30px; border-left: 6px solid #2b7cff; padding-left: 12px; }

    /* Leaderboard */
    .leaderboard ol { margin: 10px 0 0 22px; padding: 0; }
    .leaderboard li { margin: 8px 0; }
    .leaderboard .lead-note { margin: 0; margin-top: 6px; }
    .medal { display:inline-block; min-width: 26px; text-align:center; }

    /* ===== Botõezinhos dos filtros ===== */
    .chip-row { display:flex; flex-wrap: wrap; gap: 10px; margin-top: 10px; }
    .chip {
      display:inline-flex;
      align-items:center;
      gap: 8px;
      padding: 8px 12px;
      border-radius: 999px;
      border: 1px solid #e3e3e3;
      background: #fafafa;
      color: #333;
      font-weight: 700;
      font-size: 13px;
      line-height: 1;
    }
    .chip:hover { filter: brightness(0.98); }
    .chip.active {
      border-color: #2b7cff;
      background: #eef5ff;
      color: #1456c2;
      box-shadow: 0 1px 10px rgba(43,124,255,0.12);
    }
  </style>
</head>
<body>
  <h1>Painel</h1>

  <p class="muted">
    <a class="chip" href="{{url_for('home')}}">⬅ Voltar para a ferramenta</a>
  </p>

  <div class="btn-row">
    <form method="post" action="{{url_for('logout')}}">
      <button type="submit">Sair</button>
    </form>

    {% if role == "reviewer" %}
      <a class="pill" href="{{url_for('admin_review')}}">
        Fila de revisão: {{pending_count}} pendente(s)
      </a>
    {% else %}
      <span class="pill">Pendentes: {{pending_count}} (somente o professor revisa)</span>
    {% endif %}
  </div>

  <p class="muted">
    <span class="pill">Banco: {{db_path}}</span>
    <span class="pill">Total exibido: {{rules|length}}</span>
  </p>

  <div class="box leaderboard">
    <h2>🏆 Painel de Líderes da Conjugação</h2>
    <p class="muted lead-note">
      Destaques da turma: quem mais contribuiu com regras aprovadas para melhorar a ferramenta.
    </p>

    {% if leaders and leaders|length > 0 %}
      <ol>
        {% for l in leaders %}
          <li>
            {% if loop.index == 1 %}
              <span class="medal">🥇</span>
            {% elif loop.index == 2 %}
              <span class="medal">🥈</span>
            {% elif loop.index == 3 %}
              <span class="medal">🥉</span>
            {% else %}
              <span class="medal">⭐</span>
            {% endif %}
            <b>{{l.contributor}}</b> — {{l.total}} contribuição(ões)
          </li>
        {% endfor %}
      </ol>
    {% else %}
      <p class="muted">Ainda não há contribuições aprovadas para o ranking. Vamos começar? 🙂</p>
    {% endif %}
  </div>

  <div class="danger">
    <b>Fluxo:</b> novas regras entram como <b>Pendentes</b> e serão revisadas pelo professor.
  </div>

  <h2>✏️ Contribuir com uma nova regra</h2>
  <p class="muted">
    Ajude a melhorar a ferramenta cadastrando erros comuns de conjugação e suas correções.
  </p>

  <form method="post" action="{{url_for('admin_add')}}">
    <div class="row">
      <div>
        <label><b>Forma errada</b></label>
        <input name="wrong" placeholder="Ex.: nós vai" required>
      </div>
      <div>
        <label><b>Forma correta</b></label>
        <input name="right" placeholder="Ex.: nós vamos" required>
      </div>
    </div>

    <br>
    <label><b>Username do aluno (para o painel de líderes)</b></label>
    <input name="contributor" placeholder="Ex.: ana_1info" required>

    <br><br>
    <label><b>Observação (opcional)</b></label>
    <textarea name="notes" placeholder="Ex.: comentário rápido (se quiserem)"></textarea>
    <br><br>
    <button type="submit">Enviar contribuição</button>
  </form>

  {% if role == "reviewer" %}
  <details>
    <summary>Backup/Restaurar (Importar/Exportar)</summary>
    <div class="info">
      <p class="muted">
        No plano gratuito, as regras podem sumir se o serviço reiniciar.
        Use o <b>Exportar</b> para salvar um backup e o <b>Importar</b> para restaurar.
      </p>

      <div class="btn-row">
        <form method="get" action="{{url_for('admin_export_download')}}">
          <button type="submit">Exportar regras (baixar arquivo .json)</button>
        </form>

        <form method="post" action="{{url_for('admin_clear')}}" onsubmit="return confirm('Tem certeza que deseja APAGAR TODAS as regras?');">
          <button type="submit">Apagar todas as regras</button>
        </form>
      </div>

      <h3>Importar regras</h3>
      <form method="post" action="{{url_for('admin_import')}}">
        <textarea name="json_payload" placeholder='Cole aqui o JSON do backup' style="min-height: 170px;"></textarea>
        <br><br>
        <label>
          <input type="checkbox" name="replace_all" value="1">
          Substituir tudo (apagar regras atuais antes de importar)
        </label>
        <br><br>
        <button type="submit">Importar</button>
      </form>

      {% if import_msg %}
        <p><b>{{import_msg}}</b></p>
      {% endif %}
    </div>
  </details>
  {% endif %}

  <h2>Regras</h2>

  {% if role == "reviewer" %}
    <div class="muted">
      <b>Filtro:</b>
      <div class="chip-row">
        <a class="chip {% if view == 'default' %}active{% endif %}" href="{{url_for('admin', view='default')}}">✅ Aprovadas (padrão)</a>
        <a class="chip {% if view == 'approved_rank' %}active{% endif %}" href="{{url_for('admin', view='approved_rank')}}">🏆 Aprovadas (pontua)</a>
        <a class="chip {% if view == 'approved_no_rank' %}active{% endif %}" href="{{url_for('admin', view='approved_no_rank')}}">⭐ Aprovadas (não pontua)</a>
        <a class="chip {% if view == 'all' %}active{% endif %}" href="{{url_for('admin', view='all')}}">📚 Todas</a>
        <a class="chip {% if view == 'not_approved' %}active{% endif %}" href="{{url_for('admin', view='not_approved')}}">🚫 Não aprovadas</a>
      </div>
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
          <td><code>{{r.wrong}}</code></td>
          <td><code>{{r.right}}</code></td>
          <td class="muted">{{r.contributor or ""}}</td>
          <td class="muted">{{status_label(r.status)}}</td>
          <td class="muted">{{r.notes or ""}}</td>
          <td class="muted">{{r.created_at}}</td>
          <td class="muted">{{r.reviewed_at or ""}}</td>
          <td>
            {% if role == "reviewer" %}
              <form method="post" action="{{url_for('admin_delete', rule_id=r.id)}}" onsubmit="return confirm('Excluir esta regra?');">
                <button type="submit">Excluir</button>
              </form>
            {% else %}
              <span class="muted">—</span>
            {% endif %}
          </td>
        </tr>
      {% endfor %}
      {% if not rules %}
        <tr><td colspan="8" class="muted">Nenhuma regra para este filtro.</td></tr>
      {% endif %}
    </tbody>
  </table>
</body>
</html>
"""


REVIEW_HTML = """
<!doctype html>
<html lang="pt-br">
<head>
  <meta charset="utf-8">
  <title>Fila de revisão - {{title}}</title>
  <style>
    body { font-family: Arial, sans-serif; max-width: 1100px; margin: 30px auto; padding: 0 16px; }
    button { padding: 10px 14px; font-size: 14px; cursor: pointer; }
    table { width: 100%; border-collapse: collapse; margin-top: 18px; }
    th, td { border-bottom: 1px solid #eee; padding: 10px; vertical-align: top; text-align: left; }
    .muted { color: #666; }
    code { background: #f4f4f4; padding: 2px 6px; border-radius: 6px; }
    .btns { display:flex; gap: 8px; flex-wrap: wrap; }
    .top { display:flex; gap: 10px; align-items:center; flex-wrap: wrap; }
    a { text-decoration:none; }
    .pill { display:inline-block; padding: 4px 10px; border-radius: 999px; background:#f4f4f4; color:#444; font-size: 12px; }
    .chip {
      display:inline-flex; align-items:center; gap:8px;
      padding: 8px 12px; border-radius:999px;
      border:1px solid #e3e3e3; background:#fafafa; color:#333; font-weight:700; font-size:13px;
    }
  </style>
</head>
<body>
  <div class="top">
    <h1 style="margin:0;">Fila de revisão</h1>
    <span class="pill">{{pending_count}} pendente(s)</span>
  </div>

  <p class="muted">
    <a class="chip" href="{{url_for('admin')}}">⬅ Voltar para o Painel</a>
  </p>

  {% if msg %}
    <p><b>{{msg}}</b></p>
  {% endif %}

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
          <td><code>{{r.wrong}}</code></td>
          <td><code>{{r.right}}</code></td>
          <td class="muted">{{r.contributor or ""}}</td>
          <td class="muted">{{r.notes or ""}}</td>
          <td class="muted">{{r.created_at}}</td>
          <td>
            <div class="btns">
              <form method="post" action="{{url_for('admin_review_decide', rule_id=r.id)}}">
                <input type="hidden" name="decision" value="rank">
                <button type="submit">Aprovar (pontua)</button>
              </form>

              <form method="post" action="{{url_for('admin_review_decide', rule_id=r.id)}}">
                <input type="hidden" name="decision" value="no_rank">
                <button type="submit">Aprovar (não pontua)</button>
              </form>

              <form method="post" action="{{url_for('admin_review_decide', rule_id=r.id)}}" onsubmit="return confirm('Marcar como NÃO aprovada?');">
                <input type="hidden" name="decision" value="not_approved">
                <button type="submit">Não aprovar</button>
              </form>
            </div>
          </td>
        </tr>
      {% endfor %}
      {% if not pending %}
        <tr><td colspan="6" class="muted">Nenhuma regra pendente no momento.</td></tr>
      {% endif %}
    </tbody>
  </table>
</body>
</html>
"""


# ----------------------------
# Rotas (páginas)
# ----------------------------
@app.route("/", methods=["GET", "POST"])
def home():
    text = ""
    result = None
    changes = []
    if request.method == "POST":
        text = request.form.get("text", "")
        result, changes = correct_text(text)

    return render_template_string(
        HOME_HTML,
        title=APP_TITLE,
        text=text,
        result=result,
        changes=changes,
        db_path=DB_PATH,
    )


@app.route("/admin", methods=["GET"])
@admin_required
def admin():
    role = session.get("role")
    import_msg = request.args.get("msg", "")

    # Alunos não podem escolher filtros avançados: sempre default
    if role == ROLE_REVIEWER:
        view = request.args.get("view", "default").strip() or "default"
    else:
        view = "default"

    rules = get_rules_list(view=view)
    leaders = get_leaderboard(10)
    pending_count = get_pending_count()

    return render_template_string(
        ADMIN_HTML,
        title=APP_TITLE,
        rules=rules,
        leaders=leaders,
        pending_count=pending_count,
        db_path=DB_PATH,
        import_msg=import_msg,
        status_label=status_label,
        role=role,
        view=view,  # <-- necessário para destacar o filtro ativo
    )


@app.route("/admin/add", methods=["POST"])
@admin_required
def admin_add():
    wrong = request.form.get("wrong", "").strip()
    right = request.form.get("right", "").strip()
    notes = request.form.get("notes", "").strip()
    contributor = request.form.get("contributor", "").strip()

    if wrong and right:
        add_rule(wrong, right, notes, contributor)

    # Professor vai para fila, aluno volta para /admin
    if session.get("role") == ROLE_REVIEWER:
        return redirect(url_for("admin_review"))
    return redirect(url_for("admin"))


@app.route("/admin/revisao", methods=["GET"])
@reviewer_required
def admin_review():
    msg = request.args.get("msg", "")
    pending = get_rules_list(view="pending")
    pending_count = len(pending)
    return render_template_string(
        REVIEW_HTML,
        title=APP_TITLE,
        pending=pending,
        pending_count=pending_count,
        msg=msg,
    )


@app.route("/admin/revisao/decidir/<int:rule_id>", methods=["POST"])
@reviewer_required
def admin_review_decide(rule_id: int):
    decision = (request.form.get("decision", "") or "").strip().lower()

    if decision == "rank":
        set_rule_status(rule_id, STATUS_APPROVED_RANK)
        return redirect(url_for("admin_review", msg="Regra aprovada (pontua para o ranking)."))

    if decision == "no_rank":
        set_rule_status(rule_id, STATUS_APPROVED_NO_RANK)
        return redirect(url_for("admin_review", msg="Regra aprovada (não pontua para o ranking)."))

    if decision == "not_approved":
        set_rule_status(rule_id, STATUS_NOT_APPROVED)
        return redirect(url_for("admin_review", msg="Regra marcada como não aprovada."))

    return redirect(url_for("admin_review", msg="Decisão inválida."))


@app.route("/admin/delete/<int:rule_id>", methods=["POST"])
@reviewer_required
def admin_delete(rule_id: int):
    delete_rule(rule_id)
    return redirect(url_for("admin"))


# ----------------------------
# Export / Import / Clear (SOMENTE PROFESSOR)
# ----------------------------
@app.route("/admin/export", methods=["GET"])
@reviewer_required
def admin_export():
    rules = get_rules_list(view="all")
    data = [
        {
            "wrong": r["wrong"],
            "right": r["right"],
            "notes": r["notes"] or "",
            "contributor": (r["contributor"] or ""),
            "status": (r["status"] or ""),
            "created_at": r["created_at"],
            "reviewed_at": r["reviewed_at"] if "reviewed_at" in r.keys() else None,
        }
        for r in rules
    ]
    return jsonify({"exported_at": datetime.now().isoformat(timespec="seconds"), "rules": data})


@app.route("/admin/export/download", methods=["GET"])
@reviewer_required
def admin_export_download():
    payload = admin_export().get_json()
    filename = "regras-backup.json"
    body = json.dumps(payload, ensure_ascii=False, indent=2)

    resp = make_response(body)
    resp.headers["Content-Type"] = "application/json; charset=utf-8"
    resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp


@app.route("/admin/import", methods=["POST"])
@reviewer_required
def admin_import():
    json_payload = request.form.get("json_payload", "").strip()
    replace_all = request.form.get("replace_all") == "1"

    if not json_payload:
        return redirect(url_for("admin", msg="Nada importado: o campo está vazio."))

    try:
        payload = json.loads(json_payload)
        rules = payload.get("rules", [])
        if not isinstance(rules, list):
            return redirect(url_for("admin", msg="Erro: o JSON não tem uma lista válida em 'rules'."))

        if replace_all:
            clear_rules()

        count = 0
        conn = db_connect()
        cur = conn.cursor()

        for item in rules:
            if not isinstance(item, dict):
                continue

            wrong = (item.get("wrong") or "").strip()
            right = (item.get("right") or "").strip()
            notes = (item.get("notes") or "").strip()
            contributor = (item.get("contributor") or "").strip()
            status = (item.get("status") or "").strip() or STATUS_APPROVED_NO_RANK
            created_at = (item.get("created_at") or "").strip() or datetime.now().isoformat(timespec="seconds")
            reviewed_at = item.get("reviewed_at", None)

            if wrong and right:
                cur.execute(
                    "INSERT INTO rules (wrong, right, notes, created_at, contributor, status, reviewed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (wrong, right, notes, created_at, contributor, status, reviewed_at),
                )
                count += 1

        conn.commit()
        conn.close()

        return redirect(url_for("admin", msg=f"Importação concluída: {count} regra(s) adicionada(s)."))

    except Exception as e:
        return redirect(url_for("admin", msg=f"Erro ao importar JSON: {str(e)}"))


@app.route("/admin/clear", methods=["POST"])
@reviewer_required
def admin_clear():
    clear_rules()
    return redirect(url_for("admin", msg="Todas as regras foram apagadas."))


# API simples (deixa aberta)
@app.route("/api/correct", methods=["POST"])
def api_correct():
    data = request.get_json(force=True, silent=True) or {}
    text = data.get("text", "")
    corrected, changes = correct_text(text)
    return jsonify({"input": text, "corrected": corrected, "changes": changes})


if __name__ == "__main__":
    app.run()
