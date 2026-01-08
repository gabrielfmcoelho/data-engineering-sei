"""Modelos SQLAlchemy para leitura do banco SEI (origem).

IMPORTANTE: Estes modelos são APENAS para leitura do banco SEI de produção.
NÃO devem ser incluídos nas migrations do Alembic.
NÃO devem ser criados no banco local.
"""
from sqlalchemy import Column, String, DateTime, Integer, BigInteger, Text
from ..base import ExtDeclarativeBase


class SeiAtividades(ExtDeclarativeBase):
    """Modelo para leitura da tabela sei_atividades do banco SEI (origem).

    Esta tabela existe apenas no banco SEI de produção.
    Usada apenas para LEITURA durante a extração de dados.

    Colunas reais do banco:
    - id (INTEGER, PK)
    - protocolo_formatado (VARCHAR 255)
    - id_protocolo (BIGINT)
    - data_hora (TIMESTAMP)
    - unidade (VARCHAR 100)
    - usuario (VARCHAR 100)
    - tipo_procedimento (VARCHAR 255)
    - descricao_replace (TEXT)
    - data_carga (TIMESTAMP)
    """

    __tablename__ = 'sei_atividades'
    __table_args__ = {'schema': 'sei_processo'}

    # Colunas com nomes EXATOS do banco SEI
    id = Column(Integer, primary_key=True)
    protocolo_formatado = Column(String(255))
    id_protocolo = Column(BigInteger)
    data_hora = Column(DateTime)
    unidade = Column(String(100))
    usuario = Column(String(100))
    tipo_procedimento = Column(String(255))
    descricao_replace = Column(Text)
    data_carga = Column(DateTime)

    def __repr__(self):
        return f"<SeiAtividades(protocolo={self.protocolo_formatado}, id_protocolo={self.id_protocolo})>"
