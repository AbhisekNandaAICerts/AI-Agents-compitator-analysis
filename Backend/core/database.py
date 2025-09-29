# database.py
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base
from sqlalchemy.orm import sessionmaker
import os

from dotenv import load_dotenv  
load_dotenv()  # take environment variables from .env file

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://your_username:your_password@localhost:5432/your_dbname")

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()
