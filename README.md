# Competitor AI Agent

The Competitor AI Agent is a comprehensive FastAPI-based backend designed for competitor intelligence. It automates the process of gathering data from company websites and social media (specifically LinkedIn), analyzes the content using AI for sentiment and key events, and exposes this information through a powerful RESTful API.

The system allows users to track companies, monitor their social media presence, receive alerts on significant activities, and perform detailed side-by-side comparisons.

## ✨ Features

* **Full Company & Profile Management**: CRUD operations for tracking competitor companies and their social media profiles.
* **Automated Social Media Crawling**: Initiates crawlers to fetch the latest posts and engagement data from LinkedIn company pages using Apify.
* **AI-Powered Content Analysis**:
    * **Sentiment Analysis**: Automatically determines the sentiment (positive, neutral, negative) of each social media post.
    * **Alert Generation**: Uses OpenAI's GPT models to identify and create alerts for significant competitor activities like product launches, major hires, or negative press.
* **Data-Rich Dashboards**: API endpoints designed to power analytical dashboards, providing:
    * Key Performance Indicators (KPIs) like total posts, likes, and engagement rates.
    * Time-series data for engagement and sentiment trends.
    * Lists of top-performing posts.
* **In-Depth Competitor Comparison**: A dedicated endpoint to generate a detailed, side-by-side comparison of two companies across social media metrics, sentiment trends, and recent alerts.
* **Hashtag Analytics**: Track and analyze the performance of specific hashtags across companies.
* **Secure Authentication**: JWT-based authentication protects all data-sensitive endpoints.
* **Advanced Web Crawling Suite**: Includes multiple scripts for various crawling strategies (keyword-based, sitemap-based, product-focused) using Playwright, Selenium, and BeautifulSoup.

## 🚀 Tech Stack

* **Backend**: FastAPI
* **Asynchronous Server**: Uvicorn
* **Database ORM**: SQLAlchemy
* **Database**: PostgreSQL (recommended), SQLite (for development)
* **Data Validation**: Pydantic
* **Authentication**: `python-jose` for JWT, `passlib` with `bcrypt` for hashing
* **Web Crawling**: Apify Client, Playwright, Selenium, Requests, BeautifulSoup4
* **AI/ML**: OpenAI API

## 📂 Project Structure
Backend/
├── core/                  # Core modules for auth and database connection
│   ├── auth.py
│   └── database.py
├── crawler/               # Collection of various web crawler scripts
│   ├── ai_crawler.py
│   ├── linkedin_crawler.py
│   └── product_crawler.py
├── routers/               # API endpoints (controllers)
│   ├── auth.py
│   ├── company.py
│   ├── crawler.py
│   ├── dashboard.py
│   └── comparisons.py
├── .env                   # Environment variables (needs to be created)
├── main.py                # Main FastAPI application entry point
├── models.py              # SQLAlchemy database models
├── requirements.txt       # Python dependencies
└── schemas.py             # Pydantic data schemas for API I/O

## 🛠️ Setup and Installation

### 1. Prerequisites

* Python 3.9+
* A running PostgreSQL server (or other SQLAlchemy-compatible database)
* An [Apify](httpss://apify.com/) account and API Token
* An [OpenAI](httpss://openai.com/) account and API Key

### 2. Clone the Repository

```sh
git clone <your-repo-url>
cd Backend
3. Set Up a Virtual Environment
Bash

# For macOS/Linux
python3 -m venv venv
source venv/bin/activate

# For Windows
python -m venv venv
.\venv\Scripts\activate
4. Install Dependencies
Bash

pip install -r requirements.txt
### Configure Environment Variables
Create a file named .env in the Backend/ directory and populate it with your credentials.

Code snippet

# Backend/.env

# --- Database Configuration ---
# Example for PostgreSQL
DATABASE_URL="postgresql://user:password@localhost:5432/competitor_ai"
# Example for SQLite (for simple local testing)
# DATABASE_URL="sqlite:///./test.db"

# --- JWT Authentication ---
# Generate a secure secret key using: openssl rand -hex 32
JWT_SECRET="<your-strong-random-secret-key>"
JWT_ALGORITHM="HS256"
JWT_EXPIRE_MINUTES="60"

# --- External API Keys ---
APIFY_API_TOKEN="<your-apify-api-token>"
OPENAI_API_KEY="<your-openai-api-key>"
6. Run the Application
The application uses uvicorn as the ASGI server.

Bash

uvicorn main:app --reload
The API will be live at http://127.0.0.1:8000.

You can access the interactive API documentation (Swagger UI) at http://127.0.0.1:8000/docs.

### ⚙️ API Usage Overview
All endpoints are prefixed with /api. Most endpoints require a bearer token in the Authorization header.

Register a User: POST /api/auth/register

Login: POST /api/auth/login to get your access token.

Add Companies: POST /api/companies to start tracking competitors.

Trigger a Crawl: POST /api/crawler/crawl/linkedin/{company_id} to fetch data for a company.

View Data: Use the /api/dashboard/* and /api/comparisons/* endpoints to analyze the collected data.

Key Endpoints
/api/auth: User registration and login.

/api/companies: Manage the companies you want to track.

/api/crawler: Trigger data collection tasks.

/api/dashboard: Get aggregated data, KPIs, and trends.

/api/comparisons: Perform detailed side-by-side analysis of two companies.

### 🕸️ Crawler Subsystem
The crawler/ directory contains various scripts for data collection. The primary crawler used in the API (routers/crawler.py) is powered by the Apify apimaestro/linkedin-company-posts actor.

After fetching data from Apify, the system processes each post:

AI Sentiment Analysis: The post content is sent to OpenAI to determine its sentiment.

AI Alert Analysis: The content is analyzed again to check if it contains information worthy of a competitor alert (e.g., product launch, funding news).

Database Storage: The post, its engagement metrics, sentiment, and any generated alerts are stored in the database.

### 🗄️ Database Management
The database schema is automatically created on application startup based on the definitions in models.py.

The repository also includes a utility script clear_table.py to wipe data from a specific table, which can be useful during development.

Usage:

Bash

# Be careful! This will permanently delete data.
python clear_table.py
