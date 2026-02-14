"""
Migration script to move photos from local filesystem to database.
This script reads photos from static/uploads/users/ and stores them in the database.
"""

import os
import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal
from app.models import User
from Security.key_management import hash_value as _hash_value

def migrate_photos_to_db():
    """Migrate photos from filesystem to database"""
    db = SessionLocal()
    uploads_dir = os.path.join("static", "uploads", "users")
    
    if not os.path.exists(uploads_dir):
        print(f"Uploads directory not found: {uploads_dir}")
        return
    
    photo_files = os.listdir(uploads_dir)
    print(f"Found {len(photo_files)} photo files to migrate")
    
    migrated = 0
    skipped = 0
    
    for photo_file in photo_files:
        try:
            # Extract employee_id from filename (format: employee_id_randomhex.ext)
            parts = photo_file.rsplit('_', 1)
            if len(parts) != 2:
                print(f"Skipped {photo_file}: Invalid filename format")
                skipped += 1
                continue
            
            employee_id = parts[0]
            
            # Find user by employee_id hash
            user = db.query(User).filter(User.employee_id_hash == _hash_value(employee_id)).first()
            
            if not user:
                print(f"Skipped {photo_file}: Employee {employee_id} not found")
                skipped += 1
                continue
            
            # Skip if user already has photo_data
            if user.photo_data:
                print(f"Skipped {photo_file}: User already has photo_data in database")
                skipped += 1
                continue
            
            # Read photo file
            photo_path = os.path.join(uploads_dir, photo_file)
            with open(photo_path, 'rb') as f:
                photo_bytes = f.read()
            
            # Determine MIME type from extension
            ext = os.path.splitext(photo_file)[1].lower()
            mime_types = {
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".png": "image/png",
                ".webp": "image/webp"
            }
            mime_type = mime_types.get(ext, "application/octet-stream")
            
            # Store in database
            user.photo_data = photo_bytes
            user.photo_mime_type = mime_type
            db.commit()
            
            print(f"Migrated {photo_file} ({len(photo_bytes)} bytes)")
            migrated += 1
            
        except Exception as e:
            print(f"Error migrating {photo_file}: {str(e)}")
            db.rollback()
            skipped += 1
    
    db.close()
    print(f"\n✅ Migration complete: {migrated} photos migrated, {skipped} skipped")

if __name__ == "__main__":
    migrate_photos_to_db()
