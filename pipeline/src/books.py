"""전자책 홍보 문구 생성.

naver-blog-bot 프로젝트의 books.py와 같은 구조: books.json에서 책 정보를 읽어
이번 글의 주제(dataset)와 관련도가 높은 책을 고르고 홍보 문구를 만든다.
매년 1월에 books.json의 title/data_year/links 값만 바꾸면 코드 수정 없이 반영된다.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Optional

PROMO_TEMPLATE = (
    "이런 데이터를 직접 파이썬으로 분석해보고 싶으신 분들을 위해, 공공데이터포털 {property_type} "
    "매매/전월세 데이터를 분석하는 방법을 담은 전자책 《{title}》을 준비했어요. {data_year}년 데이터를 "
    "기준으로 다루고 있고, {platform}에서 만나보실 수 있어요: {link}"
)


def load_books(books_file: Path) -> Dict[str, dict]:
    return json.loads(books_file.read_text(encoding="utf-8"))


def _property_type_label(title: str) -> str:
    """책 제목 끝의 '_OOO편'에서 부동산 유형 이름만 뽑아낸다."""
    tail = title.rsplit("_", 1)[-1]
    return tail[:-1] if tail.endswith("편") else tail


def build_promo_text(book: dict) -> Optional[str]:
    """책 정보로 홍보 문구를 만든다. 등록된 링크가 하나도 없으면 None을 반환한다."""
    available_links = [(platform, link) for platform, link in book.get("links", {}).items() if link]
    if not available_links:
        return None

    # 여러 링크가 있으면 교보문고 등 먼저 등록된 순서를 우선한다 (재현 가능한 결과를 위해 random 대신 첫 항목 사용).
    platform, link = available_links[0]
    return PROMO_TEMPLATE.format(
        property_type=_property_type_label(book["title"]),
        title=book["title"],
        data_year=book["data_year"],
        platform=platform,
        link=link,
    )


def promo_for_book_key(books_file: Path, book_key: str) -> Optional[str]:
    """dataset config의 book_key(apartment/villa/officetel)로 바로 홍보 문구를 만든다."""
    books = load_books(books_file)
    book = books.get(book_key)
    if book is None:
        return None
    return build_promo_text(book)
