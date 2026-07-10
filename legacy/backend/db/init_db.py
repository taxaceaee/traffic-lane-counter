"""Create all database tables."""
from backend.db.models import Base
from backend.db.session import get_engine


def create_tables() -> None:
    Base.metadata.create_all(bind=get_engine())
