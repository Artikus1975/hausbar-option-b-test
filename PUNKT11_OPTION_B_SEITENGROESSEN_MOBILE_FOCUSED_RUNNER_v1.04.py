#!/usr/bin/env python3
"""Focused mobile-first private page-size comparison for Hausbar Option B.

Read-only source contract:
- baseline site is the sealed Option-B PAGE_SIZE=20 site
- isolated variants: 10, 15, 20, 25, 30
- 10/15/25/30 may differ only in inventory-view.js PAGE_SIZE and integrity.json
- no app/data/image/evidence file is changed
- no product decision is made by this runner

Device focus requested by Murat:
- Samsung Galaxy S25/S26-class regular flagship: Chromium, 360x780 CSS px
- iPhone 17 Pro Max: WebKit proxy, 440x956 CSS px

Mobile image/offline-image-package features are intentionally OUT OF SCOPE for the
page-size product decision. The existing 154 image assets remain byte-protected, but
no mobile image/offline-image UX requirement is imposed by this runner.

A short real iPhone/Safari finalist check remains required because Linux Playwright
WebKit is not identical to iOS Safari. Desktop is finalist-only smoke testing.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import importlib.metadata
import os
import platform
import shutil
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright

EXPECTED_BRANCH_ZIP_SHA256 = "87f406d725dbb4f807991fdbe4adaaf79a408a6629e5927c7e19743dbd0e04ab"
EXPECTED_BASE_SITE_TREE_SHA256 = "96869e90ec87d79a389c91a71d150d6276bbe0675601c27d1703f7e2f6026980"
EXPECTED_SITE_FILES = 178
EXPECTED_IMAGES = 154
EXPECTED_PRODUCTS = 142
PAGE_SIZES = [10, 15, 20, 25, 30]
DEVICE_PROFILES = [
    {"name": "galaxy_s25_s26", "engine": "chromium", "width": 360, "height": 780, "scoredRuns": 5},
    {"name": "iphone_17_pro_max", "engine": "webkit", "width": 440, "height": 956, "scoredRuns": 5},
]
EXPECTED_VARIANT_TREES = {
    10: "bcbad57530276b009f8bd0a96d886497f397d45609bf6257490034c8877a6925",
    15: "0eefb7de4c03ce24d35280509184d67bbb936aa0dab32545e9cb4e780718df76",
    20: "96869e90ec87d79a389c91a71d150d6276bbe0675601c27d1703f7e2f6026980",
    25: "3abdca577aa2d4c2af04425c48c1e16b4fbb0baa73d572bd7aa0d2066776adc4",
    30: "b3b749a7d23190c715d9da370b42ca80d87bb95449e8ae915a61a9341ea86e53",
}
PROTECTED_SITE_PATHS = [
    "data/inventory.json",
    "data/assets.json",
    "data/export-metadata.json",
    "offline-assets.json",
    "service-worker.js",
    "index.html",
    "styles.css",
    "app.js",
    "inventory-core.js",
    "manifest.webmanifest",
]
BUDGETS = {
    "sort_p95_ms": 150.0,
    "scroll_frame_gap_p95_ms": 50.0,
    "scroll_frame_gap_max_ms": 150.0,
    "scroll_long_task_max_ms": 250.0,
    "scroll_long_task_total_ms": 500.0,
}
ANDROID_UA = (
    "Mozilla/5.0 (Linux; Android 14; Mobile) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36"
)
IPHONE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def canonical_site_tree(root: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    files = sorted(p for p in root.rglob("*") if p.is_file())
    for p in files:
        rel = p.relative_to(root).as_posix()
        digest = sha256_file(p)
        size = p.stat().st_size
        h.update(rel.encode("utf-8")); h.update(b"\0")
        h.update(digest.encode("ascii")); h.update(b"\0")
        h.update(str(size).encode("ascii")); h.update(b"\n")
    return h.hexdigest(), len(files)


def percentile_linear(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    data = sorted(float(v) for v in values)
    if len(data) == 1:
        return data[0]
    pos = (len(data) - 1) * q
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return data[lo]
    return data[lo] + (data[hi] - data[lo]) * (pos - lo)


def build_variants(base_site: Path, variants_root: Path) -> list[dict[str, Any]]:
    base_tree, base_count = canonical_site_tree(base_site)
    if base_tree != EXPECTED_BASE_SITE_TREE_SHA256 or base_count != EXPECTED_SITE_FILES:
        raise RuntimeError(f"BASE SOURCE LOCK FAIL: tree={base_tree} files={base_count}")

    image_root = base_site / "assets/images/inventory"
    base_images = {
        p.relative_to(image_root).as_posix(): sha256_file(p)
        for p in image_root.rglob("*") if p.is_file()
    }
    if len(base_images) != EXPECTED_IMAGES:
        raise RuntimeError(f"BASE IMAGE COUNT FAIL: {len(base_images)}")

    if variants_root.exists():
        shutil.rmtree(variants_root)
    variants_root.mkdir(parents=True)

    rows: list[dict[str, Any]] = []
    for size in PAGE_SIZES:
        site = variants_root / f"page_size_{size}"
        shutil.copytree(base_site, site)
        changed_paths: list[str] = []
        if size != 20:
            inv = site / "inventory-view.js"
            text = inv.read_text(encoding="utf-8")
            old = "const PAGE_SIZE = 20;"
            new = f"const PAGE_SIZE = {size};"
            if text.count(old) != 1:
                raise RuntimeError(f"PAGE_SIZE source count fail for {size}")
            inv.write_text(text.replace(old, new), encoding="utf-8")
            changed_paths.append("inventory-view.js")

            integrity_path = site / "integrity.json"
            integrity = json.loads(integrity_path.read_text(encoding="utf-8"))
            found = False
            for row in integrity["files"]:
                if row["path"] == "inventory-view.js":
                    row["bytes"] = inv.stat().st_size
                    row["sha256"] = sha256_file(inv)
                    found = True
            if not found:
                raise RuntimeError("inventory-view.js integrity entry missing")
            integrity_path.write_text(
                json.dumps(integrity, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            changed_paths.append("integrity.json")

        actual_tree, count = canonical_site_tree(site)
        if actual_tree != EXPECTED_VARIANT_TREES[size] or count != EXPECTED_SITE_FILES:
            raise RuntimeError(
                f"VARIANT TREE FAIL size={size}: tree={actual_tree} files={count}"
            )

        protected_ok = all(
            sha256_file(site / rel) == sha256_file(base_site / rel)
            for rel in PROTECTED_SITE_PATHS
        )
        imgs = {
            p.relative_to(site / "assets/images/inventory").as_posix(): sha256_file(p)
            for p in (site / "assets/images/inventory").rglob("*") if p.is_file()
        }
        if not protected_ok or imgs != base_images:
            raise RuntimeError(f"PROTECTED BYTES FAIL size={size}")

        # Independent file diff against baseline.
        diffs = []
        for bp in sorted(p for p in base_site.rglob("*") if p.is_file()):
            rel = bp.relative_to(base_site)
            vp = site / rel
            if sha256_file(bp) != sha256_file(vp):
                diffs.append(rel.as_posix())
        expected_diffs = [] if size == 20 else ["integrity.json", "inventory-view.js"]
        if diffs != expected_diffs:
            raise RuntimeError(f"UNEXPECTED VARIANT DIFF size={size}: {diffs}")

        rows.append({
            "pageSize": size,
            "siteTreeSha256": actual_tree,
            "siteFiles": count,
            "images": len(imgs),
            "protectedPass": True,
            "diffsFromBaseline": diffs,
            "pageCount": math.ceil(EXPECTED_PRODUCTS / size),
        })
    return rows


def make_context(browser: Browser, engine: str, width: int, height: int) -> BrowserContext:
    kwargs: dict[str, Any] = {
        "viewport": {"width": width, "height": height},
        "is_mobile": True,
        "has_touch": True,
        "device_scale_factor": 2 if engine == "chromium" else 3,
        "locale": "de-DE",
        "timezone_id": "Europe/Berlin",
        "service_workers": "allow",
    }
    kwargs["user_agent"] = ANDROID_UA if engine == "chromium" else IPHONE_UA
    return browser.new_context(**kwargs)


def wait_ready(page: Page, timeout_ms: int = 45000) -> None:
    page.wait_for_function(
        """() => document.documentElement.dataset.ready === 'true' &&
                   document.documentElement.dataset.inventoryReady === 'true' &&
                   document.querySelector('#inventory-list')?.getAttribute('aria-busy') === 'false' &&
                   document.querySelectorAll('#inventory-list > li').length > 0""",
        timeout=timeout_ms,
    )


def settle_two_frames(page: Page) -> None:
    page.evaluate(
        """() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))"""
    )


def set_search(page: Page, value: str) -> None:
    page.locator("#inventory-search").fill(value)
    page.wait_for_timeout(140)
    settle_two_frames(page)


def same_origin(url: str, base_origin: str) -> bool:
    if url.startswith("data:") or url.startswith("blob:"):
        return True
    try:
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}" == base_origin
    except Exception:
        return False


def init_runtime_observers(page: Page) -> dict[str, Any]:
    return page.evaluate(
        """() => {
          window.__p11Errors = [];
          window.addEventListener('error', e => window.__p11Errors.push(String(e.message || e.error || 'window error')));
          window.addEventListener('unhandledrejection', e => window.__p11Errors.push(String(e.reason || 'unhandled rejection')));
          window.__p11LongTasks = [];
          let supported = false;
          try {
            window.__p11LongTaskObserver = new PerformanceObserver(list => {
              for (const e of list.getEntries()) window.__p11LongTasks.push({startTime:e.startTime,duration:e.duration});
            });
            window.__p11LongTaskObserver.observe({entryTypes:['longtask']});
            supported = true;
          } catch (e) {}
          return {longTaskSupported:supported};
        }"""
    )


def functional_mobile_case(
    browser: Browser,
    engine: str,
    url: str,
    page_size: int,
    width: int,
    height: int,
) -> dict[str, Any]:
    context = make_context(browser, engine, width, height)
    page = context.new_page()
    requests: list[str] = []
    page_errors: list[str] = []
    console_errors: list[str] = []
    page.on("request", lambda req: requests.append(req.url))
    page.on("pageerror", lambda exc: page_errors.append(str(exc)))
    page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
    base_origin = "http://127.0.0.1:4173"
    checks: dict[str, bool] = {}
    detail: dict[str, Any] = {}
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        wait_ready(page)
        settle_two_frames(page)
        # Warm SW/app then clear request evidence for measured overview reload.
        requests.clear()
        page.reload(wait_until="domcontentloaded", timeout=60000)
        wait_ready(page)
        settle_two_frames(page)

        initial = page.evaluate(
            """() => {
              const rows=[...document.querySelectorAll('#inventory-list > li')];
              const first=rows[0];
              const details=[...document.querySelectorAll('#inventory-list [data-product-open]')];
              const pag=[document.querySelector('#inventory-page-prev'),document.querySelector('#inventory-page-next')].filter(Boolean);
              const targetBoxes=[...details,...pag].map(el=>{const b=el.getBoundingClientRect();return {w:b.width,h:b.height};});
              const rowOverflow=rows.some(r=>{const b=r.getBoundingClientRect();return b.left < -1 || b.right > innerWidth + 1;});
              const buttonOverflow=details.some(r=>{const b=r.getBoundingClientRect();return b.left < -1 || b.right > innerWidth + 1;});
              return {
                visible: rows.length,
                pageStatus: document.querySelector('#inventory-page-status')?.textContent || '',
                countText: document.querySelector('#result-count')?.textContent || '',
                listTag: document.querySelector('#inventory-list')?.tagName || '',
                allLi: rows.every(r=>r.tagName==='LI'),
                oneDetailsEach: rows.every(r=>r.querySelectorAll('[data-product-open]').length===1),
                noOverviewImages: rows.every(r=>r.querySelectorAll('img').length===0),
                firstOrder: first ? [...first.querySelectorAll('.product-row__title,.product-row__maker,.product-row__category,.product-row__meta,[data-product-open]')].map(n=>n.classList.contains('product-row__title')?'title':n.classList.contains('product-row__maker')?'maker':n.classList.contains('product-row__category')?'category':n.classList.contains('product-row__meta')?'meta':'details') : [],
                liveRole: document.querySelector('#result-count')?.getAttribute('role') || '',
                liveAria: document.querySelector('#result-count')?.getAttribute('aria-live') || '',
                innerWidth,
                docScrollWidth: document.documentElement.scrollWidth,
                rowOverflow,
                buttonOverflow,
                minTargetW: Math.min(...targetBoxes.map(x=>x.w)),
                minTargetH: Math.min(...targetBoxes.map(x=>x.h)),
              };
            }"""
        )
        geometry = page.evaluate(
            """() => new Promise(resolve => {
              const list=document.querySelector('#inventory-list');
              const rows=[...document.querySelectorAll('#inventory-list > li')];
              const listHeight=list?.getBoundingClientRect().height||0;
              list?.scrollIntoView({block:'start',behavior:'auto'});
              requestAnimationFrame(()=>requestAnimationFrame(()=>{
                const visibleRows=rows.filter(r=>{const b=r.getBoundingClientRect();return b.bottom>0 && b.top<innerHeight;}).length;
                const avgRowHeight=rows.length ? rows.reduce((a,r)=>a+r.getBoundingClientRect().height,0)/rows.length : 0;
                window.scrollTo(0,0);
                resolve({listHeight,visibleRowsAtListTop:visibleRows,avgRowHeight,viewportHeight:innerHeight,listScreens:listHeight/innerHeight});
              }));
            })"""
        )
        expected_pages = math.ceil(EXPECTED_PRODUCTS / page_size)
        product_image_requests = [u for u in requests if "/assets/images/inventory/" in u]
        external_requests = [u for u in requests if not same_origin(u, base_origin)]
        checks.update({
            "initial_visible": initial["visible"] == min(page_size, EXPECTED_PRODUCTS),
            "page_status": initial["pageStatus"] == f"Seite 1 von {expected_pages}",
            "semantic_list": initial["listTag"] == "UL" and initial["allLi"],
            "details_one_each": initial["oneDetailsEach"],
            "no_overview_images": initial["noOverviewImages"],
            "automatic_product_image_requests_zero": len(product_image_requests) == 0,
            "external_requests_zero": len(external_requests) == 0,
            "live_region_semantics": initial["liveRole"] == "status" and initial["liveAria"] == "polite",
            "dom_order": initial["firstOrder"][:3] == ["title", "maker", "category"] and initial["firstOrder"][-1:] == ["details"],
            "mobile_no_horizontal_overflow": initial["docScrollWidth"] <= initial["innerWidth"] + 1 and not initial["rowOverflow"] and not initial["buttonOverflow"],
            "option_b_touch_targets_44": initial["minTargetW"] >= 44 and initial["minTargetH"] >= 44,
        })

        # Global search and exact search semantics.
        set_search(page, "Western")
        western = page.evaluate(
            """() => ({count:document.querySelectorAll('#inventory-list > li').length,
                        countText:document.querySelector('#result-count')?.textContent||'',
                        pageStatus:document.querySelector('#inventory-page-status')?.textContent||'',
                        title:document.querySelector('.product-row__title')?.textContent||''})"""
        )
        checks["search_global_western"] = (
            western["count"] == 1 and "Western Gold" in western["title"] and western["pageStatus"] == "Seite 1 von 1"
        )
        search_counts = {}
        for token in ["wes", "west", "ester"]:
            set_search(page, token)
            search_counts[token] = page.locator("#inventory-list > li").count()
        checks["search_semantics"] = search_counts == {"wes": 0, "west": 1, "ester": 0}
        set_search(page, "")

        # Filter then sort globally across all filtered pages.
        page.locator("#filter-origin").select_option("Italien")
        settle_two_frames(page)
        italy_count_text = page.locator("#result-count").inner_text()
        italy_page_status = page.locator("#inventory-page-status").inner_text()
        checks["filter_italy_global"] = (
            "21 Produkte" in italy_count_text and italy_page_status == f"Seite 1 von {math.ceil(21/page_size)}"
        )
        page.locator("#inventory-sort").select_option("name")
        settle_two_frames(page)
        italy_names: list[str] = []
        while True:
            italy_names.extend(page.locator("#inventory-list .product-row__title").all_inner_texts())
            next_btn = page.locator("#inventory-page-next")
            if next_btn.is_disabled():
                break
            next_btn.click()
            settle_two_frames(page)
        checks["sort_global_filtered"] = (
            len(italy_names) == 21
            and italy_names[0] == "Amaro Averna Siciliano"
            and italy_names == page.evaluate("names => [...names].sort((a,b)=>a.localeCompare(b, 'de'))", italy_names)
        )

        # Reset to baseline and test keyboard focus on pagination.
        reset_btn = page.locator("#filter-reset")
        if reset_btn.is_enabled():
            reset_btn.click()
        else:
            set_search(page, "")
            page.locator("#filter-origin").select_option(index=0)
            page.locator("#inventory-sort").select_option("nr")
        settle_two_frames(page)
        if expected_pages > 1:
            page.locator("#inventory-page-next").focus()
            page.locator("#inventory-page-next").press("Enter")
            settle_two_frames(page)
            focus = page.evaluate(
                """() => ({status:document.querySelector('#inventory-page-status')?.textContent||'',
                            active:document.activeElement?.dataset?.productOpen||'',
                            first:document.querySelector('[data-product-open]')?.dataset?.productOpen||''})"""
            )
            checks["pagination_focus"] = (
                focus["status"] == f"Seite 2 von {expected_pages}"
                and bool(focus["active"])
                and focus["active"] == focus["first"]
            )
            page.locator("#inventory-page-prev").click()
            settle_two_frames(page)
        else:
            checks["pagination_focus"] = True

        # Dialog/focus only. Product images are intentionally not part of the mobile page-size decision.
        first_details = page.locator("[data-product-open]").first
        first_id = first_details.get_attribute("data-product-open") or ""
        first_details.focus()
        first_details.press("Enter")
        dialog = page.locator("#product-dialog")
        dialog.wait_for(state="visible", timeout=10000)

        focus_inside = True
        for _ in range(8):
            page.keyboard.press("Tab")
            inside = page.evaluate("() => document.querySelector('#product-dialog')?.contains(document.activeElement) || false")
            focus_inside = focus_inside and bool(inside)
        page.keyboard.press("Shift+Tab")
        focus_inside = focus_inside and bool(page.evaluate("() => document.querySelector('#product-dialog')?.contains(document.activeElement) || false"))
        checks["dialog_focus_trap"] = focus_inside
        page.keyboard.press("Escape")
        settle_two_frames(page)
        returned = page.evaluate("() => document.activeElement?.dataset?.productOpen || ''")
        checks["dialog_focus_return"] = returned == first_id

        # Full 142-ID traversal / order at this viewport.
        # The reset control is correctly disabled when no search/filter state is active.
        # Only click it when enabled; otherwise the baseline is already neutral.
        ids: list[str] = []
        reset_btn = page.locator("#filter-reset")
        if reset_btn.is_enabled():
            reset_btn.click()
            settle_two_frames(page)
        checks["neutral_state_before_full_traversal"] = reset_btn.is_disabled()
        while True:
            ids.extend(page.locator("#inventory-list [data-product-open]").evaluate_all("els => els.map(e=>e.dataset.productOpen)"))
            nxt = page.locator("#inventory-page-next")
            if nxt.is_disabled():
                break
            nxt.click()
            settle_two_frames(page)
        inv_data = page.evaluate("async () => await (await fetch('./data/inventory.json')).json()")
        expected_ids = [str(r["id"]) for r in inv_data["items"]]
        checks["all_142_ids_and_order"] = ids == expected_ids and len(set(ids)) == EXPECTED_PRODUCTS

        detail = {
            "initial": initial,
            "geometry": geometry,
            "initialProductImageRequests": product_image_requests,
            "externalRequests": external_requests,
            "searchCounts": search_counts,
            "western": western,
            "italyNames": italy_names,
            "coveredIds": len(ids),
            "pageErrors": page_errors,
            "consoleErrors": console_errors,
        }
        checks["runtime_errors_zero"] = len(page_errors) == 0
        # Console errors are evidence, but not a hard gate because browser engines may log benign PWA messages.
        status = "PASS" if all(checks.values()) else "FAIL"
        return {
            "status": status,
            "engine": engine,
            "pageSize": page_size,
            "viewportCssPx": [width, height],
            "checks": checks,
            "detail": detail,
        }
    finally:
        context.close()


def measure_sort(page: Page) -> float:
    return float(page.evaluate(
        """() => new Promise(resolve => {
          const s=document.querySelector('#inventory-sort');
          s.value='nr'; s.dispatchEvent(new Event('change',{bubbles:true}));
          requestAnimationFrame(() => requestAnimationFrame(() => {
            const start=performance.now();
            s.value='name'; s.dispatchEvent(new Event('change',{bubbles:true}));
            requestAnimationFrame(() => requestAnimationFrame(() => resolve(performance.now()-start)));
          }));
        })"""
    ))


def measure_scroll(page: Page, expected_pages: int) -> dict[str, Any]:
    gaps: list[float] = []
    ids: list[str] = []
    transition_ms: list[float] = []
    for page_no in range(1, expected_pages + 1):
        rows = page.locator("#inventory-list > li")
        count = rows.count()
        for i in range(count):
            sample = rows.nth(i).evaluate(
                """el => new Promise(resolve => {
                  el.scrollIntoView({block:'center',behavior:'auto'});
                  requestAnimationFrame(t1 => requestAnimationFrame(t2 => resolve({gap:t2-t1,id:el.querySelector('[data-product-open]')?.dataset?.productOpen||''})));
                })"""
            )
            gaps.append(float(sample["gap"]))
            ids.append(str(sample["id"]))
        if page_no < expected_pages:
            transition = page.evaluate(
                """() => new Promise(resolve => {
                  const b=document.querySelector('#inventory-page-next'); const start=performance.now();
                  b.click(); requestAnimationFrame(()=>requestAnimationFrame(()=>resolve(performance.now()-start)));
                })"""
            )
            transition_ms.append(float(transition))
    return {"gaps": gaps, "ids": ids, "transitionMs": transition_ms}


def performance_run(
    browser: Browser,
    engine: str,
    url: str,
    page_size: int,
    width: int,
    height: int,
    run_number: int,
    scored: bool,
) -> dict[str, Any]:
    context = make_context(browser, engine, width, height)
    page = context.new_page()
    page_errors: list[str] = []
    page.on("pageerror", lambda exc: page_errors.append(str(exc)))
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        wait_ready(page)
        settle_two_frames(page)
        page.reload(wait_until="domcontentloaded", timeout=60000)
        wait_ready(page)
        settle_two_frames(page)
        observer = init_runtime_observers(page)
        sort_ms = measure_sort(page)
        # Return to inventory number before traversal.
        page.locator("#inventory-sort").select_option("nr")
        settle_two_frames(page)
        page.evaluate("() => { window.__p11LongTasks = []; return true; }")
        expected_pages = math.ceil(EXPECTED_PRODUCTS / page_size)
        sc = measure_scroll(page, expected_pages)
        long_tasks = page.evaluate("() => window.__p11LongTasks || []")
        runtime_errors = page.evaluate("() => window.__p11Errors || []")
        unique_ids = sorted(set(sc["ids"]))
        inv_data = page.evaluate("async () => await (await fetch('./data/inventory.json')).json()")
        expected_ids = [str(r["id"]) for r in inv_data["items"]]
        result = {
            "engine": engine,
            "pageSize": page_size,
            "viewportCssPx": [width, height],
            "run": run_number,
            "scored": scored,
            "sortNameMs": sort_ms,
            "scrollFrameGapP95Ms": percentile_linear(sc["gaps"], 0.95),
            "scrollFrameGapMaxMs": max(sc["gaps"], default=0.0),
            "frameSamples": len(sc["gaps"]),
            "coveredProducts": len(unique_ids),
            "renderedProductInstances": len(sc["ids"]),
            "paginationTransitionP95Ms": percentile_linear(sc["transitionMs"], 0.95),
            "paginationTransitionTotalMs": sum(sc["transitionMs"]),
            "longTaskSupported": bool(observer.get("longTaskSupported")),
            "scrollLongTaskCount": len(long_tasks),
            "scrollLongTaskMaxMs": max([float(x["duration"]) for x in long_tasks], default=0.0),
            "scrollLongTaskTotalMs": sum(float(x["duration"]) for x in long_tasks),
            "runtimeErrors": runtime_errors,
            "pageErrors": page_errors,
            "uniqueProductIds": unique_ids,
            "idsExactMatch": sc["ids"] == expected_ids,
        }
        hard = (
            result["frameSamples"] == EXPECTED_PRODUCTS
            and result["coveredProducts"] == EXPECTED_PRODUCTS
            and result["renderedProductInstances"] == EXPECTED_PRODUCTS
            and result["idsExactMatch"]
            and result["scrollFrameGapP95Ms"] <= BUDGETS["scroll_frame_gap_p95_ms"]
            and result["scrollFrameGapMaxMs"] <= BUDGETS["scroll_frame_gap_max_ms"]
            and not runtime_errors
            and not page_errors
        )
        if engine == "chromium" and result["longTaskSupported"]:
            hard = hard and result["scrollLongTaskMaxMs"] <= BUDGETS["scroll_long_task_max_ms"] and result["scrollLongTaskTotalMs"] <= BUDGETS["scroll_long_task_total_ms"]
        result["runPass"] = hard
        return result
    finally:
        context.close()


def performance_profile(
    browser: Browser,
    engine: str,
    url: str,
    page_size: int,
    width: int,
    height: int,
    scored_runs: int,
    out: Path,
) -> dict[str, Any]:
    out.mkdir(parents=True, exist_ok=True)
    warm = performance_run(browser, engine, url, page_size, width, height, 0, False)
    (out / "run-0-warmup.json").write_text(json.dumps(warm, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    runs = []
    for i in range(1, scored_runs + 1):
        r = performance_run(browser, engine, url, page_size, width, height, i, True)
        runs.append(r)
        (out / f"run-{i}.json").write_text(json.dumps(r, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    sort_values = [float(r["sortNameMs"]) for r in runs]
    pooled_gaps = []
    for r in runs:
        # Pooled approximation is rebuilt from run P95 only unavailable raw gaps; scoring uses worst-run metrics.
        pooled_gaps.append(float(r["scrollFrameGapP95Ms"]))
    summary = {
        "engine": engine,
        "pageSize": page_size,
        "viewportCssPx": [width, height],
        "scoredRuns": scored_runs,
        "status": "PASS" if percentile_linear(sort_values, 0.95) <= BUDGETS["sort_p95_ms"] and all(r["runPass"] for r in runs) else "FAIL",
        "sortNameValuesMs": sort_values,
        "sortNameP95Ms": percentile_linear(sort_values, 0.95),
        "worstRunScrollFrameGapP95Ms": max(float(r["scrollFrameGapP95Ms"]) for r in runs),
        "worstScrollFrameGapMaxMs": max(float(r["scrollFrameGapMaxMs"]) for r in runs),
        "worstScrollLongTaskMaxMs": max(float(r["scrollLongTaskMaxMs"]) for r in runs),
        "worstScrollLongTaskTotalMs": max(float(r["scrollLongTaskTotalMs"]) for r in runs),
        "paginationTransitionP95WorstRunMs": max(float(r["paginationTransitionP95Ms"]) for r in runs),
        "paginationTransitionTotalWorstRunMs": max(float(r["paginationTransitionTotalMs"]) for r in runs),
        "all142ProductsCovered": all(r["coveredProducts"] == EXPECTED_PRODUCTS and r["renderedProductInstances"] == EXPECTED_PRODUCTS and r["idsExactMatch"] for r in runs),
        "allFrameSamples142": all(r["frameSamples"] == EXPECTED_PRODUCTS for r in runs),
        "allRunsNoRuntimeErrors": all(not r["runtimeErrors"] and not r["pageErrors"] for r in runs),
        "longTaskSupportedAllRuns": all(bool(r["longTaskSupported"]) for r in runs),
        "runPassCount": sum(1 for r in runs if r["runPass"]),
        "runFailCount": sum(1 for r in runs if not r["runPass"]),
        "budgets": BUDGETS,
    }
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


def write_manifest(root: Path) -> None:
    rows = []
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.name != "99_MANIFEST.csv":
            rows.append({
                "path": p.relative_to(root).as_posix(),
                "bytes": p.stat().st_size,
                "sha256": sha256_file(p),
            })
    with (root / "99_MANIFEST.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["path", "bytes", "sha256"])
        w.writeheader(); w.writerows(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-site", required=True)
    ap.add_argument("--variants-root", required=True)
    ap.add_argument("--base-url-root", default="http://127.0.0.1:4173/variants")
    ap.add_argument("--output", required=True)
    ap.add_argument("--prepare-only", action="store_true")
    args = ap.parse_args()

    base = Path(args.base_site).resolve()
    variants = Path(args.variants_root).resolve()
    out = Path(args.output).resolve()
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    variant_sources = build_variants(base, variants)
    source_doc = {
        "status": "PASS",
        "baselineTree": EXPECTED_BASE_SITE_TREE_SHA256,
        "expectedBranchZipSha256": EXPECTED_BRANCH_ZIP_SHA256,
        "variants": variant_sources,
        "pageSize20Label": "TEMPORAER_NICHT_PRODUKTENTSCHEIDEND",
    }
    (out / "00_source_lock_and_variants.json").write_text(json.dumps(source_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if args.prepare_only:
        write_manifest(out)
        print(json.dumps(source_doc, ensure_ascii=False, indent=2))
        return 0

    with sync_playwright() as pw:
        chromium = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        webkit = pw.webkit.launch(headless=True)
        browsers = {"chromium": chromium, "webkit": webkit}
        try:
            env = {
                "utcStarted": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "platform": platform.platform(),
                "python": sys.version,
                "playwrightPackage": importlib.metadata.version("playwright"),
                "chromiumVersion": chromium.version,
                "webkitVersion": webkit.version,
                "runnerImage": os.environ.get("ImageOS", ""),
                "runnerImageVersion": os.environ.get("ImageVersion", ""),
                "mobileFirst": True,
                "desktopDecisionMatrix": False,
                "desktopPolicy": "finalist-only smoke test later",
                "deviceProfiles": DEVICE_PROFILES,
                "mobileImagePolicy": "out of page-size decision; no offline-image-package requirement; 154 existing assets remain source-protected",
                "realIphoneFinalistCheckRequired": True,
                "budgets": BUDGETS,
            }
            (out / "01_environment_and_method.json").write_text(json.dumps(env, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

            functional_results = []
            perf_summaries = []

            for size in PAGE_SIZES:
                url = f"{args.base_url_root}/page_size_{size}/#inventory"
                for profile in DEVICE_PROFILES:
                    engine = profile["engine"]
                    browser = browsers[engine]
                    width = int(profile["width"])
                    height = int(profile["height"])
                    name = profile["name"]
                    print(f"FUNCTIONAL profile={name} engine={engine} pageSize={size} viewport={width}x{height}", flush=True)
                    r = functional_mobile_case(browser, engine, url, size, width, height)
                    r["deviceProfile"] = name
                    functional_results.append(r)
                    (out / f"functional_{name}_ps{size}.json").write_text(json.dumps(r, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

                    print(f"PERFORMANCE profile={name} engine={engine} pageSize={size} viewport={width}x{height}", flush=True)
                    ps = performance_profile(
                        browser, engine, url, size, width, height, int(profile["scoredRuns"]),
                        out / f"performance_{name}_ps{size}"
                    )
                    ps["deviceProfile"] = name
                    perf_summaries.append(ps)

            matrix = []
            for size in PAGE_SIZES:
                fun = [r for r in functional_results if r["pageSize"] == size]
                perf = [r for r in perf_summaries if r["pageSize"] == size]
                by_fun = {r["deviceProfile"]: r for r in fun}
                by_perf = {r["deviceProfile"]: r for r in perf}
                s_fun = by_fun["galaxy_s25_s26"]
                i_fun = by_fun["iphone_17_pro_max"]
                s_perf = by_perf["galaxy_s25_s26"]
                i_perf = by_perf["iphone_17_pro_max"]
                row = {
                    "pageSize": size,
                    "pages": math.ceil(EXPECTED_PRODUCTS / size),
                    "paginationActionsFullTraversal": math.ceil(EXPECTED_PRODUCTS / size) - 1,
                    "fullPageDetailsTabStops": size,
                    "samsung360Functional": s_fun["status"],
                    "samsung360FirstPageListHeightPx": round(float(s_fun["detail"]["geometry"]["listHeight"]), 3),
                    "samsung360FirstPageListScreens": round(float(s_fun["detail"]["geometry"]["listScreens"]), 3),
                    "samsung360VisibleRowsAtListTop": int(s_fun["detail"]["geometry"]["visibleRowsAtListTop"]),
                    "samsung360SortP95Ms": s_perf["sortNameP95Ms"],
                    "samsung360ScrollP95WorstMs": s_perf["worstRunScrollFrameGapP95Ms"],
                    "samsung360ScrollMaxWorstMs": s_perf["worstScrollFrameGapMaxMs"],
                    "samsung360LongTaskMaxWorstMs": s_perf["worstScrollLongTaskMaxMs"],
                    "samsung360PaginationTransitionTotalWorstRunMs": s_perf["paginationTransitionTotalWorstRunMs"],
                    "iphone440Functional": i_fun["status"],
                    "iphone440FirstPageListHeightPx": round(float(i_fun["detail"]["geometry"]["listHeight"]), 3),
                    "iphone440FirstPageListScreens": round(float(i_fun["detail"]["geometry"]["listScreens"]), 3),
                    "iphone440VisibleRowsAtListTop": int(i_fun["detail"]["geometry"]["visibleRowsAtListTop"]),
                    "iphone440SortP95Ms": i_perf["sortNameP95Ms"],
                    "iphone440ScrollP95WorstMs": i_perf["worstRunScrollFrameGapP95Ms"],
                    "iphone440ScrollMaxWorstMs": i_perf["worstScrollFrameGapMaxMs"],
                    "iphone440PaginationTransitionTotalWorstRunMs": i_perf["paginationTransitionTotalWorstRunMs"],
                    "allAutomatedHardGatesPass": all(r["status"] == "PASS" for r in fun) and all(r["status"] == "PASS" for r in perf),
                    "realIphoneFinalistCheckStillRequired": True,
                }
                matrix.append(row)

            with (out / "02_mobile_cross_variant_matrix.csv").open("w", encoding="utf-8", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(matrix[0].keys()))
                w.writeheader(); w.writerows(matrix)

            all_hard = all(r["allAutomatedHardGatesPass"] for r in matrix)
            summary = {
                "status": "MOBILE_COMPARISON_COMPLETE_NO_PRODUCT_DECISION" if all_hard else "MOBILE_COMPARISON_HAS_HARD_GATE_FAILURE",
                "mobileFirst": True,
                "deviceFocus": ["Samsung Galaxy S25/S26 class at 360x780 CSS px / Chromium", "iPhone 17 Pro Max at 440x956 CSS px / WebKit proxy"],
                "pageSizes": PAGE_SIZES,
                "baselinePageSize": 20,
                "baselineProductDecision": False,
                "pageSize20Label": "TEMPORAER_NICHT_PRODUKTENTSCHEIDEND",
                "desktopComparison": "not part of page-size decision; finalist-only smoke test later",
                "mobileImagesOfflinePackage": "explicitly excluded from mobile page-size product decision; existing 154 assets source-protected only",
                "allAutomatedHardGatesPass": all_hard,
                "realIphoneFinalistCheckRequired": True,
                "matrix": matrix,
                "publicRelease": False,
                "utcCompleted": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            (out / "03_mobile_comparison_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

            report = [
                "# Punkt 11 – fokussierter Mobile-First-Seitengrößenvergleich – privater Realmesslauf",
                "",
                f"Status: `{summary['status']}`",
                "",
                "PAGE_SIZE 20 bleibt TEMPORAER_NICHT_PRODUKTENTSCHEIDEND.",
                "Entscheidungsgeräte: Samsung S25/S26-Klasse 360x780 CSS px (Chromium) und iPhone 17 Pro Max 440x956 CSS px (WebKit-Näherung).",
                "320/390/412 und ein umfangreicher Desktop-Vergleich sind bewusst aus diesem Block entfernt.",
                "Mobile Produktbilder und das 154-Bilder-Offlinepaket sind bewusst kein Produktkriterium dieses Seitengrößenvergleichs; die bestehenden Assets bleiben unverändert geschützt.",
                "",
                "## Automatisierter Umfang",
                "- PAGE_SIZE 10 / 15 / 20 / 25 / 30",
                "- Samsung-Profil: Chromium 360x780 CSS px",
                "- iPhone-17-Pro-Max-Profil: WebKit 440x956 CSS px",
                "- Suche, Filter, Sortierung, Pagination/Fokus, Dialog/Fokusrückgabe, Fokusfalle, Live-Region-Semantik",
                "- kein horizontaler Produkt-Overflow, Option-B-Touchziele >= 44x44 CSS px",
                "- 0 automatische Produktbildrequests in der Übersicht; keine mobile Bild-/Offlinebild-Funktionsanforderung",
                "- Performance: 1 Warmup + 5 Wertungsläufe je Gerät/Seitengröße",
                "- exakt 142 Produkte / IDs / Frame-Samples pro Performance-Lauf",
                "",
                "## Varianten",
            ]
            for r in matrix:
                report.append(
                    f"- PAGE_SIZE {r['pageSize']}: {r['pages']} Seiten / {r['paginationActionsFullTraversal']} Seitenwechsel; "
                    f"Samsung Funktion {r['samsung360Functional']}, Sort-P95 {r['samsung360SortP95Ms']:.3f} ms, Scroll-P95 {r['samsung360ScrollP95WorstMs']:.3f} ms, "
                    f"iPhone-WebKit Funktion {r['iphone440Functional']}, Sort-P95 {r['iphone440SortP95Ms']:.3f} ms, Scroll-P95 {r['iphone440ScrollP95WorstMs']:.3f} ms"
                )
            report += [
                "",
                "## Danach",
                "- Messwerte auf maximal einen Finalisten reduzieren; nur bei echtem Gleichstand maximal zwei.",
                "- kurzer echter iPhone-17-Pro-Max-Safari-Finalcheck.",
                "- kurzer Desktop-Smoke-Test des Gewinners.",
                "- keine öffentliche Freigabe in diesem Block.",
            ]
            (out / "04_mobile_report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
            write_manifest(out)
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return_code = 0 if all_hard else 3
        finally:
            webkit.close()
            chromium.close()
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
