# Guia de Uso - Integração com API do SEI

## Visão Geral

Este guia explica como usar os scripts de integração com a API REST do SEI do Estado do Piauí para consultar metadados de processos e baixar documentos.

## Pipeline Completa

```
1. extract_processos_gerados.py     → Extrai lista de processos do banco SEI
2. fetch_processos_metadata.py      → Consulta metadados via API (paralelo)
3. download_documentos.py           → Baixa documentos para MinIO (paralelo)
```

---

## Pré-requisitos

### 1. Configurar credenciais da API

Edite o arquivo `.env` com suas credenciais:

```bash
# API SEI
SEI_API_BASE_URL=https://api.sei.pi.gov.br
SEI_API_USER=seu.usuario@orgao.pi.gov.br
SEI_API_PASSWORD=sua_senha_da_api
SEI_API_ORGAO=GOV-PI
SEI_API_ID_UNIDADE=123456  # ID da sua unidade no SEI
```

**Como obter o ID da unidade:**
```bash
# Liste as unidades disponíveis
python -c "
import asyncio
from src.api.sei_client import SeiAPIClient
from src.config import settings

async def list_units():
    async with SeiAPIClient(
        base_url=settings.sei_api_base_url,
        usuario=settings.sei_api_user,
        senha=settings.sei_api_password,
        orgao=settings.sei_api_orgao
    ) as client:
        units = await client.listar_unidades()
        for u in units:
            print(f\"{u.get('IdUnidade')}: {u.get('Sigla')} - {u.get('Descricao')}\")

asyncio.run(list_units())
"
```

### 2. Instalar dependências

```bash
pip install -r requirements.txt
```

### 3. Iniciar serviços Docker

```bash
docker-compose up -d
```

---

## Uso dos Scripts

### Script 1: Extrair Lista de Processos do Banco SEI

**Primeiro**, extraia a lista de processos do banco SEI (tabela `sei_atividades`):

```bash
python -m src.scripts.extract_processos_gerados
```

Isso popula a tabela `sei_processos_temp_etl` com ~123,579 processos.

---

### Script 2: Consultar Metadados via API

**Depois**, consulte os metadados completos de cada processo via API:

```bash
python -m src.scripts.fetch_processos_metadata \
    --id-unidade 123456 \
    --batch-size 50 \
    --limit 1000  # opcional: testar com os primeiros 1000
```

#### Parâmetros:

| Parâmetro | Obrigatório | Padrão | Descrição |
|-----------|-------------|--------|-----------|
| `--id-unidade` | ✅ Sim | - | ID da unidade SEI (ex: 123456) |
| `--batch-size` | ❌ Não | 50 | Tamanho do lote para processamento paralelo |
| `--limit` | ❌ Não | None | Limite de processos a consultar (None = todos) |

#### O que faz:

1. Lê processos da tabela `sei_processos_temp_etl` que ainda não foram consultados
2. Para cada processo, consulta via API:
   - **Metadados do processo** (`/v1/unidades/{id}/procedimentos/consulta`)
   - **Lista de documentos** (`/v1/unidades/{id}/procedimentos/documentos`)
   - **Lista de andamentos** (`/v1/unidades/{id}/procedimentos/andamentos`)
3. Salva tudo no Postgres:
   - Tabela `sei_processos`: metadados completos
   - Tabela `sei_documentos`: lista de documentos (status='pending')
   - Tabela `sei_andamentos`: histórico de tramitação
   - Tabela `sei_etl_status`: controle de pipeline

#### Exemplo de saída:

```
═══════════════════════════════════════════════════════════
  Consulta de Metadados via API SEI - Execução Paralela
═══════════════════════════════════════════════════════════

Total de processos a consultar: 123,579

⠼ Consultando processos (lote: 50)... ━━━━━━━━━━━━━━━━━━━━━━ 45% 55,600/123,579 ETA: 01:23:45

✓ Consulta concluída!
  Total de processos salvos: 123,450/123,579
```

#### Performance esperada:

- **Concorrência**: 10 requisições simultâneas (configurável em `.env`)
- **Throughput**: 50-100 processos/minuto
- **Tempo estimado para 123k processos**: ~20-40 horas

#### Monitoramento:

```sql
-- Ver progresso da consulta
SELECT
    metadata_status,
    COUNT(*) as total
FROM sei_etl_status
GROUP BY metadata_status;

-- Processos com erro
SELECT protocol, metadata_error
FROM sei_etl_status
WHERE metadata_status = 'error';

-- Total de documentos encontrados
SELECT COUNT(*) FROM sei_documentos;
```

---

### Script 3: Download de Documentos para MinIO

**Por último**, baixe os documentos e salve no MinIO:

```bash
python -m src.scripts.download_documentos \
    --id-unidade 123456 \
    --batch-size 20 \
    --limit 500  # opcional: testar com os primeiros 500
```

#### Parâmetros:

| Parâmetro | Obrigatório | Padrão | Descrição |
|-----------|-------------|--------|-----------|
| `--id-unidade` | ✅ Sim | - | ID da unidade SEI |
| `--batch-size` | ❌ Não | 20 | Tamanho do lote para download paralelo |
| `--limit` | ❌ Não | None | Limite de documentos a baixar (None = todos) |

#### O que faz:

1. Lê documentos com `status='pending'` da tabela `sei_documentos`
2. Para cada documento:
   - Baixa via API (`/v1/unidades/{id}/documentos/baixar`)
   - Calcula SHA256 do conteúdo
   - Salva no MinIO: `sei-documentos/{protocol}/{id_documento}.pdf`
   - Atualiza status no Postgres para `'completed'`
3. Atualiza `sei_etl_status` para marcar processos com todos documentos baixados

#### Exemplo de saída:

```
═══════════════════════════════════════════════════════════
  Download de Documentos para MinIO - Execução Paralela
═══════════════════════════════════════════════════════════

Inicializando MinIO...
✓ MinIO pronto

Total de documentos a baixar: 456,789

⠼ Baixando documentos (lote: 20)... ━━━━━━━━━━━━━━━━━━━━━━ 32% 146,000/456,789 ETA: 04:12:30

✓ Download concluído!
  Documentos baixados com sucesso: 456,234/456,789
```

#### Performance esperada:

- **Concorrência**: 5 downloads simultâneos (configurável em `.env`)
- **Throughput**: 10-20 documentos/minuto (depende do tamanho)
- **Retry**: Até 3 tentativas por documento

#### Estrutura no MinIO:

```
sei-documentos/
├── 00123-456789-2024-01/
│   ├── 98765.pdf
│   ├── 98766.pdf
│   └── 98767.pdf
├── 00987-654321-2024-02/
│   ├── 12345.pdf
│   └── 12346.pdf
└── ...
```

#### Monitoramento:

```sql
-- Ver progresso do download
SELECT
    status,
    COUNT(*) as total,
    SUM(tamanho_bytes) as total_bytes
FROM sei_documentos
GROUP BY status;

-- Documentos com erro
SELECT
    protocol,
    id_documento,
    download_attempts,
    last_error
FROM sei_documentos
WHERE status = 'error';

-- Processos completos (todos documentos baixados)
SELECT COUNT(*)
FROM sei_etl_status
WHERE documentos_status = 'completed';
```

---

## Tratamento de Erros e Retry

### Erros comuns:

1. **401 Unauthorized**
   - Token expirado
   - Solução: O cliente reautentica automaticamente

2. **429 Too Many Requests**
   - Rate limit atingido
   - Solução: Backoff automático de 5 segundos

3. **404 Not Found**
   - Processo/documento não existe
   - Solução: Marca como erro (não retenta)

4. **500 Server Error**
   - Erro no servidor SEI
   - Solução: Retry com backoff exponencial (até 3 tentativas)

5. **Timeout**
   - Requisição demorou muito
   - Solução: Retry automático

### Reprocessamento manual:

```sql
-- Reprocessar processos com erro de metadata
UPDATE sei_etl_status
SET metadata_status = 'pending', retry_count = 0
WHERE metadata_status = 'error';

-- Reprocessar documentos com erro
UPDATE sei_documentos
SET status = 'pending', download_attempts = 0
WHERE status = 'error' AND download_attempts < 3;
```

---

## Otimização e Tunning

### Para redes mais rápidas:

```bash
# .env
SEI_API_MAX_CONCURRENT=20  # Aumenta concorrência de consultas
SEI_API_MAX_CONCURRENT_DOWNLOADS=10  # Aumenta downloads paralelos
```

### Para redes mais lentas/instáveis:

```bash
# .env
SEI_API_MAX_CONCURRENT=5  # Reduz concorrência
SEI_API_TIMEOUT=60  # Aumenta timeout
```

### Execução em etapas:

```bash
# 1. Consulta apenas 1000 processos primeiro (teste)
python -m src.scripts.fetch_processos_metadata --id-unidade 123456 --limit 1000

# 2. Se OK, consulta mais 10k
python -m src.scripts.fetch_processos_metadata --id-unidade 123456 --limit 10000

# 3. Depois roda completo
python -m src.scripts.fetch_processos_metadata --id-unidade 123456
```

---

## Logs e Debugging

### Localização dos logs:

```
logs/
├── extract_processos_gerados_2024-12-26_22-30-00.log
├── fetch_processos_metadata_2024-12-26_23-00-00.log
└── download_documentos_2024-12-26_23-45-00.log
```

### Ver logs em tempo real:

```bash
tail -f logs/fetch_processos_metadata_*.log
```

### Nível de log:

- **Console**: INFO (progresso geral)
- **Arquivo**: DEBUG (detalhes de cada requisição)

---

## Queries Úteis

### Dashboard de progresso:

```sql
-- Visão geral da pipeline
SELECT
    'Processos Extraídos' as etapa,
    COUNT(*) as total
FROM sei_processos_temp_etl

UNION ALL

SELECT
    'Metadados Consultados',
    COUNT(*)
FROM sei_etl_status
WHERE metadata_status = 'completed'

UNION ALL

SELECT
    'Documentos Encontrados',
    COUNT(*)
FROM sei_documentos

UNION ALL

SELECT
    'Documentos Baixados',
    COUNT(*)
FROM sei_documentos
WHERE status = 'completed';
```

### Processos de contratação:

```sql
-- Filtrar processos de contratação
SELECT
    protocol,
    tipo_procedimento,
    especificacao,
    data_abertura
FROM sei_processos
WHERE tipo_procedimento ILIKE '%contrat%'
   OR tipo_procedimento ILIKE '%licitação%'
   OR tipo_procedimento ILIKE '%pregão%'
ORDER BY data_abertura DESC;
```

---

## Troubleshooting

### Erro: "Token inválido"

```bash
# Verifique as credenciais
python -c "
import asyncio
from src.api.sei_client import SeiAPIClient
from src.config import settings

async def test_auth():
    async with SeiAPIClient(
        base_url=settings.sei_api_base_url,
        usuario=settings.sei_api_user,
        senha=settings.sei_api_password,
        orgao=settings.sei_api_orgao
    ) as client:
        token = await client._get_token()
        print(f'Token obtido com sucesso: {token[:20]}...')

asyncio.run(test_auth())
"
```

### Erro: "Bucket not found"

```bash
# Crie o bucket manualmente
docker exec sei-ontology-minio mc mb /data/sei-documentos
```

### Performance muito lenta

1. Verifique latência para API:
   ```bash
   ping api.sei.pi.gov.br
   ```

2. Reduza concorrência:
   ```bash
   # .env
   SEI_API_MAX_CONCURRENT=5
   ```

3. Execute em horário de menor uso (madrugada)

---

**Precisa de ajuda?** Consulte também:
- `README.md` - Documentação geral
- `ANALISE_TECNICA.md` - Arquitetura detalhada
