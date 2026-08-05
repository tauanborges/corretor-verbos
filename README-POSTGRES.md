# CONJUGA CIEBTEC — PostgreSQL

## Arquivos
- `app.py`: aplicação Flask já adaptada para PostgreSQL.
- `requirements.txt`: dependências.
- `render.yaml`: opção de infraestrutura como código no Render.

## Variáveis necessárias
- `DATABASE_URL`: URL de conexão do PostgreSQL.
- `SECRET_KEY`: chave secreta do Flask.
- `ADMIN_PASSWORD`: senha dos alunos responsáveis.
- `REVIEW_PASSWORD`: senha do professor.

## Render
Se o banco e o Web Service forem criados manualmente, basta copiar a Internal Database URL do PostgreSQL para `DATABASE_URL` no Web Service.

Se usar `render.yaml`, revise os nomes dos serviços antes de aplicar o Blueprint.

## Migração do SQLite
1. No sistema antigo, entre como professor.
2. Exporte `regras-backup.json`.
3. Publique esta versão com PostgreSQL.
4. Entre no painel como professor.
5. Abra Backup/Restaurar.
6. Cole o JSON no campo de importação e importe.
7. Confira algumas regras, rankings e contribuições.

## Importante
O PostgreSQL persistente evita que as regras desapareçam quando o Web Service reinicia. Ainda assim, mantenha backups periódicos. Persistência não significa que perda acidental, exclusão manual ou problema grave de infraestrutura seja impossível.
