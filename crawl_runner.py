import json
import re
import time
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple
from bs4 import BeautifulSoup
from util.selenium_utils import SeleniumUtils

class LululemonCrawler:
    def __init__(self):
        self.selenium_driver = SeleniumUtils(headless=False, debug=True)
        self.driver = self.selenium_driver.start_driver(
            view_mode="browser",
            window_size=(1024, 768)
        )
        self._size_rank = {}

    def product_api_data(self, url: str) -> Tuple[List[Dict[str, Any]], str]:
        try:
            print(f"[DEBUG] 접속 중: {url}")
            soup = self.fetch_product_soup(url)
            next_data = self.extract_next_data(soup)

            h1_tag = soup.find("h1")
            h1_product_name = h1_tag.get_text(" ", strip=True) if h1_tag else "상품명 없음"
            print(f"[DEBUG] 상품명 추출: {h1_product_name}")

            # 1. Variants 추출 (옵션 데이터)
            variants = self.extract_variants(next_data, soup)
            print(f"[DEBUG] 추출된 variant 개수: {len(variants)}")

            if variants:
                print(json.dumps(variants[0], indent=2, ensure_ascii=False))

            if not variants:
                print(f"[DEBUG] 추출 실패! URL 확인 필요: {url}")
                return [], h1_product_name

            # 2. 정규화
            rows = [self.normalize_variant(v) for v in variants]
            rows = [r for r in rows if r]

            # 3. 가격 계산 및 정렬
            min_price = self.find_min_price(rows)
            self.sort_rows_by_color_and_size(rows)

            out = []
            for r in rows:
                price_val = self.to_decimal(r.get("price"))
                diff = price_val - min_price if min_price is not None and price_val is not None else Decimal("0")
                out.append({
                    "컬러": self.shorten_color(str(r.get("color", ""))),
                    "사이즈": str(r.get("size", "")),
                    "옵션가": int(diff) * 1000 if diff > 0 else 0,
                    "재고수량": 5 if self.is_in_stock(r.get("availability", "")) else 0
                })

            print(f"[DEBUG] 최종 추출 옵션 개수: {len(out)}")
            return out, h1_product_name

        except Exception as e:
            print(f"[ERROR] 크롤링 전체 과정 오류: {e}")
            import traceback
            traceback.print_exc()
            return [], "Error"

    # --- 핵심 데이터 추출 ---
    def fetch_product_soup(self, url: str) -> BeautifulSoup:
        self.driver.get(url)
        self.selenium_driver.wait_ready_state_complete(timeout_sec=10)
        time.sleep(3) # 로딩 대기
        return BeautifulSoup(self.driver.page_source, "html.parser")

    def extract_next_data(self, soup: BeautifulSoup) -> Dict[str, Any]:
        sc = soup.find("script", id="__NEXT_DATA__")
        text = sc.string if sc else ""
        if not text:
            print("[DEBUG] __NEXT_DATA__ 태그 없음!")
            return {}
        try:
            data = json.loads(text)
            # 디버깅을 위해 JSON 저장
            with open("debug_next_data.json", "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            return data
        except Exception as e:
            print(f"[DEBUG] JSON 파싱 오류: {e}")
            return {}

    def extract_variants(self, next_data, soup) -> List[Dict[str, Any]]:

        try:
            product = (
                next_data.get("props", {})
                .get("pageProps", {})
                .get("initialData", {})
                .get("product", {})
            )

            variants = product.get("variants", [])

            if isinstance(variants, list) and variants:
                print(f"[DEBUG] product.variants 발견: {len(variants)}")
                return variants

        except Exception as e:
            print(f"[DEBUG] product.variants 탐색 실패: {e}")

        found = self.find_all_values_by_key(next_data, "variants")

        best = []

        for item in found:

            if not isinstance(item, list):
                continue

            dict_rows = [
                x for x in item
                if isinstance(x, dict)
            ]

            if len(dict_rows) > len(best):
                best = dict_rows

        if best:
            print(f"[DEBUG] 재귀탐색 variants 발견: {len(best)}")

        return best
    # --- 유틸리티 및 데이터 정규화 ---
    def normalize_variant(self, v) -> Optional[Dict[str, Any]]:

        offers = v.get("offers", {})

        if isinstance(offers, list):
            offers = offers[0] if offers else {}

        color = self.pick_first_text(
            v.get("color"),
            v.get("colour"),
            self.deep_get(v, ["attributes", "color"]),
            self.deep_get(v, ["attributes", "colour"]),
        )

        size = self.pick_first_text(
            v.get("size"),
            v.get("displaySize"),
            self.deep_get(v, ["attributes", "size"]),
            self.deep_get(v, ["attributes", "displaySize"]),
        )

        price = self.pick_first_text(
            offers.get("price"),
            v.get("price"),
            self.deep_get(v, ["pricing", "price"]),
            self.deep_get(v, ["priceInfo", "price"]),
        )

        avail = self.pick_first_text(
            offers.get("availability"),
            v.get("availability"),
            self.deep_get(v, ["inventory", "availability"]),
            self.deep_get(v, ["inventory", "status"]),
        )

        if not color and not size:
            return None

        return {
            "color": color,
            "size": size,
            "price": price,
            "availability": avail
        }
    def sort_rows_by_color_and_size(self, rows):
        rows.sort(key=lambda x: (str(x.get("color", "")), str(x.get("size", ""))))

    def find_min_price(self, rows):
        prices = [self.to_decimal(r.get("price")) for r in rows if r.get("price")]
        return min(prices) if prices else None

    def to_decimal(self, val):
        try: return Decimal(str(val).replace(",", ""))
        except: return None

    def is_in_stock(self, avail): return "instock" in str(avail).lower() or "available" in str(avail).lower()

    def shorten_color(self, c): return c[:25]

    def pick_first_text(self, *args):
        for a in args:
            if a: return str(a).strip()
        return ""

    def deep_get(self, d, keys):
        for k in keys:
            if isinstance(d, dict): d = d.get(k, {})
            else: return None
        return d if d != {} else None

    def find_all_values_by_key(self, data, target):
        found = []
        def walk(o):
            if isinstance(o, dict):
                for k, v in o.items():
                    if k == target: found.append(v)
                    walk(v)
            elif isinstance(o, list):
                for i in o: walk(i)
        walk(data)
        return found

    def close(self):
        if self.selenium_driver:
            self.selenium_driver.quit()