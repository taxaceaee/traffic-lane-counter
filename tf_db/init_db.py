"""Create all database tables."""
from tf_db.models import Base
from tf_db.session import get_engine


def create_tables() -> None:
    Base.metadata.create_all(bind=get_engine())
