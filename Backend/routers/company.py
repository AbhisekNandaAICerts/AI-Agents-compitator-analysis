# routers/company.py
from fastapi import APIRouter, Depends, HTTPException, status, Query
from typing import List, Optional
from sqlalchemy.orm import Session
import schemas
import models
from core.database import SessionLocal

router = APIRouter(prefix="/companies", tags=["companies"])

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# Create company
@router.post("/", response_model=schemas.CompanyOut, status_code=status.HTTP_201_CREATED)
def create_company(payload: schemas.CompanyCreate, db: Session = Depends(get_db)):
    existing = db.query(models.Company).filter(models.Company.company_name == payload.company_name).first()
    if existing:
        raise HTTPException(status_code=400, detail="Company with this name already exists")
    company = models.Company(
        company_name=payload.company_name,
        industry=payload.industry,
        headquarters=payload.headquarters,
        founded_year=payload.founded_year,
        employee_count=payload.employee_count,
        website=str(payload.website) if payload.website else None
    )
    db.add(company)
    db.commit()
    db.refresh(company)
    return company

# List companies
@router.get("/", response_model=List[schemas.CompanyOut])
def list_companies(
    db: Session = Depends(get_db),
    skip: int = Query(0, ge=0),
    limit: int = Query(25, ge=1, le=200),
    q: Optional[str] = Query(None, description="search by name or industry")
):
    query = db.query(models.Company)
    if q:
        like_q = f"%{q}%"
        query = query.filter((models.Company.company_name.ilike(like_q)) | (models.Company.industry.ilike(like_q)))
    results = query.offset(skip).limit(limit).all()
    return results

# Get single company with socials (optional)
@router.get("/{company_id}", response_model=schemas.CompanyWithSocials)
def get_company(company_id: int, db: Session = Depends(get_db)):
    company = db.query(models.Company).filter(models.Company.company_id == company_id).first()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    # eager load socials via relationship attribute
    return company

# Update company
@router.put("/{company_id}", response_model=schemas.CompanyOut)
def update_company(company_id: int, payload: schemas.CompanyUpdate, db: Session = Depends(get_db)):
    company = db.query(models.Company).filter(models.Company.company_id == company_id).first()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    update_data = payload.dict(exclude_unset=True)
    for key, value in update_data.items():
        setattr(company, key, value)
    db.add(company)
    db.commit()
    db.refresh(company)
    return company

# Delete company
@router.delete("/{company_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_company(company_id: int, db: Session = Depends(get_db)):
    company = db.query(models.Company).filter(models.Company.company_id == company_id).first()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    db.delete(company)
    db.commit()
    return None


# Search companies by name or industry (advanced search)
@router.get("/search/", response_model=List[schemas.CompanyOut])
def search_companies(
    db: Session = Depends(get_db),
    name: Optional[str] = Query(None, description="Search by company name"),
    industry: Optional[str] = Query(None, description="Search by industry"),
    headquarters: Optional[str] = Query(None, description="Search by headquarters"),
    founded_year: Optional[int] = Query(None, description="Search by founded year"),
    skip: int = Query(0, ge=0),
    limit: int = Query(25, ge=1, le=200)
):
    query = db.query(models.Company)
    if name:
        query = query.filter(models.Company.company_name.ilike(f"%{name}%"))
    if industry:
        query = query.filter(models.Company.industry.ilike(f"%{industry}%"))
    if headquarters:
        query = query.filter(models.Company.headquarters.ilike(f"%{headquarters}%"))
    if founded_year:
        query = query.filter(models.Company.founded_year == founded_year)
    results = query.offset(skip).limit(limit).all()
    return results