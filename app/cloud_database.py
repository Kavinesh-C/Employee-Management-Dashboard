
import dotenv
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
dotenv.load_dotenv()

# Cloud MariaDB connection URL is now read from the .env file
CLOUD_DATABASE_URL = os.getenv("DATABASE_URL")


def is_local_database(url):
    return (
        url is not None and (
            "localhost" in url or
            "127.0.0.1" in url
        )
    )

def is_cloud_database(url):
    return (
        url is not None and not is_local_database(url)
    )

IS_LOCAL_DB = is_local_database(CLOUD_DATABASE_URL)
IS_CLOUD_DB = is_cloud_database(CLOUD_DATABASE_URL)


def get_cloud_engine():
    if not CLOUD_DATABASE_URL:
        raise ValueError("DATABASE_URL must be set in your .env file for cloud connection.")
    # Use connection pooling and recommended options for cloud DBs
    engine = create_engine(
        CLOUD_DATABASE_URL,
        pool_size=10,            # Number of connections to keep in the pool
        max_overflow=20,         # Number of connections allowed above pool_size
        pool_timeout=30,         # Seconds to wait before giving up on getting a connection
        pool_recycle=1800,       # Recycle connections after 30 minutes
        pool_pre_ping=True,      # Test connections for liveness
        echo=True                # Enable SQL query logging
    )
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    return engine, SessionLocal

# Auto-create all tables in the connected DB (cloud or local)
def create_all_tables(Base):
    engine, _ = get_cloud_engine()
    Base.metadata.create_all(bind=engine)
