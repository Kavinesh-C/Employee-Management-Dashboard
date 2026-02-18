"""
Database migration script to add photo_data and photo_mime_type columns to users table.
"""

from sqlalchemy import text
from app.database import engine

def add_photo_columns():
    """Add photo_data and photo_mime_type columns to users table if they don't exist"""
    with engine.connect() as conn:
        # Check database type
        db_url = str(engine.url)
        is_sqlite = "sqlite" in db_url
        is_postgres = "postgresql" in db_url
        is_mysql = "mysql" in db_url
        
        try:
            # Get existing columns
            if is_sqlite:
                result = conn.execute(text("PRAGMA table_info(users)"))
                existing_cols = {row[1] for row in result}
            elif is_postgres:
                result = conn.execute(text("""
                    SELECT column_name FROM information_schema.columns 
                    WHERE table_name='users'
                """))
                existing_cols = {row[0] for row in result}
            elif is_mysql:
                result = conn.execute(text("""
                    SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS 
                    WHERE TABLE_NAME='users'
                """))
                existing_cols = {row[0] for row in result}
            else:
                print("Unknown database type")
                return
            
            # Add photo_data column if it doesn't exist
            if "photo_data" not in existing_cols:
                if is_sqlite:
                    conn.execute(text("ALTER TABLE users ADD COLUMN photo_data BLOB"))
                elif is_postgres:
                    conn.execute(text("ALTER TABLE users ADD COLUMN photo_data BYTEA"))
                elif is_mysql:
                    conn.execute(text("ALTER TABLE users ADD COLUMN photo_data LONGBLOB"))
                print("✅ Added photo_data column")
            else:
                print("ℹ️  photo_data column already exists")
            
            # Add photo_mime_type column if it doesn't exist
            if "photo_mime_type" not in existing_cols:
                if is_sqlite:
                    col_type = "VARCHAR(50)"
                elif is_postgres:
                    col_type = "VARCHAR(50)"
                elif is_mysql:
                    col_type = "VARCHAR(50)"
                
                conn.execute(text(f"ALTER TABLE users ADD COLUMN photo_mime_type {col_type}"))
                print("✅ Added photo_mime_type column")
            else:
                print("ℹ️  photo_mime_type column already exists")
            
            conn.commit()
            print("\n✅ Database migration complete!")
            
        except Exception as e:
            print(f"❌ Error during migration: {str(e)}")
            conn.rollback()

if __name__ == "__main__":
    add_photo_columns()
