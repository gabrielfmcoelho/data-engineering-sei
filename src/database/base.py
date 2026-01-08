"""Base declarativa para modelos SQLAlchemy."""
from sqlalchemy.orm import declarative_base as orm_declarative_base
from sqlalchemy.ext.declarative import declarative_base as ext_declarative_base


ORMBase = orm_declarative_base()
ExtDeclarativeBase = ext_declarative_base()