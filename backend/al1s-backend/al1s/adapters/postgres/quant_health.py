from sqlalchemy import select
from sqlalchemy.engine import Engine


def database_available(engine: Engine) -> bool:
    with engine.connect() as connection:
        return connection.scalar(select(1)) == 1
