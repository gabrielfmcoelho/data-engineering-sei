"""
Script otimizado para consultar metadados de processos via API SEI.

Otimizações implementadas:
1. Producer-Consumer pipeline - Desacopla fetch da API do salvamento no banco
2. Bulk database writes - Insere múltiplos registros em uma única transação
3. Streaming progress - Processa resultados assim que chegam
4. Cache de unidades por órgão - Evita recálculo em chamadas repetidas
5. Configurable concurrency - Permite ajustar paralelismo da API

Este script:
1. Lê processos da tabela sei_processos_temp_etl
2. Consulta API SEI em paralelo (asyncio) para cada processo
3. Acumula resultados em buffer e faz bulk insert quando atinge threshold
4. Atualiza status na tabela sei_etl_status
"""
import sys
import asyncio
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone
from dataclasses import dataclass, field

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from loguru import logger
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn, TimeRemainingColumn, TimeElapsedColumn

from src.config import settings
from src.database.session import get_local_session, get_local_engine
from src.database.models.orm_models import (
    SeiProcessoTempETL,
    SeiProcesso,
    SeiDocumento,
    SeiAndamento,
    SeiETLStatus
)
from src.api.sei_client import SeiAPIClient, SeiPermanentError, SeiUnidadeAccessError


console = Console()


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class ProcessoResult:
    """Resultado do fetch de um processo."""
    protocol: str
    success: bool = False
    data: Optional[Dict[str, Any]] = None
    error_type: Optional[str] = None  # 'permanent', 'access_denied', 'error'
    error_msg: Optional[str] = None
    unidades_tentadas: List[str] = field(default_factory=list)


@dataclass
class BulkWriteStats:
    """Estatísticas do bulk writer."""
    processos_saved: int = 0
    documentos_saved: int = 0
    andamentos_saved: int = 0
    errors: int = 0
    bulk_writes: int = 0


# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================

def clean_json_for_postgres(obj: Any) -> Any:
    """Remove None keys from dictionaries recursively."""
    if isinstance(obj, dict):
        return {
            str(k) if k is not None else 'null': clean_json_for_postgres(v)
            for k, v in obj.items()
        }
    elif isinstance(obj, list):
        return [clean_json_for_postgres(item) for item in obj]
    else:
        return obj


def parse_datetime(date_str: Optional[str]) -> Optional[datetime]:
    """Parse data do formato SEI para datetime."""
    if not date_str:
        return None
    try:
        if ' ' in date_str:
            return datetime.strptime(date_str, "%d/%m/%Y %H:%M:%S")
        return datetime.strptime(date_str, "%d/%m/%Y")
    except ValueError:
        logger.warning(f"Formato de data inválido: {date_str}")
        return None


def setup_logger(log_level: str = "normal"):
    """Configura o logger.

    Args:
        log_level: Nível de log
            - 'verbose': SUCCESS + INFO + WARNING + ERROR (mostra cada processo coletado)
            - 'normal': INFO + WARNING + ERROR (sem SUCCESS, apenas bulk writes)
            - 'quiet': WARNING + ERROR apenas
    """
    logger.remove()

    # Configura filtro baseado no nível
    if log_level == "verbose":
        # Mostra tudo incluindo SUCCESS (cada processo coletado)
        console_level = "INFO"
        filter_func = None
    elif log_level == "quiet":
        # Mostra apenas WARNING e ERROR
        console_level = "WARNING"
        filter_func = None
    else:  # normal
        # Mostra INFO mas filtra SUCCESS (não mostra cada processo)
        console_level = "INFO"
        filter_func = lambda record: record["level"].name != "SUCCESS"

    logger.add(
        sys.stderr,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>",
        level=console_level,
        filter=filter_func
    )

    # Arquivo de log sempre salva tudo
    logger.add(
        "logs/fetch_processos_metadata_{time}.log",
        rotation="100 MB",
        retention="10 days",
        level="DEBUG"
    )


# =============================================================================
# API FETCH FUNCTIONS
# =============================================================================

async def fetch_processo_completo(
    client: SeiAPIClient,
    protocol: str,
    unidade_sigla: str
) -> ProcessoResult:
    """Busca metadados completos de um processo via API.

    Args:
        client: Cliente da API SEI
        protocol: Protocolo do processo
        unidade_sigla: Sigla da unidade do processo

    Returns:
        ProcessoResult com dados ou informações de erro
    """
    result = ProcessoResult(protocol=protocol)

    # Extrai o órgão da sigla
    orgao = unidade_sigla.split('/')[0] if '/' in unidade_sigla else unidade_sigla

    # Obtém todas as unidades do órgão (usa cache interno do client)
    unidades_do_orgao = await client.get_all_unidades_do_orgao(orgao)

    # Obtém ID da unidade original
    id_unidade = await client.get_unidade_id(unidade_sigla)

    if not id_unidade:
        logger.debug(f"[{protocol}] Unidade '{unidade_sigla}' não disponível")
        result.error_type = 'access_denied'
        result.error_msg = f"Unidade '{unidade_sigla}' não disponível"
        return result

    # Prepara lista de unidades para tentar
    unidades_para_tentar = [(unidade_sigla, id_unidade)]
    for sigla, uid in unidades_do_orgao:
        if sigla != unidade_sigla:
            unidades_para_tentar.append((sigla, uid))

    # Tenta cada unidade
    for tentativa_idx, (sigla_tentativa, id_tentativa) in enumerate(unidades_para_tentar):
        try:
            if tentativa_idx == 0:
                logger.debug(f"[{protocol}] Usando unidade: {sigla_tentativa}")
            else:
                logger.debug(f"[{protocol}] Tentando unidade alternativa: {sigla_tentativa}")

            # 1. Consulta processo
            processo_data = await client.consultar_processo(id_tentativa, protocol)

            # 2. Em paralelo: documentos + andamentos
            documentos_task = client.listar_documentos(id_tentativa, protocol)
            andamentos_task = client.listar_andamentos(id_tentativa, protocol)

            documentos_data, andamentos_data = await asyncio.gather(
                documentos_task,
                andamentos_task,
                return_exceptions=True
            )

            # Trata exceções
            if isinstance(documentos_data, Exception):
                logger.error(f"Erro ao buscar documentos de {protocol}: {documentos_data}")
                documentos_data = []

            if isinstance(andamentos_data, Exception):
                logger.error(f"Erro ao buscar andamentos de {protocol}: {andamentos_data}")
                andamentos_data = []

            logger.success(
                f"[{protocol}] ✓ Consultado via {sigla_tentativa}: "
                f"{len(documentos_data)} docs, {len(andamentos_data)} andamentos"
            )

            result.success = True
            result.data = {
                'processo': processo_data,
                'documentos': documentos_data,
                'andamentos': andamentos_data
            }
            return result

        except SeiUnidadeAccessError:
            result.unidades_tentadas.append(sigla_tentativa)
            continue

        except SeiPermanentError as e:
            result.error_type = 'permanent'
            result.error_msg = str(e)
            return result

        except Exception as e:
            logger.error(f"[{protocol}] Erro: {e}")
            result.error_type = 'error'
            result.error_msg = str(e)
            return result

    # Nenhuma unidade teve acesso
    result.error_type = 'access_denied'
    result.error_msg = f"Nenhuma das {len(result.unidades_tentadas)} unidades teve acesso"
    return result


# =============================================================================
# BULK DATABASE OPERATIONS
# =============================================================================

def prepare_processo_data(protocol: str, data: Dict[str, Any]) -> Dict[str, Any]:
    """Prepara dados de um processo para inserção."""
    processo_api = data['processo']
    return {
        'protocol': protocol,
        'id_protocolo': int(processo_api.get('IdProcedimento', 0)),
        'tipo_procedimento': processo_api.get('TipoProcedimento', {}).get('Nome'),
        'especificacao': processo_api.get('Especificacao'),
        'nivel_acesso': processo_api.get('NivelAcesso'),
        'hipotese_legal': processo_api.get('HipoteseLegal'),
        'observacao': processo_api.get('Observacao'),
        'data_abertura': parse_datetime(processo_api.get('DataAutuacao')),
        'data_conclusao': parse_datetime(processo_api.get('DataConclusao')),
        'interessados': processo_api.get('Interessados', []),
        'assuntos': processo_api.get('Assuntos', []),
        'unidade_geradora': processo_api.get('UnidadeGeradora', {}).get('Descricao'),
        'raw_api_response': clean_json_for_postgres(processo_api),
        'fetched_at': datetime.now(timezone.utc),
        'updated_at': datetime.now(timezone.utc)
    }


def prepare_documento_data(protocol: str, processo_id: int, doc_api: Dict[str, Any]) -> Dict[str, Any]:
    """Prepara dados de um documento para inserção."""
    return {
        'processo_id': processo_id,
        'protocol': protocol,
        'id_documento': int(doc_api.get('IdDocumento', 0)),
        'numero_documento': doc_api.get('Numero'),
        'tipo_documento': doc_api.get('Serie', {}).get('Nome'),
        'serie': doc_api.get('Serie', {}).get('Nome'),
        'data_documento': parse_datetime(doc_api.get('Data')),
        'usuario_gerador': doc_api.get('UsuarioGerador'),
        'unidade_geradora': doc_api.get('UnidadeGeradora', {}).get('Descricao'),
        'assinado': doc_api.get('SinAssinado') == 'S',
        'assinantes': doc_api.get('Assinantes', []),
        'nivel_acesso': doc_api.get('NivelAcesso'),
        'raw_api_response': clean_json_for_postgres(doc_api),
        'status': 'pending',
    }


def prepare_andamento_data(protocol: str, processo_id: int, and_api: Dict[str, Any]) -> Dict[str, Any]:
    """Prepara dados de um andamento para inserção."""
    usuario_obj = and_api.get('Usuario', {})
    usuario_str = None
    if isinstance(usuario_obj, dict):
        usuario_str = usuario_obj.get('Sigla') or usuario_obj.get('Nome')
    elif usuario_obj:
        usuario_str = str(usuario_obj)

    return {
        'processo_id': processo_id,
        'protocol': protocol,
        'id_andamento': int(and_api.get('IdAndamento', 0)),
        'tipo_andamento': and_api.get('Tarefa'),
        'descricao': and_api.get('Descricao'),
        'tarefa': and_api.get('Tarefa'),
        'usuario': usuario_str,
        'unidade_origem': and_api.get('Unidade', {}).get('Descricao'),
        'data_hora': parse_datetime(and_api.get('DataHora')),
        'atributos': and_api.get('Atributos', []),
        'raw_api_response': clean_json_for_postgres(and_api),
    }


def bulk_save_processos(results: List[ProcessoResult]) -> BulkWriteStats:
    """Salva múltiplos processos em uma única transação.

    Args:
        results: Lista de ProcessoResult com dados a salvar

    Returns:
        BulkWriteStats com estatísticas da operação
    """
    stats = BulkWriteStats()

    if not results:
        return stats

    # Separa resultados por tipo
    successful = [r for r in results if r.success and r.data]
    permanent_errors = [r for r in results if r.error_type == 'permanent']
    access_denied = [r for r in results if r.error_type == 'access_denied']
    other_errors = [r for r in results if r.error_type == 'error']

    engine = get_local_engine()

    with engine.begin() as conn:
        try:
            # 1. Salva processos com sucesso
            if successful:
                # Primeiro, faz upsert dos processos e obtém IDs
                processos_data = [
                    prepare_processo_data(r.protocol, r.data)
                    for r in successful
                ]

                # Upsert processos
                stmt = insert(SeiProcesso).values(processos_data)
                stmt = stmt.on_conflict_do_update(
                    index_elements=['protocol'],
                    set_={k: stmt.excluded[k] for k in processos_data[0].keys() if k != 'protocol'}
                )
                conn.execute(stmt)

                # Busca IDs dos processos inseridos
                protocols = [r.protocol for r in successful]
                result = conn.execute(
                    text("SELECT id, protocol FROM sei_processos WHERE protocol = ANY(:protocols)"),
                    {"protocols": protocols}
                )
                protocol_to_id = {row[1]: row[0] for row in result}

                # Prepara documentos e andamentos com IDs corretos
                documentos_data = []
                andamentos_data = []

                for r in successful:
                    processo_id = protocol_to_id.get(r.protocol)
                    if not processo_id:
                        continue

                    for doc in r.data.get('documentos', []):
                        documentos_data.append(
                            prepare_documento_data(r.protocol, processo_id, doc)
                        )

                    for and_ in r.data.get('andamentos', []):
                        andamentos_data.append(
                            prepare_andamento_data(r.protocol, processo_id, and_)
                        )

                # Bulk insert documentos (upsert)
                if documentos_data:
                    stmt = insert(SeiDocumento).values(documentos_data)
                    stmt = stmt.on_conflict_do_update(
                        index_elements=['id_documento'],
                        set_={k: stmt.excluded[k] for k in documentos_data[0].keys()
                              if k not in ('id_documento', 'status')}
                    )
                    conn.execute(stmt)
                    stats.documentos_saved = len(documentos_data)

                # Bulk insert andamentos (sem upsert, podem haver duplicatas)
                if andamentos_data:
                    conn.execute(insert(SeiAndamento).values(andamentos_data))
                    stats.andamentos_saved = len(andamentos_data)

                stats.processos_saved = len(successful)

            # 2. Prepara ETL status para todos os resultados
            etl_status_data = []
            now = datetime.now(timezone.utc)

            for r in successful:
                docs_count = len(r.data.get('documentos', [])) if r.data else 0
                ands_count = len(r.data.get('andamentos', [])) if r.data else 0
                etl_status_data.append({
                    'protocol': r.protocol,
                    'metadata_status': 'completed',
                    'metadata_fetched_at': now,
                    'metadata_error': None,
                    'documentos_total': docs_count,
                    'documentos_status': 'pending' if docs_count > 0 else 'completed',
                    'andamentos_total': ands_count,
                    'andamentos_status': 'completed',
                    'updated_at': now
                })

            for r in permanent_errors:
                etl_status_data.append({
                    'protocol': r.protocol,
                    'metadata_status': 'not_found',
                    'metadata_fetched_at': now,
                    'metadata_error': r.error_msg,
                    'updated_at': now
                })
                stats.errors += 1

            for r in access_denied:
                unidades_str = ', '.join(r.unidades_tentadas) if r.unidades_tentadas else 'N/A'
                etl_status_data.append({
                    'protocol': r.protocol,
                    'metadata_status': 'access_denied',
                    'metadata_fetched_at': now,
                    'metadata_error': f"{r.error_msg}. Unidades tentadas: {unidades_str}",
                    'updated_at': now
                })
                stats.errors += 1

            for r in other_errors:
                etl_status_data.append({
                    'protocol': r.protocol,
                    'metadata_status': 'error',
                    'metadata_fetched_at': now,
                    'metadata_error': r.error_msg,
                    'updated_at': now
                })
                stats.errors += 1

            # Bulk upsert ETL status
            if etl_status_data:
                stmt = insert(SeiETLStatus).values(etl_status_data)
                update_cols = ['metadata_status', 'metadata_fetched_at', 'metadata_error', 'updated_at']
                # Adiciona colunas opcionais se presentes
                if 'documentos_total' in etl_status_data[0]:
                    update_cols.extend(['documentos_total', 'documentos_status', 'andamentos_total', 'andamentos_status'])

                stmt = stmt.on_conflict_do_update(
                    index_elements=['protocol'],
                    set_={k: stmt.excluded[k] for k in update_cols}
                )
                conn.execute(stmt)

            stats.bulk_writes = 1
            logger.info(
                f"💾 Bulk write: {stats.processos_saved} processos, "
                f"{stats.documentos_saved} docs, {stats.andamentos_saved} andamentos"
            )

        except Exception as e:
            logger.error(f"Erro no bulk save: {e}")
            raise

    return stats


# =============================================================================
# PRODUCER-CONSUMER PIPELINE WITH TRUE CONCURRENCY
# =============================================================================

async def fetch_with_pipeline(
    client: SeiAPIClient,
    processos: List[tuple],
    bulk_threshold: int = 50,
    max_concurrent: int = 10,
    progress=None,
    task_id=None
) -> BulkWriteStats:
    """Executa fetch com pipeline producer-consumer e concorrência real.

    Args:
        client: Cliente da API SEI
        processos: Lista de tuplas (protocol, unidade)
        bulk_threshold: Quantidade de resultados para acionar bulk insert
        max_concurrent: Máximo de processos sendo buscados simultaneamente
        progress: Objeto Rich Progress (opcional)
        task_id: ID da task no progress (opcional)

    Returns:
        BulkWriteStats com estatísticas totais
    """
    # Queue com backpressure (2x threshold)
    queue: asyncio.Queue[ProcessoResult] = asyncio.Queue(maxsize=bulk_threshold * 2)
    buffer: List[ProcessoResult] = []
    total_stats = BulkWriteStats()
    fetch_done = asyncio.Event()
    items_processed = 0
    items_processed_lock = asyncio.Lock()

    # Semaphore para limitar concorrência de fetches
    fetch_semaphore = asyncio.Semaphore(max_concurrent)

    async def fetch_one(protocol: str, unidade: str):
        """Busca um processo e coloca na queue."""
        nonlocal items_processed

        async with fetch_semaphore:
            result = await fetch_processo_completo(client, protocol, unidade)
            await queue.put(result)

            async with items_processed_lock:
                items_processed += 1
                if progress and task_id:
                    progress.update(task_id, completed=items_processed)

    async def fetcher():
        """Lança todas as tasks de fetch concorrentemente."""
        tasks = [
            asyncio.create_task(fetch_one(protocol, unidade))
            for protocol, unidade in processos
        ]
        # Aguarda todas as tasks completarem
        await asyncio.gather(*tasks, return_exceptions=True)
        fetch_done.set()

    async def writer():
        """Consome queue e faz bulk insert quando buffer enche."""
        nonlocal total_stats

        while not (fetch_done.is_set() and queue.empty()):
            try:
                result = await asyncio.wait_for(queue.get(), timeout=0.5)
                buffer.append(result)
                queue.task_done()

                # Bulk insert quando atingir threshold
                if len(buffer) >= bulk_threshold:
                    stats = bulk_save_processos(buffer)
                    total_stats.processos_saved += stats.processos_saved
                    total_stats.documentos_saved += stats.documentos_saved
                    total_stats.andamentos_saved += stats.andamentos_saved
                    total_stats.errors += stats.errors
                    total_stats.bulk_writes += 1
                    buffer.clear()

            except asyncio.TimeoutError:
                # Flush parcial se tiver dados no buffer e fetcher ainda está rodando
                # Isso evita que dados fiquem parados no buffer por muito tempo
                if buffer and len(buffer) >= bulk_threshold // 2:
                    stats = bulk_save_processos(buffer)
                    total_stats.processos_saved += stats.processos_saved
                    total_stats.documentos_saved += stats.documentos_saved
                    total_stats.andamentos_saved += stats.andamentos_saved
                    total_stats.errors += stats.errors
                    total_stats.bulk_writes += 1
                    buffer.clear()
                continue
            except Exception as e:
                logger.error(f"Erro no writer: {e}")

        # Final flush - insere restante do buffer
        if buffer:
            stats = bulk_save_processos(buffer)
            total_stats.processos_saved += stats.processos_saved
            total_stats.documentos_saved += stats.documentos_saved
            total_stats.andamentos_saved += stats.andamentos_saved
            total_stats.errors += stats.errors
            total_stats.bulk_writes += 1
            buffer.clear()

    # Executa fetcher e writer em paralelo
    await asyncio.gather(fetcher(), writer())

    return total_stats


# =============================================================================
# MAIN FUNCTION
# =============================================================================

async def fetch_all_processos(
    batch_size: int = 50,
    bulk_threshold: int = 50,
    limit: Optional[int] = None,
    orgao: Optional[str] = None,
    data_inicio: Optional[str] = None,
    max_concurrent: Optional[int] = None,
    log_level: str = "normal"
):
    """Busca metadados de todos os processos pendentes.

    Args:
        batch_size: Tamanho do lote para processamento (legado, mantido para compatibilidade)
        bulk_threshold: Quantidade de processos para acionar bulk insert
        limit: Limite de processos a consultar (None = todos)
        orgao: Filtrar por órgão (ex: 'SEAD-PI', 'SEDUC-PI')
        data_inicio: Filtrar processos criados a partir desta data (formato: YYYY-MM-DD)
        max_concurrent: Máximo de requisições concorrentes à API (default: settings)
        log_level: Nível de log ('verbose', 'normal', 'quiet')
    """
    setup_logger(log_level)

    console.print("\n[bold cyan]═══════════════════════════════════════════════════════════[/bold cyan]")
    console.print("[bold cyan]  Consulta de Metadados via API SEI - Versão Otimizada  [/bold cyan]")
    console.print("[bold cyan]═══════════════════════════════════════════════════════════[/bold cyan]\n")

    # Configurações
    api_concurrent = max_concurrent or settings.sei_api_max_concurrent
    console.print(f"[dim]Configurações: bulk_threshold={bulk_threshold}, max_concurrent={api_concurrent}[/dim]")

    # Mostra filtros ativos
    filtros_ativos = []
    if orgao:
        filtros_ativos.append(f"Órgão = {orgao}")
        logger.info(f"Aplicando filtro por órgão: {orgao}")
    if data_inicio:
        filtros_ativos.append(f"Data >= {data_inicio}")
        logger.info(f"Aplicando filtro por data de início: {data_inicio}")

    if filtros_ativos:
        console.print(f"[yellow]Filtros ativos: {', '.join(filtros_ativos)}[/yellow]")

    # Busca processos pendentes
    with get_local_session() as session:
        # Subquery: processos já consultados OU com erro permanente/acesso negado
        subq = select(SeiETLStatus.protocol).where(
            SeiETLStatus.metadata_status.in_(['completed', 'not_found', 'access_denied'])
        ).scalar_subquery()

        # Query base
        stmt = (
            select(SeiProcessoTempETL.protocol, SeiProcessoTempETL.unidade)
            .where(~SeiProcessoTempETL.protocol.in_(subq))
        )

        # Aplica filtros
        if orgao:
            stmt = stmt.where(SeiProcessoTempETL.unidade.like(f"{orgao}%"))

        if data_inicio:
            try:
                data_filtro = datetime.strptime(data_inicio, "%Y-%m-%d")
                stmt = stmt.where(SeiProcessoTempETL.data_hora >= data_filtro)
            except ValueError:
                console.print(f"[red]Erro: Data inválida '{data_inicio}'. Use YYYY-MM-DD[/red]")
                return

        stmt = stmt.order_by(SeiProcessoTempETL.data_hora.desc())

        if limit:
            stmt = stmt.limit(limit)

        result = session.execute(stmt)
        processos = [(row[0], row[1]) for row in result]

        # Estatísticas
        total_processos = session.query(SeiProcessoTempETL).count()

        if orgao or data_inicio:
            query_filtrada = session.query(SeiProcessoTempETL)
            if orgao:
                query_filtrada = query_filtrada.filter(SeiProcessoTempETL.unidade.like(f"{orgao}%"))
            if data_inicio:
                data_filtro = datetime.strptime(data_inicio, "%Y-%m-%d")
                query_filtrada = query_filtrada.filter(SeiProcessoTempETL.data_hora >= data_filtro)
            total_orgao = query_filtrada.count()

            query_consultados = session.query(SeiProcessoTempETL).join(
                SeiETLStatus, SeiProcessoTempETL.protocol == SeiETLStatus.protocol
            )
            if orgao:
                query_consultados = query_consultados.filter(SeiProcessoTempETL.unidade.like(f"{orgao}%"))
            if data_inicio:
                query_consultados = query_consultados.filter(SeiProcessoTempETL.data_hora >= data_filtro)
            query_consultados = query_consultados.filter(
                SeiETLStatus.metadata_status.in_(['completed', 'not_found', 'access_denied'])
            )
            ja_consultados = query_consultados.count()
        else:
            total_orgao = total_processos
            ja_consultados = session.query(SeiETLStatus).filter(
                SeiETLStatus.metadata_status.in_(['completed', 'not_found', 'access_denied'])
            ).count()

    # Mostra estatísticas
    console.print("\n[bold]Estatísticas:[/bold]")
    console.print(f"  Total de processos no banco: {total_processos:,}")

    if orgao or data_inicio:
        filtro_label = []
        if orgao:
            filtro_label.append(f"órgão {orgao}")
        if data_inicio:
            filtro_label.append(f"data >= {data_inicio}")
        filtro_str = " + ".join(filtro_label)
        console.print(f"  Processos com filtro ({filtro_str}): {total_orgao:,}")
        console.print(f"  Já consultados (filtro): {ja_consultados:,}")
        console.print(f"  Pendentes (filtro): {total_orgao - ja_consultados:,}")
    else:
        console.print(f"  Já consultados (geral): {ja_consultados:,}")
        console.print(f"  Pendentes (geral): {total_processos - ja_consultados:,}")

    if not processos:
        console.print("\n[yellow]Nenhum processo pendente para consultar![/yellow]")
        console.print("[green]Todos os processos do filtro já foram consultados.[/green]\n")
        return

    total = len(processos)
    percentual = (total / total_orgao * 100) if total_orgao > 0 else 0

    console.print(f"\n[bold green]Processos a consultar: {total:,} ({percentual:.1f}%)[/bold green]")

    if limit and total >= limit:
        console.print(f"[yellow](Limitado a {limit:,} pela opção --limit)[/yellow]")

    console.print()
    logger.info(f"Iniciando consulta de {total} processos com pipeline otimizado")

    # Inicia cliente API com concorrência configurável
    async with SeiAPIClient(
        base_url=settings.sei_api_base_url,
        usuario=settings.sei_api_user,
        senha=settings.sei_api_password,
        orgao=settings.sei_api_orgao,
        max_concurrent=api_concurrent,
        timeout=settings.sei_api_timeout
    ) as client:

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
            console=console
        ) as progress:

            task = progress.add_task(
                f"[cyan]Consultando (concurrent={api_concurrent}, bulk={bulk_threshold})...",
                total=total
            )

            # Executa pipeline com concorrência real
            stats = await fetch_with_pipeline(
                client=client,
                processos=processos,
                bulk_threshold=bulk_threshold,
                max_concurrent=api_concurrent,
                progress=progress,
                task_id=task
            )

    # Resultado final
    console.print(f"\n[bold green]Consulta concluída![/bold green]")
    console.print(f"[bold]Estatísticas finais:[/bold]")
    console.print(f"  Processos salvos: {stats.processos_saved:,}")
    console.print(f"  Documentos salvos: {stats.documentos_saved:,}")
    console.print(f"  Andamentos salvos: {stats.andamentos_saved:,}")
    console.print(f"  Erros/não encontrados: {stats.errors:,}")
    console.print(f"  Bulk writes executados: {stats.bulk_writes:,}")
    console.print()

    logger.success(
        f"Pipeline finalizado: {stats.processos_saved} processos, "
        f"{stats.documentos_saved} docs, {stats.andamentos_saved} andamentos, "
        f"{stats.bulk_writes} bulk writes"
    )


def main():
    """Função principal."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Consulta otimizada de metadados de processos via API SEI",
        epilog="Exemplos:\n"
               "  python -m src.scripts.fetch_processos_metadata --orgao SEAD-PI --limit 1000\n"
               "  python -m src.scripts.fetch_processos_metadata --bulk-threshold 100 --max-concurrent 20\n"
               "  python -m src.scripts.fetch_processos_metadata --data-inicio 2025-01-01",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=50,
        help="[Legado] Tamanho do lote (padrão: 50)"
    )
    parser.add_argument(
        "--bulk-threshold",
        type=int,
        default=50,
        help="Quantidade de processos para acionar bulk insert (padrão: 50)"
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Limite de processos a consultar (padrão: todos)"
    )
    parser.add_argument(
        "--orgao",
        type=str,
        help="Filtrar por órgão (ex: SEAD-PI, SEDUC-PI)"
    )
    parser.add_argument(
        "--data-inicio",
        type=str,
        help="Filtrar processos criados a partir desta data (formato: YYYY-MM-DD)"
    )
    parser.add_argument(
        "--max-concurrent",
        type=int,
        help=f"Máximo de requisições concorrentes à API (padrão: {settings.sei_api_max_concurrent})"
    )
    parser.add_argument(
        "--log-level",
        type=str,
        choices=["verbose", "normal", "quiet"],
        default="normal",
        help="Nível de log: verbose (mostra cada processo), normal (apenas bulk writes), quiet (apenas erros)"
    )

    args = parser.parse_args()

    try:
        asyncio.run(fetch_all_processos(
            batch_size=args.batch_size,
            bulk_threshold=args.bulk_threshold,
            limit=args.limit,
            orgao=args.orgao,
            data_inicio=args.data_inicio,
            max_concurrent=args.max_concurrent,
            log_level=args.log_level
        ))
    except KeyboardInterrupt:
        console.print("\n[yellow]Processo interrompido pelo usuário.[/yellow]")
        logger.warning("Processo interrompido pelo usuário")
        sys.exit(1)
    except Exception as e:
        console.print(f"\n[bold red]Erro durante a execução: {e}[/bold red]")
        logger.exception("Erro durante a consulta")
        sys.exit(1)


if __name__ == "__main__":
    main()
