"""
Script para consultar metadados de processos via API SEI em paralelo.

Este script:
1. Lê processos da tabela sei_processos_temp_etl
2. Consulta API SEI em paralelo (asyncio) para cada processo:
   - Metadados do processo
   - Lista de documentos
   - Lista de andamentos
3. Salva tudo no Postgres (sei_processos, sei_documentos, sei_andamentos)
4. Atualiza status na tabela sei_etl_status
"""
import sys
import asyncio
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from sqlalchemy import select, and_
from sqlalchemy.dialects.postgresql import insert
from loguru import logger
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn, TimeRemainingColumn

from src.config import settings
from src.database.session import get_local_session
from src.database.models.orm_models import (
    SeiProcessoTempETL,
    SeiProcesso,
    SeiDocumento,
    SeiAndamento,
    SeiETLStatus
)
from src.api.sei_client import SeiAPIClient, SeiPermanentError, SeiUnidadeAccessError


console = Console()


def clean_json_for_postgres(obj: Any) -> Any:
    """Remove None keys from dictionaries recursively.

    PostgreSQL JSON columns don't support None as dictionary keys.
    This function recursively cleans the data structure.

    Args:
        obj: Any JSON-serializable object

    Returns:
        Cleaned object with no None keys
    """
    if isinstance(obj, dict):
        # Remove None keys and recursively clean values
        return {
            str(k) if k is not None else 'null': clean_json_for_postgres(v)
            for k, v in obj.items()
        }
    elif isinstance(obj, list):
        # Recursively clean list items
        return [clean_json_for_postgres(item) for item in obj]
    else:
        # Return primitive values as-is
        return obj


def setup_logger():
    """Configura o logger."""
    logger.remove()
    logger.add(
        sys.stderr,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>",
        level="INFO"
    )
    logger.add(
        "logs/fetch_processos_metadata_{time}.log",
        rotation="100 MB",
        retention="10 days",
        level="DEBUG"
    )


async def fetch_processo_completo(
    client: SeiAPIClient,
    protocol: str,
    unidade_sigla: str
) -> Optional[Dict[str, Any]]:
    """Busca metadados completos de um processo via API.

    Args:
        client: Cliente da API SEI
        protocol: Protocolo do processo
        unidade_sigla: Sigla da unidade do processo (ex: "SEAD-PI/GAB")

    Returns:
        Dicionário com processo, documentos e andamentos
        Retorna dict especial {'_permanent_error': True, '_error_msg': str} para erros permanentes
    """
    # Extrai o órgão da sigla (ex: "SEAD-PI" de "SEAD-PI/GAB/SUPARC")
    orgao = unidade_sigla.split('/')[0] if '/' in unidade_sigla else unidade_sigla

    # Obtém todas as unidades do órgão para tentar em caso de erro de acesso
    unidades_do_orgao = await client.get_all_unidades_do_orgao(orgao)

    # Lista de unidades a tentar (começa com a unidade original)
    id_unidade = await client.get_unidade_id(unidade_sigla)

    if not id_unidade:
        logger.error(f"[{protocol}] ❌ Unidade '{unidade_sigla}' não disponível no acesso do usuário")
        return None

    # Prepara lista de unidades para tentar (original primeiro, depois outras do mesmo órgão)
    unidades_para_tentar = [(unidade_sigla, id_unidade)]

    # Adiciona outras unidades do mesmo órgão que ainda não foram tentadas
    for sigla, uid in unidades_do_orgao:
        if sigla != unidade_sigla:
            unidades_para_tentar.append((sigla, uid))

    # Tenta cada unidade até uma funcionar
    unidades_tentadas = []
    last_error = None

    for tentativa_idx, (sigla_tentativa, id_tentativa) in enumerate(unidades_para_tentar):
        try:
            if tentativa_idx == 0:
                logger.info(f"[{protocol}] Usando unidade: {sigla_tentativa} (ID: {id_tentativa})")
            else:
                logger.info(f"[{protocol}] Tentando unidade alternativa: {sigla_tentativa} (ID: {id_tentativa})")

            # 1. Consulta processo
            processo_data = await client.consultar_processo(id_tentativa, protocol)

            # 2. Em paralelo: documentos + andamentos
            logger.debug(f"Buscando documentos e andamentos do processo {protocol}")

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
                f"[{protocol}] ✓ Consultado via unidade {sigla_tentativa}: "
                f"{len(documentos_data)} documentos, {len(andamentos_data)} andamentos"
            )

            return {
                'processo': processo_data,
                'documentos': documentos_data,
                'andamentos': andamentos_data
            }

        except SeiUnidadeAccessError as e:
            # Unidade não tem acesso - tenta próxima
            unidades_tentadas.append(sigla_tentativa)
            last_error = str(e)
            logger.debug(f"[{protocol}] Unidade {sigla_tentativa} sem acesso, tentando próxima...")
            continue

        except SeiPermanentError as e:
            # Erro permanente (processo não encontrado, etc) - não tenta outras unidades
            logger.warning(f"[{protocol}] ⚠ Erro permanente: {e}")
            return {
                '_permanent_error': True,
                '_error_msg': str(e)
            }

        except Exception as e:
            logger.error(f"[{protocol}] ❌ Erro ao consultar via unidade {sigla_tentativa}: {e}")
            last_error = str(e)
            # Para outros erros, não tenta outras unidades (pode ser timeout, etc)
            return None

    # Se chegou aqui, nenhuma unidade teve acesso
    logger.warning(
        f"[{protocol}] ❌ Nenhuma unidade do órgão {orgao} teve acesso ao processo. "
        f"Tentadas: {len(unidades_tentadas)} unidades"
    )

    # Marca como erro de acesso (não permanente, pode ser reprocessado se o usuário ganhar acesso)
    return {
        '_access_denied': True,
        '_error_msg': f"Nenhuma das {len(unidades_tentadas)} unidades do órgão {orgao} teve acesso ao processo",
        '_unidades_tentadas': unidades_tentadas
    }


def save_processo_to_db(data: Dict[str, Any], protocol: str):
    """Salva processo, documentos e andamentos no banco.

    Args:
        data: Dados retornados da API
        protocol: Protocolo do processo
    """
    with get_local_session() as session:
        try:
            processo_api = data['processo']
            documentos_api = data['documentos']
            andamentos_api = data['andamentos']

            # 1. Salva/atualiza processo
            processo_dict = {
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

            # Upsert processo
            stmt = insert(SeiProcesso).values(**processo_dict)
            stmt = stmt.on_conflict_do_update(
                index_elements=['protocol'],
                set_=processo_dict
            )
            session.execute(stmt)
            session.flush()

            # Busca o processo inserido/atualizado
            processo = session.query(SeiProcesso).filter_by(protocol=protocol).first()

            # 2. Salva documentos
            for doc_api in documentos_api:
                doc_dict = {
                    'processo_id': processo.id,
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
                    'status': 'pending',  # Pendente para download
                }

                # Upsert documento
                stmt = insert(SeiDocumento).values(**doc_dict)
                stmt = stmt.on_conflict_do_update(
                    index_elements=['id_documento'],
                    set_={k: v for k, v in doc_dict.items() if k != 'status'}  # Mantém status atual
                )
                session.execute(stmt)

            # 3. Salva andamentos
            for and_api in andamentos_api:
                # Extrai usuario (Sigla ou Nome)
                usuario_obj = and_api.get('Usuario', {})
                usuario_str = usuario_obj.get('Sigla') or usuario_obj.get('Nome') if isinstance(usuario_obj, dict) else str(usuario_obj) if usuario_obj else None

                and_dict = {
                    'processo_id': processo.id,
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

                session.add(SeiAndamento(**and_dict))

            # 4. Atualiza status ETL
            etl_dict = {
                'protocol': protocol,
                'metadata_status': 'completed',
                'metadata_fetched_at': datetime.now(timezone.utc),
                'documentos_total': len(documentos_api),
                'documentos_status': 'pending' if documentos_api else 'completed',
                'andamentos_total': len(andamentos_api),
                'andamentos_status': 'completed',
                'updated_at': datetime.now(timezone.utc)
            }

            stmt = insert(SeiETLStatus).values(**etl_dict)
            stmt = stmt.on_conflict_do_update(
                index_elements=['protocol'],
                set_=etl_dict
            )
            session.execute(stmt)

            session.commit()
            logger.debug(f"Processo {protocol} salvo no banco")

        except Exception as e:
            session.rollback()
            logger.error(f"Erro ao salvar processo {protocol} no banco: {e}")

            # Marca erro no ETL status
            try:
                etl_error = {
                    'protocol': protocol,
                    'metadata_status': 'error',
                    'metadata_error': str(e),
                    'updated_at': datetime.now(timezone.utc)
                }
                stmt = insert(SeiETLStatus).values(**etl_error)
                stmt = stmt.on_conflict_do_update(
                    index_elements=['protocol'],
                    set_={'metadata_status': 'error', 'metadata_error': str(e)}
                )
                session.execute(stmt)
                session.commit()
            except:
                pass


def save_permanent_error_to_db(protocol: str, error_msg: str):
    """Salva erro permanente no banco (processo não encontrado, etc).

    Args:
        protocol: Protocolo do processo
        error_msg: Mensagem de erro
    """
    with get_local_session() as session:
        try:
            etl_dict = {
                'protocol': protocol,
                'metadata_status': 'not_found',
                'metadata_error': error_msg,
                'metadata_fetched_at': datetime.now(timezone.utc),
                'updated_at': datetime.now(timezone.utc)
            }

            stmt = insert(SeiETLStatus).values(**etl_dict)
            stmt = stmt.on_conflict_do_update(
                index_elements=['protocol'],
                set_=etl_dict
            )
            session.execute(stmt)
            session.commit()

            logger.info(f"Processo {protocol} marcado como 'not_found' (não será retentado)")

        except Exception as e:
            session.rollback()
            logger.error(f"Erro ao salvar status 'not_found' para {protocol}: {e}")


def save_access_denied_to_db(protocol: str, error_msg: str, unidades_tentadas: List[str]):
    """Salva erro de acesso negado no banco.

    Args:
        protocol: Protocolo do processo
        error_msg: Mensagem de erro
        unidades_tentadas: Lista de unidades que foram tentadas
    """
    with get_local_session() as session:
        try:
            etl_dict = {
                'protocol': protocol,
                'metadata_status': 'access_denied',
                'metadata_error': f"{error_msg}. Unidades tentadas: {', '.join(unidades_tentadas)}",
                'metadata_fetched_at': datetime.now(timezone.utc),
                'updated_at': datetime.now(timezone.utc)
            }

            stmt = insert(SeiETLStatus).values(**etl_dict)
            stmt = stmt.on_conflict_do_update(
                index_elements=['protocol'],
                set_=etl_dict
            )
            session.execute(stmt)
            session.commit()

            logger.info(
                f"Processo {protocol} marcado como 'access_denied' "
                f"({len(unidades_tentadas)} unidades tentadas)"
            )

        except Exception as e:
            session.rollback()
            logger.error(f"Erro ao salvar status 'access_denied' para {protocol}: {e}")


def parse_datetime(date_str: Optional[str]) -> Optional[datetime]:
    """Parse data do formato SEI para datetime.

    Args:
        date_str: String de data no formato "DD/MM/YYYY" ou "DD/MM/YYYY HH:MM:SS"

    Returns:
        Objeto datetime ou None
    """
    if not date_str:
        return None

    try:
        # Tenta com hora
        if ' ' in date_str:
            return datetime.strptime(date_str, "%d/%m/%Y %H:%M:%S")
        # Apenas data
        return datetime.strptime(date_str, "%d/%m/%Y")
    except ValueError:
        logger.warning(f"Formato de data inválido: {date_str}")
        return None


async def process_batch(
    client: SeiAPIClient,
    processos: List[tuple],
    progress,
    task_id
):
    """Processa um lote de processos em paralelo.

    Args:
        client: Cliente da API
        processos: Lista de tuplas (protocol, unidade)
        progress: Objeto Rich Progress
        task_id: ID da task no progress
    """
    tasks = [
        fetch_processo_completo(client, protocol, unidade)
        for protocol, unidade in processos
    ]

    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Salva resultados no banco
    saved = 0
    for (protocol, _), result in zip(processos, results):
        if isinstance(result, Exception):
            logger.error(f"Erro ao processar {protocol}: {result}")
        elif result:
            # Verifica se é erro permanente
            if result.get('_permanent_error'):
                save_permanent_error_to_db(protocol, result['_error_msg'])
            # Verifica se é erro de acesso negado
            elif result.get('_access_denied'):
                save_access_denied_to_db(
                    protocol,
                    result['_error_msg'],
                    result.get('_unidades_tentadas', [])
                )
            else:
                # Sucesso - salva processo
                save_processo_to_db(result, protocol)
                saved += 1

        progress.update(task_id, advance=1)

    return saved


async def fetch_all_processos(
    batch_size: int = 50,
    limit: Optional[int] = None,
    orgao: Optional[str] = None,
    data_inicio: Optional[str] = None
):
    """Busca metadados de todos os processos pendentes.

    Args:
        batch_size: Tamanho do lote para processamento paralelo
        limit: Limite de processos a consultar (None = todos)
        orgao: Filtrar por órgão (ex: 'SEAD-PI', 'SEDUC-PI') - opcional
        data_inicio: Filtrar processos criados a partir desta data (formato: YYYY-MM-DD) - opcional
    """
    setup_logger()

    console.print("\n[bold cyan]═══════════════════════════════════════════════════════════[/bold cyan]")
    console.print("[bold cyan]  Consulta de Metadados via API SEI - Execução Paralela  [/bold cyan]")
    console.print("[bold cyan]═══════════════════════════════════════════════════════════[/bold cyan]\n")

    # Mostra informações de filtro
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
        # Subquery: processos já consultados OU com erro permanente/acesso negado (não retentável)
        subq = select(SeiETLStatus.protocol).where(
            SeiETLStatus.metadata_status.in_(['completed', 'not_found', 'access_denied'])
        ).scalar_subquery()

        # Query base: processos pendentes (não consultados, sem erro permanente, sem acesso negado)
        stmt = (
            select(SeiProcessoTempETL.protocol, SeiProcessoTempETL.unidade)
            .where(~SeiProcessoTempETL.protocol.in_(subq))
        )

        # Aplica filtro por órgão se especificado
        if orgao:
            stmt = stmt.where(SeiProcessoTempETL.unidade.like(f"{orgao}%"))

        # Aplica filtro por data se especificado
        if data_inicio:
            try:
                data_filtro = datetime.strptime(data_inicio, "%Y-%m-%d")
                stmt = stmt.where(SeiProcessoTempETL.data_hora >= data_filtro)
            except ValueError:
                console.print(f"[red]Erro: Data inválida '{data_inicio}'. Use o formato YYYY-MM-DD[/red]")
                logger.error(f"Formato de data inválido: {data_inicio}")
                return

        stmt = stmt.order_by(SeiProcessoTempETL.data_hora.desc())

        if limit:
            stmt = stmt.limit(limit)

        result = session.execute(stmt)
        # Lista de tuplas (protocol, unidade)
        processos = [(row[0], row[1]) for row in result]

        # Estatísticas gerais
        total_processos = session.query(SeiProcessoTempETL).count()

        # Constrói queries de estatísticas respeitando os filtros
        if orgao or data_inicio:
            query_filtrada = session.query(SeiProcessoTempETL)
            if orgao:
                query_filtrada = query_filtrada.filter(
                    SeiProcessoTempETL.unidade.like(f"{orgao}%")
                )
            if data_inicio:
                data_filtro = datetime.strptime(data_inicio, "%Y-%m-%d")
                query_filtrada = query_filtrada.filter(
                    SeiProcessoTempETL.data_hora >= data_filtro
                )

            total_orgao = query_filtrada.count()

            query_consultados = session.query(SeiProcessoTempETL).join(
                SeiETLStatus,
                SeiProcessoTempETL.protocol == SeiETLStatus.protocol
            )
            if orgao:
                query_consultados = query_consultados.filter(
                    SeiProcessoTempETL.unidade.like(f"{orgao}%")
                )
            if data_inicio:
                data_filtro = datetime.strptime(data_inicio, "%Y-%m-%d")
                query_consultados = query_consultados.filter(
                    SeiProcessoTempETL.data_hora >= data_filtro
                )
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
        console.print("\n[yellow]✓ Nenhum processo pendente para consultar![/yellow]")
        console.print("[green]Todos os processos do filtro já foram consultados.[/green]\n")
        logger.info("Nenhum processo pendente encontrado")
        return

    total = len(processos)
    percentual = (total / total_orgao * 100) if total_orgao > 0 else 0

    console.print(f"\n[bold green]Processos a consultar nesta execução: {total:,} ({percentual:.1f}%)[/bold green]")

    if limit and total >= limit:
        console.print(f"[yellow](Limitado a {limit:,} pela opção --limit)[/yellow]")

    console.print()
    logger.info(f"Iniciando consulta de {total} processos")

    # Inicia cliente API
    async with SeiAPIClient(
        base_url=settings.sei_api_base_url,
        usuario=settings.sei_api_user,
        senha=settings.sei_api_password,
        orgao=settings.sei_api_orgao,
        max_concurrent=settings.sei_api_max_concurrent,
        timeout=settings.sei_api_timeout
    ) as client:

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TimeRemainingColumn(),
            console=console
        ) as progress:

            task = progress.add_task(
                f"[cyan]Consultando processos (lote: {batch_size})...",
                total=total
            )

            # Processa em lotes
            total_saved = 0
            for i in range(0, total, batch_size):
                batch = processos[i:i + batch_size]
                saved = await process_batch(client, batch, progress, task)
                total_saved += saved

    console.print(f"\n[bold green]✓ Consulta concluída![/bold green]")
    console.print(f"[bold green]  Total de processos salvos: {total_saved:,}/{total:,}[/bold green]\n")
    logger.success(f"Consulta finalizada: {total_saved}/{total} processos salvos")


def main():
    """Função principal."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Consulta metadados de processos via API SEI",
        epilog="Exemplos:\n"
               "  python -m src.scripts.fetch_processos_metadata --orgao SEAD-PI --limit 1000\n"
               "  python -m src.scripts.fetch_processos_metadata --data-inicio 2025-01-01\n"
               "  python -m src.scripts.fetch_processos_metadata --orgao SEDUC-PI --data-inicio 2025-01-01 --limit 500",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--batch-size", type=int, default=50, help="Tamanho do lote paralelo (padrão: 50)")
    parser.add_argument("--limit", type=int, help="Limite de processos a consultar (padrão: todos)")
    parser.add_argument(
        "--orgao",
        type=str,
        help="Filtrar por órgão (ex: SEAD-PI, SEDUC-PI). Busca processos onde unidade inicia com este valor"
    )
    parser.add_argument(
        "--data-inicio",
        type=str,
        help="Filtrar processos criados a partir desta data (formato: YYYY-MM-DD, ex: 2025-01-01)"
    )

    args = parser.parse_args()

    try:
        asyncio.run(fetch_all_processos(
            batch_size=args.batch_size,
            limit=args.limit,
            orgao=args.orgao,
            data_inicio=args.data_inicio
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
