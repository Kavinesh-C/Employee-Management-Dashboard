from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import dotenv
import os
dotenv.load_dotenv()

# Import cloud MariaDB connection URL for reference (not used directly)
from app.cloud_database import CLOUD_DATABASE_URL

# Always use DATABASE_URL from .env file
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL must be set in your .env file.")

# Auto-detect if the URL is local or cloud
def is_local_database(url):
    return (
        url is not None and (
            "localhost" in url or
            "127.0.0.1" in url
        )
    )

IS_LOCAL_DB = is_local_database(DATABASE_URL)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_team_info(db, user_id):
    team = db.execute("""
        SELECT
            t.name,
            leader.name,
            leader.active_leader
        FROM teams t
        JOIN users leader ON leader.id = t.leader_id
        WHERE t.id = (
            SELECT current_team_id FROM users WHERE id=?
        )
    """, (user_id,)).fetchone()

    members = db.execute("""
        SELECT name FROM users
        WHERE current_team_id = (
            SELECT current_team_id FROM users WHERE id=?
        )
    """, (user_id,)).fetchall()

    return {
        "team_name": team[0],
        "leader": team[1],
        "is_acting": bool(team[2]),
        "members": [m[0] for m in members]
    }
