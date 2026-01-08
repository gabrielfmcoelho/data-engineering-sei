# Análise Técnica - TCC: Ontologia de Processos SEI-PI em Neo4J

## 1. VISÃO GERAL DO PROJETO

### Objetivo
Modelar ontologicamente processos do Sistema Eletrônico de Informações (SEI) do Estado do Piauí em um banco de grafos Neo4J, com foco em processos de contratação pública.

### Componentes Principais
1. **Extração de Dados**: API do SEI → MinIO/Postgres
2. **Modelagem Ontológica**: Neo4J (processos, unidades, documentos, entidades)
3. **Extração de Entidades**: ML/NLP dos documentos
4. **Análise**: Queries em grafo para insights sobre contratos

---

## 2. ARQUITETURA PROPOSTA

```
┌─────────────────────────────────────────────────────────────┐
│                     API SEI Estado do Piauí                 │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
         ┌───────────────────────────────┐
         │   ETL Pipeline (Python)       │
         │   - asyncio/aiohttp           │
         │   - multiprocessing           │
         │   - Coordenação: Redis        │
         └───────┬───────────────┬───────┘
                 │               │
        ┌────────▼─────┐  ┌──────▼──────┐
        │   MinIO      │  │  Postgres   │
        │ (Documentos) │  │  (Estado    │
        │              │  │   Pipeline) │
        └──────────────┘  └─────────────┘
                 │               │
                 └───────┬───────┘
                         ▼
         ┌───────────────────────────────┐
         │  Extração de Entidades (ML)   │
         │  - Qwen2-VL 7B / Llama 3.1    │
         │  - BERTimbau NER              │
         └───────────────┬───────────────┘
                         │
                         ▼
              ┌──────────────────┐
              │     Neo4J        │
              │  (Grafo          │
              │   Ontológico)    │
              └──────────────────┘
```

---

## 3. DECISÕES TECNOLÓGICAS

### ✅ Stack Recomendada

| Componente | Tecnologia | Justificativa |
|------------|------------|---------------|
| **Orquestração** | Docker Compose | Simples, reproduzível |
| **Banco Grafo** | Neo4J | Melhor para ontologias complexas |
| **Controle Pipeline** | PostgreSQL + Alembic | Confiável, migrações versionadas |
| **Coordenação ETL** | Redis | Fila de tarefas, locks distribuídos |
| **Armazenamento** | MinIO | S3-compatible, self-hosted |
| **ETL** | Python asyncio + multiprocessing | Suficiente para volume estadual |
| **Extração Entidades** | Qwen2-VL 7B + BERTimbau | Eficiente, português, local |
| **Migrações** | Alembic | Profissional, versionamento |

### ❌ Tecnologias NÃO Recomendadas

- **PySpark**: Overhead desnecessário para volume de dados estadual
- **Kafka**: Complexidade excessiva para pipeline batch
- **Airflow**: Pode ser futuro, mas complexo para TCC inicial

---

## 4. MODELO ONTOLÓGICO PROPOSTO

### Entidades Principais (Nós)

```cypher
// Processo
(:Processo {
  numero: String,
  tipo: String,
  data_abertura: DateTime,
  data_conclusao: DateTime,
  situacao: String,
  descricao: String
})

// Unidade Organizacional
(:Unidade {
  codigo: String,
  nome: String,
  sigla: String,
  tipo: String,  // setor, departamento, secretaria
  ativa: Boolean
})

// Documento
(:Documento {
  id: String,
  tipo: String,  // despacho, ofício, contrato, etc
  numero: String,
  data: DateTime,
  hash_conteudo: String,
  caminho_minio: String
})

// Pessoa (Servidor/Cidadão)
(:Pessoa {
  cpf: String,
  nome: String,
  tipo: String  // servidor, cidadao, empresa
})

// Entidades Extraídas de Documentos
(:Empresa {
  cnpj: String,
  razao_social: String,
  nome_fantasia: String
})

(:Valor {
  montante: Float,
  moeda: String,
  tipo: String  // estimado, contratado, pago
})

(:Prazo {
  data_inicio: DateTime,
  data_fim: DateTime,
  dias: Integer
})

(:ObjetoContrato {
  descricao: String,
  categoria: String  // obra, serviço, compra
})
```

### Relacionamentos (Arestas)

```cypher
// Tramitação
(:Processo)-[:TRAMITOU_PARA {
  data: DateTime,
  usuario: String,
  observacao: String
}]->(:Unidade)

// Hierarquia Organizacional
(:Unidade)-[:SUBORDINADA_A]->(:Unidade)

// Documentos
(:Processo)-[:CONTEM]->(:Documento)
(:Pessoa)-[:ASSINOU {cargo: String, data: DateTime}]->(:Documento)

// Contratos (específico)
(:Processo)-[:TIPO_CONTRATACAO {
  modalidade: String,  // pregao, dispensa, inexigibilidade
  numero_licitacao: String
}]->(:ObjetoContrato)

(:Processo)-[:CONTRATA]->(:Empresa)
(:Processo)-[:VALOR_TOTAL]->(:Valor)
(:Processo)-[:PRAZO_EXECUCAO]->(:Prazo)

// Entidades Mencionadas
(:Documento)-[:MENCIONA]->(:Empresa)
(:Documento)-[:MENCIONA]->(:Pessoa)
```

---

## 5. PIPELINE DE ETL

### Fases

1. **Download (Extraction)**
   - Processos metadata
   - Andamentos/atividades
   - Documentos (PDFs, DOCs, etc)

2. **Transform**
   - Normalização de dados
   - Extração de entidades (ML)
   - Classificação de documentos

3. **Load**
   - PostgreSQL (controle)
   - MinIO (arquivos)
   - Neo4J (grafo ontológico)

### Estados da Pipeline (Postgres)

```sql
-- processo: pendente → baixando_metadata → metadata_ok → baixando_docs → completo
-- documento: pendente → baixado → extraindo_entidades → entidades_ok → carregado_grafo
```

---

## 6. EXTRAÇÃO DE ENTIDADES

### Abordagem Híbrida Recomendada

**Fase 1 - Extração Básica (Regras + spaCy)**
- CPF/CNPJ: Regex
- Valores monetários: Regex + validação
- Datas: Regex + parsing
- Nomes próprios: spaCy pt_core_news_lg

**Fase 2 - Extração Semântica (LLM)**
- Qwen2-VL 7B para documentos escaneados (OCR + entidades)
- Llama 3.1 8B ou Qwen2.5 7B para textos extraídos
- Prompts específicos para contratos:
  ```
  Extraia do contrato:
  - Contratada (CNPJ, razão social)
  - Valor total
  - Objeto (descrição)
  - Prazo (início, fim)
  - Garantias
  ```

**Fase 3 - Validação**
- Comparação entre métodos
- Validação humana amostral (10%)
- Métricas: Precision, Recall, F1

### Modelos Específicos

| Tipo Documento | Modelo Recomendado | Alternativa |
|----------------|-------------------|-------------|
| PDF texto nativo | Llama 3.1 8B | GPT-4o-mini API |
| PDF escaneado | Qwen2-VL 7B | PaddleOCR + Llama |
| Entidades nomeadas | BERTimbau NER | spaCy pt_core_news_lg |
| Classificação doc | SetFit PT-BR | BERT fine-tuned |

---

## 7. CRONOGRAMA SUGERIDO (TCC)

### Fase 1 - Infraestrutura (2-3 semanas)
- [ ] Setup Docker Compose
- [ ] Schema Postgres + Alembic
- [ ] Cliente API SEI
- [ ] Pipeline básica de download

### Fase 2 - Ontologia (3-4 semanas)
- [ ] Pesquisa bibliográfica (OWL, ontologias governamentais)
- [ ] Modelagem Neo4J
- [ ] Validação com especialistas
- [ ] Carga inicial de dados

### Fase 3 - Extração ML (4-5 semanas)
- [ ] Setup modelos (Qwen2-VL, Llama)
- [ ] Pipeline de extração
- [ ] Validação e métricas
- [ ] Integração com Neo4J

### Fase 4 - Análise Contratos (3-4 semanas)
- [ ] Queries analíticas
- [ ] Visualizações (grafos)
- [ ] Insights e descobertas
- [ ] Casos de uso

### Fase 5 - Documentação (2 semanas)
- [ ] Redação TCC
- [ ] Apresentação
- [ ] Documentação técnica

**Total: 14-18 semanas (3.5-4.5 meses)**

---

## 8. RISCOS E MITIGAÇÕES

| Risco | Probabilidade | Impacto | Mitigação |
|-------|---------------|---------|-----------|
| API SEI instável | Média | Alto | Cache agressivo, retry automático |
| Volume dados maior que esperado | Média | Médio | Começar subset, escalar gradualmente |
| Modelos ML insuficientes | Baixa | Alto | Abordagem híbrida (regras + ML) |
| Falta validação especialistas | Média | Alto | Engajar órgãos públicos cedo |
| Dados sensíveis/LGPD | Alta | Crítico | Anonimização, termo autorização formal |

---

## 9. DIFERENCIAIS ACADÊMICOS

1. **Originalidade**: Primeira ontologia formal para SEI
2. **Aplicação Prática**: Uso real em órgão público
3. **Interdisciplinar**: Computação + Direito Administrativo + Ciência Dados
4. **Publicações Potenciais**:
   - Artigo sobre ontologia (SBBD, ONTOBRAS)
   - Artigo sobre extração ML em documentos públicos (PROPOR, BRACIS)
   - Dataset anotado para comunidade

---

## 10. PRÓXIMOS PASSOS IMEDIATOS

1. ✅ Obter autorização formal do Estado do Piauí
2. ✅ Definir subset inicial de processos (ex: contratos 2023-2024)
3. ✅ Setup ambiente desenvolvimento
4. ✅ Validar acesso API SEI
5. ✅ Contatar especialistas para validação ontologia

---

**Avaliação Final: PROJETO VIÁVEL E EXCELENTE PARA TCC** 🎓

O escopo é ambicioso mas realizável. A combinação de ontologia + ML + dados reais traz grande valor acadêmico e prático.
