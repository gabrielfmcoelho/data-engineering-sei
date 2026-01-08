"""
Script para gerar CSV com consolidado de processos CGFR.

Este script:
1. Analisa os andamentos dos processos via raw SQL (apenas sei_andamentos)
2. Identifica fluxo: SEAD-PI → CGFR → SEAD-PI
3. Gera CSV com estrutura SeiConsolidadoCGFR

Lógica (cada etapa é subconjunto da anterior):
- Remetido SEAD-PI→CGFR: PROCESSO-REMETIDO-UNIDADE onde origem=SEAD-PI e destino=CGFR
- Recebido na CGFR: PROCESSO-RECEBIDO-UNIDADE na CGFR (subconjunto dos remetidos)
- Remetido CGFR→SEAD-PI: PROCESSO-REMETIDO-UNIDADE onde origem=CGFR e destino=SEAD-PI (subconjunto dos recebidos)
- Recebido na SEAD-PI: PROCESSO-RECEBIDO-UNIDADE na SEAD-PI (subconjunto dos remetidos de volta)
"""
import sys
import csv
import json
from pathlib import Path
from typing import Dict, Any, Optional
from datetime import datetime
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from sqlalchemy import text
from loguru import logger
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from rich.table import Table

from src.database.session import get_local_session

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
        "logs/generate_consolidado_cgfr_{time}.log",
        rotation="100 MB",
        retention="10 days",
        level="DEBUG"
    )


def extract_unidade_origem_from_atributos(raw_response: Dict[str, Any]) -> Optional[str]:
    """Extrai unidade de origem dos atributos do andamento.

    Args:
        raw_response: JSON do raw_api_response

    Returns:
        Sigla da unidade de origem ou None
    """
    if not raw_response:
        return None

    atributos = raw_response.get('Atributos', [])

    for atributo in atributos:
        if atributo.get('Nome') == 'UNIDADE':
            valor = atributo.get('Valor', '')
            # Formato: "SIGLA¥DESCRICAO"
            if '¥' in valor:
                sigla = valor.split('¥')[0]
                return sigla
            else:
                return valor

    return None


def extract_unidade_destino_from_json(raw_response: Dict[str, Any]) -> Optional[str]:
    """Extrai unidade de destino (Unidade.Sigla) do JSON.

    Args:
        raw_response: JSON do raw_api_response

    Returns:
        Sigla da unidade de destino ou None
    """
    if not raw_response:
        return None

    unidade = raw_response.get('Unidade', {})
    if isinstance(unidade, dict):
        return unidade.get('Sigla')

    return None


def parse_data_hora(raw_response: Dict[str, Any]) -> Optional[datetime]:
    """Extrai e faz parse da data/hora do andamento.

    Args:
        raw_response: JSON do raw_api_response

    Returns:
        Objeto datetime ou None
    """
    if not raw_response:
        return None

    data_hora_str = raw_response.get('DataHora')
    if not data_hora_str:
        return None

    try:
        # Formato: "01/12/2025 09:39:28"
        return datetime.strptime(data_hora_str, "%d/%m/%Y %H:%M:%S")
    except ValueError:
        logger.warning(f"Formato de data inválido: {data_hora_str}")
        return None


def contains_sead_pi(sigla: Optional[str]) -> bool:
    """Verifica se a sigla contém SEAD-PI."""
    if not sigla:
        return False
    return 'SEAD-PI' in sigla.upper()


def contains_cgfr(sigla: Optional[str]) -> bool:
    """Verifica se a sigla contém CGFR."""
    if not sigla:
        return False
    return 'CGFR' in sigla.upper()


def generate_consolidado_csv(output_file: str = "consolidado_cgfr.csv"):
    """Gera CSV com consolidado de processos CGFR.

    Args:
        output_file: Nome do arquivo CSV de saída
    """
    setup_logger()

    console.print("\n[bold cyan]═══════════════════════════════════════════════════════════[/bold cyan]")
    console.print("[bold cyan]  Geração de Consolidado CGFR - Análise de Andamentos  [/bold cyan]")
    console.print("[bold cyan]═══════════════════════════════════════════════════════════[/bold cyan]\n")

    # Estrutura: protocolo -> dados do consolidado
    consolidado: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
        'protocol': None,
        'foi_remetido_sead_cgfr': False,
        'dt_remetido_sead_cgfr': None,
        'foi_recebido_sead_cgfr': False,
        'dt_recebido_sead_cgfr': None,
        'foi_remetido_cgfr_sead': False,
        'dt_remetido_cgfr_sead': None,
        'foi_recebido_cgfr_sead': False,
        'dt_recebido_cgfr_sead': None,
    })

    console.print("[yellow]Carregando andamentos do banco...[/yellow]")

    with get_local_session() as session:
        # Query raw SQL para buscar andamentos relevantes - APENAS sei_andamentos
        query = text("""
            SELECT
                protocol,
                tarefa,
                raw_api_response,
                data_hora
            FROM sei_andamentos
            WHERE tarefa IN (
                'PROCESSO-REMETIDO-UNIDADE',
                'PROCESSO_REMETIDO_UNIDADE',
                'PROCESSO-RECEBIDO-UNIDADE',
                'PROCESSO_RECEBIDO_UNIDADE'
            )
            ORDER BY protocol, data_hora
        """)

        result = session.execute(query)
        andamentos = result.fetchall()
        total_andamentos = len(andamentos)

        console.print(f"[green]Total de andamentos relevantes: {total_andamentos:,}[/green]\n")
        logger.info(f"Processando {total_andamentos} andamentos")

    # Processa andamentos
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console
    ) as progress:

        task = progress.add_task("[cyan]Analisando andamentos...", total=total_andamentos)

        for row in andamentos:
            protocol = row.protocol
            tarefa = row.tarefa
            raw_response = row.raw_api_response
            data_hora = row.data_hora

            if not raw_response:
                progress.update(task, advance=1)
                continue

            # Parse JSON se for string
            if isinstance(raw_response, str):
                try:
                    raw_response = json.loads(raw_response)
                except:
                    progress.update(task, advance=1)
                    continue

            # Extrai unidades
            unidade_origem = extract_unidade_origem_from_atributos(raw_response)
            unidade_destino = extract_unidade_destino_from_json(raw_response)

            # Se data_hora da query é None, tenta extrair do JSON
            if not data_hora:
                data_hora = parse_data_hora(raw_response)

            tarefa_normalizada = tarefa.upper().replace('_', '-')

            # Inicializa dados do processo se ainda não existe
            if protocol not in consolidado:
                consolidado[protocol]['protocol'] = protocol

            # Lógica de classificação dos andamentos

            # 1. REMETIDO SEAD-PI → CGFR
            if tarefa_normalizada == 'PROCESSO-REMETIDO-UNIDADE':
                if contains_sead_pi(unidade_origem) and contains_cgfr(unidade_destino):
                    if not consolidado[protocol]['foi_remetido_sead_cgfr'] or \
                       (data_hora and consolidado[protocol]['dt_remetido_sead_cgfr'] and
                        data_hora < consolidado[protocol]['dt_remetido_sead_cgfr']):
                        consolidado[protocol]['foi_remetido_sead_cgfr'] = True
                        consolidado[protocol]['dt_remetido_sead_cgfr'] = data_hora
                        logger.debug(f"[{protocol}] Remetido SEAD-PI→CGFR: {unidade_origem} → {unidade_destino}")

                # 3. REMETIDO CGFR → SEAD-PI (somente se já foi recebido na CGFR)
                elif contains_cgfr(unidade_origem) and contains_sead_pi(unidade_destino):
                    if consolidado[protocol]['foi_recebido_sead_cgfr']:  # Subconjunto
                        if not consolidado[protocol]['foi_remetido_cgfr_sead'] or \
                           (data_hora and consolidado[protocol]['dt_remetido_cgfr_sead'] and
                            data_hora < consolidado[protocol]['dt_remetido_cgfr_sead']):
                            consolidado[protocol]['foi_remetido_cgfr_sead'] = True
                            consolidado[protocol]['dt_remetido_cgfr_sead'] = data_hora
                            logger.debug(f"[{protocol}] Remetido CGFR→SEAD-PI: {unidade_origem} → {unidade_destino}")

            # 2. RECEBIDO NA CGFR (somente se já foi remetido para CGFR)
            elif tarefa_normalizada == 'PROCESSO-RECEBIDO-UNIDADE':
                if contains_cgfr(unidade_destino):
                    if consolidado[protocol]['foi_remetido_sead_cgfr']:  # Subconjunto
                        if not consolidado[protocol]['foi_recebido_sead_cgfr'] or \
                           (data_hora and consolidado[protocol]['dt_recebido_sead_cgfr'] and
                            data_hora < consolidado[protocol]['dt_recebido_sead_cgfr']):
                            consolidado[protocol]['foi_recebido_sead_cgfr'] = True
                            consolidado[protocol]['dt_recebido_sead_cgfr'] = data_hora
                            logger.debug(f"[{protocol}] Recebido na CGFR: {unidade_destino}")

                # 4. RECEBIDO NA SEAD-PI (somente se já foi remetido de volta pela CGFR)
                elif contains_sead_pi(unidade_destino):
                    if consolidado[protocol]['foi_remetido_cgfr_sead']:  # Subconjunto
                        if not consolidado[protocol]['foi_recebido_cgfr_sead'] or \
                           (data_hora and consolidado[protocol]['dt_recebido_cgfr_sead'] and
                            data_hora < consolidado[protocol]['dt_recebido_cgfr_sead']):
                            consolidado[protocol]['foi_recebido_cgfr_sead'] = True
                            consolidado[protocol]['dt_recebido_cgfr_sead'] = data_hora
                            logger.debug(f"[{protocol}] Recebido na SEAD-PI: {unidade_destino}")

            progress.update(task, advance=1)

    # Filtra somente processos que foram remetidos para CGFR (base do funil)
    processos_cgfr = {
        protocol: dados
        for protocol, dados in consolidado.items()
        if dados['foi_remetido_sead_cgfr']
    }

    console.print(f"\n[bold green]Processos relacionados à CGFR: {len(processos_cgfr):,}[/bold green]")

    # Estatísticas
    stats = {
        'remetidos_sead_cgfr': sum(1 for d in processos_cgfr.values() if d['foi_remetido_sead_cgfr']),
        'recebidos_cgfr': sum(1 for d in processos_cgfr.values() if d['foi_recebido_sead_cgfr']),
        'remetidos_cgfr_sead': sum(1 for d in processos_cgfr.values() if d['foi_remetido_cgfr_sead']),
        'recebidos_sead': sum(1 for d in processos_cgfr.values() if d['foi_recebido_cgfr_sead']),
    }

    # Tabela de estatísticas
    table = Table(show_header=True, header_style="bold magenta", title="Estatísticas do Funil CGFR")
    table.add_column("Etapa", style="cyan", width=30)
    table.add_column("Quantidade", justify="right", style="green")
    table.add_column("% do Anterior", justify="right", style="yellow")

    table.add_row(
        "1. Remetidos SEAD-PI → CGFR",
        f"{stats['remetidos_sead_cgfr']:,}",
        "100.0%"
    )

    pct_recebidos = (stats['recebidos_cgfr'] / stats['remetidos_sead_cgfr'] * 100) if stats['remetidos_sead_cgfr'] > 0 else 0
    table.add_row(
        "2. Recebidos na CGFR",
        f"{stats['recebidos_cgfr']:,}",
        f"{pct_recebidos:.1f}%"
    )

    pct_devolvidos = (stats['remetidos_cgfr_sead'] / stats['recebidos_cgfr'] * 100) if stats['recebidos_cgfr'] > 0 else 0
    table.add_row(
        "3. Devolvidos CGFR → SEAD-PI",
        f"{stats['remetidos_cgfr_sead']:,}",
        f"{pct_devolvidos:.1f}%"
    )

    pct_recebidos_volta = (stats['recebidos_sead'] / stats['remetidos_cgfr_sead'] * 100) if stats['remetidos_cgfr_sead'] > 0 else 0
    table.add_row(
        "4. Recebidos de volta na SEAD-PI",
        f"{stats['recebidos_sead']:,}",
        f"{pct_recebidos_volta:.1f}%"
    )

    console.print("\n")
    console.print(table)
    console.print("\n")

    # Gera CSV
    console.print(f"[yellow]Gerando arquivo CSV: {output_file}...[/yellow]")

    output_path = Path(output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, 'w', newline='', encoding='utf-8') as csvfile:
        fieldnames = [
            'protocol',
            'foi_remetido_sead_cgfr',
            'dt_remetido_sead_cgfr',
            'foi_recebido_sead_cgfr',
            'dt_recebido_sead_cgfr',
            'foi_remetido_cgfr_sead',
            'dt_remetido_cgfr_sead',
            'foi_recebido_cgfr_sead',
            'dt_recebido_cgfr_sead',
        ]

        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()

        for protocol in sorted(processos_cgfr.keys()):
            dados = processos_cgfr[protocol]

            # Formata datas para CSV
            row = {
                'protocol': dados['protocol'],
                'foi_remetido_sead_cgfr': dados['foi_remetido_sead_cgfr'],
                'dt_remetido_sead_cgfr': dados['dt_remetido_sead_cgfr'].isoformat() if dados['dt_remetido_sead_cgfr'] else '',
                'foi_recebido_sead_cgfr': dados['foi_recebido_sead_cgfr'],
                'dt_recebido_sead_cgfr': dados['dt_recebido_sead_cgfr'].isoformat() if dados['dt_recebido_sead_cgfr'] else '',
                'foi_remetido_cgfr_sead': dados['foi_remetido_cgfr_sead'],
                'dt_remetido_cgfr_sead': dados['dt_remetido_cgfr_sead'].isoformat() if dados['dt_remetido_cgfr_sead'] else '',
                'foi_recebido_cgfr_sead': dados['foi_recebido_cgfr_sead'],
                'dt_recebido_cgfr_sead': dados['dt_recebido_cgfr_sead'].isoformat() if dados['dt_recebido_cgfr_sead'] else '',
            }

            writer.writerow(row)

    console.print(f"[bold green]✓ CSV gerado com sucesso: {output_path.absolute()}[/bold green]")
    console.print(f"[bold green]  Total de registros: {len(processos_cgfr):,}[/bold green]\n")

    logger.success(f"CSV gerado: {output_path.absolute()} ({len(processos_cgfr)} registros)")


def main():
    """Função principal."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Gera CSV com consolidado de processos CGFR",
        epilog="Exemplo: python -m src.scripts.generate_consolidado_cgfr --output data/consolidado_cgfr.csv"
    )
    parser.add_argument(
        "--output",
        type=str,
        default="consolidado_cgfr.csv",
        help="Caminho do arquivo CSV de saída (padrão: consolidado_cgfr.csv)"
    )

    args = parser.parse_args()

    try:
        generate_consolidado_csv(args.output)
    except KeyboardInterrupt:
        console.print("\n[yellow]Processo interrompido pelo usuário.[/yellow]")
        logger.warning("Processo interrompido pelo usuário")
        sys.exit(1)
    except Exception as e:
        console.print(f"\n[bold red]Erro durante a execução: {e}[/bold red]")
        logger.exception("Erro durante a geração do CSV")
        sys.exit(1)


if __name__ == "__main__":
    main()
