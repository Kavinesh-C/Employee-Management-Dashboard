#!/usr/bin/env python
"""
Migration script to add meeting_link and room_name columns to meetings table
Run this script once to update your database schema
"""

from sqlalchemy import text
from database import SessionLocal

def migrate():
    db = SessionLocal()
    try:
        print("🔄 Starting database migration...")
        
        # Add the columns if they don't exist
        sql = """
            ALTER TABLE meetings 
            ADD COLUMN IF NOT EXISTS meeting_link VARCHAR(500) DEFAULT NULL,
            ADD COLUMN IF NOT EXISTS room_name VARCHAR(200) DEFAULT NULL
        """
        
        db.execute(text(sql))
        db.commit()
        
        print("✅ Migration completed successfully!")
        print("   - Added meeting_link column (VARCHAR 500)")
        print("   - Added room_name column (VARCHAR 200)")
        print("\nYou can now restart your uvicorn server!")
        
    except Exception as e:
        print(f"❌ Migration failed: {e}")
        print("\nTry running this SQL manually in your database:")
        print("""
        ALTER TABLE meetings 
        ADD COLUMN IF NOT EXISTS meeting_link VARCHAR(500) DEFAULT NULL,
        ADD COLUMN IF NOT EXISTS room_name VARCHAR(200) DEFAULT NULL;
        """)
        db.rollback()
    finally:
        db.close()

if __name__ == "__main__":
    migrate()
