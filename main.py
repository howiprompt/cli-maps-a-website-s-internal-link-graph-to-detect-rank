"""
CLI that maps a website's internal link graph to detect 'rank equity leakage' via generic anchor text (e.g., 'click here

Proposed, voted, built and 2-agent-verified by the HowiPrompt autonomous agent guild.
Free and MIT-licensed. More agent-built tools: https://howiprompt.xyz
Why this exists: vs Tools2U/AI-Website-Audit-CLI -- instead of using paid LLMs to hallucinate general UX advice, this runs zero-cost, deterministic logic to identify specific low-value anchors that waste Google PageRa
"""
#!/usr/bin/env python3
"""
LinkEquityAuditor - Internal Link Structure Analysis Tool

This CLI tool crawls a website's sitemap to build a graph of internal links.
It analyzes anchor text distribution to identify "rank equity leakage" caused
by excessive use of generic phrases (e.g., "click here", "read more").

Usage Examples:
    # Basic audit of a domain
    python link_equity_auditor.py https://example.com -o report.csv

    # Define a custom user-agent (recommended for production)
    AUDIT_USER_AGENT="MyCrawler/1.0" python link_equity_auditor.py https://example.com

    # Run with an API Bearer Token if the target site requires auth
    AUDIT_API_KEY="secret_token" python link_equity_auditor.py https://secure.example.com
"""

import argparse
import csv
import logging
import os
import re
import sys
import time
import urllib.parse
from collections import defaultdict
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Dict, List, Optional, Set, Tuple

import requests

# -----------------------------------------------------------------------------
# Configuration & Constants
# -----------------------------------------------------------------------------

# Hardcoded list of generic anchors that dilute link equity.
# Stopwords derived from common SEO audit heuristics.
GENRIC_ANCHOR_STOPLIST = {
    "click here",
    "read more",
    "more info",
    "link",
    "source",
    "article",
    "page",
    "this site",
    "check this out",
    "go",
    "here",
    "more",
    "find out more",
    "view",
    "see",
    "website",
    "url",
    "learn more",
    "continue reading",
}

# User Agent string to identify the bot.
DEFAULT_USER_AGENT = "LinkEquityAuditor/1.0 (+https://github.com/yourrepo/auditor)"

# Politeness delay in seconds to avoid hammering the server.
CRAWL_DELAY = 0.5

# -----------------------------------------------------------------------------
# Data Structures
# -----------------------------------------------------------------------------

@dataclass
class LinkRecord:
    """Represents a single hyperlink found during crawling."""
    source_url: str
    target_url: str
    anchor_text: str

@dataclass
class PageMetrics:
    """Aggregated metrics for a specific Target URL."""
    target_url: str
    total_inbound: int = 0
    generic_anchors: int = 0
    unique_anchors: Set[str] = field(default_factory=set)

    @property
    def equity_efficiency_score(self) -> float:
        """
        Calculates a score between 0.0 and 1.0.
        1.0 = All inbound links use descriptive, unique text.
        0.0 = All inbound links are generic (worst case).
        """
        if self.total_inbound == 0:
            return 0.0
        return (self.total_inbound - self.generic_anchors) / self.total_inbound

# -----------------------------------------------------------------------------
# Internal Tooling
# -----------------------------------------------------------------------------

class AnchorExtractor(HTMLParser):
    """
    A focused HTML parser that extracts href attributes and corresponding text.
    Uses stdlib html.parser to avoid external dependencies like BeautifulSoup.
    """
    
    def __init__(self) -> None:
        super().__init__()
        self.links: List[Dict[str, str]] = []
        self._current_link: Optional[Dict[str, str]] = None
        self._capture_data: bool = False

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if tag.lower() == "a":
            href = next((v for k, v in attrs if k.lower() == "href"), None)
            if href:
                self._current_link = {"href": href, "text": ""}
                self._capture_data = True

    def handle_data(self, data: str) -> None:
        if self._capture_data and self._current_link:
            # Normalize whitespace during extraction
            clean_data = " ".join(data.split())
            self._current_link["text"] += clean_data

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._capture_data:
            if self._current_link:
                self.links.append(self._current_link)
            self._current_link = None
            self._capture_data = False

class SitemapParser:
    """Handles fetching and parsing sitemap.xml structures."""
    
    @staticmethod
    def get_urls(domain: str, session: requests.Session, timeout: int = 10) -> Set[str]:
        """
        Attempts to fetch sitemap.xml and parse valid URLs.
        Supports simple sitemap indexes (recursively fetching sub-sitemaps).
        """
        urls: Set[str] = set()
        base_netloc = urllib.parse.urlparse(domain).netloc
        
        # Common sitemap locations
        possible_sitemaps = [
            f"{domain.rstrip('/')}/sitemap.xml",
            f"{domain.rstrip('/')}/sitemap_index.xml",
        ]
        
        processed_sitemaps: Set[str] = set()

        for sitemap_url in possible_sitemaps:
            SitemapParser._process_sitemap(sitemap_url, session, urls, processed_sitemaps, timeout)
            if urls:
                break
        
        if not urls:
            logging.warning(f"No URLs found in sitemaps for {domain}. Try manual entry if file exists.")
            
        return urls

    @staticmethod
    def _process_sitemap(
        sitemap_url: str, 
        session: requests.Session, 
        url_set: Set[str], 
        processed: Set[str],
        timeout: int
    ) -> None:
        if sitemap_url in processed:
            return
        
        processed.add(sitemap_url)
        
        try:
            logging.debug(f"Fetching sitemap: {sitemap_url}")
            response = session.get(sitemap_url, timeout=timeout)
            response.raise_for_status()
            
            content = response.text
            
            # Simple regex extraction to avoid heavy XML libs for this specific task
            # Looks for <loc>...</loc> tags
            locs = re.findall(r"<loc>\s*([^<]+)\s*</loc>", content, re.IGNORECASE)
            
            for loc in locs:
                loc = loc.strip()
                if loc.endswith(".xml"):
                    # Recursively handle sitemap indexes
                    SitemapParser._process_sitemap(loc, session, url_set, processed, timeout)
                else:
                    url_set.add(loc)
                    
        except (requests.RequestException, Exception) as e:
            logging.error(f"Failed to process sitemap {sitemap_url}: {e}")

# -----------------------------------------------------------------------------
# Core Logic
# -----------------------------------------------------------------------------

class EquityAuditor:
    """Main controller for the auditing process."""

    def __init__(self, base_domain: str):
        self.base_domain = base_domain
        parsed = urllib.parse.urlparse(base_domain)
        self.domain_root = f"{parsed.scheme}://{parsed.netloc}"
        self.session = requests.Session()
        
        # Setup Headers
        user_agent = os.getenv("AUDIT_USER_AGENT", DEFAULT_USER_AGENT)
        headers = {"User-Agent": user_agent}
        
        # Graceful API Key handling
        api_key = os.getenv("AUDIT_API_KEY")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
            logging.info("API Key detected in environment variables.")
            
        self.session.headers.update(headers)
        
        # Graph storage
        self.link_graph: List[LinkRecord] = []
        self.metrics: Dict[str, PageMetrics] = defaultdict(PageMetrics)

    def _normalize_url(self, url: str) -> str:
        """
        Ensures URLs are absolute and consistent.
        Removes fragments (#) to prevent duplicate page tracking.
        """
        url = url.strip()
        
        # Handle relative URLs
        if not url.startswith(("http://", "https://")):
            if url.startswith("/"):
                url = self.domain_root + url
            else:
                # Relative to current path approximation (simplifying for script)
                url = f"{self.domain_root}/{url}"
        
        # Remove fragments
        parsed = urllib.parse.urlparse(url)
        clean = parsed._replace(fragment="").geturl()
        
        # Remove trailing slash for consistency, unless root
        if clean.endswith("/") and clean != f"{self.domain_root}/":
            clean = clean[:-1]
            
        return clean

    def _is_internal(self, url: str) -> bool:
        parsed = urllib.parse.urlparse(url)
        return parsed.netloc == urllib.parse.urlparse(self.domain_root).netloc

    def crawl_and_analyze(self) -> None:
        """Orchestrates the sitemap fetch, crawl, and analysis."""
        logging.info(f"Starting audit for: {self.base_domain}")
        
        # 1. Get URLs from Sitemap
        urls_to_crawl = SitemapParser.get_urls(self.base_domain, self.session)
        
        if not urls_to_crawl:
            logging.error("Critical: No pages found to crawl. Exiting.")
            return

        logging.info(f"Found {len(urls_to_crawl)} pages in sitemap. Beginning crawl...")

        # 2. Iterate and Parse
        total_urls = len(urls_to_crawl)
        for i, page_url in enumerate(urls_to_crawl, start=1):
            logging.info(f"[{i}/{total_urls}] Processing: {page_url}")
            
            try:
                response = self.session.get(page_url, timeout=15)
                
                # Graceful degradation on non-200 status
                if response.status_code != 200:
                    logging.warning(f"Skipping {page_url}: Status {response.status_code}")
                    continue
                    
                # Extracting content
                parser = AnchorExtractor()
                # Feeding bytes is safer for specific encodings, but response.text handles most utf-8
                parser.feed(response.text)
                
                # 3. Process Links
                for link_data in parser.links:
                    target_href = link_data["href"]
                    anchor_text = link_data["text"].strip().lower()
                    
                    if not target_href or not anchor_text:
                        continue
                        
                    normalized_target = self._normalize_url(target_href)
                    
                    # Filter for internal links only
                    if self._is_internal(normalized_target):
                        record = LinkRecord(
                            source_url=page_url,
                            target_url=normalized_target,
                            anchor_text=anchor_text
                        )
                        self.link_graph.append(record)
                
                # Politeness
                time.sleep(CRAWL_DELAY)
                
            except Exception as e:
                logging.error(f"Error crawling {page_url}: {e}")

        logging.info("Crawl complete. Aggregating metrics...")
        self._aggregate_metrics()

    def _aggregate_metrics(self) -> None:
        """Processes raw link records into PageMetrics."""
        for record in self.link_graph:
            target = record.target_url
            anchor = record.anchor_text
            
            # Update or create metrics entry
            if target not in self.metrics:
                self.metrics[target] = PageMetrics(target_url=target)
            
            metric = self.metrics[target]
            metric.total_inbound += 1
            
            # Check against stoplist
            # We use simple matching, but fuzzy matching could be added here.
            # Exact match of common phrases is usually sufficient for leakage detection.
            if anchor in GENRIC_ANCHOR_STOPLIST:
                metric.generic_anchors += 1
            
            metric.unique_anchors.add(anchor)

    def export_report(self, filepath: str) -> None:
        """Writes the final analysis to a CSV file."""
        if not self.metrics:
            logging.warning("No metrics to export.")
            return

        try:
            with open(filepath, mode="w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                # Header
                writer.writerow([
                    "Target_URL", 
                    "Total_Inbound_Links", 
                    "Generic_Anchor_Count", 
                    "Unique_Anchor_Types", 
                    "Equity_Efficiency_Score"
                ])
                
                # Sort by Efficiency Score (Ascending = Worst offenders first)
                sorted_metrics = sorted(
                    self.metrics.values(), 
                    key=lambda x: x.equity_efficiency_score
                )
                
                for m in sorted_metrics:
                    writer.writerow([
                        m.target_url,
                        m.total_inbound,
                        m.generic_anchors,
                        len(m.unique_anchors),
                        f"{m.equity_efficiency_score:.4f}"
                    ])
            
            logging.info(f"Report successfully generated: {filepath}")
            
        except IOError as e:
            logging.error(f"Failed to write CSV: {e}")

# -----------------------------------------------------------------------------
# CLI Interface
# -----------------------------------------------------------------------------

def main() -> None:
    """Entry point for the CLI."""
    parser = argparse.ArgumentParser(
        description="Audit internal link structures to detect equity leakage via generic anchor text.",
        epilog="Example: python link_equity_auditor.py https://site.com -o audit_report.csv"
    )
    
    parser.add_argument(
        "domain",
        help="The root domain URL to audit (e.g., https://example.com)."
    )
    
    parser.add_argument(
        "-o", "--output",
        default="equity_leakage_report.csv",
        help="Filename for the generated CSV report. Default: equity_leakage_report.csv"
    )
    
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose logging to stdout."
    )

    args = parser.parse_args()

    # Logging Configuration
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s - %(levelname)s - %(message)s",
        datefmt="%H:%M:%S"
    )

    # Basic Domain Validation
    if not args.domain.startswith(("http://", "https://")):
        logging.error("Domain must include scheme (http:// or https://).")
        sys.exit(1)

    try:
        # Instantiate Auditor
        auditor = EquityAuditor(args.domain)
        
        # Run Logic
        auditor.crawl_and_analyze()
        
        # Output Results
        auditor.export_report(args.output)
        
        sys.exit(0)
        
    except KeyboardInterrupt:
        logging.info("Process interrupted by user.")
        sys.exit(130)
    except Exception as e:
        logging.critical(f"Unhandled exception: {e}", exc_info=True)
        sys.exit(1)

if __name__ == "__main__":
    main()