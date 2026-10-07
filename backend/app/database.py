"""SQLite database setup and session management."""

from sqlmodel import SQLModel, create_engine, Session
from app.config import settings
from typing import Generator


# Create SQLite engine
engine = create_engine(
    f"sqlite:///{settings.db_path}",
    connect_args={"check_same_thread": False},  # Needed for SQLite
    echo=False,  # Set to True for SQL query logging
)


def init_db() -> None:
    """Initialize database by creating all tables."""
    from app.models.access_policy import DatabaseAccessPolicy  # noqa: F401
    from app.models.database import DatabaseConnection  # noqa: F401
    from app.models.metadata import DatabaseMetadata  # noqa: F401
    from app.models.query import QueryHistory  # noqa: F401

    SQLModel.metadata.create_all(engine)


def get_session() -> Generator[Session, None, None]:
    """Dependency for FastAPI to get database session."""
    with Session(engine) as session:
        yield session
