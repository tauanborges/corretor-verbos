import re
import sqlite3
from datetime import datetime
from flask import Flask, request, redirect, url_for, render_template_string, jsonify

APP_TITLE = "Corretor de Verbos (por regras da turma)"
DB_PATH = "regras.sqlite"

app = Flask(__name__)
db_init()

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

def get_rules():
    conn = db_connect()
    cur = conn.cursor()
    # Ordena por tamanho do "wrong" (maiores primeiro) para evitar troca parcial atrapalhar outra
    cur.execute("SELECT * FROM rules ORDER BY LENGTH(wrong) DESC, id DESC;")
    rows = cur.fetchall()
    conn.close()
    return rows

def add_rule(wrong: str, right: str, notes: str = ""):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO rules (wrong, right, notes, created_at) VALUES (?, ?, ?, ?)",
        (wrong.strip(), right.strip(), (notes or "").strip(), datetime.now().isoformat(timespec="seconds"))
    )
    conn.commit()
    conn.close()

def delete_rule(rule_id: int):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("DELETE FROM rules WHERE id = ?", (rule_id,))
    conn.commit()
    conn.close()

# ----------------------------
# Correção: múltiplos erros
# ----------------------------
def apply_case_like(source_text: str, replacement: str) -> str:
    """
    Tenta preservar caixa:
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
    Retorna texto corrigido + lista de alterações (para mostrar ao aluno).
    """
    rules = get_rules()
    corrected = text
    changes = []

    # Para cada regra, substitui com borda de palavra quando possível.
    # Se a regra tiver espaços, ainda tentamos achar como "frase" (com limites menos rígidos).
    for r in rules:
        wrong = r["wrong"]
        right = r["right"]

        if not wrong:
            continue

        # Se tiver letras/números, usamos um padrão com "limite" mais seguro.
        # - Para 1 palavra: \b...\b
        # - Para várias palavras: tenta respeitar espaços e pontuação ao redor
        if " " not in wrong.strip():
            pattern = re.compile(rf"\b{re.escape(wrong)}\b", re.IGNORECASE)
        else:
            # Multi-palavra: aceita início/fim ou pontuação ao redor
            pattern = re.compile(rf"(?<!\w){re.escape(wrong)}(?!\w)", re.IGNORECASE)

        def _repl(match):
            original = match.group(0)
            repl = apply_case_like(original, right)
            changes.append({"de": original, "para": repl})
            return repl

        corrected_new, n = pattern.subn(_repl, corrected)
        corrected = corrected_new

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
  </style>
</head>
<body>
  <h1>{{title}}</h1>
  <p class="muted">
    Digite uma frase e a ferramenta tentará corrigir com base nas regras cadastradas pela turma.
    <br>
    <a href="{{url_for('admin')}}">Ir para o painel de regras (/admin)</a>
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
    body { font-family: Arial, sans-serif; max-width: 1000px; margin: 30px auto; padding: 0 16px; }
    input, textarea { width: 100%; padding: 10px; font-size: 15px; }
    button { padding: 10px 14px; font-size: 15px; cursor: pointer; }
    table { width: 100%; border-collapse: collapse; margin-top: 18px; }
    th, td { border-bottom: 1px solid #eee; padding: 10px; vertical-align: top; text-align: left; }
    .row { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
    .muted { color: #666; }
    .danger { background: #ffecec; border: 1px solid #ffbcbc; padding: 10px; border-radius: 8px; }
    a { text-decoration: none; }
    code { background: #f4f4f4; padding: 2px 6px; border-radius: 6px; }
  </style>
</head>
<body>
  <h1>Painel de Regras</h1>
  <p class="muted">
    Cadastre pares <b>errado → correto</b>. A ferramenta aplica todas as regras que encontrar.
    <br>
    <a href="{{url_for('home')}}">← Voltar para a ferramenta</a>
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

  <h2>Regras cadastradas ({{rules|length}})</h2>
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

    return render_template_string(HOME_HTML, title=APP_TITLE, text=text, result=result, changes=changes)

@app.route("/admin", methods=["GET"])
def admin():
    rules = get_rules()
    return render_template_string(ADMIN_HTML, title=APP_TITLE, rules=rules)

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

# API simples (caso um aluno do 1º de informática queira integrar com outra interface)
@app.route("/api/correct", methods=["POST"])
def api_correct():
    data = request.get_json(force=True, silent=True) or {}
    text = data.get("text", "")
    corrected, changes = correct_text(text)
    return jsonify({"input": text, "corrected": corrected, "changes": changes})

if __name__ == "__main__":
    db_init()
    # host=0.0.0.0 permite acessar de outros PCs na mesma rede (opcional).
    # Para começar, deixe padrão e use no próprio computador.
    app.run()

