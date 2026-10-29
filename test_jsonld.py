#!/usr/bin/env python3
"""Testea los datos del JSON-LD de una company page.

Recibe un EMID y un environment. Internamente:

  1. Trae los datos del EMID desde el environment (GET /c/{emid}?json=1) seteando la
     cookie manta_session, que es lo que habilita la respuesta JSON.
  2. Baja e "instala" la libreria json-ld-seo desde Nexus:
       URL=http://maven.aws.ecnext.net:8081/repository/manta.release/com/manta/jsonld/json-ld-seo/1.0.11/json-ld-seo-1.0.11.jar
       curl -L -X GET -O "$URL" -u $NEXUS_USERNAME:$NEXUS_PASSWORD
     Si falla, corta con un mensaje de error explicando por que.
  3. Con la libreria bajada construye el JSON-LD del EMID (el mismo wiring que
     CompaniesControllerHelper.addJsonLD).
  4. Corre una bateria de checks sobre el JSON-LD generado, comparandolo contra los
     datos de la compania, y lo diffea contra el JSON-LD que devuelve el environment.

Uso:
    export NEXUS_USERNAME=... NEXUS_PASSWORD=...
    ./tools/test_jsonld.py m1q16vx                        # env e2e por default
    ./tools/test_jsonld.py m1q16vx --env prod
    ./tools/test_jsonld.py m1q16vx --base-url http://localhost:5700
    ./tools/test_jsonld.py m1q16vx --print-jsonld -o /tmp/jsonld.json

Exit code: 0 todo pasa, 1 algun check falla, 2 error de setup (cookie, Nexus, JDK).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from generate_jsonld import (  # noqa: E402  (import despues del sys.path)
    Failure,
    THIRD_PARTY_ARTIFACTS,
    fetch_json,
    generate_jsonld,
    log,
    resolve_jar,
)

# --------------------------------------------------------------------------- #
# Configuracion
# --------------------------------------------------------------------------- #

JSONLD_VERSION = "1.0.11"  # ver gateway-api/build.sbt

NEXUS_RELEASE_BASE = "http://maven.aws.ecnext.net:8081/repository/manta.release"

# Artefactos de Manta que se bajan de Nexus (json-utils trae el JsonHelpers que usa la lib).
NEXUS_ARTIFACTS = [
    ("com/manta/jsonld", "json-ld-seo", None),  # None => version de --version
    ("com/manta/json", "json-utils", "1.3.3"),
]

# Artefactos que siempre se vuelven a bajar: se borra el jar cacheado antes de pedirlo.
# La libreria se republica con la misma version bastante seguido, y reusar el jar de una
# corrida anterior significa testear codigo viejo sin darse cuenta.
ALWAYS_FRESH_ARTIFACTS = {"json-ld-seo"}

ENVIRONMENTS = {
    "e2e": "https://e2e.manta.com",
    "prod": "https://www.manta.com",
    "local": "http://localhost:5700",
}

# Sesion usada para que /c/{emid}?json=1 devuelva JSON en vez de HTML.
# Se puede sobreescribir con MANTA_SESSION o --cookie; caduca, y cuando caduca el
# environment responde HTML y el script lo reporta como tal.
DEFAULT_MANTA_SESSION = (
    "manta_session={%22loginIp%22:%2210.78.2.53%22%2C%22subId%22:%22126zaz5gn6hvt7q81%22"
    "%2C%22touchTimestamp%22:%221785781329593%22%2C%22fullHash%22:%226475fd4dde7241b3bbf60d7"
    "1a049ecb06fe5c434999af538e854005ac0106e21058be2bf4f97a57479d8b1c1df71e973e7d698e01a4190"
    "b5f0f7c364b0354e03%22%2C%22userRole%22:%22RS+eSv/tM9f0aLN/rCKdgQ==%22%2C%22notifType%22"
    ":%22web%22}"
)

DAYS = {"Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"}
TIME_RE = re.compile(r"^\d{2}:\d{2}$")
MANTA_PREFIX = "https://www.manta.com"


# --------------------------------------------------------------------------- #
# 1. Datos del EMID
# --------------------------------------------------------------------------- #


def fetch_company(emid: str, base_url: str, cookie: str, insecure: bool = False) -> dict:
    """Devuelve data.company del payload ?json=1 (el nodo que come el generador)."""
    url = f"{base_url.rstrip('/')}/c/{emid}?json=1"
    try:
        payload = fetch_json(url, header=f"Cookie: {cookie}", insecure=insecure)
    except Failure as exc:
        if "is not JSON" in str(exc):
            raise Failure(
                f"{base_url} devolvio HTML en vez de JSON para {emid}.\n"
                "Casi seguro la cookie manta_session esta vencida o no es de este "
                "environment: pasa una nueva con --cookie o MANTA_SESSION."
            ) from exc
        if "error: 404" in str(exc):
            raise Failure(f"{base_url} no tiene una company con EMID {emid} (404)") from exc
        raise

    company = (payload.get("data") or {}).get("company")
    if not isinstance(company, dict):
        raise Failure(
            f"El payload de {url} no trae data.company "
            f"(claves: {sorted(payload.get('data', {}))[:12]})"
        )
    return company


# --------------------------------------------------------------------------- #
# 2. Bajar la libreria de Nexus
# --------------------------------------------------------------------------- #


def download_library(version: str, dest_dir: Path, refresh: bool = False) -> list[Path]:
    """Baja json-ld-seo + json-utils de manta.release. Devuelve las rutas de los jars."""
    user = os.environ.get("NEXUS_USERNAME")
    password = os.environ.get("NEXUS_PASSWORD")
    if not user or not password:
        raise Failure(
            "No puedo bajar la libreria: falta NEXUS_USERNAME y/o NEXUS_PASSWORD.\n"
            "  export NEXUS_USERNAME=tu_usuario NEXUS_PASSWORD=tu_password"
        )

    dest_dir.mkdir(parents=True, exist_ok=True)
    jars: list[Path] = []

    for group_path, artifact, fixed_version in NEXUS_ARTIFACTS:
        ver = fixed_version or version
        jar = dest_dir / f"{artifact}-{ver}.jar"
        url = f"{NEXUS_RELEASE_BASE}/{group_path}/{artifact}/{ver}/{artifact}-{ver}.jar"
        fresh = refresh or artifact in ALWAYS_FRESH_ARTIFACTS

        if jar.is_file():
            if not fresh:
                log(f"  ya bajado: {jar.name}")
                jars.append(jar)
                continue
            log(f"  borrando cache: {jar}")
            jar.unlink()

        log(f"  curl -L -X GET -O {url}")
        proc = subprocess.run(
            [
                "curl", "-sS", "-L", "-X", "GET", "-o", str(jar),
                "-w", "%{http_code}", "-m", "120", "-u", f"{user}:{password}", url,
            ],
            capture_output=True,
            text=True,
        )
        status = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else "000"

        if proc.returncode != 0 or status != "200":
            jar.unlink(missing_ok=True)
            hints = {
                "401": "credenciales de Nexus invalidas (revisa NEXUS_USERNAME / NEXUS_PASSWORD)",
                "403": "el usuario de Nexus no tiene permiso sobre manta.release",
                "404": f"la version {ver} de {artifact} no existe en manta.release",
                "000": "no se pudo conectar a maven.aws.ecnext.net:8081 (VPN?)",
            }
            detail = hints.get(status, f"HTTP {status}")
            stderr = proc.stderr.strip()
            raise Failure(
                f"Fallo la descarga de {artifact}-{ver}.jar: {detail}"
                + (f"\n  curl: {stderr}" if stderr else "")
                + f"\n  URL: {url}"
            )

        if jar.stat().st_size < 1024:
            jar.unlink(missing_ok=True)
            raise Failure(f"{artifact}-{ver}.jar bajo vacio o truncado desde {url}")

        log(f"  ok: {jar} ({jar.stat().st_size // 1024} KB)")
        jars.append(jar)

    return jars


def build_classpath(version: str, work_dir: Path, refresh: bool = False) -> str:
    """Jars de Nexus + dependencias runtime (Maven Central o cache local)."""
    jars = [str(j) for j in download_library(version, work_dir / "lib", refresh)]
    for group, artifact, ver in THIRD_PARTY_ARTIFACTS:
        jars.append(str(resolve_jar(group, artifact, ver, work_dir / "deps", False, [], False)))
    return os.pathsep.join(jars)


# --------------------------------------------------------------------------- #
# 3. Checks
# --------------------------------------------------------------------------- #


class Result:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []  # (estado, nombre, detalle)

    def add(self, ok: bool | None, name: str, detail: str = "") -> None:
        state = "PASS" if ok else ("WARN" if ok is None else "FAIL")
        self.rows.append((state, name, detail))

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.add(ok, name, detail)

    @property
    def failed(self) -> int:
        return sum(1 for state, _, _ in self.rows if state == "FAIL")

    def render(self) -> str:
        icons = {"PASS": "✓", "FAIL": "✗", "WARN": "!"}
        lines = []
        for state, name, detail in self.rows:
            line = f"  {icons[state]} {state:4}  {name}"
            if detail:
                line += f"\n           {detail}"
            lines.append(line)
        return "\n".join(lines)


def _text(node: dict, *path: str) -> str:
    cur: object = node
    for key in path:
        if not isinstance(cur, dict):
            return ""
        cur = cur.get(key)
    if cur is None or isinstance(cur, (dict, list)):
        return ""
    return str(cur).strip()


def _norm(value: str) -> str:
    return " ".join(value.split())


def _blanks(node: object, path: str = "$") -> list[str]:
    """Rutas con string vacio o null dentro del JSON-LD (mala señal para SEO)."""
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            found += _blanks(value, f"{path}.{key}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            found += _blanks(value, f"{path}[{i}]")
    elif node is None or (isinstance(node, str) and not node.strip()):
        found.append(path)
    return found


def run_checks(company: dict, jsonld: dict, deployed: dict | None) -> Result:
    res = Result()

    # --- estructura ---
    res.check("estructura: localBusiness + breadcrumbs", set(jsonld) == {"localBusiness", "breadcrumbs"},
              f"claves: {sorted(jsonld)}")
    lb = jsonld.get("localBusiness") or {}
    bc = jsonld.get("breadcrumbs") or {}

    res.check("localBusiness: @context/@type",
              lb.get("@context") == "https://schema.org" and lb.get("@type") == "LocalBusiness",
              f"@context={lb.get('@context')!r} @type={lb.get('@type')!r}")
    res.check("breadcrumbs: @context/@type",
              bc.get("@context") == "https://schema.org" and bc.get("@type") == "BreadcrumbList",
              f"@context={bc.get('@context')!r} @type={bc.get('@type')!r}")

    # --- campos escalares contra la fuente ---
    name = _text(company, "name")
    res.check("name", lb.get("name") == name, f"jsonld={lb.get('name')!r} company={name!r}")

    expected_desc = _norm(f"{_text(company, 'shortDescription')} {_text(company, 'detailedDescription')}")
    if expected_desc:
        res.check("description = shortDescription + detailedDescription",
                  _norm(str(lb.get("description", ""))) == expected_desc,
                  f"jsonld={str(lb.get('description'))[:70]!r}")
    else:
        res.check("description ausente cuando la company no tiene descripcion", "description" not in lb)

    for field, path in (("telephone", ("contactInfo", "phone")), ("email", ("contactInfo", "email"))):
        source = _text(company, *path)
        if source:
            res.check(f"{field} == contactInfo.{path[1]}", lb.get(field) == source,
                      f"jsonld={lb.get(field)!r} company={source!r}")
        else:
            res.check(f"{field} ausente (company sin {path[1]})", field not in lb)

    website = _text(company, "contactInfo", "website")
    if website:
        res.check("sameAs viene de contactInfo.website", bool(lb.get("sameAs")),
                  f"jsonld={lb.get('sameAs')!r} company={website!r}")

    company_url = _text(company, "url")
    if company_url:
        expected_url = MANTA_PREFIX + company_url
        res.check("url y @id apuntan a la company page",
                  lb.get("url") == expected_url and lb.get("@id") == expected_url,
                  f"url={lb.get('url')!r} @id={lb.get('@id')!r}")

    year = _text(company, "yearEstablished")
    if year:
        res.check("foundingDate == yearEstablished", lb.get("foundingDate") == year,
                  f"jsonld={lb.get('foundingDate')!r} company={year!r}")

    naics = _text(company, "industry", "naics", "code")
    if naics:
        res.check("naics == industry.naics.code", lb.get("naics") == naics,
                  f"jsonld={lb.get('naics')!r} company={naics!r}")
    sic = _text(company, "industry", "sic", "standardCode")
    if sic:
        res.check("isicV4 == industry.sic.standardCode", lb.get("isicV4") == sic,
                  f"jsonld={lb.get('isicV4')!r} company={sic!r}")

    for field in ("logo", "image"):
        if field in lb:
            res.check(f"{field} es una URL absoluta https",
                      str(lb[field]).startswith("https://"), f"{field}={lb[field]!r}")

    # --- address / geo ---
    location = company.get("location") if isinstance(company.get("location"), dict) else {}
    address = lb.get("address") or {}
    if location:
        expected_parts = {
            "streetAddress": _text(location, "address"),
            "addressLocality": _text(location, "city"),
            "addressRegion": _text(location, "stateAbbrv") or _text(location, "state"),
            "postalCode": _text(location, "postalCode"),
        }
        mismatched = {
            key: (address.get(key), value)
            for key, value in expected_parts.items()
            if value and address.get(key) != value
        }
        res.check("address coincide con location", not mismatched, f"difieren: {mismatched}" if mismatched else "")
        res.check("address: @type PostalAddress",
                  address.get("@type") == "PostalAddress" if address else True,
                  f"@type={address.get('@type')!r}")

        lat, lon = _text(location, "latitude"), _text(location, "longitude")
        geo = lb.get("geo") or {}
        if lat and lon:
            ok = (
                geo.get("@type") == "GeoCoordinates"
                and str(geo.get("latitude")) == str(float(lat))
                and str(geo.get("longitude")) == str(float(lon))
            )
            res.check("geo == location.latitude/longitude", ok, f"jsonld={geo} company=({lat}, {lon})")
        else:
            res.check("geo ausente (company sin coordenadas)", "geo" not in lb)

    # --- opening hours ---
    hours = lb.get("openingHoursSpecification")
    if hours is not None:
        hours = hours if isinstance(hours, list) else [hours]
        bad_day = [h for h in hours if h.get("dayOfWeek") not in DAYS]
        bad_time = [
            h for h in hours
            if not (TIME_RE.match(str(h.get("opens", ""))) and TIME_RE.match(str(h.get("closes", ""))))
        ]
        res.check("openingHours: dayOfWeek valido", not bad_day, f"invalidos: {bad_day[:3]}" if bad_day else "")
        res.check("openingHours: opens/closes en HH:MM", not bad_time,
                  f"invalidos: {bad_time[:3]}" if bad_time else "")

        regular = (company.get("businessHours") or {}).get("regularHours") or []
        source_days = {str(d.get("day", "")).capitalize() for d in regular if isinstance(d, dict)}
        extra = {h.get("dayOfWeek") for h in hours} - source_days
        res.check("openingHours: no inventa dias fuera de businessHours.regularHours", not extra,
                  f"dias de mas: {sorted(extra)}" if extra else f"{len(hours)} specs / {len(source_days)} dias")

    # --- reviews / rating ---
    recommendations = company.get("recommendations") if isinstance(company.get("recommendations"), dict) else {}
    rec_list = recommendations.get("list") or []
    agg = recommendations.get("aggregateRating") or {}
    try:
        rating_count = int(str(agg.get("ratingCount", 0)) or 0)
        rating_value = float(str(agg.get("ratingValue", 0)) or 0)
    except ValueError:
        rating_count, rating_value = 0, 0.0
    should_have_agg = bool(rec_list) and rating_count > 0 and rating_value > 0
    res.check("aggregateRating solo cuando hay reviews con rating > 0",
              ("aggregateRating" in lb) == should_have_agg,
              f"en jsonld={'aggregateRating' in lb} esperado={should_have_agg} "
              f"(reviews={len(rec_list)} count={rating_count} value={rating_value})")
    if should_have_agg:
        agg_lb = lb.get("aggregateRating") or {}
        res.check("aggregateRating: reviewCount / ratingValue",
                  str(agg_lb.get("reviewCount")) == str(agg.get("ratingCount")).strip()
                  and str(agg_lb.get("ratingValue")) == str(agg.get("ratingValue")).strip(),
                  f"jsonld={agg_lb}")

    # --- breadcrumbs ---
    crumbs = (company.get("breadCrumbs") or {}).get("list") or []
    items = bc.get("itemListElement") or []
    items = items if isinstance(items, list) else [items]
    res.check("breadcrumbs: un ListItem por crumb", len(items) == len(crumbs),
              f"jsonld={len(items)} company={len(crumbs)}")
    res.check("breadcrumbs: position 1..n consecutivos",
              [str(i.get("position")) for i in items] == [str(i + 1) for i in range(len(items))],
              f"positions={[i.get('position') for i in items]}")
    if len(items) == len(crumbs):
        name_mismatch = [
            (c.get("name"), (i.get("item") or {}).get("name"))
            for c, i in zip(crumbs, items)
            if str(c.get("name") or "").strip() != (i.get("item") or {}).get("name")
        ]
        res.check("breadcrumbs: los nombres coinciden", not name_mismatch,
                  f"difieren: {name_mismatch[:3]}" if name_mismatch else "")
        id_wrong = [
            (c.get("url"), (i.get("item") or {}).get("@id"))
            for c, i in zip(crumbs, items)
            if bool(str(c.get("url") or "").strip()) != ("@id" in (i.get("item") or {}))
        ]
        res.check("breadcrumbs: @id solo en los crumbs con url", not id_wrong,
                  f"difieren: {id_wrong[:3]}" if id_wrong else "")
        bad_prefix = [
            (i.get("item") or {}).get("@id")
            for i in items
            if "@id" in (i.get("item") or {})
            and not str((i.get("item") or {})["@id"]).startswith(MANTA_PREFIX)
        ]
        res.check("breadcrumbs: @id absoluto a manta.com", not bad_prefix, f"raros: {bad_prefix[:3]}")

    # --- calidad general ---
    blanks = _blanks(jsonld)
    res.check("sin strings vacios ni nulls", not blanks, f"{len(blanks)} campos: {blanks[:5]}")

    # --- diff informativo contra el environment ---
    if deployed is None:
        res.add(None, "diff vs environment", "el environment no devolvio jsonld para comparar")
    else:
        same = json.dumps(deployed, sort_keys=True) == json.dumps(jsonld, sort_keys=True)
        if same:
            res.add(True, "diff vs environment: identico")
        else:
            diffs = []
            for key in ("localBusiness", "breadcrumbs"):
                mine, theirs = jsonld.get(key) or {}, (deployed or {}).get(key) or {}
                for field in sorted(set(mine) | set(theirs)):
                    if json.dumps(mine.get(field), sort_keys=True) != json.dumps(theirs.get(field), sort_keys=True):
                        diffs.append(f"{key}.{field}")
            res.add(None, "diff vs environment: hay diferencias",
                    "campos: " + ", ".join(diffs) + " (esperable si el env corre otra version de la lib)")

    return res


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Testea el JSON-LD que genera json-ld-seo para un EMID",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("emid", help="EMID de la compania, ej. m1q16vx")
    parser.add_argument("--env", choices=sorted(ENVIRONMENTS), default="e2e", help="default: e2e")
    parser.add_argument("--base-url", help="Sobreescribe la URL base del environment")
    parser.add_argument("--cookie", help="Cookie completa (default: MANTA_SESSION o la del script)")
    parser.add_argument("--version", default=JSONLD_VERSION, help=f"version de json-ld-seo (default: {JSONLD_VERSION})")
    parser.add_argument("--work-dir", type=Path, default=Path(tempfile.gettempdir()) / "manta-jsonld-test")
    parser.add_argument("--refresh", action="store_true", help="Re-baja el jar aunque ya este en el work dir")
    parser.add_argument("--insecure", action="store_true", help="curl -k")
    parser.add_argument("--print-jsonld", action="store_true", help="Imprime el JSON-LD generado")
    parser.add_argument("--save-company", type=Path, help="Guarda los datos del EMID")
    parser.add_argument("-o", "--output", type=Path, help="Guarda el JSON-LD generado")
    args = parser.parse_args(argv)

    base_url = args.base_url or ENVIRONMENTS[args.env]
    cookie = args.cookie or os.environ.get("MANTA_SESSION") or DEFAULT_MANTA_SESSION

    try:
        log(f"[1/4] Datos de {args.emid} en {args.env} ({base_url})")
        company = fetch_company(args.emid, base_url, cookie, insecure=args.insecure)
        deployed = company.pop("jsonld", None)  # el que ya publica el environment
        log(f"      ok: {company.get('name')!r} ({len(company)} campos)")
        if args.save_company:
            args.save_company.write_text(json.dumps(company, indent=2), encoding="utf-8")
            log(f"      guardado en {args.save_company}")

        log(f"[2/4] Bajando json-ld-seo {args.version} de Nexus")
        classpath = build_classpath(args.version, args.work_dir, args.refresh)

        log("[3/4] Generando el JSON-LD con la libreria")
        jsonld, _ = generate_jsonld(company, classpath=classpath)
    except Failure as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 2

    if args.output:
        args.output.write_text(json.dumps(jsonld, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        log(f"      JSON-LD escrito en {args.output}")

    log("[4/4] Checks")
    result = run_checks(company, jsonld, deployed)
    print(result.render())

    total = len(result.rows)
    failed = result.failed
    warned = sum(1 for state, _, _ in result.rows if state == "WARN")
    print(f"\n{total - failed - warned} pass, {warned} warn, {failed} fail")

    if args.print_jsonld:
        print()
        print(json.dumps(jsonld, indent=2, ensure_ascii=False))

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
