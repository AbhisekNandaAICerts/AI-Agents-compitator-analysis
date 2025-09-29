import argparse
import sqlite3
import sys
import os

def clear_sqlite_table(db_file, table_name, force):
    """
    Connects to a SQLite database and deletes all data from a specified table.
    """
    # Verify the database file exists before proceeding
    if not os.path.exists(db_file):
        print(f"Error: Database file not found at '{db_file}'")
        sys.exit(1)

    # ⚠️ Safety Check: Ask for confirmation unless the --yes flag is used
    if not force:
        # The triple quotes allow for multi-line f-strings
        confirm = input(f"""
🛑 WARNING: You are about to permanently delete ALL data
   from the table '{table_name}' in the database '{db_file}'.
   This action cannot be undone.

   Are you sure you want to continue? (y/N): """)
        if confirm.lower() != 'y':
            print("Operation cancelled by user.")
            sys.exit(0)

    conn = None
    try:
        # Establish a connection to the SQLite database
        conn = sqlite3.connect(db_file)
        cursor = conn.cursor()

        # Check if the table actually exists in the database
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,))
        if cursor.fetchone() is None:
            print(f"Error: Table '{table_name}' does not exist in '{db_file}'.")
            sys.exit(1)

        print(f"\nClearing data from table '{table_name}'...")

        # Execute the DELETE statement. For SQLite, DELETE FROM is used
        # to remove all rows.
        cursor.execute(f"DELETE FROM {table_name}")

        # Commit the transaction to make the changes permanent
        conn.commit()

        print(f"✅ Success! All data has been deleted from '{table_name}'.")

    except sqlite3.Error as e:
        print(f"❌ Database error: {e}")
        sys.exit(1)
    finally:
        # Ensure the connection is closed, even if an error occurred
        if conn:
            conn.close()
            print("Database connection closed.")

if __name__ == "__main__":


    clear_sqlite_table(db_file="test.db", table_name="hashtag", force=True)