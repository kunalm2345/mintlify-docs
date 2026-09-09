#!/usr/bin/env python3
"""Check every inventoried Fern URL against a local or deployed Mintlify site."""

import argparse
import concurrent.futures
import json
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class Metadata(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = set()
        self.canonicals = []
        self.og_urls = []
        self.robots = []
        self.title = ""
        self.in_title = False

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if "id" in attrs:
            self.ids.add(attrs["id"])
        if tag == "link" and attrs.get("rel") == "canonical":
            self.canonicals.append(attrs.get("href", ""))
        if tag == "meta" and attrs.get("property") == "og:url":
            self.og_urls.append(attrs.get("content", ""))
        if tag == "meta" and attrs.get("name") == "robots":
            self.robots.append(attrs.get("content", ""))
        if tag == "title":
            self.in_title = True

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False

    def handle_data(self, text):
        if self.in_title:
            self.title += text


class Redirects(HTTPRedirectHandler):
    def __init__(self):
        self.hops = []

    def redirect_request(self, request, fp, code, message, headers, new_url):
        self.hops.append({"status": code, "from": request.full_url, "to": new_url})
        return super().redirect_request(request, fp, code, message, headers, new_url)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", required=True, help="For example http://localhost:3300 or https://www.agentmail.to")
    parser.add_argument("--base-path", default="", help="Use /docs for the production proxy")
    parser.add_argument("--local", action="store_true", help="Check local routes and anchors; report canonicals and static files as unverified")
    parser.add_argument("--indexable", action="store_true", help="Expect the launch indexing block to be removed")
    parser.add_argument("--legacy-hosts", action="store_true", help="Also request original hosts and malformed root sitemap URLs; use after routing is deployed")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--only", nargs="+", help="Check only these inventory paths, for example /google-workspace")
    parser.add_argument("--output", default="/tmp/agentmail-migration-audit.json")
    args = parser.parse_args()
    fixture = Path(__file__).parent / "fixtures/fern-migration.json"
    inventory = json.loads(fixture.read_text())
    if args.only:
        unknown = set(args.only) - {e["path"] for e in inventory["pages"]}
        if unknown:
            parser.error("Paths not in the inventory: " + ", ".join(sorted(unknown)))
    prefix = args.origin.rstrip("/") + args.base_path.rstrip("/")
    jobs = {}
    for entry in inventory["pages"]:
        if args.only and entry["path"] not in args.only:
            continue
        paths = {entry["path"]}
        # The origin proxy removes the configured base path, leaving the
        # extra /docs segment from Fern's malformed canonical as a route.
        for canonical in entry["legacy_canonicals"]:
            path = urlsplit(canonical).path
            if path.startswith("/docs/"):
                paths.add(path.removeprefix("/docs"))
        for path in paths:
            jobs[prefix + path] = entry
        if args.legacy_hosts:
            for url in entry["legacy_urls"] + entry["legacy_canonicals"]:
                jobs[url] = entry
    if args.legacy_hosts:
        by_path = {e["path"]: e for e in inventory["pages"]}
        for rule in inventory["website_redirects_required"]:
            if args.only and rule["source"] not in args.only:
                continue
            for host in ("https://agentmail.to", "https://www.agentmail.to"):
                jobs[host + rule["source"]] = by_path[rule["source"]]
        for rule in inventory.get("website_parameter_redirects_required", []):
            source = rule["source"].removeprefix("/docs")
            if args.only and source not in args.only:
                continue
            sample = re.sub(r":\w+", "migration-audit-example", rule["source"])
            jobs[args.origin.rstrip("/") + sample] = by_path[source]

    def check(job):
        url, entry = job
        result = {"url": url, "expected_path": entry["expected_path"], "expected_canonical": entry["expected_canonical"], "errors": []}
        if args.local and entry["kind"] == "asset":
            result["unverified"] = "Mintlify local preview does not serve specification downloads or generated RSS; verify on a hosted deployment"
            return result
        redirects = Redirects()
        try:
            with build_opener(redirects).open(Request(url, headers={"User-Agent": "AgentMail-Migration-Audit/1.0"}), timeout=45) as response:
                body = response.read().decode("utf-8", errors="replace")
                result.update(status=response.status, final_url=response.url, redirects=redirects.hops)
                content_type = response.headers.get("Content-Type", "")
                xrobots = response.headers.get("X-Robots-Tag", "")
            expected = urlsplit(entry["expected_path"])
            final = urlsplit(result["final_url"])
            final_base = args.base_path.rstrip("/") if not args.legacy_hosts else "/docs"
            if final.path.rstrip("/") != (final_base + expected.path).rstrip("/"):
                result["errors"].append("Redirect landed on a different page")
            if not args.local and any(h["status"] not in (301, 308) for h in redirects.hops):
                result["errors"].append("Redirect is not permanent")
            if entry["kind"] == "asset":
                if "html" in content_type:
                    result["errors"].append("Download returned HTML")
                if expected.path.endswith(".json"):
                    json.loads(body)
                return result
            if "text/html" not in content_type:
                result["errors"].append("Page did not return HTML")
            page = Metadata()
            page.feed(body)
            result.update(title=page.title, canonicals=page.canonicals, og_urls=page.og_urls)
            if re.search(r"page (?:not found|could not be found)", page.title, re.I):
                result["errors"].append("Page rendered a 404 title")
            if expected.fragment and expected.fragment not in page.ids:
                result["errors"].append("Missing destination anchor: " + expected.fragment)
            robots = ",".join(page.robots + [xrobots]).lower()
            if args.indexable:
                if "noindex" in robots or "nofollow" in robots:
                    result["errors"].append("Indexing remains blocked")
            elif "noindex" not in robots or "nofollow" not in robots:
                result["errors"].append("Pre-launch indexing block is missing")
            if args.local:
                result["unverified"] = "Production canonical and og:url require a hosted deployment"
            else:
                canonical = entry["expected_canonical"]
                if page.canonicals != [canonical]:
                    result["errors"].append("Canonical does not identify the final production page")
                if page.og_urls != [canonical]:
                    result["errors"].append("og:url does not identify the final production page")
        except (HTTPError, OSError, ValueError) as error:
            result["errors"].append(str(error))
        return result

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        for result in pool.map(check, sorted(jobs.items())):
            results.append(result)
            if len(results) % 50 == 0:
                print(f"Checked {len(results)}/{len(jobs)} URLs", flush=True)
    failures = [r for r in results if r["errors"]]
    skipped = sum("status" not in r and not r["errors"] for r in results)
    report = {"mode": "local-routes-only" if args.local else "hosted", "cases": len(results), "checked": len(results) - skipped, "skipped": skipped, "failed": len(failures), "results": results}
    Path(args.output).write_text(json.dumps(report, indent=2) + "\n")
    print(f"Checked {len(results) - skipped} URLs; {len(failures)} failures; {skipped} hosted-only checks skipped. Report: {args.output}")
    for result in failures:
        print(result["url"], "; ".join(result["errors"]))
    if args.local:
        print("Canonical tags, download serving, RSS, and production redirect status remain unverified in local mode.")
    raise SystemExit(bool(failures))


if __name__ == "__main__":
    main()
