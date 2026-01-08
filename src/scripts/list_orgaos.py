"""
Script para listar órgãos disponíveis no banco e estatísticas.

Útil para saber quais órgãos usar com o filtro --orgao.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from sqlalchemy import func, case, text
from rich.console import Console
from rich.table import Table

from src.database.session import get_local_session
from src.database.models.orm_models import SeiProcessoTempETL, SeiETLStatus


console = Console()


def extract_orgao(unidade: str) -> str:
    """Extrai nome do órgão da unidade (primeira parte antes da /)."""
    if not unidade:
        return "Sem Unidade"
    return unidade.split('/')[0]


def list_orgaos():
    """Lista todos os órgãos com estatísticas."""
    console.print("\n[bold cyan]═══════════════════════════════════════════════════════[/bold cyan]")
    console.print("[bold cyan]     Órgãos Disponíveis no Banco - Estatísticas     [/bold cyan]")
    console.print("[bold cyan]═══════════════════════════════════════════════════════[/bold cyan]\n")

    with get_local_session() as session:
        # Query com estatísticas por órgão
        # Usa SUBSTRING para extrair primeira parte antes da /
        query = session.query(
            func.substring(SeiProcessoTempETL.unidade, '^[^/]+').label('orgao'),
            func.count(SeiProcessoTempETL.id).label('total_processos'),
            func.count(
                case(
                    (SeiETLStatus.metadata_status == 'completed', 1),
                    else_=None
                )
            ).label('consultados'),
            func.count(
                case(
                    (SeiETLStatus.metadata_status == 'error', 1),
                    else_=None
                )
            ).label('com_erro'),
        ).outerjoin(
            SeiETLStatus,
            SeiProcessoTempETL.protocol == SeiETLStatus.protocol
        ).group_by(
            'orgao'
        ).order_by(
            func.count(SeiProcessoTempETL.id).desc()
        )

        results = query.all()

    if not results:
        console.print("[yellow]Nenhum órgão encontrado no banco.[/yellow]\n")
        return

    # Cria tabela bonita
    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("Órgão", style="cyan", width=20)
    table.add_column("Total Processos", justify="right", style="green")
    table.add_column("Consultados", justify="right", style="blue")
    table.add_column("Com Erro", justify="right", style="red")
    table.add_column("Pendentes", justify="right", style="yellow")
    table.add_column("% Completo", justify="right", style="magenta")

    total_geral = 0
    total_consultados = 0
    total_erros = 0

    for row in results:
        orgao = row.orgao or "Sem Unidade"
        total = row.total_processos
        consultados = row.consultados
        erros = row.com_erro
        pendentes = total - consultados - erros
        percentual = (consultados / total * 100) if total > 0 else 0

        total_geral += total
        total_consultados += consultados
        total_erros += erros

        # Cor baseada no percentual
        if percentual >= 100:
            percent_style = "bold green"
        elif percentual >= 50:
            percent_style = "yellow"
        else:
            percent_style = "white"

        table.add_row(
            orgao,
            f"{total:,}",
            f"{consultados:,}",
            f"{erros:,}" if erros > 0 else "-",
            f"{pendentes:,}",
            f"[{percent_style}]{percentual:.1f}%[/{percent_style}]"
        )

    console.print(table)

    # Totais
    percentual_geral = (total_consultados / total_geral * 100) if total_geral > 0 else 0

    console.print(f"\n[bold]Totais:[/bold]")
    console.print(f"  Total de processos: {total_geral:,}")
    console.print(f"  Consultados: {total_consultados:,}")
    console.print(f"  Com erro: {total_erros:,}")
    console.print(f"  Pendentes: {total_geral - total_consultados - total_erros:,}")
    console.print(f"  % Completo: {percentual_geral:.1f}%")
    console.print()


def list_orgao_detail(orgao: str):
    """Mostra detalhes de um órgão específico."""
    console.print(f"\n[bold cyan]Detalhes do Órgão: {orgao}[/bold cyan]\n")

    with get_local_session() as session:
        # Total de processos do órgão
        total = session.query(SeiProcessoTempETL).filter(
            SeiProcessoTempETL.unidade.like(f"{orgao}%")
        ).count()

        # Estatísticas de status
        stats = session.query(
            SeiETLStatus.metadata_status,
            func.count(SeiETLStatus.id).label('count')
        ).join(
            SeiProcessoTempETL,
            SeiETLStatus.protocol == SeiProcessoTempETL.protocol
        ).filter(
            SeiProcessoTempETL.unidade.like(f"{orgao}%")
        ).group_by(
            SeiETLStatus.metadata_status
        ).all()

        # Top 10 unidades do órgão
        top_unidades = session.query(
            SeiProcessoTempETL.unidade,
            func.count(SeiProcessoTempETL.id).label('total')
        ).filter(
            SeiProcessoTempETL.unidade.like(f"{orgao}%")
        ).group_by(
            SeiProcessoTempETL.unidade
        ).order_by(
            func.count(SeiProcessoTempETL.id).desc()
        ).limit(10).all()

    console.print(f"[bold]Total de processos:[/bold] {total:,}\n")

    # Status
    console.print("[bold]Status de Consulta:[/bold]")
    for status, count in stats:
        console.print(f"  {status or 'pending'}: {count:,}")

    # Top unidades
    console.print(f"\n[bold]Top 10 Unidades:[/bold]")
    for unidade, count in top_unidades:
        console.print(f"  {unidade}: {count:,}")

    console.print()


def main():
    """Função principal."""
    import argparse

    parser = argparse.ArgumentParser(description="Lista órgãos disponíveis no banco")
    parser.add_argument(
        "--orgao",
        type=str,
        help="Ver detalhes de um órgão específico"
    )

    args = parser.parse_args()

    try:
        if args.orgao:
            list_orgao_detail(args.orgao)
        else:
            list_orgaos()
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrompido pelo usuário.[/yellow]")
        sys.exit(1)
    except Exception as e:
        console.print(f"\n[bold red]Erro: {e}[/bold red]")
        sys.exit(1)


if __name__ == "__main__":
    main()
