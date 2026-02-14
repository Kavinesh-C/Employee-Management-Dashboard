"""
Simple test to verify photo storage changes are working correctly.
Run this after the application starts to test the photo endpoint.
"""

import requests
import json

BASE_URL = "http://localhost:8000"

def test_photo_endpoint():
    """Test the new photo endpoint"""
    print("Testing photo storage implementation...")
    print("-" * 50)
    
    # Note: This will fail if there's no photo with this employee_id
    # But it verifies the endpoint is working
    employee_id = "2261001"  # Replace with actual employee ID
    
    url = f"{BASE_URL}/api/employee/photo/{employee_id}"
    
    try:
        response = requests.get(url)
        
        if response.status_code == 200:
            print(f"✅ Photo endpoint working!")
            print(f"   Content-Type: {response.headers.get('content-type')}")
            print(f"   Photo size: {len(response.content)} bytes")
        elif response.status_code == 404:
            print(f"⚠️  Photo not found for employee {employee_id}")
            print("   This is normal if the employee has no photo")
        elif response.status_code == 403:
            print(f"❌ Access denied (authentication required)")
        else:
            print(f"❌ Unexpected status code: {response.status_code}")
            print(f"   Response: {response.text}")
            
    except requests.exceptions.ConnectionError:
        print("❌ Cannot connect to application")
        print("   Make sure the application is running at http://localhost:8000")
    except Exception as e:
        print(f"❌ Error: {str(e)}")
    
    print("-" * 50)
    print("Note: To fully test,:")
    print("1. Register a new employee with a photo")
    print("2. Check that photo displays correctly")
    print("3. Verify photo is in database (not filesystem)")

if __name__ == "__main__":
    test_photo_endpoint()
