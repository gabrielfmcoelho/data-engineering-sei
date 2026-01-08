# SEI Ontology - Piauí

Modelagem ontológica de processos do Sistema Eletrônico de Informações (SEI) do Estado do Piauí em banco de grafos Neo4J.

## Visão Geral

Este projeto extrai dados do SEI, modela ontologicamente processos administrativos e permite análises avançadas, com foco especial em processos de contratação pública.

### Componentes

- **PostgreSQL**: Controle de estado da pipeline ETL
- **Neo4J**: Banco de grafos para modelo ontológico
- **Redis**: Coordenação e filas de tarefas
- **MinIO**: Armazenamento de documentos (S3-compatible)

## Pré-requisitos

- Python 3.11+
- Docker e Docker Compose
- Acesso ao banco de dados SEI do Estado do Piauí

## Instalação

### 1. Clone o repositório

```bash
git clone <seu-repo>
cd tcc
```

### 2. Crie ambiente virtual Python

```bash
python -m venv venv
source venv/bin/activate  # Linux/Mac
# ou
venv\Scripts\activate  # Windows
```

### 3. Instale dependências

```bash
pip install -r requirements.txt
```

### 4. Configure variáveis de ambiente

```bash
cp .env.example .env
```

Edite o arquivo `.env` com suas credenciais:

```env
# Banco SEI (Origem - Produção)
SEI_DB_HOST=seu_host_sei.pi.gov.br
SEI_DB_PORT=5432
SEI_DB_NAME=sei
SEI_DB_USER=seu_usuario
SEI_DB_PASSWORD=sua_senha
SEI_DB_SCHEMA=sei_processo

# Os demais podem permanecer com valores padrão
```

### 5. Inicie os serviços Docker

```bash
docker-compose up -d
```

Verifique se todos os serviços estão rodando:

```bash
docker-compose ps
```

### 6. Inicialize o banco de dados com Alembic

```bash
# Inicializa o Alembic (primeira vez)
alembic init alembic

# Edite alembic.ini e alembic/env.py conforme necessário
# Depois crie a migration inicial
alembic revision --autogenerate -m "Initial migration"

# Aplique as migrations
alembic upgrade head
```

**Nota**: O script de extração cria as tabelas automaticamente usando SQLAlchemy, mas é recomendado usar Alembic para controle de versão do schema.

## Uso

### Script 1: Extração de Processos Gerados

Este script extrai processos do banco SEI onde a atividade indica criação de processo.

**Execução:**

```bash
python -m src.scripts.extract_processos_gerados
```

**O que faz:**

1. Conecta ao banco SEI (origem)
2. Busca registros em `sei_processo.sei_atividade` onde:
   - `descricao_replace = "Processo @NIVEL_ACESSO@@GRAU_SIGILO@ gerado@DATA_AUTUACAO@@HIPOTESE_LEGAL@"`
3. Extrai campos: `protocol`, `id_protocolo`, `data_hora`, `tipo_procedimento`, `unidade`
4. Salva no banco local na tabela `sei_processos_temp_etl`

**Saída:**

- Registros inseridos na tabela `sei_processos_temp_etl` do banco local
- Logs em `logs/extract_processos_gerados_*.log`
- Progress bar no terminal mostrando o andamento

**Exemplo de saída:**

```
═══════════════════════════════════════════════════════════
  Extração de Processos Gerados - SEI Estado do Piauí
═══════════════════════════════════════════════════════════

Contando registros no banco SEI...
Total de registros encontrados: 15,243

⠹ Extraindo e carregando (batch size: 1000)... ━━━━━━━━━━━━━━━━━━━━━ 100% 15243/15243

✓ Extração concluída com sucesso!
  Total de registros inseridos: 15,243

Registros na tabela local: 15,243
```

## Estrutura do Projeto

```
tcc/
├── src/
│   ├── __init__.py
│   ├── config.py                 # Configurações (Pydantic Settings)
│   ├── database/
│   │   ├── __init__.py
│   │   ├── base.py              # Base declarativa SQLAlchemy
│   │   ├── models.py            # Modelos ORM
│   │   └── session.py           # Gerenciamento de sessões
│   └── scripts/
│       ├── __init__.py
│       └── extract_processos_gerados.py  # Script de extração
├── alembic/                     # Migrations do banco
│   └── versions/
├── logs/                        # Logs da aplicação
├── docker-compose.yml           # Orquestração de serviços
├── requirements.txt             # Dependências Python
├── .env.example                 # Exemplo de configuração
├── .env                         # Configuração real (não versionado)
├── .gitignore
├── README.md
└── ANALISE_TECNICA.md          # Documentação técnica detalhada
```

## Serviços Docker

### PostgreSQL
- **Porta**: 5432
- **Database**: `sei_ontology`
- **User**: `sei_user`
- **Password**: `sei_password`

### Neo4J
- **HTTP**: http://localhost:7474
- **Bolt**: bolt://localhost:7687
- **User**: `neo4j`
- **Password**: `sei_neo4j_password`

### Redis
- **Porta**: 6379

### MinIO
- **API**: http://localhost:9000
- **Console**: http://localhost:9001
- **User**: `minioadmin`
- **Password**: `minioadmin123`

## Próximos Passos

Após executar o script de extração inicial:

1. **Validar dados extraídos:**
   ```sql
   SELECT COUNT(*) FROM sei_processos_temp_etl;
   SELECT tipo_procedimento, COUNT(*) FROM sei_processos_temp_etl GROUP BY tipo_procedimento;
   ```

2. **Implementar script de download de andamentos** (próximo script)

3. **Implementar script de download de documentos**

4. **Desenvolver modelo ontológico no Neo4J**

5. **Implementar pipeline de extração de entidades (ML)**

## Troubleshooting

### Erro de conexão com SEI
```
Verifique se:
- As credenciais no .env estão corretas
- O host do SEI está acessível da sua máquina
- O usuário tem permissão de leitura no schema sei_processo
```

### Tabelas não foram criadas
```bash
# Force a criação executando o script uma vez
python -m src.scripts.extract_processos_gerados

# Ou use Alembic para criar via migration
alembic upgrade head
```

### Docker containers não sobem
```bash
# Verifique os logs
docker-compose logs -f postgres

# Reinicie os serviços
docker-compose down
docker-compose up -d
```

## Contribuindo

Este é um projeto de TCC. Para sugestões ou melhorias, abra uma issue.

## Licença

Este projeto é parte de um Trabalho de Conclusão de Curso (TCC).

## Contato

[Seu Nome] - [Seu Email]

---

**Leia também**: [ANALISE_TECNICA.md](ANALISE_TECNICA.md) para detalhes completos sobre arquitetura, decisões técnicas e cronograma do projeto.
