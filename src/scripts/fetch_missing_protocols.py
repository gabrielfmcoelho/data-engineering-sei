"""
Script para consultar protocolos que ainda não possuem andamentos no banco.

Este script:
1. Lê protocolos do arquivo data/processos-cgfr-geral.csv
2. Verifica quais protocolos já possuem andamentos no banco
3. Busca via API SEI apenas os protocolos SEM andamentos
4. Salva os dados encontrados no banco local
5. Gera relatório de sucesso/falha
"""
import sys
import asyncio
import pandas as pd
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from sqlalchemy.dialects.postgresql import insert
from loguru import logger
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn, TimeRemainingColumn
from rich.table import Table

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


def setup_logger():
    """Configura o logger."""
    logger.remove()
    logger.add(
        sys.stderr,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>",
        level="INFO"
    )
    logger.add(
        "logs/fetch_missing_protocols_{time}.log",
        rotation="100 MB",
        retention="10 days",
        level="DEBUG"
    )


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


def safe_str(value: Any, default: str = '') -> str:
    """Convert value to string, handling NaN and None values.

    Args:
        value: Value to convert
        default: Default value to return if value is NaN or None

    Returns:
        String representation of value or default
    """
    if pd.isna(value) or value is None:
        return default
    return str(value)


def parse_datetime(date_str: Optional[str]) -> Optional[datetime]:
    """Parse data do formato SEI ou ISO para datetime.

    Aceita os formatos:
    - DD/MM/YYYY HH:MM:SS (formato SEI/brasileiro)
    - DD/MM/YYYY (formato SEI/brasileiro)
    - YYYY-MM-DD HH:MM:SS (formato ISO)
    - YYYY-MM-DD (formato ISO)
    """
    if not date_str:
        return None

    date_str = str(date_str).strip()

    # Lista de formatos para tentar
    formats = [
        "%d/%m/%Y %H:%M:%S",  # Brasileiro com hora
        "%d/%m/%Y",            # Brasileiro sem hora
        "%Y-%m-%d %H:%M:%S",  # ISO com hora
        "%Y-%m-%d",            # ISO sem hora
    ]

    for fmt in formats:
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue

    # Se nenhum formato funcionou
    logger.warning(f"Formato de data inválido: {date_str}")
    return None


async def fetch_processo_completo(
    client: SeiAPIClient,
    protocol: str,
    unidade_sigla: str
) -> Optional[Dict[str, Any]]:
    """Busca metadados completos de um processo via API."""
    orgao = unidade_sigla.split('/')[0] if '/' in unidade_sigla else unidade_sigla

    # Obtém todas as unidades do órgão
    unidades_do_orgao = await client.get_all_unidades_do_orgao(orgao)

    # Lista de unidades a tentar
    id_unidade = await client.get_unidade_id(unidade_sigla)

    if not id_unidade:
        logger.error(f"[{protocol}] ❌ Unidade '{unidade_sigla}' não disponível")
        return None

    unidades_para_tentar = [(unidade_sigla, id_unidade)]

    for sigla, uid in unidades_do_orgao:
        if sigla != unidade_sigla:
            unidades_para_tentar.append((sigla, uid))

    # Tenta cada unidade
    unidades_tentadas = []
    last_error = None

    for tentativa_idx, (sigla_tentativa, id_tentativa) in enumerate(unidades_para_tentar):
        try:
            if tentativa_idx == 0:
                logger.info(f"[{protocol}] Usando unidade: {sigla_tentativa} (ID: {id_tentativa})")
            else:
                logger.info(f"[{protocol}] Tentando unidade alternativa: {sigla_tentativa} (ID: {id_tentativa})")

            # Consulta processo
            processo_data = await client.consultar_processo(id_tentativa, protocol)

            # Busca documentos e andamentos em paralelo
            documentos_task = client.listar_documentos(id_tentativa, protocol)
            andamentos_task = client.listar_andamentos(id_tentativa, protocol)

            documentos_data, andamentos_data = await asyncio.gather(
                documentos_task,
                andamentos_task,
                return_exceptions=True
            )

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
                'andamentos': andamentos_data,
                'unidade_usada': sigla_tentativa
            }

        except SeiUnidadeAccessError as e:
            unidades_tentadas.append(sigla_tentativa)
            last_error = str(e)
            logger.debug(f"[{protocol}] Unidade {sigla_tentativa} sem acesso, tentando próxima...")
            continue

        except SeiPermanentError as e:
            logger.warning(f"[{protocol}] ⚠ Erro permanente: {e}")
            return {
                '_permanent_error': True,
                '_error_msg': str(e)
            }

        except Exception as e:
            logger.error(f"[{protocol}] ❌ Erro ao consultar via unidade {sigla_tentativa}: {e}")
            last_error = str(e)
            return None

    logger.warning(
        f"[{protocol}] ❌ Nenhuma unidade do órgão {orgao} teve acesso ao processo. "
        f"Tentadas: {len(unidades_tentadas)} unidades"
    )

    return {
        '_access_denied': True,
        '_error_msg': f"Nenhuma das {len(unidades_tentadas)} unidades do órgão {orgao} teve acesso",
        '_unidades_tentadas': unidades_tentadas
    }


def save_processo_to_db(data: Dict[str, Any], protocol: str):
    """Salva processo, documentos e andamentos no banco."""
    with get_local_session() as session:
        try:
            processo_api = data['processo']
            documentos_api = data['documentos']
            andamentos_api = data['andamentos']

            # Salva processo
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

            stmt = insert(SeiProcesso).values(**processo_dict)
            stmt = stmt.on_conflict_do_update(
                index_elements=['protocol'],
                set_=processo_dict
            )
            session.execute(stmt)
            session.flush()

            processo = session.query(SeiProcesso).filter_by(protocol=protocol).first()

            # Salva documentos
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
                    'status': 'pending',
                }

                stmt = insert(SeiDocumento).values(**doc_dict)
                stmt = stmt.on_conflict_do_update(
                    index_elements=['id_documento'],
                    set_={k: v for k, v in doc_dict.items() if k != 'status'}
                )
                session.execute(stmt)

            # Salva andamentos
            for and_api in andamentos_api:
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

            # Atualiza status ETL
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
            raise


def check_protocol_has_andamentos(protocol: str) -> bool:
    """Verifica se um protocolo já possui andamentos no banco.

    Returns:
        bool: True se possui andamentos, False caso contrário
    """
    with get_local_session() as session:
        try:
            count = session.query(SeiAndamento).filter_by(protocol=protocol).count()
            return count > 0
        except Exception as e:
            logger.error(f"Erro ao verificar andamentos de {protocol}: {e}")
            return False


def check_and_save_to_temp_etl(protocol: str, row_data: Dict[str, Any]) -> bool:
    """Verifica se protocolo existe em sei_processos_temp_etl e salva se não existir.

    Returns:
        bool: True se já existia, False se foi inserido agora
    """
    with get_local_session() as session:
        try:
            # Verifica se já existe
            existing = session.query(SeiProcessoTempETL).filter_by(protocol=protocol).first()

            if existing:
                logger.debug(f"[{protocol}] Já existe em sei_processos_temp_etl")
                return True

            # Não existe - insere
            temp_dict = {
                'protocol': protocol,
                'id_protocolo': safe_str(row_data.get('id_unidade_geradora')),
                'data_hora': parse_datetime(safe_str(row_data.get('geracao_data'))) or datetime.now(timezone.utc),
                'tipo_procedimento': safe_str(row_data.get('tipo_processo')),
                'unidade': safe_str(row_data.get('geracao_sigla')),
                'created_at': datetime.now(timezone.utc)
            }

            session.add(SeiProcessoTempETL(**temp_dict))
            session.commit()
            logger.debug(f"[{protocol}] Inserido em sei_processos_temp_etl")
            return False

        except Exception as e:
            session.rollback()
            logger.error(f"Erro ao verificar/salvar {protocol} em temp_etl: {e}")
            return True  # Assume que existe em caso de erro


async def fetch_missing_protocols(csv_path: str, batch_size: int = 50):
    """Busca os protocolos faltantes do arquivo CSV."""
    setup_logger()

    console.print("\n[bold cyan]═══════════════════════════════════════════════════════════[/bold cyan]")
    console.print("[bold cyan]  Busca de Protocolos Sem Andamentos - CGFR  [/bold cyan]")
    console.print("[bold cyan]═══════════════════════════════════════════════════════════[/bold cyan]\n")

    console.print("[bold]Etapas do processo:[/bold]")
    console.print("  1. Ler arquivo CSV com protocolos (processos-cgfr-geral.csv)")
    console.print("  2. Verificar quais protocolos já possuem andamentos no banco")
    console.print("  3. Verificar/inserir em sei_processos_temp_etl")
    console.print("  4. Buscar metadados via API SEI (apenas protocolos SEM andamentos)")
    console.print("  5. Salvar em sei_processos, sei_documentos, sei_andamentos")
    console.print("  6. Gerar relatório detalhado\n")

    # Lê CSV (com tratamento de linhas malformadas)
    try:
        # Primeiro tenta ler normalmente para contar linhas no arquivo
        import subprocess
        result = subprocess.run(['wc', '-l', csv_path], capture_output=True, text=True)
        total_lines = int(result.stdout.split()[0]) - 1  # -1 para header

        # CSV brasileiro usa ; como separador
        df = pd.read_csv(csv_path, sep=',', on_bad_lines='skip', encoding='utf-8')

        console.print(f"[green]✓ Passo 1: Arquivo lido com sucesso[/green]")
        console.print(f"[cyan]  Arquivo: {csv_path}[/cyan]")
        console.print(f"[cyan]  Total de protocolos lidos: {len(df)}[/cyan]")

        # Avisa se alguma linha foi pulada
        if len(df) < total_lines:
            skipped = total_lines - len(df)
            console.print(f"[yellow]  ⚠ Linhas malformadas puladas: {skipped}[/yellow]")
            logger.warning(f"{skipped} linhas malformadas foram puladas do CSV")

        console.print()
    except Exception as e:
        console.print(f"[red]✗ Erro ao ler arquivo: {e}[/red]")
        logger.error(f"Erro ao ler CSV: {e}")
        return

    if 'processo_formatado' not in df.columns:
        console.print("[red]✗ Coluna 'processo_formatado' não encontrada no CSV[/red]")
        return

    # Passo 2: Verifica quais protocolos já possuem andamentos
    console.print("[yellow]Passo 2: Verificando quais protocolos já possuem andamentos no banco...[/yellow]")

    protocols_with_andamentos = 0
    protocols_without_andamentos = 0
    protocols_to_process = []

    for _, row in df.iterrows():
        protocol = row['processo_formatado']
        has_andamentos = check_protocol_has_andamentos(protocol)

        if has_andamentos:
            protocols_with_andamentos += 1
            logger.debug(f"[{protocol}] Já possui andamentos - pulando")
        else:
            protocols_without_andamentos += 1
            protocols_to_process.append(row.to_dict())

    console.print(f"[green]✓ Passo 2 concluído:[/green]")
    console.print(f"[cyan]  Total no CSV: {len(df)}[/cyan]")
    console.print(f"[green]  Já possuem andamentos (pulados): {protocols_with_andamentos}[/green]")
    console.print(f"[yellow]  SEM andamentos (a buscar): {protocols_without_andamentos}[/yellow]\n")

    if protocols_without_andamentos == 0:
        console.print("[green]✓ Todos os protocolos já possuem andamentos no banco![/green]")
        console.print("[cyan]Nada a fazer.[/cyan]\n")
        return

    # Passo 3: Verifica/salva em temp_etl
    console.print(f"[yellow]Passo 3: Verificando e salvando {protocols_without_andamentos} protocolos em sei_processos_temp_etl...[/yellow]")

    protocols_to_fetch = []
    already_in_temp_etl = 0
    inserted_in_temp_etl = 0

    for row_data in protocols_to_process:
        protocol = row_data['processo_formatado']
        unidade = safe_str(row_data.get('geracao_sigla'), 'SEAD-PI/GAB')

        # Verifica se existe e salva se necessário
        already_existed = check_and_save_to_temp_etl(protocol, row_data)

        if already_existed:
            already_in_temp_etl += 1
        else:
            inserted_in_temp_etl += 1

        protocols_to_fetch.append((protocol, unidade, row_data))

    console.print(f"[green]✓ Passo 3 concluído:[/green]")
    console.print(f"[cyan]  Já existiam em temp_etl: {already_in_temp_etl}[/cyan]")
    console.print(f"[cyan]  Inseridos em temp_etl: {inserted_in_temp_etl}[/cyan]")
    console.print(f"[cyan]  Total a buscar na API: {len(protocols_to_fetch)}[/cyan]\n")

    # Estatísticas
    results = {
        'success': [],
        'not_found': [],
        'access_denied': [],
        'error': []
    }

    console.print("[yellow]Passo 4: Buscando metadados via API SEI...[/yellow]\n")

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
                f"[cyan]Passo 4: Buscando via API (lote: {batch_size})...",
                total=len(protocols_to_fetch)
            )

            # Processa em lotes
            for i in range(0, len(protocols_to_fetch), batch_size):
                batch = protocols_to_fetch[i:i + batch_size]

                tasks = [
                    fetch_processo_completo(client, protocol, unidade)
                    for protocol, unidade, _ in batch
                ]

                batch_results = await asyncio.gather(*tasks, return_exceptions=True)

                # Processa resultados
                for (protocol, unidade, row_data), result in zip(batch, batch_results):
                    if isinstance(result, Exception):
                        logger.error(f"Exceção ao processar {protocol}: {result}")
                        results['error'].append({
                            'protocol': protocol,
                            'error': str(result),
                            'especificacao': safe_str(row_data.get('especificacao'))
                        })
                    elif result:
                        if result.get('_permanent_error'):
                            results['not_found'].append({
                                'protocol': protocol,
                                'error': result['_error_msg'],
                                'especificacao': safe_str(row_data.get('especificacao'))
                            })
                        elif result.get('_access_denied'):
                            results['access_denied'].append({
                                'protocol': protocol,
                                'error': result['_error_msg'],
                                'unidades_tentadas': result.get('_unidades_tentadas', []),
                                'especificacao': safe_str(row_data.get('especificacao'))
                            })
                        else:
                            # Sucesso - salva no banco
                            try:
                                save_processo_to_db(result, protocol)
                                results['success'].append({
                                    'protocol': protocol,
                                    'unidade': result.get('unidade_usada', unidade),
                                    'docs': len(result.get('documentos', [])),
                                    'andamentos': len(result.get('andamentos', [])),
                                    'especificacao': safe_str(row_data.get('especificacao'))
                                })
                            except Exception as e:
                                logger.error(f"Erro ao salvar {protocol}: {e}")
                                results['error'].append({
                                    'protocol': protocol,
                                    'error': f"Erro ao salvar: {str(e)}",
                                    'especificacao': safe_str(row_data.get('especificacao'))
                                })
                    else:
                        results['error'].append({
                            'protocol': protocol,
                            'error': 'Resultado None',
                            'especificacao': safe_str(row_data.get('especificacao'))
                        })

                    progress.update(task, advance=1)

    # Exibe relatório
    console.print("\n[green]✓ Passo 5: Dados salvos no banco (sei_processos, sei_documentos, sei_andamentos, sei_etl_status)[/green]\n")

    console.print("[bold cyan]═══════════════════════════════════════════════════════════[/bold cyan]")
    console.print("[bold cyan]  PASSO 6: RELATÓRIO FINAL  [/bold cyan]")
    console.print("[bold cyan]═══════════════════════════════════════════════════════════[/bold cyan]\n")

    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("Status", style="cyan", width=20)
    table.add_column("Quantidade", justify="right", style="green")
    table.add_column("Percentual", justify="right", style="yellow")

    total = len(protocols_to_fetch)
    table.add_row("✓ Sucesso", str(len(results['success'])), f"{len(results['success'])/total*100:.1f}%")
    table.add_row("✗ Não encontrado", str(len(results['not_found'])), f"{len(results['not_found'])/total*100:.1f}%")
    table.add_row("⚠ Acesso negado", str(len(results['access_denied'])), f"{len(results['access_denied'])/total*100:.1f}%")
    table.add_row("❌ Erro", str(len(results['error'])), f"{len(results['error'])/total*100:.1f}%")
    table.add_row("[bold]TOTAL[/bold]", f"[bold]{total}[/bold]", "[bold]100%[/bold]")

    console.print(table)

    # Detalhes de sucesso
    if results['success']:
        console.print(f"\n[bold green]Protocolos salvos com sucesso ({len(results['success'])}):[/bold green]")
        for item in results['success'][:10]:  # Mostra primeiros 10
            especificacao = item.get('especificacao', '')
            if especificacao:
                console.print(f"  ✓ {item['protocol']} - {especificacao[:60]}...")
            else:
                console.print(f"  ✓ {item['protocol']} - (sem especificação)")
        if len(results['success']) > 10:
            console.print(f"  ... e mais {len(results['success']) - 10} protocolos")

    # Detalhes de falha
    if results['not_found']:
        console.print(f"\n[bold red]Protocolos não encontrados ({len(results['not_found'])}):[/bold red]")
        for item in results['not_found'][:5]:
            error = safe_str(item.get('error', 'Erro desconhecido'))
            console.print(f"  ✗ {item['protocol']} - {error}")

    if results['access_denied']:
        console.print(f"\n[bold yellow]Protocolos com acesso negado ({len(results['access_denied'])}):[/bold yellow]")
        for item in results['access_denied'][:5]:
            error = safe_str(item.get('error', 'Acesso negado'))
            console.print(f"  ⚠ {item['protocol']} - {error}")

    if results['error']:
        console.print(f"\n[bold red]Protocolos com erro ({len(results['error'])}):[/bold red]")
        for item in results['error'][:5]:
            error = safe_str(item.get('error', 'Erro desconhecido'))
            console.print(f"  ❌ {item['protocol']} - {error}")

    # Salva relatório detalhado
    report_path = "data/fetch_protocols_without_andamentos_report.csv"
    report_df = pd.DataFrame([
        {
            'protocol': item['protocol'],
            'status': 'success',
            'especificacao': item['especificacao'],
            'unidade': item.get('unidade', ''),
            'documentos': item.get('docs', 0),
            'andamentos': item.get('andamentos', 0),
            'error': ''
        }
        for item in results['success']
    ] + [
        {
            'protocol': item['protocol'],
            'status': 'not_found',
            'especificacao': item['especificacao'],
            'unidade': '',
            'documentos': 0,
            'andamentos': 0,
            'error': item['error']
        }
        for item in results['not_found']
    ] + [
        {
            'protocol': item['protocol'],
            'status': 'access_denied',
            'especificacao': item['especificacao'],
            'unidade': '',
            'documentos': 0,
            'andamentos': 0,
            'error': item['error']
        }
        for item in results['access_denied']
    ] + [
        {
            'protocol': item['protocol'],
            'status': 'error',
            'especificacao': item['especificacao'],
            'unidade': '',
            'documentos': 0,
            'andamentos': 0,
            'error': item['error']
        }
        for item in results['error']
    ])

    report_df.to_csv(report_path, index=False)
    console.print(f"\n[cyan]Relatório detalhado salvo em: {report_path}[/cyan]\n")


def main():
    """Função principal."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Busca protocolos que ainda não possuem andamentos no banco via API SEI",
        epilog="Exemplos:\n"
               "  python src/scripts/fetch_missing_protocols.py\n"
               "  python src/scripts/fetch_missing_protocols.py --csv data/processos-cgfr-geral.csv --batch-size 5\n"
               "  python src/scripts/fetch_missing_protocols.py --csv data/custom.csv --batch-size 20",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--csv",
        type=str,
        default="data/processos-cgfr-geral.csv",
        help="Caminho para o arquivo CSV com protocolos (padrão: data/processos-cgfr-geral.csv)"
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=10,
        help="Tamanho do lote paralelo (padrão: 10, recomendado para evitar sobrecarga)"
    )

    args = parser.parse_args()

    try:
        asyncio.run(fetch_missing_protocols(args.csv, args.batch_size))
    except KeyboardInterrupt:
        console.print("\n[yellow]Processo interrompido pelo usuário.[/yellow]")
        logger.warning("Processo interrompido pelo usuário")
        sys.exit(1)
    except Exception as e:
        console.print(f"\n[bold red]Erro durante a execução: {e}[/bold red]")
        logger.exception("Erro durante a busca")
        sys.exit(1)


if __name__ == "__main__":
    main()
