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

from sqlalchemy import text
from loguru import logger
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn, TimeElapsedColumn

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

    # Limpa tabela destino
    truncate_destination_table(local_engine)

    # Query otimizada com keyset pagination
    query = text("""
        SELECT
            id,
            protocolo_formatado as protocol,
            id_protocolo,
            data_hora,
            tipo_procedimento,
            unidade
        FROM sei_processo.sei_atividades
        WHERE descricao_replace = :desc
          AND id > :last_id
        ORDER BY id
        LIMIT :batch_size
    """)

    total_inserted = 0
    last_id = 0
    batch_num = 0

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

        # Usa conexão persistente para toda a extração
        with sei_engine.connect() as sei_conn:
            while True:
                batch_num += 1

                # Extrai batch usando keyset pagination
                result = sei_conn.execute(
                    query,
                    {"desc": DESCRICAO_FILTER, "last_id": last_id, "batch_size": batch_size}
                )
                rows = result.fetchall()

                if not rows:
                    break

                # Converte para lista de dicionários
                records = [
                    {
                        'protocol': row.protocol,
                        'id_protocolo': row.id_protocolo,
                        'data_hora': row.data_hora,
                        'tipo_procedimento': row.tipo_procedimento,
                        'unidade': row.unidade,
                    }
                    for row in rows
                ]

                # Atualiza last_id para próximo batch (keyset pagination)
                last_id = rows[-1].id

                # Insere no banco local usando COPY
                inserted = copy_batch_to_local(local_engine, records)
                total_inserted += inserted

                progress.update(task, advance=inserted)
                logger.debug(f"Batch {batch_num}: {inserted} registros (last_id: {last_id})")

    return total_inserted


def extract_with_server_cursor(sei_engine, local_engine, batch_size: int = 10000):
    """
    Alternativa: Extrai dados usando server-side cursor.

    Ainda mais eficiente para volumes muito grandes pois:
    - Não carrega todos os dados na memória do servidor
    - Stream direto do banco de dados
    """
    console.print("[yellow]Obtendo estatísticas do banco SEI...[/yellow]")
    total_records = get_total_count(sei_engine)
    min_data, max_data = get_min_max_data_hora(sei_engine)

    if total_records == 0:
        console.print("[red]Nenhum registro encontrado com o filtro especificado![/red]")
        logger.warning("Nenhum registro encontrado para extração")
        return 0

    # Formata datas para exibição
    min_data_str = min_data.strftime("%d/%m/%Y %H:%M") if min_data else "N/A"
    max_data_str = max_data.strftime("%d/%m/%Y %H:%M") if max_data else "N/A"

    console.print(f"[green]Total de registros: {total_records:,}[/green]")
    console.print(f"[green]Período dos processos: {min_data_str} até {max_data_str}[/green]\n")
    logger.info(f"Total: {total_records:,}")
    logger.info(f"Período dos processos coletados: {min_data_str} até {max_data_str}")

    # Limpa tabela destino
    truncate_destination_table(local_engine)

    total_inserted = 0
    batch_num = 0

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=console
    ) as progress:

        task = progress.add_task(
            f"[cyan]Extraindo com cursor (batch: {batch_size:,})...",
            total=total_records
        )

        # Usa raw connection para server-side cursor
        raw_conn = sei_engine.raw_connection()
        try:
            # Cria cursor nomeado (server-side cursor no PostgreSQL)
            cursor = raw_conn.cursor(name='sei_extract_cursor')
            cursor.itersize = batch_size

            cursor.execute("""
                SELECT
                    protocolo_formatado as protocol,
                    id_protocolo,
                    data_hora,
                    tipo_procedimento,
                    unidade
                FROM sei_processo.sei_atividades
                WHERE descricao_replace = %s
                ORDER BY id
            """, (DESCRICAO_FILTER,))

            records = []
            for row in cursor:
                records.append({
                    'protocol': row[0],
                    'id_protocolo': row[1],
                    'data_hora': row[2],
                    'tipo_procedimento': row[3],
                    'unidade': row[4],
                })

                # Quando atingir batch_size, insere no destino
                if len(records) >= batch_size:
                    batch_num += 1
                    inserted = copy_batch_to_local(local_engine, records)
                    total_inserted += inserted
                    progress.update(task, advance=inserted)
                    logger.debug(f"Batch {batch_num}: {inserted} registros")
                    records = []

            # Insere registros restantes
            if records:
                batch_num += 1
                inserted = copy_batch_to_local(local_engine, records)
                total_inserted += inserted
                progress.update(task, advance=inserted)
                logger.debug(f"Batch {batch_num} (final): {inserted} registros")

            cursor.close()
        finally:
            raw_conn.close()

    return total_inserted


def extract_and_load(use_server_cursor: bool = False):
    """
    Função principal de extração.

    Args:
        use_server_cursor: Se True, usa server-side cursor (melhor para volumes muito grandes).
                          Se False, usa keyset pagination (default, bom equilíbrio).
    """
    setup_logger()

    console.print("\n[bold cyan]═══════════════════════════════════════════════════════════[/bold cyan]")
    console.print("[bold cyan]  Extração de Processos Gerados - SEI Estado do Piauí  [/bold cyan]")
    console.print("[bold cyan]  (Versão Otimizada)  [/bold cyan]")
    console.print("[bold cyan]═══════════════════════════════════════════════════════════[/bold cyan]\n")

    # Cria tabelas
    create_tables_if_not_exists()

    logger.info(f"Conectando ao banco SEI em {settings.sei_db_host}...")
    logger.info(f"Filtrando por descricao_replace = '{DESCRICAO_FILTER}'")

    # Obtém engines (conexões serão reutilizadas)
    sei_engine = get_sei_engine()
    local_engine = get_local_engine()

    # Escolhe método de extração
    batch_size = settings.batch_size

    if use_server_cursor:
        console.print("[yellow]Método: Server-side cursor[/yellow]")
        total_inserted = extract_with_server_cursor(sei_engine, local_engine, batch_size)
    else:
        console.print("[yellow]Método: Keyset pagination[/yellow]")
        total_inserted = extract_with_keyset_pagination(sei_engine, local_engine, batch_size)

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
