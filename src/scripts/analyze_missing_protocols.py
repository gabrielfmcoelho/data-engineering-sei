import pandas as pd

# Read both CSV files (with error handling for malformed lines)
consolidado_df = pd.read_csv('data/consolidado_cgfr.csv')
processos_df = pd.read_csv('data/processos-cgfr.csv', on_bad_lines='skip')

# Extract protocols from both dataframes
consolidado_protocols = set(consolidado_df['protocol'].dropna())
processos_protocols = set(processos_df['processo_formatado'].dropna())

# Find protocols in processos_cgfr that are NOT in consolidado_cgfr
missing_protocols = processos_protocols - consolidado_protocols

# Filter for 2025 protocols (assuming the format is like 00002.000092/2025-74)
missing_2025 = [p for p in missing_protocols if '/2025-' in str(p)]

# Get full information for missing 2025 protocols
missing_2025_info = processos_df[processos_df['processo_formatado'].isin(missing_2025)]

print(f"Total protocols in consolidado_cgfr: {len(consolidado_protocols)}")
print(f"Total protocols in processos-cgfr: {len(processos_protocols)}")
print(f"Total missing protocols: {len(missing_protocols)}")
print(f"Missing protocols from 2025: {len(missing_2025)}")
print("\n" + "="*80)
print("MISSING PROTOCOLS FROM 2025:")
print("="*80 + "\n")

# Sort the missing protocols
missing_2025_sorted = sorted(missing_2025)

for i, protocol in enumerate(missing_2025_sorted, 1):
    row = missing_2025_info[missing_2025_info['processo_formatado'] == protocol].iloc[0]
    print(f"{i}. {protocol}")
    if pd.notna(row['especificacao']):
        print(f"   Especificação: {row['especificacao']}")
    if pd.notna(row['deliberacao']):
        print(f"   Deliberação: {row['deliberacao']}")
    if pd.notna(row['tipo_processo']):
        print(f"   Tipo: {row['tipo_processo']}")
    print()

# Save to CSV file
missing_2025_info_sorted = missing_2025_info.sort_values('processo_formatado')
missing_2025_info_sorted.to_csv('data/missing_protocols_2025.csv', index=False)
print(f"\nResults saved to: data/missing_protocols_2025.csv")
