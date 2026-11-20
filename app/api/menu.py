from typing import Literal
import unicodedata

from fastapi import APIRouter, Depends, HTTPException, Path, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.auth import same_origin
from app.api.catalog import MAX_CATALOG_ID
from app.database import get_session
from app.models import MenuItem
from app.services.admin import require_admin
from app.services.reservations import begin_write

router = APIRouter(prefix='/api', tags=['Menu'])
admin_router = APIRouter(prefix='/api/admin', tags=['Menu administration'],
                         dependencies=[Depends(require_admin)])


class MenuInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default='', max_length=300)
    category: Literal['drinks', 'snacks', 'meals']
    price_minor: int = Field(ge=0, le=1_000_000)
    is_available: bool = True
    is_active: bool = True

    @field_validator('name', mode='before')
    @classmethod
    def clean_name(cls, value):
        if isinstance(value, str):
            if any(unicodedata.category(character).startswith('C') for character in value):
                raise ValueError('The name cannot contain control characters.')
            return value.strip()
        return value

    @field_validator('description', mode='before')
    @classmethod
    def clean_description(cls, value):
        return value.strip() if isinstance(value, str) else value


def menu_row(item, include_active=False):
    row = {'id': item.id, 'name': item.name, 'description': item.description,
           'category': item.category, 'price_minor': item.price_minor,
           'currency': 'PHP', 'is_available': item.is_available}
    if include_active:
        row['is_active'] = item.is_active
    return row


@router.get('/menu')
def public_menu(session: Session = Depends(get_session)):
    items = session.scalars(select(MenuItem).where(MenuItem.is_active.is_(True))
                            .order_by(MenuItem.category, MenuItem.name, MenuItem.id))
    return [menu_row(item) for item in items]


@admin_router.get('/menu')
def admin_menu(session: Session = Depends(get_session)):
    items = session.scalars(select(MenuItem).order_by(MenuItem.name, MenuItem.id))
    return [menu_row(item, include_active=True) for item in items]


@admin_router.post('/menu', status_code=201, dependencies=[Depends(same_origin)])
def create_menu_item(data: MenuInput, session: Session = Depends(get_session)):
    try:
        with begin_write(session):
            item = MenuItem(**data.model_dump())
            session.add(item)
            session.flush()
            row = menu_row(item, include_active=True)
    except IntegrityError:
        raise HTTPException(409, 'A menu item with this name already exists.') from None
    return row


@admin_router.put('/menu/{item_id}', dependencies=[Depends(same_origin)])
def update_menu_item(data: MenuInput, item_id: int = Path(gt=0, le=MAX_CATALOG_ID),
                     session: Session = Depends(get_session)):
    try:
        with begin_write(session):
            item = session.get(MenuItem, item_id)
            if item is None:
                raise HTTPException(404, 'Menu item not found.')
            item.name = data.name
            item.description = data.description
            item.category = data.category
            item.price_minor = data.price_minor
            item.is_available = data.is_available
            item.is_active = data.is_active
            session.flush()
            row = menu_row(item, include_active=True)
    except IntegrityError:
        raise HTTPException(409, 'A menu item with this name already exists.') from None
    return row


@admin_router.delete('/menu/{item_id}', status_code=204, dependencies=[Depends(same_origin)])
def delete_menu_item(item_id: int = Path(gt=0, le=MAX_CATALOG_ID),
                     session: Session = Depends(get_session)):
    try:
        with begin_write(session):
            item = session.get(MenuItem, item_id)
            if item is None:
                raise HTTPException(404, 'Menu item not found.')
            session.delete(item)
    except IntegrityError:
        raise HTTPException(409, 'This menu item could not be deleted.') from None
    return Response(status_code=204)
