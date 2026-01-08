# Guia de Uso do Alembic

Este guia mostra como configurar e usar o Alembic para gerenciar migrações do banco de dados.

## Instalação e Configuração Inicial

### 1. Inicializar Alembic

```bash
alembic init alembic
```

Isso criará:
```
alembic/
├── env.py
├── script.py.mako
└── versions/
alembic.ini
```

### 2. Configurar alembic.ini

Edite `alembic.ini` e altere a linha `sqlalchemy.url`:

```ini
# Comente ou remova esta linha:
# sqlalchemy.url = driver://user:pass@localhost/dbname

# Vamos usar a configuração do .env via código
```

### 3. Configurar alembic/env.py

Substitua o conteúdo de `alembic/env.py`:

```python
from logging.config import fileConfig
from sqlalchemy import engine_from_config
from sqlalchemy import pool
from alembic import context

# Importa nossa configuração e modelos
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import settings
from src.database.base import Base
from src.database.models import SeiProcessoTempETL  # Importa todos os modelos

# this is the Alembic Config object
config = context.config

# Sobrescreve a URL com a nossa configuração
config.set_main_option('sqlalchemy.url', settings.local_db_url)

# Interpret the config file for Python logging
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# add your model's MetaData object here for 'autogenerate' support
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
```

## Comandos Básicos

### Criar uma nova migration (autogenerate)

```bash
alembic revision --autogenerate -m "Descrição da mudança"
```

Exemplo:
```bash
alembic revision --autogenerate -m "Create sei_processos_temp_etl table"
```

### Criar uma migration vazia (manual)

```bash
alembic revision -m "Descrição da mudança"
```

### Aplicar migrations

```bash
# Aplicar todas as migrations pendentes
alembic upgrade head

# Aplicar até uma versão específica
alembic upgrade <revision_id>

# Aplicar próxima migration
alembic upgrade +1
```

### Reverter migrations

```bash
# Reverter última migration
alembic downgrade -1

# Reverter até uma versão específica
alembic downgrade <revision_id>

# Reverter todas
alembic downgrade base
```

### Ver histórico

```bash
# Ver histórico de migrations
alembic history

# Ver migrations pendentes
alembic current

# Ver próximas migrations
alembic heads
```

## Workflow Típico

### 1. Fazer mudança no modelo

Edite `src/database/models.py`:

```python
class SeiProcessoTempETL(Base):
    __tablename__ = 'sei_processos_temp_etl'

    id = Column(Integer, primary_key=True)
    # ... campos existentes ...

    # NOVO CAMPO
    status = Column(String(50), default='pendente')
```

### 2. Gerar migration automática

```bash
alembic revision --autogenerate -m "Add status column to sei_processos_temp_etl"
```

### 3. Revisar a migration gerada

Abra o arquivo gerado em `alembic/versions/<timestamp>_add_status_column.py`:

```python
def upgrade() -> None:
    op.add_column('sei_processos_temp_etl',
                  sa.Column('status', sa.String(length=50), nullable=True))

def downgrade() -> None:
    op.drop_column('sei_processos_temp_etl', 'status')
```

### 4. Aplicar migration

```bash
alembic upgrade head
```

## Migrations Comuns

### Adicionar coluna

```python
def upgrade():
    op.add_column('table_name',
                  sa.Column('column_name', sa.String(50), nullable=True))

def downgrade():
    op.drop_column('table_name', 'column_name')
```

### Remover coluna

```python
def upgrade():
    op.drop_column('table_name', 'column_name')

def downgrade():
    op.add_column('table_name',
                  sa.Column('column_name', sa.String(50)))
```

### Criar índice

```python
def upgrade():
    op.create_index('idx_protocol', 'sei_processos_temp_etl', ['protocol'])

def downgrade():
    op.drop_index('idx_protocol', 'sei_processos_temp_etl')
```

### Adicionar constraint

```python
def upgrade():
    op.create_unique_constraint('uq_id_protocolo',
                                'sei_processos_temp_etl',
                                ['id_protocolo'])

def downgrade():
    op.drop_constraint('uq_id_protocolo', 'sei_processos_temp_etl')
```

### Migration com dados

```python
from alembic import op
import sqlalchemy as sa
from sqlalchemy.sql import table, column

def upgrade():
    # Adiciona coluna
    op.add_column('sei_processos_temp_etl',
                  sa.Column('status', sa.String(50)))

    # Popula dados existentes
    temp_table = table('sei_processos_temp_etl',
                      column('status', sa.String))
    op.execute(
        temp_table.update().values(status='pendente')
    )

def downgrade():
    op.drop_column('sei_processos_temp_etl', 'status')
```

## Troubleshooting

### Erro: "Target database is not up to date"

```bash
# Veja o estado atual
alembic current

# Aplique as migrations
alembic upgrade head
```

### Erro: "Can't locate revision identified by 'xyz'"

```bash
# Veja o histórico
alembic history

# Se necessário, marque manualmente
alembic stamp head
```

### Reset completo (CUIDADO: apaga tudo!)

```bash
# Reverte todas as migrations
alembic downgrade base

# Dropa todas as tabelas
docker exec -it sei-ontology-postgres psql -U sei_user -d sei_ontology -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"

# Recria tudo
alembic upgrade head
```

### Migration não detecta mudanças

Certifique-se de:
1. Importar o modelo em `alembic/env.py`
2. O modelo herda de `Base`
3. `target_metadata = Base.metadata` está configurado

## Best Practices

1. **Sempre revise migrations autogenerate**: Elas podem não detectar tudo
2. **Teste migrations**: Aplique e reverta para garantir que funcionam
3. **Um objetivo por migration**: Facilita debugging e rollback
4. **Mensagens descritivas**: Use `-m` com descrições claras
5. **Não edite migrations aplicadas**: Crie uma nova migration para correções
6. **Versione migrations**: Commit as migrations no git
7. **Documente mudanças complexas**: Adicione comentários no código

## Exemplo Completo

```bash
# 1. Certifique-se que está no ambiente virtual
source venv/bin/activate

# 2. Certifique-se que o Docker está rodando
docker-compose up -d

# 3. Inicialize Alembic (primeira vez)
alembic init alembic

# 4. Configure alembic/env.py (veja seção acima)

# 5. Crie migration inicial
alembic revision --autogenerate -m "Initial schema"

# 6. Revise a migration gerada
cat alembic/versions/<timestamp>_initial_schema.py

# 7. Aplique
alembic upgrade head

# 8. Verifique
alembic current
```

---

**Nota**: O script `extract_processos_gerados.py` cria as tabelas automaticamente usando `Base.metadata.create_all()`, então o Alembic é opcional mas recomendado para produção.
