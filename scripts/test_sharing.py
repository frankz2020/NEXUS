#!/usr/bin/env python3
"""
Test script to verify Google Drive sharing permissions are working.
Run this locally to check if your token has the correct scopes.

Usage: python scripts/test_sharing.py
"""

import os
import sys
import pickle

# Add project root to path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from news_bot.core import config

def check_token_scopes():
    """Check what scopes the current token has."""
    print("=" * 60)
    print("Checking token.pickle scopes...")
    print("=" * 60)
    
    if not os.path.exists(config.OAUTH_TOKEN_PICKLE_FILE):
        print(f"❌ token.pickle not found at: {config.OAUTH_TOKEN_PICKLE_FILE}")
        return None
    
    try:
        with open(config.OAUTH_TOKEN_PICKLE_FILE, 'rb') as f:
            creds = pickle.load(f)
        
        print(f"✅ Token loaded successfully")
        print(f"   Valid: {creds.valid}")
        print(f"   Expired: {creds.expired}")
        
        if hasattr(creds, 'scopes') and creds.scopes:
            print(f"\n📋 Token scopes:")
            for scope in creds.scopes:
                print(f"   - {scope}")
            
            # Check for required scopes
            has_docs = 'https://www.googleapis.com/auth/documents' in creds.scopes
            has_drive = 'https://www.googleapis.com/auth/drive.file' in creds.scopes or \
                       'https://www.googleapis.com/auth/drive' in creds.scopes
            
            print(f"\n🔍 Required scopes check:")
            print(f"   documents scope: {'✅' if has_docs else '❌'}")
            print(f"   drive.file scope: {'✅' if has_drive else '❌ MISSING!'}")
            
            if not has_drive:
                print("\n⚠️  Your token is MISSING the drive.file scope!")
                print("   This is why documents are not being shared publicly.")
                print("\n   To fix this:")
                print(f"   1. Delete the token file: rm {config.OAUTH_TOKEN_PICKLE_FILE}")
                print("   2. Run this script again (or use the app)")
                print("   3. Complete the OAuth consent flow in your browser")
                print("   4. Make sure to grant ALL requested permissions")
        else:
            print("⚠️  Could not read scopes from token")
        
        return creds
        
    except Exception as e:
        print(f"❌ Error loading token: {e}")
        return None


def test_create_and_share():
    """Test creating a document and making it public."""
    print("\n" + "=" * 60)
    print("Testing document creation and sharing...")
    print("=" * 60)
    
    from news_bot.reporting.google_docs_exporter import _get_credentials, _make_document_public
    from googleapiclient.discovery import build
    
    creds = _get_credentials()
    if not creds:
        print("❌ Failed to get credentials")
        return
    
    print("✅ Got credentials")
    
    try:
        # Create a test document
        docs_service = build('docs', 'v1', credentials=creds)
        doc = docs_service.documents().create(
            body={'title': 'NEXUS Test - Delete Me'}
        ).execute()
        
        doc_id = doc.get('documentId')
        doc_url = f"https://docs.google.com/document/d/{doc_id}/edit"
        print(f"✅ Created test document: {doc_url}")
        
        # Try to make it public
        success = _make_document_public(doc_id, creds)
        
        if success:
            print("\n✅ SUCCESS! Document sharing is working correctly.")
            print(f"   Anyone with this link can now view the document:")
            print(f"   {doc_url}")
        else:
            print("\n❌ FAILED to make document public.")
            print("   Check the error messages above for details.")
        
        # Cleanup prompt
        print(f"\n🗑️  Don't forget to delete the test document:")
        print(f"   {doc_url}")
        
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()


if __name__ == '__main__':
    creds = check_token_scopes()
    
    if creds:
        response = input("\n\nWould you like to test document creation and sharing? (y/n): ")
        if response.lower() == 'y':
            test_create_and_share()
    else:
        print("\n⚠️  No valid token found. Run the OAuth flow first.")
