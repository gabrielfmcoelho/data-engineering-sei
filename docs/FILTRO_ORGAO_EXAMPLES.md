# Exemplos de Uso - Filtro por Órgão

## Funcionalidade

O script `fetch_processos_metadata.py` agora suporta filtrar processos por órgão usando o argumento `--orgao`.

Isso permite:
- ✅ Consultar apenas processos de um órgão específico (ex: SEAD-PI)
- ✅ Processar órgãos separadamente em paralelo
- ✅ Resumir de onde parou (não consulta processos já consultados)
- ✅ Ver estatísticas por órgão

---

## Como Funciona

O filtro usa a coluna `unidade` da tabela `sei_processos_temp_etl`, que contém valores como:
- `SEAD-PI/GAB/SUPARC`
- `SEDUC-PI/SUGED/02GRE`
- `SESAPI-PI/DIGES/COORDI`

Quando você especifica `--orgao SEAD-PI`, o script busca todos os processos onde `unidade` começa com `SEAD-PI`.

---

## Exemplos de Uso

### 1. Consultar apenas processos da SEAD-PI

```bash
python -m src.scripts.fetch_processos_metadata \
    --id-unidade 123456 \
    --orgao SEAD-PI
```

**Saída esperada:**
```
═══════════════════════════════════════════════════════════
  Consulta de Metadados via API SEI - Execução Paralela
═══════════════════════════════════════════════════════════

Filtro ativo: Órgão = SEAD-PI

Estatísticas:
  Total de processos no banco: 123,579
  Processos do órgão SEAD-PI: 8,456
  Já consultados (órgão): 0
  Pendentes (órgão): 8,456

Processos a consultar nesta execução: 8,456 (100.0%)

⠼ Consultando processos (lote: 50)... ━━━━━━━━━━━━━━━━━━━━━━ 45% 3,800/8,456 ETA: 00:12:30
```

---

### 2. Consultar SEDUC-PI com limite de teste

```bash
python -m src.scripts.fetch_processos_metadata \
    --id-unidade 123456 \
    --orgao SEDUC-PI \
    --limit 500
```

Consulta apenas os primeiros 500 processos da SEDUC-PI.

---

### 3. Resumir de onde parou

**Primeira execução:**
```bash
python -m src.scripts.fetch_processos_metadata \
    --id-unidade 123456 \
    --orgao SEAD-PI \
    --limit 1000
```

**Saída:**
```
Processos do órgão SEAD-PI: 8,456
Já consultados (órgão): 0
Pendentes (órgão): 8,456

Processos a consultar nesta execução: 1,000 (11.8%)
(Limitado a 1,000 pela opção --limit)

✓ Consulta concluída!
  Total de processos salvos: 998/1,000
```

**Segunda execução (continua de onde parou):**
```bash
python -m src.scripts.fetch_processos_metadata \
    --id-unidade 123456 \
    --orgao SEAD-PI \
    --limit 1000
```

**Saída:**
```
Processos do órgão SEAD-PI: 8,456
Já consultados (órgão): 998
Pendentes (órgão): 7,458

Processos a consultar nesta execução: 1,000 (11.8%)

⠼ Consultando processos...
```

Note que agora mostra **998 já consultados** e continua dos próximos.

---

### 4. Consultar todos os órgãos sem filtro

```bash
python -m src.scripts.fetch_processos_metadata \
    --id-unidade 123456
```

Sem `--orgao`, consulta todos os processos de todos os órgãos.

---

## Processamento Paralelo por Órgão

Você pode rodar múltiplas instâncias do script em paralelo, cada uma para um órgão diferente:

**Terminal 1 - SEAD-PI:**
```bash
python -m src.scripts.fetch_processos_metadata \
    --id-unidade 123456 \
    --orgao SEAD-PI
```

**Terminal 2 - SEDUC-PI:**
```bash
python -m src.scripts.fetch_processos_metadata \
    --id-unidade 123456 \
    --orgao SEDUC-PI
```

**Terminal 3 - SESAPI-PI:**
```bash
python -m src.scripts.fetch_processos_metadata \
    --id-unidade 123456 \
    --orgao SESAPI-PI
```

Cada instância processa um órgão diferente simultaneamente, acelerando o processo geral.

---

## Listar Órgãos Disponíveis

Para ver quais órgãos existem no banco:

```sql
-- Top 20 órgãos por quantidade de processos
SELECT
    SUBSTRING(unidade FROM '^[^/]+') as orgao,
    COUNT(*) as total_processos
FROM sei_processos_temp_etl
GROUP BY orgao
ORDER BY total_processos DESC
LIMIT 20;
```

**Resultado esperado:**
```
   orgao    | total_processos
------------+-----------------
 SEAD-PI    |          15,234
 SEDUC-PI   |          12,456
 SESAPI-PI  |          10,123
 SEMAR-PI   |           8,901
 ...
```

---

## Monitoramento por Órgão

### Ver progresso de um órgão específico:

```sql
SELECT
    COUNT(DISTINCT p.protocol) as total_processos,
    COUNT(DISTINCT CASE WHEN e.metadata_status = 'completed' THEN p.protocol END) as consultados,
    COUNT(DISTINCT CASE WHEN e.metadata_status IS NULL THEN p.protocol END) as pendentes
FROM sei_processos_temp_etl p
LEFT JOIN sei_etl_status e ON p.protocol = e.protocol
WHERE p.unidade LIKE 'SEAD-PI%';
```

### Ver órgãos com mais processos consultados:

```sql
SELECT
    SUBSTRING(p.unidade FROM '^[^/]+') as orgao,
    COUNT(*) as total,
    COUNT(CASE WHEN e.metadata_status = 'completed' THEN 1 END) as consultados,
    ROUND(
        100.0 * COUNT(CASE WHEN e.metadata_status = 'completed' THEN 1 END) / COUNT(*),
        1
    ) as percentual
FROM sei_processos_temp_etl p
LEFT JOIN sei_etl_status e ON p.protocol = e.protocol
GROUP BY orgao
ORDER BY consultados DESC
LIMIT 10;
```

**Resultado:**
```
   orgao    | total  | consultados | percentual
------------+--------+-------------+------------
 SEAD-PI    | 15,234 |       8,456 |       55.5
 SEDUC-PI   | 12,456 |       3,200 |       25.7
 SESAPI-PI  | 10,123 |         500 |        4.9
 ...
```

---

## Casos de Uso

### 1. Priorizar Órgãos Importantes

```bash
# Primeiro: consulta processos de contratação da SEAD
python -m src.scripts.fetch_processos_metadata \
    --id-unidade 123456 \
    --orgao SEAD-PI

# Depois: outros órgãos
python -m src.scripts.fetch_processos_metadata \
    --id-unidade 123456 \
    --orgao SEDUC-PI
```

### 2. Processar em Etapas

```bash
# Etapa 1: Testa com 100 processos
python -m src.scripts.fetch_processos_metadata \
    --id-unidade 123456 \
    --orgao SEAD-PI \
    --limit 100

# Etapa 2: Se OK, faz mais 1000
python -m src.scripts.fetch_processos_metadata \
    --id-unidade 123456 \
    --orgao SEAD-PI \
    --limit 1000

# Etapa 3: Completa o órgão
python -m src.scripts.fetch_processos_metadata \
    --id-unidade 123456 \
    --orgao SEAD-PI
```

### 3. Reprocessar Órgão com Erros

```bash
# Primeiro, marca processos com erro como pendentes
psql -h localhost -U sei_user -d sei_ontology -c "
UPDATE sei_etl_status e
SET metadata_status = 'pending', retry_count = 0
FROM sei_processos_temp_etl p
WHERE e.protocol = p.protocol
  AND e.metadata_status = 'error'
  AND p.unidade LIKE 'SEAD-PI%';
"

# Depois, reprocessa
python -m src.scripts.fetch_processos_metadata \
    --id-unidade 123456 \
    --orgao SEAD-PI
```

---

## Otimizações

### Aumentar performance para um órgão específico:

```bash
# .env
SEI_API_MAX_CONCURRENT=20  # Aumenta concorrência

# Execute
python -m src.scripts.fetch_processos_metadata \
    --id-unidade 123456 \
    --orgao SEAD-PI \
    --batch-size 100  # Lotes maiores
```

---

## Troubleshooting

### Erro: Nenhum processo encontrado

```
✓ Nenhum processo pendente para consultar!
Todos os processos do filtro já foram consultados.
```

**Solução**: Todos os processos do órgão já foram consultados com sucesso! ✅

### Filtro retorna 0 processos

Verifique se o nome do órgão está correto:

```sql
-- Ver órgãos exatos
SELECT DISTINCT SUBSTRING(unidade FROM '^[^/]+') as orgao
FROM sei_processos_temp_etl
ORDER BY orgao;
```

### Quer consultar subunidades específicas

Use um filtro mais específico:

```bash
# Apenas processos do GAB da SEAD
python -m src.scripts.fetch_processos_metadata \
    --id-unidade 123456 \
    --orgao SEAD-PI/GAB
```

---

**Resumo**: O filtro `--orgao` permite processar processos de forma incremental e organizada por órgão, com controle total de progresso e capacidade de resumir execuções interrompidas.
