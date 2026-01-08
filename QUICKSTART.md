# Guia Rápido de Início

## Setup Automático (Linux/Mac)

```bash
chmod +x setup.sh
./setup.sh
```

## Setup Manual

### 1. Instalar dependências

```bash
python -m venv venv
source venv/bin/activate  # Linux/Mac
pip install -r requirements.txt
```

### 2. Configurar credenciais

```bash
cp .env.example .env
nano .env  # ou use seu editor preferido
```

Edite especialmente estas linhas:

```env
SEI_DB_HOST=seu_host_sei.pi.gov.br
SEI_DB_USER=seu_usuario
SEI_DB_PASSWORD=sua_senha
```

### 3. Iniciar serviços

```bash
docker-compose up -d
```

### 4. Executar extração de processos

```bash
python -m src.scripts.extract_processos_gerados
```

## Comandos Úteis

### Docker

```bash
# Iniciar todos os serviços
docker-compose up -d

# Ver logs
docker-compose logs -f

# Parar serviços
docker-compose down

# Parar e remover volumes (CUIDADO: apaga dados!)
docker-compose down -v
```

### Conectar ao PostgreSQL local

```bash
docker exec -it sei-ontology-postgres psql -U sei_user -d sei_ontology
```

Queries úteis:

```sql
-- Ver total de processos extraídos
SELECT COUNT(*) FROM sei_processos_temp_etl;

-- Ver distribuição por tipo de procedimento
SELECT tipo_procedimento, COUNT(*) as total
FROM sei_processos_temp_etl
GROUP BY tipo_procedimento
ORDER BY total DESC;

-- Ver processos mais recentes
SELECT * FROM sei_processos_temp_etl
ORDER BY data_hora DESC
LIMIT 10;
```

### Acessar Neo4J

1. Abra: http://localhost:7474
2. Conecte com:
   - **URL**: bolt://localhost:7687
   - **Usuário**: neo4j
   - **Senha**: sei_neo4j_password

### Acessar MinIO

1. Abra: http://localhost:9001
2. Login:
   - **Usuário**: minioadmin
   - **Senha**: minioadmin123

## Próximos Scripts

Após executar `extract_processos_gerados.py`, você terá uma lista de processos na tabela `sei_processos_temp_etl`.

Os próximos scripts a serem desenvolvidos:

1. **extract_andamentos.py**: Baixa andamentos/atividades de cada processo
2. **extract_documentos.py**: Baixa documentos de cada processo
3. **load_neo4j.py**: Carrega dados no grafo Neo4J
4. **extract_entities.py**: Extrai entidades dos documentos usando ML

## Troubleshooting

### Erro: "could not connect to server"

```bash
# Verifique se o Postgres está rodando
docker-compose ps

# Veja os logs do Postgres
docker-compose logs postgres

# Reinicie o container se necessário
docker-compose restart postgres
```

### Erro: "ModuleNotFoundError"

```bash
# Certifique-se de estar no ambiente virtual
source venv/bin/activate

# Reinstale as dependências
pip install -r requirements.txt
```

### Erro: "permission denied" no setup.sh

```bash
chmod +x setup.sh
```

### Docker ocupa muito espaço

```bash
# Limpar containers e imagens não utilizados
docker system prune -a

# Ver uso de espaço
docker system df
```

## Estrutura de Diretórios Criados

```
tcc/
├── logs/                    # Logs da aplicação (criado automaticamente)
├── data/
│   ├── raw/                # Dados brutos extraídos
│   └── processed/          # Dados processados
└── venv/                   # Ambiente virtual Python
```

## Variáveis de Ambiente Importantes

| Variável | Descrição | Padrão |
|----------|-----------|--------|
| `BATCH_SIZE` | Tamanho do lote de extração | 1000 |
| `MAX_WORKERS` | Número de workers paralelos | 4 |
| `SEI_DB_SCHEMA` | Schema do banco SEI | sei_processo |

## Monitoramento

### Ver progresso da extração

O script mostra uma barra de progresso em tempo real:

```
Extraindo e carregando (batch size: 1000)... ━━━━━━━━━━━━━━━━━━━━━ 45% 6834/15243
```

### Ver logs detalhados

```bash
tail -f logs/extract_processos_gerados_*.log
```

## Performance

Com as configurações padrão:

- **Batch size**: 1000 registros
- **Throughput médio**: ~500-1000 registros/segundo
- **15k processos**: ~30-60 segundos

Ajuste `BATCH_SIZE` no `.env` se necessário:

```env
# Para redes mais lentas
BATCH_SIZE=500

# Para redes mais rápidas
BATCH_SIZE=2000
```

## Backup

### Backup do PostgreSQL

```bash
docker exec sei-ontology-postgres pg_dump -U sei_user sei_ontology > backup.sql
```

### Restaurar backup

```bash
cat backup.sql | docker exec -i sei-ontology-postgres psql -U sei_user -d sei_ontology
```

### Backup do Neo4J

```bash
docker exec sei-ontology-neo4j neo4j-admin dump --database=neo4j --to=/backups/neo4j.dump
```

---

**Precisa de ajuda?** Consulte o [README.md](README.md) completo ou a [ANALISE_TECNICA.md](ANALISE_TECNICA.md) para detalhes arquiteturais.
