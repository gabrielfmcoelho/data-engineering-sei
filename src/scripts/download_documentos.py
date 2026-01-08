"""
Script para baixar documentos dos processos e armazenar no MinIO.

Este script:
1. Lê documentos com status='pending' da tabela sei_documentos
2. Baixa cada documento via API SEI em paralelo
3. Salva no MinIO (bucket: sei-documentos/{protocol}/{id_documento}.pdf)
4. Atualiza status no Postgres para 'completed'
"""
import sys
import asyncio
import hashlib
from pathlib import Path
from typing import Optional
from datetime import datetime
from io import BytesIO

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from sqlalchemy import select, and_
from loguru import logger
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn, TimeRemainingColumn
from minio import Minio
from minio.error import S3Error

from src.config import settings
from src.database.session import get_local_session
from src.database.models.orm_models import SeiDocumento, SeiETLStatus
from src.api.sei_client import SeiAPIClient


console = Console()


def setup_logger():
    """Configura o logger."""
    logger.remove()
    logger.add(
        sys.stderr,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>",
        level="INFO"
    )
    logger.add(
        "logs/download_documentos_{time}.log",
        rotation="100 MB",
        retention="10 days",
        level="DEBUG"
    )


def init_minio_client() -> Minio:
    """Inicializa cliente MinIO.

    Returns:
        Cliente MinIO configurado
    """
    client = Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_secure
    )

    # Garante que bucket existe
    bucket = settings.minio_bucket
    try:
        if not client.bucket_exists(bucket):
            client.make_bucket(bucket)
            logger.info(f"Bucket '{bucket}' criado no MinIO")
        else:
            logger.debug(f"Bucket '{bucket}' já existe")
    except S3Error as e:
        logger.error(f"Erro ao verificar/criar bucket: {e}")
        raise

    return client


def calculate_sha256(data: bytes) -> str:
    """Calcula SHA256 de um conteúdo.

    Args:
        data: Dados binários

    Returns:
        Hash SHA256 hexadecimal
    """
    return hashlib.sha256(data).hexdigest()


async def download_and_save_documento(
    api_client: SeiAPIClient,
    minio_client: Minio,
    documento: SeiDocumento,
    id_unidade: str
) -> bool:
    """Baixa documento da API e salva no MinIO.

    Args:
        api_client: Cliente da API SEI
        minio_client: Cliente MinIO
        documento: Objeto SeiDocumento
        id_unidade: ID da unidade

    Returns:
        True se sucesso, False caso contrário
    """
    protocol = documento.protocol
    id_doc = str(documento.id_documento)
    protocolo_doc = f"{id_doc}"  # API usa id_documento como protocolo

    try:
        # Atualiza status para 'downloading'
        with get_local_session() as session:
            doc = session.query(SeiDocumento).filter_by(id=documento.id).first()
            if doc:
                doc.status = 'downloading'
                doc.download_attempts += 1
                doc.updated_at = datetime.utcnow()
                session.commit()

        # 1. Baixa documento da API
        logger.debug(f"Baixando documento {protocol}/{id_doc}")
        content = await api_client.baixar_documento(id_unidade, protocolo_doc)

        if not content:
            raise Exception("Documento vazio retornado pela API")

        # 2. Calcula hash
        hash_sha256 = calculate_sha256(content)

        # 3. Define caminho no MinIO
        # Formato: {protocol}/{id_documento}.pdf
        protocol_safe = protocol.replace('/', '-').replace('.', '-')
        object_name = f"{protocol_safe}/{id_doc}.pdf"

        # 4. Upload para MinIO
        logger.debug(f"Salvando no MinIO: {object_name} ({len(content)} bytes)")

        minio_client.put_object(
            bucket_name=settings.minio_bucket,
            object_name=object_name,
            data=BytesIO(content),
            length=len(content),
            content_type='application/pdf'
        )

        # 5. Atualiza banco
        with get_local_session() as session:
            doc = session.query(SeiDocumento).filter_by(id=documento.id).first()
            if doc:
                doc.status = 'completed'
                doc.minio_bucket = settings.minio_bucket
                doc.minio_path = object_name
                doc.tamanho_bytes = len(content)
                doc.hash_sha256 = hash_sha256
                doc.downloaded_at = datetime.utcnow()
                doc.updated_at = datetime.utcnow()
                doc.last_error = None
                session.commit()

        logger.success(f"Documento {protocol}/{id_doc} salvo com sucesso")
        return True

    except Exception as e:
        logger.error(f"Erro ao baixar documento {protocol}/{id_doc}: {e}")

        # Atualiza banco com erro
        with get_local_session() as session:
            doc = session.query(SeiDocumento).filter_by(id=documento.id).first()
            if doc:
                # Se já tentou 3 vezes, marca como error, senão volta para pending
                if doc.download_attempts >= 3:
                    doc.status = 'error'
                else:
                    doc.status = 'pending'

                doc.last_error = str(e)[:500]  # Limita tamanho do erro
                doc.updated_at = datetime.utcnow()
                session.commit()

        return False


async def process_batch(
    api_client: SeiAPIClient,
    minio_client: Minio,
    documentos: list,
    id_unidade: str,
    progress,
    task_id
):
    """Processa um lote de documentos em paralelo.

    Args:
        api_client: Cliente da API
        minio_client: Cliente MinIO
        documentos: Lista de documentos
        id_unidade: ID da unidade
        progress: Objeto Rich Progress
        task_id: ID da task no progress
    """
    tasks = [
        download_and_save_documento(api_client, minio_client, doc, id_unidade)
        for doc in documentos
    ]

    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Conta sucessos
    success_count = sum(1 for r in results if r is True)

    progress.update(task_id, advance=len(documentos))

    return success_count


async def download_all_documentos(
    id_unidade: str,
    batch_size: int = 20,
    limit: Optional[int] = None
):
    """Baixa todos os documentos pendentes.

    Args:
        id_unidade: ID da unidade SEI
        batch_size: Tamanho do lote para download paralelo
        limit: Limite de documentos a baixar (None = todos)
    """
    setup_logger()

    console.print("\n[bold cyan]═══════════════════════════════════════════════════════════[/bold cyan]")
    console.print("[bold cyan]  Download de Documentos para MinIO - Execução Paralela  [/bold cyan]")
    console.print("[bold cyan]═══════════════════════════════════════════════════════════[/bold cyan]\n")

    # Inicializa MinIO
    console.print("[yellow]Inicializando MinIO...[/yellow]")
    minio_client = init_minio_client()
    console.print("[green]✓ MinIO pronto[/green]\n")

    # Busca documentos pendentes
    with get_local_session() as session:
        stmt = (
            select(SeiDocumento)
            .where(
                and_(
                    SeiDocumento.status == 'pending',
                    SeiDocumento.download_attempts < 3
                )
            )
            .order_by(SeiDocumento.created_at)
        )

        if limit:
            stmt = stmt.limit(limit)

        result = session.execute(stmt)
        documentos = result.scalars().all()

    if not documentos:
        console.print("[yellow]Nenhum documento pendente para download![/yellow]")
        return

    total = len(documentos)
    console.print(f"[green]Total de documentos a baixar: {total:,}[/green]\n")
    logger.info(f"Iniciando download de {total} documentos")

    # Inicia cliente API
    async with SeiAPIClient(
        base_url=settings.sei_api_base_url,
        usuario=settings.sei_api_user,
        senha=settings.sei_api_password,
        orgao=settings.sei_api_orgao,
        max_concurrent=settings.sei_api_max_concurrent_downloads,
        timeout=settings.sei_api_timeout
    ) as api_client:

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TimeRemainingColumn(),
            console=console
        ) as progress:

            task = progress.add_task(
                f"[cyan]Baixando documentos (lote: {batch_size})...",
                total=total
            )

            # Processa em lotes
            total_success = 0
            for i in range(0, total, batch_size):
                batch = documentos[i:i + batch_size]
                success = await process_batch(
                    api_client,
                    minio_client,
                    batch,
                    id_unidade,
                    progress,
                    task
                )
                total_success += success

    # Atualiza status geral de ETL
    with get_local_session() as session:
        # Para cada protocolo, verifica se todos os documentos foram baixados
        protocols = set(doc.protocol for doc in documentos)

        for protocol in protocols:
            docs_total = session.query(SeiDocumento).filter_by(protocol=protocol).count()
            docs_completed = session.query(SeiDocumento).filter(
                and_(
                    SeiDocumento.protocol == protocol,
                    SeiDocumento.status == 'completed'
                )
            ).count()

            etl = session.query(SeiETLStatus).filter_by(protocol=protocol).first()
            if etl:
                etl.documentos_downloaded = docs_completed
                if docs_completed == docs_total:
                    etl.documentos_status = 'completed'
                etl.updated_at = datetime.utcnow()

        session.commit()

    console.print(f"\n[bold green]✓ Download concluído![/bold green]")
    console.print(f"[bold green]  Documentos baixados com sucesso: {total_success:,}/{total:,}[/bold green]\n")
    logger.success(f"Download finalizado: {total_success}/{total} documentos")


def main():
    """Função principal."""
    import argparse

    parser = argparse.ArgumentParser(description="Download de documentos para MinIO")
    parser.add_argument("--id-unidade", required=True, help="ID da unidade SEI")
    parser.add_argument("--batch-size", type=int, default=20, help="Tamanho do lote paralelo")
    parser.add_argument("--limit", type=int, help="Limite de documentos a baixar")

    args = parser.parse_args()

    try:
        asyncio.run(download_all_documentos(
            id_unidade=args.id_unidade,
            batch_size=args.batch_size,
            limit=args.limit
        ))
    except KeyboardInterrupt:
        console.print("\n[yellow]Processo interrompido pelo usuário.[/yellow]")
        logger.warning("Processo interrompido pelo usuário")
        sys.exit(1)
    except Exception as e:
        console.print(f"\n[bold red]Erro durante a execução: {e}[/bold red]")
        logger.exception("Erro durante o download")
        sys.exit(1)


if __name__ == "__main__":
    main()
