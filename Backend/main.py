# main.py
from fastapi import FastAPI,Depends
from core.database import Base, engine
from routers import crawler, company, company_social, auth,dashboard,alert_sentiments, comparisons
from core.auth import get_current_user


Base.metadata.create_all(bind=engine)

app = FastAPI(title="Competitor AI Agent")

app.include_router(auth.router, prefix="/api", tags=["auth"])
app.include_router(crawler.router, prefix="/api", tags=["crawler"],dependencies=[Depends(get_current_user)])
app.include_router(company.router, prefix="/api", tags=["companies"],dependencies=[Depends(get_current_user)])
app.include_router(company_social.router, prefix="/api", tags=["company_socials"],dependencies=[Depends(get_current_user)])
app.include_router(dashboard.router, prefix="/api", dependencies=[Depends(get_current_user)])
app.include_router(alert_sentiments.router, prefix="/api", dependencies=[Depends(get_current_user)])
app.include_router(comparisons.router, prefix="/api", dependencies=[Depends(get_current_user)])
