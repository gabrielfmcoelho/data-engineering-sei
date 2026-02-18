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

from sqlalchemy import select, func, text
from loguru import logger
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from typing import List, Dict, Any
from datetime import datetime

import time

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

    # Processa em batches com keyset pagination
    batch_size = settings.batch_size
    total_inserted = 0
    last_id = 0  # Keyset pagination: start from id > 0

    # Prepare raw SQL insert statement for better performance
    insert_sql = text("""
        INSERT INTO sei_processos_temp_etl
        (protocol, id_protocolo, data_hora, tipo_procedimento, unidade, created_at)
        VALUES (:protocol, :id_protocolo, :data_hora, :tipo_procedimento, :unidade, :created_at)
    """)

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

        total_read_time = 0.0
        total_insert_time = 0.0
        batch_num = 0

        while True:
            batch_num += 1
            logger.debug(f"Processando batch {batch_num} (last_id: {last_id})")

            # Extrai batch do SEI usando keyset pagination (WHERE id > last_id)
            records_to_insert: List[Dict[str, Any]] = []

            read_start = time.perf_counter()
            with get_sei_session() as sei_session:
                stmt = (
                    select(SeiAtividades)
                    .where(SeiAtividades.descricao_replace == descricao_filter)
                    .where(SeiAtividades.id > last_id)
                    .order_by(SeiAtividades.id)
                    .limit(batch_size)
                )
                atividades = sei_session.execute(stmt).scalars().all()

                # Extrai dados DENTRO da sessão, enquanto os objetos ainda estão atachados
                now = datetime.utcnow()
                for atividade in atividades:
                    records_to_insert.append({
                        'protocol': atividade.protocolo_formatado,
                        'id_protocolo': str(atividade.id_protocolo),  # Convert to string for the table
                        'data_hora': atividade.data_hora,
                        'tipo_procedimento': atividade.tipo_procedimento,
                        'unidade': atividade.unidade,
                        'created_at': now,
                    })
                    last_id = atividade.id  # Update cursor for next batch

            read_elapsed = time.perf_counter() - read_start
            total_read_time += read_elapsed

            if not records_to_insert:
                break

            # Insere no banco local usando raw SQL executemany
            insert_start = time.perf_counter()
            engine = get_local_engine()
            with engine.begin() as conn:
                conn.execute(insert_sql, records_to_insert)
            insert_elapsed = time.perf_counter() - insert_start
            total_insert_time += insert_elapsed

            batch_inserted = len(records_to_insert)
            total_inserted += batch_inserted

            progress.update(task, advance=batch_inserted)
            logger.debug(f"Batch {batch_num}: read={read_elapsed:.2f}s, insert={insert_elapsed:.2f}s")

        # Print timing summary
        console.print(f"\n[bold yellow]⏱ Timing Summary:[/bold yellow]")
        console.print(f"  [cyan]Total READ time (SEI DB):    {total_read_time:.2f}s[/cyan]")
        console.print(f"  [cyan]Total INSERT time (Local DB): {total_insert_time:.2f}s[/cyan]")
        read_pct = (total_read_time / (total_read_time + total_insert_time)) * 100 if (total_read_time + total_insert_time) > 0 else 0
        console.print(f"  [yellow]Bottleneck: {'READ (SEI)' if read_pct > 50 else 'INSERT (Local)'} ({read_pct:.1f}% read / {100-read_pct:.1f}% insert)[/yellow]")

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
