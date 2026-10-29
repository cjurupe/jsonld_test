#!/usr/bin/env python3
"""Generates the JSON-LD of a company page using the com.manta.jsonld:json-ld-seo library.

Mirrors what CompaniesControllerHelper.addJsonLD() does in gateway-api:

    JsonNode cData  = companyData.get("data");
    JsonNode jsonLD = companyPageJsonLDGenerator.generateJsonLDForCompanyViewPage(cData);
    ((ObjectNode) cData).set("jsonld", jsonLD);

Steps it runs:
  1. Builds the input JSON (the `data` node the generator receives) by hitting the e2e
     services, the same way getSecondaryCalls() does:
       - core:            livedata-service  /v1/company/full/mid/{emid}
       - recommendations: recommendation-service /v1/recommendations/{emid}   (shallow merge)
       - products:        products-service /v3/products?claim_id={claimId}  -> products.productTerms
       - services:        products-service /v3/services?claim_id={claimId}  -> services.serviceTerms
         (claimed / PBL companies only, as gateway-api's getSecondaryCalls() does)
     Those are the only upstreams that feed the JSON-LD (the remaining secondary calls
     contribute no field the library reads).
  2. Downloads json-ld-seo (+ dependencies) into a local cache. Manta artifacts come from
     Nexus using NEXUS_USERNAME / NEXUS_PASSWORD; everything else from Maven Central.
     json-ld-seo itself is always re-downloaded: its cached jar is deleted first and the
     ~/.m2 / ~/.ivy2 copies are ignored, so a run never tests a stale library. The
     dependencies are still reused from those caches.
  3. Compiles and runs a minimal Java driver that wires the generator by hand (the wiring
     Guice normally does) and returns the JSON-LD the library produces.

Usage:
    ./tools/generate_jsonld.py                              # m1q16vx from e2e
    ./tools/generate_jsonld.py --emid m1q16vx --full        # whole data node with jsonld
    ./tools/generate_jsonld.py --save-company /tmp/c.json -o /tmp/jsonld.json
    ./tools/generate_jsonld.py --input /tmp/c.json          # already downloaded payload
    ./tools/generate_jsonld.py --url http://localhost:5700/api/v2/companies/m1q16vx --key $GATEWAY_KEY

Heads-up: https://e2e.manta.com/c/{emid}/{slug}?json=1 returns HTML, not JSON; that is why
the default source is the internal services and not that URL.

As a library:
    from generate_jsonld import build_company_node, generate_jsonld
    jsonld, data = generate_jsonld(build_company_node("m1q16vx"))
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

JSONLD_VERSION = "1.1.0"  # see gateway-api/build.sbt: "com.manta.jsonld" % "json-ld-seo"

# Nexus bases tried in order (Nexus 3 and Nexus 2 layouts).
NEXUS_BASES = [
    "http://maven.aws.ecnext.net:8081/repository/manta.release",
    "http://maven.aws.ecnext.net:8081/repository/manta.snapshot",
    "http://maven.aws.ecnext.net:8081/nexus/content/repositories/releases",
    "http://maven.aws.ecnext.net:8081/nexus/content/repositories/snapshots",
]

CENTRAL_BASE = "https://repo1.maven.org/maven2"

# Manta artifacts -> Nexus (credentials required).
MANTA_ARTIFACTS = [
    ("com.manta.jsonld", "json-ld-seo", JSONLD_VERSION),
    ("com.manta.json", "json-utils", "1.3.3"),
    # LocalBusinessJsonLD resolves countries through com.manta.geo.v2.country.CountryLocator
    # in its static initialiser, so this jar is required even though nothing here names it.
    # It bundles its own regions.data, so the jar alone is enough.
    ("com.manta.geo", "geo-common", "2.10.5"),
]

# Artifacts that are always re-downloaded: the cached jar is deleted before fetching it
# and the ~/.m2 and ~/.ivy2 copies are ignored. The library is republished under the same
# version often enough that reusing a jar from an earlier run silently tests old code.
ALWAYS_FRESH_ARTIFACTS = {"json-ld-seo"}

# Runtime dependencies of json-ld-seo (same classpath as target/cp.txt, minus test scope).
THIRD_PARTY_ARTIFACTS = [
    ("com.fasterxml.jackson.core", "jackson-core", "2.9.7"),
    ("com.fasterxml.jackson.core", "jackson-databind", "2.9.7"),
    ("com.fasterxml.jackson.core", "jackson-annotations", "2.9.0"),
    ("commons-io", "commons-io", "1.3.2"),
    ("ch.qos.logback", "logback-core", "1.2.3"),
    ("ch.qos.logback", "logback-classic", "1.2.3"),
    ("org.slf4j", "slf4j-api", "1.7.25"),
    ("com.google.inject", "guice", "4.1.0"),
    ("javax.inject", "javax.inject", "1"),
    ("aopalliance", "aopalliance", "1.0"),
    ("com.google.guava", "guava", "19.0"),
    ("org.apache.commons", "commons-lang3", "3.6"),
    ("com.github.mautini", "schemaorg-java", "1.0.1"),
    ("com.google.code.findbugs", "jsr305", "3.0.1"),
    ("com.google.code.gson", "gson", "2.5"),
]

# DEFAULT_EMID = "mm7v0lf"
# DEFAULT_EMID = "mm046sr"
# DEFAULT_EMID = "mm84h8f"
# DEFAULT_EMID = "mt83hx0" 
# DEFAULT_EMID = "mrl7qb8"
# DEFAULT_EMID = "m1q740y"
# DEFAULT_EMID = "m14gkm6" #US COMPANY
# DEFAULT_EMID = "mx4dph1" #US 
# DEFAULT_EMID = "mk7ykx5"
# DEFAULT_EMID = "m14c2yn"
# DEFAULT_EMID = "m15ry2w"
DEFAULT_EMID = "mmjjpxt"

# Internal e2e hosts (the same services gateway-api consumes).
E2E_LIVEDATA = "http://livedata-service.manta-site.e2e.smoketest.aws.ecnext.net:5721/v1/company"
E2E_RECOMMENDATIONS = (
    "http://recommendation-service.manta-site.e2e.smoketest.aws.ecnext.net:5707/v1/recommendations"
)
# products-service v3: the product and service lists published as hasOfferCatalog.
E2E_PRODUCTS = "http://products-service-app.manta-site.e2e.smoketest.aws.ecnext.net:5712"
# company-service: resolves the FAQ section (POST faqs/{emid}/resolve), as gateway-api does.
E2E_COMPANY = "http://company-service.manta-site.e2e.smoketest.aws.ecnext.net:5701/v1/company"
# Cap gateway-api advertises on each list (productService.v3.maxItems).
CATALOG_MAX_ITEMS = 10
# E2E_LIVEDATA = "http://livedata-service.manta-site.main.production.aws.ecnext.net:5721/v1/company"
# E2E_RECOMMENDATIONS = (
#     "http://recommendation-service.manta-site.main.production.aws.ecnext.net:5707/v1/recommendations"
# )

DEFAULT_WORK_DIR = Path(tempfile.gettempdir()) / "manta-jsonld"

# Java driver: the wiring Guice does via @Inject, done by hand.
JAVA_DRIVER = r"""
import java.nio.file.Files;
import java.nio.file.Paths;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;

import com.manta.json.JsonHelpers;
import com.manta.jsonld.BreadCrumbsJsonLD;
import com.manta.jsonld.CompanyPageJsonLDGenerator;
import com.manta.jsonld.FaqJsonLD;
import com.manta.jsonld.JsonLDUtils;
import com.manta.jsonld.LocalBusinessJsonLD;
import com.manta.jsonld.ServiceAreas;

/**
 * Usage: JsonLdDriver <company-node.json> <out-jsonld.json> <out-company-with-jsonld.json>
 *
 * The first argument is the "data" node of the gateway-api response
 * (the equivalent of cData in CompaniesControllerHelper.addJsonLD).
 */
public class JsonLdDriver {

    public static void main(String[] args) throws Exception {
        final ObjectMapper mapper = new ObjectMapper();
        final JsonNode cData = mapper.readTree(Files.readAllBytes(Paths.get(args[0])));

        // Manual wiring: in gateway-api Guice injects this.
        final JsonHelpers jsonHelpers = new JsonHelpers();
        final JsonLDUtils jsonLDUtils = new JsonLDUtils(jsonHelpers);
        final ServiceAreas serviceAreas = new ServiceAreas(jsonHelpers);
        final LocalBusinessJsonLD localBusinessJsonLD =
                new LocalBusinessJsonLD(jsonHelpers, jsonLDUtils, serviceAreas);
        final BreadCrumbsJsonLD breadCrumbsJsonLD = new BreadCrumbsJsonLD(jsonHelpers, jsonLDUtils);
        final FaqJsonLD faqJsonLD = new FaqJsonLD(jsonHelpers, jsonLDUtils);
        final CompanyPageJsonLDGenerator companyPageJsonLDGenerator =
                new CompanyPageJsonLDGenerator(localBusinessJsonLD, breadCrumbsJsonLD, faqJsonLD);

        // --- copy of addJsonLD() ---
        JsonNode jsonLD = null;
        if (cData != null && !cData.isMissingNode()) {
            jsonLD = companyPageJsonLDGenerator.generateJsonLDForCompanyViewPage(cData);
            ((ObjectNode) cData).set("jsonld", jsonLD);
        }
        // --- end of copy ---

        if (jsonLD == null) {
            System.err.println("companyData is empty or a missing node; no JSON-LD was generated");
            System.exit(2);
        }

        Files.write(Paths.get(args[1]), mapper.writerWithDefaultPrettyPrinter().writeValueAsBytes(jsonLD));
        Files.write(Paths.get(args[2]), mapper.writerWithDefaultPrettyPrinter().writeValueAsBytes(cData));
    }
}
"""

# The library logs through logback; send it to stderr so stdout stays clean.
LOGBACK_XML = """<configuration>
  <appender name="STDERR" class="ch.qos.logback.core.ConsoleAppender">
    <target>System.err</target>
    <encoder><pattern>%-5level %logger{36} - %msg%n</pattern></encoder>
  </appender>
  <root level="WARN"><appender-ref ref="STDERR"/></root>
</configuration>
"""


class Failure(RuntimeError):
    """User-facing error: reported without a traceback."""


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


# --------------------------------------------------------------------------- #
# HTTP (curl, same pattern as the Nexus recipe)
# --------------------------------------------------------------------------- #


def _curl(
    url: str,
    dest: Path,
    auth: str | None = None,
    header: str | None = None,
    insecure: bool = False,
    timeout: int = 30,
) -> None:
    cmd = ["curl", "-sS", "-L", "-f", "-m", str(timeout), "-o", str(dest), url]
    if auth:
        cmd += ["-u", auth]
    if header:
        cmd += ["-H", header]
    if insecure:
        cmd.append("-k")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        dest.unlink(missing_ok=True)
        raise Failure(f"curl failed ({proc.returncode}) for {url}: {proc.stderr.strip()}")


def fetch_json(url: str, header: str | None = None, insecure: bool = False) -> dict:
    log(f"GET {url}")
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        dest = Path(tmp.name)
    try:
        _curl(url, dest, header=header, insecure=insecure)
        raw = dest.read_text(encoding="utf-8", errors="replace")
    finally:
        dest.unlink(missing_ok=True)

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        head = raw[:200].replace("\n", " ")
        raise Failure(
            f"The response from {url} is not JSON ({exc}). First bytes: {head!r}\n"
            "Remember that /c/{emid}/{slug}?json=1 on e2e returns HTML."
        ) from exc


def post_json(url: str, body: dict, insecure: bool = False, timeout: int = 30) -> dict:
    log(f"POST {url}")
    cmd = ["curl", "-sS", "-f", "-m", str(timeout), "-X", "POST",
           "-H", "Content-Type: application/json", "--data-binary", "@-", url]
    if insecure:
        cmd.append("-k")
    proc = subprocess.run(cmd, input=json.dumps(body), capture_output=True, text=True)
    if proc.returncode != 0:
        raise Failure(f"curl failed ({proc.returncode}) for {url}: {proc.stderr.strip()}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise Failure(f"The response from {url} is not JSON ({exc}): {proc.stdout[:200]!r}") from exc


# --------------------------------------------------------------------------- #
# 1. Jar resolution
# --------------------------------------------------------------------------- #


def _maven_path(group: str, artifact: str, version: str) -> str:
    return f"{group.replace('.', '/')}/{artifact}/{version}/{artifact}-{version}.jar"


def _local_candidates(group: str, artifact: str, version: str) -> list[Path]:
    home = Path.home()
    return [
        home / ".m2" / "repository" / _maven_path(group, artifact, version),
        home / ".ivy2" / "cache" / group / artifact / "jars" / f"{artifact}-{version}.jar",
        home / ".ivy2" / "local" / group / artifact / version / "jars" / f"{artifact}.jar",
    ]


def nexus_auth() -> str:
    user = os.environ.get("NEXUS_USERNAME")
    password = os.environ.get("NEXUS_PASSWORD")
    if not user or not password:
        raise Failure(
            "NEXUS_USERNAME / NEXUS_PASSWORD are required to download the Manta artifacts "
            "(json-ld-seo, json-utils). Export them or pass --extra-jar with the local jar."
        )
    return f"{user}:{password}"


def resolve_jar(
    group: str,
    artifact: str,
    version: str,
    cache_dir: Path,
    from_nexus: bool,
    nexus_bases: list[str],
    refresh: bool = False,
) -> Path:
    """Returns the path to the jar, downloading it when needed."""
    fresh = refresh or artifact in ALWAYS_FRESH_ARTIFACTS
    if not fresh:
        for candidate in _local_candidates(group, artifact, version):
            if candidate.is_file():
                return candidate

    cached = cache_dir / f"{artifact}-{version}.jar"
    if cached.is_file():
        if not fresh:
            return cached
        log(f"  deleting cached {cached}")
        cached.unlink()

    cache_dir.mkdir(parents=True, exist_ok=True)
    rel = _maven_path(group, artifact, version)

    if from_nexus:
        auth = nexus_auth()
        errors = []
        for base in nexus_bases:
            url = f"{base.rstrip('/')}/{rel}"
            try:
                log(f"  downloading {artifact}-{version}.jar from {base}")
                _curl(url, cached, auth=auth)
                return cached
            except Failure as exc:
                errors.append(str(exc))
        raise Failure(
            f"Could not download {group}:{artifact}:{version} from Nexus.\n  " + "\n  ".join(errors)
        )

    log(f"  downloading {artifact}-{version}.jar from Maven Central")
    _curl(f"{CENTRAL_BASE}/{rel}", cached)
    return cached


def build_classpath(
    cache_dir: Path,
    nexus_bases: list[str] | None = None,
    version: str = JSONLD_VERSION,
    refresh: bool = False,
    extra_jars: list[str] | None = None,
    local_lib: str | None = None,
) -> str:
    nexus_bases = nexus_bases or NEXUS_BASES
    jars: list[Path] = []

    for group, artifact, ver in MANTA_ARTIFACTS:
        if artifact == "json-ld-seo":
            ver = version
            # An explicit local build replaces the published jar: nothing is downloaded
            # for it, so a version that is no longer in Nexus can still be run.
            if local_lib:
                lib = Path(local_lib).expanduser()
                if not lib.is_file():
                    raise Failure(f"--local-lib does not point to a jar: {lib}")
                log(f"  using the local build {lib} instead of json-ld-seo {ver}")
                jars.append(lib)
                continue
        jars.append(resolve_jar(group, artifact, ver, cache_dir, True, nexus_bases, refresh))
    for group, artifact, ver in THIRD_PARTY_ARTIFACTS:
        jars.append(resolve_jar(group, artifact, ver, cache_dir, False, nexus_bases, refresh))

    jars += [Path(j) for j in (extra_jars or [])]
    return os.pathsep.join(str(j) for j in jars)


# --------------------------------------------------------------------------- #
# 2. Input JSON (the `data` node)
# --------------------------------------------------------------------------- #


def build_company_node(
    emid: str = DEFAULT_EMID,
    livedata_base: str = E2E_LIVEDATA,
    recommendations_base: str | None = E2E_RECOMMENDATIONS,
    insecure: bool = False,
    products_base: str | None = E2E_PRODUCTS,
    company_base: str | None = E2E_COMPANY,
) -> dict:
    """Builds the `data` node by hitting livedata, recommendations, products-service and
    company-service (FAQs), like the gateway does."""
    core = fetch_json(f"{livedata_base.rstrip('/')}/full/mid/{emid}", insecure=insecure)
    data = core.get("data")
    if not isinstance(data, dict):
        raise Failure(f"livedata returned no data node for {emid}: {json.dumps(core)[:200]}")

    if recommendations_base:
        try:
            recs = fetch_json(f"{recommendations_base.rstrip('/')}/{emid}", insecure=insecure)
        except Failure as exc:
            log(f"  warning: no recommendations ({exc}); the JSON-LD will have no review/aggregateRating")
        else:
            # mergeIntoCompanyData(): shallow merge of the secondary response into `data`
            for key, value in recs.items():
                data[key] = value

    if products_base:
        add_catalog_items(data, products_base, insecure)

    # Last, as in gateway-api: the FAQs are composed from the products, services and
    # service areas merged above.
    if company_base:
        add_faqs(data, emid, company_base, insecure)

    return data


def faqs_resolve_payload(data: dict) -> dict:
    """The body gateway-api posts to company-service (CompanyData.buildFaqsResolvePayload)."""
    def get(path: str):
        node = data
        for key in path.split("."):
            if not isinstance(node, dict):
                return None
            node = node.get(key)
        return node

    def catalog_names(path: str) -> list | None:
        items = get(path)
        if not isinstance(items, list):
            return None
        return [i.get("name") for i in items
                if isinstance(i, dict) and i.get("isActive", True) is not False]

    def service_areas() -> list | None:
        areas = data.get("googleServiceAreas")
        if not isinstance(areas, list):
            return None
        out = []
        for area in areas:
            desc = area.get("description") if isinstance(area, dict) else None
            if desc:
                parts = desc.split(",")
                out.append(parts[0] + " " + parts[1] if len(parts) > 1 else desc)
        return out

    def format_phone(phone):
        if isinstance(phone, str) and re.fullmatch(r"\d{10}", phone):
            return f"({phone[:3]}) {phone[3:6]}-{phone[6:]}"
        return phone

    name = get("name")
    body = {
        "name": name,
        "email": get("contactInfo.email"),
        "category": get("industry.sicm.name"),
        "city": get("location.city"),
        "state": get("location.stateAbbrv"),
    }
    for key, value in (("products", catalog_names("products.productTerms.list")),
                       ("services", catalog_names("services.serviceTerms.list")),
                       ("serviceAreas", service_areas())):
        if value is not None:
            body[key] = value
    phone = get("contactInfo.phone")
    if phone is not None:
        body["phone"] = format_phone(phone)
    body["hidePhone"] = get("contactInfo.hidePhone") is True
    # DAC Group (Manta Verified) State Farm listings answer pricing/booking with a Get Quote link.
    if data.get("claimSource") == "DAC_GROUP" and "state farm" in str(name or "").lower():
        body["getQuoteUrl"] = get("contactInfo.website") or "https://www.statefarm.com/"
    return body


def add_faqs(data: dict, emid: str, company_base: str, insecure: bool = False) -> None:
    """Sets data.faqs to the FAQ section the page shows (hidden questions left out), as
    gateway-api does; an unreachable company-service degrades to no FAQs."""
    url = f"{company_base.rstrip('/')}/faqs/{emid}/resolve?filterHidden=true"
    try:
        faqs = post_json(url, faqs_resolve_payload(data), insecure=insecure).get("faqs")
    except Failure as exc:
        log(f"  warning: no FAQs ({exc}); the JSON-LD will have no FAQPage")
        faqs = None
    data["faqs"] = faqs if isinstance(faqs, list) else []


def add_catalog_items(data: dict, products_base: str, insecure: bool = False) -> None:
    """Adds products.productTerms and services.serviceTerms from products-service v3.

    Mirrors gateway-api: only a claimed or PBL company gets them, and an unreachable
    products-service degrades a list to empty instead of failing.
    """
    claim_status = str(data.get("claimStatus") or "").upper()
    claim_id = data.get("claimId")
    if claim_status not in ("CLAIMED", "PBL") or not claim_id:
        log(f"  no products/services: claimStatus={claim_status or None}, claimId={claim_id}")
        return

    for path, collection, terms in (("products", "products", "productTerms"),
                                    ("services", "services", "serviceTerms")):
        url = f"{products_base.rstrip('/')}/v3/{path}?claim_id={claim_id}"
        try:
            items = fetch_json(url, insecure=insecure).get("data") or []
        except Failure as exc:
            log(f"  warning: no {collection} ({exc}); the JSON-LD will have no catalog for them")
            items = []
        data[collection] = {
            terms: {
                "count": len(items),
                "maxCreationAllowed": CATALOG_MAX_ITEMS,
                "maxActiveAllowed": CATALOG_MAX_ITEMS,
                "list": items,
            }
        }


def extract_company_node(payload: dict) -> dict:
    """Pulls out the node the generator expects (the cData of addJsonLD).

    Accepts the gateway-api / livedata response (`data` is the company), the web's
    ?json=1 payload (`data.company`) or the bare company node.
    """
    if not isinstance(payload, dict):
        raise Failure("The company payload is not a JSON object")

    data = payload.get("data")
    if isinstance(data, dict):
        company = data.get("company")
        node = company if isinstance(company, dict) else data
    else:
        node = payload

    if not any(k in node for k in ("name", "emid", "location", "breadCrumbs")):
        raise Failure(
            "Could not find the company node in the payload "
            f"(top-level keys: {sorted(node)[:12]})"
        )

    node.pop("jsonld", None)  # if the gateway already injected it, we regenerate it
    return node


# --------------------------------------------------------------------------- #
# 3. Compile + run the driver
# --------------------------------------------------------------------------- #


def _require(binary: str) -> str:
    path = shutil.which(binary)
    if not path:
        raise Failure(f"Could not find '{binary}' on the PATH; a JDK 8+ is required")
    return path


def generate_jsonld(
    company_node: dict,
    cache_dir: Path = DEFAULT_WORK_DIR / "jars",
    classpath: str | None = None,
    keep_work_dir: bool = False,
    **classpath_kwargs,
) -> tuple[dict, dict]:
    """Runs the library's generator and returns (jsonld, data_with_jsonld)."""
    javac, java = _require("javac"), _require("java")
    if classpath is None:
        log("Resolving the json-ld-seo classpath...")
        classpath = build_classpath(cache_dir, **classpath_kwargs)

    work = Path(tempfile.mkdtemp(prefix="jsonld-driver-"))
    try:
        src = work / "JsonLdDriver.java"
        src.write_text(JAVA_DRIVER, encoding="utf-8")
        logback = work / "logback.xml"
        logback.write_text(LOGBACK_XML, encoding="utf-8")

        in_json = work / "company.json"
        in_json.write_text(json.dumps(company_node), encoding="utf-8")
        out_jsonld = work / "jsonld.json"
        out_company = work / "company-with-jsonld.json"

        classes = work / "classes"
        classes.mkdir()
        compile_proc = subprocess.run(
            [javac, "-nowarn", "-cp", classpath, "-d", str(classes), str(src)],
            capture_output=True,
            text=True,
        )
        if compile_proc.returncode != 0:
            raise Failure(f"javac failed:\n{compile_proc.stdout}\n{compile_proc.stderr}")

        run_proc = subprocess.run(
            [
                java,
                f"-Dlogback.configurationFile={logback}",
                "-cp",
                os.pathsep.join([str(classes), classpath]),
                "JsonLdDriver",
                str(in_json),
                str(out_jsonld),
                str(out_company),
            ],
            capture_output=True,
            text=True,
        )
        if run_proc.stderr.strip():
            log(run_proc.stderr.rstrip())
        if run_proc.returncode != 0:
            raise Failure(f"The Java driver failed (exit {run_proc.returncode})")

        return (
            json.loads(out_jsonld.read_text(encoding="utf-8")),
            json.loads(out_company.read_text(encoding="utf-8")),
        )
    finally:
        if keep_work_dir:
            log(f"work dir: {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generates the JSON-LD of a company page with the json-ld-seo library",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--url", help="Already assembled payload (gateway /api/v2/companies/:emid, livedata, ?json=1)")
    source.add_argument("--input", type=Path, help="Already downloaded JSON file")
    parser.add_argument("--emid", default=DEFAULT_EMID, help=f"default: {DEFAULT_EMID}")
    parser.add_argument("--livedata-url", default=E2E_LIVEDATA, help=f"default: {E2E_LIVEDATA}")
    parser.add_argument("--recommendations-url", default=E2E_RECOMMENDATIONS, help="recommendation-service base")
    parser.add_argument("--no-recommendations", action="store_true", help="Skip the recommendation-service call")
    parser.add_argument("--products-url", default=E2E_PRODUCTS, help="products-service base (v3 products/services)")
    parser.add_argument("--no-products", action="store_true", help="Skip the products-service calls")
    parser.add_argument("--company-url", default=E2E_COMPANY, help="company-service base (FAQ resolve)")
    parser.add_argument("--no-faqs", action="store_true", help="Skip the company-service FAQ call")
    parser.add_argument("--key", help="Value of the 'key' header (gateway-api auth) for --url")
    parser.add_argument("--insecure", action="store_true", help="curl -k (self-signed certs)")
    parser.add_argument("--save-company", type=Path, help="Saves the data node handed to the generator")
    parser.add_argument("-o", "--output", type=Path, help="Writes the result to a file")
    parser.add_argument(
        "--full",
        action="store_true",
        help="Prints the whole data node with jsonld injected (as the gateway returns it)",
    )
    parser.add_argument("--version", default=JSONLD_VERSION, help=f"default: {JSONLD_VERSION}")
    parser.add_argument(
        "--nexus-base",
        action="append",
        dest="nexus_bases",
        help="Nexus base (repeatable). Default: the 4 known layouts of maven.aws.ecnext.net",
    )
    parser.add_argument("--extra-jar", action="append", dest="extra_jars", help="Extra jar for the classpath")
    parser.add_argument(
        "--local-lib",
        help="Run this json-ld-seo jar (e.g. json-ld-seo/target/json-ld-seo.jar) instead of "
        "downloading it from Nexus; the only way to run a version Nexus no longer has",
    )
    parser.add_argument("--work-dir", type=Path, default=DEFAULT_WORK_DIR, help=f"default: {DEFAULT_WORK_DIR}")
    parser.add_argument("--refresh", action="store_true", help="Re-downloads the jars, ignoring the cache")
    parser.add_argument("--keep-work-dir", action="store_true", help="Keeps the driver's temp dir")
    args = parser.parse_args(argv)

    try:
        if args.input:
            company_node = extract_company_node(json.loads(args.input.read_text(encoding="utf-8")))
        elif args.url:
            header = f"key: {args.key}" if args.key else None
            company_node = extract_company_node(fetch_json(args.url, header=header, insecure=args.insecure))
        else:
            company_node = build_company_node(
                args.emid,
                livedata_base=args.livedata_url,
                recommendations_base=None if args.no_recommendations else args.recommendations_url,
                insecure=args.insecure,
                products_base=None if args.no_products else args.products_url,
                company_base=None if args.no_faqs else args.company_url,
            )

        if args.save_company:
            args.save_company.write_text(json.dumps(company_node, indent=2), encoding="utf-8")
            log(f"data node saved to {args.save_company}")

        jsonld, data_with_jsonld = generate_jsonld(
            company_node,
            cache_dir=args.work_dir / "jars",
            keep_work_dir=args.keep_work_dir,
            nexus_bases=args.nexus_bases or NEXUS_BASES,
            version=args.version,
            refresh=args.refresh,
            extra_jars=args.extra_jars,
            local_lib=args.local_lib,
        )
    except Failure as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    result = data_with_jsonld if args.full else jsonld
    text = json.dumps(result, indent=2, ensure_ascii=False)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
        log(f"written to {args.output}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
