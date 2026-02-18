"""
Script otimizado para extrair processos gerados do banco SEI e salvar no banco local.

Otimizações implementadas:
1. Keyset pagination (cursor-based) em vez de OFFSET/LIMIT - O(1) vs O(n)
2. Server-side cursor para streaming de grandes volumes
3. Conexões persistentes (reutilização de sessões)
4. COPY protocol para bulk inserts (mais rápido que INSERT)
5. Processamento em chunks maiores

Este script:
1. Conecta ao banco SEI (origem)
2. Busca registros da tabela sei_atividades onde descricao_replace = "Processo @NIVEL_ACESSO@@GRAU_SIGILO@ gerado@DATA_AUTUACAO@@HIPOTESE_LEGAL@"
3. Extrai: protocol, id_protocolo, data_hora, tipo_procedimento, unidade
4. Salva no banco local na tabela sei_processos_temp_etl
"""
import sys
import io
from pathlib import Path
from datetime import datetime, timezone

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
from src.database.session import get_sei_engine, get_local_engine
from src.database.models.orm_models import SeiProcessoTempETL
from src.database.base import ORMBase


console = Console()

# Descrição exata a ser filtrada
DESCRICAO_FILTER = "Processo @NIVEL_ACESSO@@GRAU_SIGILO@ gerado@DATA_AUTUACAO@@HIPOTESE_LEGAL@"


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


def get_total_count(sei_engine) -> int:
    """Retorna o total de registros a serem extraídos usando conexão existente."""
    with sei_engine.connect() as conn:
        result = conn.execute(
            text("""
                SELECT COUNT(*)
                FROM sei_processo.sei_atividades
                WHERE descricao_replace = :desc
            """),
            {"desc": DESCRICAO_FILTER}
        )
        return result.scalar()


def get_min_max_id(sei_engine) -> tuple[int, int]:
    """Retorna o ID mínimo e máximo dos registros filtrados."""
    with sei_engine.connect() as conn:
        result = conn.execute(
            text("""
                SELECT MIN(id), MAX(id)
                FROM sei_processo.sei_atividades
                WHERE descricao_replace = :desc
            """),
            {"desc": DESCRICAO_FILTER}
        )
        row = result.fetchone()
        return (row[0] or 0, row[1] or 0)


def get_min_max_data_hora(sei_engine) -> tuple[datetime | None, datetime | None]:
    """Retorna a data mínima e máxima de criação dos processos filtrados."""
    with sei_engine.connect() as conn:
        result = conn.execute(
            text("""
                SELECT MIN(data_hora), MAX(data_hora)
                FROM sei_processo.sei_atividades
                WHERE descricao_replace = :desc
            """),
            {"desc": DESCRICAO_FILTER}
        )
        row = result.fetchone()
        return (row[0], row[1])


def truncate_destination_table(local_engine):
    """Limpa tabela destino usando TRUNCATE (mais rápido que DELETE)."""
    logger.info("Limpando tabela de destino com TRUNCATE...")
    with local_engine.connect() as conn:
        conn.execute(text("TRUNCATE TABLE sei_processos_temp_etl RESTART IDENTITY"))
        conn.commit()
    logger.success("Tabela limpa!")


def copy_batch_to_local(local_engine, records: list[dict]):
    """
    Insere batch usando COPY protocol (mais rápido que INSERT).
    Usa StringIO para criar um buffer CSV em memória.
    """
    if not records:
        return 0

    # Cria buffer CSV em memória
    buffer = io.StringIO()
    now = datetime.now(timezone.utc).isoformat()

    for rec in records:
        # Escapa valores para formato COPY (tab-separated)
        protocol = rec['protocol'] or ''
        id_protocolo = str(rec['id_protocolo']) if rec['id_protocolo'] else ''
        data_hora = rec['data_hora'].isoformat() if rec['data_hora'] else ''
        tipo_procedimento = (rec['tipo_procedimento'] or '').replace('\t', ' ').replace('\n', ' ')
        unidade = (rec['unidade'] or '').replace('\t', ' ').replace('\n', ' ')

        buffer.write(f"{protocol}\t{id_protocolo}\t{data_hora}\t{tipo_procedimento}\t{unidade}\t{now}\n")

    buffer.seek(0)

    # Usa raw connection para COPY
    raw_conn = local_engine.raw_connection()
    try:
        cursor = raw_conn.cursor()
        cursor.copy_from(
            buffer,
            'sei_processos_temp_etl',
            columns=('protocol', 'id_protocolo', 'data_hora', 'tipo_procedimento', 'unidade', 'created_at'),
            sep='\t'
        )
        raw_conn.commit()
    finally:
        raw_conn.close()

    return len(records)


def extract_with_keyset_pagination(sei_engine, local_engine, batch_size: int = 5000):
    """
    Extrai dados usando keyset pagination (cursor-based).

    Vantagens sobre OFFSET/LIMIT:
    - Performance constante O(1) independente do offset
    - Não "pula" registros se houver inserções durante a extração
    - Muito mais eficiente para grandes volumes de dados
    """
    # Obtém estatísticas
    console.print("[yellow]Obtendo estatísticas do banco SEI...[/yellow]")
    total_records = get_total_count(sei_engine)
    min_id, max_id = get_min_max_id(sei_engine)
    min_data, max_data = get_min_max_data_hora(sei_engine)

    if total_records == 0:
        console.print("[red]Nenhum registro encontrado com o filtro especificado![/red]")
        logger.warning("Nenhum registro encontrado para extração")
        return 0

    # Formata datas para exibição
    min_data_str = min_data.strftime("%d/%m/%Y %H:%M") if min_data else "N/A"
    max_data_str = max_data.strftime("%d/%m/%Y %H:%M") if max_data else "N/A"

    console.print(f"[green]Total de registros: {total_records:,}[/green]")
    console.print(f"[green]Range de IDs: {min_id:,} - {max_id:,}[/green]")
    console.print(f"[green]Período dos processos: {min_data_str} até {max_data_str}[/green]\n")
    logger.info(f"Total: {total_records:,} | IDs: {min_id} - {max_id}")
    logger.info(f"Período dos processos coletados: {min_data_str} até {max_data_str}")

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
        TimeElapsedColumn(),
        console=console
    ) as progress:

        task = progress.add_task(
            f"[cyan]Extraindo (batch: {batch_size:,})...",
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

    # Mostra estatísticas finais
    with local_engine.connect() as conn:
        result = conn.execute(text("SELECT COUNT(*) FROM sei_processos_temp_etl"))
        total_local = result.scalar()
        console.print(f"[cyan]Registros na tabela local: {total_local:,}[/cyan]\n")


def main():
    """Função principal."""
    import argparse

    parser = argparse.ArgumentParser(description="Extração otimizada de processos gerados do SEI")
    parser.add_argument(
        "--cursor",
        action="store_true",
        help="Usa server-side cursor (recomendado para volumes muito grandes)"
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=None,
        help="Tamanho do batch (default: settings.batch_size)"
    )

    args = parser.parse_args()

    try:
        extract_and_load(use_server_cursor=args.cursor)
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
