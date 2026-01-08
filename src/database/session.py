"""Gerenciamento de sessões e engines do SQLAlchemy."""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from contextlib import contextmanager
from typing import Generator

from src.config import settings


# Engines
_sei_engine = None
_local_engine = None


def get_sei_engine():
    """Obtém engine do banco SEI (origem)."""
    global _sei_engine
    if _sei_engine is None:
        _sei_engine = create_engine(
            settings.sei_db_url,
            pool_pre_ping=True,
            pool_size=10,
            max_overflow=20,
            echo=False,
        )
    return _sei_engine


def get_local_engine():
    """Obtém engine do banco local (destino)."""
    global _local_engine
    if _local_engine is None:
        _local_engine = create_engine(
            settings.local_db_url,
            pool_pre_ping=True,
            pool_size=10,
            max_overflow=20,
            echo=False,
        )
    return _local_engine


# Session makers
SeiSessionLocal = sessionmaker(autocommit=False, autoflush=False)
LocalSessionLocal = sessionmaker(autocommit=False, autoflush=False)


@contextmanager
def get_sei_session() -> Generator[Session, None, None]:
    """Context manager para sessão do banco SEI."""
    engine = get_sei_engine()
    SeiSessionLocal.configure(bind=engine)
    session = SeiSessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def get_local_session() -> Generator[Session, None, None]:
    """Context manager para sessão do banco local."""
    engine = get_local_engine()
    LocalSessionLocal.configure(bind=engine)
    session = LocalSessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
