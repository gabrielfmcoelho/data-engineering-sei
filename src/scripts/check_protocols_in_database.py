import pandas as pd
import psycopg2
from psycopg2 import sql
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Database connection parameters
# Try SEI database first, fallback to local if connection fails
DB_CONFIGS = {
    'SEI_PROD': {
        'host': os.getenv('SEI_DB_HOST'),
        'port': os.getenv('SEI_DB_PORT'),
        'database': os.getenv('SEI_DB_NAME'),
        'user': os.getenv('SEI_DB_USER'),
        'password': os.getenv('SEI_DB_PASSWORD'),
        'schema': os.getenv('SEI_DB_SCHEMA', 'sei_processo')
    },
    'LOCAL': {
        'host': os.getenv('LOCAL_DB_HOST'),
        'port': os.getenv('LOCAL_DB_PORT'),
        'database': os.getenv('LOCAL_DB_NAME'),
        'user': os.getenv('LOCAL_DB_USER'),
        'password': os.getenv('LOCAL_DB_PASSWORD'),
        'schema': 'public'  # Default schema for local DB
    }
}

# Tables to check
TABLES_TO_CHECK = ['sei_andamentos', 'sei_processos', 'sei_processos_temp_etl']

def connect_to_db():
    """Connect to PostgreSQL database - try production first, then local"""
    for db_name, config in DB_CONFIGS.items():
        try:
            print(f"Attempting to connect to {db_name} database...")
            conn_params = {k: v for k, v in config.items() if k != 'schema'}
            conn = psycopg2.connect(**conn_params)
            schema = config['schema']
            print(f"✓ Successfully connected to {db_name} database (schema: {schema})")
            return conn, schema
        except Exception as e:
            print(f"✗ Could not connect to {db_name}: {e}")
            continue

    print("✗ Failed to connect to any database")
    return None, None

def check_protocol_in_table(cursor, schema, table, protocol):
    """Check if a protocol exists in a specific table"""
    # Common column names for protocol
    possible_columns = ['processo_formatado', 'protocolo_formatado', 'numero_processo', 'protocolo']

    # First, get the actual columns in the table
    try:
        cursor.execute(sql.SQL("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s
        """), [schema, table])

        existing_columns = [row[0] for row in cursor.fetchall()]

        # Find which protocol column exists
        protocol_column = None
        for col in possible_columns:
            if col in existing_columns:
                protocol_column = col
                break

        if not protocol_column:
            return False, "No protocol column found"

        # Check if the protocol exists
        query = sql.SQL("SELECT COUNT(*) FROM {}.{} WHERE {} = %s").format(
            sql.Identifier(schema),
            sql.Identifier(table),
            sql.Identifier(protocol_column)
        )

        cursor.execute(query, [protocol])
        count = cursor.fetchone()[0]

        return count > 0, protocol_column

    except Exception as e:
        return False, f"Error: {str(e)}"

def main():
    # Load missing protocols
    print("Loading missing protocols from CSV...")
    missing_df = pd.read_csv('data/missing_protocols_2025.csv')
    protocols = missing_df['processo_formatado'].tolist()

    print(f"Found {len(protocols)} protocols to check\n")

    # Connect to database
    conn, schema = connect_to_db()
    if not conn:
        return

    cursor = conn.cursor()
    print(f"Using schema: {schema}\n")

    # Results dictionary
    results = {
        'protocol': [],
        'sei_andamentos': [],
        'sei_processos': [],
        'sei_processos_temp_etl': [],
        'found_in_any_table': []
    }

    print("=" * 80)
    print("CHECKING PROTOCOLS IN DATABASE TABLES")
    print("=" * 80)
    print()

    # Check each protocol
    for i, protocol in enumerate(protocols, 1):
        print(f"[{i}/{len(protocols)}] Checking protocol: {protocol}")

        results['protocol'].append(protocol)
        found_in_any = False

        for table in TABLES_TO_CHECK:
            found, info = check_protocol_in_table(cursor, schema, table, protocol)
            results[table].append(found)

            if found:
                print(f"  ✓ Found in {table} (column: {info})")
                found_in_any = True
            else:
                print(f"  ✗ Not found in {table}")

        results['found_in_any_table'].append(found_in_any)

        if not found_in_any:
            print(f"  ⚠ Protocol NOT FOUND in any table")

        print()

    # Close database connection
    cursor.close()
    conn.close()

    # Create results dataframe
    results_df = pd.DataFrame(results)

    # Save to CSV
    output_file = 'data/protocols_database_check.csv'
    results_df.to_csv(output_file, index=False)
    print(f"Results saved to: {output_file}")

    # Print summary
    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)
    print(f"Total protocols checked: {len(protocols)}")
    print(f"Found in sei_andamentos: {results_df['sei_andamentos'].sum()}")
    print(f"Found in sei_processos: {results_df['sei_processos'].sum()}")
    print(f"Found in sei_processos_temp_etl: {results_df['sei_processos_temp_etl'].sum()}")
    print(f"Found in at least one table: {results_df['found_in_any_table'].sum()}")
    print(f"NOT found in any table: {(~results_df['found_in_any_table']).sum()}")

    # Show protocols not found anywhere
    not_found = results_df[~results_df['found_in_any_table']]['protocol'].tolist()
    if not_found:
        print("\n" + "=" * 80)
        print("PROTOCOLS NOT FOUND IN ANY TABLE:")
        print("=" * 80)
        for protocol in not_found:
            print(f"  - {protocol}")

if __name__ == "__main__":
    main()
