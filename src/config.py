"""Configurações do projeto usando Pydantic Settings."""
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    """Configurações gerais do projeto."""

    model_config = SettingsConfigDict(
        env_file='.env',
        env_file_encoding='utf-8',
        case_sensitive=False
    )

    # Banco SEI (Origem)
    sei_db_host: str = Field(..., description="Host do banco SEI")
    sei_db_port: int = Field(default=5432, description="Porta do banco SEI")
    sei_db_name: str = Field(..., description="Nome do banco SEI")
    sei_db_user: str = Field(..., description="Usuário do banco SEI")
    sei_db_password: str = Field(..., description="Senha do banco SEI")
    sei_db_schema: str = Field(default="sei_processo", description="Schema do banco SEI")

    # Banco Local (Destino)
    local_db_host: str = Field(default="localhost", description="Host do banco local")
    local_db_port: int = Field(default=5432, description="Porta do banco local")
    local_db_name: str = Field(default="sei_ontology", description="Nome do banco local")
    local_db_user: str = Field(default="sei_user", description="Usuário do banco local")
    local_db_password: str = Field(default="sei_password", description="Senha do banco local")

    # Neo4J
    neo4j_uri: str = Field(default="bolt://localhost:7687", description="URI do Neo4J")
    neo4j_user: str = Field(default="neo4j", description="Usuário do Neo4J")
    neo4j_password: str = Field(default="sei_neo4j_password", description="Senha do Neo4J")

    # Redis
    redis_host: str = Field(default="localhost", description="Host do Redis")
    redis_port: int = Field(default=6379, description="Porta do Redis")

    # MinIO
    minio_endpoint: str = Field(default="localhost:9000", description="Endpoint do MinIO")
    minio_access_key: str = Field(default="minioadmin", description="Access key do MinIO")
    minio_secret_key: str = Field(default="minioadmin123", description="Secret key do MinIO")
    minio_bucket: str = Field(default="sei-documentos", description="Bucket padrão do MinIO")
    minio_secure: bool = Field(default=False, description="Usar HTTPS para MinIO")

    # Extração (Banco SEI direto)
    batch_size: int = Field(default=1000, description="Tamanho do batch para extração")
    max_workers: int = Field(default=4, description="Número máximo de workers")

    # API SEI (Consulta via API REST)
    sei_api_base_url: str = Field(default="https://api.sei.pi.gov.br", description="URL base da API SEI")
    sei_api_user: str = Field(..., description="Usuário da API SEI")
    sei_api_password: str = Field(..., description="Senha da API SEI")
    sei_api_orgao: str = Field(default="GOV-PI", description="Órgão do usuário SEI")
    sei_api_id_unidade: str = Field(..., description="ID da unidade padrão para consultas")
    sei_api_max_concurrent: int = Field(default=10, description="Máximo de requisições simultâneas")
    sei_api_max_concurrent_downloads: int = Field(default=5, description="Máximo de downloads simultâneos")
    sei_api_timeout: int = Field(default=30, description="Timeout em segundos")

    @property
    def sei_db_url(self) -> str:
        """URL de conexão do banco SEI."""
        return (
            f"postgresql://{self.sei_db_user}:{self.sei_db_password}"
            f"@{self.sei_db_host}:{self.sei_db_port}/{self.sei_db_name}"
        )

    @property
    def local_db_url(self) -> str:
        """URL de conexão do banco local."""
        return (
            f"postgresql://{self.local_db_user}:{self.local_db_password}"
            f"@{self.local_db_host}:{self.local_db_port}/{self.local_db_name}"
        )

    @property
    def redis_url(self) -> str:
        """URL de conexão do Redis."""
        return f"redis://{self.redis_host}:{self.redis_port}"


# Singleton para as configurações
settings = Settings()
