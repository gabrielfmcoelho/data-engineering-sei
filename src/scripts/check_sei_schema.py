"""Script para verificar o schema da tabela sei_atividades no banco SEI."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from sqlalchemy import inspect, text
from src.database.session import get_sei_engine

def check_schema():
    engine = get_sei_engine()
    inspector = inspect(engine)

    print("=" * 70)
    print("VERIFICANDO SCHEMA DO BANCO SEI")
    print("=" * 70)
    print()

    # Verifica schemas disponíveis
    print("Schemas disponíveis:")
    schemas = inspector.get_schema_names()
    for schema in schemas:
        print(f"  - {schema}")
    print()

    # Verifica se sei_processo existe
    if 'sei_processo' in schemas:
        print("✓ Schema 'sei_processo' encontrado")
        print()

        # Lista tabelas no schema
        print("Tabelas no schema 'sei_processo':")
        tables = inspector.get_table_names(schema='sei_processo')
        for table in sorted(tables):
            if 'atividad' in table.lower():
                print(f"  * {table} (relacionada a atividade)")
            else:
                print(f"    {table}")
        print()

        # Verifica a tabela de atividades
        atividade_tables = [t for t in tables if 'atividad' in t.lower()]

        if atividade_tables:
            for table_name in atividade_tables:
                print(f"\nColunas da tabela '{table_name}':")
                print("-" * 70)
                columns = inspector.get_columns(table_name, schema='sei_processo')
                for col in columns:
                    print(f"  - {col['name']:<30} {str(col['type']):<20} nullable={col['nullable']}")
        else:
            print("⚠️ Nenhuma tabela de atividades encontrada!")
    else:
        print("✗ Schema 'sei_processo' NÃO encontrado!")
        print()

    # Tenta query direta para confirmar
    print("\n" + "=" * 70)
    print("TESTE DE QUERY DIRETA")
    print("=" * 70)

    try:
        with engine.connect() as conn:
            result = conn.execute(text(
                "SELECT column_name, data_type "
                "FROM information_schema.columns "
                "WHERE table_schema = 'sei_processo' "
                "AND table_name LIKE '%atividad%' "
                "ORDER BY table_name, ordinal_position"
            ))

            print("\nColunas encontradas via information_schema:")
            for row in result:
                print(f"  {row[0]:<30} {row[1]}")

    except Exception as e:
        print(f"Erro ao consultar: {e}")

if __name__ == "__main__":
    check_schema()
