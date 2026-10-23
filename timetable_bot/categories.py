"""The two timetable categories monitored by the bot."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Category(str, Enum):
    """Stable identifiers stored in SQLite."""

    SPECIALTY = "specialty"
    GENERAL = "general"


@dataclass(frozen=True, slots=True)
class CategoryInfo:
    category: Category
    label: str
    title: str
    filename_prefix: str
    slug: str


CATEGORY_INFO: dict[Category, CategoryInfo] = {
    Category.SPECIALTY: CategoryInfo(
        category=Category.SPECIALTY,
        label="Γ΄ Τάξης Ειδικότητας",
        title="📚 ΝΕΟ ΩΡΟΛΟΓΙΟ ΠΡΟΓΡΑΜΜΑ — Γ΄ ΤΑΞΗ ΕΙΔΙΚΟΤΗΤΑΣ",
        filename_prefix="Γ_ΤΑΞΗ_ΕΙΔΙΚΟΤΗΤΑΣ",
        slug="eidikotita",
    ),
    Category.GENERAL: CategoryInfo(
        category=Category.GENERAL,
        label="Γ΄ Τάξης Γενικής Παιδείας",
        title="📚 ΝΕΟ ΩΡΟΛΟΓΙΟ ΠΡΟΓΡΑΜΜΑ — Γ΄ ΤΑΞΗ ΓΕΝΙΚΗΣ ΠΑΙΔΕΙΑΣ",
        filename_prefix="Γ_ΤΑΞΗ_ΓΕΝΙΚΗΣ_ΠΑΙΔΕΙΑΣ",
        slug="geniki_paideia",
    ),
}

CATEGORY_ORDER: tuple[Category, ...] = (Category.SPECIALTY, Category.GENERAL)
