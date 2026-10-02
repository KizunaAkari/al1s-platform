from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Declarative metadata root used by Alembic and PostgreSQL repositories."""
