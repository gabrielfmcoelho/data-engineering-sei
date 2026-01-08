"""
Script para extrair processos gerados do banco SEI e salvar no banco local.

Este script:
1. Conecta ao banco SEI (origem)
2. Busca registros da tabela sei_atividades onde descricao_replace = "Processo @NIVEL_ACESSO@@GRAU_SIGILO@ gerado@DATA_AUTUACAO@@HIPOTESE_LEGAL@"
3. Extrai: protocol, id_protocolo, data_hora, tipo_procedimento, unidade
4. Salva no banco local na tabela sei_processos_temp_etl
"""
import sys
from pathlib import Path

# Adiciona o diretório raiz ao path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from sqlalchemy import select, func
from loguru import logger
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from typing import List, Dict, Any

from src.config import settings
from src.database.session import get_sei_session, get_local_session
from src.database.models.declarative_models import SeiAtividades
from src.database.models.orm_models import SeiProcessoTempETL  # Modelo do banco local (destino)
from src.database.base import ORMBase as ORMBase
from src.database.session import get_local_engine


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
        "logs/extract_processos_gerados_{time}.log",
        rotation="100 MB",
        retention="10 days",
        level="DEBUG"
    )


def create_tables_if_not_exists():
    """Cria as tabelas no banco local se não existirem."""
    logger.info("Criando tabelas no banco local se necessário...")
    engine = get_local_engine()
    ORMBase.metadata.create_all(engine)
    logger.success("Tabelas verificadas/criadas com sucesso!")


def get_total_count() -> int:
    """Retorna o total de registros a serem extraídos."""
    descricao_filter = "Processo @NIVEL_ACESSO@@GRAU_SIGILO@ gerado@DATA_AUTUACAO@@HIPOTESE_LEGAL@"

    with get_sei_session() as session:
        stmt = select(func.count()).select_from(SeiAtividades).where(
            SeiAtividades.descricao_replace == descricao_filter
        )
        total = session.execute(stmt).scalar()

    return total


def extract_and_load():
    """Extrai dados do SEI e carrega no banco local."""
    setup_logger()

    console.print("\n[bold cyan]═══════════════════════════════════════════════════════════[/bold cyan]")
    console.print("[bold cyan]  Extração de Processos Gerados - SEI Estado do Piauí  [/bold cyan]")
    console.print("[bold cyan]═══════════════════════════════════════════════════════════[/bold cyan]\n")

    # Cria tabelas
    create_tables_if_not_exists()

    # Descrição exata a ser filtrada
    descricao_filter = "Processo @NIVEL_ACESSO@@GRAU_SIGILO@ gerado@DATA_AUTUACAO@@HIPOTESE_LEGAL@"

    logger.info(f"Conectando ao banco SEI em {settings.sei_db_host}...")
    logger.info(f"Filtrando por descricao_replace = '{descricao_filter}'")

    # Conta total de registros
    console.print("[yellow]Contando registros no banco SEI...[/yellow]")
    total_records = get_total_count()

    if total_records == 0:
        console.print("[red]Nenhum registro encontrado com o filtro especificado![/red]")
        logger.warning("Nenhum registro encontrado para extração")
        return

    console.print(f"[green]Total de registros encontrados: {total_records:,}[/green]\n")
    logger.info(f"Total de registros a serem extraídos: {total_records:,}")

    # Limpa tabela destino antes de inserir
    with get_local_session() as local_session:
        logger.info("Limpando tabela de destino...")
        local_session.query(SeiProcessoTempETL).delete()
        local_session.commit()
        logger.success("Tabela limpa!")

    # Processa em batches
    batch_size = settings.batch_size
    total_batches = (total_records + batch_size - 1) // batch_size
    offset = 0
    total_inserted = 0

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console
    ) as progress:

        task = progress.add_task(
            f"[cyan]Extraindo e carregando (batch size: {batch_size})...",
            total=total_records
        )

        for batch_num in range(1, total_batches + 1):
            logger.debug(f"Processando batch {batch_num}/{total_batches} (offset: {offset})")

            # Extrai batch do SEI e prepara dados (dentro da sessão para evitar detached instances)
            records_to_insert: List[Dict[str, Any]] = []

            with get_sei_session() as sei_session:
                stmt = (
                    select(SeiAtividades)
                    .where(SeiAtividades.descricao_replace == descricao_filter)
                    .offset(offset)
                    .limit(batch_size)
                )
                atividades = sei_session.execute(stmt).scalars().all()

                # Extrai dados DENTRO da sessão, enquanto os objetos ainda estão atachados
                for atividade in atividades:
                    records_to_insert.append({
                        'protocol': atividade.protocolo_formatado,
                        'id_protocolo': atividade.id_protocolo,
                        'data_hora': atividade.data_hora,
                        'tipo_procedimento': atividade.tipo_procedimento,
                        'unidade': atividade.unidade,
                    })

            if not records_to_insert:
                break

            # Insere no banco local
            with get_local_session() as local_session:
                local_session.bulk_insert_mappings(SeiProcessoTempETL, records_to_insert)
                local_session.commit()

            batch_inserted = len(records_to_insert)
            total_inserted += batch_inserted
            offset += batch_size

            progress.update(task, advance=batch_inserted)
            logger.debug(f"Batch {batch_num} inserido: {batch_inserted} registros")

    console.print(f"\n[bold green]✓ Extração concluída com sucesso![/bold green]")
    console.print(f"[bold green]  Total de registros inseridos: {total_inserted:,}[/bold green]\n")

    logger.success(f"Extração finalizada: {total_inserted:,} registros inseridos")

    # Mostra estatísticas
    with get_local_session() as session:
        total_local = session.query(func.count(SeiProcessoTempETL.id)).scalar()
        console.print(f"[cyan]Registros na tabela local: {total_local:,}[/cyan]\n")


def main():
    """Função principal."""
    try:
        extract_and_load()
    except KeyboardInterrupt:
        console.print("\n[yellow]Processo interrompido pelo usuário.[/yellow]")
        logger.warning("Processo interrompido pelo usuário")
        sys.exit(1)
    except Exception as e:
        console.print(f"\n[bold red]Erro durante a execução: {e}[/bold red]")
        logger.exception("Erro durante a extração")
        sys.exit(1)


if __name__ == "__main__":
    main()
