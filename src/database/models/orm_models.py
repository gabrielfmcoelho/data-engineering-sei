"""Modelos SQLAlchemy para o banco local (destino).

IMPORTANTE: Apenas modelos que devem ser criados no banco LOCAL.
Modelos para leitura do banco SEI estão em declarative_models.py
"""
from sqlalchemy import Column, String, DateTime, Integer, BigInteger, Text, Boolean, JSON
from sqlalchemy import ForeignKey, Index
from sqlalchemy.orm import relationship
from datetime import datetime
from ..base import ORMBase

# -------------------- TABELAS DE ETL --------------------
class SeiProcessoTempETL(ORMBase):
    """Modelo para tabela temporária de processos no banco local (destino)."""
    __tablename__ = 'sei_processos_temp_etl'

    id = Column(Integer, primary_key=True, autoincrement=True)
    protocol = Column(String(50), nullable=False, index=True)
    id_protocolo = Column(String(50), nullable=False, index=True)
    data_hora = Column(DateTime, nullable=False)
    tipo_procedimento = Column(String(255))
    unidade = Column(String(255))
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    def __repr__(self):
        return f"<SeiProcessoTempETL(protocol={self.protocol}, id_protocolo={self.id_protocolo})>"

class SeiETLStatus(ORMBase):
    """Controle de estado da pipeline ETL."""

    __tablename__ = 'sei_etl_status'

    id = Column(Integer, primary_key=True, autoincrement=True)
    protocol = Column(String(50), unique=True, nullable=False, index=True)

    # Status de cada etapa
    metadata_status = Column(String(50), default='pending', index=True)  # pending, processing, completed, error
    metadata_fetched_at = Column(DateTime)
    metadata_error = Column(Text)

    documentos_status = Column(String(50), default='pending', index=True)
    documentos_total = Column(Integer, default=0)
    documentos_downloaded = Column(Integer, default=0)
    documentos_error = Column(Text)

    andamentos_status = Column(String(50), default='pending', index=True)
    andamentos_total = Column(Integer, default=0)
    andamentos_error = Column(Text)

    # Retry control
    retry_count = Column(Integer, default=0)
    last_retry_at = Column(DateTime)
    next_retry_at = Column(DateTime)

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    def __repr__(self):
        return f"<SeiETLStatus(protocol={self.protocol}, metadata={self.metadata_status}, docs={self.documentos_status})>"

# -------------------- PESSOAS FISICAS/JURIDICAS/USUARIOS --------------------
class PessoaFisica(ORMBase):
    """Modelo para pessoas envolvidas em processos SEI no banco local (destino)."""
    __tablename__ = 'pessoa_fisica'

    id = Column(Integer, primary_key=True, autoincrement=True)
    raw_nome_pesssoa = Column(String(255), nullable=False)
    nome_pessoa = Column(String(255), nullable=False)
    email_pessoa = Column(String(255)) # sigla
    cpf = Column(String(14), unique=True, index=True)
    tipo_pessoa = Column(String(50))
    id_sei_usuario = Column(BigInteger, unique=True, nullable=False, index=True)
    sei_matricula = Column(String(50))

# -------------------- ORGANOGRAMA --------------------
class SeiOrgao(ORMBase):
    """Modelo para órgãos SEI no banco local (destino)."""
    __tablename__ = 'sei_orgaos'

    id = Column(Integer, primary_key=True, autoincrement=True)
    nome_orgao = Column(String(255), nullable=False)
    sigla_orgao = Column(String(50))
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    def __repr__(self):
        return f"<SeiOrgao(nome={self.sigla_orgao})>"

class SeiUnidade(ORMBase):
    """Modelo para unidades SEI no banco local (destino)."""
    __tablename__ = 'sei_unidades'

    id = Column(Integer, primary_key=True, autoincrement=True)
    id_sei_unidade = Column(Integer, unique=True, nullable=False, index=True)
    sigla_unidade = Column(String(50))
    nome_unidade = Column(String(255), nullable=False)
    nivel_unidade = Column(Integer)
    id_orgao = Column(Integer, ForeignKey('sei_orgaos.id_orgao'))
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
)

    def __repr__(self):
        return f"<SeiUnidade(sei_id_unidade={self.sei_id_unidade}, nome={self.nome_unidade})>"

# -------------------- METADADOS DE PROCESSOS --------------------
class SeiAssuntoProcesso(ORMBase):
    """Modelo para assuntos de processos SEI no banco local (destino)."""
    __tablename__ = 'sei_assuntos_processos'

    id = Column(Integer, primary_key=True, autoincrement=True)
    descricao_assunto = Column(String(255), nullable=False, index=True)
    codigo_sei_assunto = Column(String(50), nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    def __repr__(self):
        return f"<SeiAssuntoProcesso(nome={self.nome_assunto})>"

class SeiTipoProcesso(ORMBase):
    """Modelo para tipos de processos SEI no banco local (destino)."""
    __tablename__ = 'sei_tipos_processos'

    id = Column(Integer, primary_key=True, autoincrement=True)
    descricao_tipo = Column(String(255), nullable=False, index=True, unique=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    def __repr__(self):
        return f"<SeiTipoProcesso(nome={self.nome_tipo})>"

class SeiProcessoStatus(ORMBase):
    """Modelo para status de processos SEI no banco local (destino)."""
    __tablename__ = 'sei_status_processos'

    id = Column(Integer, primary_key=True, autoincrement=True)
    descricao_status = Column(String(255), nullable=False, index=True, unique=True) # Aberto, Pausado, Concluído
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    def __repr__(self):
        return f"<SeiProcessoStatus(nome={self.nome_status})>"

class SeiProcesso(ORMBase):
    """Metadados completos dos processos consultados via API SEI."""
    __tablename__ = 'sei_processos'

    id = Column(Integer, primary_key=True, autoincrement=True)
    protocol = Column(String(50), unique=True, nullable=False, index=True)
    id_protocolo = Column(BigInteger, index=True)
    id_unidade = Column(Integer)

    # Metadados básicos
    tipo_procedimento = Column(String(255))
    especificacao = Column(Text)
    nivel_acesso = Column(String(50))  # Público, Restrito, Sigiloso
    hipotese_legal = Column(Text)
    observacao = Column(Text)

    # Datas
    data_abertura = Column(DateTime, index=True)
    data_conclusao = Column(DateTime)

    # Arrays JSON
    interessados = Column(JSON)  # Array de interessados
    assuntos = Column(JSON)  # Array de assuntos

    # Unidades
    unidade_geradora = Column(String(255))
    unidade_atual = Column(String(255))

    # Auditoria e dados brutos
    raw_api_response = Column(JSON)  # Resposta completa da API (para auditoria/debug)

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    fetched_at = Column(DateTime)  # Quando foi consultado na API

    # Relationships
    documentos = relationship("SeiDocumento", back_populates="processo", cascade="all, delete-orphan")
    andamentos = relationship("SeiAndamento", back_populates="processo", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<SeiProcesso(protocol={self.protocol}, tipo={self.tipo_procedimento})>"

# -------------------- PROCESSOS ABERTOS POR UNIDADE --------------------
class SeiD0ProcessoUnidadeAberta(ORMBase):
    """Modelo para processos em aberto por unidade no banco local (destino)."""
    __tablename__ = 'sei_d0_processos_unidade_aberta'

    id = Column(Integer, primary_key=True, autoincrement=True)
    id_sei_unidade = Column(Integer, index=True)
    protocolo_formatado = Column(String(50), unique=True, nullable=False, index=True)
    id_sei_protocolo = Column(BigInteger, index=True, unique=True, nullable=False)
    id_usuario_atribuidor = Column(BigInteger, index=True)  # Pessoa fisica atribuida

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    def __repr__(self):
        return f"<SeiD0ProcessoUnidadeAberta(unidade={self.id_sei_unidade}, protocolo={self.protocolo_formatado})>"

# -------------------- METADADOS DE DOCUMENTOS --------------------
class SeiDocumentoTipo(ORMBase):
    """Modelo para tipos de documentos SEI (nomeados originalmente de "serie") no banco local (destino)."""
    __tablename__ = 'sei_documentos_tipos'

    id = Column(Integer, primary_key=True, autoincrement=True)
    id_sei_serie = Column(Integer, unique=True, nullable=False, index=True)
    descricao_tipo = Column(String(255), nullable=False, index=True, unique=True)
    aplicabilidade = Column(String(255))
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    def __repr__(self):
        return f"<SeiDocumentoTipo(nome={self.nome_tipo})>"

class SeiDocumento(ORMBase):
    """Documentos de cada processo."""
    __tablename__ = 'sei_documentos'

    id = Column(Integer, primary_key=True, autoincrement=True)
    processo_id = Column(Integer, ForeignKey('sei_processos.id'), nullable=False, index=True)
    protocol = Column(String(50), nullable=False, index=True)

    # Identificação do documento
    id_documento = Column(BigInteger, unique=True, nullable=False, index=True)
    numero_documento = Column(String(50))
    tipo_documento = Column(String(255))

    # Metadados
    serie = Column(String(255))
    numero = Column(String(50))
    data_documento = Column(DateTime)

    # Assinatura/Produção
    usuario_gerador = Column(String(255))
    unidade_geradora = Column(String(255))
    assinado = Column(Boolean, default=False)
    assinantes = Column(JSON)  # Array de assinantes

    # Acesso
    nivel_acesso = Column(String(50))
    tamanho_bytes = Column(BigInteger)
    formato = Column(String(50))  # pdf, docx, etc
    hash_sha256 = Column(String(64))

    # Storage MinIO
    minio_bucket = Column(String(100))
    minio_path = Column(String(500))
    download_url = Column(Text)

    # Status de download
    status = Column(String(50), default='pending', index=True)  # pending, downloading, completed, error
    download_attempts = Column(Integer, default=0)
    last_error = Column(Text)

    # Auditoria
    raw_api_response = Column(JSON)

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    downloaded_at = Column(DateTime)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    # Relationship
    processo = relationship("SeiProcesso", back_populates="documentos")

    def __repr__(self):
        return f"<SeiDocumento(id_doc={self.id_documento}, tipo={self.tipo_documento}, status={self.status})>"

# -------------------- MOVIMENTAÇÕES/ANDAMENTOS/ATIVIDADES DOS PROCESSOS --------------------
class SeiAndamento(ORMBase):
    """Andamentos/atividades dos processos."""

    __tablename__ = 'sei_andamentos'

    id = Column(Integer, primary_key=True, autoincrement=True)
    processo_id = Column(Integer, ForeignKey('sei_processos.id'), nullable=False, index=True)
    protocol = Column(String(50), nullable=False, index=True)

    # Identificação
    id_andamento = Column(BigInteger)
    sequencia = Column(Integer)

    # Detalhes do andamento
    tipo_andamento = Column(String(255))
    descricao = Column(Text)
    tarefa = Column(String(50))

    # Quem e onde
    usuario = Column(String(255))
    unidade_origem = Column(String(255))
    unidade_destino = Column(String(255))

    # Quando
    data_hora = Column(DateTime, index=True)

    # Atributos adicionais
    atributos = Column(JSON)

    # Resposta completa da API
    raw_api_response = Column(JSON)

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Relationship
    processo = relationship("SeiProcesso", back_populates="andamentos")

    def __repr__(self):
        return f"<SeiAndamento(id_and={self.id_andamento}, tipo={self.tipo_andamento})>"

# -------------------- CONSOLIDAÇÕES --------------------
class SeiConsolidadoUnidade(ORMBase):
    """Modelo para consolidado de processos por unidade no banco local (destino)."""
    __tablename__ = 'sei_consolidado_unidades'

    id = Column(Integer, primary_key=True, autoincrement=True)
    id_sei_unidade = Column(Integer, index=True)
    total_processos_abertos = Column(Integer, default=0)
    total_processos_concluidos = Column(Integer, default=0)
    total_processos_pausados = Column(Integer, default=0)
    total_documentos = Column(Integer, default=0)
    total_andamentos = Column(Integer, default=0)

    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    def __repr__(self):
        return f"<seiConsolidadoUnidade(unidade={self.id_sei_unidade}, abertos={self.total_processos_abertos})>"

class SeiConsolidadoCGFR(ORMBase):
    """Modelo para consolidado de processos da CGFR no banco local (destino)."""
    __tablename__ = 'sei_consolidado_cgfr'

    id = Column(Integer, primary_key=True, autoincrement=True)
    sei_protocolo_formatado = Column(String(50), unique=True, nullable=False, index=True)
    id_sei_protocolo = Column(BigInteger, index=True, unique=True, nullable=False)
    id_sei_unidade = Column(Integer, index=True)
    foi_remetido_sead_cgfr = Column(Boolean, default=False, index=True)
    dt_remetido_sead_cgfr = Column(DateTime)
    foi_recebido_sead_cgfr = Column(Boolean, default=False, index=True)
    dt_recebido_sead_cgfr = Column(DateTime)
    foi_remetido_cgfr_sead = Column(Boolean, default=False, index=True)
    dt_remetido_cgfr_sead = Column(DateTime)
    foi_recebido_cgfr_sead = Column(Boolean, default=False, index=True)
    dt_recebido_cgfr_sead = Column(DateTime)

    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    def __repr__(self):
        return f"<SeiConsolidadoCGFR(protocol={self.sei_protocolo_formatado}, unidade={self.id_sei_unidade})>"


