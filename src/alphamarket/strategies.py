"""Listing editor (M4.3): name, thesis, tags, public toggle.

Only the qualitative fields are editable. Everything numeric on a strategy
page is platform-computed (invariant #5) and has no write path here.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from alphamarket.auth import current_user
from alphamarket.db import get_db
from alphamarket.models import Strategy, User

router = APIRouter(prefix="/strategies", tags=["strategies"])


class ListingIn(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    description: str | None = Field(default=None, max_length=4000)
    style_tags: str | None = Field(default=None, max_length=255)
    public: bool | None = None


class StrategyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    slug: str
    name: str
    description: str
    style_tags: str
    public: bool
    status: str


def owned_strategy(db: Session, strategy_id: int, user: User) -> Strategy:
    strategy = db.get(Strategy, strategy_id)
    if strategy is None or strategy.user_id != user.id:
        raise HTTPException(status_code=404, detail="Strategy not found")
    return strategy


def update_listing(db: Session, user: User, strategy_id: int, payload: ListingIn) -> Strategy:
    strategy = owned_strategy(db, strategy_id, user)
    for field_name, value in payload.model_dump(exclude_none=True).items():
        setattr(strategy, field_name, value)
    db.commit()
    return strategy


@router.patch("/{strategy_id}", response_model=StrategyOut)
def patch_listing(
    strategy_id: int,
    payload: ListingIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    return update_listing(db, user, strategy_id, payload)
