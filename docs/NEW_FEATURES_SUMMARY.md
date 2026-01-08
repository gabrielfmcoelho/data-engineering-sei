# Novas Funcionalidades - Filtro por Órgão

## ✅ O Que Foi Adicionado

### 1. Filtro por Órgão no Script de Consulta

**Arquivo modificado**: `src/scripts/fetch_processos_metadata.py`

**Nova funcionalidade**: Argumento `--orgao` para filtrar processos por órgão.

**Como usar**:
```bash
python -m src.scripts.fetch_processos_metadata \
    --id-unidade 123456 \
    --orgao SEAD-PI
```

#### Benefícios:

✅ **Processamento Seletivo**: Consulte apenas processos de um órgão específico
✅ **Resumo Automático**: Não consulta processos já consultados (continua de onde parou)
✅ **Estatísticas Detalhadas**: Mostra progresso específico do órgão
✅ **Paralelização**: Execute múltiplas instâncias para diferentes órgãos simultaneamente

#### Estatísticas Exibidas:

Quando você executa com `--orgao`, o script agora mostra:

```
Estatísticas:
  Total de processos no banco: 123,579
  Processos do órgão SEAD-PI: 8,456
  Já consultados (órgão): 2,340
  Pendentes (órgão): 6,116

Processos a consultar nesta execução: 6,116 (72.3%)
```

Isso permite:
- Saber quantos processos do órgão já foram consultados
- Ver quantos ainda faltam
- Continuar de onde parou sem re-processar

---

### 2. Script para Listar Órgãos Disponíveis

**Arquivo novo**: `src/scripts/list_orgaos.py`

**Função**: Listar todos os órgãos disponíveis no banco com estatísticas.

**Como usar**:

```bash
# Listar todos os órgãos
python -m src.scripts.list_orgaos
```

**Saída**:
```
═══════════════════════════════════════════════════════
     Órgãos Disponíveis no Banco - Estatísticas
═══════════════════════════════════════════════════════

┏━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━┓
┃ Órgão              ┃ Total Processos ┃ Consultados ┃ Com Erro ┃ Pendentes ┃ % Completo ┃
┡━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━┩
│ SEAD-PI            │         15,234 │       8,456 │        2 │     6,776 │      55.5% │
│ SEDUC-PI           │         12,456 │       3,200 │       10 │     9,246 │      25.7% │
│ SESAPI-PI          │         10,123 │         500 │        0 │     9,623 │       4.9% │
│ SEMAR-PI           │          8,901 │           0 │        0 │     8,901 │       0.0% │
│ ...                │            ... │         ... │      ... │       ... │       ...  │
└────────────────────┴────────────────┴─────────────┴──────────┴───────────┴────────────┘

Totais:
  Total de processos: 123,579
  Consultados: 45,234
  Com erro: 23
  Pendentes: 78,322
  % Completo: 36.6%
```

**Ver detalhes de um órgão**:
```bash
python -m src.scripts.list_orgaos --orgao SEAD-PI
```

**Saída**:
```
Detalhes do Órgão: SEAD-PI

Total de processos: 15,234

Status de Consulta:
  completed: 8,456
  pending: 6,776
  error: 2

Top 10 Unidades:
  SEAD-PI/GAB/SUPARC: 3,456
  SEAD-PI/SUGED/COORD: 2,123
  SEAD-PI/DIGES/SETOR: 1,890
  ...
```

---

## 📋 Casos de Uso

### 1. Processar Órgão por Órgão

```bash
# Passo 1: Ver quais órgãos existem
python -m src.scripts.list_orgaos

# Passo 2: Começar pela SEAD-PI
python -m src.scripts.fetch_processos_metadata \
    --id-unidade 123456 \
    --orgao SEAD-PI

# Passo 3: Depois SEDUC-PI
python -m src.scripts.fetch_processos_metadata \
    --id-unidade 123456 \
    --orgao SEDUC-PI
```

### 2. Processar em Paralelo

Execute em terminais separados:

**Terminal 1**:
```bash
python -m src.scripts.fetch_processos_metadata --id-unidade 123456 --orgao SEAD-PI
```

**Terminal 2**:
```bash
python -m src.scripts.fetch_processos_metadata --id-unidade 123456 --orgao SEDUC-PI
```

**Terminal 3**:
```bash
python -m src.scripts.fetch_processos_metadata --id-unidade 123456 --orgao SESAPI-PI
```

Cada terminal processa um órgão diferente simultaneamente!

### 3. Resumir Execução Interrompida

```bash
# Primeira execução (processa 5,000 processos e para)
python -m src.scripts.fetch_processos_metadata \
    --id-unidade 123456 \
    --orgao SEAD-PI \
    --limit 5000

# Saída:
# Consultados: 0
# Pendentes: 15,234
# Processos a consultar: 5,000
# ✓ Total salvos: 4,998/5,000

# Segunda execução (continua de onde parou)
python -m src.scripts.fetch_processos_metadata \
    --id-unidade 123456 \
    --orgao SEAD-PI

# Saída:
# Consultados: 4,998  ← Mostra que já tem 4,998 consultados
# Pendentes: 10,236
# Processos a consultar: 10,236  ← Continua dos restantes
```

O script **sempre resume** de onde parou, nunca re-processa processos já consultados!

---

## 🔍 Queries SQL Úteis

### Ver progresso por órgão:

```sql
SELECT
    SUBSTRING(p.unidade FROM '^[^/]+') as orgao,
    COUNT(*) as total,
    COUNT(CASE WHEN e.metadata_status = 'completed' THEN 1 END) as consultados,
    COUNT(CASE WHEN e.metadata_status = 'error' THEN 1 END) as erros,
    COUNT(CASE WHEN e.metadata_status IS NULL THEN 1 END) as pendentes
FROM sei_processos_temp_etl p
LEFT JOIN sei_etl_status e ON p.protocol = e.protocol
GROUP BY orgao
ORDER BY total DESC;
```

### Ver processos pendentes de um órgão:

```sql
SELECT
    p.protocol,
    p.unidade,
    p.data_hora
FROM sei_processos_temp_etl p
LEFT JOIN sei_etl_status e ON p.protocol = e.protocol
WHERE p.unidade LIKE 'SEAD-PI%'
  AND (e.metadata_status IS NULL OR e.metadata_status != 'completed')
ORDER BY p.data_hora DESC
LIMIT 10;
```

---

## 📝 Arquivos Criados/Modificados

### Código:
1. **`src/scripts/fetch_processos_metadata.py`** - ✏️ MODIFICADO
   - Adicionado parâmetro `--orgao`
   - Adicionadas estatísticas por órgão
   - Melhorado tracking de progresso

2. **`src/scripts/list_orgaos.py`** - ✅ NOVO
   - Lista todos os órgãos com estatísticas
   - Mostra detalhes de órgão específico

### Documentação:
3. **`docs/FILTRO_ORGAO_EXAMPLES.md`** - ✅ NOVO
   - Exemplos completos de uso do filtro
   - Casos de uso e queries SQL

4. **`docs/NEW_FEATURES_SUMMARY.md`** - ✅ NOVO
   - Este arquivo - resumo das funcionalidades

---

## 🎯 Exemplos Práticos

### Exemplo 1: Descobrir e Processar o Maior Órgão

```bash
# 1. Listar órgãos (já ordenados por tamanho)
python -m src.scripts.list_orgaos

# Saída mostra SEAD-PI com 15,234 processos no topo

# 2. Testar com 100 processos
python -m src.scripts.fetch_processos_metadata \
    --id-unidade 123456 \
    --orgao SEAD-PI \
    --limit 100

# 3. Se OK, processa tudo
python -m src.scripts.fetch_processos_metadata \
    --id-unidade 123456 \
    --orgao SEAD-PI
```

### Exemplo 2: Monitorar Progresso

```bash
# Terminal 1: Executa consulta
python -m src.scripts.fetch_processos_metadata \
    --id-unidade 123456 \
    --orgao SEAD-PI

# Terminal 2: Monitora progresso (executar periodicamente)
python -m src.scripts.list_orgaos --orgao SEAD-PI
```

### Exemplo 3: Processar Apenas Contratos da SEAD

```bash
# Consulta processos da SEAD
python -m src.scripts.fetch_processos_metadata \
    --id-unidade 123456 \
    --orgao SEAD-PI

# Depois, filtra apenas contratos no banco
SELECT *
FROM sei_processos
WHERE protocol IN (
    SELECT protocol
    FROM sei_processos_temp_etl
    WHERE unidade LIKE 'SEAD-PI%'
)
AND (
    tipo_procedimento ILIKE '%contrat%'
    OR tipo_procedimento ILIKE '%licit%'
    OR tipo_procedimento ILIKE '%pregão%'
);
```

---

## ✅ Validação

O script garante:
- ✅ Nunca consulta o mesmo processo duas vezes
- ✅ Pode ser interrompido e retomado a qualquer momento
- ✅ Estatísticas precisas em tempo real
- ✅ Suporte a execução paralela de múltiplos órgãos
- ✅ Tracking completo de progresso por órgão

---

**Status**: ✅ **IMPLEMENTADO E TESTADO**

As novas funcionalidades estão prontas para uso e totalmente documentadas!
