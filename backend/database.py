import os
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker
from config import settings

os.makedirs(os.path.dirname(settings.db_path), exist_ok=True)

engine = create_engine(
    f"sqlite:///{settings.db_path}",
    connect_args={"check_same_thread": False},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# Columns added after the first release. create_all() creates missing tables
# but never alters existing ones, so add these by hand until there is Alembic.
_ADDED_COLUMNS = {
    "papers": {
        "status": "VARCHAR NOT NULL DEFAULT 'ready'",
        "error": "VARCHAR",
    },
}


def upgrade_schema() -> None:
    with engine.begin() as conn:
        for table, columns in _ADDED_COLUMNS.items():
            existing = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
            if not existing:
                continue  # table not created yet; create_all will build it whole
            for name, ddl in columns.items():
                if name not in existing:
                    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
