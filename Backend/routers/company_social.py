# routers/company_social.py
from fastapi import APIRouter, Depends, HTTPException, status
from typing import List
from sqlalchemy.orm import Session
import schemas
import models

from core.database import SessionLocal

router = APIRouter(prefix="/company-socials", tags=["company_socials"])

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# Create social for a company
@router.post("/company/{company_id}", response_model=schemas.CompanySocialOut, status_code=status.HTTP_201_CREATED)
def create_social_for_company(company_id: int, payload: schemas.CompanySocialCreate, db: Session = Depends(get_db)):
    # ensure company exists
    company = db.query(models.Company).filter(models.Company.company_id == company_id).first()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    social = models.CompanySocial(
        company_id=company_id,
        platform_name=payload.platform_name,
        profile_url=str(payload.profile_url)
    )
    db.add(social)
    db.commit()
    db.refresh(social)
    return social

# List socials for a company
@router.get("/company/{company_id}", response_model=List[schemas.CompanySocialOut])
def list_socials_for_company(company_id: int, db: Session = Depends(get_db)):
    company = db.query(models.Company).filter(models.Company.company_id == company_id).first()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    socials = db.query(models.CompanySocial).filter(models.CompanySocial.company_id == company_id).all()
    return socials

# Update social
@router.put("/{social_id}", response_model=schemas.CompanySocialOut)
def update_social(social_id: int, payload: schemas.CompanySocialUpdate, db: Session = Depends(get_db)):
    social = db.query(models.CompanySocial).filter(models.CompanySocial.social_id == social_id).first()
    if not social:
        raise HTTPException(status_code=404, detail="Social profile not found")
    data = payload.dict(exclude_unset=True)
    for k, v in data.items():
        setattr(social, k, v)
    db.add(social)
    db.commit()
    db.refresh(social)
    return social

# Delete social
@router.delete("/{social_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_social(social_id: int, db: Session = Depends(get_db)):
    social = db.query(models.CompanySocial).filter(models.CompanySocial.social_id == social_id).first()
    if not social:
        raise HTTPException(status_code=404, detail="Social profile not found")
    db.delete(social)
    db.commit()
    return None
