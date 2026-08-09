#!/usr/bin/env python3
"""Read-only Chromium/ChromeDriver performance harness for Hausbar Option B.

Measures only the already-sealed site served from localhost. It never writes into
_site. Outputs go to the directory given with --output.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

EXPECTED_BRANCH_ZIP_SHA256 = "87f406d725dbb4f807991fdbe4adaaf79a408a6629e5927c7e19743dbd0e04ab"
EXPECTED_SITE_TREE_SHA256 = "96869e90ec87d79a389c91a71d150d6276bbe0675601c27d1703f7e2f6026980"
EXPECTED_SITE_FILES = 178
EXPECTED_IMAGES = 154
EXPECTED_PRODUCTS = 142
EXPECTED_PAGES = 8
PAGE_SIZE = 20
SCORED_RUNS = 5
SCROLL_STEPS_PER_PAGE = 17
VIEWPORT_WIDTH = 1440
VIEWPORT_HEIGHT = 1200

BUDGETS = {
    "sort_p95_ms": 150.0,
    "scroll_frame_gap_p95_ms": 50.0,
    "scroll_frame_gap_max_ms": 150.0,
    "scroll_long_task_max_ms": 250.0,
    "scroll_long_task_total_ms": 500.0,
}


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
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(digest.encode("ascii"))
        h.update(b"\0")
        h.update(str(size).encode("ascii"))
        h.update(b"\n")
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


def request_json(method: str, url: str, payload: dict[str, Any] | None = None, timeout: float = 120.0) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json; charset=utf-8")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        raise RuntimeError(f"WebDriver HTTP {exc.code}: {raw.decode('utf-8', errors='replace')}") from exc
    body = json.loads(raw.decode("utf-8")) if raw else {}
    value = body.get("value")
    if isinstance(value, dict) and value.get("error"):
        raise RuntimeError(f"WebDriver error: {json.dumps(value, ensure_ascii=False)}")
    return body


class WebDriver:
    def __init__(self, base: str, chrome_binary: str):
        self.base = base.rstrip("/")
        self.session_id = ""
        payload = {
            "capabilities": {
                "alwaysMatch": {
                    "browserName": "chrome",
                    "pageLoadStrategy": "normal",
                    "goog:chromeOptions": {
                        "binary": chrome_binary,
                        "args": [
                            "--headless=new",
                            "--no-sandbox",
                            "--disable-dev-shm-usage",
                            f"--window-size={VIEWPORT_WIDTH},{VIEWPORT_HEIGHT}",
                        ],
                    },
                }
            }
        }
        body = request_json("POST", f"{self.base}/session", payload)
        value = body.get("value", {})
        self.session_id = value.get("sessionId") or body.get("sessionId") or ""
        if not self.session_id:
            raise RuntimeError(f"No WebDriver session id in response: {body}")
        self.command("POST", "/timeouts", {"script": 120000, "pageLoad": 120000, "implicit": 0})
        # Force exact CSS viewport independent of headless window decorations.
        self.cdp(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": VIEWPORT_WIDTH,
                "height": VIEWPORT_HEIGHT,
                "deviceScaleFactor": 1,
                "mobile": False,
            },
        )

    def command(self, method: str, path: str, payload: dict[str, Any] | None = None, timeout: float = 120.0) -> Any:
        body = request_json(method, f"{self.base}/session/{self.session_id}{path}", payload, timeout=timeout)
        return body.get("value")

    def cdp(self, cmd: str, params: dict[str, Any]) -> Any:
        return self.command("POST", "/goog/cdp/execute", {"cmd": cmd, "params": params})

    def navigate(self, url: str) -> None:
        self.command("POST", "/url", {"url": url})

    def execute(self, script: str, args: list[Any] | None = None) -> Any:
        return self.command("POST", "/execute/sync", {"script": script, "args": args or []})

    def execute_async(self, script: str, args: list[Any] | None = None, timeout: float = 120.0) -> Any:
        return self.command("POST", "/execute/async", {"script": script, "args": args or []}, timeout=timeout)

    def close(self) -> None:
        if self.session_id:
            try:
                request_json("DELETE", f"{self.base}/session/{self.session_id}", timeout=30)
            except Exception:
                pass
            self.session_id = ""


def wait_ready(wd: WebDriver, timeout_s: float = 30.0) -> None:
    deadline = time.time() + timeout_s
    last: Any = None
    while time.time() < deadline:
        try:
            last = wd.execute(
                "return {ready: document.documentElement.dataset.ready || '', inventoryReady: document.documentElement.dataset.inventoryReady || '', busy: document.querySelector('#inventory-list')?.getAttribute('aria-busy'), count: document.querySelectorAll('#inventory-list > li').length, error: document.documentElement.dataset.ready === 'error'};"
            )
            if last and last.get("ready") == "true" and last.get("inventoryReady") == "true" and last.get("busy") == "false" and last.get("count", 0) > 0:
                return
        except Exception:
            pass
        time.sleep(0.1)
    raise RuntimeError(f"App did not become ready: {last}")


def settle_two_frames(wd: WebDriver) -> None:
    result = wd.execute_async(
        """
        const done = arguments[arguments.length - 1];
        requestAnimationFrame(() => requestAnimationFrame(() => done(true)));
        """
    )
    if result is not True:
        raise RuntimeError("Two-frame settle failed")


def init_runtime_observers(wd: WebDriver) -> None:
    wd.execute(
        """
        window.__p11Errors = [];
        window.addEventListener('error', (e) => window.__p11Errors.push(String(e.message || e.error || 'window error')));
        window.addEventListener('unhandledrejection', (e) => window.__p11Errors.push(String(e.reason || 'unhandled rejection')));
        window.__p11LongTasks = [];
        if (window.__p11LongTaskObserver) { try { window.__p11LongTaskObserver.disconnect(); } catch {} }
        try {
          window.__p11LongTaskObserver = new PerformanceObserver((list) => {
            for (const entry of list.getEntries()) {
              window.__p11LongTasks.push({startTime: entry.startTime, duration: entry.duration});
            }
          });
          window.__p11LongTaskObserver.observe({entryTypes: ['longtask']});
        } catch (e) {
          window.__p11Errors.push('LongTask observer unavailable: ' + String(e));
        }
        return true;
        """
    )


def measure_sort_name(wd: WebDriver) -> dict[str, Any]:
    result = wd.execute_async(
        """
        const done = arguments[arguments.length - 1];
        (async () => {
          const select = document.querySelector('#inventory-sort');
          if (!select) throw new Error('inventory-sort missing');
          select.value = 'nr';
          select.dispatchEvent(new Event('change', {bubbles: true}));
          await new Promise(requestAnimationFrame);
          await new Promise(requestAnimationFrame);
          performance.clearMeasures('hausbar-inventory-render');
          performance.clearMarks('hausbar-inventory-render-start');
          performance.clearMarks('hausbar-inventory-render-ready');
          const start = performance.now();
          select.value = 'name';
          select.dispatchEvent(new Event('change', {bubbles: true}));
          await new Promise(requestAnimationFrame);
          await new Promise(requestAnimationFrame);
          // Force final layout read after at least one paint opportunity.
          const height = document.documentElement.getBoundingClientRect().height;
          const duration = performance.now() - start;
          const names = [...document.querySelectorAll('#inventory-list .product-row__title')].map((n) => n.textContent.trim());
          const renderEntries = performance.getEntriesByName('hausbar-inventory-render');
          const internalRender = renderEntries.length ? renderEntries[renderEntries.length - 1].duration : null;
          done({durationMs: duration, internalRenderMs: internalRender, firstVisibleName: names[0] || '', visibleCount: names.length, height});
        })().catch((e) => done({__error: String(e && e.stack || e)}));
        """
    )
    if isinstance(result, dict) and result.get("__error"):
        raise RuntimeError(result["__error"])
    return result


def reset_for_scroll(wd: WebDriver, url: str) -> None:
    # Reload the already-warm profile to isolate scrolling from the sort action.
    wd.navigate(url)
    wait_ready(wd)
    settle_two_frames(wd)
    init_runtime_observers(wd)
    wd.execute("document.scrollingElement.scrollTop = 0; return document.scrollingElement.scrollTop;")
    settle_two_frames(wd)


def measure_one_page_scroll(wd: WebDriver, steps: int = SCROLL_STEPS_PER_PAGE) -> dict[str, Any]:
    result = wd.execute_async(
        """
        const steps = arguments[0];
        const done = arguments[arguments.length - 1];
        (async () => {
          const scroller = document.scrollingElement;
          if (!scroller) throw new Error('document.scrollingElement missing');
          scroller.style.scrollBehavior = 'auto';
          scroller.scrollTop = 0;
          await new Promise(requestAnimationFrame);
          await new Promise(requestAnimationFrame);
          const maxScroll = Math.max(0, scroller.scrollHeight - scroller.clientHeight);
          const startTime = performance.now();
          const longStart = (window.__p11LongTasks || []).length;
          let previous = performance.now();
          const gaps = [];
          for (let i = 1; i <= steps; i += 1) {
            scroller.scrollTop = maxScroll * (i / steps);
            const stamp = await new Promise((resolve) => requestAnimationFrame(resolve));
            gaps.push(stamp - previous);
            previous = stamp;
          }
          await new Promise(requestAnimationFrame);
          // Force final geometry read after the final frame.
          const finalTop = scroller.scrollTop;
          const endTime = performance.now();
          const longTasks = (window.__p11LongTasks || []).slice(longStart).filter((e) => e.startTime >= startTime && e.startTime <= endTime + 1);
          const ids = [...document.querySelectorAll('#inventory-list > li[data-product-id]')].map((n) => n.dataset.productId);
          done({
            gaps,
            maxScroll,
            finalTop,
            fullPageScroll: maxScroll === 0 ? true : Math.abs(finalTop - maxScroll) <= 2,
            longTasks,
            ids,
            status: document.querySelector('#inventory-page-status')?.textContent || '',
            viewport: {width: innerWidth, height: innerHeight, devicePixelRatio},
            scrollHeight: scroller.scrollHeight,
            clientHeight: scroller.clientHeight,
          });
        })().catch((e) => done({__error: String(e && e.stack || e)}));
        """,
        [steps],
    )
    if isinstance(result, dict) and result.get("__error"):
        raise RuntimeError(result["__error"])
    return result


def next_page(wd: WebDriver) -> dict[str, Any]:
    result = wd.execute_async(
        """
        const done = arguments[arguments.length - 1];
        (async () => {
          const button = document.querySelector('#inventory-page-next');
          if (!button || button.disabled) throw new Error('Next page unavailable');
          const before = document.querySelector('#inventory-page-status')?.textContent || '';
          button.click();
          await new Promise(requestAnimationFrame);
          await new Promise(requestAnimationFrame);
          document.scrollingElement.scrollTop = 0;
          await new Promise(requestAnimationFrame);
          const after = document.querySelector('#inventory-page-status')?.textContent || '';
          done({before, after, visible: document.querySelectorAll('#inventory-list > li').length});
        })().catch((e) => done({__error: String(e && e.stack || e)}));
        """
    )
    if isinstance(result, dict) and result.get("__error"):
        raise RuntimeError(result["__error"])
    return result


def run_measurement(base_url: str, wd_base: str, chrome_binary: str, run_number: int, scored: bool) -> dict[str, Any]:
    wd = WebDriver(wd_base, chrome_binary)
    try:
        browser_version = wd.command("GET", "/") if False else None
        # Cold load establishes Service Worker/cache for this fresh profile.
        wd.navigate(base_url)
        wait_ready(wd)
        settle_two_frames(wd)
        # Scored warm load.
        wd.navigate(base_url)
        wait_ready(wd)
        settle_two_frames(wd)
        init_runtime_observers(wd)

        viewport = wd.execute("return {width: innerWidth, height: innerHeight, dpr: devicePixelRatio, ua: navigator.userAgent};")
        inventory_meta = wd.execute(
            "return {countText: document.querySelector('#result-count')?.textContent || '', pageStatus: document.querySelector('#inventory-page-status')?.textContent || '', visible: document.querySelectorAll('#inventory-list > li').length};"
        )
        sort = measure_sort_name(wd)

        reset_for_scroll(wd, base_url)
        all_gaps: list[float] = []
        all_long_tasks: list[dict[str, float]] = []
        all_ids: list[str] = []
        page_results: list[dict[str, Any]] = []
        full_pages = True

        for page_number in range(1, EXPECTED_PAGES + 1):
            page = measure_one_page_scroll(wd)
            page["page"] = page_number
            page["frameGapP95Ms"] = percentile_linear([float(x) for x in page["gaps"]], 0.95)
            page["frameGapMaxMs"] = max([float(x) for x in page["gaps"]], default=0.0)
            page["longTaskMaxMs"] = max([float(x["duration"]) for x in page["longTasks"]], default=0.0)
            page["longTaskTotalMs"] = sum(float(x["duration"]) for x in page["longTasks"])
            page_results.append(page)
            all_gaps.extend(float(x) for x in page["gaps"])
            all_long_tasks.extend(page["longTasks"])
            all_ids.extend(page["ids"])
            full_pages = full_pages and bool(page["fullPageScroll"])
            if page_number < EXPECTED_PAGES:
                next_page(wd)

        unique_ids = sorted(set(all_ids))
        final_status = wd.execute("return document.querySelector('#inventory-page-status')?.textContent || '';")
        runtime_errors = wd.execute("return window.__p11Errors || [];") or []

        result = {
            "run": run_number,
            "scored": scored,
            "browserUserAgent": viewport.get("ua"),
            "viewport": {"width": viewport.get("width"), "height": viewport.get("height"), "dpr": viewport.get("dpr")},
            "initialInventory": inventory_meta,
            "sortNameMs": float(sort["durationMs"]),
            "sortInternalRenderMs": None if sort.get("internalRenderMs") is None else float(sort["internalRenderMs"]),
            "sortFirstVisibleName": sort.get("firstVisibleName"),
            "scrollFrameGapP95Ms": percentile_linear(all_gaps, 0.95),
            "scrollFrameGapMaxMs": max(all_gaps, default=0.0),
            "scrollLongTaskMaxMs": max([float(x["duration"]) for x in all_long_tasks], default=0.0),
            "scrollLongTaskTotalMs": sum(float(x["duration"]) for x in all_long_tasks),
            "scrollLongTaskCount": len(all_long_tasks),
            "fullScroll": full_pages,
            "frameSamples": len(all_gaps),
            "coveredProducts": len(unique_ids),
            "renderedProductInstances": len(all_ids),
            "uniqueProductIds": unique_ids,
            "finalPageStatus": final_status,
            "runtimeErrors": runtime_errors,
            "pages": page_results,
        }
        result["runPass"] = (
            result["fullScroll"]
            and result["coveredProducts"] == EXPECTED_PRODUCTS
            and result["frameSamples"] == EXPECTED_PAGES * SCROLL_STEPS_PER_PAGE
            and result["scrollFrameGapP95Ms"] <= BUDGETS["scroll_frame_gap_p95_ms"]
            and result["scrollFrameGapMaxMs"] <= BUDGETS["scroll_frame_gap_max_ms"]
            and result["scrollLongTaskMaxMs"] <= BUDGETS["scroll_long_task_max_ms"]
            and result["scrollLongTaskTotalMs"] <= BUDGETS["scroll_long_task_total_ms"]
            and not result["runtimeErrors"]
        )
        return result
    finally:
        wd.close()


def write_csv(path: Path, runs: list[dict[str, Any]]) -> None:
    fields = [
        "run",
        "scored",
        "sortNameMs",
        "sortInternalRenderMs",
        "scrollFrameGapP95Ms",
        "scrollFrameGapMaxMs",
        "scrollLongTaskMaxMs",
        "scrollLongTaskTotalMs",
        "scrollLongTaskCount",
        "fullScroll",
        "frameSamples",
        "coveredProducts",
        "renderedProductInstances",
        "runtimeErrorCount",
        "runPass",
    ]
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for run in runs:
            row = {k: run.get(k) for k in fields if k != "runtimeErrorCount"}
            row["runtimeErrorCount"] = len(run.get("runtimeErrors", []))
            writer.writerow(row)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:4173/#inventory")
    parser.add_argument("--webdriver", default="http://127.0.0.1:9515")
    parser.add_argument("--chrome-binary", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    site = Path(args.site).resolve()
    out = Path(args.output).resolve()
    out.mkdir(parents=True, exist_ok=True)

    tree_hash, file_count = canonical_site_tree(site)
    image_dir = site / "assets" / "images" / "inventory"
    image_count = sum(1 for p in image_dir.rglob("*") if p.is_file()) if image_dir.exists() else 0
    source_lock = {
        "expectedBranchZipSha256": EXPECTED_BRANCH_ZIP_SHA256,
        "expectedSiteTreeSha256": EXPECTED_SITE_TREE_SHA256,
        "actualSiteTreeSha256": tree_hash,
        "expectedSiteFiles": EXPECTED_SITE_FILES,
        "actualSiteFiles": file_count,
        "expectedImages": EXPECTED_IMAGES,
        "actualImages": image_count,
        "pass": tree_hash == EXPECTED_SITE_TREE_SHA256 and file_count == EXPECTED_SITE_FILES and image_count == EXPECTED_IMAGES,
    }
    (out / "00_source_lock.json").write_text(json.dumps(source_lock, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if not source_lock["pass"]:
        print(json.dumps({"sourceLock": source_lock}, ensure_ascii=False, indent=2))
        return 2

    metadata = {
        "utcStarted": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "platform": platform.platform(),
        "python": sys.version,
        "chromeBinary": args.chrome_binary,
        "chromeVersion": subprocess.check_output([args.chrome_binary, "--version"], text=True).strip(),
        "chromedriverVersion": subprocess.check_output([shutil.which("chromedriver") or "chromedriver", "--version"], text=True).strip(),
        "runnerImage": os.environ.get("ImageOS", ""),
        "runnerImageVersion": os.environ.get("ImageVersion", ""),
        "githubSha": os.environ.get("GITHUB_SHA", ""),
        "measurementProfile": {
            "name": "OPTION_B_DESKTOP_READ_ONLY",
            "viewportCssPx": [VIEWPORT_WIDTH, VIEWPORT_HEIGHT],
            "deviceScaleFactor": 1,
            "cpuThrottling": "none",
            "network": "localhost / native",
            "warmupRuns": 1,
            "scoredRuns": SCORED_RUNS,
            "freshBrowserProfilePerRun": True,
            "coldLoadThenWarmLoadPerRun": True,
            "scrollElement": "document.scrollingElement",
            "pages": EXPECTED_PAGES,
            "scrollStepsPerPage": SCROLL_STEPS_PER_PAGE,
            "expectedFrameSamplesPerRun": EXPECTED_PAGES * SCROLL_STEPS_PER_PAGE,
            "outlierRemoval": False,
            "performanceObserver": "longtask",
            "sortAction": "#inventory-sort nr -> name, two requestAnimationFrame boundaries and final layout read",
        },
        "budgets": BUDGETS,
    }
    (out / "01_environment_and_method.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    all_runs: list[dict[str, Any]] = []
    # One unscored warmup run on a fresh profile, then five scored fresh-profile runs.
    print("P11 Option B performance: warmup run")
    warmup = run_measurement(args.base_url, args.webdriver, args.chrome_binary, 0, False)
    (out / "run-0-warmup.json").write_text(json.dumps(warmup, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    for i in range(1, SCORED_RUNS + 1):
        print(f"P11 Option B performance: scored run {i}/{SCORED_RUNS}")
        run = run_measurement(args.base_url, args.webdriver, args.chrome_binary, i, True)
        all_runs.append(run)
        (out / f"run-{i}.json").write_text(json.dumps(run, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({
            "run": i,
            "sortNameMs": round(run["sortNameMs"], 3),
            "scrollP95Ms": round(run["scrollFrameGapP95Ms"], 3),
            "scrollMaxMs": round(run["scrollFrameGapMaxMs"], 3),
            "longTaskMaxMs": round(run["scrollLongTaskMaxMs"], 3),
            "longTaskTotalMs": round(run["scrollLongTaskTotalMs"], 3),
            "fullScroll": run["fullScroll"],
            "coveredProducts": run["coveredProducts"],
            "errors": len(run["runtimeErrors"]),
            "runPass": run["runPass"],
        }))

    sort_values = [float(r["sortNameMs"]) for r in all_runs]
    sort_p95 = percentile_linear(sort_values, 0.95)
    pooled_gaps = [float(g) for r in all_runs for page in r["pages"] for g in page["gaps"]]
    summary = {
        "status": "OPTION_B_PERFORMANCE_PASS" if (
            sort_p95 <= BUDGETS["sort_p95_ms"]
            and all(bool(r["runPass"]) for r in all_runs)
        ) else "OPTION_B_PERFORMANCE_FAIL",
        "sourceLockPass": source_lock["pass"],
        "scoredRuns": SCORED_RUNS,
        "sortNameValuesMs": sort_values,
        "sortNameP95Ms": sort_p95,
        "sortBudgetMs": BUDGETS["sort_p95_ms"],
        "sortPass": sort_p95 <= BUDGETS["sort_p95_ms"],
        "pooledScrollFrameGapP95Ms": percentile_linear(pooled_gaps, 0.95),
        "worstRunScrollFrameGapP95Ms": max(float(r["scrollFrameGapP95Ms"]) for r in all_runs),
        "worstScrollFrameGapMaxMs": max(float(r["scrollFrameGapMaxMs"]) for r in all_runs),
        "worstScrollLongTaskMaxMs": max(float(r["scrollLongTaskMaxMs"]) for r in all_runs),
        "worstScrollLongTaskTotalMs": max(float(r["scrollLongTaskTotalMs"]) for r in all_runs),
        "allFullScroll": all(bool(r["fullScroll"]) for r in all_runs),
        "all142ProductsCovered": all(int(r["coveredProducts"]) == EXPECTED_PRODUCTS for r in all_runs),
        "allFrameSamplesComplete": all(int(r["frameSamples"]) == EXPECTED_PAGES * SCROLL_STEPS_PER_PAGE for r in all_runs),
        "allRunsNoRuntimeErrors": all(not r["runtimeErrors"] for r in all_runs),
        "runPassCount": sum(1 for r in all_runs if r["runPass"]),
        "runFailCount": sum(1 for r in all_runs if not r["runPass"]),
        "budgets": BUDGETS,
        "historicalVariant1ValuesIncludedInVerdict": False,
        "historicalVariant1Values": {
            "sortNameP95Ms": 129.8,
            "scrollFrameGapP95Ms": 83.3,
            "worstTotalLongTasksMs": 1303.0,
            "note": "Reference only; never used to derive Option-B verdict.",
        },
        "publicRelease": False,
        "utcCompleted": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    (out / "02_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_csv(out / "03_scored_runs.csv", all_runs)

    report = [
        "# Punkt 11 – Option B – Read-only Performance-Messreihe",
        "",
        f"Status: `{summary['status']}`",
        "",
        "## Source-Lock",
        f"- Site-Tree: `{tree_hash}` – PASS",
        f"- Dateien: {file_count}/{EXPECTED_SITE_FILES} – PASS",
        f"- Produktbilder: {image_count}/{EXPECTED_IMAGES} – PASS",
        f"- Gebundener Branch-ZIP-SHA-256: `{EXPECTED_BRANCH_ZIP_SHA256}`",
        "",
        "## Messprofil",
        f"- Desktop-Viewport: {VIEWPORT_WIDTH} × {VIEWPORT_HEIGHT} CSS px",
        "- CPU-Drosselung: keine",
        "- ein ungewerteter Warmup-Lauf + fünf Wertungsläufe",
        "- pro Lauf frisches Browserprofil; Kaltstart zur SW-/Cache-Etablierung, danach Warmstart",
        "- Scroll: document.scrollingElement; acht Seiten; 17 rAF-Schritte je Seite = 136 Frame-Samples je Lauf",
        "- keine Ausreißerentfernung",
        "",
        "## Option-B-Ergebnisse",
        f"- Sortierung Name P95: {summary['sortNameP95Ms']:.3f} ms (Budget ≤ {BUDGETS['sort_p95_ms']:.0f} ms) – {'PASS' if summary['sortPass'] else 'FAIL'}",
        f"- gepoolter Scroll-Frame-Gap-P95: {summary['pooledScrollFrameGapP95Ms']:.3f} ms",
        f"- schlechtester Lauf Scroll-Frame-Gap-P95: {summary['worstRunScrollFrameGapP95Ms']:.3f} ms (Budget je Lauf ≤ {BUDGETS['scroll_frame_gap_p95_ms']:.0f} ms)",
        f"- schlechtestes Scroll-Maximum: {summary['worstScrollFrameGapMaxMs']:.3f} ms (Budget ≤ {BUDGETS['scroll_frame_gap_max_ms']:.0f} ms)",
        f"- längster Scroll-Long-Task: {summary['worstScrollLongTaskMaxMs']:.3f} ms (Budget ≤ {BUDGETS['scroll_long_task_max_ms']:.0f} ms)",
        f"- höchste Scroll-Long-Task-Summe eines Laufs: {summary['worstScrollLongTaskTotalMs']:.3f} ms (Budget ≤ {BUDGETS['scroll_long_task_total_ms']:.0f} ms)",
        f"- vollständiger Scroll: {summary['allFullScroll']}",
        f"- 142/142 Produkte in jedem Lauf abgedeckt: {summary['all142ProductsCovered']}",
        f"- Runtime-Fehler: {'0 in allen Läufen' if summary['allRunsNoRuntimeErrors'] else 'vorhanden'}",
        "",
        "## Abgrenzung",
        "Die historischen Variante-1-Werte sind nur Referenz und wurden nicht zur Option-B-Wertung verwendet.",
        "Keine App-, Daten-, Bild- oder Evidence-Datei wurde durch die Messung verändert. Keine öffentliche Freigabe.",
    ]
    (out / "04_report.md").write_text("\n".join(report) + "\n", encoding="utf-8")

    # Hash all outputs except final manifest itself.
    manifest_rows = []
    for p in sorted(out.iterdir()):
        if p.is_file() and p.name != "99_manifest.csv":
            manifest_rows.append({"file": p.name, "bytes": p.stat().st_size, "sha256": sha256_file(p)})
    with (out / "99_manifest.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["file", "bytes", "sha256"])
        writer.writeheader()
        writer.writerows(manifest_rows)

    print("=== OPTION B PERFORMANCE SUMMARY ===")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["status"] == "OPTION_B_PERFORMANCE_PASS" else 3


if __name__ == "__main__":
    raise SystemExit(main())
