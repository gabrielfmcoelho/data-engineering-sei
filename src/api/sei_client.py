"""Cliente HTTP para a API do SEI do Estado do Piauí.

Implementa:
- Autenticação JWT
- Rate limiting
- Retry com backoff exponencial
- Timeout configurável
- Logging estruturado
"""
import asyncio
from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta, timezone
import aiohttp
from loguru import logger
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log
)


class SeiPermanentError(Exception):
    """Erro permanente da API SEI que não deve ser retentado.

    Exemplos:
    - Processo não encontrado
    - Documento não encontrado
    - Acesso negado (quando nenhuma unidade tem acesso)
    """
    pass


class SeiUnidadeAccessError(Exception):
    """Erro de acesso de uma unidade específica ao processo.

    Este erro indica que a unidade não tem acesso, mas outras unidades podem ter.
    Deve-se tentar outras unidades antes de desistir.
    """
    pass


class SeiAPIClient:
    """Cliente assíncrono para a API do SEI."""

    def __init__(
        self,
        base_url: str = "https://api.sei.pi.gov.br",
        usuario: str = None,
        senha: str = None,
        orgao: str = "GOV-PI",
        max_concurrent: int = 10,
        timeout: int = 30,
    ):
        """
        Inicializa o cliente da API SEI.

        Args:
            base_url: URL base da API
            usuario: Usuário SEI
            senha: Senha do usuário
            orgao: Órgão do usuário
            max_concurrent: Máximo de requisições concorrentes
            timeout: Timeout em segundos
        """
        self.base_url = base_url.rstrip('/')
        self.usuario = usuario
        self.senha = senha
        self.orgao = orgao
        self.timeout = aiohttp.ClientTimeout(total=timeout)

        # Controle de concorrência
        self.semaphore = asyncio.Semaphore(max_concurrent)

        # Token de autenticação
        self._token: Optional[str] = None
        self._token_expires_at: Optional[datetime] = None
        self._token_lock = asyncio.Lock()  # Lock para evitar múltiplos logins simultâneos

        # Unidades disponíveis (sigla -> id)
        self._unidades: Dict[str, str] = {}

        # Session
        self._session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self):
        """Context manager entry."""
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        await self.close()

    async def start(self):
        """Inicia a sessão HTTP."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self.timeout,
                headers={'Content-Type': 'application/json'}
            )
            logger.debug("Sessão HTTP iniciada")

    async def close(self):
        """Fecha a sessão HTTP."""
        if self._session and not self._session.closed:
            await self._session.close()
            logger.debug("Sessão HTTP fechada")

    async def get_unidade_id(self, sigla_processo: str) -> Optional[str]:
        """Obtém o ID da unidade a partir da sigla do processo.

        Tenta match exato primeiro, depois match por prefixo (da mais específica para menos).
        Exemplo: "SEAD-PI/GAB/SUPARC" pode fazer match com "SEAD-PI/GAB" ou "SEAD-PI"

        Args:
            sigla_processo: Sigla da unidade do processo (ex: "SEAD-PI/GAB/SUPARC")

        Returns:
            ID da unidade ou None se não encontrar
        """
        if not sigla_processo:
            return None

        # Garante que temos o token (e as unidades carregadas)
        # Só chama _get_token se ainda não temos unidades
        if not self._unidades:
            await self._get_token()

        # Tenta match exato primeiro
        if sigla_processo in self._unidades:
            return self._unidades[sigla_processo]

        # Tenta match por prefixo (da mais específica para menos)
        # Ex: "SEAD-PI/GAB/SUPARC" -> tenta "SEAD-PI/GAB", depois "SEAD-PI"
        parts = sigla_processo.split('/')
        for i in range(len(parts) - 1, 0, -1):
            prefix = '/'.join(parts[:i])
            if prefix in self._unidades:
                logger.debug(f"Match de unidade: {sigla_processo} -> {prefix}")
                return self._unidades[prefix]

        # Debug: mostra unidades disponíveis que começam com o mesmo prefixo
        orgao = sigla_processo.split('/')[0] if '/' in sigla_processo else sigla_processo
        matching = [s for s in self._unidades.keys() if s.startswith(orgao)]

        logger.warning(
            f"Unidade não encontrada para: {sigla_processo}. "
            f"Unidades disponíveis do órgão {orgao}: {len(matching)} "
            f"(primeiras 5: {matching[:5]})"
        )
        return None

    async def get_all_unidades_do_orgao(self, orgao_prefix: str) -> List[tuple]:
        """Obtém todas as unidades disponíveis de um órgão específico.

        Args:
            orgao_prefix: Prefixo do órgão (ex: "SEAD-PI", "SEDUC-PI")

        Returns:
            Lista de tuplas (sigla, id_unidade) ordenadas por especificidade
        """
        # Garante que temos as unidades carregadas
        if not self._unidades:
            await self._get_token()

        # Filtra unidades do órgão e ordena por especificidade (mais níveis primeiro)
        unidades_orgao = [
            (sigla, id_unidade)
            for sigla, id_unidade in self._unidades.items()
            if sigla.startswith(orgao_prefix)
        ]

        # Ordena por número de níveis (mais específicas primeiro)
        # Ex: "SEAD-PI/GAB/SUPARC" vem antes de "SEAD-PI/GAB"
        unidades_orgao.sort(key=lambda x: x[0].count('/'), reverse=True)

        return unidades_orgao

    async def _get_token(self) -> str:
        """Obtém ou renova o token de autenticação.

        Returns:
            Token JWT
        """
        # Fast path - verifica se token ainda é válido (sem lock)
        if self._token and self._token_expires_at:
            if datetime.now(timezone.utc) < self._token_expires_at - timedelta(minutes=5):
                return self._token

        # Slow path - precisa fazer login (adquire lock)
        async with self._token_lock:
            # Double-check após adquirir lock (outro coroutine pode ter feito login)
            if self._token and self._token_expires_at:
                if datetime.now(timezone.utc) < self._token_expires_at - timedelta(minutes=5):
                    return self._token

            # Faz login para obter novo token
            logger.info("Autenticando na API SEI...")

            url = f"{self.base_url}/v1/orgaos/usuarios/login"
            payload = {
                "Usuario": self.usuario,
                "Senha": self.senha,
                "Orgao": self.orgao
            }

            try:
                async with self._session.post(url, json=payload) as response:
                    response.raise_for_status()
                    data = await response.json()

                    # Token vem com T maiúsculo: "Token"
                    self._token = data.get('Token')

                    if not self._token:
                        logger.error(f"Token não encontrado na resposta! Keys: {list(data.keys())}")
                        raise ValueError("Token não encontrado na resposta de autenticação")

                    # API não retorna expiração, assume 1 hora
                    self._token_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)

                    # Armazena mapeamento de unidades (sigla -> id)
                    unidades_list = data.get('Unidades', [])
                    self._unidades = {
                        unidade.get('Sigla'): unidade.get('Id')
                        for unidade in unidades_list
                        if unidade.get('Sigla') and unidade.get('Id')
                    }

                    logger.success(
                        f"Autenticado com sucesso (token expira em ~1h). "
                        f"Acesso a {len(self._unidades)} unidades."
                    )
                    return self._token

            except aiohttp.ClientError as e:
                logger.error(f"Erro ao autenticar: {e}")
                raise

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((aiohttp.ClientError, asyncio.TimeoutError)),
        before_sleep=before_sleep_log(logger, "WARNING")
    )
    async def _request(
        self,
        method: str,
        endpoint: str,
        **kwargs
    ) -> Dict[str, Any]:
        """Faz requisição HTTP com retry e rate limiting.

        Args:
            method: Método HTTP (GET, POST, etc)
            endpoint: Endpoint da API (ex: /v1/unidades/123/procedimentos)
            **kwargs: Argumentos adicionais para aiohttp

        Returns:
            Resposta JSON da API
        """
        async with self.semaphore:
            # Garante que temos token
            token = await self._get_token()

            # Adiciona token ao header
            headers = kwargs.get('headers', {})
            headers['token'] = token
            kwargs['headers'] = headers

            # URL completa
            url = f"{self.base_url}{endpoint}"

            logger.debug(f"{method} {url}")

            try:
                async with self._session.request(method, url, **kwargs) as response:
                    # Log de resposta
                    logger.debug(f"Status: {response.status}")

                    # Trata erros HTTP
                    if response.status == 401:
                        # Token inválido, limpa e tenta novamente
                        logger.warning("Token inválido, reautenticando...")
                        self._token = None
                        raise aiohttp.ClientError("Token inválido")

                    elif response.status == 429:
                        # Rate limit
                        logger.warning("Rate limit atingido, aguardando...")
                        await asyncio.sleep(5)
                        raise aiohttp.ClientError("Rate limit")

                    # Para erros 4xx e 5xx, verifica tipo de erro
                    if response.status >= 400:
                        try:
                            error_data = await response.json()

                            # Detecta erro de acesso da unidade (deve tentar outras)
                            if self._is_unidade_access_error(error_data):
                                error_msg = self._extract_error_message(error_data)
                                logger.debug(f"Erro de acesso da unidade: {error_msg}")
                                raise SeiUnidadeAccessError(error_msg)

                            # Detecta erros permanentes (não retentáveis)
                            if self._is_permanent_error(error_data):
                                error_msg = self._extract_error_message(error_data)
                                logger.warning(f"Erro permanente detectado: {error_msg}")
                                raise SeiPermanentError(error_msg)
                        except (aiohttp.ContentTypeError, ValueError):
                            # Se não conseguir parsear JSON, segue com raise_for_status normal
                            pass

                    response.raise_for_status()

                    # Retorna JSON
                    return await response.json()

            except SeiPermanentError:
                # Repropaga erros permanentes sem modificar
                raise
            except aiohttp.ClientError as e:
                logger.error(f"Erro na requisição {method} {url}: {e}")
                raise

    def _is_unidade_access_error(self, error_data: Dict[str, Any]) -> bool:
        """Verifica se o erro é de acesso de uma unidade específica ao processo.

        Args:
            error_data: Dados do erro retornado pela API

        Returns:
            True se for erro de acesso da unidade
        """
        if not isinstance(error_data, dict):
            return False

        detail = error_data.get('detail', [])
        if not isinstance(detail, list):
            return False

        for error_item in detail:
            if not isinstance(error_item, dict):
                continue

            msg = error_item.get('msg', '')
            if not isinstance(msg, str):
                continue

            # Detecta mensagem específica de falta de acesso da unidade
            msg_lower = msg.lower()
            if 'não possui acesso ao processo' in msg_lower or 'does not have access to process' in msg_lower:
                return True

        return False

    def _is_permanent_error(self, error_data: Dict[str, Any]) -> bool:
        """Verifica se o erro é permanente (não deve ser retentado em nenhuma unidade).

        Args:
            error_data: Dados do erro retornado pela API

        Returns:
            True se for erro permanente
        """
        if not isinstance(error_data, dict):
            return False

        # Estrutura: {"detail": [{"msg": "Processo [...] não encontrado.", ...}]}
        detail = error_data.get('detail', [])
        if not isinstance(detail, list):
            return False

        for error_item in detail:
            if not isinstance(error_item, dict):
                continue

            msg = error_item.get('msg', '')
            if not isinstance(msg, str):
                continue

            # Detecta mensagens de erro permanente (mas NÃO erro de acesso da unidade)
            msg_lower = msg.lower()

            # Se for erro de acesso da unidade, não é permanente
            if 'não possui acesso ao processo' in msg_lower or 'does not have access to process' in msg_lower:
                return False

            # Detecta outros erros permanentes
            permanent_patterns = [
                'não encontrado',
                'not found',
                'não existe',
                'does not exist'
            ]

            if any(pattern in msg_lower for pattern in permanent_patterns):
                return True

        return False

    def _extract_error_message(self, error_data: Dict[str, Any]) -> str:
        """Extrai mensagem de erro da resposta da API.

        Args:
            error_data: Dados do erro retornado pela API

        Returns:
            Mensagem de erro formatada
        """
        if not isinstance(error_data, dict):
            return str(error_data)

        detail = error_data.get('detail', [])
        if isinstance(detail, list) and detail:
            messages = []
            for item in detail:
                if isinstance(item, dict):
                    msg = item.get('msg', '')
                    if msg:
                        messages.append(msg)
            if messages:
                return '; '.join(messages)

        return str(error_data)

    # ========================================================================
    # ENDPOINTS DA API SEI
    # ========================================================================

    async def consultar_processo(
        self,
        id_unidade: str,
        protocolo: str,
        sin_retornar_atributos: str = "N"
    ) -> Dict[str, Any]:
        """Consulta informações de um processo.

        Args:
            id_unidade: ID da unidade
            protocolo: Protocolo do processo
            sin_retornar_atributos: Retornar atributos adicionais (S/N)

        Returns:
            Dados do processo
        """
        endpoint = f"/v1/unidades/{id_unidade}/procedimentos/consulta"
        params = {
            "protocolo_procedimento": protocolo,
            "sin_retornar_atributos": sin_retornar_atributos,
            "sinal_completo": "S"
        }

        return await self._request("GET", endpoint, params=params)

    async def listar_documentos(
        self,
        id_unidade: str,
        protocolo_procedimento: str
    ) -> List[Dict[str, Any]]:
        """Lista documentos de um processo com paginação paralela.

        Primeira requisição obtém Info.TotalPaginas, depois busca
        páginas restantes em paralelo (15 documentos por página).

        Args:
            id_unidade: ID da unidade
            protocolo_procedimento: Protocolo do procedimento (ex: "00002.012471/2025-15")

        Returns:
            Lista completa de documentos
        """
        endpoint = f"/v1/unidades/{id_unidade}/procedimentos/documentos"
        itens_por_pagina = 15

        # Primeira requisição para obter Info
        params = {
            "protocolo_procedimento": protocolo_procedimento,
            "sinal_completo": "S",
            "pagina": 1,
            "quantidade": itens_por_pagina
        }

        logger.debug(f"Buscando documentos - unidade: {id_unidade}, protocolo: {protocolo_procedimento}")

        try:
            first_response = await self._request("GET", endpoint, params=params)

            # Debug: mostra estrutura da resposta
            if isinstance(first_response, dict):
                logger.debug(f"Documentos response keys: {list(first_response.keys())}")
                logger.debug(f"Documentos response: {str(first_response)[:200]}...")
            else:
                logger.debug(f"Documentos response type: {type(first_response)}, length: {len(first_response) if isinstance(first_response, list) else 'N/A'}")

            # Extrai documentos da primeira página (API retorna com 'D' maiúsculo)
            first_docs = first_response if isinstance(first_response, list) else first_response.get('Documentos', [])
            all_docs = list(first_docs)

            # Extrai informações de paginação
            info = first_response.get('Info', {}) if isinstance(first_response, dict) else {}
            total_paginas = info.get('TotalPaginas', 1)
            total_itens = info.get('TotalItens', len(first_docs))

            logger.debug(f"Documentos: {total_itens} itens em {total_paginas} página(s)")

            # Se houver mais páginas, busca em paralelo
            if total_paginas > 1:
                tasks = []
                for pagina in range(2, total_paginas + 1):
                    params = {
                        "protocolo_procedimento": protocolo_procedimento,
                        "sinal_completo": "S",
                        "pagina": pagina,
                        "quantidade": itens_por_pagina
                    }
                    tasks.append(self._request("GET", endpoint, params=params))

                # Busca todas as páginas restantes em paralelo
                responses = await asyncio.gather(*tasks, return_exceptions=True)

                # Consolida resultados
                for pagina, resp in enumerate(responses, start=2):
                    if isinstance(resp, Exception):
                        logger.error(f"Erro ao buscar página {pagina} de documentos: {resp}")
                        continue

                    docs = resp if isinstance(resp, list) else resp.get('Documentos', [])
                    all_docs.extend(docs)

            logger.debug(f"Total de documentos coletados: {len(all_docs)}")
            return all_docs

        except Exception as e:
            logger.error(f"Erro ao listar documentos: {e}")
            return []

    async def listar_andamentos(
        self,
        id_unidade: str,
        protocolo_procedimento: str
    ) -> List[Dict[str, Any]]:
        """Lista andamentos de um processo com paginação paralela.

        Primeira requisição obtém Info.TotalPaginas, depois busca
        páginas restantes em paralelo (100 andamentos por página).

        Args:
            id_unidade: ID da unidade
            protocolo_procedimento: Protocolo do procedimento (ex: "00002.012471/2025-15")

        Returns:
            Lista completa de andamentos
        """
        endpoint = f"/v1/unidades/{id_unidade}/procedimentos/andamentos"
        itens_por_pagina = 100

        # Primeira requisição para obter Info
        params = {
            "protocolo_procedimento": protocolo_procedimento,
            "sinal_atributos": "S",
            "pagina": 1,
            "quantidade": itens_por_pagina
        }

        logger.debug(f"Buscando andamentos - unidade: {id_unidade}, protocolo: {protocolo_procedimento}")

        try:
            first_response = await self._request("GET", endpoint, params=params)

            # Debug: mostra estrutura da resposta
            if isinstance(first_response, dict):
                logger.debug(f"Andamentos response keys: {list(first_response.keys())}")
                logger.debug(f"Andamentos response: {str(first_response)[:200]}...")
            else:
                logger.debug(f"Andamentos response type: {type(first_response)}, length: {len(first_response) if isinstance(first_response, list) else 'N/A'}")

            # Extrai andamentos da primeira página (API retorna com 'A' maiúsculo)
            first_andamentos = first_response if isinstance(first_response, list) else first_response.get('Andamentos', [])
            all_andamentos = list(first_andamentos)

            # Extrai informações de paginação
            info = first_response.get('Info', {}) if isinstance(first_response, dict) else {}
            total_paginas = info.get('TotalPaginas', 1)
            total_itens = info.get('TotalItens', len(first_andamentos))

            logger.debug(f"Andamentos: {total_itens} itens em {total_paginas} página(s)")

            # Se houver mais páginas, busca em paralelo
            if total_paginas > 1:
                tasks = []
                for pagina in range(2, total_paginas + 1):
                    params = {
                        "protocolo_procedimento": protocolo_procedimento,
                        "sinal_atributos": "S",
                        "pagina": pagina,
                        "quantidade": itens_por_pagina
                    }
                    tasks.append(self._request("GET", endpoint, params=params))

                # Busca todas as páginas restantes em paralelo
                responses = await asyncio.gather(*tasks, return_exceptions=True)

                # Consolida resultados
                for pagina, resp in enumerate(responses, start=2):
                    if isinstance(resp, Exception):
                        logger.error(f"Erro ao buscar página {pagina} de andamentos: {resp}")
                        continue

                    andamentos = resp if isinstance(resp, list) else resp.get('Andamentos', [])
                    all_andamentos.extend(andamentos)

            logger.debug(f"Total de andamentos coletados: {len(all_andamentos)}")
            return all_andamentos

        except Exception as e:
            logger.error(f"Erro ao listar andamentos: {e}")
            return []

    async def consultar_documento(
        self,
        id_unidade: str,
        protocolo_documento: str,
        sin_retornar_geracao: str = "N"
    ) -> Dict[str, Any]:
        """Consulta informações de um documento.

        Args:
            id_unidade: ID da unidade
            protocolo_documento: Protocolo do documento
            sin_retornar_geracao: Retornar info de geração (S/N)

        Returns:
            Dados do documento
        """
        endpoint = f"/v1/unidades/{id_unidade}/documentos"
        params = {
            "protocolo_documento": protocolo_documento,
            "sin_retornar_geracao": sin_retornar_geracao,
            "sinal_completo": "S"
        }

        return await self._request("GET", endpoint, params=params)

    async def baixar_documento(
        self,
        id_unidade: str,
        protocolo_documento: str
    ) -> bytes:
        """Baixa conteúdo binário de um documento.

        Args:
            id_unidade: ID da unidade
            protocolo_documento: Protocolo do documento

        Returns:
            Conteúdo binário do documento
        """
        async with self.semaphore:
            token = await self._get_token()

            endpoint = f"/v1/unidades/{id_unidade}/documentos/baixar"
            url = f"{self.base_url}{endpoint}"
            params = {"protocolo_documento": protocolo_documento}
            headers = {'token': token}

            logger.debug(f"Baixando documento {protocolo_documento}")

            try:
                async with self._session.get(url, params=params, headers=headers) as response:
                    response.raise_for_status()
                    content = await response.read()
                    logger.success(f"Documento baixado: {len(content)} bytes")
                    return content

            except aiohttp.ClientError as e:
                logger.error(f"Erro ao baixar documento {protocolo_documento}: {e}")
                raise

    async def listar_unidades(self, id_tipo_procedimento: Optional[str] = None) -> List[Dict[str, Any]]:
        """Lista unidades disponíveis.

        Args:
            id_tipo_procedimento: Filtrar por tipo de procedimento (opcional)

        Returns:
            Lista de unidades
        """
        endpoint = "/v1/unidades"
        params = {}
        if id_tipo_procedimento:
            params["id_tipo_procedimento"] = id_tipo_procedimento

        response = await self._request("GET", endpoint, params=params)
        return response if isinstance(response, list) else response.get('unidades', [])
