from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import dotenv
import os
dotenv.load_dotenv()

# MariaDB connection (update with your credentials)
DATABASE_URL = os.getenv("DATABASE_URL")

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
