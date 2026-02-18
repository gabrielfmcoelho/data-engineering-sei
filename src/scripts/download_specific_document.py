"""
Script para baixar um documento específico de um processo SEI.

Este script:
1. Aceita ID do documento e protocolo do processo como argumentos
2. Busca o id_unidade automaticamente (do banco ou das configurações)
3. Baixa o documento via API SEI
4. Detecta o formato real do documento via Content-Disposition header
5. Salva no filesystem local (./downloads/{protocol}/{filename})

Uso:
    python src/scripts/download_specific_document.py \
        --document-id 18911448 \
        --protocol "00002.006238/2025-95"
"""
import sys
import re
import asyncio
import hashlib
from pathlib import Path
from typing import Optional
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from sqlalchemy import select
from loguru import logger
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from src.config import settings
from src.database.session import get_local_session
from src.database.models.orm_models import SeiProcesso, SeiDocumento
from src.api.sei_client import SeiAPIClient, SeiUnidadeAccessError, SeiPermanentError


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
        "logs/download_specific_document_{time}.log",
        rotation="10 MB",
        retention="7 days",
        level="DEBUG"
    )


def calculate_sha256(data: bytes) -> str:
    """Calcula SHA256 de um conteúdo.

    Args:
        data: Dados binários

    Returns:
        Hash SHA256 hexadecimal
    """
    return hashlib.sha256(data).hexdigest()


def extract_filename_from_content_disposition(content_disposition: str) -> Optional[str]:
    """Extrai o nome do arquivo do header Content-Disposition.

    Args:
        content_disposition: Valor do header Content-Disposition
                            Exemplo: 'attachment; filename="21025378.html"'

    Returns:
        Nome do arquivo extraído, ou None se não encontrar
    """
    if not content_disposition:
        return None

    # Procura por filename="..."
    match = re.search(r'filename="([^"]+)"', content_disposition)
    if match:
        return match.group(1)

    # Procura por filename=... (sem aspas)
    match = re.search(r'filename=([^\s;]+)', content_disposition)
    if match:
        return match.group(1)

    return None


def get_unidade_id_from_database(protocol: str) -> Optional[str]:
    """Busca o id_unidade do processo no banco de dados.

    Args:
        protocol: Protocolo do processo (ex: "00002.006238/2025-95")

    Returns:
        id_unidade se encontrado, None caso contrário
    """
    try:
        with get_local_session() as session:
            stmt = select(SeiProcesso).where(SeiProcesso.protocol == protocol)
            result = session.execute(stmt)
            processo = result.scalar_one_or_none()

            if processo and processo.id_unidade:
                logger.info(f"Unidade encontrada no banco: {processo.id_unidade}")
                return str(processo.id_unidade)

        logger.debug(f"Processo {protocol} não encontrado no banco de dados")
        return None

    except Exception as e:
        logger.warning(f"Erro ao buscar processo no banco: {e}")
        return None


async def try_download_with_unidades(
    api_client: SeiAPIClient,
    protocol: str,
    document_id: str,
    unidade_ids: list[str]
) -> Optional[tuple[bytes, str, dict]]:
    """Tenta baixar documento usando uma lista de unidades.

    Args:
        api_client: Cliente da API SEI
        protocol: Protocolo do processo
        document_id: ID do documento
        unidade_ids: Lista de IDs de unidades para tentar

    Returns:
        Tupla (conteúdo_binário, unidade_id_usada, headers) se sucesso, None se falhar
    """
    for id_unidade in unidade_ids:
        try:
            logger.debug(f"Tentando baixar documento com unidade {id_unidade}...")

            # Tenta baixar o documento com headers
            content, headers = await api_client.baixar_documento(
                id_unidade=id_unidade,
                protocolo_documento=document_id,
                return_headers=True
            )

            if content and len(content) > 0:
                logger.success(f"Documento baixado com sucesso usando unidade {id_unidade}")
                return (content, id_unidade, headers)
            else:
                logger.warning(f"Documento vazio retornado pela unidade {id_unidade}")

        except SeiUnidadeAccessError as e:
            logger.debug(f"Unidade {id_unidade} não tem acesso ao documento: {e}")
            continue

        except SeiPermanentError as e:
            logger.error(f"Erro permanente ao baixar documento: {e}")
            return None

        except Exception as e:
            logger.warning(f"Erro ao tentar unidade {id_unidade}: {e}")
            continue

    return None


async def download_specific_document(
    document_id: str,
    protocol: str,
    output_dir: str = "./downloads"
):
    """Baixa um documento específico de um processo.

    Args:
        document_id: ID do documento (ex: "18911448")
        protocol: Protocolo do processo (ex: "00002.006238/2025-95")
        output_dir: Diretório de saída para os downloads
    """
    setup_logger()

    console.print("\n[bold cyan]═══════════════════════════════════════════════════════════[/bold cyan]")
    console.print("[bold cyan]     Download de Documento Específico - SEI API          [/bold cyan]")
    console.print("[bold cyan]═══════════════════════════════════════════════════════════[/bold cyan]\n")

    console.print(f"[yellow]Protocolo:[/yellow] {protocol}")
    console.print(f"[yellow]Documento ID:[/yellow] {document_id}\n")

    # Prepara lista de unidades para tentar
    unidade_ids = []

    # 1. Tenta buscar do banco de dados
    console.print("[cyan]Buscando processo no banco de dados...[/cyan]")
    db_unidade = get_unidade_id_from_database(protocol)
    if db_unidade:
        unidade_ids.append(db_unidade)
        console.print(f"[green]✓ Unidade encontrada no banco: {db_unidade}[/green]\n")
    else:
        console.print("[yellow]⚠ Processo não encontrado no banco[/yellow]\n")

    # 2. Adiciona unidade padrão das configurações
    if settings.sei_api_id_unidade and settings.sei_api_id_unidade not in unidade_ids:
        unidade_ids.append(settings.sei_api_id_unidade)
        console.print(f"[cyan]Usando unidade padrão das configurações: {settings.sei_api_id_unidade}[/cyan]\n")

    if not unidade_ids:
        console.print("[bold red]✗ Nenhuma unidade disponível para download![/bold red]")
        console.print("[yellow]Dica: Configure SEI_API_ID_UNIDADE no arquivo .env[/yellow]")
        return

    # Inicia cliente API
    console.print("[cyan]Conectando à API SEI...[/cyan]")
    async with SeiAPIClient(
        base_url=settings.sei_api_base_url,
        usuario=settings.sei_api_user,
        senha=settings.sei_api_password,
        orgao=settings.sei_api_orgao,
        timeout=settings.sei_api_timeout
    ) as api_client:

        console.print("[green]✓ Conectado à API[/green]\n")

        # Baixa o documento (o nome/formato virá dos headers da resposta)
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console
        ) as progress:
            task = progress.add_task(
                f"[cyan]Baixando documento {document_id}...",
                total=None
            )

            result = await try_download_with_unidades(
                api_client=api_client,
                protocol=protocol,
                document_id=document_id,
                unidade_ids=unidade_ids
            )

            progress.update(task, completed=True)

        if not result:
            console.print(f"\n[bold red]✗ Falha ao baixar documento {document_id}[/bold red]")
            console.print("[yellow]Possíveis causas:[/yellow]")
            console.print("  • Documento não existe")
            console.print("  • Nenhuma das unidades disponíveis tem acesso")
            console.print("  • Protocolo incorreto")
            return

        content, used_unidade, headers = result

        # Extrai nome do arquivo do header Content-Disposition
        content_disposition = headers.get('content-disposition') or headers.get('Content-Disposition')
        filename = extract_filename_from_content_disposition(content_disposition)

        if not filename:
            # Fallback: usa o document_id com extensão genérica
            logger.warning("Não foi possível extrair filename do Content-Disposition, usando fallback")
            filename = f"{document_id}.bin"

        # Extrai MIME type do header
        mime_type = headers.get('content-type') or headers.get('Content-Type') or 'application/octet-stream'

        console.print(f"[green]✓ Documento recebido: {filename}[/green]")
        console.print(f"[green]  Tipo: {mime_type}[/green]\n")

        # Calcula hash
        hash_sha256 = calculate_sha256(content)

        # Prepara diretório de saída
        protocol_safe = protocol.replace('/', '-').replace('.', '-')
        output_path = Path(output_dir) / protocol_safe
        output_path.mkdir(parents=True, exist_ok=True)

        # Nome do arquivo (usa o filename do header)
        file_path = output_path / filename

        # Salva arquivo
        console.print(f"\n[cyan]Salvando documento...[/cyan]")
        with open(file_path, 'wb') as f:
            f.write(content)

        console.print(f"[bold green]✓ Documento salvo com sucesso![/bold green]\n")
        console.print(f"[green]Arquivo:[/green] {file_path}")
        console.print(f"[green]Nome original:[/green] {filename}")
        console.print(f"[green]Tipo:[/green] {mime_type}")
        console.print(f"[green]Tamanho:[/green] {len(content):,} bytes ({len(content) / 1024:.2f} KB)")
        console.print(f"[green]SHA256:[/green] {hash_sha256}")
        console.print(f"[green]Unidade:[/green] {used_unidade}\n")

        logger.success(
            f"Documento {document_id} baixado: {filename} "
            f"({mime_type}, {len(content)} bytes, SHA256: {hash_sha256})"
        )


def main():
    """Função principal."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Download de documento específico da API SEI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exemplos:
  # Download de documento específico
  python src/scripts/download_specific_document.py \\
      --document-id 18911448 \\
      --protocol "00002.006238/2025-95"

  # Download com diretório customizado
  python src/scripts/download_specific_document.py \\
      --document-id 18911448 \\
      --protocol "00002.006238/2025-95" \\
      --output-dir "./meus_documentos"
        """
    )

    parser.add_argument(
        "--document-id",
        required=True,
        help="ID do documento SEI (ex: 18911448)"
    )

    parser.add_argument(
        "--protocol",
        required=True,
        help="Protocolo do processo (ex: 00002.006238/2025-95)"
    )

    parser.add_argument(
        "--output-dir",
        default="./downloads",
        help="Diretório de saída para os downloads (padrão: ./downloads)"
    )

    args = parser.parse_args()

    try:
        asyncio.run(download_specific_document(
            document_id=args.document_id,
            protocol=args.protocol,
            output_dir=args.output_dir
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
