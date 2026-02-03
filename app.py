import os
import re
import json
import sqlite3
from datetime import datetime
from flask import Flask, request, redirect, url_for, render_template_string, jsonify, make_response

APP_TITLE = "Corretor de Verbos (por regras da turma)"

# =========================================================
# Render Free: use /tmp (gravável). Pode resetar em reinícios.
# =========================================================
DB_PATH = os.environ.get("DB_PATH", "/tmp/regras.sqlite")

app = Flask(__name__)

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
    conn.commit()
    conn.close()

# Inicializa o banco ao carregar (essencial no Render/Gunicorn)
db_init()

def get_rules():
    conn = db_connect()
    cur = conn.cursor()
    try:
        cur.execute("SELECT * FROM rules ORDER BY LENGTH(wrong) DESC, id DESC;")
        rows = cur.fetchall()
    except sqlite3.OperationalError as e:
        if "no such table" in str(e).lower():
            db_init()
            rows = []
        else:
            raise
    finally:
        conn.close()
    return rows

def add_rule(wrong: str, right: str, notes: str = ""):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO rules (wrong, right, notes, created_at) VALUES (?, ?, ?, ?)",
        (wrong.strip(), right.strip(), (notes or "").strip(),
         datetime.now().isoformat(timespec="seconds"))
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

# ----------------------------
# Correção: múltiplos erros
# ----------------------------
def apply_case_like(source_text: str, replacement: str) -> str:
    """
    Preserva caixa:
    - tudo maiúsculo -> tudo maiúsculo
    - Capitalizado -> capitalizado
    - resto -> como está
    """
    if source_text.isupper():
        return replacement.upper()
    if source_text[:1].isupper() and source_text[1:].islower():
        return replacement[:1].upper() + replacement[1:]
    return replacement

def correct_text(text: str):
    """
    Aplica TODAS as regras encontradas na frase (múltiplos erros).
    Retorna texto corrigido + lista de alterações.
    """
    rules = get_rules()
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
# Templates (HTML) simples
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
  </style>
</head>
<body>
  <h1>{{title}}</h1>

  <p class="muted">
    Digite uma frase e a ferramenta tentará corrigir com base nas regras cadastradas pela turma.
    <br>
    <a href="{{url_for('admin')}}">Ir para o painel de regras (/admin)</a>
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
        <p class="muted">Nenhuma regra cadastrada bateu com o texto. (Talvez falte cadastrar esse caso no /admin.)</p>
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
  <title>Painel de Regras - {{title}}</title>
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
    .btn-row { display:flex; gap: 10px; flex-wrap: wrap; }
    .pill { display:inline-block; padding: 4px 10px; border-radius: 999px; background:#f4f4f4; color:#444; font-size: 12px; }
  </style>
</head>
<body>
  <h1>Painel de Regras</h1>

  <p class="muted">
    Cadastre pares <b>errado → correto</b>. A ferramenta aplica todas as regras que encontrar.
    <br>
    <a href="{{url_for('home')}}">← Voltar para a ferramenta</a>
  </p>

  <p class="muted">
    <span class="pill">Banco: {{db_path}}</span>
    <span class="pill">Total de regras: {{rules|length}}</span>
  </p>

  <div class="danger">
    <b>Dica didática:</b> vocês podem criar “missões” para os alunos alimentarem o sistema:
    (1) identificar o erro, (2) justificar a correção, (3) cadastrar a regra e (4) testar com frases diferentes.
  </div>

  <h2>Adicionar nova regra</h2>
  <form method="post" action="{{url_for('admin_add')}}">
    <div class="row">
      <div>
        <label><b>Forma errada (como o aluno costuma escrever)</b></label>
        <input name="wrong" placeholder="Ex.: nós vai" required>
      </div>
      <div>
        <label><b>Forma correta</b></label>
        <input name="right" placeholder="Ex.: nós vamos" required>
      </div>
    </div>
    <br>
    <label><b>Observação (opcional)</b> — use para explicar o porquê (tempo verbal, concordância etc.)</label>
    <textarea name="notes" placeholder="Ex.: 1ª pessoa do plural no presente do indicativo"></textarea>
    <br><br>
    <button type="submit">Salvar regra</button>
  </form>

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
      <p class="muted">
        Abra o arquivo JSON do backup, copie tudo e cole abaixo. Depois clique em <b>Importar</b>.
      </p>

      <form method="post" action="{{url_for('admin_import')}}">
        <textarea name="json_payload" placeholder='Cole aqui o JSON do backup (começa com {"exported_at": ... })' style="min-height: 170px;"></textarea>
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

  <h2>Regras cadastradas</h2>
  <table>
    <thead>
      <tr>
        <th>Errado</th>
        <th>Correto</th>
        <th>Observação</th>
        <th>Criada em</th>
        <th>Ação</th>
      </tr>
    </thead>
    <tbody>
      {% for r in rules %}
        <tr>
          <td><code>{{r.wrong}}</code></td>
          <td><code>{{r.right}}</code></td>
          <td class="muted">{{r.notes or ""}}</td>
          <td class="muted">{{r.created_at}}</td>
          <td>
            <form method="post" action="{{url_for('admin_delete', rule_id=r.id)}}" onsubmit="return confirm('Excluir esta regra?');">
              <button type="submit">Excluir</button>
            </form>
          </td>
        </tr>
      {% endfor %}
      {% if not rules %}
        <tr><td colspan="5" class="muted">Nenhuma regra ainda. Cadastre as primeiras acima.</td></tr>
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
        db_path=DB_PATH
    )

@app.route("/admin", methods=["GET"])
def admin():
    rules = get_rules()
    import_msg = request.args.get("msg", "")
    return render_template_string(
        ADMIN_HTML,
        title=APP_TITLE,
        rules=rules,
        db_path=DB_PATH,
        import_msg=import_msg
    )

@app.route("/admin/add", methods=["POST"])
def admin_add():
    wrong = request.form.get("wrong", "").strip()
    right = request.form.get("right", "").strip()
    notes = request.form.get("notes", "").strip()

    if wrong and right:
        add_rule(wrong, right, notes)

    return redirect(url_for("admin"))

@app.route("/admin/delete/<int:rule_id>", methods=["POST"])
def admin_delete(rule_id):
    delete_rule(rule_id)
    return redirect(url_for("admin"))

# ----------------------------
# Export / Import com interface
# ----------------------------
@app.route("/admin/export", methods=["GET"])
def admin_export():
    rules = get_rules()
    data = [
        {"wrong": r["wrong"], "right": r["right"], "notes": r["notes"] or "", "created_at": r["created_at"]}
        for r in rules
    ]
    return jsonify({"exported_at": datetime.now().isoformat(timespec="seconds"), "rules": data})

@app.route("/admin/export/download", methods=["GET"])
def admin_export_download():
    payload = admin_export().get_json()
    filename = "regras-backup.json"
    body = json.dumps(payload, ensure_ascii=False, indent=2)

    resp = make_response(body)
    resp.headers["Content-Type"] = "application/json; charset=utf-8"
    resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp

@app.route("/admin/import", methods=["POST"])
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
        for item in rules:
            if not isinstance(item, dict):
                continue
            wrong = (item.get("wrong") or "").strip()
            right = (item.get("right") or "").strip()
            notes = (item.get("notes") or "").strip()
            if wrong and right:
                add_rule(wrong, right, notes)
                count += 1

        return redirect(url_for("admin", msg=f"Importação concluída: {count} regra(s) adicionada(s)."))

    except Exception as e:
        return redirect(url_for("admin", msg=f"Erro ao importar JSON: {str(e)}"))

@app.route("/admin/clear", methods=["POST"])
def admin_clear():
    clear_rules()
    return redirect(url_for("admin", msg="Todas as regras foram apagadas."))

# API simples (caso a turma de informática queira integrar com outra interface)
@app.route("/api/correct", methods=["POST"])
def api_correct():
    data = request.get_json(force=True, silent=True) or {}
    text = data.get("text", "")
    corrected, changes = correct_text(text)
    return jsonify({"input": text, "corrected": corrected, "changes": changes})

if __name__ == "__main__":
    app.run()
