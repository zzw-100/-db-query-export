"""Per-database access policy stored beside the connection record."""

from sqlmodel import SQLModel, Field


class DatabaseAccessPolicy(SQLModel, table=True):
    """Table and column denylist plus the EXPLAIN switch for one connection."""

    __tablename__ = "databaseaccesspolicies"

    database_name: str = Field(
        primary_key=True,
        foreign_key="databaseconnections.name",
        max_length=50,
    )
    blocked_tables: str = Field(default="", max_length=2000)
    blocked_columns: str = Field(default="", max_length=4000)
    allow_explain: bool = Field(default=False)
