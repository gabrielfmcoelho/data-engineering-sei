#!/bin/bash
# Script de setup do projeto SEI Ontology

set -e

echo "========================================="
echo "  SEI Ontology - Setup"
echo "========================================="
echo ""

# Cores
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Verifica Python
echo -e "${YELLOW}Verificando Python...${NC}"
if ! command -v python3 &> /dev/null; then
    echo "Python 3 não encontrado. Instale Python 3.11+"
    exit 1
fi
echo -e "${GREEN}✓ Python encontrado: $(python3 --version)${NC}"
echo ""

# Verifica Docker
echo -e "${YELLOW}Verificando Docker...${NC}"
if ! command -v docker &> /dev/null; then
    echo "Docker não encontrado. Instale o Docker primeiro."
    exit 1
fi
echo -e "${GREEN}✓ Docker encontrado: $(docker --version)${NC}"
echo ""

# Verifica Docker Compose
echo -e "${YELLOW}Verificando Docker Compose...${NC}"
if ! command -v docker-compose &> /dev/null; then
    echo "Docker Compose não encontrado. Instale o Docker Compose primeiro."
    exit 1
fi
echo -e "${GREEN}✓ Docker Compose encontrado: $(docker-compose --version)${NC}"
echo ""

# Cria ambiente virtual
echo -e "${YELLOW}Criando ambiente virtual Python...${NC}"
if [ ! -d "venv" ]; then
    python3 -m venv venv
    echo -e "${GREEN}✓ Ambiente virtual criado${NC}"
else
    echo -e "${GREEN}✓ Ambiente virtual já existe${NC}"
fi
echo ""

# Ativa ambiente virtual
echo -e "${YELLOW}Ativando ambiente virtual...${NC}"
source venv/bin/activate
echo -e "${GREEN}✓ Ambiente virtual ativado${NC}"
echo ""

# Instala dependências
echo -e "${YELLOW}Instalando dependências Python...${NC}"
pip install -q --upgrade pip
pip install -q -r requirements.txt
echo -e "${GREEN}✓ Dependências instaladas${NC}"
echo ""

# Cria arquivo .env se não existir
if [ ! -f ".env" ]; then
    echo -e "${YELLOW}Criando arquivo .env...${NC}"
    cp .env.example .env
    echo -e "${GREEN}✓ Arquivo .env criado${NC}"
    echo -e "${YELLOW}⚠️  ATENÇÃO: Edite o arquivo .env com suas credenciais do SEI!${NC}"
else
    echo -e "${GREEN}✓ Arquivo .env já existe${NC}"
fi
echo ""

# Cria diretórios necessários
echo -e "${YELLOW}Criando diretórios...${NC}"
mkdir -p logs
mkdir -p data/raw
mkdir -p data/processed
echo -e "${GREEN}✓ Diretórios criados${NC}"
echo ""

# Inicia Docker Compose
echo -e "${YELLOW}Iniciando serviços Docker...${NC}"
docker-compose up -d
echo -e "${GREEN}✓ Serviços iniciados${NC}"
echo ""

# Aguarda serviços ficarem prontos
echo -e "${YELLOW}Aguardando serviços ficarem prontos...${NC}"
sleep 10

# Verifica status dos containers
echo -e "${YELLOW}Verificando status dos containers...${NC}"
docker-compose ps
echo ""

echo "========================================="
echo -e "${GREEN}✓ Setup concluído com sucesso!${NC}"
echo "========================================="
echo ""
echo "Próximos passos:"
echo "1. Edite o arquivo .env com suas credenciais do SEI"
echo "2. Execute: source venv/bin/activate"
echo "3. Execute: python -m src.scripts.extract_processos_gerados"
echo ""
echo "Acesse os serviços:"
echo "- Neo4J: http://localhost:7474"
echo "- MinIO: http://localhost:9001"
echo ""
