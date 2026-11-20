"""Food and drinks displayed on the digital menu."""
from sqlalchemy import Boolean, CheckConstraint, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class MenuItem(Base):
    __tablename__ = 'menu_items'
    __table_args__ = (
        CheckConstraint('length(name) BETWEEN 1 AND 80'),
        CheckConstraint('length(description) <= 300'),
        CheckConstraint("category IN ('drinks', 'snacks', 'meals')"),
        CheckConstraint('price_minor BETWEEN 0 AND 1000000'),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80, collation='NOCASE'), unique=True)
    description: Mapped[str] = mapped_column(String(300), default='')
    category: Mapped[str] = mapped_column(String(10))
    price_minor: Mapped[int] = mapped_column(Integer)
    is_available: Mapped[bool] = mapped_column(Boolean, default=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
